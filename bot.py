import asyncio
import logging
import requests
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8751822340:AAH-sKgtw58OUiUPnor5av_VeoIAMyWb5JA"
CHAT_ID = "426470592"

UPSCALE_PAIRS = [
    "ethereum","binancecoin","ripple","solana","aave","cardano",
    "algorand","aptos","arbitrum","cosmos","avalanche-2","axie-infinity",
    "bitcoin-cash","curve-dao-token","dash","dogecoin","polkadot",
    "ethena","ethereum-classic","fetch-ai","filecoin","the-graph",
    "hedera-hashgraph","hyperliquid","internet-computer","injective-protocol",
    "iota","jasmycoin","jito-governance-token","jupiter-exchange-solana",
    "kaia","kaspa","lido-dao","chainlink","litecoin","decentraland",
    "mantle","near","ondo-finance","optimism","pendle",
    "pudgy-penguins","peanut-the-squirrel","matic-network","raydium",
    "render-token","thorchain","the-sandbox","sei-network","starknet",
    "blockstack","sui","bittensor","celestia","trump","tron",
    "uniswap","vechain","dogwifcoin","worldcoin-wld","stellar",
    "monero","tezos","zcash","shiba-inu","bonk","pepe",
    "turbo","grass","morpho","movement"
]

SYMBOLS = {
    "ethereum":"ETH","binancecoin":"BNB","ripple":"XRP","solana":"SOL",
    "aave":"AAVE","cardano":"ADA","algorand":"ALGO","aptos":"APT",
    "arbitrum":"ARB","cosmos":"ATOM","avalanche-2":"AVAX","axie-infinity":"AXS",
    "bitcoin-cash":"BCH","curve-dao-token":"CRV","dash":"DASH","dogecoin":"DOGE",
    "polkadot":"DOT","ethena":"ENA","ethereum-classic":"ETC","fetch-ai":"FET",
    "filecoin":"FIL","the-graph":"GRT","hedera-hashgraph":"HBAR",
    "hyperliquid":"HYPE","internet-computer":"ICP","injective-protocol":"INJ",
    "iota":"IOTA","jasmycoin":"JASMY","jito-governance-token":"JTO",
    "jupiter-exchange-solana":"JUP","kaia":"KAIA","kaspa":"KAS",
    "lido-dao":"LDO","chainlink":"LINK","litecoin":"LTC","decentraland":"MANA",
    "mantle":"MNT","near":"NEAR","ondo-finance":"ONDO","optimism":"OP",
    "pendle":"PENDLE","pudgy-penguins":"PENGU","peanut-the-squirrel":"PNUT",
    "matic-network":"POL","raydium":"RAY","render-token":"RENDER",
    "thorchain":"RUNE","the-sandbox":"SAND","sei-network":"SEI",
    "starknet":"STRK","blockstack":"STX","sui":"SUI","bittensor":"TAO",
    "celestia":"TIA","trump":"TRUMP","tron":"TRX","uniswap":"UNI",
    "vechain":"VET","dogwifcoin":"WIF","worldcoin-wld":"WLD","stellar":"XLM",
    "monero":"XMR","tezos":"XTZ","zcash":"ZEC","shiba-inu":"SHIB",
    "bonk":"BONK","pepe":"PEPE","turbo":"TURBO","grass":"GRASS",
    "morpho":"MORPHO","movement":"MOVE"
}

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

def send_tg(text):
    try:
        r = requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        result = r.json()
        if not result.get("ok"):
            logger.error(f"TG error: {result}")
    except Exception as e:
        logger.error(f"TG error: {e}")

def get_prices():
    try:
        ids = "bitcoin," + ",".join(UPSCALE_PAIRS)
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"}
        r = requests.get(url, params=params, timeout=20)
        return r.json()
    except Exception as e:
        logger.error(f"Price error: {e}")
        return None

def fmt(p):
    if p >= 1000: return f"${p:,.0f}"
    if p >= 1: return f"${p:.3f}"
    if p >= 0.01: return f"${p:.5f}"
    return f"${p:.8f}"

def scan():
    data = get_prices()
    if not data or "bitcoin" not in data:
        logger.warning("No data from CoinGecko")
        return

    btc_change = data["bitcoin"].get("usd_24h_change", 0)
    btc_price = data["bitcoin"]["usd"]

    candidates = []
    for coin_id in UPSCALE_PAIRS:
        if coin_id not in data:
            continue
        change = data[coin_id].get("usd_24h_change", 0)
        price = data[coin_id]["usd"]
        diff = change - btc_change
        if diff >= 1.5:
            candidates.append({
                "sym": SYMBOLS.get(coin_id, coin_id.upper()),
                "price": price,
                "change": change,
                "diff": diff
            })

    candidates.sort(key=lambda x: x["diff"], reverse=True)
    top = candidates[:3]

    if not top:
        logger.info("No candidates found")
        return

    signals = []
    for c in top:
        price = c["price"]
        stop = price * 0.96
        tp1 = price * 1.06
        tp2 = price * 1.10
        sig = "🔥" if c["diff"] >= 4 else "⚡" if c["diff"] >= 2.5 else "👀"
        signals.append({**c, "stop": stop, "tp1": tp1, "tp2": tp2, "sig": sig})

    now = datetime.now().strftime("%H:%M")
    btc_arrow = "⬇️" if btc_change < 0 else "⬆️"
    msg = f"📊 <b>СКАН {now} МСК</b>\n\nBTC: {fmt(btc_price)} | {btc_change:+.2f}% {btc_arrow}\n{'─'*22}\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, s in enumerate(signals):
        msg += f"{medals[i]} <b>{s['sym']}/USD</b> {s['sig']} +{s['diff']:.1f}% vs BTC\n"
        msg += f"📍 Вход: {fmt(s['price'])}\n"
        msg += f"🛑 Стоп: {fmt(s['stop'])}\n"
        msg += f"🎯 TP1: {fmt(s['tp1'])} | TP2: {fmt(s['tp2'])}\n\n"
    msg += "💡 Входи в 1-2 лучших\nЕсли один против — выходишь, второй держишь"

    send_tg(msg)
    logger.info(f"Signal sent: {[s['sym'] for s in signals]}")

def main():
    logger.info("Upscale Signal Bot started!")
    send_tg("🤖 <b>Upscale Signal Bot запущен!</b>\nСканирую каждые 15 минут...")
    while True:
        try:
            scan()
        except Exception as e:
            logger.error(f"Error: {e}")
        import time
        time.sleep(900)

if __name__ == "__main__":
    main()
