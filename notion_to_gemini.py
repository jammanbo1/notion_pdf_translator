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

# 우선순위별 모델 폴백 리스트
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
    
    prompt_text = """당신은 최고의 대학 이공계열 전공 학업 요약 전문가이자 세계적 물리학/수학 교재의 공식 삽화가입니다.
첨부된 자료를 정밀 분석하여, 표준 전공 교재(Stewart Calculus 9th, Griffiths Electrodynamics, Feynman Lectures)의 Figure와 100% 일치하는 엄밀한 SVG 다이어그램 및 학습 점검용 실전 예제가 포함된 A4 요약 리포트를 작성해주세요.
(참고 과목: """ + subject_hint + """, 단원명: """ + unit_hint + """)

[필수 출력 양식 1단계: 제목 생성]
답변 첫 줄에 반드시 다음 형식으로 출력:
DOC_TITLE: [과목/단원 핵심 키워드 중심의 명확한 리포트 제목]

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요.

★ [매우 중요: 벡터 기호 상단 화살표 표기 절대 규칙]
1. 모든 벡터는 굵은 볼드체(\\mathbf)를 절대 쓰지 말고, 해당 문자 바로 위에 화살표(\\vec{...})를 그려서 표기할 것!
   - 예: \\vec{v}, \\vec{E}, \\vec{B}, \\vec{A}, \\vec{F}, \\vec{r}, \\vec{h}, \\vec{\\nabla}
2. 미소 벡터 변위 및 면적 요소:
   - d 전체가 아닌 문자 위에만 화살표를 표기할 것: d\\vec{l}, d\\vec{r}, d\\vec{s}, d\\vec{a} = \\hat{n} da, d\\vec{S} = \\hat{n} dS
   - 예: \\int_C \\vec{F} \\cdot d\\vec{r}, \\quad \\oint \\vec{B} \\cdot d\\vec{l}, \\quad \\iint_S \\vec{E} \\cdot d\\vec{a}
3. 단위 벡터(Unit Vector)는 윗꺽쇠(\\hat{...})로 통일:
   - \\hat{n}, \\hat{r}, \\hat{x}, \\hat{y}, \\hat{z}, \\hat{\\theta}, \\hat{\\phi}

[핵심 규칙 1: 전공 표준 교재 Figure(도판) 1:1 완벽 정밀 복원]
절대로 개념과 무관한 임의의 그림이나 장식용 다이어그램을 그리지 마세요.
수학/물리/생물 개념이 등장할 경우, 반드시 표준 교재의 정식 Figure 구조를 SVG(<div class="svg-container"><svg viewBox="0 0 600 350" ...>...</svg><p class="caption">Fig. [교재식 번호 및 설명]</p></div>)로 최소 3~5개 이상 작도하세요:
- Stewart Calculus 규격: 3차원 오른손 좌표계, 등위곡선군 및 수직 그레이디언트 벡터장, 곡면 적분/스토크스 정리 정사영 D.
- Griffiths/Feynman 규격: 가우스 폐곡면 유선과 법선 분해 성분, 와도 및 폐루프 순환(Circulation), 분리 벡터 삼각도.
- 생명/공학 규격: 원형 플라스미드 맵, Blotting 적층 장치도, 유전체 지도 축 비교도, 메모리 맵.

[핵심 규칙 2: 최종 학습 점검용 실전 연습 예제 (Practice Problems) 필수 수록]
본문 마지막 부분(치트시트 직전 또는 직후)에 해당 단원의 핵심 개념을 종합 평가할 수 있는 대표 고난도 실전 예제 1~2개를 반드시 아래 구조로 수록하세요:

<div class="practice-box">
  <div class="practice-header">🎯 [학습 점검] 핵심 개념 실전 적용 예제</div>
  <div class="practice-question">
    <strong>[문제]</strong> (실제 대학 중간/기말고사 또는 전공 자격시험 스타일의 정밀한 문제 상황 제시)
  </div>
  <div class="practice-solution">
    <div class="step-label">Step 1. 문제 분석 및 핵심 조건/공식 수립</div>
    <p>...</p>
    <div class="step-label">Step 2. 수식 전개 및 단계별 풀이 과정</div>
    <div class="calc-step">$$ ... $$</div>
    <div class="step-label">Step 3. 물리적/학문적 의미 해석 및 오답 함정 방어</div>
    <p>...</p>
  </div>
</div>

[본문 구성 및 마크업 규칙]
1. Mindset 액션 가이드 (<div class="mindset-box">)
2. 한 줄 직관 비유 (<div class="analogy-box">)
3. 샤프/연필 필기 체크포인트 (<div class="checkpoint-box"><span class="checkpoint-tag">#체크포인트</span> 원문 메모 <span class="tutor-add">(튜터 첨언: ...)</span></div>)
4. 수식 표기: 모든 수식은 엄밀한 LaTeX $...$ 및 $$...$$ 사용. (벡터는 무조건 \\vec{...})
5. 최종 학습 점검 실전 예제 (<div class="practice-box">)
6. 시험 대비 치트시트 테이블 (<table class="cheat-sheet-table">)
7. 별도의 <html>, <head>, <body> 태그 없이 <div>로 감싼 순수 HTML 본문만 반환할 것.
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
        print(f"  -> [{model_name}] 모델로 분석, 도판 SVG 및 실전 점검 예제 렌더링 시도 중...")
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
  @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap');
  @page {{ size: A4; margin: 18mm 14mm; }}
  body {{ font-family: 'Pretendard', sans-serif; color: #2D3748; line-height: 1.7; font-size: 13px; margin: 0; }}
  .header-container {{ border-bottom: 2px solid #2B6CB0; padding-bottom: 12px; margin-bottom: 20px; }}
  .doc-title {{ font-size: 21px; font-weight: 800; color: #1A365D; margin: 0 0 6px 0; }}
  .doc-subtitle {{ font-size: 12px; color: #718096; margin: 0; }}
  h2 {{ font-size: 16px; font-weight: 700; color: #2B6CB0; border-left: 4px solid #3182CE; padding-left: 8px; margin-top: 24px; }}
  h3 {{ font-size: 14px; font-weight: 700; color: #2D3748; margin-top: 16px; }}
  
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
  
  .svg-container {{ text-align: center; margin: 18px 0; background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  .svg-container svg {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}
  .caption {{ font-size: 11.5px; color: #4A5568; font-weight: 600; margin-top: 8px; text-align: center; }}

  .practice-box {{ background-color: #F8FAFC; border: 1.5px solid #CBD5E0; border-left: 5px solid #4C51BF; border-radius: 4px 8px 8px 4px; padding: 16px; margin: 24px 0; }}
  .practice-header {{ font-weight: 800; font-size: 14px; color: #3C366B; margin-bottom: 10px; border-bottom: 1px solid #E2E8F0; padding-bottom: 6px; }}
  .practice-question {{ background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 6px; padding: 12px; margin-bottom: 12px; font-size: 12.5px; line-height: 1.6; }}
  .practice-solution {{ background-color: #F7FAFC; border-radius: 6px; padding: 10px 12px; font-size: 12px; }}
  .practice-solution .step-label {{ font-weight: 700; color: #2B6CB0; margin-top: 8px; margin-bottom: 2px; }}
  .calc-step {{ background-color: #FFFFFF; border: 1px solid #EDF2F7; border-radius: 4px; padding: 8px; margin: 4px 0 8px 0; text-align: center; }}

  .concept-map {{ display: flex; justify-content: space-between; align-items: stretch; background-color: #F7FAFC; border: 1px solid #CBD5E0; border-radius: 8px; padding: 14px; margin: 16px 0; gap: 10px; }}
  .map-col {{ flex: 1; background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 6px; padding: 12px; }}
  .map-header {{ font-weight: 700; font-size: 13px; color: #2B6CB0; margin-bottom: 8px; border-bottom: 1.5px solid #E2E8F0; padding-bottom: 4px; text-align: center; }}

  .voice-phishing-box {{ background-color: #FAF5FF; border: 1.5px solid #D6BCFA; border-left: 5px solid #805AD5; border-radius: 4px 8px 8px 4px; padding: 14px; margin: 16px 0; }}
  .trap-box {{ background-color: #FFF5F5; border: 1.5px solid #FEB2B2; border-left: 5px solid #E53E3E; border-radius: 4px 8px 8px 4px; padding: 12px 14px; margin: 14px 0; }}

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
        page.wait_for_timeout(800)
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
                print("  -> Notion 업데이트 완료 (링크 등록 완료)!\n")

                time.sleep(1)

            except Exception as e:
                print(f"  -> 최종 실패: {e}\n")


if __name__ == "__main__":
    main()
