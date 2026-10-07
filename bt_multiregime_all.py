"""
bt_multiregime_all.py v1.1 — MULTI-REGIME на всём пуле (103 пары).

Запуск: RUN_BACKTEST=multiregime_all   (env: MRA_DAYS=1095 MRA_OFFSET=0)

Отличия от bt_multiregime.py (10 пар):
  - универсум: B.UPSCALE_PAIRS (103), фильтр ликвидности ≥ $20M/день (фикс)
  - слот $60/пара (экспозиция ≤62% капитала при всех лонгах)
  - издержки по группе: BTC/ETH slip 0.03%, остальные 0.08% за сторону
    (измерено в серии: альты дороже); fee 0.05%/сторона + фандинг 0.03%/день
  - пары с короткой историей включаются с момента появления данных
Окна фиксированы как в multiregime: SLOW SMA(90) 4h / FAST SMA(45) 4h.

v1.1 фиксы:
  - sim_pair: дата закрытия бралась как c["t"] (список вместо элемента) → исправлено
  - fetch: 4h свечи, Gate.io даёт ~10000 баров → максимум ~1667 дн, реально
    данные есть ~1095 дн (3 года) по большинству пар; MRA_DAYS=1095 по умолчанию

КРИТЕРИИ (пре-рег, 2.64s, SLOW основное):
  1) Итог$ − CI$ > 0
  2) согласованность: итог SLOW>0 ИЛИ FAST>0
  3) MaxDD ≤ 25% капитала
  4) MaxDD системы ≤ 60% MaxDD B&H того же пула
  5) MRA_OFFSET=365 — знак
"""
import os, time, math, statistics, traceback
from datetime import datetime, timezone, timedelta
import bot as B

DAYS   = int(os.environ.get("MRA_DAYS", "1095"))
OFFSET = int(os.environ.get("MRA_OFFSET", "0"))
TF     = os.environ.get("MRA_TF", "4h")
TF_SEC = 14400
BARS_D = 6
WINDOWS = {"SLOW": 90, "FAST": 45}
LIQ_MIN = float(os.environ.get("MRA_LIQ_MIN", "20")) * 1e6
SLOT    = float(os.environ.get("MRA_SLOT", "60"))
CAP     = float(os.environ.get("MRA_CAP", "10000"))
FEE_SIDE = 0.05
SLIP_BLUE = float(os.environ.get("MRA_SLIP_B", "0.03"))
SLIP_ALT  = float(os.environ.get("MRA_SLIP_A", "0.08"))
FUND_D  = 0.03
Z = 2.64
MSK = timezone(timedelta(hours=3)); DAY = 86400
BLUE = {"BTC", "ETH"}

GROUPS = {
    "bluechip": {"BTC", "ETH", "BNB", "SOL", "XRP"},
    "midcap": {"LINK","AVAX","NEAR","ARB","OP","INJ","SUI","APT","TIA","ATOM","AAVE",
               "UNI","LTC","DOT","ADA","ICP","HBAR","FIL","ETC","BCH","VET","LDO",
               "ONDO","PENDLE","MNT","TAO","RENDER","SEI","JUP","JTO","PYTH","RAY",
               "VIRTUAL","GRASS","KAITO","EIGEN","WLD","ZRO","QNT","GRT","IMX",
               "RUNE","STX","ORDI","DYDX","CRV","CAKE","SKY","MORPHO","STRK","MOVE",
               "LINEA","GRAM","BERA","IOTA","KAIA","DEEP","0G","DATA"},
    "meme": {"DOGE","PEPE","SHIB","FLOKI","BONK","WIF","BRETT","FARTCOIN","TURBO",
             "PNUT","POPCAT","TRUMP","PENGU","PUMP"},
}

def group_of(sym):
    for g, s in GROUPS.items():
        if sym in s:
            return g
    return "altcoin"

def slip_of(sym):
    return SLIP_BLUE if sym in BLUE else SLIP_ALT


def fetch(sym):
    now = int(time.time()) - OFFSET * DAY
    out, cur = [], now - (DAYS + 60) * DAY
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": TF,
                                         "from": cur, "to": min(now, cur + 1000 * TF_SEC)})
        part = B.parse_candles(raw) if raw else []
        if not part:
            break
        out.extend(part)
        nxt = part[-1].get("t", 0) + TF_SEC
        if nxt <= cur:
            break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"]); u.append(c)
    return u[:-1]


def sim_pair(sym, candles, n_sma):
    """Простой трендовый алго: лонг пока close > SMA(n_sma), вход/выход по open следующей свечи."""
    closes = [x["c"] for x in candles]
    slip   = slip_of(sym)
    cost_rt = (FEE_SIDE + slip) * 2   # % круг
    trades  = []
    in_pos  = False
    entry   = 0.0
    entry_i = 0
    for i in range(n_sma, len(candles) - 1):
        sma  = sum(closes[i - n_sma:i]) / n_sma
        want = closes[i] > sma
        nxt_open = candles[i + 1]["o"]
        if want and not in_pos and nxt_open > 0:
            entry   = nxt_open
            in_pos  = True
            entry_i = i + 1
        elif (not want) and in_pos:
            pnl  = (nxt_open / entry - 1) * 100 - cost_rt
            held = (i + 1 - entry_i) * TF_SEC / DAY
            pnl -= FUND_D * held
            dts  = datetime.fromtimestamp(candles[i + 1]["t"], MSK).strftime("%Y-%m-%d")
            trades.append((dts, pnl / 100 * SLOT))
            in_pos = False
    # незакрытая позиция — закрываем по последней свече
    if in_pos:
        pnl  = (closes[-1] / entry - 1) * 100 - cost_rt
        held = (len(candles) - 1 - entry_i) * TF_SEC / DAY
        pnl -= FUND_D * held
        dts  = datetime.fromtimestamp(candles[-1]["t"], MSK).strftime("%Y-%m-%d")
        trades.append((dts, pnl / 100 * SLOT))
    return trades


def money(tr):
    if not tr:
        return None
    by_d = {}
    for d, v in tr:
        by_d[d] = by_d.get(d, 0.0) + v
    tot  = sum(v for _, v in tr)
    sums = list(by_d.values())
    se   = statistics.pstdev(sums) if len(sums) > 1 else 0.0
    ci   = Z * se * math.sqrt(len(sums))
    eq   = pk = 0.0; dd = 0.0
    for d in sorted(by_d):
        eq += by_d[d]; pk = max(pk, eq); dd = min(dd, eq - pk)
    return {"tot": tot, "ci": ci, "dd": dd, "worst": min(sums), "n": len(tr),
            "days": len(sums)}


def bh_maxdd(candles):
    """MaxDD стратегии Buy&Hold (слот $SLOT, вход в первую открытую свечу)."""
    eq = pk = 0.0; dd = 0.0
    for i in range(1, len(candles)):
        if candles[i - 1]["o"] > 0:
            r = (candles[i]["o"] / candles[i - 1]["o"] - 1) * SLOT
        else:
            r = 0.0
        eq += r; pk = max(pk, eq); dd = min(dd, eq - pk)
    return dd


def run():
    print(f"[MRA] {TF} × {DAYS} дн, offset {OFFSET}, весь пул, слот ${SLOT:.0f}")
    data = {}
    for sym in B.UPSCALE_PAIRS:
        try:
            c = fetch(sym)
        except Exception as e:
            print(f"[MRA] {sym}: ошибка fetch — {e}")
            continue
        if len(c) < WINDOWS["SLOW"] + 100:
            continue
        # ликвидность: средний дневной объём за 14 дней
        v14 = sum(x.get("v", 0) * x["c"] for x in c[-14 * BARS_D:]) / 14
        if v14 < LIQ_MIN:
            continue
        data[sym] = c
    print(f"[MRA] пар прошло фильтр ликвидности: {len(data)}")
    if len(data) < 30:
        print("[MRA] мало пар, выход")
        return

    L = [f"🧭 <b>MULTI-REGIME ALL: пер-парный тренд на {TF}</b> — {len(data)} пар, "
         f"~{DAYS} дн, слот ${SLOT:.0f}, капитал ${CAP:,.0f}"
         + (f"\n⏪ СДВИНУТ НА {OFFSET} ДН" if OFFSET else ""),
         f"<i>slip: BTC/ETH {SLIP_BLUE}%, альты {SLIP_ALT}% за сторону | "
         f"издержки {(FEE_SIDE + SLIP_ALT) * 2:.2f}% круг на альты</i>", ""]

    results = {}
    for wn, n_sma in WINDOWS.items():
        all_tr, per_pair, by_group = [], {}, {}
        for sym, c in data.items():
            tr = sim_pair(sym, c, n_sma)
            all_tr.extend(tr)
            per_pair[sym] = money(tr)
            g   = group_of(sym)
            by_group.setdefault(g, []).extend(tr)
        st     = money(all_tr)
        bh_dd  = min(bh_maxdd(c) for c in data.values())   # худший B&H по пулу
        results[wn] = (st, per_pair, by_group, bh_dd)
        if not st:
            continue
        L.append(f"══ <b>{wn}: SMA({n_sma}) ≈ {n_sma / BARS_D:.1f} дней</b> ══")
        L.append(f"  {st['n']} сделок / {st['days']} дн-пар | "
                 f"<b>Итог ${st['tot']:+,.0f} (±{st['ci']:,.0f})</b> | "
                 f"худший день ${st['worst']:+,.0f} | MaxDD ${st['dd']:+,.0f}")
        L.append("  группы: " + " | ".join(
            f"{g}: ${money(tr)['tot']:+,.0f} ({len(tr)} сд)" if money(tr) else f"{g}: нет"
            for g, tr in sorted(by_group.items())))
        top = sorted(((s, p) for s, p in per_pair.items() if p), key=lambda kv: -kv[1]["tot"])
        L.append("  топ-5: " + " | ".join(f"{s} ${p['tot']:+,.0f}" for s, p in top[:5]))
        L.append("  аутсайдеры: " + " | ".join(f"{s} ${p['tot']:+,.0f}" for s, p in top[-5:]))
        dead = [s for s, p in per_pair.items() if p and p["tot"] < -150]
        L.append(f"  мёртвые (<−$150): {len(dead)} шт"
                 + (f": {', '.join(dead[:8])}" if dead else ""))
        L.append("")

    if "SLOW" in results and results["SLOW"][0]:
        st, per_pair, by_group, bh_dd = results["SLOW"]
        fast_st = results.get("FAST", (None,))[0]
        ok1 = st["tot"] - st["ci"] > 0
        ok2 = (st["tot"] > 0) or bool(fast_st and fast_st["tot"] > 0)
        ok3 = st["dd"] >= -0.25 * CAP
        ok4 = (bh_dd == 0) or (st["dd"] >= 0.6 * bh_dd)
        L.append(f"<b>КРИТЕРИИ (SLOW, {len(data)} пар)</b>: "
                 f"① {'✅' if ok1 else '❌'} "
                 f"② {'✅' if ok2 else '❌'} "
                 f"③ {'✅' if ok3 else '❌'} "
                 f"④ {'✅' if ok4 else '❌'} | ⑤ offset — отдельно")
        L.append("  → " + ("все ✅: MRA_OFFSET=365 → форвард" if (ok1 and ok2 and ok3 and ok4)
                           else "провал по пре-регу — закрываем ветку"))

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[MRA] отправка: {e}")


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ multiregime_all упал: {traceback.format_exc()[-400:]}")
        except Exception:
            pass

if __name__ == "__main__":
    main()
