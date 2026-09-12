import logging
import requests
import time
from datetime import datetime
from collections import defaultdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8751822340:AAH-sKgtw58OUiUPnor5av_VeoIAMyWb5JA"
CHAT_ID = "426470592"
CMC_API_KEY = "b2e925cc66dc4dacacb1c3de4af26f35"

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
CMC_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"

UPSCALE_SYMBOLS = set([
    "ETH","BNB","XRP","SOL",
    "AAVE","ADA","AERO","ALGO","APT","ARB","ASTER","ATOM","AVAX","AXS",
    "BCH","BERA","BGB","BONK","BRETT","BSV",
    "CAKE","CHZ","CRO","CRV",
    "DASH","DATA","DEEP","DEXE","DOGE","DOT","DYDX",
    "EIGEN","ENA","ENS","ETC",
    "FARTCOIN","FET","FIL","FLOKI",
    "GALA","GRAM","GRASS","GRT",
    "HBAR","HYPE",
    "ICP","IMX","INJ","IOTA",
    "JASMY","JTO","JUP",
    "KAIA","KAITO","KAS",
    "LDO","LINEA","LINK","LTC",
    "MANA","MNT","MORPHO","MOVE",
    "NEAR","ONDO","OP","ORDI",
    "PENDLE","PENGU","PEPE","PNUT","POL","POPCAT","PUMP","PYTH",
    "QNT","RAY","RENDER","RUNE",
    "S","SAND","SEI","SHIB","SKY","STRK","STX","SUI",
    "TAO","TIA","TRUMP","TRX","TURBO",
    "UNI","VET","VIRTUAL",
    "WAL","WIF","WLD",
    "XLM","XMR","XTZ","ZEC","ZRO",
    "1000BONK","1000PEPE","1000SHIB"
])

START_HOUR = 4
END_HOUR = 22
MIN_SCANS_FOR_BASE = 4   # минимум 4 скана (1 час) для базы
RVOL_THRESHOLD = 2.0     # спайк = текущий прирост в 2x выше среднего

# Память
prev_volumes = {}                          # объём предыдущего скана
volume_deltas = defaultdict(list)          # история приростов за день
scan_count = 0
last_status_hour = -1

def send_tg(text):
    try:
        r = requests.post(TG_URL, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.json().get("ok"):
            logger.error(f"TG error: {r.json()}")
        else:
            logger.info("TG sent OK")
    except Exception as e:
        logger.error(f"TG error: {e}")

def get_msk_time():
    now = datetime.utcnow()
    msk_hour = (now.hour + 3) % 24
    return msk_hour, f"{msk_hour:02d}:{now.minute:02d}"

def is_trading_hours():
    msk_hour, _ = get_msk_time()
    return START_HOUR <= msk_hour < END_HOUR

def get_prices():
    try:
        headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
        params = {"limit": 500, "convert": "USD", "sort": "market_cap"}
        r = requests.get(CMC_URL, headers=headers, params=params, timeout=20)
        data = r.json()
        if data.get("status", {}).get("error_code") != 0:
            logger.error(f"CMC error: {data.get('status')}")
            return None
        prices = {}
        for coin in data["data"]:
            sym = coin["symbol"]
            if sym == "BTC" or sym in UPSCALE_SYMBOLS:
                try:
                    q = coin["quote"]["USD"]
                    prices[sym] = {
                        "price": q["price"],
                        "change_1h": q.get("percent_change_1h", 0) or 0,
                        "volume_24h": q.get("volume_24h", 0) or 0,
                    }
                except:
                    pass
        logger.info(f"Got {len(prices)} prices")
        return prices
    except Exception as e:
        logger.error(f"CMC error: {e}")
        return None

def fmt(p):
    if p >= 1000: return f"${p:,.0f}"
    if p >= 1: return f"${p:.3f}"
    if p >= 0.01: return f"${p:.5f}"
    return f"${p:.8f}"

def get_rvol(sym, cur_vol):
    """
    RVOL логика:
    1. Считаем прирост объёма за 15 минут (дельта)
    2. Сравниваем с средним приростом за день
    3. Если текущий прирост в 2x выше среднего — спайк
    """
    prev_vol = prev_volumes.get(sym, 0)
    if prev_vol <= 0 or cur_vol <= 0:
        return 0, False

    # Абсолютный прирост за 15 минут
    delta = cur_vol - prev_vol

    # Только положительный прирост имеет смысл
    if delta <= 0:
        return 0, False

    # Добавляем в историю
    volume_deltas[sym].append(delta)

    # Нужно минимум MIN_SCANS_FOR_BASE точек для надёжной базы
    if len(volume_deltas[sym]) < MIN_SCANS_FOR_BASE:
        return 0, False

    # Средний прирост за последние 20 сканов (или сколько есть)
    recent = volume_deltas[sym][-20:]
    avg_delta = sum(recent) / len(recent)

    if avg_delta <= 0:
        return 0, False

    # RVOL = текущий прирост / средний прирост
    rvol = delta / avg_delta

    spike = rvol >= RVOL_THRESHOLD
    return rvol, spike

def scan():
    global prev_volumes, scan_count, last_status_hour

    msk_hour, msk_time = get_msk_time()

    if not is_trading_hours():
        logger.info(f"Outside trading hours. MSK: {msk_time}")
        # Сброс в начале нового дня
        if msk_hour == START_HOUR:
            prev_volumes.clear()
            volume_deltas.clear()
            scan_count = 0
            last_status_hour = -1
            logger.info("New day — reset all")
        return

    data = get_prices()
    if not data or "BTC" not in data:
        logger.warning("No data")
        return

    scan_count += 1
    btc = data["BTC"]
    btc_change_1h = btc["change_1h"]
    btc_price = btc["price"]

    # Считаем раскорреляции для статуса
    decorr_count = sum(
        1 for sym, info in data.items()
        if sym != "BTC" and (info["change_1h"] - btc_change_1h) >= 1.5
    )

    # Статус раз в час
    if msk_hour != last_status_hour:
        btc_arrow = "⬇️" if btc_change_1h < 0 else "⬆️"
        base_ready = scan_count >= MIN_SCANS_FOR_BASE
        status_msg = (
            f"📡 <b>Статус {msk_time} МСК</b>\n\n"
            f"BTC: {fmt(btc_price)} | 1h: {btc_change_1h:+.2f}% {btc_arrow}\n"
            f"Сканирую: {len(data)-1} пар\n"
            f"Раскорреляций 1h: {decorr_count}\n"
            f"База RVOL: {'✅ готова' if base_ready else f'⏳ скан {scan_count}/{MIN_SCANS_FOR_BASE}'}\n\n"
            f"{'😴 Жду спайк объёма...' if base_ready else '⏳ Накапливаю базу...'}"
        )
        send_tg(status_msg)
        last_status_hour = msk_hour

    # Первый скан — только заполняем память объёмов
    if scan_count == 1:
        for sym, info in data.items():
            prev_volumes[sym] = info["volume_24h"]
        logger.info("First scan — volume memory filled")
        return

    # Нужно минимум 4 скана для базы
    if scan_count < MIN_SCANS_FOR_BASE:
        for sym, info in data.items():
            get_rvol(sym, info["volume_24h"])
            prev_volumes[sym] = info["volume_24h"]
        logger.info(f"Building base... scan {scan_count}/{MIN_SCANS_FOR_BASE}")
        return

    candidates = []
    for sym, info in data.items():
        if sym == "BTC":
            continue

        cur_vol = info["volume_24h"]
        rvol, spike = get_rvol(sym, cur_vol)

        # Раскорреляция по 1h
        diff_1h = info["change_1h"] - btc_change_1h

        # Оба условия: раскорреляция И спайк объёма
        if diff_1h >= 1.5 and spike:
            candidates.append({
                "sym": sym,
                "price": info["price"],
                "change_1h": info["change_1h"],
                "diff_1h": diff_1h,
                "rvol": rvol
            })

    # Обновляем память объёмов
    for sym, info in data.items():
        prev_volumes[sym] = info["volume_24h"]

    if not candidates:
        logger.info(f"No signals. Scan #{scan_count}, decorr: {decorr_count}, rvol_pairs: 0")
        return

    # Сортируем по RVOL * раскорреляция
    candidates.sort(key=lambda x: x["rvol"] * x["diff_1h"], reverse=True)
    top = candidates[:3]

    btc_arrow = "⬇️" if btc_change_1h < 0 else "⬆️"
    msg = f"🚨 <b>СИГНАЛ {msk_time} МСК</b>\n\n"
    msg += f"BTC: {fmt(btc_price)} | 1h: {btc_change_1h:+.2f}% {btc_arrow}\n"
    msg += f"{'─'*22}\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, c in enumerate(top):
        sig = "🔥" if c["diff_1h"] >= 3 else "⚡"
        price = c["price"]
        stop = price * 0.97
        tp1 = price * 1.05
        tp2 = price * 1.09

        msg += f"{medals[i]} <b>{c['sym']}/USDT</b> {sig}\n"
        msg += f"📊 RVOL: {c['rvol']:.1f}x (объём в {c['rvol']:.1f}x выше нормы)\n"
        msg += f"⚡ 1h: {c['change_1h']:+.2f}% | vs BTC: +{c['diff_1h']:.1f}%\n"
        msg += f"📍 Вход: {fmt(price)}\n"
        msg += f"🛑 Стоп: {fmt(stop)}\n"
        msg += f"🎯 TP1: {fmt(tp1)} | TP2: {fmt(tp2)}\n\n"

    msg += "💡 Входи в 1-2 лучших\nЕсли один против — выходишь, второй держишь"

    send_tg(msg)
    logger.info(f"Signals: {[(c['sym'], round(c['rvol'],1)) for c in top]}")

def main():
    logger.info("Bot v3.2 RVOL started!")
    send_tg(
        "🤖 <b>Upscale Signal Bot v3.2</b>\n\n"
        "✅ RVOL — реальный скачок объёма (2x от нормы)\n"
        "✅ Раскорреляция с BTC по 1h\n"
        "✅ Торговые часы: 04:00-22:00 МСК\n"
        "✅ Статус каждый час\n"
        f"✅ {len(UPSCALE_SYMBOLS)} пар USDT\n\n"
        "Накапливаю базу объёма (1 час)...\n"
        "Потом жду реальный спайк!"
    )
    while True:
        try:
            scan()
        except Exception as e:
            logger.error(f"Error: {e}")
        time.sleep(900)

if __name__ == "__main__":
    main()
