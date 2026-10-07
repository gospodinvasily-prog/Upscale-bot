"""
bt_final2.py — DIP-HARVESTER: исправленная статистика (деньги, не среднее по дням).
Запуск: RUN_BACKTEST=final2   (env: FN2_DAYS=1825 FN2_OFFSET=0 FN2_QUEUE=5)

Метрика: m = Σ(net_i)/N — среднее НА ФИЛЛ; кластер-CI по дням на СУММЕ дня;
итог в $. Критерии (пре-рег, $-термины, 2.64s):
  ① Итог$ − CI > 0   ② худший день ≥ −$500   ③ MaxDD ≤ $2,000
  ④ нет года с итогом < −$500   ⑤ offset: Итог$ > 0   ⑥ queue 25: Итог$ > 0
Основная ячейка: d=1.0, KNIFE+REGIME — параметры НЕ трогаем.
"""
import os, time, statistics, traceback, math
from datetime import datetime, timezone, timedelta
import bot as B

DAYS   = int(os.environ.get("FN2_DAYS", "1825"))
OFFSET = int(os.environ.get("FN2_OFFSET", "0"))
QUEUE  = float(os.environ.get("FN2_QUEUE", "5"))
D_GRID = [float(x) for x in os.environ.get("FN2_D_GRID", "0.5,1.0,1.5").split(",")]
KNIFE_K= float(os.environ.get("FN2_KNIFE_K", "3.0"))
SMA_N  = int(os.environ.get("FN2_SMA_N", "30"))
SLOT   = float(os.environ.get("FN2_SLOT", "200"))
Z      = 2.64
MSK = timezone(timedelta(hours=3)); DAY = 86400
LIQ10 = ["BTC","ETH","SOL","XRP","BNB","ADA","DOGE","LINK","LTC","AVAX"]


def fetch_daily(sym):
    now = int(time.time()) - OFFSET * DAY
    out, cur = [], now - (DAYS + 45) * DAY
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1d",
                                         "from": cur, "to": min(now, cur + 1000 * DAY)})
        part = B.parse_candles(raw) if raw else []
        if not part: break
        out.extend(part)
        nxt = part[-1].get("t", 0) + DAY
        if nxt <= cur: break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"]); u.append(c)
    return u[:-1]


def money_stats(fills):
    """fills: [(date, net%)]. Деньги: Σ(net)×SLOT/100. CI: кластер по дням на СУММЕ дня."""
    by_d = {}
    for dt, v in fills:
        by_d[dt] = by_d.get(dt, 0.0) + v
    t = list(by_d.values())
    N, D = len(fills), len(t)
    tot = sum(t)                                   # %-пункты
    m = tot / N if N else 0.0
    mean_t = tot / D if D else 0.0
    var_tot = sum((x - mean_t) ** 2 for x in t) / (D - 1) * D if D > 1 else 0.0
    se_tot = math.sqrt(var_tot)                    # SE итога в %-пунктах
    tot_usd = tot * SLOT / 100
    ci_usd = Z * se_tot * SLOT / 100
    # портфель: худший день и MaxDD
    eq = peak = 0.0; dd = 0.0
    day_usd = {}
    for dt in sorted(by_d):
        day_usd[dt] = by_d[dt] * SLOT / 100
        eq += day_usd[dt]; peak = max(peak, eq); dd = min(dd, eq - peak)
    worst = min(day_usd.values()) if day_usd else 0.0
    return {"N": N, "days": D, "per_fill": m, "tot_usd": tot_usd, "ci_usd": ci_usd,
            "worst": worst, "dd": dd, "day_usd": day_usd}


def run():
    print(f"[FN2] {len(LIQ10)} пар × {DAYS} дн, queue {QUEUE:.0f} б.п., offset {OFFSET}")
    data = {s: fetch_daily(s) for s in LIQ10}
    arr = [(c["t"] // DAY, c["c"]) for c in data.get("BTC", [])]
    reg = {}
    for i in range(SMA_N, len(arr)):
        win = [p for _, p in arr[i - SMA_N:i]]
        reg[arr[i][0]] = arr[i - 1][1] > sum(win) / len(win)

    fills = {d: {"none": [], "knife": [], "full": []} for d in D_GRID}
    for sym in LIQ10:
        c = data.get(sym)
        if not c or len(c) < 60: continue
        for i in range(1, len(c) - 1):
            Dc, X = c[i], c[i + 1]
            o, l, cl = X["o"], X["l"], X["c"]
            if o <= 0 or l <= 0: continue
            dt = X["t"] // DAY
            retD = (Dc["c"] / c[i - 1]["c"] - 1) * 100 if c[i - 1]["c"] > 0 else 0.0
            for dv in D_GRID:
                limit = o * (1 - dv / 100)
                if l > limit * (1 - QUEUE / 10000): continue
                net = (cl / limit - 1) * 100 - 0.07 - 0.03
                fills[dv]["none"].append((dt, net))
                if retD > -KNIFE_K:
                    fills[dv]["knife"].append((dt, net))
                    if reg.get(dt, False):
                        fills[dv]["full"].append((dt, net))

    L = [f"🧺 <b>DIP-HARVESTER v2 — денежная статистика</b>: queue {QUEUE:.0f} б.п., "
         f"offset {OFFSET}" + (" ⏪" if OFFSET else ""),
         "<i>Итог$ = сумма по филлам; CI кластеризован по дням. Это ЭКОНОМИЧЕСКИ верная "
         "метрика (v1.0 считала среднее по дням — завышение в разы)</i>", ""]
    primary = None
    for dv in D_GRID:
        L.append(f"══ −{dv:.1f}% ══")
        for mode, lbl in (("none", "без фильтров"), ("knife", "KNIFE"), ("full", "KNIFE+REGIME")):
            fl = fills[dv][mode]
            if len(fl) < 100:
                L.append(f"  {lbl:14}: мало филлов"); continue
            st = money_stats(fl)
            L.append(f"  {lbl:14}: {st['N']} филлов / {st['days']} дн | "
                     f"на-филл {st['per_fill']:+.3f}% | <b>Итог ${st['tot_usd']:+,.0f} "
                     f"(±{st['ci_usd']:,.0f})</b> | худший день ${st['worst']:+,.0f} | "
                     f"MaxDD ${st['dd']:+,.0f}")
            if mode == "full" and abs(dv - 1.0) < 1e-9:
                primary = st
        L.append("")
    if primary:
        yr = {}
        for dt, v in fills[1.0]["full"]:
            y = str(datetime.fromtimestamp(dt * DAY, MSK).year)
            yr.setdefault(y, []).append(v * SLOT / 100)
        L.append("<b>Годы в $ (KNIFE+REGIME, −1.0%)</b>: " +
                 " | ".join(f"{y}: ${sum(v):+,.0f} ({len(v)})" for y, v in sorted(yr.items())))
        L.append("")
        ok1 = primary["tot_usd"] - primary["ci_usd"] > 0
        ok2 = primary["worst"] >= -500
        ok3 = primary["dd"] >= -2000
        ok4 = bool(yr) and all(sum(v) > -500 for v in yr.values())
        L.append(f"<b>КРИТЕРИИ ($)</b>: ① итог−CI>0: {'✅' if ok1 else '❌'}  "
                 f"② худший день: {'✅' if ok2 else '❌'}  ③ MaxDD: {'✅' if ok3 else '❌'}  "
                 f"④ годы: {'✅' if ok4 else '❌'}  ⑤ offset и ⑥ queue25 — отдельные прогоны")
    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", ""))
    try: B.send_blocks(msg.split("\n"))
    except Exception as e: print(f"[FN2] отправка: {e}")


def main():
    try: run()
    except Exception:
        traceback.print_exc()
        try: B.send_telegram(f"⚠️ final2 упал: {traceback.format_exc()[-400:]}")
        except Exception: pass

if __name__ == "__main__":
    main()