import os
import re
import requests
from dotenv import load_dotenv
from notion_client import Client
import google.generativeai as genai

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

notion = Client(auth=NOTION_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3.6-flash")


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

        if current_status != "처리완료":
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


def extract_summary_with_gemini(pdf_url: str) -> str:
    res = requests.get(pdf_url, stream=True, timeout=120)
    res.raise_for_status()
    pdf_bytes = res.content

    prompt = (
        "이 PDF 문서의 내용을 체계적으로 요약 및 정리해줘.\n"
        "작성 규칙:\n"
        "1. ## 대제목, ### 중제목, - 불릿 포인트 형태로 구조화할 것.\n"
        "2. 핵심 요약은 [핵심요약] 머리말로 시작하는 단락으로 작성할 것.\n"
        "3. 수식은 LaTeX 문법($$...$$ 또는 $...$)을 사용할 것.\n"
        "4. 중요 키워드는 **볼드체**로 강조할 것."
    )

    response = model.generate_content(
        [prompt, {"mime_type": "application/pdf", "data": pdf_bytes}],
        request_options={"timeout": 300}
    )
    return response.text


def append_markdown_to_notion(page_id: str, markdown_text: str):
    """마크다운을 파싱하여 노션의 제목, 본문, 콜아웃 블록으로 변환해 페이지에 추가합니다."""
    blocks = []
    lines = markdown_text.split("\n")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": stripped[4:]}}]}
            })
        elif stripped.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": stripped[3:]}}]}
            })
        elif stripped.startswith("[핵심요약]"):
            blocks.append({
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {"content": stripped}}],
                    "icon": {"emoji": "💡"}
                }
            })
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]}
            })
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": stripped[:1800]}}]}
            })

    # 노션 API는 1회 요청 시 최대 100개 블록 제한이 있으므로 50개씩 분할 전송
    for i in range(0, len(blocks), 50):
        notion.blocks.children.append(
            block_id=page_id,
            children=blocks[i:i + 50]
        )


def mark_page_as_done(page_id: str):
    try:
        notion.pages.update(
            page_id=page_id,
            properties={"상태": {"select": {"name": "처리완료"}}}
        )
    except Exception:
        notion.pages.update(
            page_id=page_id,
            properties={"상태": {"status": {"name": "처리완료"}}}
        )


def main():
    items = get_unprocessed_items()
    if not items:
        print("처리할 새 PDF가 없습니다.")
        return

    print(f"새 미처리 항목 {len(items)}개 발견.")
    for page in items:
        page_id = page["id"]
        pdfs = find_pdf_attachments(page)
        if not pdfs:
            continue

        for pdf in pdfs:
            print(f"'{pdf['name']}' 요약 및 Notion 본문 기록 중...")
            try:
                summary = extract_summary_with_gemini(pdf["url"])
                append_markdown_to_notion(page_id, summary)
                mark_page_as_done(page_id)
                print("  -> Notion 페이지 저장 및 상태 업데이트 완료!")
            except Exception as e:
                print(f"  -> 실패: {e}")


if __name__ == "__main__":
    main()