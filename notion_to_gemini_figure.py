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
        # '참고 사진' 컬럼이 비어있는 항목만 필터링
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


def extract_and_design_figures(file_list: list, subject_hint: str = "", unit_hint: str = "") -> tuple:
    content_payload = []
    
    # 피드백을 통해 학습·축적된 SVG 고도화 프롬프트
    prompt_text = f"""당신은 세계적 이공계열 전공 교재의 수석 테크니컬 일러스트레이터입니다.
첨부된 자료에서 직관 형성에 필수적인 핵심 그래프, 벡터장, 전자기학적 기하 구조, 회로도 3~5개를 선별하여 정밀 SVG로 작도하고,
각각에 대해 1~2줄 핵심 설명이 담긴 [참고 사진 리포트]를 작성해주세요.
(참고 과목: {subject_hint}, 단원명: {unit_hint})

[필수 출력 양식 1단계: 제목 생성]
답변 첫 줄에 반드시 다음 형식으로 출력:
DOC_TITLE: [{subject_hint} - {unit_hint}] 핵심 도판 및 시각 자료 정리

[2단계: 본문 HTML 작성]
제목 아랫줄부터는 본문 HTML 코드만 작성하세요. 각 도판은 반드시 아래 <div class="figure-card"> 구조를 따릅니다.

★ [도판 카드 표준 마크업 양식]
<div class="figure-card">
  <div class="figure-header">
    <span class="badge badge-blue">Fig 1</span> <strong>(도판 제목: 예 - 직렬 RLC 회로의 공진 주파수 응답 곡선)</strong>
  </div>
  <div class="svg-container">
    <!-- SVG 코드 본문 (반드시 viewBox를 정의하여 반응형 크기 지원) -->
  </div>
  <div class="figure-desc">
    <p class="desc-line"><strong>현상 및 조건:</strong> [X축]에 따른 [Y축]의 변화를 나타내며, ...일 때 성립함.</p>
    <p class="desc-line"><strong>시각적 핵심:</strong> [변곡점/기울기/특이점]에서 ... 메커니즘이 발생하므로 주의.</p>
  </div>
</div>

★ [피드백 학습 기반 SVG 작도 절대 규칙]
1. [라벨 글자 깨짐 방지]: SVG 내부의 모든 수식과 축 라벨은 <foreignObject width="..." height="...">를 쓰고, 내부에 $...$ KaTeX 문법을 사용하여 선명하게 렌더링할 것.
2. [벡터 표기 규칙]: 
   - 벡터 화살표: \\vec{{E}}, \\vec{{B}}, \\vec{{A}}, \\vec{{J}}, \\vec{{\\nabla}} (볼드체 \\mathbf 절대 금지)
   - 단위 벡터: \\hat{{n}}, \\hat{{r}}, \\hat{{x}}, \\hat{{y}}, \\hat{{z}}
   - 미소 요소: d\\vec{{l}}, d\\vec{{a}} = \\hat{{n}} da
3. [좌표축 및 시각성]:
   - 축 화살표(<marker>)를 명확히 정의하고 축 이름($x, y, z$ 또는 주파수, 시간)을 끝점에 반드시 표시.
   - 배경은 투명 또는 #FFFFFF, 주 선 색상은 #2563EB(Blue), #DC2626(Red), 보조선은 점선(#94A3B8) 사용.
4. [설명 분량 제약]:
   - 도판 설명(figure-desc)은 줄글을 길게 쓰지 말고, 현상/조건 1줄, 시각적 핵심 포인트 1줄로 딱 2줄 요약할 것!
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
        print(f"  [참고 사진] -> [{model_name}] 모델 호출 시도 중...")
        try:
            current_model = genai.GenerativeModel(model_name)
            response = current_model.generate_content(
                content_payload, request_options={"timeout": 600}
            )
            raw_text = response.text
            
            extracted_title = "전공_참고_사진"
            body_html = raw_text
            
            match = re.search(r"DOC_TITLE:\s*(.+)", raw_text)
            if match:
                extracted_title = match.group(1).strip()
                body_html = re.sub(r"DOC_TITLE:\s*.+\n?", "", raw_text).strip()
                
            print(f"  [참고 사진] -> [{model_name}] 생성 성공!")
            return extracted_title, body_html

        except Exception as e:
            last_exception = e
            print(f"  [경고] {model_name} 실패 (사유: {e})")
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
    line-height: 1.7; 
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
    font-size: 21px; 
    font-weight: 800; 
    color: #0F172A; 
    margin: 0 0 6px 0; 
    letter-spacing: -0.5px;
  }}
  .doc-subtitle {{ font-size: 12px; color: #64748B; margin: 0; font-weight: 500; }}

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
  .badge-blue {{ background-color: #EFF6FF; color: #2563EB; border: 1px solid #BFDBFE; }}

  .figure-card {{
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    padding: 16px;
    margin: 22px 0;
    page-break-inside: avoid;
  }}
  .figure-header {{
    font-size: 14px;
    font-weight: 700;
    color: #0F172A;
    margin-bottom: 12px;
    border-bottom: 1px solid #F1F5F9;
    padding-bottom: 8px;
  }}
  .svg-container {{ 
    text-align: center; 
    margin: 12px 0; 
    background-color: #FFFFFF; 
    border-radius: 6px; 
    padding: 10px; 
  }}
  .svg-container svg {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}

  .figure-desc {{
    background-color: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 10px 14px;
    margin-top: 12px;
    font-size: 12px;
    line-height: 1.65;
  }}
  .figure-desc p {{ margin: 4px 0; }}
  .desc-line strong {{ color: #1E293B; }}
</style>
</head>
<body>
  <div class="header-container">
    <h1 class="doc-title">{title}</h1>
    <p class="doc-subtitle">핵심 도판 및 시각 자료 분석 카드북</p>
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
            margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
        )
        browser.close()


def update_notion_figure_success(page: dict, download_url: str):
    page_id = page["id"]
    props = page.get("properties", {})
    
    update_data = {"참고 사진": {"url": download_url}}
    
    # '내용 요약본' 컬럼도 이미 URL이 들어있으면 최종 '완료' 처리
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
        print("[참고 사진] 처리할 새 항목이 없습니다.")
        return

    print(f"[참고 사진] 미처리 항목 {len(items)}개 발견.")

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

            print(f"\n[참고 사진 작업 시작] 과목: '{subject_hint}', 단원: '{unit_hint}' (파일 {len(files)}개)")

            try:
                doc_title, body_html = extract_and_design_figures(files, subject_hint, unit_hint)
                safe_title = sanitize_filename(doc_title)
                
                full_html = build_full_html(doc_title, body_html)
                temp_pdf_path = os.path.join(temp_dir, f"{safe_title}.pdf")
                render_html_to_pdf(full_html, temp_pdf_path)

                print("  -> GitHub Storage 업로드 중...")
                pdf_url = upload_pdf_to_github_release(temp_pdf_path, f"{safe_title}.pdf")
                print(f"  -> 다운로드 링크: {pdf_url}")

                update_notion_figure_success(page, pdf_url)
                print("  -> Notion '참고 사진' 등록 완료!\n")

                time.sleep(1)

            except Exception as e:
                print(f"  -> 최종 실패: {e}\n")


if __name__ == "__main__":
    main()
