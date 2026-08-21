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
model = genai.GenerativeModel("gemini-3.5-flash")


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
        "prerelease": False
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
                    url = file_obj.get("file", {}).get("url") or file_obj.get("external", {}).get("url")
                    supported_files.append({"name": file_name, "url": url})

    return supported_files


def extract_and_design_multiple_files(file_list: list) -> str:
    content_payload = []
    prompt = """
당신은 최고의 시험 대비 튜터이자 전공 학업 요약 전문가입니다.
첨부된 모든 문서/손글씨 필기 자료를 분석하여 이론, 선수 개념 복습, 실전 계산, 다이어그램이 조화된 고품질 요약 리포트를 HTML 코드로 작성해주세요.

[작성 규칙]
1. 최상단 요약 박스: <div class="summary-box"><strong> 핵심 요약</strong>: 전체 자료의 핵심 개념 요약</div>
2. 중요 키워드는 <span class="highlight">강조</span> 처리.
3. 수식은 반드시 LaTeX 문법($$...$$ 또는 $...$)으로 작성:
   <div class="formula-box">수식 설명 및 $$ E = mc^2 $$</div>

4. 💡 Recall (선수 개념 & 까먹기 쉬운 필수 아이디어):
   중요한 개념이나 복잡한 수식 유도를 전개하기 전에, 이해에 꼭 필요한 선수 지식(예: 미적분 공식, 삼각함수 항등식, 이전 단원 공식, 물리적 기본 전제)이 있다면 설명 직전에 반드시 아래 형식으로 박스를 배치하세요:
   <div class="recall-box">
     <div class="recall-header">💡 Recall (사전 필수 개념 & 리마인드)</div>
     <p><strong>꼭 기억해야 할 배경 지식:</strong> 설명 및 적용될 수학적/물리적 전제</p>
     <div class="recall-formula">$$ 필수 수식/정리 $$</div>
   </div>

5. 핵심 포인트: <div class="callout-box"><strong> Key Point:</strong> ... </div>

6. 개념 간 대칭/비교 구조:
   <div class="concept-map">
     <div class="map-col">
       <div class="map-header">좌측 개념명</div>
       <div class="map-formula">$$ 수식 $$</div>
       <p class="map-desc">설명</p>
     </div>
     <div class="map-arrow">$$\\longleftrightarrow$$</div>
     <div class="map-col">
       <div class="map-header">우측 개념명</div>
       <div class="map-formula">$$ 수식 $$</div>
       <p class="map-desc">설명</p>
     </div>
   </div>

7. 실전 적용 예제 문항 및 계산 전개:
   원문의 예제를 살리거나, 핵심 공식마다 빈출 예제 1~2개를 다음 형식으로 작성:
   <div class="example-box">
     <div class="example-header">📝 실전 적용 예제 (Example Problem)</div>
     <div class="example-question"><strong>[문제]</strong> 문제 상황 및 조건</div>
     <div class="example-solution">
       <div class="solution-title"> 정석 풀이 및 계산 과정:</div>
       <p>1단계: 조건 분석 및 공식 선정</p>
       <div class="calc-step">$$ \\text{수식 전개} $$</div>
       <p>2단계: 결과 도출</p>
       <div class="calc-step">$$ \\therefore \\text{결과값} $$</div>
     </div>
   </div>

8. 시험용 숏컷 / 극한 단축 풀이 (#보이스피싱):
   <div class="voice-phishing-box">
     <div class="phishing-header">⚡ #보이스피싱 (실전 초단축 풀이법)</div>
     <p><strong>핵심 아이디어:</strong> 직관적 도출 원리</p>
     <div class="phishing-formula">$$ 단축 수식 $$</div>
     <p class="phishing-desc">실전 적용 팁 서술</p>
   </div>

9. 시각화 다이어그램 / 물리 도식 (인라인 SVG):
   좌표계, 단면도, 회로도 등 그림 설명이 필요한 경우 순수 <svg> 코드로 직접 삽입:
   <div class="svg-container">
     <svg viewBox="0 0 400 150" xmlns="http://www.w3.org/2000/svg">
       <!-- 선, 도형, 화살표, 라벨 구성 -->
     </svg>
     <div class="caption">도식 설명</div>
   </div>

10. 별도의 <html>, <head>, <body> 태그 없이 <div>로 감싼 순수 HTML 본문만 반환하세요.
"""
    content_payload.append(prompt)

    for item in file_list:
        res = requests.get(item["url"], stream=True, timeout=120)
        res.raise_for_status()
        mime_type, _ = mimetypes.guess_type(item["name"])
        if not mime_type:
            mime_type = "application/pdf" if item["name"].lower().endswith(".pdf") else "image/jpeg"
        content_payload.append({"mime_type": mime_type, "data": res.content})

    for attempt in range(3):
        try:
            response = model.generate_content(content_payload, request_options={"timeout": 300})
            return response.text
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                print("  [알림] API 호출 제한 감지. 45초 후 자동 재시도합니다...")
                time.sleep(45)
            else:
                raise e


def build_full_html(title: str, content_html: str) -> str:
    clean_html = re.sub(r"^```html\s*|\s*```$", "", content_html.strip(), flags=re.MULTILINE)

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
  @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;600;700;800&display=swap');
  @page {{ size: A4; margin: 20mm 15mm; }}
  body {{ font-family: 'Pretendard', sans-serif; color: #2D3748; line-height: 1.7; font-size: 13px; margin: 0; }}
  .header-container {{ border-bottom: 2px solid #2B6CB0; padding-bottom: 12px; margin-bottom: 20px; }}
  .doc-title {{ font-size: 22px; font-weight: 800; color: #1A365D; margin: 0 0 6px 0; }}
  .doc-subtitle {{ font-size: 12px; color: #718096; margin: 0; }}
  h2 {{ font-size: 16px; font-weight: 700; color: #2B6CB0; border-left: 4px solid #3182CE; padding-left: 8px; margin-top: 24px; }}
  .highlight {{ background-color: #FEFCBF; padding: 2px 5px; border-radius: 4px; font-weight: 600; }}
  .summary-box {{ background-color: #EBF8FF; border-left: 5px solid #3182CE; border-radius: 4px 8px 8px 4px; padding: 14px; margin-bottom: 20px; }}
  .formula-box {{ background-color: #F8FAFC; border: 1px solid #E2E8F0; border-left: 5px solid #4A5568; border-radius: 4px 8px 8px 4px; padding: 12px; margin: 12px 0; }}
  .callout-box {{ background-color: #FFFDF5; border-left: 5px solid #D69E2E; padding: 12px 14px; margin: 12px 0; border-radius: 4px 8px 8px 4px; }}
  
  /* Recall (선수 개념 & 리마인드) 전용 박스 스타일 */
  .recall-box {{ background-color: #FFFAF0; border: 1.5px solid #FBD38D; border-left: 5px solid #DD6B20; border-radius: 4px 8px 8px 4px; padding: 12px 14px; margin: 14px 0; }}
  .recall-header {{ font-weight: 800; font-size: 12.5px; color: #C05621; margin-bottom: 6px; }}
  .recall-formula {{ background-color: #FFFFFF; border: 1px dashed #ED8936; border-radius: 4px; padding: 6px; margin: 6px 0; text-align: center; }}

  /* 대칭 마인드맵 박스 */
  .concept-map {{ display: flex; justify-content: space-between; align-items: stretch; background-color: #F7FAFC; border: 1px solid #CBD5E0; border-radius: 8px; padding: 14px; margin: 16px 0; gap: 10px; }}
  .map-col {{ flex: 1; background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 6px; padding: 12px; display: flex; flex-direction: column; justify-content: flex-start; }}
  .map-header {{ font-weight: 700; font-size: 13px; color: #2B6CB0; margin-bottom: 8px; border-bottom: 1.5px solid #E2E8F0; padding-bottom: 4px; text-align: center; }}
  .map-formula {{ background-color: #F8FAFC; border-radius: 4px; padding: 6px; margin: 6px 0; text-align: center; border: 1px dashed #CBD5E0; }}
  .map-arrow {{ display: flex; align-items: center; justify-content: center; font-size: 20px; color: #4A5568; padding: 0 4px; }}
  .map-desc {{ font-size: 11px; color: #4A5568; margin: 4px 0 0 0; line-height: 1.5; }}

  /* 실전 적용 예제 박스 */
  .example-box {{ background-color: #F7FAFC; border: 1px solid #CBD5E0; border-left: 5px solid #319795; border-radius: 4px 8px 8px 4px; padding: 14px; margin: 18px 0; }}
  .example-header {{ font-weight: 800; font-size: 13px; color: #285E61; margin-bottom: 8px; }}
  .example-question {{ background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 4px; padding: 10px 12px; margin-bottom: 10px; font-size: 12.5px; }}
  .example-solution {{ padding-left: 4px; font-size: 12px; }}
  .solution-title {{ font-weight: 700; color: #2C7A7B; margin-bottom: 4px; }}
  .calc-step {{ background-color: #FFFFFF; border: 1px solid #EDF2F7; border-radius: 4px; padding: 8px; margin: 6px 0 10px 0; text-align: center; }}

  /* #보이스피싱 전용 숏컷 스타일 */
  .voice-phishing-box {{ background-color: #FAF5FF; border: 1.5px solid #D6BCFA; border-left: 5px solid #805AD5; border-radius: 4px 8px 8px 4px; padding: 14px; margin: 16px 0; }}
  .phishing-header {{ font-weight: 800; font-size: 13px; color: #6B46C1; margin-bottom: 6px; }}
  .phishing-formula {{ background-color: #FFFFFF; border: 1px dashed #B794F4; border-radius: 4px; padding: 8px; margin: 8px 0; text-align: center; }}
  .phishing-desc {{ font-size: 12px; color: #4A5568; margin: 0; line-height: 1.6; }}

  /* 인라인 SVG 다이어그램 컨테이너 */
  .svg-container {{ text-align: center; margin: 18px 0; background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px; }}
  .svg-container svg {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}
  .caption {{ font-size: 11px; color: #718096; margin-top: 6px; text-align: center; }}
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
        page.wait_for_timeout(2500)
        page.pdf(
            path=output_pdf_path,
            format="A4",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"}
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
            notion.pages.update(page_id=page_id, properties={"정리본 링크": {"url": download_url}})


def main():
    items = get_unprocessed_items()
    if not items:
        print("처리할 새 파일이 없습니다.")
        return

    print(f"새 미처리 항목 {len(items)}개 발견.")

    with tempfile.TemporaryDirectory() as temp_dir:
        for page in items:
            page_id = page["id"]
            files = find_supported_attachments(page)
            if not files:
                continue

            main_title = os.path.splitext(files[0]["name"])[0]
            if len(files) > 1:
                main_title = f"{main_title}_외_{len(files)-1}건_통합본"

            print(f"'{main_title}' (총 {len(files)}개 파일) 종합 분석 및 디자인 PDF 생성 중...")

            try:
                body_html = extract_and_design_multiple_files(files)
                full_html = build_full_html(main_title, body_html)

                temp_pdf_path = os.path.join(temp_dir, f"{main_title}_정리본.pdf")
                render_html_to_pdf(full_html, temp_pdf_path)

                print("  -> GitHub Storage에 통합본 업로드 중...")
                pdf_url = upload_pdf_to_github_release(temp_pdf_path, f"{main_title}_정리본.pdf")
                print(f"  -> 다운로드 링크 생성 완료: {pdf_url}")

                update_notion_success(page_id, pdf_url)
                print("  -> Notion 업데이트 완료!\n")

                time.sleep(5)

            except Exception as e:
                print(f"  -> 실패: {e}\n")


if __name__ == "__main__":
    main()
