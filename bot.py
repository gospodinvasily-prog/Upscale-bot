import logging
import requests
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8751822340:AAH-sKgtw58OUiUPnor5av_VeoIAMyWb5JA"
CHAT_ID = "426470592"
CMC_API_KEY = "b2e925cc66dc4dacacb1c3de4af26f35"

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
CMC_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"

UPSCALE_SYMBOLS = [
    "ETH","BNB","XRP","SOL","AAVE","ADA","ALGO","APT","ARB","ATOM",
    "AVAX","AXS","BCH","CRV","DASH","DOGE","DOT","ENA","ETC","FET",
    "FIL","GRT","HBAR","HYPE","ICP","INJ","IOTA","JASMY","JTO","JUP",
    "KAIA","KAS","LDO","LINK","LTC","MANA","MNT","NEAR","ONDO","OP",
    "PENDLE","PENGU","PNUT","POL","RAY","RENDER","RUNE","SAND","SEI",
    "STRK","STX","SUI","TAO","TIA","TRUMP","TRX","UNI","VET","WIF",
    "WLD","XLM","XMR","XTZ","ZEC","SHIB","BONK","PEPE","TURBO","MOVE"
]

def send_tg(text):
    try:
        r = requests.post(TG_URL, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        result = r.json()
        if not result.get("ok"):
            logger.error(f"TG error: {result}")
        else:
            logger.info("Message sent OK")
    except Exception as e:
        logger.error(f"TG error: {e}")

def get_prices():
    try:
        headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
        params = {
            "limit": 200,
            "convert": "USD",
            "sort": "market_cap"
        }
        r = requests.get(CMC_URL, headers=headers, params=params, timeout=15)
        data = r.json()

        if data.get("status", {}).get("error_code") != 0:
            logger.error(f"CMC error: {data.get('status')}")
            return None

        prices = {}
        for coin in data["data"]:
            sym = coin["symbol"]
            try:
                change = coin["quote"]["USD"]["percent_change_24h"]
                price = coin["quote"]["USD"]["price"]
                volume = coin["quote"]["USD"]["volume_24h"]
                if sym == "BTC" or sym in UPSCALE_SYMBOLS:
                    prices[sym] = {
                        "price": price,
                        "change": change,
                        "volume": volume
                    }
            except:
                pass

        logger.info(f"Got {len(prices)} prices from CMC")
        return prices

    except Exception as e:
        logger.error(f"CMC error: {e}")
        return None

def fmt(p):
    if p >= 1000: return f"${p:,.0f}"
    if p >= 1: return f"${p:.3f}"
    if p >= 0.01: return f"${p:.5f}"
    return f"${p:.8f}"

def scan():
    data = get_prices()
    if not data or "BTC" not in data:
        logger.warning("No data received")
        return

    btc_change = data["BTC"]["change"]
    btc_price = data["BTC"]["price"]

    candidates = []
    for sym, info in data.items():
        if sym == "BTC":
            continue
        diff = info["change"] - btc_change
        if diff >= 1.5:
            candidates.append({
                "sym": sym,
                "price": info["price"],
                "change": info["change"],
                "volume": info["volume"],
                "diff": diff
            })

    candidates.sort(key=lambda x: x["diff"], reverse=True)
    top = candidates[:3]

    if not top:
        logger.info("No candidates found")
        return

    now = datetime.now().strftime("%H:%M")
    btc_arrow = "⬇️" if btc_change < 0 else "⬆️"
    msg = f"📊 <b>СКАН {now} МСК</b>\n\nBTC: {fmt(btc_price)} | {btc_change:+.2f}% {btc_arrow}\n{'─'*22}\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, c in enumerate(top):
        price = c["price"]
        stop = price * 0.96
        tp1 = price * 1.06
        tp2 = price * 1.10
        sig = "🔥" if c["diff"] >= 4 else "⚡" if c["diff"] >= 2.5 else "👀"

        msg += f"{medals[i]} <b>{c['sym']}/USD</b> {sig} +{c['diff']:.1f}% vs BTC\n"
        msg += f"📍 Вход: {fmt(price)}\n"
        msg += f"🛑 Стоп: {fmt(stop)}\n"
        msg += f"🎯 TP1: {fmt(tp1)} | TP2: {fmt(tp2)}\n\n"

    msg += "💡 Входи в 1-2 лучших\nЕсли один против — выходишь, второй держишь"

    send_tg(msg)
    logger.info(f"Signals sent: {[c['sym'] for c in top]}")

def main():
    logger.info("Upscale Signal Bot started!")
    send_tg("🤖 <b>Upscale Signal Bot запущен!</b>\nСканирую каждые 15 минут...\nИсточник: CoinMarketCap API ✅")

    while True:
        try:
            logger.info("Running scan...")
            scan()
        except Exception as e:
            logger.error(f"Scan error: {e}")
        time.sleep(900)

if __name__ == "__main__":
    main()
