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

# 3.6 소진/실패 시 3.7 플래시로 자동 폴백
FALLBACK_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
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
        "body": "자동 생성된 PDF 전공 요약 리포트 보관소입니다.",
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
첨부된 자료를 정밀 분석하여, 지정된 4대 컬러 체계(빨간색/파란색/초록색/보라색)와 모노크롬 인라인 SVG 도판이 완벽히 포함된 최고급 A4 요약 리포트를 작성해주세요.
(참고 과목: {subject_hint}, 단원명: {unit_hint})

[필수 출력 양식 1단계: 제목 생성]
답변 첫 줄에 반드시 다음 형식으로 출력:
DOC_TITLE: [과목 및 단원 핵심 키워드 중심의 명확한 리포트 제목 (특수문자/밑줄 없이 깔끔한 한글/영어 제목)]

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요. (코드블록 백틱 ```html 은 생략하거나 감싸도 무방)

★ [본문 텍스트 절대 규칙: 한자 사용 금지]
본문의 모든 텍스트에서 한자(漢字)를 절대로 사용하지 마세요. 모든 용어는 한글 전용 표기로 바꾸어야 합니다.

★ [LaTeX 수식 절대 규칙 (문법 오류 및 이스케이프 금지)]
1. 인라인 수식은 반드시 $수식$ 형태로만 작성하세요. 절대 \\$ 또는 $\\$ 형태로 작성하지 마세요!
2. 블록 수식은 반드시 $$수식$$ 형태로만 작성하세요.
3. 벡터는 \\vec{{v}}, \\vec{{E}}, \\vec{{B}}, \\vec{{A}}, \\vec{{r}}, 단위벡터는 \\hat{{n}}, \\hat{{r}}, \\hat{{i}}, \\hat{{j}}, \\hat{{k}}, \\hat{{\\phi}} 를 사용하세요.

★ [4대 전용 컬러 배정 및 마크업 통일 규칙]
1. 빨간색 (Red) -> [중요], 핵심 공식 유도:
   - <div class="note-box note-red"><span class="badge badge-red">중요</span> ...</div>
2. 파란색 (Blue) -> [핵심 개념], [학습 목표]:
   - <div class="note-box note-blue"><span class="badge badge-blue">학습 목표</span> ...</div>
3. 초록색 (Green) -> [직관 비유], [해석 팁]:
   - <div class="note-box note-green"><span class="badge badge-green">직관 비유</span> ...</div>
4. 보라색 (Purple) -> [학습 점검], [실전 예제]:
   <div class="practice-box">
     <div class="practice-header"><span class="badge badge-purple">학습 점검</span> 실전 기출/적용 예제</div>
     <div class="practice-question"><strong>[문제]</strong> ...</div>
     <div class="practice-solution">
       <div class="step-label">Step 1. 문제 모델링 및 핵심 공식 수립</div>
       <p>...</p>
       <div class="step-label">Step 2. 수식 전개 과정</div>
       <div class="calc-step">$$ ... $$</div>
       <div class="step-label">Step 3. 결과 해석 및 함정 방어</div>
       <p>...</p>
     </div>
   </div>

★ [전공 표준 SVG 그래픽 도판 작도 절대 규칙 (최소 2~3개 필수 삽입)]
- 반드시 아래 구조로 감싸서 작성:
  <div class="svg-container">
    <svg viewBox="0 0 520 320" xmlns="[http://www.w3.org/2000/svg](http://www.w3.org/2000/svg)" style="background-color: #f8fafc; font-family: 'Pretendard', sans-serif;">
      <!-- 다이어그램 내용 -->
    </svg>
    <p class="caption">그림 X. [도해에 대한 명확한 한글 캡션]</p>
  </div>
- ★ [SVG 색상 엄격 통제 절대 규칙]: 
  * 파란색, 빨간색, 초록색 등 어떠한 원색이나 임의의 컬러(stroke 또는 fill)도 **절대 사용 금지**합니다.
  * 오직 지정된 모노크롬 팔레트 컬러만 사용하세요:
    - 메인 선, 벡터 및 화살표: #0f172a (다크 네이비/블랙)
    - 배경색: #f8fafc (연한 슬레이트)
    - 보조선 및 점선: #94a3b8 (그레이)
    - 텍스트 및 라벨: #475569 (차콜 그레이)
    - 도형 내부 채움(필요시): #ffffff 또는 #e2e8f0
- SVG 내부 텍스트에는 LaTeX($..$)를 쓰지 말고 순수 텍스트/tspan만 사용할 것. (예: <tspan font-style="italic">x</tspan>)
- 수치 샘플링(Numerical Sampling) 기반 C^1 매끄러운 곡선 적용.
- 도판 내 한자(漢字) 사용 절대 금지.

★ [시험 대비 종합 치트시트 테이블]
최하단에 아래와 같이 3개 컬럼 테이블로 핵심 요약:
<table class="cheat-sheet-table">
  <thead>
    <tr>
      <th style="width: 22%;">구분 / 정리명</th>
      <th style="width: 38%;">수학적 공식 (LaTeX)</th>
      <th style="width: 40%;">물리적 의미 및 핵심 노트</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>가우스 발산 정리</td>
      <td>$$\\int_V (\\nabla \\cdot \\vec{{V}}) d\\tau = \\oint_S \\vec{{V}} \\cdot d\\vec{{a}}$$</td>
      <td>체적 내부 생성 유량의 총합은 경계면 순 유출량과 같음</td>
    </tr>
  </tbody>
</table>
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
    clean_html = re.sub(r"^```html\s*|\s*```$", "", content_html.strip(), flags=re.MULTILINE)

    # LaTeX 이중 이스케이프 깨짐 완벽 보정 (\$ -> $, \$\$ -> $$)
    clean_html = re.sub(r'\\\$', '$', clean_html)
    clean_html = re.sub(r'\\\\\(', '(', clean_html)
    clean_html = re.sub(r'\\\\\)', ')', clean_html)

    clean_title = re.sub(r'[_.]+', ' ', title).strip()

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{clean_title}</title>
<link rel="stylesheet" href="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css)">
<script src="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js)"></script>
<script src="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js)"></script>
<style>
  @import url('[https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap](https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap)');
  @page {{ size: A4; margin: 15mm 12mm; }}
  body {{ 
    font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, sans-serif; 
    color: #1E293B; 
    line-height: 1.7; 
    font-size: 12.5px; 
    margin: 0; 
    background-color: #FFFFFF;
  }}
  
  .header-container {{ 
    border-bottom: 2px solid #0F172A; 
    padding-bottom: 10px; 
    margin-bottom: 18px; 
  }}
  .doc-title {{ 
    font-size: 20px; 
    font-weight: 800; 
    color: #0F172A; 
    margin: 0 0 4px 0; 
    letter-spacing: -0.5px;
  }}
  .doc-subtitle {{ font-size: 11.5px; color: #64748B; margin: 0; font-weight: 500; }}
  
  h2 {{ 
    font-size: 14.5px; 
    font-weight: 700; 
    color: #0F172A; 
    border-left: 3.5px solid #2563EB; 
    padding-left: 8px; 
    margin-top: 22px; 
    margin-bottom: 10px; 
    letter-spacing: -0.3px;
  }}
  h3 {{ 
    font-size: 13px; 
    font-weight: 700; 
    color: #334155; 
    margin-top: 16px; 
    margin-bottom: 6px; 
  }}

  .badge {{
    display: inline-block;
    font-size: 10.5px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 4px;
    margin-right: 5px;
    letter-spacing: -0.2px;
    vertical-align: middle;
    white-space: nowrap;
  }}
  .badge-red {{ background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; }}
  .badge-blue {{ background-color: #EFF6FF; color: #2563EB; border: 1px solid #BFDBFE; }}
  .badge-green {{ background-color: #F0FDF4; color: #16A34A; border: 1px solid #BBF7D0; }}
  .badge-purple {{ background-color: #FAF5FF; color: #7C3AED; border: 1px solid #E9D5FF; }}

  .note-box {{
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 10px 0;
    font-size: 12px;
    line-height: 1.6;
  }}
  .note-red {{ border-left: 4px solid #DC2626; background-color: #FEF2F20D; }}
  .note-blue {{ border-left: 4px solid #2563EB; background-color: #EFF6FF0D; }}
  .note-green {{ border-left: 4px solid #16A34A; background-color: #F0FDF40D; }}
  .note-purple {{ border-left: 4px solid #7C3AED; background-color: #FAF5FF0D; }}

  .svg-container {{ 
    text-align: center; 
    margin: 16px 0; 
    background-color: #FFFFFF; 
    border: 1px solid #E2E8F0; 
    border-radius: 6px; 
    padding: 12px; 
    overflow: hidden; 
  }}
  .svg-container svg {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}
  .caption {{ font-size: 11px; color: #64748B; font-weight: 600; margin-top: 6px; text-align: center; }}

  .practice-box {{ 
    background-color: #FFFFFF; 
    border: 1px solid #E9D5FF; 
    border-left: 4px solid #7C3AED; 
    border-radius: 6px; 
    padding: 12px; 
    margin: 18px 0; 
  }}
  .practice-header {{ 
    font-weight: 700; 
    font-size: 13px; 
    color: #5B21B6; 
    margin-bottom: 8px; 
    border-bottom: 1px solid #F3E8FF; 
    padding-bottom: 4px; 
  }}
  .practice-question {{ 
    background-color: #FAF5FF; 
    border: 1px solid #F3E8FF; 
    border-radius: 4px; 
    padding: 9px; 
    margin-bottom: 8px; 
    font-size: 12px; 
    line-height: 1.55; 
  }}
  .practice-solution {{ 
    background-color: #FFFFFF; 
    padding: 4px; 
    font-size: 11.5px; 
  }}
  .practice-solution .step-label {{ 
    font-weight: 700; 
    color: #7C3AED; 
    margin-top: 6px; 
    margin-bottom: 2px; 
  }}
  .calc-step {{ 
    background-color: #FAF5FF; 
    border: 1px solid #F3E8FF; 
    border-radius: 4px; 
    padding: 6px; 
    margin: 4px 0 6px 0; 
    text-align: center; 
  }}

  .cheat-sheet-table {{ 
    width: 100%; 
    border-collapse: collapse; 
    margin: 16px 0 8px 0; 
    font-size: 11px; 
    table-layout: fixed;
  }}
  .cheat-sheet-table th {{ 
    background-color: #0F172A; 
    color: #FFFFFF; 
    font-weight: 600; 
    padding: 7px 8px; 
    border: 1px solid #334155; 
    text-align: center; 
    word-break: keep-all;
  }}
  .cheat-sheet-table td {{ 
    border: 1px solid #CBD5E1; 
    padding: 7px 8px; 
    text-align: left; 
    background-color: #FFFFFF; 
    word-break: keep-all;
    vertical-align: middle;
  }}
  .cheat-sheet-table td:nth-child(1) {{
    text-align: center;
    font-weight: 600;
  }}
  .cheat-sheet-table td:nth-child(2) {{
    text-align: center;
  }}
  .cheat-sheet-table tr:nth-child(even) td {{ background-color: #F8FAFC; }}
</style>
</head>
<body>
  <div class="header-container">
    <h1 class="doc-title">{clean_title}</h1>
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
        
        # KaTeX 로딩 보장 및 수식 렌더링 강제 실행
        page.evaluate("""() => {
            if (typeof renderMathInElement === 'function') {
                renderMathInElement(document.body, {
                    delimiters: [
                        {left: '$$', right: '$$', display: true},
                        {left: '$', right: '$', display: false},
                        {left: '\\\\[', right: '\\\\]', display: true},
                        {left: '\\\\(', right: '\\\\)', display: false}
                    ],
                    macros: {
                        '\\\\vec': '\\\\overrightarrow',
                        '\\\\oiint': '\\\\oint\\\\mkern-13mu\\\\oint'
                    },
                    throwOnError: false
                });
            }
        }""")
        
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(800)

        page.pdf(
            path=output_pdf_path,
            format="A4",
            print_background=True,
            margin={
                "top": "12mm",
                "bottom": "12mm",
                "left": "12mm",
                "right": "12mm",
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
    cleaned = re.sub(r'[\\/*?:"<>|]', "", filename).strip()
    cleaned = re.sub(r'[_.]+', '_', cleaned)
    return cleaned.strip('_')


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
                doc_title, body_html = extract_and_design_multiple_files(files, subject_hint, unit_hint)
                
                safe_title = sanitize_filename(doc_title)
                print(f"  -> PDF 리포트 제목/파일명 생성: {safe_title}")

                full_html = build_full_html(doc_title, body_html)
                temp_pdf_path = os.path.join(temp_dir, f"{safe_title}.pdf")
                render_html_to_pdf(full_html, temp_pdf_path)

                print("  -> GitHub Release에 업로드 중...")
                pdf_url = upload_pdf_to_github_release(temp_pdf_path, f"{safe_title}.pdf")
                print(f"  -> 다운로드 링크: {pdf_url}")

                update_notion_success(page_id, pdf_url)
                print("  -> Notion 업데이트 완료 (링크 등록 완료)!\n")

                time.sleep(1)

            except Exception as e:
                print(f"  -> 최종 실패: {e}\n")


if __name__ == "__main__":
    main()
