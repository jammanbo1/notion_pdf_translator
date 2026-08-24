import mimetypes
import os
import re
import tempfile
import time
import google.generativeai as genai
from dotenv import load_dotenv
from notion_client import Client
from playwright.sync_api import sync_playwright
import requests

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")

notion = Client(auth=NOTION_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# --- 이전 코드의 모델 설정을 그대로 유지 ---
FALLBACK_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite"
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

    safe_name = f"{int(time.time())}_{os.path.basename(file_path)}"
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
        status_prop = props.get("상태", {})
        status_type = status_prop.get("type")
        current_status = ""

        if status_type == "status" and status_prop.get("status"):
            current_status = status_prop["status"].get("name", "")
        elif status_type == "select" and status_prop.get("select"):
            current_status = status_prop["select"].get("name", "")

        if current_status not in ["처리완료", "완료"]:
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


def extract_and_design_multiple_files(file_list: list, subject_hint: str = "", unit_hint: str = "") -> tuple:
    content_payload = []
    
    prompt_text = f"""당신은 최고의 대학 이공계열 전공 학업 요약 전문가이자 세계적 물리학/수학 교재의 수석 기술 편집자입니다.
첨부된 자료를 정밀 분석하여, 지정된 4대 컬러 체계(빨간색/파란색/초록색/보라색)와 대학 출판 표준 기하학적 SVG 도판이 완벽히 포함된 최고급 A4 요약 리포트를 작성해주세요.
(참고 과목: {subject_hint}, 단원명: {unit_hint})

[필수 출력 양식 1단계: 제목 생성]
답변 첫 줄에 반드시 다음 형식으로 출력:
DOC_TITLE: [과목/단원 핵심 키워드 중심의 명확한 리포트 제목]

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요. (코드블록 백틱 ```html 은 생략하거나 감싸도 무방)

★ [본문 수식 표기 및 벡터 규격 (HTML KaTeX 렌더링)]
- 화살표 뭉개짐을 방지하기 위해 모든 벡터는 \\vec{{...}} 또는 \\overrightarrow{{...}}를 사용할 것! (예: \\vec{{F}}, \\vec{{r}}, \\vec{{E}}, \\vec{{B}}, \\vec{{v}})
- 단위 벡터는 윗꺽쇠 표기: \\hat{{n}}, \\hat{{r}}, \\hat{{i}}, \\hat{{j}}, \\hat{{k}}
- 미소 벡터 요소: d\\vec{{r}}, d\\vec{{l}}, d\\vec{{S}} = \\hat{{n}} dS

★ [SVG 그래픽 도판 작도 및 표기 규칙 (수식 깨짐 방지 핵심)]
1. 모든 SVG 내부 수식/라벨 표기:
   - SVG 내부 <text> 태그 안에서 $\\vec{{F}}$ 같은 KaTeX 소스 코드나 유니코드 결합 문자를 절대 사용하지 말 것! PDF 렌더링 시 깨짐.
   - **글로벌 대학 교재 표준 볼드 이탤릭체(Bold Italic)**로 작성할 것! 폰트 어긋남 0%.
   - 벡터량: <tspan font-style="italic" font-weight="bold">F</tspan>, <tspan font-style="italic" font-weight="bold">r</tspan>, <tspan font-style="italic" font-weight="bold">E</tspan>
   - 단위 벡터: <tspan font-style="italic" font-weight="bold">n̂</tspan>, <tspan font-style="italic" font-weight="bold">r̂</tspan>
   - 스칼라/좌표: 이탤릭 (<tspan font-style="italic">x</tspan>, <tspan font-style="italic">y</tspan>, <tspan font-style="italic">z</tspan>)

2. 기하학적 엄밀성 (Mathematical Rigor):
   - 스타일 테마: 깔끔한 **모노크롬(흑백) 출판 스타일**(`#0f172a`, 배경 `#f8fafc`, 점선 `#94a3b8`).
   - 모든 경로(path)는 원점과 스케일을 정하고 **실제 함수 수식 수치 샘플링(Numerical Sampling)**을 기반으로 생성할 것. 제어점 임의 추정 금지.
   - 닫힌 곡선/곡면 작도 시 모서리 꺾임(Cusp)이 없도록 **접선 벡터가 연속인 $C^1$ 매끄러운 스무딩** 적용.

★ [마크업 및 테이블 규격]
- 모든 SVG는 <div class="svg-container"><svg ...>...</svg><p class="caption">그림 X. 설명</p></div> 형식으로 작성. 최소 3~4개 필수 삽입.
- 최하단에 <table class="cheat-sheet-table">로 핵심 공식 및 정리를 성질별로 집대성할 것.
"""
    content_payload.append(prompt_text)

    # --- 이전 코드의 requests 사용 방식을 유지 ---
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
        print(f"  -> [{model_name}] 모델로 분석 및 리포트 렌더링 시도 중...")
        try:
            current_model = genai.GenerativeModel(model_name)
            response = current_model.generate_content(
                content_payload, request_options={"timeout": 600}
            )
            raw_text = response.text
            
            extracted_title = "전공_핵심_요약_리포트"
            body_html = raw_text
            
            match = re.search(r"DOC_TITLE:\s*(.+)", raw_text)
            if match:
                extracted_title = match.group(1).strip()
                body_html = re.sub(r"DOC_TITLE:\s*.+\n?", "", raw_text).strip()
                
            print(f"  -> [{model_name}] 생성 성공!")
            return extracted_title, body_html

        except Exception as e:
            last_exception = e
            err_msg = str(e)
            print(f"  [경고] {model_name} 실패 (사유: {err_msg})")
            time.sleep(2)
            continue

    raise RuntimeError(f"모든 후보 모델 호출 실패: {last_exception}")


def build_full_html(title: str, content_html: str) -> str:
    clean_html = re.sub(
        r"^```html\s*|\s*```$", "", content_html.strip(), flags=re.MULTILINE
    )

    # --- KaTeX 설정 보완: 화살표 매크로 설정을 강제하여 유지 ---
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
            macros: {{
                '\\\\vec': '\\\\overrightarrow',
                '\\\\oiint': '\\\\oint\\\\mkern-13mu\\\\oint'
            }},
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
  
  /* 헤더 섹션 */
  .header-container {{ 
    border-bottom: 2px solid #0F172A; 
    padding-bottom: 12px; 
    margin-bottom: 22px; 
  }}
  .doc-title {{ 
    font-size: 21px; 
    font-weight: 800; 
    color: #0F172A; 
    margin: 0 0 6px 0; 
    letter-spacing: -0.5px;
  }}
  .doc-subtitle {{ font-size: 12px; color: #64748B; margin: 0; font-weight: 500; }}
  
  /* 제목 태그 */
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

  /* 뱃지 및 노트 박스 시스템 */
  .badge {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 4px; margin-right: 6px; letter-spacing: -0.2px; vertical-align: middle; }}
  .badge-red {{ background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; }}
  .badge-blue {{ background-color: #EFF6FF; color: #2563EB; border: 1px solid #BFDBFE; }}
  .badge-green {{ background-color: #F0FDF4; color: #16A34A; border: 1px solid #BBF7D0; }}
  .badge-purple {{ background-color: #FAF5FF; color: #7C3AED; border: 1px solid #E9D5FF; }}

  .note-box {{ background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 6px; padding: 12px 14px; margin: 12px 0; font-size: 12.5px; line-height: 1.65; }}
  .note-red {{ border-left: 4px solid #DC2626; background-color: #FEF2F20D; }}
  .note-blue {{ border-left: 4px solid #2563EB; background-color: #EFF6FF0D; }}
  .note-green {{ border-left: 4px solid #16A34A; background-color: #F0FDF40D; }}
  .note-purple {{ border-left: 4px solid #7C3AED; background-color: #FAF5FF0D; }}

  /* SVG 다이어그램 컨테이너 */
  .svg-container {{ 
    text-align: center; 
    margin: 20px 0; 
    background-color: #FFFFFF; 
    border: 1px solid #E2E8F0; 
    border-radius: 8px; 
    padding: 14px; 
    overflow: hidden; 
  }}
  .svg-container svg {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}
  .caption {{ font-size: 11.5px; color: #64748B; font-weight: 600; margin-top: 8px; text-align: center; }}

  /* 학습 점검 실전 예제 (Practice Box) */
  .practice-box {{ background-color: #FFFFFF; border: 1px solid #E9D5FF; border-left: 4px solid #7C3AED; border-radius: 6px; padding: 15px; margin: 24px 0; }}
  .practice-header {{ font-weight: 700; font-size: 13.5px; color: #5B21B6; margin-bottom: 10px; border-bottom: 1px solid #F3E8FF; padding-bottom: 6px; }}
  .practice-question {{ background-color: #FAF5FF; border: 1px solid #F3E8FF; border-radius: 4px; padding: 11px; margin-bottom: 10px; font-size: 12.5px; line-height: 1.6; }}
  .practice-solution {{ background-color: #FFFFFF; padding: 6px 4px; font-size: 12px; }}
  .calc-step {{ background-color: #FAF5FF; border: 1px solid #F3E8FF; border-radius: 4px; padding: 8px; margin: 4px 0 8px 0; text-align: center; }}

  /* 치트시트 테이블 */
  .cheat-sheet-table {{ width: 100%; border-collapse: collapse; margin: 20px 0 10px 0; font-size: 12px; }}
  .cheat-sheet-table th {{ background-color: #0F172A; color: #FFFFFF; font-weight: 600; padding: 8px 10px; border: 1px solid #334155; text-align: center; }}
  .cheat-sheet-table td {{ border: 1px solid #E2E8F0; padding: 8px 10px; text-align: center; background-color: #FFFFFF; }}
</style>
</head>
<body>
  <div class="header-container">
    <h1 class="doc-title">{title}</h1>
    <p class="doc-subtitle">핵심 요약 및 개념 정리 리포트</p>
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
            margin={
                "top": "15mm",
                "bottom": "15mm",
                "left": "15mm",
                "right": "15mm",
            },
        )
        browser.close()


def update_notion_success(page_id: str, download_url: str):
    update_data = {"정리본 링크": {"url": download_url}}
    try:
        update_data["상태"] = {"status": {"name": "완료"}}
        notion.pages.update(page_id=page_id, properties=update_data)
    except Exception:
        try:
            update_data["상태"] = {"select": {"name": "완료"}}
            notion.pages.update(page_id=page_id, properties=update_data)
        except Exception:
            notion.pages.update(
                page_id=page_id,
                properties={"정리본 링크": {"url": download_url}},
            )


def sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", filename).strip().replace(" ", "_")


def main():
    items = get_unprocessed_items()
    if not items:
        print("처리할 새 파일이 없습니다.")
        return

    print(f"새 미처리 항목 {len(items)}개 발견.")

    with tempfile.TemporaryDirectory() as temp_dir:
        for page in items:
            page_id = page["id"]
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

            print(f"분석 시작 (과목: '{subject_hint}', 단원명: '{unit_hint}', 첨부파일 {len(files)}개)...")

            try:
                # 제미나이 모델이 SVG 내부 수식을 그리지 않고 텍스트/표로만 요약하도록 프롬프트가 보완됨
                doc_title, body_html = extract_and_design_multiple_files(files, subject_hint, unit_hint)
                
                safe_title = sanitize_filename(doc_title)
                print(f"  -> PDF 리포트 제목/파일명 생성: {doc_title}")

                full_html = build_full_html(doc_title, body_html)
                temp_pdf_path = os.path.join(temp_dir, f"{safe_title}.pdf")
                render_html_to_pdf(full_html, temp_pdf_path)

                print("  -> GitHub Storage에 업로드 중...")
                pdf_url = upload_pdf_to_github_release(temp_pdf_path, f"{safe_title}.pdf")
                print(f"  -> 다운로드 링크: {pdf_url}")

                update_notion_success(page_id, pdf_url)
                print("  -> Notion 업데이트 완료 (링크 등록 완료)!\n")

                time.sleep(1)

            except Exception as e:
                print(f"  -> 최종 실패: {e}\n")


if __name__ == "__main__":
    main()
