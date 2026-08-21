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
model = genai.GenerativeModel("gemini-2.5-flash-lite")


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
    """PDF뿐만 아니라 이미지 파일(PNG, JPG, JPEG)도 함께 추출"""
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


def extract_and_design_with_gemini(file_url: str, file_name: str) -> str:
    """PDF 및 손글씨 이미지 파일을 분석하여 깔끔한 HTML 리포트 생성"""
    res = requests.get(file_url, stream=True, timeout=120)
    res.raise_for_status()
    file_bytes = res.content

    mime_type, _ = mimetypes.guess_type(file_name)
    if not mime_type:
        mime_type = "application/pdf" if file_name.lower().endswith(".pdf") else "image/jpeg"

    prompt = """
당신은 최고의 문서 디자이너이자 전공 학업 정리 전문가입니다.
주어진 파일(문서 또는 손글씨 노트 사진)을 꼼꼼히 분석하여 가독성이 뛰어난 요약 리포트를 HTML 코드로 작성해주세요.
만약 손글씨 필기 노트 사진인 경우, 필기된 글씨와 수식 기호를 정확히 판독하여 체계적으로 정리해주세요.

[작성 규칙]
1. 최상단 요약 박스: <div class="summary-box"><strong> 핵심 요약</strong>: ... </div>
2. 중요 키워드는 <span class="highlight">강조</span> 처리.
3. 수식은 반드시 LaTeX 문법($$...$$ 또는 $...$)으로 작성:
   <div class="formula-box">수식 설명 및 $$ E = mc^2 $$</div>
4. 핵심 포인트: <div class="callout-box"><strong> Key Point:</strong> ... </div>
5. 관련 Unsplash 무료 이미지 1개 배치:
   <div class="image-container"><img src="https://source.unsplash.com/800x400/?{주제영문키워드}" alt="참고이미지" onerror="this.style.display='none'"/><div class="caption">관련 참고 자료</div></div>
6. 별도의 <html>, <head>, <body> 태그 없이 <div>로 감싼 순수 HTML 본문만 반환하세요.
"""
    for attempt in range(3):
        try:
            response = model.generate_content(
                [prompt, {"mime_type": mime_type, "data": file_bytes}],
                request_options={"timeout": 300}
            )
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
  .image-container {{ text-align: center; margin: 16px 0; }}
  .image-container img {{ max-width: 90%; max-height: 220px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
  .caption {{ font-size: 11px; color: #718096; margin-top: 4px; }}
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

            for file_item in files:
                file_name = file_item["name"]
                base_name = os.path.splitext(file_name)[0]
                print(f"'{file_name}' 분석 및 디자인 PDF 생성 중...")

                try:
                    body_html = extract_and_design_with_gemini(file_item["url"], file_name)
                    full_html = build_full_html(base_name, body_html)

                    temp_pdf_path = os.path.join(temp_dir, f"{base_name}_정리본.pdf")
                    render_html_to_pdf(full_html, temp_pdf_path)

                    print("  -> GitHub Storage에 업로드 중...")
                    pdf_url = upload_pdf_to_github_release(temp_pdf_path, f"{base_name}_정리본.pdf")
                    print(f"  -> 다운로드 링크 생성 완료: {pdf_url}")

                    update_notion_success(page_id, pdf_url)
                    print("  -> Notion 업데이트 완료!\n")

                    time.sleep(5)

                except Exception as e:
                    print(f"  -> 실패: {e}\n")


if __name__ == "__main__":
    main()
