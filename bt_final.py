"""
bt_final.py v1.0 — DIP-HARVESTER: финальная конструкция (дневная торговля).

Запуск: RUN_BACKTEST=final   (env: FN_DAYS=1825 FN_OFFSET=0 FN_QUEUE=5)

Из 17 тестов: альфы после тейкер-издержек нет; пассивная премия ЕСТЬ
(лимитка ниже open -> NET +0.28..0.61%/филл, значимо); хвост — каскадные дни
(−15..20% капитала); вклад «сигнала после падения» ОТРИЦАТЕЛЕН; режим рынка
объяснял PnL всех прогонов. Финал: maker-дип-байинг + KNIFE + REGIME.

Сделка: интрадей. Филл в день X, выход close(X). Одна лимитка на пару в день.

Фильтры (без заглядывания — только данные до X-1):
  KNIFE:  день X-1 был <= -3% -> лимитку не ставим (дни-ножи хуже безусловных)
  REGIME: close BTC[X-1] > SMA30 closes[X-30..X-1] -> ставим, иначе нет

КРИТЕРИИ (пре-рег, 2.64s по дням, основная ячейка d=1.0 KNIFE+REGIME):
  1. NET на филл минус CI > +0.10%
  2. Худший портфельный день >= -5% капитала (слоты $200, капитал $10k)
  3. MaxDD кривой <= 20% капитала
  4. Нет года со средним NET < -0.50%
  5. Offset FN_OFFSET=365: NET > 0 и хвост в норме (отдельный прогон)
Пост-хок планка: пройдёт -> live-бот -> форвард 3-4 недели -> деньги. Не раньше.
"""
import os, time, statistics, traceback
from datetime import datetime, timezone, timedelta

import bot as B

DAYS    = int(os.environ.get("FN_DAYS", "1825"))
OFFSET  = int(os.environ.get("FN_OFFSET", "0"))
QUEUE   = float(os.environ.get("FN_QUEUE", "5"))          # прокол через лимит, б.п.
D_GRID  = [float(x) for x in os.environ.get("FN_D_GRID", "0.5,1.0,1.5").split(",")]
KNIFE_K = float(os.environ.get("FN_KNIFE_K", "3.0"))
SMA_N   = int(os.environ.get("FN_SMA_N", "30"))
SLOT    = float(os.environ.get("FN_SLOT", "200"))
CAPITAL = float(os.environ.get("FN_CAPITAL", "10000"))
COST_MK = 0.02 + 0.05                                     # maker вход + taker выход
FUND_D  = 0.03                                            # лонг платит (1 платёж в интрадее)
Z       = 2.64
MSK = timezone(timedelta(hours=3))
DAY = 86400
LIQ10 = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "LTC", "AVAX"]


def fetch_daily(sym):
    now = int(time.time()) - OFFSET * DAY
    out, cur = [], now - (DAYS + 45) * DAY
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1d",
                                         "from": cur, "to": min(now, cur + 1000 * DAY)})
        part = B.parse_candles(raw) if raw else []
        if not part:
            break
        out.extend(part)
        nxt = part[-1].get("t", 0) + DAY
        if nxt <= cur:
            break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"])
            u.append(c)
    return u[:-1]                     # незакрытая не нужна


def day_ci(pairs_list):
    by_d = {}
    for dt, v in pairs_list:
        by_d.setdefault(dt, []).append(v)
    dm = [sum(v) / len(v) for v in by_d.values()]
    if not dm:
        return None
    m = sum(dm) / len(dm)
    se = statistics.pstdev(dm) / len(dm) ** 0.5 if len(dm) > 1 else 0.0
    return sum(len(v) for v in by_d.values()), len(dm), m, Z * se


def portfolio(fills):
    """fills: [(trade_date, net%)] -> (стат, худший день $, MaxDD $)."""
    st = day_ci(fills)
    by_d = {}
    for dt, v in fills:
        by_d[dt] = by_d.get(dt, 0.0) + v / 100 * SLOT      # портфельный $ за ТОРГОВЫЙ день
    eq = peak = 0.0
    dd = 0.0
    for d in sorted(by_d):
        eq += by_d[d]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    worst = min(by_d.values()) if by_d else 0.0
    return st, worst, dd


def run():
    print(f"[FN] {len(LIQ10)} пар × {DAYS} дн, очередь {QUEUE:.0f} б.п., offset {OFFSET}")
    data = {sym: fetch_daily(sym) for sym in LIQ10}
    if len(data.get("BTC", [])) < SMA_N + 50:
        print("нет BTC-истории")
        return

    # REGIME: решение на день X по данным до X-1 ВКЛЮЧИТЕЛЬНО (без заглядывания)
    arr = [(c["t"] // DAY, c["c"]) for c in data["BTC"]]
    reg = {}
    for i in range(SMA_N, len(arr)):
        win = [p for _, p in arr[i - SMA_N:i]]            # SMA_N закрытий до X-1
        reg[arr[i][0]] = arr[i - 1][1] > sum(win) / len(win)

    modes = ("none", "knife", "full")
    fills = {d: {m: [] for m in modes} for d in D_GRID}

    for sym in LIQ10:
        c = data.get(sym)
        if not c or len(c) < 60:
            continue
        for i in range(1, len(c) - 1):
            D, X = c[i], c[i + 1]
            o, l, cl = X["o"], X["l"], X["c"]
            if o <= 0 or l <= 0:
                continue
            dt = X["t"] // DAY                            # ТОРГОВЫЙ день (не сигнальный)
            retD = (D["c"] / c[i - 1]["c"] - 1) * 100 if c[i - 1]["c"] > 0 else 0.0
            for dv in D_GRID:
                limit = o * (1 - dv / 100)
                if l > limit * (1 - QUEUE / 10000):       # прокол сквозь лимит = филл
                    continue
                net = (cl / limit - 1) * 100 - COST_MK - FUND_D
                fills[dv]["none"].append((dt, net))
                if retD > -KNIFE_K:                       # KNIFE: вчера не был ножом
                    fills[dv]["knife"].append((dt, net))
                    if reg.get(dt, False):                # REGIME: BTC выше SMA30
                        fills[dv]["full"].append((dt, net))

    L = [f"🧺 <b>DIP-HARVESTER v1.0 — финальная конструкция</b>: {len(LIQ10)} пар, ~{DAYS} дн, "
         f"слоты ${SLOT:.0f}, капитал ${CAPITAL:,.0f}"
         + (f"\n⏪ СДВИНУТ НА {OFFSET} ДН" if OFFSET else ""),
         f"<i>лимитка −d% ниже open, выход close того же дня (интрадей) | очередь {QUEUE:.0f} б.п., "
         f"издержки {COST_MK:.2f}%+фандинг | KNIFE: после дня ≤−{KNIFE_K:.0f}% не ставим | "
         f"REGIME: BTC выше SMA{SMA_N}</i>",
         "<i>ПОСТ-ХОК: планка повышена, live-бот + форвард 3-4 недели обязательны до денег</i>", ""]

    primary = None
    for dv in D_GRID:
        L.append(f"══ глубина −{dv:.1f}% ══")
        for m, lbl in (("none", "без фильтров"), ("knife", "KNIFE"),
                       ("full", "KNIFE+REGIME")):
            fl = fills[dv][m]
            if len(fl) < 100:
                L.append(f"  {lbl:14}: мало филлов ({len(fl)})")
                continue
            st, worst, dd = portfolio(fl)
            n, nd_, mean, ci = st
            L.append(f"  {lbl:14}: {n} филлов / {nd_} дн ({n / nd_:.1f}/день), "
                     f"NET {mean:+.3f}% (±{ci:.3f}) | худший день ${worst:+,.0f} | "
                     f"MaxDD ${dd:+,.0f}")
            if m == "full" and abs(dv - 1.0) < 1e-9:
                primary = (mean, ci, worst, dd)
        L.append("")

    yf = {}
    for dt, v in fills.get(1.0, {}).get("full", []):
        y = str(datetime.fromtimestamp(dt * DAY, MSK).year)
        yf.setdefault(y, []).append(v)
    if len(yf) >= 3:
        L.append("<b>Годы (KNIFE+REGIME, −1.0%)</b>: " +
                 " | ".join(f"{y}: {sum(v) / len(v):+.2f}% ({len(v)})"
                            for y, v in sorted(yf.items())))
        L.append("")

    if primary:
        mean, ci, worst, dd = primary
        ok1 = mean - ci > 0.10
        ok2 = worst >= -0.05 * CAPITAL
        ok3 = dd >= -0.20 * CAPITAL
        ok4 = bool(yf) and all(sum(v) / len(v) > -0.50 for v in yf.values())
        L.append("<b>КРИТЕРИИ (пре-рег: d=1.0, KNIFE+REGIME)</b>")
        L.append(f"  ① NET−CI > +0.10%: {'✅' if ok1 else '❌'} ({mean:+.3f} ±{ci:.3f})")
        L.append(f"  ② худший день ≥ −5% капитала: {'✅' if ok2 else '❌'} (${worst:+,.0f})")
        L.append(f"  ③ MaxDD ≤ 20% капитала: {'✅' if ok3 else '❌'} (${dd:+,.0f})")
        L.append(f"  ④ нет года хуже −0.50%: {'✅' if ok4 else '❌'}")
        L.append("  ⑤ offset FN_OFFSET=365 — отдельный прогон")
        allok = ok1 and ok2 and ok3 and ok4
        L.append("")
        L.append("  → <b>" + ("ВСЕ КРИТЕРИИ ✅ — offset, затем live-бот + форвард 3-4 недели"
                             if allok else
                             "КРИТЕРИИ НЕ ВЫПОЛНЕНЫ — в этой форме не деплоится") + "</b>")
    L += ["", "<i>если ②/③ провалены хвостами: НЕ крутить параметры после просмотра — "
              "единственный честный рычаг, зафиксированный заранее, это размер слота "
              "(FN_SLOT) и длина SMA (FN_SMA_N), менять можно только ДО прогона. "
              "Реальные заливки хуже модели: очередь, частичные филлы, гэпы — филлов/день "
              "и NET в форварде будет ниже. Это условие деплоя, не опция</i>"]

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[FN] отправка: {e}")


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ final упал: {traceback.format_exc()[-400:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()