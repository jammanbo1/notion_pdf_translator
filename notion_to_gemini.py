import os
import re
import json
import tempfile
import requests
from dotenv import load_dotenv
from notion_client import Client
import google.generativeai as genai
from playwright.sync_api import sync_playwright

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
GDRIVE_JSON_STR = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")

notion = Client(auth=NOTION_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3.5-flash-lite")


def get_drive_service():
    """Google Drive API 서비스 클라이언트 생성 (전체 드라이브 권한 부여)"""
    service_account_info = json.loads(GDRIVE_JSON_STR)
    creds = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


def upload_pdf_to_drive(file_path: str, file_name: str) -> str:
    """PDF를 구글 드라이브에 업로드하고 공유 URL을 반환"""
    drive_service = get_drive_service()

    file_metadata = {
        "name": file_name,
    }
    if GDRIVE_FOLDER_ID:
        file_metadata["parents"] = [GDRIVE_FOLDER_ID]

    media = MediaFileUpload(file_path, mimetype="application/pdf", resumable=True)

    # 1. 파일 생성 및 업로드
    uploaded_file = (
        drive_service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )

    file_id = uploaded_file.get("id")

    # 2. 링크 공유 권한 부여 (누구나 보기 가능)
    try:
        drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        print(f"  (권한 설정 안내: {e})")

    # 3. 링크 반환
    web_link = uploaded_file.get("webViewLink")
    if not web_link:
        web_link = f"https://drive.google.com/file/d/{file_id}/view"

    return web_link


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

        # '처리완료' 또는 '완료'가 아닌 항목 처리
        if current_status not in ["처리완료", "완료"]:
            unprocessed.append(page)

    return unprocessed


def find_pdf_attachments(page):
    pdf_files = []
    properties = page.get("properties", {})

    for prop_value in properties.values():
        if prop_value.get("type") == "files":
            for file_obj in prop_value.get("files", []):
                file_name = file_obj.get("name", "")
                if file_name.lower().endswith(".pdf"):
                    url = file_obj.get("file", {}).get("url") or file_obj.get("external", {}).get("url")
                    pdf_files.append({"name": file_name, "url": url})

    return pdf_files


def extract_and_design_with_gemini(pdf_url: str) -> str:
    res = requests.get(pdf_url, stream=True, timeout=120)
    res.raise_for_status()
    pdf_bytes = res.content

    prompt = """
당신은 최고의 문서 디자이너이자 요약 정리 전문가입니다.
주어진 PDF 문서 내용을 분석하여 이해하기 쉽고 시각적으로 매우 수려한 요약 리포트를 HTML 본문 코드로 작성해주세요.

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
    response = model.generate_content(
        [prompt, {"mime_type": "application/pdf", "data": pdf_bytes}],
        request_options={"timeout": 300}
    )
    return response.text


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


def update_notion_success(page_id: str, drive_url: str):
    """Notion의 '정리본 링크' 속성에 URL 등록 및 '상태'를 '완료'로 변경"""
    update_data = {
        "정리본 링크": {"url": drive_url}
    }
    
    # Status 타입과 Select 타입 모두 호환되도록 처리
    try:
        update_data["상태"] = {"status": {"name": "완료"}}
        notion.pages.update(page_id=page_id, properties=update_data)
    except Exception:
        try:
            update_data["상태"] = {"select": {"name": "완료"}}
            notion.pages.update(page_id=page_id, properties=update_data)
        except Exception as e:
            print(f"  (상태 속성 업데이트 건너뜀: {e})")
            # 상태 변경 실패 시 링크만 업데이트
            notion.pages.update(page_id=page_id, properties={"정리본 링크": {"url": drive_url}})


def main():
    items = get_unprocessed_items()
    if not items:
        print("처리할 새 PDF가 없습니다.")
        return

    print(f"새 미처리 항목 {len(items)}개 발견.")

    with tempfile.TemporaryDirectory() as temp_dir:
        for page in items:
            page_id = page["id"]
            pdfs = find_pdf_attachments(page)
            if not pdfs:
                continue

            for pdf in pdfs:
                file_name = pdf["name"]
                base_name = os.path.splitext(file_name)[0]
                print(f"'{file_name}' 분석 및 디자인 PDF 생성 중...")

                try:
                    # 1. Gemini로 디자인 본문 HTML 생성
                    body_html = extract_and_design_with_gemini(pdf["url"])
                    full_html = build_full_html(base_name, body_html)

                    # 2. 임시 PDF 파일 렌더링
                    temp_pdf_path = os.path.join(temp_dir, f"{base_name}_정리본.pdf")
                    render_html_to_pdf(full_html, temp_pdf_path)

                    # 3. Google Drive 업로드
                    print("  -> Google Drive 업로드 중...")
                    drive_link = upload_pdf_to_drive(temp_pdf_path, f"{base_name}_정리본.pdf")
                    print(f"  -> Drive 링크 생성 완료: {drive_link}")

                    # 4. Notion 속성 업데이트
                    update_notion_success(page_id, drive_link)
                    print("  -> Notion '정리본 링크' 및 상태 업데이트 완료!\n")

                except Exception as e:
                    print(f"  -> 실패: {e}\n")


if __name__ == "__main__":
    main()
