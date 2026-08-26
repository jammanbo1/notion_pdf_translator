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
        "body": "자동 생성된 전공 PDF 심층 해설집 보관소입니다.",
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

    safe_name = f"doc_{int(time.time())}_{os.path.basename(file_path)}"
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

def plan_balanced_chunks(file_payload: list, subject_hint: str = "", unit_hint: str = "") -> list:
    planning_prompt = f"""당신은 이공계 전공 강의 교재 전문 기획자입니다.
첨부된 강의 자료(과목: {subject_hint}, 단원명: {unit_hint})의 전체 내용을 분석하여, 각 파트가 8,192 토큰 한도 내에서 100% 깊이 있게 해설될 수 있도록 **[최적 분할 계획]**을 수립하세요.

★ [엄격한 분할 용량 규칙]:
1. 1개 파트(Part)당 용량 합계는 반드시 **[핵심 개념 2~3개 + 예제/과제(H.W.) 1~2개] (총합 최대 4개 이하)**로 제한하세요.
2. 예시:
   - 개념 2개 + 예제 2개 = 1개 파트 (적정)
   - 개념 3개 + 예제 1개 = 1개 파트 (적정)
   - 순수 문제/H.W.만 있는 구간: 문제 3~4개 = 1개 파트 (적정)
3. 전체 슬라이드에 개념이 많다면 규칙에 맞춰 여러 파트로 균등하게 쪼개야 합니다.
4. 반드시 아래 JSON 배열 형식으로만 출력하세요. (추가 설명 금지)
"""
    print("  [1단계: 단원 구조 스캔] -> 개념 및 예제/H.W. 수량 기반 안전 분할 계획 수립 중...")
    for model_name in FALLBACK_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([planning_prompt] + file_payload)
            clean_json = re.sub(r"^```json\s*|^```\s*|\s*```$", "", response.text.strip(), flags=re.MULTILINE)
            plan = json.loads(clean_json)
            if isinstance(plan, list) and len(plan) > 0:
                print(f"  [분할 계획 확정] 총 {len(plan)}개 독립 파트로 안전 분할 완료.")
                return plan
        except Exception as e:
            print(f"  [경고] {model_name} 계획 수립 실패 ({e}), 백업 모델 시도...")
            time.sleep(1)

    return [{"part_index": 1, "part_title": f"{unit_hint} 핵심 해설", "concepts": ["핵심 이론"], "examples": ["대표 예제"]}]

def generate_part_html(file_payload: list, subject_hint: str, unit_hint: str, chunk_info: dict, total_parts: int) -> tuple:
    part_idx = chunk_info.get("part_index", 1)
    part_title = chunk_info.get("part_title", "")
    concepts = chunk_info.get("concepts", [])
    examples = chunk_info.get("examples", [])

    prompt_text = f"""당신은 세계 최고 수준의 이공계 전공 수석 해설위원이자 공식 전공서 편집자입니다.
첨부된 강의 자료(과목: {subject_hint}, 단원명: {unit_hint})에서 아래 **[지정된 파트 내용]**에만 100% 토큰을 집중하여 완결된 심층 보충 해설집을 작성하세요.

★ [현재 작성 대상 파트 ({part_idx}/{total_parts})]:
- 파트 제목: {part_title}
- 담당 핵심 개념 목록: {json.dumps(concepts, ensure_ascii=False)}
- 담당 예제/H.W./코드 목록: {json.dumps(examples, ensure_ascii=False)}

★ [작성 원칙]:
1. 지정되지 않은 다른 소주제는 과감히 생략하고, 오직 위 목록의 개념과 예제에 모든 분량을 쏟아부으세요.
2. 유도 과정(Step 1, 2, 3)과 문제 풀이는 중간 생략 없이 수식($\LaTeX$)과 논리를 빈틈없이 전개하세요.
3. 수식 기호를 쓸 때 달러($) 기호 앞에 절대로 역슬래시(\\)를 붙이지 마세요. 순수하게 $$ 공식 $$ 형태로만 작성하세요.

[1단계: 제목 생성]
답변 첫 줄에 반드시 다음 형식으로 출력:
DOC_TITLE: [{subject_hint} - {unit_hint}] (Part {part_idx}. {part_title})

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요.

★ [주제별 5단계 완결 마크업 절대 규칙]
다루는 각 개념마다:
1. [원문 공식/개념]: <div class="note-box note-blue"><span class="badge badge-blue">원문 공식/개념</span> $$수식$$ <p>요약</p></div>
2. [도입 배경 & 핵심 직관]: <div class="note-box note-green"><span class="badge badge-green">도입 배경 & 핵심 직관</span> <p>물리적/공학적 직관</p></div>
3. [생략된 행간 유도 & 증명]: <div class="note-box note-red"><span class="badge badge-red">생략된 행간 복원</span> (Step별 수식 전개)</div>
4. [시험 함정 & 필기 팁]: <div class="comment-box"><span class="badge badge-orange">시험 함정 & 필기 팁</span> (오개념 주의)</div>
5. [실전 예제 / 코드 트레이싱]: <div class="practice-box">
     <div class="practice-header"><span class="badge badge-purple">실전 분석</span> 문제 제목</div>
     <div class="practice-question"><strong>[문제 상황]</strong></div>
     <div class="practice-solution">
       <div class="step-label">Step 1. 물리적 조건 분석</div>
       <div class="step-label">Step 2. 트레이싱 및 계산</div><div class="calc-step">$$ ... $$</div>
       <div class="step-label">Step 3. 결과 해석</div>
     </div>
   </div>

★ [최하단 치트시트]: <table class="cheat-sheet-table">로 현재 파트의 핵심 공식 요약.
"""

    for model_name in FALLBACK_MODELS:
        print(f"    -> [{model_name}] Part {part_idx}/{total_parts} 호출 중...")
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([prompt_text] + file_payload, request_options={"timeout": 600})
            
            raw_text = response.text
            extracted_title = f"Part_{part_idx}_{part_title}"
            body_html = raw_text

            match = re.search(r"DOC_TITLE:\s*(.+)", raw_text)
            if match:
                extracted_title = match.group(1).strip()
                body_html = re.sub(r"DOC_TITLE:\s*.+\n?", "", raw_text).strip()

            return extracted_title, body_html

        except Exception as e:
            print(f"    [경고] {model_name} 실패 ({e}), 백업 모델로 전환...")
            time.sleep(2)
            continue

    raise RuntimeError(f"Part {part_idx} 생성 최종 실패")

def build_full_html(title: str, subtitle: str, content_html: str) -> str:
    # 1. AI가 오류로 붙인 하나 이상의 역슬래시(\$)를 모두 순수한 $ 기호로 강제 복구 (정규표현식 적용)
    clean_html = re.sub(r'\\+\$', '$', content_html)
    clean_html = re.sub(r"^```html\s*|\s*```$", "", clean_html.strip(), flags=re.MULTILINE)

    # 2. MathJax가 수식 오류를 무시하고 렌더링을 끝마치도록 'noerrors' 패키지 추가 및 오류 캐치 로직 적용
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<script>
  window.MathJax = {{
    loader: {{load: ['[tex]/noerrors']}},
    tex: {{
      packages: {{'[+]': ['noerrors']}},
      inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
      displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
      processEscapes: true
    }},
    startup: {{
      pageReady: () => {{
        return MathJax.startup.defaultPageReady().then(() => {{
          const flag = document.createElement('div');
          flag.id = 'math-rendered-flag';
          document.body.appendChild(flag);
        }}).catch((err) => {{
          console.log("MathJax 에러 무시:", err);
          const flag = document.createElement('div');
          flag.id = 'math-rendered-flag';
          document.body.appendChild(flag);
        }});
      }}
    }}
  }};
</script>
<script id="MathJax-script" async src="[https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js](https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js)"></script>
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
  .header-container {{ border-bottom: 2px solid #0F172A; padding-bottom: 12px; margin-bottom: 22px; }}
  .doc-title {{ font-size: 19px; font-weight: 800; color: #0F172A; margin: 0 0 6px 0; letter-spacing: -0.5px; }}
  .doc-subtitle {{ font-size: 12px; color: #64748B; margin: 0; font-weight: 500; }}
  h2 {{ font-size: 15px; font-weight: 700; color: #0F172A; border-left: 3.5px solid #2563EB; padding-left: 9px; margin-top: 24px; margin-bottom: 12px; }}
  .badge {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 4px; margin-right: 6px; letter-spacing: -0.2px; vertical-align: middle; }}
  .badge-red {{ background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; }}
  .badge-blue {{ background-color: #EFF6FF; color: #2563EB; border: 1px solid #BFDBFE; }}
  .badge-green {{ background-color: #F0FDF4; color: #16A34A; border: 1px solid #BBF7D0; }}
  .badge-purple {{ background-color: #FAF5FF; color: #7C3AED; border: 1px solid #E9D5FF; }}
  .badge-orange {{ background-color: #FFF7ED; color: #EA580C; border: 1px solid #FFEDD5; }}
  .note-box, .comment-box, .practice-box {{ break-inside: avoid; page-break-inside: avoid; }}
  .note-box {{ background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 6px; padding: 12px 14px; margin: 12px 0; font-size: 12.5px; line-height: 1.65; }}
  .note-red {{ border-left: 4px solid #DC2626; background-color: #FEF2F20D; }}
  .note-blue {{ border-left: 4px solid #2563EB; background-color: #EFF6FF0D; }}
  .note-green {{ border-left: 4px solid #16A34A; background-color: #F0FDF40D; }}
  .comment-box {{ background-color: #FFF7ED; border: 1px solid #FFEDD5; border-left: 4px solid #EA580C; border-radius: 6px; padding: 10px 12px; margin: 12px 0; font-size: 12px; line-height: 1.6; color: #9A3412; }}
  .practice-box {{ background-color: #FFFFFF; border: 1px solid #E9D5FF; border-left: 4px solid #7C3AED; border-radius: 6px; padding: 14px; margin: 16px 0; }}
  .practice-header {{ font-weight: 700; font-size: 13px; color: #5B21B6; margin-bottom: 8px; border-bottom: 1px solid #F3E8FF; padding-bottom: 6px; }}
  .practice-question {{ background-color: #FAF5FF; border: 1px solid #F3E8FF; border-radius: 4px; padding: 10px; margin-bottom: 10px; font-size: 12px; line-height: 1.6; }}
  .practice-solution {{ background-color: #FFFFFF; padding: 4px; font-size: 12px; }}
  .practice-solution .step-label {{ font-weight: 700; color: #7C3AED; margin-top: 8px; margin-bottom: 2px; }}
  .calc-step {{ background-color: #FAF5FF; border: 1px solid #F3E8FF; border-radius: 4px; padding: 8px; margin: 6px 0; text-align: center; }}
  .cheat-sheet-table {{ width: 100%; border-collapse: collapse; margin: 16px 0 8px 0; font-size: 12px; }}
  .cheat-sheet-table th {{ background-color: #0F172A; color: #FFFFFF; font-weight: 600; padding: 8px 10px; border: 1px solid #334155; text-align: center; }}
  .cheat-sheet-table td {{ border: 1px solid #E2E8F0; padding: 8px 10px; text-align: center; background-color: #FFFFFF; }}
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
        
        try:
            # 수식 렌더링이 완료될 때까지 안전하게 대기
            page.wait_for_selector("#math-rendered-flag", timeout=15000)
        except Exception as e:
            print("    [경고] 수식 렌더링 타임아웃. 강제로 3초 추가 대기 후 인쇄합니다.")
            page.wait_for_timeout(3000)
            
        page.wait_for_timeout(500)
        page.pdf(
            path=output_pdf_path,
            format="A4",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
        )
        browser.close()

def update_notion_results(page: dict, pdf_results: list):
    page_id = page["id"]
    props = page.get("properties", {})
    
    first_url = pdf_results[0]["url"] if pdf_results else ""
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

    bookmark_blocks = [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": f"📚 전공 심층 해설집 (총 {len(pdf_results)}개 파트 완결)"}}]
            }
        }
    ]
    for item in pdf_results:
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
        print("[심층 해설집 파이프라인] 처리할 새 항목이 없습니다.")
        return

    print(f"[심층 해설집 파이프라인] 미처리 항목 {len(items)}개 발견.")

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

            print(f"\n========================================================")
            print(f"[작업 시작] 과목: '{subject_hint}', 단원: '{unit_hint}' (파일 {len(files)}개)")
            print(f"========================================================")

            try:
                file_payload = prepare_file_payload(files)

                # 1. 내용 기반 똑똑한 분할 로직 실행
                chunks = plan_balanced_chunks(file_payload, subject_hint, unit_hint)
                total_parts = len(chunks)

                pdf_results = []
                # 2. 파트별 독립 생성 및 PDF 렌더링 루프
                for idx, chunk in enumerate(chunks, 1):
                    chunk["part_index"] = idx
                    print(f"\n  [Part {idx}/{total_parts}] '{chunk.get('part_title')}' 생성 시작...")

                    p_title, p_html = generate_part_html(file_payload, subject_hint, unit_hint, chunk, total_parts)
                    p_full_html = build_full_html(p_title, f"Part {idx}/{total_parts}. {chunk.get('part_title')}", p_html)
                    
                    pdf_filename = f"{sanitize_filename(p_title)}.pdf"
                    pdf_path = os.path.join(temp_dir, pdf_filename)
                    render_html_to_pdf(p_full_html, pdf_path)

                    pdf_url = upload_pdf_to_github_release(pdf_path, pdf_filename)
                    print(f"  ✅ [Part {idx}/{total_parts}] 업로드 완료: {pdf_url}")
                    pdf_results.append({"title": p_title, "url": pdf_url})

                    time.sleep(1)

                # 3. 노션 데이터베이스 및 본문 북마크 업데이트
                update_notion_results(page, pdf_results)
                print(f"\n  🎉 Notion 등록 완판! (총 {len(pdf_results)}개 파트 생성 완료)\n")

            except Exception as e:
                print(f"  ❌ 최종 처리 실패: {e}\n")

if __name__ == "__main__":
    main()
