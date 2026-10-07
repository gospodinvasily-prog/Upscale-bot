"""
bt_draft.py v1.2 — DRAFT: Day Range Accumulation & Fade Trade.

Запуск: RUN_BACKTEST=draft   (env: DR_DAYS=365 DR_OFFSET=0 DR_QUEUE=10)

МЕХАНИКА:
  Рамка дня: high/low 00:00-18:00 UTC (18 часов).
  В 18:00: высота рамки в [1.5%, 6.0%] -> рабочий день.
  REGIME: BTC close(X-1) > SMA30 -> лонг-нога, иначе шорт-нога.
  Лонг: лимитка BUY на low*(1-0.25%), стоп = low*(1-1%), TP = середина рамки.
  Шорт: лимитка SELL на high*(1+0.25%), стоп = high*(1+1%), TP = середина рамки.
  Фильтр KNIFE: день X-1 пары <= -3% -> пропуск.
  Выход: TP / стоп / принудительно close дня.

КРИТЕРИИ (пре-рег, Z=2.64):
  ① Итог$ − CI$ > 0
  ② худший день >= -$500
  ③ MaxDD <= $2,000
  ④ нет года с итогом < -$500
  ⑤ offset DR_OFFSET=365: знак совпал
  ⑥ DR_QUEUE=25: Итог$ > 0
Провал ① -> архив ветки без разговоров.
"""
import os, time, math, statistics, traceback
from datetime import datetime, timezone, timedelta
import bot as B

DAYS     = int(os.environ.get("DR_DAYS",   "365"))
OFFSET   = int(os.environ.get("DR_OFFSET", "0"))
QUEUE    = float(os.environ.get("DR_QUEUE", "10"))
FRAME_LO, FRAME_HI = 1.5, 6.0
DIP      = 0.25       # % за границу рамки
STOP_BUF = 1.0        # % от своей границы рамки
KNIFE_K  = 3.0
SMA_N    = 30
SLOT     = float(os.environ.get("DR_SLOT", "200"))
FEE_MK, FEE_TK, STOP_SLIP, FUND_D = 0.02, 0.05, 0.10, 0.03
Z        = 2.64
MSK = timezone(timedelta(hours=3)); DAY = 86400; HOUR = 3600
UTC18 = 18 * HOUR
LIQ10 = ["BTC","ETH","SOL","XRP","BNB","ADA","DOGE","LINK","LTC","AVAX"]
MAX_1H_DAYS = 400   # Gate.io: max ~10000 свечей 1h
BATCH = 999


def fetch_1h(sym):
    """Грузим постранично от сейчас назад через limit+to (from/to даёт 400 на глубоких датах)."""
    now = int(time.time()) - OFFSET * DAY
    limit_ts = now - min(DAYS + 5, MAX_1H_DAYS) * DAY
    out = []
    to_ts = now
    while to_ts > limit_ts:
        raw = B.api_get("candlesticks", {
            "contract": f"{sym}_USDT", "interval": "1h",
            "to": to_ts, "limit": BATCH
        })
        part = B.parse_candles(raw) if isinstance(raw, list) and raw else []
        if not part:
            break
        out.extend(part)
        earliest = part[0].get("t", to_ts)
        if earliest >= to_ts:
            break
        to_ts = earliest - 1
        if to_ts <= limit_ts:
            break
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"]); u.append(c)
    u = [c for c in u if c["t"] >= limit_ts]
    return u[:-1] if len(u) > 1 else u


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


def run():
    effective = min(DAYS, MAX_1H_DAYS - 5)
    print(f"[DR] {len(LIQ10)} пар × {effective} дн, queue {QUEUE:.0f}, offset {OFFSET}")

    # BTC режим: SMA30 по дневным
    btc_d = fetch_daily("BTC")
    closes = [c["c"] for c in btc_d]
    keys = [c["t"] // DAY for c in btc_d]
    reg_day = {}
    for k in range(SMA_N, len(btc_d)):
        reg_day[keys[k]] = closes[k-1] > sum(closes[k-SMA_N:k]) / SMA_N

    fills = []
    for sym in LIQ10:
        h1 = fetch_1h(sym)
        print(f"[DR] {sym}: {len(h1)} свечей 1h")
        if len(h1) < effective * 24 * 0.3:
            print(f"[DR] {sym}: мало данных, пропуск")
            continue
        by_day = {}
        for c in h1:
            by_day.setdefault(c["t"] // DAY, []).append(c)
        ds = sorted(by_day)
        for k in range(1, len(ds)):
            Xd, Pd = ds[k], ds[k-1]
            bars, prev = by_day[Xd], by_day[Pd]
            if len(bars) < 22 or len(prev) < 20 or prev[0]["o"] <= 0:
                continue
            if (prev[-1]["c"] / prev[0]["o"] - 1) * 100 <= -KNIFE_K:
                continue
            side = "long" if reg_day.get(Xd, True) else "short"
            frame = [c for c in bars if (c["t"] % DAY) < UTC18]
            if len(frame) < 16:
                continue
            fh = max(c["h"] for c in frame)
            fl = min(c["l"] for c in frame)
            height = (fh - fl) / fl * 100
            if not (FRAME_LO <= height <= FRAME_HI) or fl <= 0:
                continue
            after = [c for c in bars if (c["t"] % DAY) >= UTC18]
            if not after:
                continue
            qb = QUEUE / 10000
            if side == "long":
                lvl  = fl * (1 - DIP / 100)
                stop = fl * (1 - STOP_BUF / 100)   # ниже fl на 1%
                tp   = (fh + fl) / 2
                if not any(c["l"] <= lvl * (1 - qb) for c in after):
                    continue
            else:
                lvl  = fh * (1 + DIP / 100)
                stop = fh * (1 + STOP_BUF / 100)   # выше fh на 1%
                tp   = (fh + fl) / 2
                if not any(c["h"] >= lvl * (1 + qb) for c in after):
                    continue

            pos = SLOT
            fee_in = pos * FEE_MK / 100
            started = False
            net = None
            done = False
            outcome = "eod"

            for c in after:
                if not started:
                    if (side == "long"  and c["l"] <= lvl * (1 - qb)) or \
                       (side == "short" and c["h"] >= lvl * (1 + qb)):
                        started = True
                if not started:
                    continue
                hit_stop = (c["l"] <= stop) if side == "long" else (c["h"] >= stop)
                hit_tp   = (c["h"] >= tp)   if side == "long" else (c["l"] <= tp)
                if hit_stop:
                    if side == "long":
                        px = stop * (1 - STOP_SLIP / 100)
                        gross = (px / lvl - 1) * 100
                    else:
                        px = stop * (1 + STOP_SLIP / 100)
                        gross = (lvl / px - 1) * 100
                    net = gross / 100 * pos - fee_in - pos * (FEE_TK + STOP_SLIP) / 100
                    outcome = "sl"; done = True; break
                if hit_tp:
                    gross = (tp / lvl - 1) * 100 if side == "long" else (lvl / tp - 1) * 100
                    net = gross / 100 * pos - fee_in - pos * FEE_TK / 100
                    outcome = "tp"; done = True; break

            if not done:
                cl = after[-1]["c"]
                gross = (cl / lvl - 1) * 100 if side == "long" else (lvl / cl - 1) * 100
                net = gross / 100 * pos - fee_in - pos * FEE_TK / 100

            net += (pos * FUND_D / 100 if side == "short" else -pos * FUND_D / 100)
            dts = datetime.fromtimestamp(Xd * DAY, MSK).strftime("%Y-%m-%d")
            fills.append((dts, net / SLOT * 100, side, outcome))

    if not fills:
        print("филлов нет"); return

    by_d = {}
    for dts, n, s, o in fills:
        by_d.setdefault(dts, []).append(n)
    tot_usd = sum(n for _, n, _, _ in fills) * SLOT / 100
    day_sums = [sum(v) for v in by_d.values()]   # плоский список сумм по дням
    se = statistics.pstdev(day_sums) if len(day_sums) > 1 else 0.0
    ci_usd = Z * se * math.sqrt(len(day_sums)) * SLOT / 100
    eq = pk = 0.0; dd = 0.0; day_usd = {}
    for d in sorted(by_d):
        day_usd[d] = sum(by_d[d]) * SLOT / 100
        eq += day_usd[d]; pk = max(pk, eq); dd = min(dd, eq - pk)
    worst = min(day_usd.values())
    wr = sum(1 for _, n, _, _ in fills if n > 0) / len(fills) * 100
    outs = {}
    for _, _, _, o in fills: outs[o] = outs.get(o, 0) + 1
    sanity = " ⚠️ ВР>95% — вероятен баг" if wr > 95 and len(fills) > 100 else ""

    yr = {}
    for dts, n, s, o in fills:
        yr.setdefault(dts[:4], []).append(n * SLOT / 100)

    L = [f"🎯 <b>DRAFT v1.2</b>: {len(LIQ10)} пар × {effective} дн, "
         f"queue {QUEUE:.0f}, offset {OFFSET}" + (" ⏪" if OFFSET else ""),
         f"<i>лонг: вход low−{DIP}%, стоп low−{STOP_BUF}%, TP середина | "
         f"шорт зеркально | REGIME SMA{SMA_N} | KNIFE −{KNIFE_K}% | "
         f"издержки {FEE_MK}+{FEE_TK}+slip{STOP_SLIP}+фандинг{FUND_D}</i>", "",
         f"<b>ИТОГ</b>: {len(fills)} филлов / {len(by_d)} дн | ВР {wr:.0f}%{sanity}",
         f"  исходы: {outs}",
         f"  на-филл {sum(n for _,n,_,_ in fills)/len(fills):+.3f}% | "
         f"<b>Итог ${tot_usd:+,.0f} (±{ci_usd:,.0f})</b>",
         f"  худший день ${worst:+,.0f} | MaxDD ${dd:+,.0f}", ""]

    for side in ("long", "short"):
        sub = [n for _, n, s, _ in fills if s == side]
        if len(sub) >= 20:
            L.append(f"  {side}: {len(sub)} филлов, NET ${sum(sub)*SLOT/100:+,.0f}, "
                     f"на-филл {sum(sub)/len(sub):+.3f}%")

    if len(yr) >= 2:
        L.append("  годы: " + " | ".join(f"{y}: ${sum(v):+,.0f} ({len(v)})"
                                          for y, v in sorted(yr.items())))

    ok1 = tot_usd - ci_usd > 0
    ok2 = worst >= -500
    ok3 = dd >= -2000
    ok4 = all(sum(v) > -500 for v in yr.values()) if yr else True

    L += ["", f"<b>КРИТЕРИИ</b>: ① {'✅' if ok1 else '❌'}  "
              f"② {'✅' if ok2 else '❌'}  "
              f"③ {'✅' if ok3 else '❌'}  "
              f"④ {'✅' if ok4 else '❌'}  | ⑤ offset ⑥ queue25 — отдельно"]
    L.append("  → " + ("①✅: следующие шаги — DR_OFFSET=365, DR_QUEUE=25, форвард 4 нед"
                        if ok1 else "①❌ — архив ветки по пре-регу, без перебора фильтров"))

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", ""))
    try: B.send_blocks(msg.split("\n"))
    except Exception as e: print(f"[DR] отправка: {e}")


def main():
    try: run()
    except Exception:
        traceback.print_exc()
        try: B.send_telegram(f"⚠️ draft упал: {traceback.format_exc()[-400:]}")
        except Exception: pass

if __name__ == "__main__":
    main()
