"""
bt_draft.py v1.0 — DRAFT: Day Range Accumulation & Fade Trade.

Запуск: RUN_BACKTEST=draft   (env: DR_DAYS=1095 DR_OFFSET=0 DR_QUEUE=10)

МЕХАНИКА (одна, без сеток):
  Рамка дня: high/low 00:00-18:00 UTC (18 часов).
  В 18:00: высота рамки в [1.5%, 6.0%] -> рабочий день.
  Две лимитки: BUY low*(1-0.25%), SELL short high*(1+0.25%).
  Фильтр направления (REGIME): BTC close(X-1) > SMA30 -> только лонг-нога,
    ниже -> только шорт-нога (толкаемся по режиму, не против).
  Фильтр KNIFE: день X-1 пары <= -3% -> сегодня эту пару не трогаем.
  Исполнение: fill если low/high дня проколол уровень на QUEUE б.п. (очередь).
  Ведение: стоп = противоположная граница рамки (буфер 0.1%), TP = середина рамки,
    всё закрывается по close дня. Спорная свеча — стоп. Никаких переносов.

КРИТЕРИИ (пре-рег, 2.64s по дням, основная нога = та, что выбрал REGIME):
  1) Итог$ − CI$ > 0
  2) худший день >= -$500 (капитал $10k, слот $200, макс 10 филлов/день)
  3) MaxDD <= $2,000
  4) нет года с итогом < -$500
  5) offset DR_OFFSET=365: знак совпал
  6) DR_QUEUE=25: Итог$ > 0
Провал 1 на основном -> архив ветки без разговоров. Пост-хок природа честно
заявлена: планка повышена (Z=2.64), форвард 4 недели обязателен при прохождении.
"""
import os, time, math, statistics, traceback
from datetime import datetime, timezone, timedelta
import bot as B

DAYS    = int(os.environ.get("DR_DAYS", "1095"))
OFFSET  = int(os.environ.get("DR_OFFSET", "0"))
QUEUE   = float(os.environ.get("DR_QUEUE", "10"))       # б.п. прокола = филл
FRAME_LO, FRAME_HI = 1.5, 6.0                            # % высота рамки
DIP     = 0.25                                             # % за границу рамки
KNIFE_K = 3.0                                              # % — день-нож
SMA_N   = 30                                               # режим BTC
SLOT    = float(os.environ.get("DR_SLOT", "200"))
CAP     = float(os.environ.get("DR_CAP", "10000"))
FEE_MK, FEE_TK = 0.02, 0.05                                # % сторона
STOP_SLIP = 0.10                                            # % стоп
FUND_D  = 0.03                                              # % день (лонг платит)
Z       = 2.64
MSK = timezone(timedelta(hours=3)); DAY = 86400; HOUR = 3600
UTC18 = 18 * HOUR
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


MAX_1H_DAYS = 720   # Gate.io ограничение глубины 1h свечей

def fetch_1h(sym):
    now = int(time.time()) - OFFSET * DAY
    days_load = min(DAYS + 10, MAX_1H_DAYS)
    out, cur = [], now - days_load * DAY
    empty_streak = 0
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1h",
                                         "from": cur, "to": min(now, cur + 999 * HOUR)})
        part = B.parse_candles(raw) if raw else []
        if not part:
            empty_streak += 1
            if empty_streak >= 3: break
            cur += 7 * DAY   # перепрыгнуть дыру
            continue
        empty_streak = 0
        out.extend(part)
        nxt = part[-1].get("t", 0) + HOUR
        if nxt <= cur: break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"]); u.append(c)
    return u[:-1]


def run():
    effective_days = min(DAYS, MAX_1H_DAYS - 10)
    print(f"[DR] {len(LIQ10)} пар × {effective_days} дн (запрошено {DAYS}, макс 1h={MAX_1H_DAYS}), "
          f"queue {QUEUE:.0f} б.п., offset {OFFSET}")
    # BTC для режима: дневные closes
    btc_d = fetch_daily("BTC")
    closes = [c["c"] for c in btc_d]
    reg = {}
    for i in range(SMA_N, len(btc_d)):
        sma = sum(closes[i-SMA_N:i]) / SMA_N               # SMA по закрытиям ДО дня i
        reg[btc_d[i]["t"] // DAY] = closes[i-1] > sma      # решение на день i по close(i-1)
    # индекс дня -> BTC-режим: режим дня X решается по close(X-1)
    reg_day = {}
    for k, (d, c) in enumerate(arr := [(c["t"] // DAY, c["c"]) for c in btc_d]):
        if k >= SMA_N:
            sma = sum(closes[k-SMA_N:k]) / SMA_N
            reg_day[d] = closes[k-1] > sma

    data = {s: fetch_1h(s) for s in LIQ10}
    fills = []          # (trade_day_str, net%, side)
    for sym in LIQ10:
        h1 = data[sym]
        print(f"[DR] {sym}: {len(h1)} свечей 1h")
        if len(h1) < effective_days * 24 * 0.3:   # хватит хотя бы 30% периода
            print(f"[DR] {sym}: слишком мало данных, пропуск")
            continue
        by_day = {}
        for c in h1:
            by_day.setdefault(c["t"] // DAY, []).append(c)
        days_sorted = sorted(by_day)
        dbg = {"days": 0, "bars_short": 0, "knife": 0, "no_frame": 0,
               "height_fail": 0, "no_after": 0, "no_fill": 0, "ok": 0}
        for k in range(1, len(days_sorted)):
            Xd = days_sorted[k]
            Pd = days_sorted[k-1]
            bars = by_day[Xd]
            prev = by_day[Pd]
            dbg["days"] += 1
            if len(bars) < 22 or len(prev) < 20:
                dbg["bars_short"] += 1; continue
            prev_close = prev[-1]["c"]
            prev_open = prev[0]["o"]
            if prev_open <= 0: continue
            knife = (prev_close / prev_open - 1) * 100 <= -KNIFE_K
            if knife:
                dbg["knife"] += 1; continue
            side_mode = "long" if reg_day.get(Xd, True) else "short"
            # рамка: 00:00-18:00 UTC дня X
            frame = [c for c in bars if (c["t"] % DAY) < UTC18]
            if len(frame) < 16:
                dbg["no_frame"] += 1; continue
            fh, fl = max(c["h"] for c in frame), min(c["l"] for c in frame)
            height = (fh - fl) / fl * 100
            if not (FRAME_LO <= height <= FRAME_HI) or fl <= 0:
                dbg["height_fail"] += 1; continue
            # вечером 18:00 ставим обе ноги? НЕТ — только ногу режима
            qb = QUEUE / 10000
            if side_mode == "long":
                lvl = fl * (1 - DIP / 100)
                # филл: прокол вниз после 18:00
                after = [c for c in bars if (c["t"] % DAY) >= UTC18]
                if not after: dbg["no_after"] += 1; continue
                filled = any(c["l"] <= lvl * (1 - qb) for c in after)
                if not filled: dbg["no_fill"] += 1; continue
                dbg["ok"] += 1
                stop = fh * (1 + 0.001)
                tp = (fh + fl) / 2
                # ведение по свечам после филла
                net = None
                ent = lvl
                risk = abs(ent - stop)
                if risk <= 0: continue
                pos = SLOT
                fee_in = pos * FEE_MK / 100
                done = False
                started = False
                for c in after:
                    if not started and c["l"] <= lvl * (1 - qb):
                        started = True
                    if not started: continue
                    if c["l"] <= stop:
                        px = stop * (1 - STOP_SLIP / 100)
                        gross = (px / ent - 1) * 100
                        net = gross / 100 * pos - fee_in - pos * (FEE_TK + STOP_SLIP) / 100
                        done = True; break
                    if c["h"] >= tp:
                        gross = (tp / ent - 1) * 100
                        net = gross / 100 * pos - fee_in - pos * FEE_TK / 100
                        done = True; break
                if not done:
                    cl = after[-1]["c"]
                    gross = (cl / ent - 1) * 100
                    net = gross / 100 * pos - fee_in - pos * FEE_TK / 100
                net -= pos * FUND_D / 100
                dts = datetime.fromtimestamp(Xd * DAY, MSK).strftime("%Y-%m-%d")
                fills.append((dts, net / SLOT * 100, "long"))
            else:
                lvl = fh * (1 + DIP / 100)
                after = [c for c in bars if (c["t"] % DAY) >= UTC18]
                if not after: dbg["no_after"] += 1; continue
                filled = any(c["h"] >= lvl * (1 + qb) for c in after)
                if not filled: dbg["no_fill"] += 1; continue
                dbg["ok"] += 1
                stop = fl * (1 - 0.001)
                tp = (fh + fl) / 2
                net = None
                ent = lvl
                pos = SLOT
                fee_in = pos * FEE_MK / 100
                done = False
                started = False
                for c in after:
                    if not started and c["h"] >= lvl * (1 + qb):
                        started = True
                    if not started: continue
                    if c["h"] >= stop:
                        px = stop * (1 + STOP_SLIP / 100)
                        gross = (ent / px - 1) * 100
                        net = gross / 100 * pos - fee_in - pos * (FEE_TK + STOP_SLIP) / 100
                        done = True; break
                    if c["l"] <= tp:
                        gross = (ent / tp - 1) * 100
                        net = gross / 100 * pos - fee_in - pos * FEE_TK / 100
                        done = True; break
                if not done:
                    cl = after[-1]["c"]
                    gross = (ent / cl - 1) * 100
                    net = gross / 100 * pos - fee_in - pos * FEE_TK / 100
                net += pos * FUND_D / 100
                dts = datetime.fromtimestamp(Xd * DAY, MSK).strftime("%Y-%m-%d")
                fills.append((dts, net / SLOT * 100, "short"))
        print(f"[DR DBG {sym}] days={dbg['days']} bars_short={dbg['bars_short']} "
              f"knife={dbg['knife']} no_frame={dbg['no_frame']} "
              f"height_fail={dbg['height_fail']} no_after={dbg['no_after']} "
              f"no_fill={dbg['no_fill']} fills={dbg['ok']}")

    if not fills:
        print("филлов нет"); return
    by_d = {}
    for dts, net, side in fills:
        by_d.setdefault(dts, []).append(net)
    tot_pct = sum(n for _, n, _ in fills)
    tot_usd = tot_pct * SLOT / 100
    day_sums = list(by_d.values())
    se = statistics.pstdev(day_sums) if len(day_sums) > 1 else 0.0
    ci_usd = Z * se * math.sqrt(len(day_sums)) * SLOT / 100
    # MaxDD по торговым дням
    eq = pk = 0.0; dd = 0.0
    day_usd = {}
    for d in sorted(by_d):
        day_usd[d] = sum(by_d[d]) * SLOT / 100
        eq += day_usd[d]; pk = max(pk, eq); dd = min(dd, eq - pk)
    worst = min(day_usd.values()) if day_usd else 0.0
    wr = sum(1 for _, n, _ in fills if n > 0) / len(fills) * 100
    sides = {}
    for _, _, s in fills: sides[s] = sides.get(s, 0) + 1

    L = [f"🎯 <b>DRAFT v1.0 — Day Range Fade</b>: {len(LIQ10)} пар × {DAYS} дн, "
         f"queue {QUEUE:.0f} б.п., offset {OFFSET}" + (" ⏪" if OFFSET else ""),
         f"<i>рамка 00-18 UTC [{FRAME_LO}-{FRAME_HI}%], лимитка за рамкой {DIP}%, стоп за "
         f"противоположной границей, TP в середину, close в 23:55 UTC | REGIME выбирает ногу | "
         f"KNIFE -{KNIFE_K}% | издержки maker {FEE_MK}+taker {FEE_TK}+стоп-slip {STOP_SLIP}+фандинг</i>",
         f"<i>слот ${SLOT:.0f}, капитал ${CAP:,.0f}</i>", "",
         f"<b>ИТОГ</b>: {len(fills)} филлов / {len(by_d)} дней ({len(fills)/max(len(by_d),1):.1f}/день)",
         f"  стороны: {sides} | ВР {wr:.0f}%",
         f"  на-филл {tot_pct/len(fills):+.3f}% | <b>Итог ${tot_usd:+,.0f} (±{ci_usd:,.0f})</b>",
         f"  худший день ${worst:+,.0f} | MaxDD ${dd:+,.0f}", ""]
    yr = {}
    for dts, n, s in fills:
        yr.setdefault(dts[:4], []).append(n * SLOT / 100)
    if len(yr) >= 3:
        L.append("<b>Годы ($)</b>: " + " | ".join(
            f"{y}: ${sum(v):+,.0f} ({len(v)})" for y, v in sorted(yr.items())))
        L.append("")
    # разрез по сторонам
    for side in ("long", "short"):
        sub = [n for _, n, s in fills if s == side]
        if len(sub) >= 30:
            t = sum(sub) * SLOT / 100
            L.append(f"  нога {side}: {len(sub)} филлов, NET ${t:+,.0f}, "
                     f"на-филл {sum(sub)/len(sub):+.3f}%")
    L.append("")
    ok1 = tot_usd - ci_usd > 0
    ok2 = worst >= -500
    ok3 = dd >= -2000
    ok4 = len(yr) >= 1 and all(sum(v) > -500 for v in yr.values())
    L.append("<b>КРИТЕРИИ</b>: "
             f"① итог−CI>0: {'✅' if ok1 else '❌'}  "
             f"② худший день: {'✅' if ok2 else '❌'}  "
             f"③ MaxDD: {'✅' if ok3 else '❌'}  "
             f"④ годы: {'✅' if ok4 else '❌'}  "
             f"⑤ offset ⑥ queue25 — отдельные прогоны")
    if not ok1:
        L.append("  → <b>① ПРОВАЛЕН — архив ветки</b> (по пре-регу, без разговоров)")
    else:
        L.append("  → ①✅: следующие шаги — DR_OFFSET=365, DR_QUEUE=25, потом форвард 4 нед")
    L += ["", "<i>честные оговорки: конструкция пост-хок (планка повышена), филл-модель "
              "упрощена (реальная очередь хуже), выход close без слипа — реальность хуже "
              "на ~0.05-0.1%. Если ①✅ на основном + offset + queue25 — только тогда "
              "live-скелет и форвард 4 недели. НЕ раньше</i>"]

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