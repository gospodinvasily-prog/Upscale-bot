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

# ── Скан 2: Sweep Reversal 1H (каждые 30 минут) ──
BREAKOUT_SCAN_INTERVAL = 30

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

def calc_atr(highs, lows, closes, period=14) -> list:
    trs = [max(highs[i]-lows[i],
               abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    atrs = [sum(trs[:period])/period]
    for tr in trs[period:]:
        atrs.append((atrs[-1]*(period-1) + tr) / period)
    return atrs

# ─── СКАН 1: РАСКОРРЕЛЯЦИЯ (Gate.io закрытые свечи) ─────────────────────────

def run_decorr_scan():
    print(f"[DECORR] Старт {msk_time_str()}")

    # BTC свечи — один раз для всех пар
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
            params={"contract": "BTC_USDT", "interval": "15m", "limit": 25},
            timeout=10
        )
        btc_candles = r.json() if r.status_code == 200 else []
    except Exception:
        btc_candles = []

    if not btc_candles or len(btc_candles) < 4:
        print("[DECORR] BTC свечи не получены")
        return 0, None

    btc_open  = float(btc_candles[-2]["o"])
    btc_close = float(btc_candles[-2]["c"])
    btc_chg   = (btc_close - btc_open) / btc_open * 100 if btc_open else 0
    print(f"[DECORR] BTC 15М: {btc_chg:+.2f}%")

    candidates = []
    for sym in UPSCALE_PAIRS:
        try:
            r = requests.get(
                "https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                params={"contract": f"{sym}_USDT", "interval": "15m", "limit": 25},
                timeout=8
            )
            if r.status_code != 200:
                time.sleep(0.2)
                continue

            candles = r.json()
            if not candles or len(candles) < 4:
                time.sleep(0.2)
                continue

            # 1 закрытая свеча: [-2] открытие и закрытие
            alt_open  = float(candles[-2]["o"])
            alt_close = float(candles[-2]["c"])
            alt_curr  = float(candles[-1]["c"])  # текущая цена входа

            if alt_open == 0:
                time.sleep(0.2)
                continue

            alt_chg = (alt_close - alt_open) / alt_open * 100
            decorr  = alt_chg - btc_chg

            if decorr < BTC_DECORR_THRESHOLD:
                time.sleep(0.2)
                continue

            # RVOL на закрытой свече [-2]
            vol_closed = float(candles[-2]["v"])
            history_vols = [float(c["v"]) for c in candles[-22:-2]]
            vol_avg = sum(history_vols) / len(history_vols) if history_vols else 0
            rvol = round(vol_closed / vol_avg, 2) if vol_avg > 0 else 0

            candidates.append({
                "symbol":  sym,
                "price":   alt_curr,
                "alt_chg": alt_chg,
                "btc_chg": btc_chg,
                "decorr":  decorr,
                "rvol":    rvol,
            })
            print(f"  ✅ {sym}: decorr={decorr:+.2f}% rvol={rvol}x")

        except Exception as e:
            print(f"  [DECORR ERROR] {sym}: {e}")

        time.sleep(0.2)

    print(f"[DECORR] Кандидатов: {len(candidates)}")
    if not candidates:
        return 0, btc_chg

    # Фильтр RVOL
    signals = [c for c in candidates if c["rvol"] >= RVOL_THRESHOLD]
    signals.sort(key=lambda x: x["decorr"], reverse=True)

    if not signals:
        print("[DECORR] Нет сигналов с RVOL")
        return len(candidates), btc_chg

    top = signals[:TOP_N_DECORR]

    lines = [f"📡 <b>РАСКОРРЕЛЯЦИЯ</b> | {msk_time_str()}\n"
             f"BTC 15М: <b>{btc_chg:+.2f}%</b>\n"]

    medals = ["🥇","🥈","🥉"]
    for i, s in enumerate(top):
        entry = s["price"]
        stop  = entry * (1 + STOP_PCT / 100)
        tp1   = entry * (1 + TP1_PCT  / 100)
        tp2   = entry * (1 + TP2_PCT  / 100)
        medal = medals[i] if i < len(medals) else "▪️"

        lines.append(
            f"{medal} <b>{s['symbol']}/USDT</b>\n"
            f"   RVOL: <b>{s['rvol']}x</b> ✅ Gate.io (закр. свеча)\n"
            f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC | Альт 15М: {s['alt_chg']:+.2f}%\n"
            f"   Вход: <b>{entry:.6g}</b>\n"
            f"   Стоп: {stop:.6g} ({STOP_PCT}%)\n"
            f"   TP1:  {tp1:.6g} ({TP1_PCT:+}%) — 50%\n"
            f"   TP2:  {tp2:.6g} ({TP2_PCT:+}%) — 50%\n"
        )

    lines.append("⚠️ Проверь структуру и CVD. Решение за тобой.")
    send_telegram("\n".join(lines))
    print(f"[DECORR] Отправлено {len(top)} сигналов")
    return len(candidates), btc_chg

# ─── СКАН 2: LIQUIDITY SWEEP REVERSAL (ложный пробой) 1H ────────────────────

PIVOT_LOOKBACK   = 12      # свечей для поиска локального хая/лоя
DISPLACEMENT_MULT = 1.5    # тело displacement свечи > 1.5x среднего тела
VOL_MULT_SWEEP   = 1.8     # объём displacement > 1.8x среднего
MSS_LOOKBACK     = 8       # свечей для поиска точки MSS (противоположный экстремум)
ATR_SL_BUFFER    = 0.5     # буфер стопа сверх фитиля свипа, в ATR
TOP_N_SWEEP      = 3

def find_pivot_high(highs, end_idx, lookback):
    """Локальный максимум в окне [end_idx-lookback : end_idx]."""
    window = highs[end_idx-lookback:end_idx]
    return max(window) if window else None

def find_pivot_low(lows, end_idx, lookback):
    """Локальный минимум в окне [end_idx-lookback : end_idx]."""
    window = lows[end_idx-lookback:end_idx]
    return min(window) if window else None

def analyze_sweep(symbol: str):
    """
    Liquidity Sweep Reversal на 1H:
    1. Свеча [-3] пробивает фитилём локальный хай/лой (последние PIVOT_LOOKBACK свечей до неё),
       но закрывается ОБРАТНО внутри диапазона (тело не пробивает уровень) — это свип.
    2. Свеча [-2] — displacement: тело > DISPLACEMENT_MULT от среднего тела,
       объём > VOL_MULT_SWEEP от среднего объёма, направлена ПРОТИВ свипа.
    3. MSS: displacement пробивает ближайший противоположный локальный экстремум
       за последние MSS_LOOKBACK свечей — подтверждение слома структуры.
    4. FVG (бонус) — гэп между свечами вокруг displacement в сторону разворота.
    """
    candles = get_gate_candles_1h(symbol, limit=60)
    if not candles or len(candles) < 40:
        return None

    closes  = [float(c["c"]) for c in candles]
    highs   = [float(c["h"]) for c in candles]
    lows    = [float(c["l"]) for c in candles]
    opens   = [float(c["o"]) for c in candles]
    volumes = [float(c["v"]) for c in candles]

    atrs = calc_atr(highs, lows, closes, 14)
    if not atrs:
        return None
    atr = atrs[-1]

    # Индексы: -3 свип-свеча, -2 displacement-свеча, -1 текущая (для входа)
    sweep_idx = -3
    disp_idx  = -2

    # Средние значения для сравнения (за 20 свечей до свип-свечи)
    body_window = [abs(closes[i]-opens[i]) for i in range(sweep_idx-20, sweep_idx)]
    avg_body = sum(body_window)/len(body_window) if body_window else 0
    vol_window = volumes[sweep_idx-20:sweep_idx]
    avg_vol = sum(vol_window)/len(vol_window) if vol_window else 0

    if avg_body == 0 or avg_vol == 0:
        return None

    # Пивоты ДО свип-свечи
    pivot_high = find_pivot_high(highs, sweep_idx, PIVOT_LOOKBACK)
    pivot_low  = find_pivot_low(lows, sweep_idx, PIVOT_LOOKBACK)

    sweep_high = highs[sweep_idx]
    sweep_low  = lows[sweep_idx]
    sweep_close= closes[sweep_idx]
    sweep_open = opens[sweep_idx]

    disp_open  = opens[disp_idx]
    disp_close = closes[disp_idx]
    disp_high  = highs[disp_idx]
    disp_low   = lows[disp_idx]
    disp_body  = abs(disp_close - disp_open)
    disp_vol   = volumes[disp_idx]

    entry_price = closes[-1]

    is_displacement = disp_body > DISPLACEMENT_MULT * avg_body
    is_high_vol = disp_vol > VOL_MULT_SWEEP * avg_vol

    print(f"[SWEEP] {symbol}: sweepH={sweep_high:.4g} sweepL={sweep_low:.4g} "
          f"pivH={pivot_high:.4g} pivL={pivot_low:.4g} "
          f"disp_body={disp_body:.4g}(avg={avg_body:.4g}) disp_vol={round(disp_vol/avg_vol,1)}x")

    # ── БЫЧИЙ SWEEP: пробили лоу фитилём, закрылись внутри, потом displacement вверх ──
    swept_low = (sweep_low < pivot_low and sweep_close > pivot_low) if pivot_low else False
    if swept_low and is_displacement and is_high_vol and disp_close > disp_open:
        # MSS: displacement должен пробить ближайший противоположный хай
        mss_level = find_pivot_high(highs, disp_idx, MSS_LOOKBACK)
        mss_confirmed = mss_level and disp_close > mss_level

        # Проверяем FVG между свечами sweep и текущей — гэп в сторону разворота
        fvg_bull = highs[sweep_idx] < lows[-1] if len(closes) >= abs(sweep_idx) else False

        if mss_confirmed:
            stop = sweep_low - ATR_SL_BUFFER * atr
            risk = entry_price - stop
            target = entry_price + risk * 2.5
            return {
                "symbol": symbol, "direction": "ЛОНГ (свип лоя)",
                "entry": entry_price, "stop": stop, "tp": target,
                "atr": atr, "disp_vol_x": round(disp_vol/avg_vol,1),
                "disp_body_x": round(disp_body/avg_body,1),
                "fvg": fvg_bull, "sweep_level": pivot_low,
            }

    # ── МЕДВЕЖИЙ SWEEP: пробили хай фитилём, закрылись внутри, потом displacement вниз ──
    swept_high = (sweep_high > pivot_high and sweep_close < pivot_high) if pivot_high else False
    if swept_high and is_displacement and is_high_vol and disp_close < disp_open:
        mss_level = find_pivot_low(lows, disp_idx, MSS_LOOKBACK)
        mss_confirmed = mss_level and disp_close < mss_level

        fvg_bear = lows[sweep_idx] > highs[-1] if len(closes) >= abs(sweep_idx) else False

        if mss_confirmed:
            stop = sweep_high + ATR_SL_BUFFER * atr
            risk = stop - entry_price
            target = entry_price - risk * 2.5
            return {
                "symbol": symbol, "direction": "ШОРТ (свип хая)",
                "entry": entry_price, "stop": stop, "tp": target,
                "atr": atr, "disp_vol_x": round(disp_vol/avg_vol,1),
                "disp_body_x": round(disp_body/avg_body,1),
                "fvg": fvg_bear, "sweep_level": pivot_high,
            }

    return None

def run_sweep_scan():
    print(f"[SWEEP] Старт {msk_time_str()}")
    signals = []

    for sym in UPSCALE_PAIRS:
        result = analyze_sweep(sym)
        if result:
            signals.append(result)
            print(f"  ✅ {sym}: {result['direction']} | disp_vol {result['disp_vol_x']}x | disp_body {result['disp_body_x']}x")
        time.sleep(0.4)

    if not signals:
        print("[SWEEP] Свипов нет")
        return

    signals.sort(key=lambda x: x["disp_vol_x"], reverse=True)
    top = signals[:TOP_N_SWEEP]

    medals = ["🥇","🥈","🥉"]
    lines = [f"🎯 <b>SWEEP REVERSAL 1H</b> | {msk_time_str()}\n"]

    for i, s in enumerate(top):
        medal = medals[i] if i < len(medals) else "▪️"
        emoji = "🟢" if "ЛОНГ" in s["direction"] else "🔴"
        entry = s["entry"]
        stop  = s["stop"]
        tp    = s["tp"]
        rr    = abs(tp-entry) / abs(entry-stop) if abs(entry-stop) > 0 else 0
        fvg_mark = "🔥 FVG в сторону разворота" if s["fvg"] else ""

        lines.append(
            f"{medal} {emoji} <b>{s['symbol']}/USDT — {s['direction']}</b>\n"
            f"   Уровень свипа: {s['sweep_level']:.6g} {fvg_mark}\n"
            f"   Displacement: тело {s['disp_body_x']}x | объём {s['disp_vol_x']}x\n"
            f"   Вход: <b>{entry:.6g}</b>\n"
            f"   Стоп: {stop:.6g} (за фитилём +{ATR_SL_BUFFER} ATR)\n"
            f"   TP:   {tp:.6g} | RR 1:{rr:.2f}\n"
        )

    lines.append("⚠️ Проверь график — см. инструкцию. Решение за тобой.")
    send_telegram("\n".join(lines))
    print(f"[SWEEP] Отправлено {len(top)} сигналов")

# ─── СТАТУС ───────────────────────────────────────────────────────────────────

def send_status(decorr_count=0, btc_1h=None):
    now = datetime.now(MSK)
    btc_line = f"BTC 1h: <b>{btc_1h:+.2f}%</b> " + ("⬇️" if btc_1h and btc_1h < 0 else "➡️") + "\n" if btc_1h is not None else ""
    status = (
        f"🤖 <b>Upscale Bot v6.0</b> | {now.strftime('%H:%M МСК')}\n"
        f"✅ Gate.io Раскорреляция 15М + Sweep Reversal 1H\n"
        f"{btc_line}"
        f"Раскорреляций: <b>{decorr_count}</b>\n"
        f"{'😴 Жду спайк объёма...' if decorr_count > 0 else '🔍 Раскорреляций нет'}\n"
        f"Пар в скане: {len(UPSCALE_PAIRS)}"
    )
    send_telegram(status)

# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────

def main():
    send_telegram(
        f"🚀 <b>Upscale Bot v6.0 запущен</b>\n"
        "📡 Раскорреляция 15М (Gate.io закр. свеча)\n"
        "🎯 Sweep Reversal 1H каждые 30 мин (свип + displacement + MSS)\n"
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
                run_sweep_scan()
                last_break_scan = now_ts
        else:
            print(f"[LOOP] Вне часов ({msk_time_str()})")

        time.sleep(60)

if __name__ == "__main__":
    main()
