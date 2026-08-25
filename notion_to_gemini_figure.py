import os
import re
import json
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


# ==============================================================================
# STAGE 1: 도판 기획기 (Planner) - 핵심 도판 2개 선정 및 설계서(JSON) 추출
# ==============================================================================
def plan_figures_with_gemini(raw_file_payload: list, subject_hint: str, unit_hint: str) -> dict:
    prompt_text = f"""당신은 세계 최고 수준의 이공계 전공서적 전문 편집위원장입니다.
첨부된 강의 자료를 분석하여, 학생들에게 시각적 직관과 물리적 이해를 제공하기 위한 [가장 핵심적인 도판 딱 2개]를 엄선하여 기획서를 작성하세요.
(과목: {subject_hint}, 단원명: {unit_hint})

★ 절대 규칙:
1. 도판 개수는 무조건 [가장 중요한 2개]만 선정하세요.
2. SVG 코드나 HTML 태그는 일절 작성하지 마세요. (순수 기획 데이터만 생성)
3. 출력은 반드시 아래의 JSON 포맷으로만 응답해야 합니다. (앞뒤 잡담, 마크다운 설명 금지)

{{
  "doc_title": "[{subject_hint} - {unit_hint}] 핵심 시각 자료 및 도판 해설집",
  "figures": [
    {{
      "fig_num": "Fig 1",
      "title": "도판 1 제목 (예: 직렬 RLC 회로의 과도 응답 및 복소 주파수 평면)",
      "condition": "현상 및 조건 (회로 파라미터, 입력 신호, 기하학적 조건 등 LaTeX 수식 포함 1~2줄 서술)",
      "visual_key": "시각적 핵심 (파형의 시정수, 3D 투영, 오버슈트, 점근선 등 핵심 해석 1~2줄 서술)",
      "drawing_spec": "SVG 작도를 위한 구체적 설계 지침 (예: 좌측에는 RLC 폐회로 소자 배치, 우측에는 감쇠 진동 곡선 v(t)와 시간축 t, 시정수 가이드 점선 배치 등)"
    }},
    {{
      "fig_num": "Fig 2",
      "title": "도판 2 제목",
      "condition": "현상 및 조건 서술",
      "visual_key": "시각적 핵심 서술",
      "drawing_spec": "SVG 작도를 위한 구체적 설계 지침"
    }}
  ]
}}
"""
    payload = [prompt_text] + raw_file_payload

    for model_name in FALLBACK_MODELS:
        try:
            print(f"  [Stage 1: 도판 기획] -> [{model_name}] 호출 중...")
            model = genai.GenerativeModel(model_name, generation_config={"temperature": 0.2, "response_mime_type": "application/json"})
            res = model.generate_content(payload, request_options={"timeout": 300})
            
            clean_json = res.text.strip()
            # JSON 추출 방어
            match = re.search(r"(\{[\s\S]*\})", clean_json)
            if match:
                clean_json = match.group(1)
            
            data = json.loads(clean_json)
            print(f"  [Stage 1: 기획 완료] 도판 {len(data.get('figures', []))}개 기획 수립 성공!")
            return data
        except Exception as e:
            print(f"  [Stage 1 경고] {model_name} 실패: {e}")
            time.sleep(2)
            continue

    raise RuntimeError("Stage 1 도판 기획 단계 실패")


# ==============================================================================
# STAGE 2: 도판 전문 작도기 (Artist) - 단일 도판 1:1 순수 SVG 렌더링
# ==============================================================================
def draw_single_svg_with_gemini(fig_info: dict, subject_hint: str) -> str:
    prompt_text = f"""당신은 세계 최고의 물리/수학/공학 도판 전문 SVG 아티스트입니다.
아래 주어진 [도판 기획서]를 바탕으로, 전공 교재에 수록될 최고 해상도의 [단색 흑백(Monochrome) 인라인 SVG 코드]를 작도하세요.

[도판 정보]
- 과목: {subject_hint}
- 도판 번호: {fig_info.get('fig_num')}
- 도판 제목: {fig_info.get('title')}
- 작도 상세 지침: {fig_info.get('drawing_spec')}

★ [출력 절대 규칙]
1. 오직 `<svg ...> ... </svg>` 태그만 출력하세요.
2. 앞뒤 설명, HTML 태그, 마크다운 코드블록(```) 등 메타 텍스트를 1글자도 출력하지 마세요.

★ [SVG 작도 스타일 규격]
1. 뷰박스 및 레이아웃:
   - `<svg viewBox="0 0 540 280" width="100%" height="240" xmlns="[http://www.w3.org/2000/svg](http://www.w3.org/2000/svg)">` 표준 적용.
2. 모노크롬 전공서 스타일:
   - 외곽선/주요 곡선/중요 벡터: `#0F172A` (stroke-width: 2.0 ~ 2.5)
   - 좌표축/회로 도선/눈금: `#334155` 또는 `#475569` (stroke-width: 1.2 ~ 1.4)
   - 보조선/투영 점선/점근선: `#64748B` 또는 `#94A3B8` (stroke-dasharray="4,4")
   - 3D 입체 음영/면적: Linear/Radial Gradient (`#FFFFFF` -> `#F1F5F9` -> `#CBD5E1` -> `#94A3B8`)
3. 3차원 투영 및 기하학:
   - z축(수직 상향), x축(좌하단 사선), y축(우측 수평/우상향).
   - 뒷면 가림선 점선 처리, 3D 단면 원은 납작한 타원(`rx:ry ≈ 3:1 ~ 4:1`) 작도.
4. 엄밀한 라벨링:
   - 변수/좌표축 기호는 이탤릭 세리프체 (`font-family="Times New Roman, serif" font-style="italic"`).
   - 첨자는 `<tspan>` 활용. 화살표 머리는 `<defs><marker>`로 깔끔하게 처리.
"""
    for model_name in FALLBACK_MODELS:
        try:
            print(f"    [Stage 2: SVG 작도] -> {fig_info.get('fig_num')} ({model_name})...")
            model = genai.GenerativeModel(model_name, generation_config={"temperature": 0.2})
            res = model.generate_content([prompt_text], request_options={"timeout": 300})
            
            raw_svg = res.text.strip()
            # 순수 SVG 태그만 정밀 추출
            svg_match = re.search(r"(<svg[\s\S]*?</svg>)", raw_svg, re.IGNORECASE)
            if svg_match:
                print(f"    [Stage 2: 작도 성공] -> {fig_info.get('fig_num')} 완성!")
                return svg_match.group(1).strip()
            else:
                raise ValueError("SVG 태그를 감지하지 못함")
        except Exception as e:
            print(f"    [Stage 2 경고] {fig_info.get('fig_num')} 실패: {e}")
            time.sleep(2)
            continue

    raise RuntimeError(f"도판 작도 실패: {fig_info.get('fig_num')}")


# ==============================================================================
# STAGE 3: 파이썬 로컬 템플릿 조립기 (Assembly)
# ==============================================================================
def sanitize_latex_html(text: str) -> str:
    """KaTeX 수식 내 부등호 충돌 방지"""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def assemble_full_html(plan_data: dict, rendered_svgs: list) -> str:
    title = plan_data.get("doc_title", "핵심 시각 자료 및 도판 해설집")
    figures_data = plan_data.get("figures", [])

    cards_html = []
    for fig_info, svg_code in zip(figures_data, rendered_svgs):
        fig_num = fig_info.get("fig_num", "Fig")
        fig_title = fig_info.get("title", "")
        cond = sanitize_latex_html(fig_info.get("condition", ""))
        vkey = sanitize_latex_html(fig_info.get("visual_key", ""))

        card_template = f"""
  <div class="figure-card">
    <div class="figure-header">
      <span class="badge">{fig_num}</span> <strong>{fig_title}</strong>
    </div>
    <div class="svg-container">
      {svg_code}
    </div>
    <div class="figure-desc">
      <p><strong>현상 및 조건:</strong> {cond}</p>
      <p><strong>시각적 핵심:</strong> {vkey}</p>
    </div>
  </div>
"""
        cards_html.append(card_template)

    body_content = "\n".join(cards_html)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<link rel="stylesheet" href="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css)">
<script defer src="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js)"></script>
<script defer src="[https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js](https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js)"
        onload="renderMathInElement(document.body, {{
            delimiters: [
                {{left: '$$', right: '$$', display: true}},
                {{left: '$', right: '$', display: false}}
            ],
            throwOnError: false
        }});"></script>
<style>
  @import url('[https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap](https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap)');
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
  {body_content}
</body>
</html>
"""


def render_html_to_pdf(html_content: str, output_pdf_path: str):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html_content, wait_until="networkidle")
        
        # 폰트 및 KaTeX 렌더링 완료 대기
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

            print(f"\n[작업 시작] 과목: '{subject_hint}', 단원: '{unit_hint}' (파일 {len(files)}개)...")

            # 파일 다운로드 및 멀티모달 페이로드 구성
            raw_file_payload = []
            for item in files:
                try:
                    res = requests.get(item["url"], stream=True, timeout=120)
                    res.raise_for_status()
                    mime_type, _ = mimetypes.guess_type(item["name"])
                    if not mime_type:
                        mime_type = "application/pdf" if item["name"].lower().endswith(".pdf") else "image/jpeg"
                    raw_file_payload.append({"mime_type": mime_type, "data": res.content})
                except Exception as e:
                    print(f"  [다운로드 오류] {item['name']}: {e}")

            if not raw_file_payload:
                continue

            try:
                # 1단계: 도판 2개 기획 (JSON 수신)
                plan_data = plan_figures_with_gemini(raw_file_payload, subject_hint, unit_hint)
                
                # 2단계: 기획된 도판 1개씩 순차 독립 작도 (순수 SVG 수신)
                rendered_svgs = []
                for fig_info in plan_data.get("figures", []):
                    svg_code = draw_single_svg_with_gemini(fig_info, subject_hint)
                    rendered_svgs.append(svg_code)
                    time.sleep(1)  # 안정적인 호출 간격

                # 3단계: 파이썬에서 HTML 조립 & PDF 컴파일
                doc_title = plan_data.get("doc_title", f"[{subject_hint} - {unit_hint}] 핵심 시각 자료 및 도판 해설집")
                safe_title = sanitize_filename(doc_title)
                full_html = assemble_full_html(plan_data, rendered_svgs)

                temp_pdf_path = os.path.join(temp_dir, f"{safe_title}.pdf")
                render_html_to_pdf(full_html, temp_pdf_path)

                # 4단계: GitHub Releases 배포 & 노션 링크 갱신
                print("  -> GitHub Releases 저장소 업로드 중...")
                pdf_url = upload_pdf_to_github_release(temp_pdf_path, f"{safe_title}.pdf")
                print(f"  -> 다운로드 링크: {pdf_url}")

                update_notion_figure_success(page, pdf_url)
                print("  -> Notion '참고 사진' 컬럼 업데이트 완료!\n")

                time.sleep(2)

            except Exception as e:
                print(f"  -> 최종 처리 실패: {e}\n")


if __name__ == "__main__":
    main()
