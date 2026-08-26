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

    safe_name = f"commentary_{int(time.time())}_{os.path.basename(file_path)}"
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


def extract_and_design_theory(file_list: list, subject_hint: str = "", unit_hint: str = "") -> tuple:
    content_payload = []

    prompt_text = f"""당신은 세계 최고 수준의 이공계 전공 수석 해설위원이자 공식 전공서 편집자입니다.
첨부된 강의 자료(과목: {subject_hint}, 단원명: {unit_hint})를 바탕으로, [원문 공식(기준점) + 도입 배경/직관 + 생략된 행간 유도 + 시험 함정]이 완벽히 대조되는 심층 보충 해설집을 작성하세요.

★ [작성 원칙]:
단순 목차 나열을 배제하고, 슬라이드의 각 핵심 주제마다 반드시 아래 순서대로 1:1 대조 박스를 구성하세요.

[필수 출력 양식 1단계: 제목 생성]
답변 첫 줄에 반드시 다음 형식으로 출력:
DOC_TITLE: [{subject_hint} - {unit_hint}] 강의 보충 심층 해설집

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요.

★ [주제별 5단계 마크업 절대 규칙]
각 핵심 주제(h2, h3)마다 다음 순서로 배치하세요:

1. [슬라이드 원문 핵심 공식/개념] (파란색 박스 - 기준점)
   - 슬라이드에 제시된 핵심 수식이나 정의를 명확하게 제시
   - <div class="note-box note-blue"><span class="badge badge-blue">원문 공식/개념</span> $$수식$$ <p>(슬라이드 원문 정의 및 의미 요약)</p></div>

2. [도입 배경 & 물리적/공학적 직관] (초록색 박스 - Why)
   - "왜 이 공식/자료구조가 필요한가?"에 대한 문제의식, 한계 극복 배경, 직관적 비유 서술 (2~3줄)
   - <div class="note-box note-green"><span class="badge badge-green">도입 배경 & 핵심 직관</span> <p>(기존 방식의 한계와 이 개념이 등장한 필연적 이유, 물리적/공학적 직관 설명)</p></div>

3. [생략된 행간 유도 & 증명] (빨간색 박스 - How)
   - 슬라이드에서 1줄로 축약되거나 건너뛴 중간 계산 단계, 증명 과정, 알고리즘 내부 로직을 Step 1, 2, 3으로 상세히 전개
   - <div class="note-box note-red"><span class="badge badge-red">생략된 행간 복원</span> (단계별 수식 전개 및 논리적 징검다리 해설)</div>

4. [시험 함정 & 손글씨 필기 팁] (주황색 박스 - Pitfall)
   - 빈출 오답 포인트, 인덱스 실수, N=0 등 경계 조건(Corner Cases), 자료 여백의 손글씨/샤프 필기 팁 복원
   - <div class="comment-box"><span class="badge badge-orange">시험 함정 & 필기 팁</span> (오개념 주의, 경계 조건, 필기 복원 메모)</div>

5. [슬라이드 대표 예제 / 코드 트레이싱] (보라색 박스 - Practice, 해당 내용이 있을 때)
   - 슬라이드에 수록된 코드나 예제를 바탕으로 메모리 상태 변화 및 실행 흐름을 줄별로 트레이싱
   <div class="practice-box">
     <div class="practice-header"><span class="badge badge-purple">코드/예제 심층 분석</span> (예제 주제)</div>
     <div class="practice-question"><strong>[분석 대상]</strong> (코드 또는 예제 상황)</div>
     <div class="practice-solution">
       <div class="step-label">Step 1. 동작 원리 및 메모리 레이아웃</div>
       <p>(포인터/배열 상태 및 구조 설명)</p>
       <div class="step-label">Step 2. 실행 흐름 트레이싱</div>
       <p>(변수 변화 및 반복문 전개 과정)</p>
     </div>
   </div>

★ [벡터 및 수식 표기 절대 통일 규칙]
1. 물리/수학 벡터: 볼드체(\\mathbf) 금지, 기호 위에 화살표(\\vec{{...}}) 표기 준수 (예: \\vec{{E}}, \\vec{{B}}, \\vec{{v}}, \\vec{{\\nabla}})
2. 단위 벡터: 윗꺽쇠 표기 (\\hat{{n}}, \\hat{{r}}, \\hat{{x}}, \\hat{{y}}, \\hat{{z}})
3. SVG 코드는 절대 작성하지 마세요. (도판은 별도 생성됨)

★ [단원 핵심 비교 표]
최하단에 <table class="cheat-sheet-table">을 사용하여 단원의 핵심 공식, 시간/공간 복잡도 비교를 표로 깔끔히 마무리하세요.
"""
    content_payload.append(prompt_text)

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
        content_payload.append({"mime_type": mime_type, "data": res.content})

    last_exception = None
    for model_name in FALLBACK_MODELS:
        print(f"  [해설집 생성] -> [{model_name}] 모델 호출 시도 중...")
        try:
            current_model = genai.GenerativeModel(model_name)
            response = current_model.generate_content(
                content_payload, request_options={"timeout": 600}
            )
            raw_text = response.text

            extracted_title = "전공_강의_보충_해설집"
            body_html = raw_text

            match = re.search(r"DOC_TITLE:\s*(.+)", raw_text)
            if match:
                extracted_title = match.group(1).strip()
                body_html = re.sub(r"DOC_TITLE:\s*.+\n?", "", raw_text).strip()

            print(f"  [해설집 생성] -> [{model_name}] 생성 성공!")
            return extracted_title, body_html

        except Exception as e:
            last_exception = e
            print(f"  [경고] {model_name} 실패 (사유: {e})")
            time.sleep(2)
            continue

    raise RuntimeError(f"모든 후보 모델 호출 실패: {last_exception}")


def build_full_html(title: str, content_html: str) -> str:
    clean_html = re.sub(
        r"^```html\s*|\s*```$", "", content_html.strip(), flags=re.MULTILINE
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"
        onload="renderMathInElement(document.body, {{
            delimiters: [
                {{left: '$$', right: '$$', display: true}},
                {{left: '$', right: '$', display: false}}
            ],
            throwOnError: false
        }});"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap');
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
    <p class="doc-subtitle">핵심 행간 복원 및 강의 보충 심층 해설집</p>
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


def update_notion_text_success(page: dict, download_url: str):
    page_id = page["id"]
    props = page.get("properties", {})
    
    update_data = {"내용 요약본": {"url": download_url}}
    
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

    try:
        notion.pages.update(page_id=page_id, properties=update_data)
    except Exception as e:
        print(f"  [오류] Notion 업데이트 실패: {e}")


def sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", filename).strip().replace(" ", "_")


def main():
    items = get_unprocessed_items()
    if not items:
        print("[해설집 생성] 처리할 새 항목이 없습니다.")
        return

    print(f"[해설집 생성] 미처리 항목 {len(items)}개 발견.")

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

            print(f"\n[해설집 작업 시작] 과목: '{subject_hint}', 단원: '{unit_hint}' (파일 {len(files)}개)")

            try:
                doc_title, body_html = extract_and_design_theory(files, subject_hint, unit_hint)
                safe_title = sanitize_filename(doc_title)
                
                full_html = build_full_html(doc_title, body_html)
                temp_pdf_path = os.path.join(temp_dir, f"{safe_title}.pdf")
                render_html_to_pdf(full_html, temp_pdf_path)

                print("  -> GitHub Releases 저장소 업로드 중...")
                pdf_url = upload_pdf_to_github_release(temp_pdf_path, f"{safe_title}.pdf")
                print(f"  -> 다운로드 링크: {pdf_url}")

                update_notion_text_success(page, pdf_url)
                print("  -> Notion '내용 요약본' 등록 완료!\n")

                time.sleep(1)

            except Exception as e:
                print(f"  -> 최종 실패: {e}\n")


if __name__ == "__main__":
    main()
