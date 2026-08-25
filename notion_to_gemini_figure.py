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
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
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

    safe_name = f"fig_{int(time.time())}_{os.path.basename(file_path)}"
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
        photo_link_prop = props.get("참고 사진", {})
        existing_url = photo_link_prop.get("url") if photo_link_prop.get("type") == "url" else None

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


def clean_extracted_html(raw_text: str) -> str:
    """잡담/생각과정/마크다운 메타텍스트를 원천 차단하고 순수 HTML만 추출"""
    # 1. ```html ... ``` 블록 추출
    code_match = re.search(r"```html\s*([\s\S]*?)\s*```", raw_text, re.IGNORECASE)
    if code_match:
        return code_match.group(1).strip()
    
    # 2. figure-card <div> 태그 추출
    div_match = re.search(r"(<div class=[\"']figure-card[\"'][\s\S]*</div>)", raw_text, re.IGNORECASE)
    if div_match:
        return div_match.group(1).strip()

    # 3. 일반 ``` ... ``` 블록 추출
    generic_match = re.search(r"```\s*([\s\S]*?)\s*```", raw_text)
    if generic_match:
        return generic_match.group(1).strip()

    # 4. 최후의 수단: DOC_TITLE 라인만 제거
    cleaned = re.sub(r"DOC_TITLE:\s*.+\n?", "", raw_text)
    return cleaned.strip()


def extract_and_design_figures(file_list: list, subject_hint: str = "", unit_hint: str = "") -> tuple:
    content_payload = []
    
    prompt_text = f"""당신은 세계 최고 수준의 이공계열 전공 학술서 전문 그래픽 아티스트이자 물리학/수학/전자공학 교재 수석 편집자입니다.
첨부된 자료에서 가장 핵심적인 도판, 그래프, 개념 다이어그램을 [엄선하여 2개~최대 3개] 선정하고,
[단색 흑백(Monochrome) 학술 전공서 스타일]의 극도로 정밀한 인라인 SVG 코드로 재작도하여 고품질 A4 [도판 해설 리포트]를 작성해주세요.
(참고 과목: {subject_hint}, 단원명: {unit_hint})

★ [출력 형식 절대 준수 - 잡담/메타 텍스트 금지]:
1. 첫 번째 줄에는 오직 문서 제목만 다음 포맷으로 작성하세요:
DOC_TITLE: [{subject_hint} - {unit_hint}] 핵심 시각 자료 및 도판 해설집
2. 두 번째 줄부터는 사전 설명, 계획(Plan), 생각 과정, 인사말을 일절 쓰지 말고 오직 ```html 로 시작하여 ``` 로 끝나는 코드 블록만 출력하세요.

★ [도판 엄선 및 완성도 절대 규칙]
1. 도판 개수는 무조건 [가장 중요한 2개~3개]만 엄선하여 집중 작도하세요. (4개 이상 남발 금지)
2. 텍스트 땜질 절대 금지:
   - 텍스트나 수식만 띄워놓고 도형/곡선을 생략하는 것은 절대 허용되지 않습니다.
   - 실제 함수 파형(톱니파, 구형파, 지수함수, 정현파 등)과 회로 소자, 좌표축, 투영선, 임펄스 화살표를 구체적인 SVG 패스(<path d="...">, <line>, <rect>)로 끝까지 정밀하게 완벽히 그리세요.

★ [도판 스타일 및 SVG 작도 규격]
1. 완전한 모노크롬(흑백 및 단색 그레이스케일) 원칙:
   - 유색 컬러(Blue, Red, Green 등) 사용 금지.
   - 주요 외곽선 / 함수 곡선 / 주요 벡터: #0F172A (stroke-width="2.0" ~ "2.5")
   - 기준 좌표축 / 회로 도선 / 눈금: #334155 또는 #475569 (stroke-width="1.2" ~ "1.4")
   - 보조 투영선 / 점근선 / 가이드선: #64748B 또는 #94A3B8 (stroke-dasharray="4,4")
   - 3D 입체 음영 및 면적 채움: 단색 그레이스케일 Linear/Radial Gradient(#FFFFFF -> #F1F5F9 -> #CBD5E1 -> #94A3B8).

2. SVG 뷰박스 및 레이아웃:
   - 각 SVG는 viewBox="0 0 540 280" (또는 viewBox="0 0 540 320")으로 충분한 여백을 확보하세요.

3. 엄밀한 수학/물리 라벨링:
   - 모든 좌표축($x, y, z, t, \\omega$), 물리 변수($\\vec{{v}}, \\vec{{E}}, \\vec{{B}}, \\vec{{P}}, I, V_0, \\tau, \\omega_0$)는 학술 세리프 이탤릭체(font-family="Times New Roman, serif", font-style="italic")를 적용하세요.
   - 위첨자/아래첨자는 SVG의 <tspan> 태그를 정밀하게 사용하세요.
   - 화살표는 <defs><marker>로 선 끝에 깔끔하게 결합하세요.

4. 도판별 구성 템플릿 (각 도판마다 아래 구조를 독립적으로 엄수):
   <div class="figure-card">
     <div class="figure-header">
       <span class="badge">Fig. 번호</span> <strong>도판 주제 및 핵심 물리/회로 현상 제목</strong>
     </div>
     <div class="svg-container">
       <svg viewBox="0 0 540 280" width="100%" height="240" xmlns="http://www.w3.org/2000/svg">
         <!-- 정밀 인라인 SVG 작도 -->
       </svg>
     </div>
     <div class="figure-desc">
       <p><strong>현상 및 조건:</strong> (기하학적 설정, 주어진 입력 신호, 회로 파라미터 등 물리 조건 서술)</p>
       <p><strong>시각적 핵심:</strong> (파형의 대칭성, 감쇠율, 오버슈트, 주파수 스펙트럼 등 핵심 해석 서술)</p>
     </div>
   </div>

★ [KaTeX 수식 파싱 보호 절대 규칙]
- figure-desc 본문 수식 작성 시 부등호(<, >)나 앰퍼샌드(&)는 반드시 '&lt;', '&gt;', '&amp;' 엔티티로 변환하여 작성하세요! (예: $t &lt; 0$, $\\omega &gt; 0$)
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

    generation_config = {
        "max_output_tokens": 8192,
        "temperature": 0.2,
    }

    last_exception = None
    for model_name in FALLBACK_MODELS:
        print(f"  [도판 생성] -> [{model_name}] 모델 호출 시도 중...")
        try:
            current_model = genai.GenerativeModel(model_name, generation_config=generation_config)
            response = current_model.generate_content(
                content_payload, request_options={"timeout": 600}
            )
            raw_text = response.text
            
            extracted_title = "전공_도판_해설집"
            title_match = re.search(r"DOC_TITLE:\s*(.+)", raw_text)
            if title_match:
                extracted_title = title_match.group(1).strip()
                
            body_html = clean_extracted_html(raw_text)
                
            print(f"  [도판 생성] -> [{model_name}] 생성 성공!")
            return extracted_title, body_html

        except Exception as e:
            last_exception = e
            print(f"  [경고] {model_name} 실패 (사유: {e})")
            time.sleep(2)
            continue

    raise RuntimeError(f"모든 후보 모델 호출 실패: {last_exception}")


def build_full_html(title: str, content_html: str) -> str:
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

  .figure-card {{
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    padding: 16px;
    margin: 22px 0;
    page-break-inside: avoid;
  }}
  .figure-header {{
    font-size: 13.5px;
    font-weight: 700;
    color: #0F172A;
    margin-bottom: 12px;
    border-bottom: 1px solid #F1F5F9;
    padding-bottom: 8px;
  }}
  .badge {{
    display: inline-block;
    font-size: 11px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    margin-right: 6px;
    background: #F1F5F9;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    letter-spacing: -0.2px;
  }}
  .svg-container {{
    text-align: center;
    background: #FFFFFF;
    border: 1px solid #F8FAFC;
    border-radius: 6px;
    padding: 12px;
  }}
  .figure-desc {{
    background: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 10px 12px;
    margin-top: 12px;
    font-size: 12px;
    line-height: 1.65;
  }}
  .figure-desc p {{
    margin: 3px 0;
  }}
</style>
</head>
<body>
  <div class="header-container">
    <h1 class="doc-title">{title}</h1>
    <p class="doc-subtitle">핵심 시각 자료 및 도판 해설집</p>
  </div>
  {content_html}
</body>
</html>
"""


def render_html_to_pdf(html_content: str, output_pdf_path: str):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html_content, wait_until="networkidle")
        
        # 웹폰트 및 KaTeX 수식 로딩 완료 대기
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(1500)
        
        page.pdf(
            path=output_pdf_path,
            format="A4",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
        )
        browser.close()


def update_notion_figure_success(page: dict, download_url: str):
    page_id = page["id"]
    props = page.get("properties", {})
    
    update_data = {"참고 사진": {"url": download_url}}
    
    text_prop = props.get("내용 요약본", {})
    text_url = text_prop.get("url") if text_prop.get("type") == "url" else None
    
    if text_url:
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
        print("[도판 생성] 처리할 새 항목이 없습니다.")
        return

    print(f"[도판 생성] 미처리 항목 {len(items)}개 발견.")

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

            print(f"\n[도판 작업 시작] 과목: '{subject_hint}', 단원명: '{unit_hint}' (첨부파일 {len(files)}개)...")

            try:
                doc_title, body_html = extract_and_design_figures(files, subject_hint, unit_hint)
                
                safe_title = sanitize_filename(doc_title)
                print(f"  -> PDF 리포트 제목/파일명: {doc_title}")

                full_html = build_full_html(doc_title, body_html)
                temp_pdf_path = os.path.join(temp_dir, f"{safe_title}.pdf")
                render_html_to_pdf(full_html, temp_pdf_path)

                print("  -> GitHub Storage 업로드 중...")
                pdf_url = upload_pdf_to_github_release(temp_pdf_path, f"{safe_title}.pdf")
                print(f"  -> 다운로드 링크: {pdf_url}")

                update_notion_figure_success(page, pdf_url)
                print("  -> Notion '참고 사진' 컬럼 업데이트 완료!\n")

                time.sleep(1)

            except Exception as e:
                print(f"  -> 최종 실패: {e}\n")


if __name__ == "__main__":
    main()
