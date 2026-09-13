import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────

CMC_API_KEY    = os.environ.get("CMC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = "426470592"

SCAN_INTERVAL_MIN  = 15
TRADING_START_MSK  = 4
TRADING_END_MSK    = 22

BTC_DECORR_THRESHOLD = 1.5   # % раскорреляция vs BTC за 1h
RVOL_THRESHOLD       = 1.2   # снижено с 1.5 для тестирования Gate.io
TOP_N_SIGNALS        = 3

STOP_PCT = -3.0
TP1_PCT  = +5.0
TP2_PCT  = +9.0

MSK = timezone(timedelta(hours=3))

# ─── UPSCALE PAIRS ────────────────────────────────────────────────────────────

UPSCALE_PAIRS = [
    "ETH","BNB","XRP","SOL","AAVE","ADA","AERO","ALGO","APT","ARB",
    "ASTER","ATOM","AVAX","AXS","BCH","BERA","BGB","BONK","BRETT","BSV",
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

# ─── CMC: цены и 1h изменения ─────────────────────────────────────────────────

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

# ─── GATE.IO: реальный объём свечей ───────────────────────────────────────────

def get_gate_rvol(symbol: str):
    """
    Берёт 21 свечу 15m с Gate.io фьючерсов.
    Считает EMA-20 по объёму (поле 'a' — USDT объём, универсальнее чем 'v').
    Возвращает (rvol, raw_data) или (None, error_str).
    """
    contract = f"{symbol}_USDT"
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    params = {"contract": contract, "interval": "15m", "limit": 21}
    try:
        r = requests.get(url, params=params, timeout=10)

        # Логируем статус для каждой пары — видно в Render логах
        print(f"[GATE] {symbol}: HTTP {r.status_code}")

        if r.status_code != 200:
            print(f"[GATE ERROR] {symbol}: статус {r.status_code}, ответ: {r.text[:200]}")
            return None, f"HTTP {r.status_code}"

        candles = r.json()

        if not candles:
            print(f"[GATE ERROR] {symbol}: пустой ответ")
            return None, "empty"

        if len(candles) < 21:
            print(f"[GATE ERROR] {symbol}: мало свечей ({len(candles)} < 21)")
            return None, f"only {len(candles)} candles"

        # Логируем первую свечу чтобы видеть формат
        print(f"[GATE] {symbol} первая свеча: {candles[0]}")

        # Берём 'a' — объём в USDT (универсальнее чем 'v' в контрактах)
        try:
            volumes = [float(c["a"]) for c in candles]
        except (KeyError, TypeError) as e:
            # Fallback на 'v' если 'a' нет
            print(f"[GATE] {symbol}: поле 'a' недоступно ({e}), пробуем 'v'")
            try:
                volumes = [float(c["v"]) for c in candles]
            except Exception as e2:
                print(f"[GATE ERROR] {symbol}: ни 'a' ни 'v' не работают: {e2}")
                return None, "no volume field"

        history_vols = volumes[:20]
        current_vol  = volumes[20]

        if sum(history_vols) == 0:
            print(f"[GATE ERROR] {symbol}: нулевой объём в истории")
            return None, "zero volume"

        # EMA-20
        k = 2 / (20 + 1)
        ema = history_vols[0]
        for v in history_vols[1:]:
            ema = v * k + ema * (1 - k)

        if ema == 0:
            return None, "ema zero"

        rvol = round(current_vol / ema, 2)
        print(f"[GATE] {symbol}: RVOL={rvol}x (cur={current_vol:.0f}, ema={ema:.0f})")
        return rvol, None

    except Exception as e:
        print(f"[GATE ERROR] {symbol}: {e}")
        return None, str(e)

# ─── ВРЕМЯ ────────────────────────────────────────────────────────────────────

def is_trading_hours() -> bool:
    now_msk = datetime.now(MSK)
    return TRADING_START_MSK <= now_msk.hour < TRADING_END_MSK

def msk_time_str() -> str:
    return datetime.now(MSK).strftime("%H:%M МСК")

# ─── ПРОВЕРКА ПАР НА GATE.IO ──────────────────────────────────────────────────

def check_gate_pairs() -> list:
    """При старте проверяет все пары и убирает нерабочие."""
    bad = []
    print("[CHECK] Проверяю пары на Gate.io...")
    for sym in UPSCALE_PAIRS:
        contract = f"{sym}_USDT"
        url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
        params = {"contract": contract, "interval": "15m", "limit": 2}
        try:
            r = requests.get(url, params=params, timeout=8)
            if r.status_code != 200 or not r.json():
                bad.append(sym)
                print(f"  [CHECK] ❌ {sym} — HTTP {r.status_code}")
            else:
                print(f"  [CHECK] ✅ {sym}")
        except Exception as e:
            bad.append(sym)
            print(f"  [CHECK] ❌ {sym} — {e}")
        time.sleep(0.3)
    return bad

# ─── ОСНОВНОЙ СКАН ────────────────────────────────────────────────────────────

def run_scan():
    print(f"[SCAN] Старт {msk_time_str()}")

    all_symbols = ["BTC"] + UPSCALE_PAIRS
    quotes = get_cmc_quotes(all_symbols)

    if "BTC" not in quotes:
        print("[SCAN] BTC не получен, пропускаем")
        return

    btc_1h = quotes["BTC"]["pct_1h"]
    print(f"[SCAN] BTC 1h: {btc_1h:+.2f}%")

    # Фильтр раскорреляции
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

    print(f"[SCAN] Кандидатов с раскорреляцией >= {BTC_DECORR_THRESHOLD}%: {len(candidates)}")

    if not candidates:
        return

    # Gate.io RVOL + fallback только CMC
    signals_rvol = []   # с подтверждением Gate.io
    signals_cmc  = []   # только CMC (Gate.io не ответил)

    for c in candidates:
        rvol, err = get_gate_rvol(c["symbol"])

        if rvol is None:
            # Fallback — сигнал только по CMC без RVOL
            c["rvol"] = None
            c["gate_err"] = err
            signals_cmc.append(c)
            print(f"  {c['symbol']}: Gate.io ошибка ({err}) → только CMC")
        else:
            if rvol >= RVOL_THRESHOLD:
                c["rvol"] = rvol
                signals_rvol.append(c)
            else:
                print(f"  {c['symbol']}: RVOL {rvol}x < {RVOL_THRESHOLD} — пропуск")
        time.sleep(0.3)

    # Сортировка
    signals_rvol.sort(key=lambda x: (x["rvol"], x["decorr"]), reverse=True)
    signals_cmc.sort(key=lambda x: x["decorr"], reverse=True)

    # Формируем топ — сначала RVOL сигналы, потом CMC fallback
    top = (signals_rvol + signals_cmc)[:TOP_N_SIGNALS]

    if not top:
        print("[SCAN] Нет сигналов")
        return

    lines = [f"📡 <b>СИГНАЛЫ v4.1</b> | {msk_time_str()}\n"
             f"BTC 1h: <b>{btc_1h:+.2f}%</b>\n"]

    medals = ["🥇","🥈","🥉"]
    for i, s in enumerate(top):
        entry = s["price"]
        stop  = entry * (1 + STOP_PCT / 100)
        tp1   = entry * (1 + TP1_PCT  / 100)
        tp2   = entry * (1 + TP2_PCT  / 100)
        medal = medals[i] if i < len(medals) else "▪️"

        if s["rvol"] is not None:
            rvol_line = f"   RVOL: <b>{s['rvol']}x</b> ✅ Gate.io подтверждён\n"
        else:
            rvol_line = f"   ⚡ Только CMC (Gate.io: {s.get('gate_err','?')})\n"

        lines.append(
            f"{medal} <b>{s['symbol']}/USDT</b>\n"
            f"{rvol_line}"
            f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC | Альт 1h: {s['alt_1h']:+.2f}%\n"
            f"   Вход: <b>{entry:.6g}</b>\n"
            f"   Стоп: {stop:.6g} ({STOP_PCT}%)\n"
            f"   TP1:  {tp1:.6g} ({TP1_PCT:+}%) — 50%\n"
            f"   TP2:  {tp2:.6g} ({TP2_PCT:+}%) — 50%\n"
        )

    lines.append("⚠️ Проверь структуру и CVD. Решение за тобой.")
    send_telegram("\n".join(lines))
    print(f"[SCAN] Отправлено: {len(signals_rvol)} RVOL + {min(len(signals_cmc), TOP_N_SIGNALS - len(signals_rvol))} CMC")

# ─── СТАТУС ───────────────────────────────────────────────────────────────────

def send_status():
    now = datetime.now(MSK)
    status = (
        f"🤖 <b>Upscale Bot v4.1</b> | {now.strftime('%H:%M МСК')}\n"
        f"✅ Работает | Gate.io RVOL + CMC fallback\n"
        f"RVOL порог: {RVOL_THRESHOLD}x | Раскорр: {BTC_DECORR_THRESHOLD}%\n"
        f"Пар в скане: {len(UPSCALE_PAIRS)}\n"
        f"Торговые часы: {TRADING_START_MSK}:00 – {TRADING_END_MSK}:00 МСК"
    )
    send_telegram(status)

# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────

def main():
    # Пары которых нет на Gate.io — проверено вручную
    known_bad = ["BGB"]
    for sym in known_bad:
        if sym in UPSCALE_PAIRS:
            UPSCALE_PAIRS.remove(sym)

    send_telegram(
        "🚀 <b>Upscale Bot v4.2 запущен</b>\n"
        "🔄 CMC раскорреляция + Gate.io RVOL (EMA-20)\n"
        f"RVOL порог: {RVOL_THRESHOLD}x | Раскорр: {BTC_DECORR_THRESHOLD}%\n"
        f"Пар в скане: {len(UPSCALE_PAIRS)}\n"
        f"Торговые часы: {TRADING_START_MSK}:00–{TRADING_END_MSK}:00 МСК"
    )

    scan_count  = 0
    status_sent = -1

    while True:
        now_msk  = datetime.now(MSK)
        cur_hour = now_msk.hour

        if cur_hour != status_sent:
            send_status()
            status_sent = cur_hour

        if is_trading_hours():
            scan_count += 1
            print(f"\n[LOOP] Скан #{scan_count}")
            run_scan()
        else:
            print(f"[LOOP] Вне часов ({msk_time_str()}), пропуск")

        time.sleep(SCAN_INTERVAL_MIN * 60)

if __name__ == "__main__":
    main()
