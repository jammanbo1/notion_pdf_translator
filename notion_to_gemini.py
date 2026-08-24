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
    
    # 코멘트 스타일을 위한 지시사항 추가
    prompt_text = """당신은 최고의 대학 이공계열 전공 학업 요약 전문가이자 세계적 물리학/수학 교재의 공식 편집자입니다.
첨부된 자료를 정밀 분석하여, 지정된 컬러 체계(빨간색/파란색/초록색/보라색 및 주황색 코멘트)로 완벽히 통일된 최고급 A4 요약 리포트를 작성해주세요.
(참고 과목: """ + subject_hint + """, 단원명: """ + unit_hint + """)

[필수 출력 양식 1단계: 제목 생성]
답변 첫 줄에 반드시 다음 형식으로 출력:
DOC_TITLE: [과목/단원 핵심 키워드 중심의 명확한 리포트 제목]

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요.

★ [전용 컬러 배정 및 마크업 통일 절대 규칙]
본문의 모든 박스와 뱃지, 코멘트는 반드시 아래 지정된 클래스만 사용하세요:

1. 빨간색 (Red) -> [중요], [체크포인트], 핵심 공식/유도:
   - <div class="note-box note-red"><span class="badge badge-red">중요</span> ...</div>
   - <div class="note-box note-red"><span class="badge badge-red">체크포인트</span> ...</div>
   - <div class="note-box note-red"><span class="badge badge-red">핵심 공식 유도</span> ...</div>

2. 파란색 (Blue) -> [핵심 개념], [학습 목표]:
   - <div class="note-box note-blue"><span class="badge badge-blue">학습 목표</span> ...</div>
   - <div class="note-box note-blue"><span class="badge badge-blue">핵심 개념</span> ...</div>

3. 초록색 (Green) -> [직관 비유], [해석 팁]:
   - <div class="note-box note-green"><span class="badge badge-green">직관 비유</span> ...</div>

4. 보라색 (Purple) -> [학습 점검], [실전 예제]:
   - 아래 실전 점검 예제(practice-box)에 전용 적용:
   <div class="practice-box">
     <div class="practice-header"><span class="badge badge-purple">학습 점검</span> 실전 기출/적용 예제</div>
     <div class="practice-question">
       <strong>[문제]</strong> (상황 제시 및 질문)
     </div>
     <div class="practice-solution">
       <div class="step-label">Step 1. 문제 모델링 및 핵심 공식 수립</div>
       <p>...</p>
       <div class="step-label">Step 2. 수식 전개 과정</div>
       <div class="calc-step">$$ ... $$</div>
       <div class="step-label">Step 3. 결과 해석 및 함정 방어</div>
       <p>...</p>
     </div>
   </div>

5. 주황색 (Orange) -> [코멘트], [참고], [주의]:
   - 본문 내용 중 보충 설명이 필요한 부분 옆이나 아래에 배치하세요.
   <div class="comment-box">
     <span class="badge badge-orange">코멘트</span> (보충 설명, 오개념 주의, 또는 교수님 강조 사항 등)
   </div>

★ [벡터 및 수식 표기 절대 통일 규칙]
1. 모든 벡터는 볼드체(\\mathbf)를 일절 쓰지 말고, 기호 위에 화살표(\\vec{...})를 붙여 일관되게 표기할 것!
   - 예: \\vec{v}, \\vec{E}, \\vec{B}, \\vec{A}, \\vec{F}, \\vec{r}, \\vec{h}, \\vec{\\nabla}
2. 미소 벡터 요소: 문자 위에 직접 화살표 표기 (d\\vec{l}, d\\vec{r}, d\\vec{s}, d\\vec{a} = \\hat{n} da, d\\vec{S} = \\hat{n} dS)
3. 단위 벡터: 윗꺽쇠 표기 (\\hat{n}, \\hat{r}, \\hat{x}, \\hat{y}, \\hat{z})
4. SVG 내부 수식 라벨은 글자 깨짐 방지를 위해 반드시 <foreignObject>를 사용하여 $...$ 형식으로 작성할 것.

★ [전공 표준 교재 스타일 도판 SVG 작도 (최소 3~5개)]
- 해당 단원과 직접 관련된 물리적/수학적/공학적 핵심 상황만 정밀 작도할 것:
  * 3차원 오른손 좌표계, 등위면/등위선 및 법선 벡터장 화살표
  * 가우스 폐곡면, 선적분 폐루프(Amperian loop), 전계/자계 유선
  * 회로도/메모리맵/실험장치도 등 해당 전공에 꼭 필요한 표준 다이어그램

★ [시험 대비 치트시트 테이블]
최하단에 <table class="cheat-sheet-table">로 핵심 공식 및 성질 요약 정리.
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

  /* 뱃지 (Badge) 시스템 */
  .badge {{
    display: inline-block;
    font-size: 11px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    margin-right: 6px;
    letter-spacing: -0.2px;
    vertical-align: middle;
  }}
  /* 1. 빨간색: 중요, 체크포인트, 공식 */
  .badge-red {{ background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; }}
  /* 2. 파란색: 개념, 학습 목표 */
  .badge-blue {{ background-color: #EFF6FF; color: #2563EB; border: 1px solid #BFDBFE; }}
  /* 3. 초록색: 직관 비유, 팁 */
  .badge-green {{ background-color: #F0FDF4; color: #16A34A; border: 1px solid #BBF7D0; }}
  /* 4. 보라색: 실전 점검, 예제 */
  .badge-purple {{ background-color: #FAF5FF; color: #7C3AED; border: 1px solid #E9D5FF; }}
  /* 5. 주황색: 코멘트 전용 */
  .badge-orange {{ background-color: #FFF7ED; color: #EA580C; border: 1px solid #FFEDD5; }}

  /* 모던 노트 박스 */
  .note-box {{
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 12px 14px;
    margin: 12px 0;
    font-size: 12.5px;
    line-height: 1.65;
  }}
  .note-red {{ border-left: 4px solid #DC2626; background-color: #FEF2F20D; }}
  .note-blue {{ border-left: 4px solid #2563EB; background-color: #EFF6FF0D; }}
  .note-green {{ border-left: 4px solid #16A34A; background-color: #F0FDF40D; }}
  .note-purple {{ border-left: 4px solid #7C3AED; background-color: #FAF5FF0D; }}

  /* 코멘트 박스 스타일 추가 (주황색 테마) */
  .comment-box {{
    background-color: #FFF7ED;
    border: 1px solid #FFEDD5;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 10px 0;
    font-size: 12px;
    line-height: 1.6;
    color: #9A3412;
    font-style: italic;
  }}

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
  .practice-box {{ 
    background-color: #FFFFFF; 
    border: 1px solid #E9D5FF; 
    border-left: 4px solid #7C3AED; 
    border-radius: 6px; 
    padding: 15px; 
    margin: 24px 0; 
  }}
  .practice-header {{ 
    font-weight: 700; 
    font-size: 13.5px; 
    color: #5B21B6; 
    margin-bottom: 10px; 
    border-bottom: 1px solid #F3E8FF; 
    padding-bottom: 6px; 
  }}
  .practice-question {{ 
    background-color: #FAF5FF; 
    border: 1px solid #F3E8FF; 
    border-radius: 4px; 
    padding: 11px; 
    margin-bottom: 10px; 
    font-size: 12.5px; 
    line-height: 1.6; 
  }}
  .practice-solution {{ 
    background-color: #FFFFFF; 
    padding: 6px 4px; 
    font-size: 12px; 
  }}
  .practice-solution .step-label {{ 
    font-weight: 700; 
    color: #7C3AED; 
    margin-top: 8px; 
    margin-bottom: 2px; 
  }}
  .calc-step {{ 
    background-color: #FAF5FF; 
    border: 1px solid #F3E8FF; 
    border-radius: 4px; 
    padding: 8px; 
    margin: 4px 0 8px 0; 
    text-align: center; 
  }}

  /* 치트시트 테이블 */
  .cheat-sheet-table {{ 
    width: 100%; 
    border-collapse: collapse; 
    margin: 20px 0 10px 0; 
    font-size: 12px; 
  }}
  .cheat-sheet-table th {{ 
    background-color: #0F172A; 
    color: #FFFFFF; 
    font-weight: 600; 
    padding: 8px 10px; 
    border: 1px solid #334155; 
    text-align: center; 
  }}
  .cheat-sheet-table td {{ 
    border: 1px solid #E2E8F0; 
    padding: 8px 10px; 
    text-align: center; 
    background-color: #FFFFFF; 
  }}
  .cheat-sheet-table tr:nth-child(even) td {{ background-color: #F8FAFC; }}
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
