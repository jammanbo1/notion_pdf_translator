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


def extract_and_design_multiple_files(file_list: list) -> str:
    content_payload = []
    prompt = """
당신은 최고의 시험 대비 튜터이자 전공 학업 요약 전문가입니다.
첨부된 모든 문서/손글씨 필기 자료를 분석하여 이론, 표준 전공서 연계 개념, 실전 계산, 함정 방지, 요약 치트시트가 조화된 최고급 요약 리포트를 HTML 코드로 작성해주세요.

[요약 범위 및 균형 구성 원칙 (필수 준수)]
1. 전단원 균형 커버리지:
   - 특정 1개 단원에 치우쳐 길게 서술하지 마세요.
   - 첨부 파일에 등장하는 모든 대단원(예: AC 해석 및 전력, 상호 결합 회로, 라플라스 변환 회로, 능동/수동 필터, 푸리에 해석, 2포트 네트워크 등)을 누락 없이 순서대로 포함하세요.
2. 각 단원별 균등 구조화:
   - 각 대단원마다 [Recall(선수 개념) -> 핵심 개념/대칭 구조 -> 대표 계산 예제 1개 -> #보이스피싱 또는 #함정주의]가 골고루 배분되도록 작성하세요.

[엄격 수식 및 표준 전공 교재 연계 규칙]
1. 표준 전공서(Alexander & Sadiku 회로이론, Griffiths 등) 내장 지식 교차 참조:
   - 본 강의 자료/필기의 핵심 주제와 직접 매핑되는 표준 전공 교재의 정석 표기법(Notation), 논리적 증명 단계, 핵심 연습문제 패턴을 능동적으로 연계하여 필기에서 누락된 물리적 맥락을 자연스럽게 보강하세요.
2. 원본 필기 오류 및 오개념 능동 검증 (Fact-Check):
   - 원본 손글씨/문서에 오류(예: RMS vs Peak 전력 계수 1/2 누락, 변수 혼용 등)가 발견될 경우 올바른 수식으로 교정하여 반영하세요.
   - 오개념 교정 사항은 #함정주의 박스에 명시하세요.
3. 수식 정확도 및 물리 차원(Dimension) 검증:
   - 모든 LaTeX 수식에서 상수와 기하 변수가 뒤바뀌거나 누락되지 않도록 검증하세요.
   - 수식은 반드시 단독 블록($$...$$) 또는 인라인($...$)으로 명확히 닫아주세요.

[컴포넌트 HTML 가이드]
- 최상단 요약 박스: <div class="summary-box"><strong> 핵심 요약</strong>: 전체 대단원을 아우르는 3~4줄 핵심 요약</div>
- 💡 Recall 박스:
   <div class="recall-box">
     <div class="recall-header">💡 Recall (선수 개념 & 리마인드)</div>
     <p><strong>꼭 기억해야 할 배경 지식:</strong> 설명</p>
     <div class="recall-formula">$$ 필수 수식/정리 $$</div>
   </div>
- 핵심 포인트: <div class="callout-box"><strong> Key Point:</strong> ... </div>
- 개념 대칭 구조:
   <div class="concept-map">
     <div class="map-col"><div class="map-header">좌측 개념명</div><div class="map-formula">$$ 수식 $$</div><p class="map-desc">설명</p></div>
     <div class="map-arrow">$$\\longleftrightarrow$$</div>
     <div class="map-col"><div class="map-header">우측 개념명</div><div class="map-formula">$$ 수식 $$</div><p class="map-desc">설명</p></div>
   </div>
- 실전 적용 예제:
   <div class="example-box">
     <div class="example-header">📝 실전 적용 예제 (Example Problem)</div>
     <div class="example-question"><strong>[문제]</strong> 문제 상황 및 조건</div>
     <div class="example-solution">
       <div class="solution-title"> 정석 풀이 및 계산 과정:</div>
       <div class="calc-step">$$ 1단계: 수식 전개 $$</div>
       <div class="calc-step">$$ \\therefore 최종 결과 $$</div>
     </div>
   </div>
- 시험용 숏컷 (#보이스피싱):
   <div class="voice-phishing-box">
     <div class="phishing-header">⚡ #보이스피싱 (실전 초단축 풀이법)</div>
     <div class="phishing-formula">$$ 단축 공식 $$</div>
     <p class="phishing-desc">실전 적용 팁</p>
   </div>
- 시험 함정 주의 (#함정주의):
   <div class="trap-box">
     <div class="trap-header">⚠️ #함정주의 (자주 낚이는 오개념 & 실수 포인트)</div>
     <p class="trap-desc">감점 방지 팁</p>
   </div>
- 시각화 다이어그램 (경량 인라인 SVG):
   <div class="svg-container">
     <svg viewBox="0 0 400 140" xmlns="http://www.w3.org/2000/svg"><!-- 회로/도식 --></svg>
     <div class="caption">도식 설명</div>
   </div>
- 최하단 단원 공식 치트시트:
   <table class="cheat-sheet-table">
     <thead><tr><th>공식/정리명</th><th>핵심 수식 (LaTeX)</th><th>적용 조건 및 핵심 주의사항</th></tr></thead>
     <tbody>
       <tr><td>항목명</td><td>$$ 수식 $$</td><td>조건 및 팁</td></tr>
     </tbody>
   </table>

* 별도의 html, head, body 태그 없이 <div>로 감싼 순수 HTML 본문만 반환하세요.
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
            return response.text
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
  .highlight {{ background-color: #FEFCBF; padding: 2px 5px; border-radius: 4px; font-weight: 600; }}
  .summary-box {{ background-color: #EBF8FF; border-left: 5px solid #3182CE; border-radius: 4px 8px 8px 4px; padding: 14px; margin-bottom: 20px; }}
  .formula-box {{ background-color: #F8FAFC; border: 1px solid #E2E8F0; border-left: 5px solid #4A5568; border-radius: 4px 8px 8px 4px; padding: 12px; margin: 12px 0; }}
  .callout-box {{ background-color: #FFFDF5; border-left: 5px solid #D69E2E; padding: 12px 14px; margin: 12px 0; border-radius: 4px 8px 8px 4px; }}
  
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
  .solution-title {{ font-weight: 700; color: #2C7A7B; margin-bottom: 4px; }}
  .calc-step {{ background-color: #FFFFFF; border: 1px solid #EDF2F7; border-radius: 4px; padding: 8px; margin: 6px 0 10px 0; text-align: center; }}

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
                page_id=page_id, properties={"정리본 링크": {"url": download_url}}
            )


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

            print(
                f"'{main_title}' (총 {len(files)}개 파일) 종합 분석 및 디자인"
                " PDF 생성 중..."
            )

            try:
                body_html = extract_and_design_multiple_files(files)
                full_html = build_full_html(main_title, body_html)

                temp_pdf_path = os.path.join(temp_dir, f"{main_title}_정리본.pdf")
                render_html_to_pdf(full_html, temp_pdf_path)

                print("  -> GitHub Storage에 통합본 업로드 중...")
                pdf_url = upload_pdf_to_github_release(
                    temp_pdf_path, f"{main_title}_정리본.pdf"
                )
                print(f"  -> 다운로드 링크 생성 완료: {pdf_url}")

                update_notion_success(page_id, pdf_url)
                print("  -> Notion 업데이트 완료!\n")

                time.sleep(5)

            except Exception as e:
                print(f"  -> 실패: {e}\n")


if __name__ == "__main__":
    main()
