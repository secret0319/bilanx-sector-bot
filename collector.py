"""
BILANX 섹터 트래픽 봇
매일 GICS 11개 섹터의 미국 시장 동향을 가져와서
텔레그램으로 인라인 키보드 형태의 브리핑을 발행한다.
"""

import os
import json
import time
import requests
from datetime import datetime

# ── 환경변수 (GitHub Secrets에서 주입) ──
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
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

# 실패 시 기본값 (None 대신 사용 — 비교 연산 오류 방지)
EMPTY_RESULT = {"change_percent": 0, "news": []}


def fetch_sector_page(index_code: str) -> str:
    """Google Finance 섹터 페이지를 가져온다."""
    url = f"https://www.google.com/finance/beta/quote/{index_code}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()
    return res.text


def extract_with_gemini(sector_name: str, html: str, max_retries: int = 3) -> dict:
    """Gemini에게 HTML을 던져서 등락률과 뉴스 리스트를 구조화된 JSON으로 추출.
    503(서버 일시 오류) 시 최대 3회까지 재시도한다."""
    prompt = f"""다음은 Google Finance에서 가져온 "{sector_name}" 섹터 페이지의 HTML/텍스트입니다.

여기서 다음 정보를 추출해 JSON으로만 응답하세요 (다른 설명 없이):
{{
  "change_percent": 숫자 (오늘 등락률, %, 음수 가능),
  "news": [
    {{"title": "뉴스 제목 한국어 요약", "source": "출처", "time": "몇 분/시간 전"}}
  ]
}}

뉴스는 최대 5개까지만, 원문에 있는 사실만 사용하고 추론하지 마세요.
정보를 찾을 수 없으면 change_percent는 0, news는 빈 배열로 응답하세요.

페이지 내용:
{html[:8000]}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

    for attempt in range(1, max_retries + 1):
        try:
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
            parsed = json.loads(raw)

            # change_percent가 None이거나 숫자가 아니면 0으로 보정
            pct = parsed.get("change_percent")
            if pct is None or not isinstance(pct, (int, float)):
                parsed["change_percent"] = 0
            if "news" not in parsed or not isinstance(parsed["news"], list):
                parsed["news"] = []

            return parsed

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 503 and attempt < max_retries:
                wait = attempt * 5
                print(f"  [RETRY] {sector_name}: 503 오류, {wait}초 후 재시도 ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"  [FAIL-HTTP] {sector_name}: {e}")
            return dict(EMPTY_RESULT)

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  [FAIL-PARSE] {sector_name}: {e}")
            return dict(EMPTY_RESULT)

    return dict(EMPTY_RESULT)


def collect_all_sectors() -> dict:
    """11개 섹터를 순회하며 데이터 수집. 실패해도 절대 전체를 멈추지 않는다."""
    result = {}
    for name, code in SECTORS.items():
        try:
            html = fetch_sector_page(code)
            parsed = extract_with_gemini(name, html)
        except Exception as e:
            print(f"[FAIL-FETCH] {name}: {e}")
            parsed = dict(EMPTY_RESULT)

        result[name] = parsed
        pct = parsed.get("change_percent", 0)
        news_count = len(parsed.get("news", []))
        status = "OK" if news_count > 0 else "EMPTY"
        print(f"[{status}] {name}: {pct}%, 뉴스 {news_count}건")

    return result


def save_data(data: dict):
    payload = {
        "updated_at": datetime.utcnow().isoformat(),
        "sectors": data,
    }
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def safe_pct(info: dict) -> float:
    """change_percent를 항상 숫자로 안전하게 반환."""
    pct = info.get("change_percent", 0)
    if pct is None or not isinstance(pct, (int, float)):
        return 0.0
    return float(pct)


def build_main_keyboard(data: dict) -> dict:
    """섹터별 카드를 인라인 키보드 버튼으로 구성. 변동폭 절대값 큰 순으로 정렬."""
    sorted_sectors = sorted(
        data.items(),
        key=lambda x: abs(safe_pct(x[1])),
        reverse=True,
    )
    buttons = []
    row = []
    for name, info in sorted_sectors:
        pct = safe_pct(info)
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
