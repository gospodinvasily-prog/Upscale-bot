import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────

CMC_API_KEY    = os.environ.get("CMC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = "426470592"

TRADING_START_MSK = 4
TRADING_END_MSK   = 22
MSK = timezone(timedelta(hours=3))

# ── Скан 1: Раскорреляция (каждые 15 минут) ──
DECORR_SCAN_INTERVAL = 15
BTC_DECORR_THRESHOLD = 1.5
RVOL_THRESHOLD       = 1.2
TOP_N_DECORR         = 3
STOP_PCT  = -3.0
TP1_PCT   = +5.0
TP2_PCT   = +9.0

# ── Скан 2: Пробой 1H (каждые 60 минут) ──
BREAKOUT_SCAN_INTERVAL = 60
BREAKOUT_PERIOD  = 20       # хай/лой последних 20 закрытых свечей
BB_PERIOD        = 20       # Bollinger Bands период
BB_SQUEEZE_RATIO = 0.06     # ширина BB < 6% от цены = сжатие было
VOL_MULT_BREAK   = 2.0      # объём > 2x нормы при пробое
ADX_MIN          = 18
RSI_LONG_MAX     = 72
RSI_SHORT_MIN    = 28
TOP_N_BREAKOUT   = 3
ATR_TP  = 3.8
ATR_SL  = 1.6

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

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG ERROR] {e}")

# ─── ВРЕМЯ ────────────────────────────────────────────────────────────────────

def is_trading_hours() -> bool:
    return TRADING_START_MSK <= datetime.now(MSK).hour < TRADING_END_MSK

def msk_time_str() -> str:
    return datetime.now(MSK).strftime("%H:%M МСК")

# ─── CMC ──────────────────────────────────────────────────────────────────────

def get_cmc_quotes(symbols: list) -> dict:
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params  = {"symbol": ",".join(symbols), "convert": "USDT"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", {})
        result = {}
        for sym, info in data.items():
            q = info.get("quote", {}).get("USDT", {})
            result[sym] = {
                "price":  q.get("price", 0),
                "pct_1h": q.get("percent_change_1h", 0),
            }
        return result
    except Exception as e:
        print(f"[CMC ERROR] {e}")
        return {}

# ─── GATE.IO: 15M RVOL ────────────────────────────────────────────────────────

def get_gate_rvol(symbol: str):
    contract = f"{symbol}_USDT"
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    params = {"contract": contract, "interval": "15m", "limit": 21}
    try:
        r = requests.get(url, params=params, timeout=10)
        print(f"[GATE] {symbol}: HTTP {r.status_code}")
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        candles = r.json()
        if not candles or len(candles) < 21:
            return None, "not enough candles"
        try:
            volumes = [float(c["v"]) for c in candles]
        except Exception:
            return None, "no volume field"
        history_vols = volumes[:20]
        current_vol  = volumes[20]
        if sum(history_vols) == 0:
            return None, "zero volume"
        k = 2 / (20 + 1)
        ema = history_vols[0]
        for v in history_vols[1:]:
            ema = v * k + ema * (1 - k)
        if ema == 0:
            return None, "ema zero"
        rvol = round(current_vol / ema, 2)
        print(f"[GATE] {symbol}: RVOL={rvol}x")
        return rvol, None
    except Exception as e:
        print(f"[GATE ERROR] {symbol}: {e}")
        return None, str(e)

# ─── GATE.IO: 1H свечи для пробоя ────────────────────────────────────────────

def get_gate_candles_1h(symbol: str, limit: int = 230):
    contract = f"{symbol}_USDT"
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    params = {"contract": contract, "interval": "1h", "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        candles = r.json()
        if not candles or len(candles) < limit:
            return None
        return candles
    except Exception as e:
        print(f"[GATE 1H ERROR] {symbol}: {e}")
        return None

# ─── ИНДИКАТОРЫ ───────────────────────────────────────────────────────────────

def calc_ema(prices: list, period: int) -> list:
    k = 2 / (period + 1)
    ema = [prices[0]]
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

def calc_atr(highs, lows, closes, period=14) -> list:
    trs = [max(highs[i]-lows[i],
               abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    atrs = [sum(trs[:period])/period]
    for tr in trs[period:]:
        atrs.append((atrs[-1]*(period-1) + tr) / period)
    return atrs

def calc_rsi(closes, period=14) -> list:
    gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[:period])/period
    al = sum(losses[:period])/period
    rsis = []
    for i in range(period, len(gains)):
        ag = (ag*(period-1)+gains[i])/period
        al = (al*(period-1)+losses[i])/period
        rs = ag/al if al else 100
        rsis.append(100 - 100/(1+rs))
    return rsis

def calc_adx(highs, lows, closes, period=14) -> list:
    pdms = [max(highs[i]-highs[i-1],0) if highs[i]-highs[i-1]>lows[i-1]-lows[i] else 0 for i in range(1,len(closes))]
    mdms = [max(lows[i-1]-lows[i],0) if lows[i-1]-lows[i]>highs[i]-highs[i-1] else 0 for i in range(1,len(closes))]
    trs  = [max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    def smooth(arr, p):
        s = [sum(arr[:p])]
        for v in arr[p:]: s.append(s[-1]-s[-1]/p+v)
        return s
    str_ = smooth(trs, period)
    spdm = smooth(pdms, period)
    smdm = smooth(mdms, period)
    dxs = [100*abs(100*spdm[i]/str_[i]-100*smdm[i]/str_[i])/(100*spdm[i]/str_[i]+100*smdm[i]/str_[i]+0.001) for i in range(len(str_))]
    adxs = [sum(dxs[:period])/period]
    for dx in dxs[period:]: adxs.append((adxs[-1]*(period-1)+dx)/period)
    return adxs

def calc_bollinger(closes, period=20, std_mult=2.0):
    """Возвращает (upper, middle, lower, width_pct) для последней свечи."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = variance ** 0.5
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    width_pct = (upper - lower) / sma  # относительная ширина
    return upper, sma, lower, width_pct

# ─── СКАН 1: РАСКОРРЕЛЯЦИЯ (15М) ─────────────────────────────────────────────

def run_decorr_scan():
    print(f"[DECORR] Старт {msk_time_str()}")

    all_symbols = ["BTC"] + UPSCALE_PAIRS
    quotes = get_cmc_quotes(all_symbols)

    if "BTC" not in quotes:
        print("[DECORR] BTC не получен")
        return 0, None

    btc_1h = quotes["BTC"]["pct_1h"]
    print(f"[DECORR] BTC 1h: {btc_1h:+.2f}%")

    candidates = []
    for sym in UPSCALE_PAIRS:
        if sym not in quotes:
            continue
        alt_1h = quotes[sym]["pct_1h"]
        decorr = alt_1h - btc_1h
        if decorr >= BTC_DECORR_THRESHOLD:
            candidates.append({
                "symbol": sym,
                "price":  quotes[sym]["price"],
                "alt_1h": alt_1h,
                "btc_1h": btc_1h,
                "decorr": decorr,
            })

    print(f"[DECORR] Кандидатов: {len(candidates)}")
    if not candidates:
        return 0, btc_1h

    signals_rvol = []
    signals_cmc  = []

    for c in candidates:
        rvol, err = get_gate_rvol(c["symbol"])
        if rvol is None:
            c["rvol"] = None
            c["gate_err"] = err
            signals_cmc.append(c)
        else:
            if rvol >= RVOL_THRESHOLD:
                c["rvol"] = rvol
                signals_rvol.append(c)
            else:
                print(f"  {c['symbol']}: RVOL {rvol}x < {RVOL_THRESHOLD} — пропуск")
        time.sleep(0.3)

    # Сортировка по раскорреляции (сегодня убедились что это важнее RVOL)
    signals_rvol.sort(key=lambda x: x["decorr"], reverse=True)
    signals_cmc.sort(key=lambda x: x["decorr"], reverse=True)

    top = (signals_rvol + signals_cmc)[:TOP_N_DECORR]
    if not top:
        print("[DECORR] Нет сигналов")
        return len(candidates), btc_1h

    lines = [f"📡 <b>РАСКОРРЕЛЯЦИЯ</b> | {msk_time_str()}\n"
             f"BTC 1h: <b>{btc_1h:+.2f}%</b>\n"]

    medals = ["🥇","🥈","🥉"]
    for i, s in enumerate(top):
        entry = s["price"]
        stop  = entry * (1 + STOP_PCT / 100)
        tp1   = entry * (1 + TP1_PCT  / 100)
        tp2   = entry * (1 + TP2_PCT  / 100)
        medal = medals[i] if i < len(medals) else "▪️"

        if s["rvol"] is not None:
            rvol_line = f"   RVOL: <b>{s['rvol']}x</b> ✅ Gate.io\n"
        else:
            rvol_line = f"   ⚡ Только CMC\n"

        lines.append(
            f"{medal} <b>{s['symbol']}/USDT</b>\n"
            f"{rvol_line}"
            f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC\n"
            f"   Вход: <b>{entry:.6g}</b>\n"
            f"   Стоп: {stop:.6g} ({STOP_PCT}%)\n"
            f"   TP1:  {tp1:.6g} ({TP1_PCT:+}%) — 50%\n"
            f"   TP2:  {tp2:.6g} ({TP2_PCT:+}%) — 50%\n"
        )

    lines.append("⚠️ Проверь структуру и CVD. Решение за тобой.")
    send_telegram("\n".join(lines))
    return len(candidates), btc_1h

# ─── СКАН 2: ПРОБОЙ 1H ───────────────────────────────────────────────────────

def analyze_breakout(symbol: str):
    """
    Анализирует пробой на 1H:
    - Закрытая свеча [-2] пробила хай/лой последних 20 закрытых свечей
    - Объём на свече пробоя > 2x SMA50
    - BB сжатие было перед пробоем (ширина BB < BB_SQUEEZE_RATIO)
    - EMA50 > EMA200 (тренд)
    - RSI не перекуплен
    - ADX > порога
    """
    candles = get_gate_candles_1h(symbol, limit=230)
    if not candles:
        return None

    closes  = [float(c["c"]) for c in candles]
    highs   = [float(c["h"]) for c in candles]
    lows    = [float(c["l"]) for c in candles]
    volumes = [float(c["v"]) for c in candles]

    if len(closes) < 220:
        return None

    # Смотрим на закрытую свечу [-2], не текущую [-1]
    idx = -2

    close_prev = closes[idx]
    high_prev  = highs[idx]
    low_prev   = lows[idx]
    vol_prev   = volumes[idx]

    # Индикаторы по данным до свечи пробоя
    ema50  = calc_ema(closes[:idx], 50)
    ema200 = calc_ema(closes[:idx], 200)
    atrs   = calc_atr(highs[:idx], lows[:idx], closes[:idx], 14)
    rsis   = calc_rsi(closes[:idx], 14)
    adxs   = calc_adx(highs[:idx], lows[:idx], closes[:idx], 14)

    if not all([ema50, ema200, atrs, rsis, adxs]):
        return None

    ef  = ema50[-1]
    es  = ema200[-1]
    atr = atrs[-1]
    rsi_val = rsis[-1]
    adx_val = adxs[-1]

    # Объём SMA50 из предыдущих 50 свечей
    vol_window = volumes[idx-51:idx-1]
    vol_sma = sum(vol_window) / len(vol_window) if vol_window else 0
    high_vol = vol_prev > VOL_MULT_BREAK * vol_sma if vol_sma > 0 else False

    # Пробой: хай/лой последних 20 закрытых свечей (до свечи пробоя)
    window_20_h = highs[idx-21:idx-1]
    window_20_l = lows[idx-21:idx-1]
    high_20 = max(window_20_h) if window_20_h else 0
    low_20  = min(window_20_l) if window_20_l else 99999

    # BB сжатие: смотрим ширину BB за 5 свечей ДО пробоя
    bb_before = calc_bollinger(closes[idx-25:idx-1], BB_PERIOD)
    bb_squeeze = bb_before and bb_before[3] < BB_SQUEEZE_RATIO

    vol_ok = (atr / close_prev) > 0.005

    # ── Лонг пробой ──
    if (close_prev > high_20 and
        high_vol and
        ef > es and
        rsi_val < RSI_LONG_MAX and
        adx_val > ADX_MIN and
        vol_ok):
        return {
            "symbol":    symbol,
            "direction": "ЛОНГ",
            "entry":     closes[-1],   # текущая цена для входа
            "stop":      closes[-1] - ATR_SL * atr,
            "tp":        closes[-1] + ATR_TP * atr,
            "atr":       atr,
            "rsi":       rsi_val,
            "adx":       adx_val,
            "rvol":      round(vol_prev / vol_sma, 1) if vol_sma else 0,
            "bb_squeeze": bb_squeeze,
            "close_candle": close_prev,
            "high_20":   high_20,
        }

    # ── Шорт пробой ──
    if (close_prev < low_20 and
        high_vol and
        ef < es and
        rsi_val > RSI_SHORT_MIN and
        adx_val > ADX_MIN and
        vol_ok):
        return {
            "symbol":    symbol,
            "direction": "ШОРТ",
            "entry":     closes[-1],
            "stop":      closes[-1] + 1.4 * atr,
            "tp":        closes[-1] - 2.5 * atr,
            "atr":       atr,
            "rsi":       rsi_val,
            "adx":       adx_val,
            "rvol":      round(vol_prev / vol_sma, 1) if vol_sma else 0,
            "bb_squeeze": bb_squeeze,
            "close_candle": close_prev,
            "low_20":    low_20,
        }

    return None

def run_breakout_scan():
    print(f"[BREAK] Старт {msk_time_str()}")
    signals = []

    for sym in UPSCALE_PAIRS:
        result = analyze_breakout(sym)
        if result:
            signals.append(result)
            print(f"  ✅ {sym}: {result['direction']} | Vol {result['rvol']}x | ADX {result['adx']:.0f} | BB сжатие: {result['bb_squeeze']}")
        time.sleep(0.4)

    if not signals:
        print("[BREAK] Пробоев нет")
        return

    signals.sort(key=lambda x: x["adx"], reverse=True)
    top = signals[:TOP_N_BREAKOUT]

    medals = ["🥇","🥈","🥉"]
    lines = [f"📊 <b>ПРОБОЙ 1H</b> | {msk_time_str()}\n"]

    for i, s in enumerate(top):
        medal = medals[i] if i < len(medals) else "▪️"
        emoji = "🟢" if s["direction"] == "ЛОНГ" else "🔴"
        entry = s["entry"]
        stop  = s["stop"]
        tp    = s["tp"]
        rr    = abs(tp-entry) / abs(entry-stop) if abs(entry-stop) > 0 else 0
        squeeze_mark = "🔥 BB сжатие было" if s["bb_squeeze"] else ""

        lines.append(
            f"{medal} {emoji} <b>{s['symbol']}/USDT — {s['direction']}</b>\n"
            f"   Закр. свеча: {s['close_candle']:.6g} | Vol: {s['rvol']}x {squeeze_mark}\n"
            f"   ADX: {s['adx']:.0f} | RSI: {s['rsi']:.0f}\n"
            f"   Вход: <b>{entry:.6g}</b>\n"
            f"   Стоп: {stop:.6g} (ATR×{ATR_SL})\n"
            f"   TP:   {tp:.6g} (ATR×{ATR_TP}) | RR 1:{rr:.2f}\n"
        )

    lines.append("⚠️ Проверь структуру. Решение за тобой.")
    send_telegram("\n".join(lines))
    print(f"[BREAK] Отправлено {len(top)} сигналов")

# ─── СТАТУС ───────────────────────────────────────────────────────────────────

def send_status(decorr_count=0, btc_1h=None):
    now = datetime.now(MSK)
    btc_line = f"BTC 1h: <b>{btc_1h:+.2f}%</b> " + ("⬇️" if btc_1h and btc_1h < 0 else "➡️") + "\n" if btc_1h is not None else ""
    status = (
        f"🤖 <b>Upscale Bot v5.0</b> | {now.strftime('%H:%M МСК')}\n"
        f"✅ Раскорреляция 15M + Пробой 1H\n"
        f"{btc_line}"
        f"Раскорреляций: <b>{decorr_count}</b>\n"
        f"{'😴 Жду спайк объёма...' if decorr_count > 0 else '🔍 Раскорреляций нет'}\n"
        f"Пар в скане: {len(UPSCALE_PAIRS)}"
    )
    send_telegram(status)

# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────

def main():
    send_telegram(
        "🚀 <b>Upscale Bot v5.0 запущен</b>\n"
        "📡 Раскорреляция каждые 15М\n"
        "📊 Пробой 1H каждый час\n"
        f"Пар: {len(UPSCALE_PAIRS)} | Часы: {TRADING_START_MSK}:00–{TRADING_END_MSK}:00 МСК"
    )

    last_decorr      = 0
    last_btc         = None
    status_sent_hour = -1
    last_decorr_scan = 0
    last_break_scan  = 0

    while True:
        now_msk  = datetime.now(MSK)
        cur_hour = now_msk.hour
        now_ts   = time.time()

        # Статус раз в час
        if cur_hour != status_sent_hour:
            send_status(last_decorr, last_btc)
            status_sent_hour = cur_hour

        if is_trading_hours():
            # Скан раскорреляции каждые 15 минут
            if now_ts - last_decorr_scan >= DECORR_SCAN_INTERVAL * 60:
                result = run_decorr_scan()
                if result:
                    last_decorr, last_btc = result
                last_decorr_scan = now_ts

            # Скан пробоя каждые 60 минут
            if now_ts - last_break_scan >= BREAKOUT_SCAN_INTERVAL * 60:
                run_breakout_scan()
                last_break_scan = now_ts
        else:
            print(f"[LOOP] Вне часов ({msk_time_str()})")

        time.sleep(60)

if __name__ == "__main__":
    main()
