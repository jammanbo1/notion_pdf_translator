import json
import os
import re
import time
import mimetypes
import tempfile
import requests
from dotenv import load_dotenv
from notion_client import Client
import google.generativeai as genai
from playwright.sync_api import sync_playwright

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")

notion = Client(auth=NOTION_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

FALLBACK_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
]


def get_or_create_release(tag="pdf-reports"):
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/tags/{tag}"
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        return res.json()

    create_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases"
    payload = {
        "tag_name": tag,
        "name": "Generated PDF Reports",
        "body": "자동 생성된 PDF 정리본 보관소입니다.",
        "draft": False,
        "prerelease": False,
    }
    create_res = requests.post(create_url, headers=headers, json=payload)
    create_res.raise_for_status()
    return create_res.json()


def upload_pdf_to_github_release(file_path: str, file_name: str) -> str:
    release = get_or_create_release()
    upload_url_template = release["upload_url"]
    upload_url = upload_url_template.replace("{?name,label}", "")

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type": "application/pdf",
    }

    safe_name = f"part_{int(time.time())}_{os.path.basename(file_path)}"
    params = {"name": safe_name}

    with open(file_path, "rb") as f:
        res = requests.post(upload_url, headers=headers, params=params, data=f)
        res.raise_for_status()
        asset_data = res.json()
        return asset_data["browser_download_url"]


def get_data_source_id():
    database = notion.databases.retrieve(database_id=NOTION_DB_ID)
    data_sources = database.get("data_sources", [])
    if not data_sources:
        raise RuntimeError("이 데이터베이스에서 data_source를 찾을 수 없습니다.")
    return data_sources[0]["id"]


def get_unprocessed_items():
    data_source_id = get_data_source_id()
    results = []
    cursor = None

    while True:
        response = notion.data_sources.query(
            data_source_id=data_source_id,
            start_cursor=cursor,
            page_size=100,
        )
        results.extend(response["results"])
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    unprocessed = []
    for page in results:
        props = page.get("properties", {})
        text_link_prop = props.get("내용 요약본", {})
        existing_url = text_link_prop.get("url") if text_link_prop.get("type") == "url" else None

        if not existing_url:
            unprocessed.append(page)

    return unprocessed


def find_supported_attachments(page):
    supported_files = []
    properties = page.get("properties", {})
    allowed_exts = (".pdf", ".png", ".jpg", ".jpeg")

    for prop_value in properties.values():
        if prop_value.get("type") == "files":
            for file_obj in prop_value.get("files", []):
                file_name = file_obj.get("name", "")
                if file_name.lower().endswith(allowed_exts):
                    url = file_obj.get("file", {}).get("url") or file_obj.get(
                        "external", {}
                    ).get("url")
                    supported_files.append({"name": file_name, "url": url})

    return supported_files


def prepare_file_payload(file_list: list) -> list:
    payload = []
    for item in file_list:
        res = requests.get(item["url"], stream=True, timeout=120)
        res.raise_for_status()
        mime_type, _ = mimetypes.guess_type(item["name"])
        if not mime_type:
            mime_type = (
                "application/pdf"
                if item["name"].lower().endswith(".pdf")
                else "image/jpeg"
            )
        payload.append({"mime_type": mime_type, "data": res.content})
    return payload


def plan_content_sections(file_payload: list, subject_hint: str = "", unit_hint: str = "") -> list:
    planning_prompt = f"""당신은 이공계 전공 커리큘럼 설계자입니다.
첨부된 강의 자료(과목: {subject_hint}, 단원명: {unit_hint})의 전체 분량을 검토하고, 한 번에 집중해서 학습할 수 있는 **[독립된 소주제 목록]**으로 내용을 분할하세요.

★ [분할 기준]:
1. 각 파트는 '핵심 개념 1~2개 + 관련 유도 과정 + 연계 예제/코드'가 하나로 완결되도록 묶으세요.
2. 자료 분량이 적으면 1~2개 파트로, 자료가 방대하거나 문제가 많으면 3~5개 파트 이상으로 유연하게 결정하세요.
3. 반드시 아래의 순수 JSON 배열 형식으로만 응답하세요. 다른 설명 문장은 절대 작성하지 마세요.

[응답 JSON 형식 예시]:
[
  {{"part_index": 1, "topic_title": "쿨롱 법칙과 전기장의 중첩 원리", "scope_description": "점전하 분포 및 연속 전하에 의한 전기장 적분 계산"}},
  {{"part_index": 2, "topic_title": "가우스 법칙과 전하 대칭성", "scope_description": "구/원통/평면 대칭성 분석 및 내부/외부 전기장 유도"}}
]
"""
    print("  [단원 분석] -> 강의 자료 구조 및 분할 계획 수립 중...")
    for model_name in FALLBACK_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([planning_prompt] + file_payload)
            clean_json = re.sub(r"^```json\s*|^```\s*|\s*```$", "", response.text.strip(), flags=re.MULTILINE)
            plan = json.loads(clean_json)
            if isinstance(plan, list) and len(plan) > 0:
                print(f"  [단원 분석 완료] 총 {len(plan)}개 파트로 동적 분할 결정.")
                return plan
        except Exception as e:
            print(f"  [경고] {model_name} 계획 수립 실패 ({e}), 재시도 중...")
            time.sleep(1)

    return [{"part_index": 1, "topic_title": f"{unit_hint} 핵심 해설", "scope_description": "단원 전체 내용 심층 해설"}]


def generate_section_commentary(file_payload: list, subject_hint: str, unit_hint: str, section: dict, total_parts: int) -> tuple:
    part_idx = section.get("part_index", 1)
    topic_title = section.get("topic_title", "")
    scope_desc = section.get("scope_description", "")

    prompt_text = f"""당신은 세계 최고 수준의 이공계 전공 수석 해설위원입니다.
첨부된 강의 자료(과목: {subject_hint}, 단원명: {unit_hint})에서 아래 지정된 **[대상 소주제]**에만 100% 토큰을 집중하여 완결된 심층 보충 해설집을 작성하세요.

★ [현재 작성 대상 소주제]:
- 파트: Part {part_idx}/{total_parts}
- 주제: {topic_title}
- 범위 및 세부 내용: {scope_desc}

★ [작성 원칙]:
다른 소주제는 과감히 배제하고, 현재 지정된 소주제의 '원문 공식, 직관, 생략된 행간 유도, 시험 함정, 관련 예제'를 최고 밀도로 서술하세요.

[1단계: 제목 생성]
답변 첫 줄에 반드시 다음 형식으로 출력:
DOC_TITLE: [{subject_hint} - {unit_hint}] (Part {part_idx}. {topic_title})

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요.

★ [5단계 완결 마크업 절대 규칙]
1. [슬라이드 원문 핵심 공식/개념] (파란색 박스 - 기준점)
   <div class="note-box note-blue"><span class="badge badge-blue">원문 공식/개념</span> $$수식$$ <p>(슬라이드 원문 정의 및 의미 요약)</p></div>

2. [도입 배경 & 핵심 직관] (초록색 박스 - Why)
   <div class="note-box note-green"><span class="badge badge-green">도입 배경 & 핵심 직관</span> <p>(기존 한계 및 등장 배경, 물리적/공학적 직관 2~3줄)</p></div>

3. [생략된 행간 유도 & 증명] (빨간색 박스 - How)
   <div class="note-box note-red"><span class="badge badge-red">생략된 행간 복원</span> (Step 1, 2, 3 단계별 수식 전개 및 논리 징검다리 해설)</div>

4. [시험 함정 & 필기 팁] (주황색 박스 - Pitfall)
   <div class="comment-box"><span class="badge badge-orange">시험 함정 & 필기 팁</span> (오개념 주의, N=0 등 경계 조건, 손글씨 필기 복원)</div>

5. [해당 주제 연계 예제 / 코드 트레이싱] (보라색 박스 - Practice, 관련 예제 있을 시)
   <div class="practice-box">
     <div class="practice-header"><span class="badge badge-purple">실전 분석</span> (예제/코드 주제)</div>
     <div class="practice-question"><strong>[문제 상황 / 코드]</strong> (상황 서술 또는 코드 제시)</div>
     <div class="practice-solution">
       <div class="step-label">Step 1. 초기 조건 및 레이아웃 분석</div>
       <p>(경계 조건 또는 포인터/메모리 구조)</p>
       <div class="step-label">Step 2. 실행 흐름 트레이싱 및 계산</div>
       <div class="calc-step">$$ ... $$</div>
       <div class="step-label">Step 3. 결과 해석 및 주의점</div>
     </div>
   </div>

★ [벡터 표기 통일]: 화살표(\\vec{{...}}), 단위벡터(\\hat{{...}}), 볼드체(\\mathbf) 금지.
★ [최하단 치트시트]: <table class="cheat-sheet-table">로 현재 파트의 핵심 공식/복잡도 요약.
"""

    for model_name in FALLBACK_MODELS:
        print(f"  [Part {part_idx}/{total_parts}] -> [{model_name}] 생성 호출 중...")
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([prompt_text] + file_payload, request_options={"timeout": 600})
            raw_text = response.text

            extracted_title = f"Part_{part_idx}_{topic_title}"
            body_html = raw_text

            match = re.search(r"DOC_TITLE:\s*(.+)", raw_text)
            if match:
                extracted_title = match.group(1).strip()
                body_html = re.sub(r"DOC_TITLE:\s*.+\n?", "", raw_text).strip()

            print(f"  [Part {part_idx}/{total_parts}] -> [{model_name}] 생성 완료!")
            return extracted_title, body_html
        except Exception as e:
            print(f"  [경고] {model_name} 실패 ({e}), 백업 모델로 전환...")
            time.sleep(2)
            continue

    raise RuntimeError(f"Part {part_idx} 생성 실패")


def build_full_html(title: str, subtitle: str, content_html: str) -> str:
    clean_html = re.sub(r"^```html\s*|\s*```$", "", content_html.strip(), flags=re.MULTILINE)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<link rel="stylesheet" href="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css)">
<script defer src="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js)"></script>
<script defer src="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js)"
        onload="renderMathInElement(document.body, {{
            delimiters: [
                {{left: '$$', right: '$$', display: true}},
                {{left: '$', right: '$', display: false}}
            ],
            throwOnError: false
        }});"></script>
<style>
  @import url('[https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap](https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap)');
  @page {{ size: A4; margin: 18mm 14mm; }}
  body {{ 
    font-family: 'Pretendard', sans-serif; 
    color: #1E293B; 
    line-height: 1.75; 
    font-size: 13px; 
    margin: 0; 
    background-color: #FFFFFF;
  }}
  
  .header-container {{ 
    border-bottom: 2px solid #0F172A; 
    padding-bottom: 12px; 
    margin-bottom: 22px; 
  }}
  .doc-title {{ 
    font-size: 20px; 
    font-weight: 800; 
    color: #0F172A; 
    margin: 0 0 6px 0; 
    letter-spacing: -0.5px;
  }}
  .doc-subtitle {{ font-size: 12px; color: #64748B; margin: 0; font-weight: 500; }}
  
  h2 {{ 
    font-size: 15.5px; 
    font-weight: 700; 
    color: #0F172A; 
    border-left: 3.5px solid #2563EB; 
    padding-left: 9px; 
    margin-top: 26px; 
    margin-bottom: 12px; 
    letter-spacing: -0.3px;
  }}
  h3 {{ 
    font-size: 13.5px; 
    font-weight: 700; 
    color: #334155; 
    margin-top: 18px; 
    margin-bottom: 8px; 
  }}

  .badge {{
    display: inline-block;
    font-size: 11px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    margin-right: 6px;
    letter-spacing: -0.2px;
    vertical-align: middle;
  }}
  .badge-red {{ background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; }}
  .badge-blue {{ background-color: #EFF6FF; color: #2563EB; border: 1px solid #BFDBFE; }}
  .badge-green {{ background-color: #F0FDF4; color: #16A34A; border: 1px solid #BBF7D0; }}
  .badge-purple {{ background-color: #FAF5FF; color: #7C3AED; border: 1px solid #E9D5FF; }}
  .badge-orange {{ background-color: #FFF7ED; color: #EA580C; border: 1px solid #FFEDD5; }}

  .note-box, .comment-box, .practice-box {{
    break-inside: avoid;
    page-break-inside: avoid;
  }}

  .note-box {{
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 12px 14px;
    margin: 12px 0;
    font-size: 12.5px;
    line-height: 1.65;
  }}
  .note-red {{ border-left: 4px solid #DC2626; background-color: #FEF2F20D; }}
  .note-blue {{ border-left: 4px solid #2563EB; background-color: #EFF6FF0D; }}
  .note-green {{ border-left: 4px solid #16A34A; background-color: #F0FDF40D; }}

  .comment-box {{
    background-color: #FFF7ED;
    border: 1px solid #FFEDD5;
    border-left: 4px solid #EA580C;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 12px 0;
    font-size: 12px;
    line-height: 1.6;
    color: #9A3412;
  }}

  .practice-box {{ 
    background-color: #FFFFFF; 
    border: 1px solid #E9D5FF; 
    border-left: 4px solid #7C3AED; 
    border-radius: 6px; 
    padding: 14px; 
    margin: 18px 0; 
  }}
  .practice-header {{ 
    font-weight: 700; 
    font-size: 13px; 
    color: #5B21B6; 
    margin-bottom: 8px; 
    border-bottom: 1px solid #F3E8FF; 
    padding-bottom: 6px; 
  }}
  .practice-question {{ 
    background-color: #FAF5FF; 
    border: 1px solid #F3E8FF; 
    border-radius: 4px; 
    padding: 10px; 
    margin-bottom: 10px; 
    font-size: 12px; 
    line-height: 1.6; 
  }}
  .practice-solution {{ 
    background-color: #FFFFFF; 
    padding: 4px; 
    font-size: 12px; 
  }}
  .practice-solution .step-label {{ 
    font-weight: 700; 
    color: #7C3AED; 
    margin-top: 8px; 
    margin-bottom: 2px; 
  }}
  .calc-step {{ 
    background-color: #FAF5FF; 
    border: 1px solid #F3E8FF; 
    border-radius: 4px; 
    padding: 8px; 
    margin: 6px 0; 
    text-align: center; 
  }}

  .cheat-sheet-table {{ 
    width: 100%; 
    border-collapse: collapse; 
    margin: 18px 0 10px 0; 
    font-size: 12px; 
  }}
  .cheat-sheet-table th {{ 
    background-color: #0F172A; 
    color: #FFFFFF; 
    font-weight: 600; 
    padding: 8px 10px; 
    border: 1px solid #334155; 
    text-align: center; 
  }}
  .cheat-sheet-table td {{ 
    border: 1px solid #E2E8F0; 
    padding: 8px 10px; 
    text-align: center; 
    background-color: #FFFFFF; 
  }}
  .cheat-sheet-table tr:nth-child(even) td {{ background-color: #F8FAFC; }}
</style>
</head>
<body>
  <div class="header-container">
    <h1 class="doc-title">{title}</h1>
    <p class="doc-subtitle">{subtitle}</p>
  </div>
  {clean_html}
</body>
</html>
"""


def render_html_to_pdf(html_content: str, output_pdf_path: str):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html_content, wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.pdf(
            path=output_pdf_path,
            format="A4",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
        )
        browser.close()


def update_notion_dynamic_results(page: dict, part_links: list):
    page_id = page["id"]
    props = page.get("properties", {})
    
    first_url = part_links[0]["url"] if part_links else ""
    update_data = {"내용 요약본": {"url": first_url}}
    
    photo_prop = props.get("참고 사진", {})
    photo_url = photo_prop.get("url") if photo_prop.get("type") == "url" else None
    
    if photo_url:
        try:
            update_data["상태"] = {"status": {"name": "완료"}}
        except Exception:
            try:
                update_data["상태"] = {"select": {"name": "완료"}}
            except Exception:
                pass

    notion.pages.update(page_id=page_id, properties=update_data)

    # 본문에 N개 파트 북마크 블록 순차 추가
    bookmark_blocks = [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": f"📚 자동 분할 심층 해설집 (총 {len(part_links)}개 파트)"}}]
            }
        }
    ]
    for item in part_links:
        bookmark_blocks.append({
            "object": "block",
            "type": "bookmark",
            "bookmark": {"url": item["url"]}
        })

    try:
        notion.blocks.children.append(block_id=page_id, children=bookmark_blocks)
    except Exception as e:
        print(f"  [알림] 노션 본문 블록 추가 건너뜀: {e}")


def sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", filename).strip().replace(" ", "_")


def main():
    items = get_unprocessed_items()
    if not items:
        print("[동적 분할 해설집] 처리할 새 항목이 없습니다.")
        return

    print(f"[동적 분할 해설집] 미처리 항목 {len(items)}개 발견.")

    with tempfile.TemporaryDirectory() as temp_dir:
        for page in items:
            props = page.get("properties", {})

            subject_hint = ""
            select_prop = props.get("선택", {})
            if select_prop.get("type") == "select" and select_prop.get("select"):
                subject_hint = select_prop["select"].get("name", "")

            unit_hint = ""
            name_prop = props.get("이름", {})
            if name_prop.get("type") == "title" and name_prop.get("title"):
                unit_hint = "".join([t.get("plain_text", "") for t in name_prop["title"]])

            files = find_supported_attachments(page)
            if not files:
                continue

            print(f"\n[작업 시작] 과목: '{subject_hint}', 단원: '{unit_hint}' (파일 {len(files)}개)")

            try:
                file_payload = prepare_file_payload(files)

                # 1. 분량 및 소단원 동적 분석
                sections = plan_content_sections(file_payload, subject_hint, unit_hint)
                total_parts = len(sections)

                part_links = []
                # 2. 파트별 독립 생성 및 업로드 루프
                for idx, section in enumerate(sections, 1):
                    section["part_index"] = idx
                    p_title, p_html = generate_section_commentary(file_payload, subject_hint, unit_hint, section, total_parts)
                    p_full_html = build_full_html(p_title, f"Part {idx}/{total_parts}. {section.get('topic_title', '')}", p_html)
                    
                    pdf_filename = f"{sanitize_filename(p_title)}.pdf"
                    pdf_path = os.path.join(temp_dir, pdf_filename)
                    render_html_to_pdf(p_full_html, pdf_path)

                    pdf_url = upload_pdf_to_github_release(pdf_path, pdf_filename)
                    print(f"  -> Part {idx}/{total_parts} 업로드 완료: {pdf_url}")
                    part_links.append({"title": p_title, "url": pdf_url})
                    time.sleep(1)

                # 3. 노션 등록 (컬럼 + N개 본문 북마크 블록)
                update_notion_dynamic_results(page, part_links)
                print(f"  -> Notion에 총 {total_parts}개 해설집 등록 완료!\n")

            except Exception as e:
                print(f"  -> 최종 실패: {e}\n")


if __name__ == "__main__":
    main()
