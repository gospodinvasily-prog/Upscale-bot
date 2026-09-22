"""
Upscale Bot v8.0 — воронка из трёх ступеней (Gate.io USDT-фьючерсы):
  ⏳ ЗАРЯД   — монета сжимается, объём/OI растут, цена стоит (ДО движения)
  ⚡ ПРОБОЙ  — быстрый триггер по watchlist из ЗАРЯДа (1м свечи, каждые 20 сек)
  🚀 ИМПУЛЬС — прежний RS Momentum (взрыв / разгон объёма) как подтверждение

Все сигналы пишутся в CSV, через 60 минут бот сам считает исход
(что было раньше: TP1 или стоп, максимальный ход в плюс/минус).
Зависимости: только requests. Переменные окружения: TELEGRAM_TOKEN, LOG_DIR (необязательно).
"""

import os
import csv
import html
import math
import time
import threading
import traceback
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = "426470592"
BOT_VERSION    = "v8.0"

TRADING_START_MSK = 4
TRADING_END_MSK   = 22
MSK = timezone(timedelta(hours=3))

GATE = "https://api.gateio.ws/api/v4/futures/usdt"

# ── Сеть ──
API_RATE_PER_SEC = 12      # общий лимит запросов к Gate (с запасом к публичному лимиту)
SCAN_WORKERS     = 8       # параллельных потоков в основном скане

# ── RS Momentum (ИМПУЛЬС) ──
SCAN_INTERVAL        = 5       # минут (5M таймфрейм)
BTC_DECORR_THRESHOLD = 1.5     # % раскорр для лонга
BTC_DECORR_SHORT     = -1.5    # % раскорр для шорта
DECORR_WATCH         = 1.0     # % сниженный порог для монет из watchlist ЗАРЯДа
RVOL_THRESHOLD           = 1.2   # минимум для «разгона объёма»
RVOL_EXPLOSION_THRESHOLD = 2.5   # порог одиночного взрывного скачка
RVOL_HOT_THRESHOLD   = 8.0     # горячий объём 🔥🔥
CLOSE_POS_THRESHOLD  = 0.6     # закрытие в верхних 40% для лонга
CLOSE_POS_SHORT      = 0.4     # закрытие в нижних 40% для шорта
FUNDING_HOT_LONG     = 0.03    # % — лонги переполнены (единый порог для меток и оценки)
FUNDING_HOT_SHORT    = -0.03   # % — шорты переполнены
STOP_PCT             = -3.0    # % аварийный потолок стопа
STOP_PCT_BASE        = 1.5     # % базовый стоп
ATR_STOP_MULT        = 1.0
ATR_TP2_MULT         = 2.0
TOP_N                = 3
MOMENTUM_COOLDOWN_MIN = 30     # не повторять ту же монету/сторону чаще (если оценка не выросла на 2+)

# ── Свечи / база объёма ──
CANDLES_LIMIT    = 300         # 5м свечей (~25ч)
BASE_FROM        = 84          # база объёма: закрытые свечи [-84 : -12] = 6 часов,
BASE_TO          = 12          # заканчиваются час назад (не включают проверяемые свечи)
SWING_LOOKBACK   = 144         # 12 часов для свинг-уровней

# ── ЗАРЯД (накопление) ──
ACC_WINDOW        = 12         # 12 × 5м = 1 час
ACC_FLAT_ATR      = 2.0        # |изменение цены за час| ≤ 2 × нормальный ATR
ACC_MAX_RANGE_PCT = 5.0        # диапазон часа не шире 5%
ACC_SQUEEZE_PCTL  = 35         # ширина Боллинджера в нижних 35% за 12ч
ACC_TR_RATIO_MAX  = 0.75       # или средний диапазон свечей ≤ 75% от нормы
ACC_RVOL_MIN      = 1.3        # объём за 30 мин ≥ 1.3× нормы
ACC_OI_PREFILTER  = 1.5        # или OI за час ≥ +1.5% (по тикерам)
ACC_MIN_SCORE     = 6          # минимальная сила заряда для алерта
ACC_MAX_SCORE     = 12
ACC_REALERT_DELTA = 2          # повторный алерт, только если сила выросла на 2+

# ── ПРОБОЙ (быстрый триггер) ──
FAST_INTERVAL_SEC = 20         # опрос watchlist
WATCH_TTL_MIN     = 120        # сколько живёт ЗАРЯД в watchlist
WATCH_MAX         = 15
BREAK_BUFFER      = 0.001      # 0.1% за уровень, чтобы не ловить касания
FAST_PACE_STRONG  = 3.0        # темп объёма ≥3× — сигнал сразу
FAST_PACE_MIN     = 1.2        # темп ≥1.2× — сигнал после удержания за уровнем
BREAK_STOP_MIN    = 0.6        # % минимальный стоп пробоя
BREAKOUT_COOLDOWN_MIN = 45

# ── Логирование исходов ──
LOG_DIR          = os.environ.get("LOG_DIR", ".")
SIGNALS_CSV      = os.path.join(LOG_DIR, "signals_v8.csv")
OUTCOMES_CSV     = os.path.join(LOG_DIR, "outcomes_v8.csv")
OUTCOME_HORIZON  = 3600        # оцениваем через 60 минут
CHARGES_CSV          = os.path.join(LOG_DIR, "charges_v8.csv")
CHARGE_OUTCOMES_CSV  = os.path.join(LOG_DIR, "charge_outcomes_v8.csv")
CHARGE_EVAL_WINDOW   = 7200    # заряд оцениваем по ценам за 2 часа после первого алерта
CHARGE_FOLLOW_MIN    = 60      # насколько ушла цена за 60 мин после выхода из диапазона
CHARGE_RETURN_MIN    = 15      # возврат внутрь диапазона за 15 мин = ложный выход

# ─── UPSCALE PAIRS ────────────────────────────────────────────────────────────

UPSCALE_PAIRS = [
    "ETH","BNB","XRP","SOL","AAVE","ADA","AERO","ALGO","APT","ARB",
    "ASTER","ATOM","AVAX","AXS","BCH","BERA","BONK","BRETT","BSV",
    "CAKE","CHZ","CRO","CRV","DASH","DATA","DEEP","DEXE","DOGE","DOT",
    "DYDX","EIGEN","ENA","ENS","ETC","FARTCOIN","FET","FIL","FLOKI",
    "GALA","GRAM","GRASS","GRT","HBAR","HYPE","ICP","IMX","INJ","IOTA",
    "JASMY","JTO","JUP","KAIA","KAITO","KAS","LDO","LINEA","LINK","LTC",
    "MANA","MNT","MORPHO","MOVE","NEAR","ONDO","OP","ORDI","PENDLE",
    "PENGU","PEPE","PNUT","POL","POPCAT","PUMP","PYTH","QNT","RAY",
    "RENDER","RUNE","S","SAND","SEI","SHIB","SKY","STRK","STX","SUI",
    "TAO","TIA","TRUMP","TRX","TURBO","UNI","VET","VIRTUAL","WAL",
    "WIF","WLD","XLM","XMR","XTZ","ZEC","ZRO","0G"
]

# ─── ГЛОБАЛЬНОЕ СОСТОЯНИЕ ────────────────────────────────────────────────────

WATCHLIST: dict = {}          # sym -> данные ЗАРЯДа (уровни, база объёма, свинги...)
LAST_SENT: dict = {}          # (kind, sym, side) -> (ts, score)
OI_TICKER_HIST: list = []     # [(ts, {sym: oi})] — запасной источник OI (≤75 мин)
LAST_BTC = {"chg15": 0.0, "chg1h": 0.0, "ts": 0}
PENDING_OUTCOMES: list = []
DAY_RESULTS: list = []
CHARGE_PENDING: list = []     # эпизоды зарядов, ждущие оценки
CHARGE_ACTIVE: dict = {}      # sym -> текущий эпизод заряда (для пометки «бот прислал пробой»)

# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s), quote=False)

def fnum(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default

def pct(a: float, b: float) -> float:
    """Изменение от a к b в %."""
    return (b - a) / a * 100 if a else 0.0

def fmt_usd(x: float) -> str:
    if x >= 1_000_000: return f"${x/1_000_000:.1f}M"
    if x >= 1_000:     return f"${x/1_000:.0f}K"
    return f"${x:.0f}"

def msk_time_str(ts=None) -> str:
    dt = datetime.fromtimestamp(ts, MSK) if ts else datetime.now(MSK)
    return dt.strftime("%H:%M МСК")

class RateLimiter:
    """Равномерно распределяет запросы между потоками."""
    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next_ts = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next_ts < now:
                self.next_ts = now
            delay = self.next_ts - now
            self.next_ts += self.interval
        if delay > 0:
            time.sleep(delay)

LIMITER = RateLimiter(API_RATE_PER_SEC)

def api_get(path: str, params: dict, timeout: int = 8):
    url = f"{GATE}/{path}"
    for attempt in range(2):
        LIMITER.wait()
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1.5)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            if attempt == 1:
                print(f"[API ERROR] {path} {params.get('contract', '')}: {e}")
    return None

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG ERROR] {e}")

def send_document(path: str, caption: str = ""):
    """Отправляет файл в Telegram. На Render диск стирается при каждом деплое —
    так статистика сохраняется у пользователя в чате."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, "rb") as f:
            r = requests.post(url, data={"chat_id": CHAT_ID, "caption": caption},
                              files={"document": (os.path.basename(path), f)}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG DOC ERROR] {path}: {e}")

def send_blocks(blocks: list, limit: int = 3800):
    """Telegram режет сообщения > 4096 символов — собираем блоки в пачки."""
    buf = ""
    for b in blocks:
        if buf and len(buf) + len(b) + 1 > limit:
            send_telegram(buf)
            buf = ""
        buf = f"{buf}\n{b}" if buf else b
    if buf:
        send_telegram(buf)

# ─── СВЕЧИ ────────────────────────────────────────────────────────────────────

def parse_candles(raw) -> list:
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        try:
            out.append({"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                        "l": float(c["l"]), "c": float(c["c"]), "v": fnum(c.get("v", 0))})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["t"])
    return out

def split_closed(candles: list, interval_sec: int, now_ts: float):
    """Закрытые свечи определяем по времени, а не по позиции [-2].
    Если по неликвидной монете новая свеча ещё не появилась, [-1] уже закрыта —
    старый код в этом случае сдвигал все окна на одну свечу."""
    closed = [c for c in candles if c["t"] + interval_sec <= now_ts]
    forming = candles[-1] if candles and candles[-1]["t"] + interval_sec > now_ts else None
    price = candles[-1]["c"] if candles else 0.0
    return closed, forming, price

def get_candles(symbol: str, interval: str, limit: int) -> list:
    return parse_candles(api_get("candlesticks", {"contract": f"{symbol}_USDT",
                                                  "interval": interval, "limit": limit}))

# ─── ИНДИКАТОРЫ ───────────────────────────────────────────────────────────────

def calc_ema_simple(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_atr(highs, lows, closes, period=14) -> list:
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    if not trs:
        return [0]
    if len(trs) < period:
        return [sum(trs)/len(trs)]
    atrs = [sum(trs[:period])/period]
    for tr in trs[period:]:
        atrs.append((atrs[-1]*(period-1) + tr) / period)
    return atrs

def true_ranges(candles: list) -> list:
    """TR, выровненный по индексам свечей (TR[0] = h-l)."""
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["h"] - c["l"])
        else:
            pc = candles[i-1]["c"]
            trs.append(max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc)))
    return trs

def trimmed_mean(vals: list, trim: float = 0.1) -> float:
    """Среднее без 10% самых больших значений — устойчиво к редким выбросам."""
    v = sorted(x for x in vals if x > 0)
    if not v:
        return 0.0
    cut = int(len(v) * trim)
    v = v[:len(v) - cut] if cut else v
    return sum(v) / len(v)

def bb_width_percentile(closes: list, period: int = 20, lookback: int = SWING_LOOKBACK):
    """Процентиль текущей ширины Боллинджера среди последних lookback значений.
    Малый процентиль = рынок сжат сильнее обычного."""
    n = len(closes)
    start = max(period - 1, n - lookback)
    widths = []
    for i in range(start, n):
        w = closes[i - period + 1:i + 1]
        m = sum(w) / period
        if m <= 0:
            continue
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / period)
        widths.append(2 * sd / m)
    if len(widths) < 30:
        return None
    cur = widths[-1]
    return sum(1 for x in widths if x <= cur) / len(widths) * 100

def find_swings(highs: list, lows: list, left: int = 2, right: int = 1):
    sh, sl = [], []
    for i in range(left, len(highs) - right):
        if all(highs[i] > highs[i-k] for k in range(1, left+1)) and \
           all(highs[i] > highs[i+k] for k in range(1, right+1)):
            sh.append(highs[i])
        if all(lows[i] < lows[i-k] for k in range(1, left+1)) and \
           all(lows[i] < lows[i+k] for k in range(1, right+1)):
            sl.append(lows[i])
    return sh, sl

# ─── ВРЕМЯ ТОРГОВЛИ ──────────────────────────────────────────────────────────

def is_trading_hours() -> bool:
    return TRADING_START_MSK <= datetime.now(MSK).hour < TRADING_END_MSK

DEAD_ZONES = [
    (13, 45, 14, 30),
    (15, 45, 16, 30),
    (17, 45, 18, 30),
]

def is_dead_zone() -> bool:
    now = datetime.now(MSK)
    now_min = now.hour * 60 + now.minute
    for h_start, m_start, h_end, m_end in DEAD_ZONES:
        if h_start * 60 + m_start <= now_min < h_end * 60 + m_end:
            return True
    return False

# ─── GATE: тикеры (funding + OI) ─────────────────────────────────────────────

def get_gate_tickers() -> dict:
    data = api_get("tickers", {}, timeout=10)
    result = {}
    if not isinstance(data, list):
        print("[TICKERS] не получены")
        return result
    for t in data:
        contract = t.get("contract", "")
        if not contract.endswith("_USDT"):
            continue
        sym = contract[:-5]
        oi_raw = t.get("total_size") or t.get("open_interest") or t.get("position_size") or 0
        result[sym] = {
            "funding":    fnum(t.get("funding_rate", 0)) * 100,
            "oi":         fnum(oi_raw),
            "change_24h": fnum(t.get("change_percentage", 0)),
            "last":       fnum(t.get("last", 0)),
        }
    print(f"[TICKERS] Загружено {len(result)} пар")
    return result

def push_oi_snapshot(ticker_data: dict, now_ts: float):
    OI_TICKER_HIST.append((now_ts, {s: d["oi"] for s, d in ticker_data.items()}))
    while OI_TICKER_HIST and now_ts - OI_TICKER_HIST[0][0] > 75 * 60:
        OI_TICKER_HIST.pop(0)

def ticker_oi_change(sym: str, minutes: int):
    """Запасной OI: ищем снимок ровно N минут назад (±2.5 мин).
    Старый код сравнивал «3 скана назад» — после мёртвой зоны это были снимки через разрыв."""
    if len(OI_TICKER_HIST) < 2:
        return None
    now_ts, cur = OI_TICKER_HIST[-1]
    target = now_ts - minutes * 60
    best = min(OI_TICKER_HIST[:-1], key=lambda x: abs(x[0] - target))
    if abs(best[0] - target) > 150:
        return None
    old, new = best[1].get(sym, 0), cur.get(sym, 0)
    if not old or not new:
        return None
    return round(pct(old, new), 2)

# ─── GATE: статистика контракта (OI-история, taker L/S, ликвидации) ─────────

def get_contract_stats(sym: str):
    data = api_get("contract_stats", {"contract": f"{sym}_USDT", "interval": "5m", "limit": 16})
    if not isinstance(data, list) or len(data) < 2:
        return None
    rows = []
    for d in data:
        rows.append({
            "t":     int(fnum(d.get("time", 0))),
            "oi":    fnum(d.get("open_interest", 0)),
            "oiu":   fnum(d.get("open_interest_usd", 0)),
            "taker": fnum(d.get("lsr_taker", 0)),
            "liq_l": fnum(d.get("long_liq_usd", 0)),
            "liq_s": fnum(d.get("short_liq_usd", 0)),
        })
    rows = [r for r in rows if r["t"] > 0]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: r["t"])
    # OI в контрактах не зависит от цены — это чистый приток/отток позиций.
    key = "oi" if all(r["oi"] > 0 for r in rows) else "oiu"
    last = rows[-1]

    def oi_change(sec):
        older = [r for r in rows if r["t"] <= last["t"] - sec]
        if not older or not older[-1][key]:
            return None
        return round(pct(older[-1][key], last[key]), 2)

    def taker_avg(sec):
        vals = [r["taker"] for r in rows if r["t"] > last["t"] - sec and 0.05 < r["taker"] < 20]
        if not vals:
            return None
        return round(math.exp(sum(math.log(v) for v in vals) / len(vals)), 2)  # геометрическое среднее

    hour_rows = [r for r in rows if r["t"] > last["t"] - 3600]
    return {
        "oi_15m": oi_change(900),
        "oi_1h":  oi_change(3600),
        "taker_15m": taker_avg(900),
        "taker_1h":  taker_avg(3600),
        "liq_long_1h":  sum(r["liq_l"] for r in hour_rows),
        "liq_short_1h": sum(r["liq_s"] for r in hour_rows),
    }

def get_trade_delta(sym: str, seconds: int = 120):
    """Доля агрессивных покупок за последние N секунд.
    На фьючерсах Gate size > 0 — покупка тейкером, size < 0 — продажа."""
    data = api_get("trades", {"contract": f"{sym}_USDT", "limit": 1000})
    if not isinstance(data, list):
        return None
    cutoff = time.time() - seconds
    buy = sell = 0.0
    for tr in data:
        # create_time_ms у Gate бывает в секундах с дробной частью (REST) или в мс (WS) —
        # определяем по величине, иначе все сделки отбрасываются и дельта всегда «недоступна»
        t = fnum(tr.get("create_time_ms", 0)) or fnum(tr.get("create_time", 0))
        if t > 1e11:
            t /= 1000
        if t < cutoff:
            continue
        size = fnum(tr.get("size", 0))
        if size > 0: buy += size
        else:        sell += -size
    tot = buy + sell
    return round(buy / tot, 2) if tot > 0 else None

# ─── КОНТЕКСТ РЫНКА: BTC 1D + 4H + 15M + EQH/EQL ────────────────────────────

_market_cache = {"text": "", "updated_at": 0, "h4_bias": "❓"}

def find_eq_levels(levels: list, price: float, above: bool, tolerance: float = 0.15) -> list:
    filtered = [l for l in levels if (l > price if above else l < price)]
    if not filtered:
        return []
    filtered.sort(key=lambda x: abs(x - price))
    groups, used = [], set()
    for i, level in enumerate(filtered):
        if i in used:
            continue
        group = [level]
        for j, other in enumerate(filtered):
            if j != i and j not in used and abs(other - level) / level * 100 <= tolerance:
                group.append(other)
                used.add(j)
        if len(group) >= 2:
            groups.append((sum(group)/len(group), len(group)))
        used.add(i)
    groups.sort(key=lambda x: abs(x[0] - price))
    return groups[:2]

def format_liq_line(eq_highs, eq_lows, price, tf_label):
    parts = []
    eq_highs = [(lvl, cnt) for lvl, cnt in eq_highs if lvl > price * 1.001]
    eq_lows  = [(lvl, cnt) for lvl, cnt in eq_lows  if lvl < price * 0.999]
    if eq_highs:
        lvl, cnt = eq_highs[0]
        parts.append(f"⬆️ EQH: {lvl:,.0f} ({pct(price, lvl):+.1f}%) {'⭐' * min(cnt, 3)}")
    if eq_lows:
        lvl, cnt = eq_lows[0]
        parts.append(f"⬇️ EQL: {lvl:,.0f} ({pct(price, lvl):+.1f}%) {'⭐' * min(cnt, 3)}")
    if eq_highs and eq_lows:
        nearest = "⬆️ вверх" if abs(eq_highs[0][0] - price) < abs(eq_lows[0][0] - price) else "⬇️ вниз"
        parts.append(f"🎯 {nearest}")
    elif eq_highs:
        parts.append("🎯 ⬆️ вверх")
    elif eq_lows:
        parts.append("🎯 ⬇️ вниз")
    return f"   {tf_label}: " + " | ".join(parts) if parts else ""

def get_market_context() -> str:
    now_ts = time.time()
    if now_ts - _market_cache["updated_at"] < 900 and _market_cache["text"]:
        return _market_cache["text"]
    try:
        c1d = get_candles("BTC", "1d", 210)
        c4h = get_candles("BTC", "4h", 100)
        c15 = get_candles("BTC", "15m", 10)
        c15_closed, _, _ = split_closed(c15, 900, now_ts)

        d_bias, price = "❓", 0
        if len(c1d) >= 60:
            closes_1d = [c["c"] for c in c1d]
            price = closes_1d[-1]
            ema50  = calc_ema_simple(closes_1d, 50)
            ema200 = calc_ema_simple(closes_1d, 200) if len(closes_1d) >= 200 else None
            if ema200 and price > ema200 and price > ema50:   d_bias = "🐂 Бычий"
            elif ema200 and price < ema200 and price < ema50: d_bias = "🐻 Медвежий"
            elif price > ema50:                               d_bias = "📈 Выше EMA50"
            else:                                             d_bias = "📉 Ниже EMA50"

        h4_bias, h4_liq = "❓", ""
        if len(c4h) >= 20:
            cl4 = [c["c"] for c in c4h]
            hi4 = [c["h"] for c in c4h]
            lo4 = [c["l"] for c in c4h]
            p4 = cl4[-1]
            price = p4
            ema20 = calc_ema_simple(cl4, 20)
            rh = [max(hi4[i-3:i]) for i in range(3, len(hi4))]
            rl = [min(lo4[i-3:i]) for i in range(3, len(lo4))]
            hh = rh[-1] > rh[-4] if len(rh) >= 4 else None
            hl = rl[-1] > rl[-4] if len(rl) >= 4 else None
            lh = rh[-1] < rh[-4] if len(rh) >= 4 else None
            ll = rl[-1] < rl[-4] if len(rl) >= 4 else None
            if hh and hl:    h4_bias = "📈 Восходящий"
            elif lh and ll:  h4_bias = "📉 Нисходящий"
            elif p4 > ema20: h4_bias = "↗️ Выше EMA20"
            else:            h4_bias = "↘️ Ниже EMA20"
            sh4, sl4 = find_swings(hi4, lo4, left=2, right=2)
            h4_liq = format_liq_line(find_eq_levels(sh4, p4, True), find_eq_levels(sl4, p4, False), p4, "4H")

        m15_line = ""
        if len(c15_closed) >= 3:
            last3 = c15_closed[-3:]
            arrows = []
            for c in last3:
                chg = pct(c["o"], c["c"])
                arrows.append(("⬆️" if chg > 0.05 else "⬇️" if chg < -0.05 else "➡️") + f"{chg:+.2f}%")
            total = pct(last3[0]["o"], last3[-1]["c"])
            te = "⬆️" if total > 0.05 else ("⬇️" if total < -0.05 else "➡️")
            m15_line = f"   15М (45 мин): {' → '.join(arrows)} | Итого: {te}{total:+.2f}%"

        lines = [f"📊 BTC: {price:,.0f} USDT" if price else "📊 BTC: —",
                 f"   1D: {d_bias}", f"   4H: {h4_bias}"]
        if m15_line: lines.append(m15_line)
        if h4_liq:
            lines.append("💧 BTC ликвидность 4H:")
            lines.append(h4_liq)
        # FIX v8: раньше словарь перезаписывался целиком и h4_bias терялся —
        # оценка тренда BTC 4H в анализе всегда была «неопределён».
        _market_cache.update({"text": "\n".join(lines), "updated_at": now_ts, "h4_bias": h4_bias})
        print(f"[MARKET] обновлён, 4H: {h4_bias}")
        return _market_cache["text"]
    except Exception as e:
        print(f"[MARKET ERROR] {e}")
        return _market_cache["text"] or "📊 BTC: данные недоступны"

def h4_score(is_long: bool, plus: list, minus: list) -> int:
    h4 = _market_cache.get("h4_bias", "❓")
    if is_long:
        if "Восходящий" in h4:  plus.append("BTC 4H восходящий — по тренду"); return 1
        if "Нисходящий" in h4:  minus.append("BTC 4H нисходящий — вход против старшего тренда"); return -2
        if "Ниже EMA20" in h4:  minus.append("BTC 4H слабый (ниже EMA20)"); return -1
    else:
        if "Нисходящий" in h4:  plus.append("BTC 4H нисходящий — по тренду"); return 1
        if "Восходящий" in h4:  minus.append("BTC 4H восходящий — вход против старшего тренда"); return -2
        if "Выше EMA20" in h4:  minus.append("BTC 4H сильный (выше EMA20)"); return -1
    minus.append(f"BTC 4H нейтральный ({h4})")
    return 0

# ─── ЛОГИРОВАНИЕ И ИСХОДЫ ────────────────────────────────────────────────────

def _append_csv(path: str, row: dict):
    try:
        header = ",".join(row.keys())
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                first = f.readline().strip()
            if first != header:   # колонки изменились (новая версия) — старый файл в архив
                os.replace(path, f"{path}.old-{int(time.time())}")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[CSV ERROR] {path}: {e}")

def log_signal(kind: str, s: dict, side: str, score: int):
    now_ts = time.time()
    sid = f"{int(now_ts)}-{s['symbol']}-{kind}-{side}"
    row = {
        "id": sid, "time_msk": datetime.fromtimestamp(now_ts, MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind, "symbol": s["symbol"], "side": side, "score": score,
        "price": s["price"], "stop": s["stop"], "tp1": s["tp1_price"], "tp2": s["tp2_price"],
        "rvol": s.get("rvol", ""), "decorr": round(s.get("decorr", 0), 2),
        "oi_15m": s.get("oi_15m", ""), "oi_1h": s.get("oi_1h", ""),
        "taker": s.get("taker_15m", ""), "funding": round(s.get("funding", 0), 4),
        "ch24": round(s.get("change_24h", 0), 2), "from_charge": s.get("from_charge", False),
        "charge_score": s.get("charge_score", ""), "charge_side": s.get("charge_side", ""),
        "pace": s.get("pace", ""), "delta": s.get("delta", ""),
        "h4": _market_cache.get("h4_bias", ""), "btc15": round(LAST_BTC["chg15"], 2),
    }
    _append_csv(SIGNALS_CSV, row)
    PENDING_OUTCOMES.append({"id": sid, "kind": kind, "symbol": s["symbol"], "side": side,
                             "score": score, "entry": s["price"], "stop": s["stop"],
                             "tp1": s["tp1_price"], "ts": now_ts})

def evaluate_outcome(p: dict):
    raw = api_get("candlesticks", {"contract": f"{p['symbol']}_USDT", "interval": "1m",
                                   "from": int(p["ts"]), "to": int(p["ts"] + OUTCOME_HORIZON)})
    # свеча, в которой пришёл сигнал, началась раньше входа — её не учитываем
    candles = [c for c in parse_candles(raw) if c["t"] >= p["ts"] - 1]
    if not candles:
        return None
    is_long = p["side"] == "long"
    entry = p["entry"]
    first, close30 = "none", None
    for c in candles:
        stop_hit = c["l"] <= p["stop"] if is_long else c["h"] >= p["stop"]
        tp_hit   = c["h"] >= p["tp1"]  if is_long else c["l"] <= p["tp1"]
        if first == "none":
            if stop_hit:  first = "stop"       # обе цели в одной свече — считаем стоп (консервативно)
            elif tp_hit:  first = "tp1"
        if close30 is None and c["t"] >= p["ts"] + 1800:
            close30 = c["c"]
    hi = max(c["h"] for c in candles); lo = min(c["l"] for c in candles)
    mfe = pct(entry, hi) if is_long else -pct(entry, lo)
    mae = pct(entry, lo) if is_long else -pct(entry, hi)
    sign = 1 if is_long else -1
    r30 = sign * pct(entry, close30) if close30 else None
    r60 = sign * pct(entry, candles[-1]["c"])
    return {"id": p["id"], "kind": p["kind"], "symbol": p["symbol"], "side": p["side"],
            "score": p["score"], "first_hit": first, "mfe_pct": round(mfe, 2), "mae_pct": round(mae, 2),
            "ret_30m": round(r30, 2) if r30 is not None else "", "ret_60m": round(r60, 2),
            "date": datetime.fromtimestamp(p["ts"], MSK).strftime("%Y-%m-%d")}

def log_charge(c: dict):
    """Первый алерт заряда открывает эпизод: пишем все признаки и ставим в очередь оценки."""
    now_ts = time.time()
    cid = f"{int(now_ts)}-{c['symbol']}-charge"
    row = {
        "id": cid, "time_msk": datetime.fromtimestamp(now_ts, MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": c["symbol"], "bias": c["side"], "score": c["score"], "bias_pts": c["bias"],
        "hi": c["hi"], "lo": c["lo"], "range_pct": round(c["rng_pct"], 2), "price": c["price"],
        "pos_in_range": round(c["pos"], 2),
        "bb_pctl": round(c["sq_pct"], 1) if c["sq_pct"] is not None else "",
        "tr_ratio": round(c["tr_ratio"], 2), "rvol30m": round(c["rvol6"], 2), "vol_rising": c["vol_rising"],
        "oi_1h": c["oi_1h"] if c["oi_1h"] is not None else "",
        "oi_15m": c["oi_15m"] if c["oi_15m"] is not None else "",
        "taker_1h": c["taker_1h"] if c["taker_1h"] is not None else "",
        "funding": round(c["funding"], 4), "chg_1h": round(c["chg_1h"], 2), "ch24": round(c["change_24h"], 2),
        "h4": _market_cache.get("h4_bias", ""), "btc_1h": round(LAST_BTC["chg1h"], 2),
    }
    _append_csv(CHARGES_CSV, row)
    ep = {"id": cid, "symbol": c["symbol"], "bias": c["side"], "score": c["score"], "max_score": c["score"],
          "hi": c["hi"], "lo": c["lo"], "height": c["height"], "ts": now_ts,
          "bot_signal": "", "side_changes": 0}
    CHARGE_PENDING.append(ep)
    CHARGE_ACTIVE[c["symbol"]] = ep

def evaluate_charge(ep: dict):
    """Оценка заряда ТОЛЬКО по ценам — независимо от того, прислал ли бот пробой:
    вышла ли цена из диапазона, куда, когда, насколько далеко и не вернулась ли назад.
    Уровни — те, что были в первом алерте (по ним трейдер ставил алерты)."""
    raw = api_get("candlesticks", {"contract": f"{ep['symbol']}_USDT", "interval": "1m",
                                   "from": int(ep["ts"]), "to": int(ep["ts"] + CHARGE_EVAL_WINDOW)})
    candles = [c for c in parse_candles(raw) if c["t"] >= ep["ts"] - 1]
    if not candles:
        return None
    hi, lo, height = ep["hi"], ep["lo"], ep["height"]
    up_lvl, dn_lvl = hi * (1 + BREAK_BUFFER), lo * (1 - BREAK_BUFFER)
    broke, bi = "none", None
    for i, c in enumerate(candles):
        up, dn = c["h"] >= up_lvl, c["l"] <= dn_lvl
        if up and dn:   # прокол в обе стороны одной минуткой — решаем по закрытию
            broke = "up" if c["c"] > hi else ("down" if c["c"] < lo else "whipsaw")
        elif up:
            broke = "up"
        elif dn:
            broke = "down"
        if broke != "none":
            bi = i
            break
    res = {"id": ep["id"], "kind": "charge", "symbol": ep["symbol"], "bias": ep["bias"],
           "score": ep["score"], "max_score": ep["max_score"], "broke": broke,
           "break_min": "", "matched_bias": "", "follow_pct": "", "follow_heights": "",
           "reached_1x": "", "returned_15m": "", "adverse_pct": "", "bot_signal": ep["bot_signal"],
           "date": datetime.fromtimestamp(ep["ts"], MSK).strftime("%Y-%m-%d")}
    if broke in ("up", "down"):
        bc = candles[bi]
        res["break_min"] = round((bc["t"] - ep["ts"]) / 60, 1)
        if ep["bias"] != "both":
            res["matched_bias"] = (broke == "up") == (ep["bias"] == "long")
        after = [c for c in candles[bi:] if c["t"] <= bc["t"] + CHARGE_FOLLOW_MIN * 60]
        early = [c for c in candles[bi:] if c["t"] <= bc["t"] + CHARGE_RETURN_MIN * 60]
        if broke == "up":
            ext = max(c["h"] for c in after) - hi
            adv = min(c["l"] for c in after) - hi
            res["returned_15m"] = any(c["c"] < hi for c in early)
            res["follow_pct"] = round(pct(hi, hi + ext), 2)
            res["adverse_pct"] = round(pct(hi, hi + adv), 2)
        else:
            ext = lo - min(c["l"] for c in after)
            adv = lo - max(c["h"] for c in after)
            res["returned_15m"] = any(c["c"] > lo for c in early)
            res["follow_pct"] = round(pct(lo, lo - ext) * -1, 2)
            res["adverse_pct"] = round(pct(lo, lo - adv) * -1, 2)
        if height > 0:
            res["follow_heights"] = round(ext / height, 2)
            res["reached_1x"] = ext >= height
    return res

def process_outcomes(max_items: int = 5):
    now_ts = time.time()
    done = 0
    for p in list(PENDING_OUTCOMES):
        if done >= max_items:
            break
        if now_ts < p["ts"] + OUTCOME_HORIZON + 90:
            continue
        PENDING_OUTCOMES.remove(p)
        try:
            res = evaluate_outcome(p)
        except Exception as e:
            print(f"[OUTCOME ERROR] {p['symbol']}: {e}")
            res = None
        done += 1
        if res:
            _append_csv(OUTCOMES_CSV, res)
            DAY_RESULTS.append(res)
    for ep in list(CHARGE_PENDING):
        if done >= max_items:
            break
        if now_ts < ep["ts"] + CHARGE_EVAL_WINDOW + 90:
            continue
        CHARGE_PENDING.remove(ep)
        if CHARGE_ACTIVE.get(ep["symbol"]) is ep:
            CHARGE_ACTIVE.pop(ep["symbol"], None)
        try:
            res = evaluate_charge(ep)
        except Exception as e:
            print(f"[CHARGE OUTCOME ERROR] {ep['symbol']}: {e}")
            res = None
        done += 1
        if res:
            _append_csv(CHARGE_OUTCOMES_CSV, res)
            DAY_RESULTS.append(res)

def send_daily_summary():
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    # берём всё, что оценено с прошлой сводки: заряды оцениваются через 2ч,
    # поэтому поздние вчерашние заряды попадают в сегодняшнюю сводку, а не теряются
    res = list(DAY_RESULTS)
    lines = [f"📒 <b>Итог дня {today}</b> (оценено с прошлой сводки)"]
    if not res:
        lines.append("Оценённых сигналов пока нет.")
    ch = [r for r in res if r["kind"] == "charge"]
    if ch:
        n = len(ch)
        brk = [r for r in ch if r["broke"] in ("up", "down")]
        nb = len(brk)
        line = f"⏳ ЗАРЯД: {n} | выход из диапазона за 2ч: {nb} ({nb/n*100:.0f}%)"
        dirn = [r for r in brk if r["matched_bias"] != ""]
        if dirn:
            ok = sum(1 for r in dirn if r["matched_bias"])
            line += f" | по уклону: {ok}/{len(dirn)} ({ok/len(dirn)*100:.0f}%)"
        if nb:
            r1 = sum(1 for r in brk if r["reached_1x"])
            ret = sum(1 for r in brk if r["returned_15m"])
            line += (f" | дошли до ×1 высоты: {r1/nb*100:.0f}% | вернулись внутрь за 15м: {ret/nb*100:.0f}%"
                     f" | ср. ход после выхода {sum(r['follow_pct'] for r in brk)/nb:.2f}%")
        lines.append(line)
    names = {"breakout": "⚡ ПРОБОЙ", "momentum": "🚀 ИМПУЛЬС"}
    for kind in ("breakout", "momentum"):
        rs = [r for r in res if r["kind"] == kind]
        if not rs:
            continue
        n = len(rs)
        tp = sum(1 for r in rs if r["first_hit"] == "tp1")
        st = sum(1 for r in rs if r["first_hit"] == "stop")
        mfe = sum(r["mfe_pct"] for r in rs) / n
        mae = sum(r["mae_pct"] for r in rs) / n
        lines.append(f"{names[kind]}: {n} сигн. | TP1 первым {tp} ({tp/n*100:.0f}%) | "
                     f"стоп первым {st} ({st/n*100:.0f}%) | ср. макс. ход {mfe:+.2f}% / просадка {mae:+.2f}%")
    lines.append("📎 Файлы с подробностями — ниже (накопительные, с начала работы после последнего деплоя).")
    send_telegram("\n".join(lines))
    DAY_RESULTS.clear()
    for path, cap in ((SIGNALS_CSV, "Все сигналы со всеми признаками"),
                      (OUTCOMES_CSV, "Исходы сигналов: TP1/стоп первым, макс. ход"),
                      (CHARGES_CSV, "Все ЗАРЯДы со всеми признаками"),
                      (CHARGE_OUTCOMES_CSV, "Исходы ЗАРЯДов: куда вышла цена, ложные выходы")):
        send_document(path, f"{today} — {cap}")

# ─── ОБЩИЕ РАСЧЁТЫ СТОПОВ И ЦЕЛЕЙ ────────────────────────────────────────────

def calc_dynamic_stop_pct(atr: float, price: float) -> float:
    """Стоп = max(базовый 1.5%, ATR%), но не дальше аварийного потолка 3%."""
    if price <= 0:
        return STOP_PCT_BASE
    atr_pct = (atr / price) * 100 * ATR_STOP_MULT
    return min(max(STOP_PCT_BASE, atr_pct), abs(STOP_PCT))

def structure_targets(price: float, is_long: bool, swings: list, atr: float):
    """TP1 — ближайший свинг за ценой, TP2 — следующий, иначе ATR (как в v7)."""
    if is_long:
        above = sorted(h for h in swings if h > price * 1.0001)
        if above:
            tp1, l1 = above[0], "следующий хай"
        else:
            tp1, l1 = price + atr, "ATR×1.0"
        nxt = [h for h in above if h > tp1 * 1.001]
        if nxt:
            tp2, l2 = nxt[0], "следующий хай"
        else:
            tp2, l2 = max(price + ATR_TP2_MULT * atr, tp1 * 1.001), f"ATR×{ATR_TP2_MULT}"
    else:
        below = sorted((l for l in swings if l < price * 0.9999), reverse=True)
        if below:
            tp1, l1 = below[0], "следующий лой"
        else:
            tp1, l1 = price - atr, "ATR×1.0"
        nxt = [l for l in below if l < tp1 * 0.999]
        if nxt:
            tp2, l2 = nxt[0], "следующий лой"
        else:
            tp2, l2 = min(price - ATR_TP2_MULT * atr, tp1 * 0.999), f"ATR×{ATR_TP2_MULT}"
    return tp1, l1, tp2, l2

# ═════════════════════════════════════════════════════════════════════════════
#  ⏳ ЗАРЯД — поиск накопления ДО движения
# ═════════════════════════════════════════════════════════════════════════════

def detect_charge(sym, closed, price, baseline, atr_norm, ctx, tick, stats_cache):
    W = ACC_WINDOW
    if len(closed) < 60 or atr_norm <= 0 or baseline <= 0:
        return None
    win = closed[-W:]
    hi = max(c["h"] for c in win)
    lo = min(c["l"] for c in win)
    if lo <= 0:
        return None
    height = hi - lo
    rng_pct = height / lo * 100
    move = win[-1]["c"] - win[0]["o"]
    chg_1h = pct(win[0]["o"], win[-1]["c"])

    # 1) Цена стоит
    if abs(move) > ACC_FLAT_ATR * atr_norm or rng_pct > ACC_MAX_RANGE_PCT:
        return None
    # 2) Сжатие
    closes = [c["c"] for c in closed]
    sq_pct = bb_width_percentile(closes)
    trs = true_ranges(closed)
    tr_ratio = (sum(trs[-W:]) / W) / atr_norm
    squeezed = (sq_pct is not None and sq_pct <= ACC_SQUEEZE_PCTL) or tr_ratio <= ACC_TR_RATIO_MAX
    if not squeezed:
        return None
    # 3) Объём или OI растут
    vol6      = sum(c["v"] for c in win[-6:]) / 6
    vol_prev6 = sum(c["v"] for c in win[:6]) / 6
    rvol6 = vol6 / baseline
    vol_rising = vol_prev6 > 0 and vol6 > vol_prev6 * 1.15
    oi_1h_tk = ticker_oi_change(sym, 60)
    if not (rvol6 >= ACC_RVOL_MIN or (oi_1h_tk is not None and oi_1h_tk >= ACC_OI_PREFILTER)):
        return None

    stats = stats_cache(sym)
    oi_1h  = stats["oi_1h"]  if stats and stats["oi_1h"]  is not None else oi_1h_tk
    oi_15m = stats["oi_15m"] if stats and stats["oi_15m"] is not None else ticker_oi_change(sym, 15)
    taker_1h = stats["taker_1h"] if stats else None
    funding = tick.get("funding", 0)

    score, plus, minus = 0, [], []

    if sq_pct is not None and sq_pct <= 15:
        score += 2; plus.append(f"сильное сжатие (ширина BB в нижних {sq_pct:.0f}% за 12ч)")
    elif sq_pct is not None and sq_pct <= ACC_SQUEEZE_PCTL:
        score += 1; plus.append(f"сжатие (ширина BB в нижних {sq_pct:.0f}%)")
    if tr_ratio <= 0.6:
        score += 1; plus.append(f"свечи сузились до {tr_ratio*100:.0f}% от нормы")

    if rvol6 >= 2:
        score += 2; plus.append(f"объём 30 мин {rvol6:.1f}× нормы при стоящей цене — поглощение")
    elif rvol6 >= ACC_RVOL_MIN:
        score += 1; plus.append(f"объём 30 мин {rvol6:.1f}× нормы")
    else:
        minus.append(f"объём обычный ({rvol6:.1f}×) — заряд держится на OI")
    if vol_rising:
        score += 1; plus.append("объём нарастает внутри диапазона")

    if oi_1h is None:
        minus.append("нет данных OI — заряд не подтверждён позициями")
    elif oi_1h >= 4:
        score += 3; plus.append(f"OI +{oi_1h}% за час без движения цены — крупный набор позиций")
    elif oi_1h >= 2:
        score += 2; plus.append(f"OI +{oi_1h}% за час — позиции набираются")
    elif oi_1h >= 1:
        score += 1; plus.append(f"OI слегка растёт (+{oi_1h}%)")
    elif oi_1h <= -2:
        score -= 2; minus.append(f"OI падает ({oi_1h}%) — это выход из позиций, а не накопление")
    else:
        minus.append(f"OI стоит ({oi_1h:+}%)")

    # ── Направление: кто кого поглощает ──
    bl, bs, dir_notes = 0, 0, []
    pos = min(max((price - lo) / height, 0.0), 1.0) if height > 0 else 0.5
    if pos >= 0.66:   bl += 1; dir_notes.append("цена прижата к верхней границе")
    elif pos <= 0.33: bs += 1; dir_notes.append("цена прижата к нижней границе")
    lows_a  = min(c["l"] for c in win[:4]);  lows_c  = min(c["l"] for c in win[-4:])
    highs_a = max(c["h"] for c in win[:4]);  highs_c = max(c["h"] for c in win[-4:])
    if lows_c > lows_a and highs_c >= highs_a * 0.998:
        bl += 1; dir_notes.append("повышающиеся минимумы (сжатие к сопротивлению)")
    if highs_c < highs_a and lows_c <= lows_a * 1.002:
        bs += 1; dir_notes.append("понижающиеся максимумы (сжатие к поддержке)")
    decorr_1h = chg_1h - ctx["btc_chg_1h"]
    if ctx["btc_chg_1h"] <= -0.3 and decorr_1h >= 0.3:
        bl += 1; dir_notes.append(f"держится при падении BTC ({ctx['btc_chg_1h']:+.2f}%) — скрытая сила")
    if ctx["btc_chg_1h"] >= 0.3 and decorr_1h <= -0.3:
        bs += 1; dir_notes.append(f"не растёт при росте BTC ({ctx['btc_chg_1h']:+.2f}%) — скрытая слабость")
    oi_up = oi_1h is not None and oi_1h >= 1
    if funding < -0.005 and oi_up:
        bl += 1; dir_notes.append(f"шорты набиваются при фандинге {funding:+.3f}% — топливо для сквиза вверх")
    if funding > FUNDING_HOT_LONG and oi_up:
        bs += 1; dir_notes.append(f"лонги набиваются при фандинге {funding:+.3f}% — топливо для пролива")
    if taker_1h is not None:
        if taker_1h <= 0.9 and chg_1h >= 0:
            bl += 2; dir_notes.append(f"агрессивные продажи (taker L/S {taker_1h}) не давят цену — внизу лимитный покупатель")
        elif taker_1h >= 1.1 and chg_1h <= 0:
            bs += 2; dir_notes.append(f"агрессивные покупки (taker L/S {taker_1h}) не поднимают цену — сверху лимитный продавец")
        elif taker_1h >= 1.1:
            bl += 1; dir_notes.append(f"покупатели агрессивнее (taker L/S {taker_1h})")
        elif taker_1h <= 0.9:
            bs += 1; dir_notes.append(f"продавцы агрессивнее (taker L/S {taker_1h})")

    bias = bl - bs
    side = "long" if bias >= 2 else ("short" if bias <= -2 else "both")
    if abs(bias) >= 3:
        score += 2; plus.append("направление читается уверенно")
    elif abs(bias) >= 2:
        score += 1
    else:
        minus.append("направление неясно — ждём пробой в любую сторону")

    if side != "both":
        score += h4_score(side == "long", plus, minus)

    if score < ACC_MIN_SCORE:
        return None

    sw = closed[-SWING_LOOKBACK:]
    sh, sl = find_swings([c["h"] for c in sw], [c["l"] for c in sw])
    return {
        "symbol": sym, "side": side, "score": score, "bias": bias,
        "hi": hi, "lo": lo, "height": height, "rng_pct": rng_pct, "price": price, "pos": pos,
        "sq_pct": sq_pct, "tr_ratio": tr_ratio, "rvol6": rvol6, "vol_rising": vol_rising,
        "oi_1h": oi_1h, "oi_15m": oi_15m, "taker_1h": taker_1h, "funding": funding,
        "change_24h": tick.get("change_24h", 0), "chg_1h": chg_1h,
        "liq_long_1h": stats["liq_long_1h"] if stats else 0,
        "liq_short_1h": stats["liq_short_1h"] if stats else 0,
        "baseline5": baseline, "atr": atr_norm, "swing_highs": sh, "swing_lows": sl,
        "plus": plus, "minus": minus, "dir_notes": dir_notes,
    }

def format_charge(c: dict) -> str:
    side_txt = {"long": "🟢 ЛОНГ-уклон", "short": "🔴 ШОРТ-уклон", "both": "⚪ Неясно — ждём любой пробой"}[c["side"]]
    filled = max(1, min(5, round(c["score"] / ACC_MAX_SCORE * 5)))
    bat = "🔋" * filled + "▫️" * (5 - filled)
    oi_line = (f"OI 1ч: <b>{c['oi_1h']:+.2f}%</b>" if c["oi_1h"] is not None else "OI 1ч: нет данных") + \
              (f" | 15м: {c['oi_15m']:+.2f}%" if c["oi_15m"] is not None else "")
    taker = f"{c['taker_1h']}" if c["taker_1h"] is not None else "—"
    # Ориентир стопа для пробоя: за уровнем на 1 нормальный ATR, но не дальше противоположной границы
    up_stop = max(c["lo"], c["hi"] - c["atr"])
    dn_stop = min(c["hi"], c["lo"] + c["atr"])
    sq = f"BB в нижних {c['sq_pct']:.0f}%" if c["sq_pct"] is not None else "BB —"
    lines = [
        f"⏳ <b>ЗАРЯД — {c['symbol']}/USDT</b> | {msk_time_str()}",
        f"Направление: <b>{side_txt}</b>",
        f"Сила заряда: {bat} ({c['score']}/{ACC_MAX_SCORE})",
        f"Диапазон 1ч: {c['lo']:.6g} – {c['hi']:.6g} (ширина {c['rng_pct']:.2f}%)",
        f"Цена: {c['price']:.6g} — {c['pos']*100:.0f}% диапазона | за час {c['chg_1h']:+.2f}%",
        f"Сжатие: {sq} | свечи {c['tr_ratio']*100:.0f}% от нормы",
        f"Объём 30м: {c['rvol6']:.1f}× нормы{' 📈 растёт' if c['vol_rising'] else ''}",
        oi_line,
        f"Taker L/S 1ч: {taker} | Фандинг: {c['funding']:+.3f}% | 24ч: {c['change_24h']:+.1f}%",
    ]
    if c["liq_short_1h"] or c["liq_long_1h"]:
        lines.append(f"Ликвидации 1ч: шортов {fmt_usd(c['liq_short_1h'])} / лонгов {fmt_usd(c['liq_long_1h'])}")
    if c["side"] in ("long", "both"):
        lines.append(f"⚡ Пробой вверх: выше <b>{c['hi']:.6g}</b> → стоп ~{up_stop:.6g} "
                     f"({pct(c['hi'], up_stop):.2f}% от уровня)")
    if c["side"] in ("short", "both"):
        lines.append(f"⚡ Пробой вниз: ниже <b>{c['lo']:.6g}</b> → стоп ~{dn_stop:.6g} "
                     f"({pct(c['lo'], dn_stop):+.2f}% от уровня)")
    lines.append("🔎 <b>Анализ:</b>")
    for n in c["plus"]:      lines.append(f"  ✅ {esc(n)}")
    for n in c["dir_notes"]: lines.append(f"  🧭 {esc(n)}")
    for n in c["minus"]:     lines.append(f"  ⚠️ {esc(n)}")
    lines.append("👀 <b>На что смотреть:</b>")
    lines.append("  • Внутри диапазона не входить — ждём ⚡ПРОБОЙ, бот пришлёт его сам.")
    if c["oi_1h"] is not None and c["oi_1h"] >= 2:
        lines.append("  • Если OI продолжит расти, а цена стоять — пружина сжимается сильнее.")
    lines.append("  • Резкий прокол границы с возвратом внутрь = сбор стопов; настоящий выход часто в обратную сторону.")
    if c["side"] == "both":
        lines.append("  • Направление покажет первая сторона, куда уйдут объём и дельта.")
    lines.append(f"  • Заряд действует до {msk_time_str(time.time() + WATCH_TTL_MIN * 60)}.")
    return "\n".join(lines)

def register_charges(charges: list):
    """Добавляет/обновляет watchlist. Возвращает список зарядов для алерта."""
    now_ts = time.time()
    to_alert = []
    for c in sorted(charges, key=lambda x: x["score"], reverse=True):
        sym = c["symbol"]
        recent_break = [v for k, v in LAST_SENT.items() if k[0] == "breakout" and k[1] == sym
                        and now_ts - v[0] < BREAKOUT_COOLDOWN_MIN * 60]
        if recent_break:
            continue  # недавно уже был пробой по этой монете
        old = WATCHLIST.get(sym)
        if not old and len(WATCHLIST) >= WATCH_MAX:
            weakest = min(WATCHLIST.values(), key=lambda x: x["score"])
            if weakest["score"] >= c["score"]:
                continue
            WATCHLIST.pop(weakest["symbol"], None)
        entry = dict(c)
        entry["created"] = old["created"] if old else now_ts
        entry["expires"] = now_ts + WATCH_TTL_MIN * 60
        entry["alert_score"] = old["alert_score"] if old else -99
        entry["pending"] = old.get("pending") if old else None
        WATCHLIST[sym] = entry
        side_changed = old is not None and old["side"] != c["side"]
        ep = CHARGE_ACTIVE.get(sym)
        if old is None or ep is None:
            log_charge(c)                       # новый эпизод заряда → журнал
        else:
            ep["max_score"] = max(ep["max_score"], c["score"])
            if side_changed:
                ep["side_changes"] += 1
        if c["score"] >= entry["alert_score"] + ACC_REALERT_DELTA or side_changed:
            entry["alert_score"] = c["score"]
            to_alert.append(entry)
    return to_alert

# ═════════════════════════════════════════════════════════════════════════════
#  ⚡ ПРОБОЙ — быстрый триггер по watchlist
# ═════════════════════════════════════════════════════════════════════════════

def build_breakout(w: dict, side: str, price: float, pace: float, delta):
    is_long = side == "long"
    level  = w["hi"] if is_long else w["lo"]
    height = w["height"]
    atr    = w["atr"]
    # Стоп: за уровнем пробоя на 1 ATR (но не дальше противоположной границы),
    # расстояние ограничено 0.6%…3% от цены входа.
    if is_long:
        stop_raw = max(w["lo"], level - atr)
        dist = pct(stop_raw, price)
    else:
        stop_raw = min(w["hi"], level + atr)
        dist = pct(price, stop_raw)
    dist = min(max(dist, BREAK_STOP_MIN), abs(STOP_PCT))
    stop = price * (1 - dist / 100) if is_long else price * (1 + dist / 100)

    # Цели: свинги + measured move (высота диапазона от уровня пробоя)
    if is_long:
        cands = [(h, "свинг-хай") for h in w["swing_highs"]]
        cands += [(level + height, "высота диапазона ×1"), (level + 2 * height, "высота диапазона ×2")]
        cands = sorted([x for x in cands if x[0] > price * 1.003], key=lambda x: x[0])
    else:
        cands = [(l, "свинг-лой") for l in w["swing_lows"]]
        cands += [(level - height, "высота диапазона ×1"), (level - 2 * height, "высота диапазона ×2")]
        cands = sorted([x for x in cands if 0 < x[0] < price * 0.997], key=lambda x: -x[0])
    if cands:
        tp1, l1 = cands[0]
    else:
        tp1, l1 = (price + atr, "ATR×1.0") if is_long else (price - atr, "ATR×1.0")
    nxt = [x for x in cands if (x[0] > tp1 * 1.003 if is_long else x[0] < tp1 * 0.997)]
    if nxt:
        tp2, l2 = nxt[0]
    else:
        tp2, l2 = (tp1 + atr, "TP1+ATR") if is_long else (tp1 - atr, "TP1+ATR")

    ext  = pct(level, price) if is_long else -pct(level, price)   # насколько уже ушли за уровень
    room = pct(price, tp1) if is_long else -pct(price, tp1)
    return {
        "symbol": w["symbol"], "side": side, "price": price, "level": level, "ext": ext,
        "stop": stop, "stop_pct": dist, "tp1_price": tp1, "tp1_label": l1, "tp2_price": tp2, "tp2_label": l2,
        "tp1_pct": pct(price, tp1), "tp2_pct": pct(price, tp2), "room_pct": room,
        "pace": round(pace, 2), "delta": delta, "charge_score": w["score"], "charge_side": w["side"],
        "charge_created": w["created"], "funding": w["funding"], "change_24h": w["change_24h"],
        "oi_1h": w["oi_1h"], "oi_15m": w["oi_15m"], "taker_15m": w.get("taker_1h"),
        "decorr": 0.0, "rvol": round(pace, 2), "from_charge": True,
    }

def analyze_breakout(b: dict):
    is_long = b["side"] == "long"
    score, plus, minus = 0, [], []
    p = b["pace"]
    if p >= 5:     score += 3; plus.append(f"взрывной темп объёма ({p:.1f}× нормы)")
    elif p >= 3:   score += 2; plus.append(f"сильный темп объёма ({p:.1f}×)")
    elif p >= 1.5: score += 1; plus.append(f"объём выше нормы ({p:.1f}×)")
    else:          score -= 1; minus.append(f"пробой на слабом объёме ({p:.1f}×) — риск ложного")

    d = b["delta"]
    if d is None:
        minus.append("дельта недоступна — проверь CVD вручную")
    else:
        dd = d if is_long else 1 - d
        if dd >= 0.6:    score += 2; plus.append(f"дельта 2 мин {dd*100:.0f}% в нашу сторону — агрессор с нами")
        elif dd >= 0.55: score += 1; plus.append(f"дельта слегка в нашу сторону ({dd*100:.0f}%)")
        elif dd <= 0.45: score -= 2; minus.append(f"дельта против ({dd*100:.0f}%) — пробой без агрессора, похоже на вынос")
        else:            minus.append(f"дельта нейтральная ({dd*100:.0f}%)")

    cs = b["charge_score"]
    if cs >= 9:   score += 2; plus.append(f"сильный заряд перед пробоем ({cs}/{ACC_MAX_SCORE})")
    elif cs >= 7: score += 1; plus.append(f"заряд {cs}/{ACC_MAX_SCORE}")

    if b["charge_side"] == b["side"]:
        score += 1; plus.append("пробой в сторону уклона ЗАРЯДа")
    elif b["charge_side"] != "both":
        score -= 2; minus.append("пробой ПРОТИВ уклона ЗАРЯДа — осторожно")

    if b["ext"] <= 0.4:
        score += 1; plus.append(f"вход у самого уровня (+{b['ext']:.2f}%)")
    elif b["ext"] > 1.0:
        score -= 1; minus.append(f"цена уже ушла на {b['ext']:.2f}% за уровень — догоняешь, лучше ретест")

    btc = LAST_BTC["chg15"]
    if (btc > 0.1 and is_long) or (btc < -0.1 and not is_long):
        score += 1; plus.append(f"BTC 15M поддерживает ({btc:+.2f}%)")
    elif (btc < -0.3 and is_long) or (btc > 0.3 and not is_long):
        score -= 1; minus.append(f"BTC 15M против ({btc:+.2f}%)")

    score += h4_score(is_long, plus, minus)

    f = b["funding"]
    if is_long and f > FUNDING_HOT_LONG:
        score -= 1; minus.append(f"фандинг перегрет ({f:+.3f}%)")
    if not is_long and f < FUNDING_HOT_SHORT:
        score -= 1; minus.append(f"фандинг против шорта ({f:+.3f}%)")
    if b["room_pct"] < 0.5:
        score -= 1; minus.append(f"до TP1 всего {b['room_pct']:.2f}% — мало места")

    if score >= 7:   v = "🟢 Сильный. Входи."
    elif score >= 4: v = "🟡 Нормальный. Подтверди 1м закрытием за уровнем."
    elif score >= 1: v = "🟠 Слабый. Половина позиции или жди ретест."
    else:            v = "🔴 Пропустить / только ретест."
    return score, v, plus, minus

def format_breakout(b: dict, score: int, verdict: str, plus: list, minus: list) -> str:
    is_long = b["side"] == "long"
    head = "⚡ <b>ПРОБОЙ — ЛОНГ</b>" if is_long else "⚡ <b>ПРОБОЙ — ШОРТ</b>"
    delta = f"{(b['delta'] if is_long else 1 - b['delta'])*100:.0f}% в нашу сторону" if b["delta"] is not None else "—"
    oi = f"{b['oi_1h']:+.2f}%" if b["oi_1h"] is not None else "—"
    sign = "-" if is_long else "+"
    lines = [
        f"{head} {b['symbol']}/USDT | {msk_time_str()}",
        f"Уровень {'вверх' if is_long else 'вниз'}: {b['level']:.6g} пробит, цена {b['price']:.6g} "
        f"({b['ext']:+.2f}% за уровнем)",
        f"Темп объёма: <b>{b['pace']:.1f}×</b> | Дельта 2м: {delta}",
        f"Из ЗАРЯДа от {msk_time_str(b['charge_created'])} (сила {b['charge_score']}) | OI 1ч: {oi}",
        f"Фандинг: {b['funding']:+.3f}% | 24ч: {b['change_24h']:+.1f}%",
        f"   Вход: <b>{b['price']:.6g}</b>",
        f"   Стоп: {b['stop']:.6g} ({sign}{b['stop_pct']:.2f}%) — за уровнем",
        f"   TP1: {b['tp1_price']:.6g} ({b['tp1_pct']:+.1f}%) — {b['tp1_label']} — 50%",
        f"   TP2: {b['tp2_price']:.6g} ({b['tp2_pct']:+.1f}%) — {b['tp2_label']} — 50%",
        f"📊 Оценка: <b>{score}</b> — {verdict}",
        "🔎 <b>Анализ:</b>",
    ]
    lines += [f"  ✅ {esc(n)}" for n in plus]
    lines += [f"  ⚠️ {esc(n)}" for n in minus]
    lines.append("👀 <b>На что смотреть:</b>")
    lines.append(f"  • Возврат за {b['level']:.6g} и закрытие 1м внутри диапазона = ложный пробой, выходи.")
    lines.append(f"  • Ретест {b['level']:.6g} с удержанием — второй шанс входа с коротким стопом.")
    if b["ext"] > 1.0:
        lines.append("  • Цена уже далеко от уровня: не гонись, лимитка ближе к уровню.")
    lines.append("  • После TP1 стоп в безубыток.")
    return "\n".join(lines)

def fast_check():
    """Опрос watchlist по 1м свечам: цена за уровнем + темп объёма."""
    now_ts = time.time()
    for sym in [s for s, w in WATCHLIST.items() if w["expires"] < now_ts]:
        print(f"[WATCH] {sym}: заряд истёк")
        WATCHLIST.pop(sym, None)

    for sym, w in list(WATCHLIST.items()):
        try:
            candles = get_candles(sym, "1m", 3)
            if not candles:
                continue
            now_ts = time.time()
            closed, forming, price = split_closed(candles, 60, now_ts)
            base1m = w["baseline5"] / 5
            if base1m <= 0 or price <= 0:
                continue
            proj = 0.0
            if forming:
                elapsed = max(now_ts - forming["t"], 10)   # первые секунды свечи не экстраполируем
                proj = forming["v"] / elapsed * 60
            last_closed_v = closed[-1]["v"] if closed else 0
            pace = max(proj, last_closed_v) / base1m

            up   = price > w["hi"] * (1 + BREAK_BUFFER)
            down = price < w["lo"] * (1 - BREAK_BUFFER)
            if not up and not down:
                w["pending"] = None
                continue
            side = "long" if up else "short"

            pend = w.get("pending")
            held = pend is not None and pend["side"] == side and now_ts - pend["ts"] >= FAST_INTERVAL_SEC * 0.8
            fire = pace >= FAST_PACE_STRONG or (held and pace >= FAST_PACE_MIN)
            if not fire:
                if not pend or pend["side"] != side:
                    w["pending"] = {"side": side, "ts": now_ts}
                continue

            delta = get_trade_delta(sym, 120)
            b = build_breakout(w, side, price, pace, delta)
            score, verdict, plus, minus = analyze_breakout(b)
            if not held:
                plus.append("отправлен сразу — темп объёма ≥3×, без ожидания удержания")
            send_telegram(format_breakout(b, score, verdict, plus, minus))
            log_signal("breakout", b, side, score)
            LAST_SENT[("breakout", sym, side)] = (now_ts, score)
            if sym in CHARGE_ACTIVE:
                CHARGE_ACTIVE[sym]["bot_signal"] = f"{side}:{score}"
                CHARGE_ACTIVE.pop(sym, None)     # эпизод закрыт пробоем, но оценка по ценам остаётся в очереди
            WATCHLIST.pop(sym, None)
            print(f"[BREAKOUT] {sym} {side} pace={pace:.1f} score={score}")
        except Exception as e:
            print(f"[FAST ERROR] {sym}: {e}")

# ═════════════════════════════════════════════════════════════════════════════
#  🚀 ИМПУЛЬС — RS Momentum (логика v7 + исправления)
# ═════════════════════════════════════════════════════════════════════════════

def analyze_signal(s: dict, is_long: bool, btc_chg: float) -> tuple:
    score, plus, minus = 0, [], []

    rvol = s.get("rvol", 0)
    if s.get("signal_mode") == "accumulation":
        if rvol >= 3:     score += 2; plus.append(f"разгон объёма сильный ({rvol}x)")
        elif rvol >= 1.5: score += 1; plus.append(f"разгон объёма умеренный ({rvol}x)")
        else:             minus.append(f"разгон объёма слабый ({rvol}x, у порога 1.2x)")
    else:
        if rvol >= 10:   score += 3; plus.append(f"взрывной объём ({rvol}x)")
        elif rvol >= 5:  score += 2; plus.append(f"сильный объём ({rvol}x)")
        elif rvol >= 3:  score += 1; plus.append(f"объём выше среднего ({rvol}x)")
        else:            minus.append(f"взрыв слабый ({rvol}x, у порога 2.5x)")

    oi = s.get("oi_15m")
    if oi is None:
        minus.append("нет данных OI")
    elif oi > 1.5:
        score += 2; plus.append(f"OI растёт (+{oi}% за 15м) — новые деньги")
    elif oi > 0.5:
        score += 1; plus.append(f"OI слегка растёт (+{oi}%)")
    elif oi < -1:
        score -= 2; minus.append(f"OI падает ({oi}%) — движение на закрытии позиций, не новый вход")
    else:
        minus.append(f"OI стоит ({oi:+}%) — нет подтверждения новыми деньгами")

    tk = s.get("taker_15m")
    if tk:
        side_tk = tk if is_long else 1 / tk
        if side_tk >= 1.2:    score += 1; plus.append(f"тейкеры давят в нашу сторону (L/S {tk})")
        elif side_tk <= 0.85: score -= 1; minus.append(f"тейкеры против (L/S {tk}) — движение без агрессора")

    cp = s.get("close_position", 0.5)
    eff_cp = cp if is_long else (1 - cp)
    if eff_cp >= 0.90:   score += 2; plus.append("свеча закрылась у края — контроль полный")
    elif eff_cp >= 0.75: score += 1; plus.append("свеча закрылась уверенно")
    elif eff_cp < 0.60:  score -= 1; minus.append("слабое закрытие свечи — нет полного контроля стороны")
    else:                minus.append("закрытие свечи среднее")

    decorr = abs(s.get("decorr", 0))
    if decorr >= 3:   score += 2; plus.append(f"сильный раскорр ({decorr:.1f}%)")
    elif decorr >= 2: score += 1; plus.append(f"умеренный раскорр ({decorr:.1f}%)")
    else:             minus.append(f"раскорр небольшой ({decorr:.1f}%)")

    if s.get("from_charge"):
        score += 2; plus.append("монета из ЗАРЯДа — импульс вышел из накопления")

    ch24 = s.get("change_24h", 0)
    if is_long:
        if ch24 < 5:     score += 1; plus.append(f"монета не разогрета ({ch24:+.1f}% за 24ч)")
        elif ch24 > 25:  score -= 2; minus.append(f"монета перегрета ({ch24:+.1f}% за 24ч) — риск разворота")
        elif ch24 > 15:  score -= 1; minus.append(f"монета разогрета ({ch24:+.1f}% за 24ч)")
        else:            minus.append(f"рост 24ч нейтральный ({ch24:+.1f}%)")
    else:
        if ch24 > 15:    score += 1; plus.append(f"перегрета ({ch24:+.1f}%) — шорт логичен")
        elif ch24 < -10: score += 1; plus.append(f"уже сильно падает ({ch24:+.1f}%) — тренд на нашей стороне")
        else:            minus.append(f"рост 24ч нейтральный ({ch24:+.1f}%)")

    funding = s.get("funding", 0)
    if is_long:
        if funding < -0.005:             score += 1; plus.append(f"фандинг отрицательный ({funding:+.3f}%) — шорты платят")
        elif funding > FUNDING_HOT_LONG: score -= 1; minus.append(f"фандинг перегрет ({funding:+.3f}%) — лонги переполнены")
        else:                            minus.append(f"фандинг нейтральный ({funding:+.3f}%)")
    else:
        if funding > 0.02:                score += 1; plus.append(f"лонги перегружены ({funding:+.3f}%) — топливо для шорта")
        elif funding < FUNDING_HOT_SHORT: score -= 1; minus.append(f"фандинг против шорта ({funding:+.3f}%)")
        else:                             minus.append(f"фандинг нейтральный ({funding:+.3f}%)")

    if "хай" in s.get("tp1_label", "") or "лой" in s.get("tp1_label", ""):
        score += 1; plus.append("есть структурный уровень для TP1")
    else:
        minus.append("TP1 по ATR — впереди нет свинг-уровня")

    if s.get("room_pct", 1) < 0.3:
        score -= 2; minus.append(f"цена почти у TP1 ({s.get('room_pct', 0):.2f}%) — сигнал запоздал")

    if is_long:
        if btc_chg > 0.1:    score += 1; plus.append(f"BTC 15M поддерживает ({btc_chg:+.2f}%)")
        elif btc_chg < -0.3: score -= 1; minus.append(f"BTC 15M против ({btc_chg:+.2f}%)")
        else:                minus.append(f"BTC 15M нейтральный ({btc_chg:+.2f}%)")
    else:
        if btc_chg < -0.1:   score += 1; plus.append(f"BTC 15M поддерживает ({btc_chg:+.2f}%)")
        elif btc_chg > 0.3:  score -= 1; minus.append(f"BTC 15M против ({btc_chg:+.2f}%)")
        else:                minus.append(f"BTC 15M нейтральный ({btc_chg:+.2f}%)")

    score += h4_score(is_long, plus, minus)

    if score >= 8:
        v = "🟢 Сильный" + (f" — {', '.join(plus[:2])}" if plus else "") + ". Входи."
    elif score >= 5:
        v = "🟡 Нормальный" + (f" — {plus[0]}" if plus else "") + (f"; но {minus[0]}" if minus else "") + ". Проверь CVD."
    elif score >= 2:
        v = "🟠 Слабый" + (f" — {', '.join(minus[:2])}" if minus else "") + ". Уменьши позицию."
    else:
        v = "🔴 Пропустить" + (f" — {', '.join(minus[:3])}" if minus else "")
    return score, v, plus, minus

def momentum_tips(s: dict, is_long: bool) -> list:
    tips = []
    oi = s.get("oi_15m")
    h4 = _market_cache.get("h4_bias", "")
    if oi is not None and oi < -1:
        tips.append("Движение на закрытии чужих позиций (OI падает) — быстро выдыхается, TP1 фиксируй без жадности.")
    if oi is not None and oi > 1.5 and ((is_long and s["funding"] > FUNDING_HOT_LONG) or
                                         (not is_long and s["funding"] < FUNDING_HOT_SHORT)):
        tips.append("Толпа набивается при горячем фандинге — риск обратного сквиза, стоп не отодвигать.")
    if s.get("near_wall") or s.get("near_floor"):
        tips.append(f"До уровня меньше 0.5% — лучше вход после пробоя {s['tp1_price']:.6g} и закрепления на 1м.")
    if s.get("signal_mode") == "explosion" and s.get("rvol", 0) >= RVOL_HOT_THRESHOLD:
        tips.append("Климакс-объём: если следующая 5м свеча закроется за серединой сигнальной — импульс исчерпан.")
    if is_long and s.get("change_24h", 0) > 15:
        tips.append("Монета разогрета за сутки — вход только на откате, размер меньше.")
    tk = s.get("taker_15m")
    if tk and ((is_long and tk < 0.9) or (not is_long and tk > 1.1)):
        tips.append("Цена идёт, а агрессор против — возможен вынос стопов, жди подтверждения дельтой.")
    if (is_long and "Нисходящий" in h4) or (not is_long and "Восходящий" in h4):
        tips.append("Против тренда BTC 4H — половина размера и быстрый TP1.")
    if s.get("from_charge"):
        tips.append("Импульс вышел из ЗАРЯДа — граница диапазона теперь опора, ретест = второй шанс входа.")
    tips.append("Проверь дельту/CVD на 1м: подтверждение — агрессор в сторону сделки.")
    return tips[:4]

def apply_marks(s: dict, is_long: bool):
    s["near_wall"]  = is_long and s["room_pct"] < 0.5
    s["near_floor"] = (not is_long) and s["room_pct"] < 0.5
    quality, marks = 0, ""
    if s["rvol"] >= RVOL_HOT_THRESHOLD:    quality += 2; marks += "🔥🔥"
    elif s["rvol"] >= RVOL_THRESHOLD * 2:  quality += 1; marks += "🔥"
    if is_long and s["funding"] > FUNDING_HOT_LONG:       quality -= 1; marks += "⚠️фандинг"
    if not is_long and s["funding"] < FUNDING_HOT_SHORT:  quality -= 1; marks += "⚠️фандинг"
    if s["near_wall"]:  quality -= 1; marks += "🧱стена"
    if s["near_floor"]: quality -= 1; marks += "🧱пол"
    if s.get("from_charge"): marks += "⏳"
    s["quality"], s["marks"] = quality, marks

def build_momentum(sym, closed, price, baseline, ctx, tick, stats_cache):
    """RS Momentum на закрытых 5м свечах. closed[-1] = последняя закрытая
    (в v7 это было candles[-2])."""
    # ── Условие 1: ВЗРЫВ — лучшая из 3 последних закрытых свечей ──
    best_candle, best_rvol = None, 0.0
    for c in closed[-3:]:
        r = round(c["v"] / baseline, 2)
        if r > best_rvol:
            best_rvol, best_candle = r, c
    signal_mode, rvol = None, 0.0
    if best_rvol >= RVOL_EXPLOSION_THRESHOLD:
        signal_mode, rvol = "explosion", best_rvol
    else:
        # ── Условие 2: РАЗГОН ОБЪЁМА (в v7 называлось «накопление») ──
        v_prev, v_last = closed[-2]["v"], closed[-1]["v"]
        avg2 = round((v_prev + v_last) / (2 * baseline), 2)
        if v_last > v_prev * 1.10 and avg2 >= RVOL_THRESHOLD:
            signal_mode, rvol, best_candle = "accumulation", avg2, closed[-1]
    if signal_mode is None:
        return None

    # Раскорр: одно и то же 15-минутное окно для альта и BTC (FIX v8)
    alt_chg_window = pct(closed[-3]["o"], closed[-1]["c"])
    decorr = alt_chg_window - ctx["btc_chg_15"]
    watch_side = ctx["watch"].get(sym)
    thr_long  = DECORR_WATCH if watch_side in ("long", "both") else BTC_DECORR_THRESHOLD
    thr_short = -DECORR_WATCH if watch_side in ("short", "both") else BTC_DECORR_SHORT

    rng = best_candle["h"] - best_candle["l"]
    close_position = (best_candle["c"] - best_candle["l"]) / rng if rng > 0 else 0.5

    is_long = None
    if decorr >= thr_long and close_position >= (CLOSE_POS_THRESHOLD if signal_mode == "explosion" else 0.4):
        is_long = True
    elif decorr <= thr_short and close_position <= (CLOSE_POS_SHORT if signal_mode == "explosion" else 0.6):
        is_long = False
    if is_long is None:
        return None

    recent = closed[-100:]
    atrs = calc_atr([c["h"] for c in recent], [c["l"] for c in recent], [c["c"] for c in recent], 14)
    atr = atrs[-1] if atrs else 0
    sw = closed[-SWING_LOOKBACK:]
    sh, sl = find_swings([c["h"] for c in sw], [c["l"] for c in sw])
    tp1, l1, tp2, l2 = structure_targets(price, is_long, sh if is_long else sl, atr)

    dyn = calc_dynamic_stop_pct(atr, price)
    stop = price * (1 - dyn / 100) if is_long else price * (1 + dyn / 100)
    room = pct(price, tp1) if is_long else -pct(price, tp1)

    stats = stats_cache(sym)
    oi_15m = stats["oi_15m"] if stats and stats["oi_15m"] is not None else ticker_oi_change(sym, 15)
    oi_1h  = stats["oi_1h"]  if stats and stats["oi_1h"]  is not None else ticker_oi_change(sym, 60)

    s = {
        "symbol": sym, "side": "long" if is_long else "short", "price": price, "decorr": decorr,
        "decorr_thr": thr_long if is_long else thr_short, "stop_pct": dyn, "stop": stop,
        "alt_chg": alt_chg_window, "rvol": rvol, "signal_mode": signal_mode,
        "close_position": close_position, "funding": tick.get("funding", 0),
        "change_24h": tick.get("change_24h", 0), "oi_15m": oi_15m, "oi_1h": oi_1h,
        "taker_15m": stats["taker_15m"] if stats else None,
        "liq_long_1h": stats["liq_long_1h"] if stats else 0,
        "liq_short_1h": stats["liq_short_1h"] if stats else 0,
        "room_pct": room, "tp1_price": tp1, "tp1_pct": pct(price, tp1), "tp1_label": l1,
        "tp2_price": tp2, "tp2_pct": pct(price, tp2), "tp2_label": l2,
        "from_charge": watch_side == "both" or watch_side == ("long" if is_long else "short"),
        "charge_score": ctx["watch_score"].get(sym, ""), "charge_side": watch_side or "",
    }
    apply_marks(s, is_long)
    return s

def get_fresh_price(symbol: str):
    data = api_get("tickers", {"contract": f"{symbol}_USDT"}, timeout=5)
    if isinstance(data, list) and data:
        return fnum(data[0].get("last", 0)) or None
    return None

def refresh_signal(s: dict, is_long: bool) -> dict:
    """Свежая цена перед отправкой: вход, стоп, комната, TP% и метки пересчитываются."""
    fresh = get_fresh_price(s["symbol"])
    if not fresh:
        return s
    old = s["price"]
    s["price"] = fresh
    sp = s.get("stop_pct", STOP_PCT_BASE)
    s["stop"] = fresh * (1 - sp / 100) if is_long else fresh * (1 + sp / 100)
    s["room_pct"] = pct(fresh, s["tp1_price"]) if is_long else -pct(fresh, s["tp1_price"])
    s["tp1_pct"] = pct(fresh, s["tp1_price"])
    s["tp2_pct"] = pct(fresh, s["tp2_price"])
    apply_marks(s, is_long)   # FIX v8: в v7 метки «стена/пол» не обновлялись после refresh
    print(f"  [REFRESH] {s['symbol']}: {old:.6g} → {fresh:.6g}")
    return s

def format_momentum(s: dict, i: int, is_long: bool, score: int, verdict: str, plus, minus) -> str:
    medals = ["🥇", "🥈", "🥉"]
    medal = medals[i] if i < len(medals) else "▪️"
    marks = f" {s['marks']}" if s["marks"] else ""
    mode = "🚀 Взрыв" if s["signal_mode"] == "explosion" else "📊 Разгон объёма"
    oi = s.get("oi_15m")
    if oi is None:
        oi_txt = "⏳ OI нет данных"
    elif oi > 1:
        oi_txt = f"📈 OI 15м +{oi}% — новые {'лонги' if is_long else 'шорты'} ✅"
    elif oi < -1:
        oi_txt = f"📉 OI 15м {oi}% — {'шорты' if is_long else 'лонги'} закрываются ⚠️"
    else:
        oi_txt = f"➡️ OI 15м {oi:+}% (стоит)"
    if s.get("oi_1h") is not None:
        oi_txt += f" | 1ч {s['oi_1h']:+.2f}%"
    tk = s.get("taker_15m")
    tk_txt = f"{tk} ({'покупатели' if tk > 1 else 'продавцы'} агрессивнее)" if tk else "—"
    sign = "-" if is_long else "+"
    lines = [
        f"{medal} <b>{s['symbol']}/USDT</b>{marks}",
        f"   RVOL: <b>{s['rvol']}x</b> {mode} ✅ Gate.io",
        f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC (порог {abs(s['decorr_thr']):.1f}%) | Альт 15М: {s['alt_chg']:+.2f}%",
        f"   Закрытие свечи: {s['close_position']*100:.0f}% | Фандинг: {s['funding']:+.3f}%",
        f"   📈 Рост 24ч: {s['change_24h']:+.2f}%",
        f"   {oi_txt}",
        f"   Taker L/S 15м: {tk_txt}",
    ]
    if s.get("liq_short_1h") or s.get("liq_long_1h"):
        lines.append(f"   Ликвидации 1ч: шортов {fmt_usd(s['liq_short_1h'])} / лонгов {fmt_usd(s['liq_long_1h'])}")
    lines += [
        f"   Комната до {'хая' if is_long else 'лоя'}: {s['room_pct']:.2f}%",
        f"   Вход: <b>{s['price']:.6g}</b>",
        f"   Стоп: {s['stop']:.6g} ({sign}{s['stop_pct']:.2f}%)",
        f"   TP1: {s['tp1_price']:.6g} ({s['tp1_pct']:+.1f}%) — {s['tp1_label']} — 50%",
        f"   TP2: {s['tp2_price']:.6g} ({s['tp2_pct']:+.1f}%) — {s['tp2_label']} — 50%",
        f"   📊 Оценка: <b>{score}</b>",
        f"   💡 {esc(verdict)}",
        "   🔎 <b>Анализ:</b>",
    ]
    lines += [f"     ✅ {esc(n)}" for n in plus]
    lines += [f"     ⚠️ {esc(n)}" for n in minus]
    lines.append("   👀 <b>На что смотреть:</b>")
    lines += [f"     • {esc(t)}" for t in momentum_tips(s, is_long)]
    return "\n".join(lines)

# ═════════════════════════════════════════════════════════════════════════════
#  ОСНОВНОЙ СКАН (каждые 5 минут)
# ═════════════════════════════════════════════════════════════════════════════

def scan_symbol(sym: str, ctx: dict) -> dict:
    out = {"momentum": None, "charge": None}
    candles = get_candles(sym, "5m", CANDLES_LIMIT)
    closed, _, price = split_closed(candles, 300, ctx["now_ts"])
    if len(closed) < 60 or price <= 0:
        return out
    baseline = trimmed_mean([c["v"] for c in closed[-BASE_FROM:-BASE_TO]])
    if baseline <= 0:
        return out
    tr_slice = true_ranges(closed)[-BASE_FROM:-BASE_TO]
    atr_norm = sum(tr_slice) / len(tr_slice) if tr_slice else 0
    tick = ctx["tickers"].get(sym, {})

    cache = {}
    def stats_cache(s):
        if s not in cache:
            cache[s] = get_contract_stats(s)
        return cache[s]

    out["momentum"] = build_momentum(sym, closed, price, baseline, ctx, tick, stats_cache)
    out["charge"] = detect_charge(sym, closed, price, baseline, atr_norm, ctx, tick, stats_cache)
    return out

def run_scan():
    now_ts = time.time()
    print(f"[SCAN] Старт {msk_time_str()}")

    btc = get_candles("BTC", "5m", 20)
    btc_closed, _, _ = split_closed(btc, 300, now_ts)
    if len(btc_closed) < 13:
        print("[SCAN] BTC свечи не получены")
        return None
    btc_chg_15 = pct(btc_closed[-3]["o"], btc_closed[-1]["c"])
    btc_chg_1h = pct(btc_closed[-12]["o"], btc_closed[-1]["c"])
    LAST_BTC.update({"chg15": btc_chg_15, "chg1h": btc_chg_1h, "ts": now_ts})
    print(f"[SCAN] BTC 15М: {btc_chg_15:+.2f}% | 1ч: {btc_chg_1h:+.2f}%")

    tickers = get_gate_tickers()
    if tickers:
        push_oi_snapshot(tickers, now_ts)
    market_ctx = get_market_context()

    ctx = {"now_ts": now_ts, "btc_chg_15": btc_chg_15, "btc_chg_1h": btc_chg_1h,
           "tickers": tickers, "watch": {s: w["side"] for s, w in WATCHLIST.items()},
           "watch_score": {s: w["score"] for s, w in WATCHLIST.items()}}

    results = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = [(ex.submit(scan_symbol, sym, ctx), sym) for sym in UPSCALE_PAIRS]
        for f, sym in futures:
            try:
                results.append(f.result())
            except Exception as e:
                print(f"  [ERROR] {sym}: {e}")
    print(f"[SCAN] готово за {time.time() - now_ts:.1f}с")

    # ── ⏳ ЗАРЯДЫ ──
    charges = [r["charge"] for r in results if r["charge"]]
    alerts = register_charges(charges)
    if alerts:
        send_blocks([format_charge(c) for c in alerts])
        for c in alerts:
            print(f"[CHARGE] {c['symbol']} {c['side']} score={c['score']}")

    # ── 🚀 ИМПУЛЬС ──
    longs  = [r["momentum"] for r in results if r["momentum"] and r["momentum"]["side"] == "long"]
    shorts = [r["momentum"] for r in results if r["momentum"] and r["momentum"]["side"] == "short"]
    sent = 0
    for group, is_long in ((longs, True), (shorts, False)):
        if not group:
            continue
        # FIX v8: сортируем по полной оценке, а не по упрощённой quality
        for s in group:
            s["score"] = analyze_signal(s, is_long, btc_chg_15)[0]
        group.sort(key=lambda x: (x["score"], x["quality"], abs(x["decorr"])), reverse=True)
        side = "long" if is_long else "short"
        top = []
        for s in group:
            last = LAST_SENT.get(("momentum", s["symbol"], side))
            if last and now_ts - last[0] < MOMENTUM_COOLDOWN_MIN * 60 and s["score"] < last[1] + 2:
                continue
            top.append(s)
            if len(top) >= TOP_N:
                break
        if not top:
            continue
        top = [refresh_signal(s, is_long) for s in top]
        header = (f"📡 <b>{'🚀 ИМПУЛЬС — ЛОНГ' if is_long else '🚀 ИМПУЛЬС — ШОРТ'}</b> | {msk_time_str()}\n"
                  f"BTC 15М: <b>{btc_chg_15:+.2f}%</b> | 1ч: {btc_chg_1h:+.2f}%\n{market_ctx}\n")
        blocks = [header]
        for i, s in enumerate(top):
            score, verdict, plus, minus = analyze_signal(s, is_long, btc_chg_15)
            blocks.append(format_momentum(s, i, is_long, score, verdict, plus, minus))
            log_signal("momentum", s, side, score)
            LAST_SENT[("momentum", s["symbol"], side)] = (now_ts, score)
            sent += 1
        send_blocks(blocks)

    print(f"[SCAN] Лонгов: {len(longs)} | Шортов: {len(shorts)} | Зарядов: {len(charges)} | Watchlist: {len(WATCHLIST)}")
    return sent + len(alerts), btc_chg_15

# ─── СТАТУС ───────────────────────────────────────────────────────────────────

def send_status(signal_count=0, btc_chg=None):
    now = datetime.now(MSK)
    ctx = get_market_context()
    btc_line = f"BTC 15М: <b>{btc_chg:+.2f}%</b>\n" if btc_chg is not None else ""
    wl = ", ".join(f"{s}{'🟢' if w['side']=='long' else '🔴' if w['side']=='short' else '⚪'}"
                   for s, w in WATCHLIST.items()) or "пусто"
    send_telegram(
        f"🤖 <b>Upscale Bot {BOT_VERSION}</b> | {now.strftime('%H:%M МСК')}\n"
        f"✅ ⏳ЗАРЯД → ⚡ПРОБОЙ → 🚀ИМПУЛЬС\n"
        f"{btc_line}{ctx}\n"
        f"Сигналов в последнем скане: <b>{signal_count}</b>\n"
        f"Watchlist ({len(WATCHLIST)}): {wl}\n"
        f"Ожидают оценки: сигналов {len(PENDING_OUTCOMES)}, зарядов {len(CHARGE_PENDING)}\n"
        f"Пар в скане: {len(UPSCALE_PAIRS)}"
    )

# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────

def main():
    send_telegram(
        f"🚀 <b>Upscale Bot {BOT_VERSION} запущен</b>\n"
        "⏳ ЗАРЯД — сжатие + объём/OI при стоящей цене (до движения)\n"
        f"⚡ ПРОБОЙ — триггер по watchlist каждые {FAST_INTERVAL_SEC}с на 1м свечах\n"
        "🚀 ИМПУЛЬС — RS Momentum 5М (взрыв / разгон объёма)\n"
        "📊 BTC контекст: 1D + 4H + 15M + EQH/EQL\n"
        f"📒 Лог сигналов и исходов: {os.path.basename(SIGNALS_CSV)}\n"
        f"Пар: {len(UPSCALE_PAIRS)} | Часы: {TRADING_START_MSK}:00–{TRADING_END_MSK}:00 МСК"
    )

    last_signal_count, last_btc = 0, None
    status_sent_hour = -1
    last_scan_key = None
    last_fast = 0.0
    last_outcomes = 0.0
    summary_sent_date = None

    while True:
        try:
            now_msk = datetime.now(MSK)

            if now_msk.hour != status_sent_hour:
                send_status(last_signal_count, last_btc)
                status_sent_hour = now_msk.hour

            today = now_msk.strftime("%Y-%m-%d")
            # сводка в 23:05 — к этому времени оценены и сигналы последнего часа торговли
            if now_msk.hour == (TRADING_END_MSK + 1) % 24 and now_msk.minute >= 5 \
                    and summary_sent_date != today:
                send_daily_summary()
                summary_sent_date = today

            if is_trading_hours() and not is_dead_zone():
                aligned = (now_msk.hour, (now_msk.minute // SCAN_INTERVAL) * SCAN_INTERVAL)
                # весь первый минутный отрезок после границы свечи, а не только первые 15 сек:
                # если fast_check затянулся из-за таймаута API, скан не пропадёт на 5 минут
                if now_msk.minute % SCAN_INTERVAL == 0 and aligned != last_scan_key:
                    last_scan_key = aligned
                    result = run_scan()
                    if result:
                        last_signal_count, last_btc = result
                    last_fast = time.time()
                elif WATCHLIST and time.time() - last_fast >= FAST_INTERVAL_SEC:
                    fast_check()
                    last_fast = time.time()

            if time.time() - last_outcomes >= 60:
                process_outcomes()
                last_outcomes = time.time()

        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            traceback.print_exc()
        time.sleep(2)

if __name__ == "__main__":
    main()
