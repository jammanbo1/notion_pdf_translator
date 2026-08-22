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
model = genai.GenerativeModel("gemini-3.6-flash")


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
    prompt = f"""
당신은 최고의 대학 이공계열 전공 학업 요약 전문가이자 시험 대비 튜터입니다.
첨부된 전공 문서/필기 자료를 정밀 분석하여 최고급 요약 리포트를 작성해주세요.
(참고 과목 힌트: {subject_hint}, 참고 단원명 힌트: {unit_hint})

[필수 출력 양식 1단계: 지능형 제목 생성]
답변의 첫 번째 줄에 반드시 아래 형식으로 문서의 핵심 내용을 포괄하는 정갈한 한국어 제목을 1개 출력하세요:
DOC_TITLE: [과목/단원 핵심 키워드 중심의 명확한 리포트 제목]

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요.

[핵심 서술 및 필기 메모 보존 규칙]
1. 샤프/연필 필기 메모 집중 판독 & 체크 포인트 블록 (<div class="checkpoint-box">):
   - 노트 본문 여백, 수식 옆, 상단에 '샤프/연필'이나 체크 표시(V, ★, #, ※)로 적힌 개인 메모를 정밀 판독할 것.
   - 이를 <div class="checkpoint-box"><span class="checkpoint-tag">#체크포인트</span> [원문 코멘트] <span class="tutor-add">(튜터 첨언: [해당 메모와 관련된 엄밀한 보충 설명/수식])</span></div> 형태로 담백하고 직관적이게 배치할 것.

2. Mindset 액션 가이드 (<div class="mindset-box">):
   - 문서 최상단에 해당 단원 문제를 접할 때 가장 먼저 의식해야 하는 핵심 행동 강령(Thinking Point)을 1줄로 명시할 것.

3. 한 줄 직관 비유 (<div class="analogy-box">):
   - 추상적인 개념에 대해 1초 만에 이해되는 직관적인 비유를 1줄로 간결하게 명시할 것.

4. 3단계 솔루션 프로세스 (3-Step Solution Flow):
   - 대표 예제 풀이(<div class="example-box">) 작성 시: [Step 1. 모델링/조건 분석] -> [Step 2. 수학적 해법] -> [Step 3. 물리적 해석 및 검증] 단계를 준수할 것.

5. 적용 한계 및 경계 조건 명시 (<div class="boundary-box">):
   - 공식이나 해법이 성립하는 유효 범위와, 성립하지 않는 예외 조건을 명확히 대조 서술할 것.

6. 학문적 도메인 자동 판별 및 맞춤형 가중치:
   - Mode A [물리 / 소자 / 자연과학 개념]: 물리적 메커니즘, 장(Field)/소자 시각화 인라인 SVG 도식 2개 이상 필수, 개념 대칭/비교 맵(<div class="concept-map">), #함정주의(<div class="trap-box">).
   - Mode B [수학 / 회로 / 신호 / 계산 알고리즘]: 정석 예제 풀이, #보이스피싱(<div class="voice-phishing-box">) 숏컷, Recall 선수 공식 박스.

7. 공통 완성도 규칙:
   - 전단원 균형 커버리지: 모든 핵심 소단원 누락 없이 포함.
   - 표준 전공서 교차 검증: 엄밀한 수식 표기법과 부호 규약 적용.
   - 수식 표기: 모든 LaTeX 수식은 $...$(인라인) 또는 $$...$$(단독 블록)으로 정확히 표기.
   - 최상단 요약 박스: <div class="summary-box"><strong> 핵심 요약</strong>: 전체 통합 요약</div>
   - 최하단 치트시트: <table class="cheat-sheet-table">로 핵심 공식, 적용 조건, 주의사항 정리.
   - 별도의 <html>, <head>, <body> 태그 없이 <div>로 감싼 순수 HTML 본문만 반환할 것.
"""
    content_payload.append(prompt)

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

    for attempt in range(3):
        try:
            response = model.generate_content(
                content_payload, request_options={"timeout": 300}
            )
            raw_text = response.text
            
            extracted_title = "전공_핵심_요약_리포트"
            body_html = raw_text
            
            match = re.search(r"DOC_TITLE:\s*(.+)", raw_text)
            if match:
                extracted_title = match.group(1).strip()
                body_html = re.sub(r"DOC_TITLE:\s*.+\n?", "", raw_text).strip()
                
            return extracted_title, body_html

        except Exception as e:
            if "429" in str(e) and attempt < 2:
                print("  [알림] API 호출 제한 감지. 45초 후 자동 재시도합니다...")
                time.sleep(45)
            else:
                raise e


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
  @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;600;700;800&display=swap');
  @page {{ size: A4; margin: 20mm 15mm; }}
  body {{ font-family: 'Pretendard', sans-serif; color: #2D3748; line-height: 1.7; font-size: 13px; margin: 0; }}
  .header-container {{ border-bottom: 2px solid #2B6CB0; padding-bottom: 12px; margin-bottom: 20px; }}
  .doc-title {{ font-size: 22px; font-weight: 800; color: #1A365D; margin: 0 0 6px 0; }}
  .doc-subtitle {{ font-size: 12px; color: #718096; margin: 0; }}
  h2 {{ font-size: 16px; font-weight: 700; color: #2B6CB0; border-left: 4px solid #3182CE; padding-left: 8px; margin-top: 24px; }}
  
  .mindset-box {{ background-color: #F0FFF4; border: 1.5px solid #9AE6B4; border-left: 5px solid #38A169; border-radius: 4px 8px 8px 4px; padding: 12px 14px; margin-bottom: 16px; }}
  .mindset-header {{ font-weight: 800; font-size: 12.5px; color: #22543D; margin-bottom: 4px; }}
  .mindset-desc {{ font-size: 12px; color: #276749; margin: 0; font-weight: 600; }}

  .checkpoint-box {{ background-color: #FFFDF5; border: 1.5px solid #F6E05E; border-left: 5px solid #D69E2E; border-radius: 4px 8px 8px 4px; padding: 10px 14px; margin: 12px 0; font-size: 12.5px; color: #744210; line-height: 1.6; }}
  .checkpoint-tag {{ font-weight: 800; color: #B7791F; background-color: #FEFCBF; padding: 2px 6px; border-radius: 4px; margin-right: 4px; font-size: 11.5px; }}
  .tutor-add {{ color: #4A5568; font-size: 11.5px; margin-left: 4px; font-weight: normal; }}

  .analogy-box {{ background-color: #FDF2F8; border: 1.5px solid #FBCFE8; border-left: 5px solid #DB2777; border-radius: 4px 8px 8px 4px; padding: 10px 14px; margin: 12px 0; }}
  .analogy-header {{ font-weight: 800; font-size: 12px; color: #9D174D; margin-bottom: 2px; }}
  .analogy-desc {{ font-size: 12px; color: #831843; margin: 0; font-weight: 600; line-height: 1.5; }}

  .summary-box {{ background-color: #EBF8FF; border-left: 5px solid #3182CE; border-radius: 4px 8px 8px 4px; padding: 14px; margin-bottom: 20px; }}
  .formula-box {{ background-color: #F8FAFC; border: 1px solid #E2E8F0; border-left: 5px solid #4A5568; border-radius: 4px 8px 8px 4px; padding: 12px; margin: 12px 0; }}
  
  .boundary-box {{ background-color: #F7FAFC; border: 1px solid #E2E8F0; border-left: 5px solid #4A5568; border-radius: 4px 8px 8px 4px; padding: 12px 14px; margin: 14px 0; }}
  .boundary-header {{ font-weight: 800; font-size: 12.5px; color: #2D3748; margin-bottom: 6px; }}

  .recall-box {{ background-color: #FFFAF0; border: 1.5px solid #FBD38D; border-left: 5px solid #DD6B20; border-radius: 4px 8px 8px 4px; padding: 12px 14px; margin: 14px 0; }}
  .recall-header {{ font-weight: 800; font-size: 12.5px; color: #C05621; margin-bottom: 6px; }}
  .recall-formula {{ background-color: #FFFFFF; border: 1px dashed #ED8936; border-radius: 4px; padding: 6px; margin: 6px 0; text-align: center; }}

  .concept-map {{ display: flex; justify-content: space-between; align-items: stretch; background-color: #F7FAFC; border: 1px solid #CBD5E0; border-radius: 8px; padding: 14px; margin: 16px 0; gap: 10px; }}
  .map-col {{ flex: 1; background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 6px; padding: 12px; display: flex; flex-direction: column; justify-content: flex-start; }}
  .map-header {{ font-weight: 700; font-size: 13px; color: #2B6CB0; margin-bottom: 8px; border-bottom: 1.5px solid #E2E8F0; padding-bottom: 4px; text-align: center; }}
  .map-formula {{ background-color: #F8FAFC; border-radius: 4px; padding: 6px; margin: 6px 0; text-align: center; border: 1px dashed #CBD5E0; }}
  .map-arrow {{ display: flex; align-items: center; justify-content: center; font-size: 20px; color: #4A5568; padding: 0 4px; }}
  .map-desc {{ font-size: 11px; color: #4A5568; margin: 4px 0 0 0; line-height: 1.5; }}

  .example-box {{ background-color: #F7FAFC; border: 1px solid #CBD5E0; border-left: 5px solid #319795; border-radius: 4px 8px 8px 4px; padding: 14px; margin: 18px 0; }}
  .example-header {{ font-weight: 800; font-size: 13px; color: #285E61; margin-bottom: 8px; }}
  .example-question {{ background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 4px; padding: 10px 12px; margin-bottom: 10px; font-size: 12.5px; }}
  .example-solution {{ padding-left: 4px; font-size: 12px; }}
  .step-label {{ font-weight: 700; color: #234E52; margin-top: 8px; margin-bottom: 2px; }}
  .calc-step {{ background-color: #FFFFFF; border: 1px solid #EDF2F7; border-radius: 4px; padding: 8px; margin: 4px 0 8px 0; text-align: center; }}

  .voice-phishing-box {{ background-color: #FAF5FF; border: 1.5px solid #D6BCFA; border-left: 5px solid #805AD5; border-radius: 4px 8px 8px 4px; padding: 14px; margin: 16px 0; }}
  .phishing-header {{ font-weight: 800; font-size: 13px; color: #6B46C1; margin-bottom: 6px; }}
  .phishing-formula {{ background-color: #FFFFFF; border: 1px dashed #B794F4; border-radius: 4px; padding: 8px; margin: 8px 0; text-align: center; }}
  .phishing-desc {{ font-size: 12px; color: #4A5568; margin: 0; line-height: 1.6; }}

  .trap-box {{ background-color: #FFF5F5; border: 1.5px solid #FEB2B2; border-left: 5px solid #E53E3E; border-radius: 4px 8px 8px 4px; padding: 12px 14px; margin: 14px 0; }}
  .trap-header {{ font-weight: 800; font-size: 12.5px; color: #C53030; margin-bottom: 6px; }}
  .trap-desc {{ font-size: 12px; color: #4A5568; margin: 0; line-height: 1.6; }}

  .svg-container {{ text-align: center; margin: 18px 0; background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px; }}
  .svg-container svg {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}
  .caption {{ font-size: 11px; color: #718096; margin-top: 6px; text-align: center; }}

  .cheat-sheet-table {{ width: 100%; border-collapse: collapse; margin: 20px 0 10px 0; font-size: 12px; }}
  .cheat-sheet-table th {{ background-color: #2B6CB0; color: #FFFFFF; font-weight: 700; padding: 8px 10px; border: 1px solid #CBD5E0; text-align: center; }}
  .cheat-sheet-table td {{ border: 1px solid #E2E8F0; padding: 8px 10px; text-align: center; background-color: #FFFFFF; }}
  .cheat-sheet-table tr:nth-child(even) td {{ background-color: #F7FAFC; }}
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
                properties={"정리본 링크": {"url": download_url}}
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
                print("  -> Notion 업데이트 완료 (단원명 보존, 링크 등록 완료)!\n")

                time.sleep(5)

            except Exception as e:
                print(f"  -> 실패: {e}\n")


if __name__ == "__main__":
    main()
