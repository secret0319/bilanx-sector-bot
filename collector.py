"""
BILANX 섹터 트래픽 봇 (v4)
GICS 11개 섹터 ETF(SPDR Select Sector)의 등락률을 yfinance로 안정적으로 가져오고,
관련 뉴스는 Google News RSS로 가져와 Gemini가 한국어로 요약 + 원문 링크를 함께 보존한다.
"""

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

import yfinance as yf

# ── 환경변수 ──
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# ── GICS 11개 섹터 ↔ SPDR Select Sector ETF 티커 ──
SECTORS = {
    "정보기술":          {"ticker": "XLK",  "query": "technology stocks"},
    "금융":              {"ticker": "XLF",  "query": "financial stocks bank"},
    "헬스케어":          {"ticker": "XLV",  "query": "healthcare pharma stocks"},
    "임의소비재":        {"ticker": "XLY",  "query": "consumer discretionary retail stocks"},
    "필수소비재":        {"ticker": "XLP",  "query": "consumer staples stocks"},
    "산업재":            {"ticker": "XLI",  "query": "industrial stocks"},
    "소재":              {"ticker": "XLB",  "query": "materials mining stocks"},
    "에너지":            {"ticker": "XLE",  "query": "energy oil gas stocks"},
    "유틸리티":          {"ticker": "XLU",  "query": "utilities stocks"},
    "부동산":            {"ticker": "XLRE", "query": "real estate REIT stocks"},
    "커뮤니케이션서비스": {"ticker": "XLC",  "query": "communication services media stocks"},
}

STORAGE_FILE = "sector_data.json"


def fetch_sector_change(ticker: str) -> float:
    """yfinance로 ETF 당일 등락률(%)을 가져온다. 실패하면 0을 반환."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        last = info.get("lastPrice")
        prev = info.get("previousClose")
        if last is None or prev is None or prev == 0:
            return 0.0
        return round((last - prev) / prev * 100, 2)
    except Exception as e:
        print(f"  [FETCH-FAIL] {ticker}: {e}")
        return 0.0


def fetch_news_rss(query: str, max_items: int = 10) -> list:
    """Google News RSS로 키워드 관련 뉴스를 가져온다. 원문 링크와 발행시각도 함께 보존."""
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}+when:1d&hl=en-US&gl=US&ceid=US:en"
    try:
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        root = ET.fromstring(res.content)
        items = []
        for item in root.findall(".//item")[:max_items]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            source_el = item.find("source")
            source = source_el.text if source_el is not None else ""
            pub_date = item.findtext("pubDate", "")  # RFC822 형식, 예: "Mon, 30 Jun 2026 06:30:00 GMT"
            items.append({"title": title, "link": link, "source": source, "pubDate": pub_date})
        return items
    except Exception as e:
        print(f"  [RSS-FAIL] {query}: {e}")
        return []


def summarize_with_gemini(sector_name: str, raw_news: list, max_retries: int = 3) -> list:
    """Gemini로 뉴스 제목들을 한국어로 요약/번역.
    - 발행시각순(최신 우선)으로 검토하도록 지시해 시점이 다른 모순 정보 혼동을 줄인다.
    - 원문 링크는 Gemini를 거치지 않고 코드에서 직접 매칭해 보존한다 (AI가 링크를 잘못 만들 위험 제거)."""
    if not raw_news:
        return []

    # 최신순 정렬 (pubDate 기준) — 같은 주제라도 더 최근 정보를 우선 검토하도록
    def parse_date(item):
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(item["pubDate"])
        except Exception:
            return datetime.min

    sorted_news = sorted(raw_news, key=parse_date, reverse=True)

    # Gemini에게는 인덱스 번호로 식별시켜서, 응답에서 그 인덱스로 원문 링크를 역매칭한다
    numbered_list = "\n".join(
        f"[{i}] {n['title']} ({n['source']}, 발행: {n['pubDate']})"
        for i, n in enumerate(sorted_news)
    )

    prompt = f"""다음은 "{sector_name}" 섹터 관련 영문 뉴스 제목 목록입니다. 발행시각이 최신순으로 정렬되어 있습니다.

{numbered_list}

작업 지침:
1. 중복되거나 의미 없는 것은 제외하고, 최대 4개를 선택하세요.
2. 같은 사안에 대해 서로 다른 시점·맥락의 정보가 섞여 있다면(예: "이번 주 하락" vs "오늘 반등"), 둘 다 의미가 있다면 시점 차이를 제목에 명시하세요 (예: "이번 주 하락세, 단 오늘 새벽 반등").
3. 각 뉴스는 한국어로 짧게 번역/요약하세요. 원문에 없는 내용은 추가하지 마세요.
4. 선택한 뉴스의 원래 인덱스 번호([0], [1] 등)를 반드시 포함하세요.

JSON 형식으로만 응답하세요 (다른 설명 없이):
{{"news": [{{"index": 0, "title": "한국어 요약 제목"}}]}}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900},
                },
                timeout=30,
            )
            res.raise_for_status()
            data = res.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            raw = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)
            picked = parsed.get("news", [])
            if not isinstance(picked, list):
                return []

            # Gemini가 고른 인덱스를 원본 뉴스(링크 포함)와 다시 매칭
            result = []
            for p in picked:
                idx = p.get("index")
                if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(sorted_news):
                    continue
                original = sorted_news[idx]
                result.append({
                    "title": p.get("title", original["title"]),
                    "source": original["source"],
                    "link": original["link"],
                    "time": format_relative_time(original["pubDate"]),
                })
            return result

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 503 and attempt < max_retries:
                wait = attempt * 5
                print(f"  [RETRY] {sector_name}: 503, {wait}초 후 재시도 ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"  [GEMINI-FAIL] {sector_name}: {e}")
            return []
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  [PARSE-FAIL] {sector_name}: {e}")
            return []

    return []


def format_relative_time(pub_date_str: str) -> str:
    """RFC822 발행시각을 'N시간 전' 형태의 한국어 상대시간으로 변환."""
    try:
        from email.utils import parsedate_to_datetime
        pub = parsedate_to_datetime(pub_date_str)
        now = datetime.now(pub.tzinfo)
        diff = now - pub
        hours = int(diff.total_seconds() // 3600)
        if hours < 1:
            minutes = int(diff.total_seconds() // 60)
            return f"{minutes}분 전" if minutes > 0 else "방금"
        elif hours < 24:
            return f"{hours}시간 전"
        else:
            days = hours // 24
            return f"{days}일 전"
    except Exception:
        return "오늘"


def collect_all_sectors() -> dict:
    """11개 섹터의 등락률 + 뉴스(링크 포함)를 수집. 절대 중간에 멈추지 않는다."""
    result = {}
    for name, conf in SECTORS.items():
        ticker = conf["ticker"]
        query = conf["query"]

        pct = fetch_sector_change(ticker)
        raw_news = fetch_news_rss(query)
        news = summarize_with_gemini(name, raw_news)

        result[name] = {"change_percent": pct, "news": news}
        status = "OK" if news else ("PRICE-ONLY" if pct != 0 else "EMPTY")
        print(f"[{status}] {name}({ticker}): {pct}%, 뉴스 {len(news)}건")

    return result


def save_data(data: dict):
    payload = {"updated_at": datetime.utcnow().isoformat(), "sectors": data}
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def safe_pct(info: dict) -> float:
    pct = info.get("change_percent", 0)
    if pct is None or not isinstance(pct, (int, float)):
        return 0.0
    return float(pct)


def build_main_keyboard(data: dict) -> dict:
    sorted_sectors = sorted(data.items(), key=lambda x: abs(safe_pct(x[1])), reverse=True)
    buttons, row = [], []
    for name, info in sorted_sectors:
        pct = safe_pct(info)
        arrow = "▲" if pct >= 0 else "▼"
        row.append({"text": f"{name} {arrow}{abs(pct):.1f}%", "callback_data": f"sector:{name}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return {"inline_keyboard": buttons}


def send_telegram_message(text: str, reply_markup: dict = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    res = requests.post(url, json=payload, timeout=15)
    res.raise_for_status()
    return res.json()


def main():
    print("=== 섹터 데이터 수집 시작 (yfinance + Google News RSS + 링크 보존) ===")
    data = collect_all_sectors()
    save_data(data)

    today = datetime.now().strftime("%Y.%m.%d")
    header = f"📊 <b>BILANX RESEARCH</b>\n오늘의 섹터별 트래픽 ({today})\n\n섹터를 누르면 관련 뉴스를 볼 수 있어요."
    keyboard = build_main_keyboard(data)

    send_telegram_message(header, keyboard)
    print("=== 텔레그램 발행 완료 ===")


if __name__ == "__main__":
    main()
