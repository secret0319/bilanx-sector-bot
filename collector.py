"""
BILANX 섹터 트래픽 봇 (v5 - 최종)

흐름:
1. 휴장일(주말/공휴일) 체크 → 휴장이면 조용히 종료
2. GICS 11개 섹터 ETF의 당일 등락률을 yfinance로 수집
3. 각 섹터마다:
   - Top10 보유종목 중 "당일 등락폭(절댓값)이 큰 3종목"을 골라 종목명으로 뉴스 검색
   - Reuters/Bloomberg/CNBC/WSJ/Investing.com 등 정통매체에서 그 섹터 종합 뉴스 2건 검색
   - 총 5건의 뉴스 후보를 Gemini가 한국어로 요약 (원문 링크는 코드가 직접 보존)
4. 텔레그램 채널에 섹터별 트래픽 카드 발행
"""

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import yfinance as yf

# ── 환경변수 ──
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# ── GICS 11개 섹터 ↔ SPDR Select Sector ETF 티커 ──
SECTORS = {
    "정보기술":          "XLK",
    "금융":              "XLF",
    "헬스케어":          "XLV",
    "임의소비재":        "XLY",
    "필수소비재":        "XLP",
    "산업재":            "XLI",
    "소재":              "XLB",
    "에너지":            "XLE",
    "유틸리티":          "XLU",
    "부동산":            "XLRE",
    "커뮤니케이션서비스": "XLC",
}

# 정통 매체 (종합 뉴스용 — 칼럼/분석성 사이트 배제)
TRUSTED_SOURCES = [
    "reuters.com", "bloomberg.com", "cnbc.com",
    "wsj.com", "investing.com", "ft.com",
]

STORAGE_FILE = "sector_data.json"


# ─────────────────────────────────────────
# 0. 휴장일 체크
# ─────────────────────────────────────────

def is_market_open_yesterday() -> bool:
    """미국 시장이 '어제(미국 동부시간 기준)' 거래된 날이었는지 확인.
    SPY의 최근 거래일과 비교해서 판단 — 별도 공휴일 캘린더 불필요."""
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="5d")
        if hist.empty:
            print("[WARN] SPY 거래 데이터를 가져오지 못함 — 안전하게 발행 진행")
            return True  # 판단 불가 시 발행은 막지 않음 (false negative 방지)

        last_trade_date = hist.index[-1].date()

        # 미국 동부시간 기준 "어제" 날짜 계산 (외부 패키지 없이 UTC-5 근사)
        utc_now = datetime.utcnow()
        et_now = utc_now - timedelta(hours=5)  # EST 근사치 (서머타임 시 약간의 오차 허용)
        yesterday_et = (et_now - timedelta(days=1)).date()

        print(f"최근 거래일: {last_trade_date}, 기준 어제(ET): {yesterday_et}")
        return last_trade_date >= yesterday_et  # 어제 또는 그 이후 데이터가 있으면 정상 거래일로 간주

    except Exception as e:
        print(f"[WARN] 휴장일 체크 실패: {e} — 안전하게 발행 진행")
        return True


# ─────────────────────────────────────────
# 1. 등락률 수집
# ─────────────────────────────────────────

def fetch_sector_change(ticker: str) -> float:
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


# ─────────────────────────────────────────
# 2. Top10 보유종목 중 변동폭 큰 3종목 선정
# ─────────────────────────────────────────

def get_top_movers(ticker: str, top_n: int = 3) -> list:
    """ETF의 top_holdings(최대 10개) 중 당일 등락폭(절댓값) 기준 상위 N종목을 반환.
    반환: [{"symbol": "NVDA", "name": "NVIDIA Corp", "change_percent": 5.8}, ...]"""
    try:
        t = yf.Ticker(ticker)
        holdings_df = t.funds_data.top_holdings
        if holdings_df is None or holdings_df.empty:
            return []

        candidates = []
        for symbol, row in holdings_df.iterrows():
            try:
                h = yf.Ticker(symbol)
                info = h.fast_info
                last = info.get("lastPrice")
                prev = info.get("previousClose")
                if last is None or prev is None or prev == 0:
                    continue
                pct = round((last - prev) / prev * 100, 2)
                candidates.append({
                    "symbol": symbol,
                    "name": row.get("Name", symbol),
                    "change_percent": pct,
                })
            except Exception:
                continue

        candidates.sort(key=lambda x: abs(x["change_percent"]), reverse=True)
        return candidates[:top_n]

    except Exception as e:
        print(f"  [HOLDINGS-FAIL] {ticker}: {e}")
        return []


# ─────────────────────────────────────────
# 3. 뉴스 수집 (종목별 + 정통매체 종합)
# ─────────────────────────────────────────

def get_edition() -> str:
    hour_utc = datetime.utcnow().hour
    if 8 <= hour_utc < 16:
        return "오후"
    return "오전"

def fetch_news_rss(query: str, max_items: int = 5, window: str = "when:1d") -> list:
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}+{window}&hl=en-US&gl=US&ceid=US:en"
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
            pub_date = item.findtext("pubDate", "")
            items.append({"title": title, "link": link, "source": source, "pubDate": pub_date})
        return items
    except Exception as e:
        print(f"  [RSS-FAIL] {query}: {e}")
        return []


def fetch_stock_news(stock_name: str, window: str = "when:1d") -> list:
    """개별 종목명으로 뉴스 검색 (Top1건만 채택용 후보 풀)."""
    return fetch_news_rss(f"{stock_name} stock", max_items=3, window=window)


def fetch_trusted_sector_news(sector_query: str, window: str = "when:1d") -> list:
    """정통매체로 한정해 그 섹터의 종합 뉴스를 검색."""
    site_filter = " OR ".join(f"site:{s}" for s in TRUSTED_SOURCES)
    query = f"({site_filter}) {sector_query}"
    return fetch_news_rss(query, max_items=6, window=window)


# ─────────────────────────────────────────
# 4. Gemini로 한국어 요약 (링크는 코드가 직접 매칭해 보존)
# ─────────────────────────────────────────

def summarize_candidates_with_gemini(sector_name: str, candidates: list, max_pick: int, max_retries: int = 3) -> list:
    """candidates: [{"title", "link", "source", "pubDate"}, ...]
    Gemini는 인덱스로 골라서 한국어 제목만 만들고, 링크는 코드가 원본에서 그대로 가져온다."""
    if not candidates:
        return []

    def parse_date(item):
        try:
            return parsedate_to_datetime(item["pubDate"])
        except Exception:
            return datetime.min

    sorted_candidates = sorted(candidates, key=parse_date, reverse=True)
    numbered_list = "\n".join(
        f"[{i}] {n['title']} ({n['source']})" for i, n in enumerate(sorted_candidates)
    )

    prompt = f"""다음은 "{sector_name}" 섹터 관련 영문 뉴스 제목 후보 목록입니다.

{numbered_list}

작업 지침:
1. 중복되거나 의미 없는 것은 제외하고, 최대 {max_pick}개를 선택하세요.
2. 광고성·칼럼성("~해야 할 이유", "~주식 3가지" 같은 투자 가이드 글)보다는 실제 사건을 다루는 뉴스를 우선하세요.
3. 한국어로 짧게 번역/요약하세요. 원문에 없는 내용은 추가하지 마세요.
4. 선택한 뉴스의 원래 인덱스 번호를 반드시 포함하세요.

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
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800},
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

            result = []
            for p in picked:
                idx = p.get("index")
                if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(sorted_candidates):
                    continue
                original = sorted_candidates[idx]
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
    try:
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


# ─────────────────────────────────────────
# 5. 섹터별 전체 수집 흐름
# ─────────────────────────────────────────

def collect_sector(name: str, ticker: str, edition: str) -> dict:
    pct = fetch_sector_change(ticker)

    # 오전판: 최근 24시간(전날 장마감 이후 전체) / 오후판: 최근 13시간(오전 발행 이후 새 소식 위주)
    window = "when:1d" if edition == "오전" else "when:13h"

    # (1) Top10 중 변동폭 큰 3종목 → 종목별 뉴스 후보 모으기
    movers = get_top_movers(ticker, top_n=3)
    stock_candidates = []
    for m in movers:
        stock_candidates.extend(fetch_stock_news(m["name"], window=window))

    # (2) 정통매체 종합 뉴스 후보
    trusted_candidates = fetch_trusted_sector_news(name, window=window)

    # 종목별 뉴스 3건 + 정통매체 종합 2건 → Gemini에게 각각 따로 요청해 비율 보장
    stock_news = summarize_candidates_with_gemini(name, stock_candidates, max_pick=3)
    trusted_news = summarize_candidates_with_gemini(name, trusted_candidates, max_pick=2)

    combined = stock_news + trusted_news
    return {"change_percent": pct, "news": combined}


def collect_all_sectors(edition: str) -> dict:
    result = {}
    for name, ticker in SECTORS.items():
        try:
            result[name] = collect_sector(name, ticker, edition)
        except Exception as e:
            print(f"[SECTOR-FAIL] {name}: {e}")
            result[name] = {"change_percent": 0, "news": []}

        info = result[name]
        print(f"[{'OK' if info['news'] else 'EMPTY'}] {name}({ticker}): {info['change_percent']}%, 뉴스 {len(info['news'])}건")

    return result


# ─────────────────────────────────────────
# 6. 저장 및 텔레그램 발행
# ─────────────────────────────────────────

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
    print("=== 휴장일 체크 ===")
    if not is_market_open_yesterday():
        print("=== 휴장일로 판단 — 발행 건너뜀 ===")
        return

    edition = get_edition()
    print(f"=== {edition}판 — 섹터 데이터 수집 시작 ===")
    data = collect_all_sectors(edition)
    save_data(data)

    today = datetime.now().strftime("%Y.%m.%d")
    if edition == "오전":
        sub_line = "전날 미국 장마감 기준 결과예요."
    else:
        sub_line = "오전 발행 이후 새로 나온 소식 위주예요."
    header = f"📊 <b>BILANX RESEARCH</b>\n오늘의 섹터별 트래픽 — {edition}판 ({today})\n{sub_line}\n\n섹터를 누르면 관련 뉴스를 볼 수 있어요."
    keyboard = build_main_keyboard(data)

    send_telegram_message(header, keyboard)
    print(f"=== {edition}판 텔레그램 발행 완료 ===")


if __name__ == "__main__":
    main()
