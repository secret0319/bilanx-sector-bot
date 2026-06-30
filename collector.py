"""
BILANX 섹터 트래픽 봇
매일 GICS 11개 섹터의 미국 시장 동향을 가져와서
텔레그램으로 인라인 키보드 형태의 브리핑을 발행한다.
"""

import os
import json
import requests
from datetime import datetime

# ── 환경변수 (GitHub Secrets에서 주입) ──
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]  # 채널 ID (예: @bilanx_research)
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# ── GICS 11개 섹터 - Google Finance 인덱스 코드 매핑 ──
SECTORS = {
    "정보기술":      "SIXT:INDEXCBOE",
    "금융":          "SIXM:INDEXCBOE",
    "헬스케어":      "SIXV:INDEXCBOE",
    "임의소비재":    "SIXY:INDEXCBOE",
    "필수소비재":    "SIXR:INDEXCBOE",
    "산업재":        "SIXI:INDEXCBOE",
    "소재":          "SIXB:INDEXCBOE",
    "에너지":        "SIXE:INDEXCBOE",
    "유틸리티":      "SIXU:INDEXCBOE",
    "부동산":        "SIXRE:INDEXCBOE",
    "커뮤니케이션서비스": "SIXC:INDEXCBOE",
}

STORAGE_FILE = "sector_data.json"


def fetch_sector_page(index_code: str) -> str:
    """Google Finance 섹터 페이지를 가져온다."""
    url = f"https://www.google.com/finance/beta/quote/{index_code}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()
    return res.text


def extract_with_gemini(sector_name: str, html: str) -> dict:
    """Gemini에게 HTML을 던져서 등락률과 뉴스 리스트를 구조화된 JSON으로 추출."""
    prompt = f"""다음은 Google Finance에서 가져온 "{sector_name}" 섹터 페이지의 HTML/텍스트입니다.

여기서 다음 정보를 추출해 JSON으로만 응답하세요 (다른 설명 없이):
{{
  "change_percent": 숫자 (오늘 등락률, %, 음수 가능),
  "news": [
    {{"title": "뉴스 제목 한국어 요약", "source": "출처", "time": "몇 분/시간 전"}}
  ]
}}

뉴스는 최대 5개까지만, 원문에 있는 사실만 사용하고 추론하지 마세요.

페이지 내용:
{html[:8000]}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    res = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024},
        },
        timeout=30,
    )
    res.raise_for_status()
    data = res.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"change_percent": 0, "news": []}


def collect_all_sectors() -> dict:
    """11개 섹터를 순회하며 데이터 수집."""
    result = {}
    for name, code in SECTORS.items():
        try:
            html = fetch_sector_page(code)
            parsed = extract_with_gemini(name, html)
            result[name] = parsed
            print(f"[OK] {name}: {parsed.get('change_percent')}%, 뉴스 {len(parsed.get('news', []))}건")
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            result[name] = {"change_percent": 0, "news": []}
    return result


def save_data(data: dict):
    payload = {
        "updated_at": datetime.utcnow().isoformat(),
        "sectors": data,
    }
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def build_main_keyboard(data: dict) -> dict:
    """섹터별 카드를 인라인 키보드 버튼으로 구성. 변동폭 절대값 큰 순으로 정렬."""
    sorted_sectors = sorted(
        data.items(),
        key=lambda x: abs(x[1].get("change_percent", 0)),
        reverse=True,
    )
    buttons = []
    row = []
    for i, (name, info) in enumerate(sorted_sectors):
        pct = info.get("change_percent", 0)
        arrow = "▲" if pct >= 0 else "▼"
        label = f"{name} {arrow}{abs(pct):.1f}%"
        row.append({"text": label, "callback_data": f"sector:{name}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return {"inline_keyboard": buttons}


def send_telegram_message(text: str, reply_markup: dict = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    res = requests.post(url, json=payload, timeout=15)
    res.raise_for_status()
    return res.json()


def main():
    print("=== 섹터 데이터 수집 시작 ===")
    data = collect_all_sectors()
    save_data(data)

    today = datetime.now().strftime("%Y.%m.%d")
    header = f"📊 <b>BILANX RESEARCH</b>\n오늘의 섹터별 트래픽 ({today})\n\n섹터를 누르면 관련 뉴스를 볼 수 있어요."
    keyboard = build_main_keyboard(data)

    send_telegram_message(header, keyboard)
    print("=== 텔레그램 발행 완료 ===")


if __name__ == "__main__":
    main()
