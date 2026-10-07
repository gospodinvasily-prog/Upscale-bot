"""
bt_draft3.py v1.1 — DRAFT+FLOW: финальный тест серии.

База: DRAFT v1.2 (рамка 00-18UTC, лимитка за рамкой 0.25%, стоп за своей 1.0%,
TP середина, close 23:55).

ФИЛЬТРЫ (event-time, по contract_stats 1h):
  F1 НОГА ПО ПОТОКУ: обе ноги кандидаты; нога выбирается тем, КУДА шла агрессия
     последних FLOW_H часов ДО касания уровня.
     Тейкеры продавали (fl_st > fl_lt) -> кандидат-лонг (фейдим выдохшегося продавца).
     Тейкеры покупали (fl_lt > fl_st) -> кандидат-шорт (фейдим выдохшегося покупателя).
     Если поток нулевой — пропускаем оба филла.
     REGIME SMA30 убран полностью.

  F2 НОЖ-БЛОК: если в ПОСЛЕДНИЙ час перед филлом агрессия В СТОРОНУ НАШЕЙ НОГИ
     >= BLOCK_K * противоположной — пропускаем (толпа давит нам навстречу, не фейдим).
     Лонг-нога: агрессивные покупки (lt_last) >= BLOCK_K * продажи -> пропуск.
     Шорт-нога: агрессивные продажи (st_last) >= BLOCK_K * покупки -> пропуск.

Запуск: RUN_BACKTEST=draft3   (env: DR3_DAYS=60 DR3_QUEUE=10)
КРИТЕРИЙ: NET$ > 0 И лучше безусловного DRAFT на том же периоде.
Провал -> серия закрыта окончательно.
"""
import os, time, math, statistics, traceback
from datetime import datetime, timezone, timedelta
import bot as B

DAYS    = int(os.environ.get("DR3_DAYS", "60"))
QUEUE   = float(os.environ.get("DR3_QUEUE", "10"))
FRAME_LO, FRAME_HI = 1.5, 6.0
DIP     = 0.25
STOP_BUF = 1.0
KNIFE_K = 3.0
FLOW_H  = 2          # часов потока для F1
BLOCK_K = 1.5        # множитель агрессии для F2 (нож-блок)
SLOT    = float(os.environ.get("DR3_SLOT", "200"))
FEE_MK, FEE_TK, STOP_SLIP, FUND_D = 0.02, 0.05, 0.10, 0.03
MSK = timezone(timedelta(hours=3)); DAY = 86400; HOUR = 3600
UTC18 = 18 * HOUR
LIQ10 = ["BTC","ETH","SOL","XRP","BNB","ADA","DOGE","LINK","LTC","AVAX"]
MAX_1H_DAYS = 400
BATCH = 999


def fetch_1h(sym):
    """limit+to назад — обход Gate.io лимита глубины для from/to."""
    now = int(time.time())
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


def fetch_flow(sym):
    """contract_stats 1h: {ts_hour: (long_taker, short_taker)}."""
    now = int(time.time())
    limit_ts = now - min(DAYS + 5, MAX_1H_DAYS) * DAY
    out = []
    to_ts = now
    while to_ts > limit_ts:
        raw = B.api_get("contract_stats", {
            "contract": f"{sym}_USDT", "interval": "1h",
            "to": to_ts, "limit": 100
        })
        rows = raw if isinstance(raw, list) else []
        if not rows:
            break
        out.extend(rows)
        earliest_t = min(int(float(r.get("time") or 0)) for r in rows)
        if earliest_t >= to_ts:
            break
        to_ts = earliest_t - 1
        if to_ts <= limit_ts:
            break
    res = {}
    for r in out:
        t = int(float(r.get("time") or 0))
        lt = float(r.get("long_taker_size") or 0)
        st = float(r.get("short_taker_size") or 0)
        if t and lt + st > 0:
            res[t // HOUR * HOUR] = (lt, st)
    return res


def sim_leg(after, lvl, is_long, stop, tp, started_idx):
    pos = SLOT
    fee_in = pos * FEE_MK / 100
    for c in after[started_idx:]:
        hit_stop = c["l"] <= stop if is_long else c["h"] >= stop
        hit_tp   = c["h"] >= tp   if is_long else c["l"] <= tp
        if hit_stop:
            if is_long:
                px = stop * (1 - STOP_SLIP / 100)
                gross = (px / lvl - 1) * 100
            else:
                px = stop * (1 + STOP_SLIP / 100)
                gross = (lvl / px - 1) * 100
            net = gross / 100 * pos - fee_in - pos * (FEE_TK + STOP_SLIP) / 100
            fund = -pos * FUND_D / 100 if is_long else pos * FUND_D / 100
            return (net + fund) / pos * 100, "sl"
        if hit_tp:
            gross = (tp / lvl - 1) * 100 if is_long else (lvl / tp - 1) * 100
            net = gross / 100 * pos - fee_in - pos * FEE_TK / 100
            fund = -pos * FUND_D / 100 if is_long else pos * FUND_D / 100
            return (net + fund) / pos * 100, "tp"
    cl = after[-1]["c"]
    gross = (cl / lvl - 1) * 100 if is_long else (lvl / cl - 1) * 100
    net = gross / 100 * pos - fee_in - pos * FEE_TK / 100
    fund = -pos * FUND_D / 100 if is_long else pos * FUND_D / 100
    return (net + fund) / pos * 100, "eod"


def run():
    effective = min(DAYS, MAX_1H_DAYS - 5)
    print(f"[DR3] {len(LIQ10)} пар × {effective} дн, queue {QUEUE:.0f}")

    fills_flow = []   # филлы с F1+F2
    fills_base = []   # безусловный DRAFT (те же рамочные условия, обе ноги)

    for sym in LIQ10:
        h1 = fetch_1h(sym)
        print(f"[DR3] {sym}: {len(h1)} свечей 1h")
        if len(h1) < effective * 24 * 0.3:
            print(f"[DR3] {sym}: мало свечей, пропуск")
            continue
        flow = fetch_flow(sym)
        print(f"[DR3] {sym}: {len(flow)} flow-записей")

        by_day = {}
        for c in h1:
            by_day.setdefault(c["t"] // DAY, []).append(c)
        ds = sorted(by_day)

        for k in range(1, len(ds)):
            Xd, Pd = ds[k], ds[k - 1]
            bars, prev = by_day[Xd], by_day[Pd]
            if len(bars) < 22 or len(prev) < 20 or prev[0]["o"] <= 0:
                continue
            if (prev[-1]["c"] / prev[0]["o"] - 1) * 100 <= -KNIFE_K:
                continue
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

            # Проверяем оба уровня
            for side in ("long", "short"):
                is_l = (side == "long")
                lvl  = fl * (1 - DIP / 100) if is_l else fh * (1 + DIP / 100)
                stop = fl * (1 - STOP_BUF / 100) if is_l else fh * (1 + STOP_BUF / 100)
                tp   = (fh + fl) / 2

                # Проверяем факт касания уровня
                si = next((i for i, c in enumerate(after)
                           if (c["l"] <= lvl * (1 - qb) if is_l
                               else c["h"] >= lvl * (1 + qb))), None)
                if si is None:
                    continue

                dts = datetime.fromtimestamp(Xd * DAY, MSK).strftime("%Y-%m-%d")

                # ── Безусловный DRAFT (контрольная группа) ──
                net_b, out_b = sim_leg(after, lvl, is_l, stop, tp, si + 1)
                fills_base.append((dts, net_b, side, out_b))

                # ── F1: определяем ногу по потоку FLOW_H часов до касания ──
                touch_t = after[si]["t"]
                fl_lt = sum(flow.get(t, (0, 0))[0]
                            for t in range(touch_t - FLOW_H * HOUR, touch_t, HOUR))
                fl_st = sum(flow.get(t, (0, 0))[1]
                            for t in range(touch_t - FLOW_H * HOUR, touch_t, HOUR))
                if fl_lt + fl_st <= 0:
                    continue  # нет данных потока — пропуск

                # Тейкеры продавали -> фейдим -> лонг-нога
                # Тейкеры покупали -> фейдим -> шорт-нога
                selling = fl_st > fl_lt
                want_long = selling   # продавали — берём лонг

                if (side == "long") != want_long:
                    continue  # F1: нога не соответствует потоку

                # ── F2: нож-блок — последний час ДО касания ──
                lt_last = flow.get(touch_t - HOUR, (0, 0))[0]
                st_last = flow.get(touch_t - HOUR, (0, 0))[1]

                if is_l and lt_last >= BLOCK_K * max(st_last, 1e-9):
                    continue  # покупают активно в нашу сторону — нож в лонге
                if (not is_l) and st_last >= BLOCK_K * max(lt_last, 1e-9):
                    continue  # продают активно в нашу сторону — нож в шорте

                # ── Прошёл оба фильтра — симулируем ──
                net_f, out_f = sim_leg(after, lvl, is_l, stop, tp, si + 1)
                fills_flow.append((dts, net_f, side, out_f))

    # ── Статистика безусловного DRAFT ──
    if fills_base:
        base_usd = sum(n for _, n, _, _ in fills_base) * SLOT / 100
    else:
        base_usd = 0.0

    if not fills_flow:
        print(f"филлов нет — фильтры слишком строгие | безусловный DRAFT: ${base_usd:+,.0f}")
        return

    by_d = {}
    for dts, n, s, o in fills_flow:
        by_d.setdefault(dts, []).append(n)
    tot_usd = sum(n for _, n, _, _ in fills_flow) * SLOT / 100
    day_sums = [sum(v) for v in by_d.values()]
    se = statistics.pstdev(day_sums) if len(day_sums) > 1 else 0.0
    ci_usd = 2.64 * se * math.sqrt(len(day_sums)) * SLOT / 100
    eq = pk = 0.0; dd = 0.0; day_usd = {}
    for d in sorted(by_d):
        day_usd[d] = sum(by_d[d]) * SLOT / 100
        eq += day_usd[d]; pk = max(pk, eq); dd = min(dd, eq - pk)
    worst = min(day_usd.values()) if day_usd else 0.0
    wr = sum(1 for _, n, _, _ in fills_flow if n > 0) / len(fills_flow) * 100
    outs = {}
    for _, _, _, o in fills_flow: outs[o] = outs.get(o, 0) + 1
    sides = {}
    for _, _, s, _ in fills_flow: sides[s] = sides.get(s, 0) + 1

    ok = tot_usd > 0 and tot_usd > base_usd

    L = [f"🌊 <b>DRAFT+FLOW v1.1</b>: {len(LIQ10)} пар × {effective} дн, queue {QUEUE:.0f}",
         f"<i>F1: нога против потока {FLOW_H}ч | F2: нож-блок агрессия ≥{BLOCK_K}× | "
         f"REGIME убран | обе ноги открыты</i>", "",
         f"<b>ИТОГ (с фильтрами)</b>: {len(fills_flow)} филлов / {len(by_d)} дн | "
         f"стороны {sides} | ВР {wr:.0f}%",
         f"  исходы: {outs}",
         f"  на-филл {sum(n for _, n, _, _ in fills_flow)/len(fills_flow):+.3f}% | "
         f"<b>NET ${tot_usd:+,.0f} (±{ci_usd:,.0f})</b>",
         f"  худший день ${worst:+,.0f} | MaxDD ${dd:+,.0f}",
         f"<b>Контроль (безусловный DRAFT)</b>: {len(fills_base)} филлов | "
         f"<b>NET ${base_usd:+,.0f}</b>", ""]

    L.append(f"<b>КРИТЕРИЙ (NET>0 И лучше контроля): {'✅ ПРОЙДЕН' if ok else '❌ ПРОВАЛЕН'}</b>")
    L.append("  → " + ("жив: форвард 2-3 недели paper-лимитками" if ok else
                       "серия закрыта окончательно: contract_stats-фильтр проверен"))

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", ""))
    try: B.send_blocks(msg.split("\n"))
    except Exception as e: print(f"[DR3] отправка: {e}")


def main():
    try: run()
    except Exception:
        traceback.print_exc()
        try: B.send_telegram(f"⚠️ draft3 упал: {traceback.format_exc()[-400:]}")
        except Exception: pass

if __name__ == "__main__":
    main()
