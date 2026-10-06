"""
bt_pulse.py — OI-PULSE: тренд, подтверждённый позиционированием.

Запуск: RUN_BACKTEST=pulse

Гипотеза: рост цены на РАСТУЩЕМ OI и с преобладанием агрессивных покупок —
движение на новых деньгах, оно продолжается. Рост на падающем OI — выдох.
Чистый price-momentum (X-MOM) покупал оба состояния и получил ноль.
Здесь отбор только «новых денег»: OI ↑3д ≥2%, тейкеры в нашу сторону,
фандинг не экстремальный. Лонг-only: шорт-нога убила X-MOM (−2.14%/цикл).

КОНТРОЛИ встроены:
  price-only — тот же ранг по цене БЕЗ фильтров позиционирования
  random     — случайные лонги (бета рынка)
  половины   — стабильность внутри периода

Данные: contract_stats (OI, тейкеры, фандинг) — Gate хранит ~60 дней.

Настройки (env): PULSE_DAYS(60) PULSE_OFFSET(0) PULSE_MOM_D(5) PULSE_OI_D(3)
  PULSE_OI_K(2.0) PULSE_TAKER_K(1.1) PULSE_FUND_MAX(0.05) PULSE_TOP(5)
  PULSE_HOLD(3) PULSE_HOLD2(5) PULSE_FEE(0.05) PULSE_SLIP(0.05) PULSE_SLOT(500)
"""
import os
import time
import random
import traceback
import statistics
from datetime import timezone, timedelta

import bot as B

DAYS     = int(os.environ.get("PULSE_DAYS", "60"))
OFFSET   = int(os.environ.get("PULSE_OFFSET", "0"))
MOM_D    = int(os.environ.get("PULSE_MOM_D", "5"))
OI_D     = int(os.environ.get("PULSE_OI_D", "3"))
OI_K     = float(os.environ.get("PULSE_OI_K", "2.0"))       # % роста OI за OI_D дней
TAKER_K  = float(os.environ.get("PULSE_TAKER_K", "1.1"))    # long/short тейкеры
FUND_MAX = float(os.environ.get("PULSE_FUND_MAX", "0.05"))   # % за 8ч, среднее за день
TOP_N    = int(os.environ.get("PULSE_TOP", "5"))
HOLD_D   = int(os.environ.get("PULSE_HOLD", "3"))
HOLD2_D  = int(os.environ.get("PULSE_HOLD2", "5"))
SLOT     = float(os.environ.get("PULSE_SLOT", "500"))
FEE      = float(os.environ.get("PULSE_FEE", "0.05"))
SLIP     = float(os.environ.get("PULSE_SLIP", "0.05"))
COST     = (FEE + SLIP) / 100 * 2

MSK = timezone(timedelta(hours=3))
DAY = 86400


def _fetch_stats(sym, days):
    """contract_stats построчно, 1h, за days дней. Пагинация через from/limit."""
    now = int(time.time()) - OFFSET * DAY
    t0 = now - days * DAY
    out, cur, empty = [], t0, 0
    while cur < now and empty < 4:
        raw = B.api_get("contract_stats", {"contract": f"{sym}_USDT", "interval": "1h",
                                           "from": cur, "limit": 100})
        if not isinstance(raw, list) or not raw:
            empty += 1
            cur += 100 * 3600
            continue
        empty = 0
        out.extend(raw)
        nxt = max(int(float(d.get("time") or 0)) for d in raw) + 3600
        if nxt <= cur:
            break
        cur = nxt
    seen, rows = set(), []
    for d in sorted(out, key=lambda x: int(float(x.get("time") or 0))):
        t = int(float(d.get("time") or 0))
        if t and t not in seen:
            seen.add(t)
            rows.append(d)
    return rows


def _fetch_1h(sym, days):
    """Часовые свечи за days дней."""
    sec = 3600
    now = int(time.time()) - OFFSET * DAY
    out, cur, probes = [], now - days * DAY, 0
    while cur < now:
        to = min(now, cur + 900 * sec)
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1h",
                                         "from": cur, "to": to})
        part = B.parse_candles(raw) if raw else []
        if not part:
            probes += 1
            if probes > 20:
                break
            cur += 5 * DAY
            continue
        out.extend(part)
        nxt = part[-1].get("t", 0) + sec
        if nxt <= cur:
            break
        cur = nxt
    seen, uniq = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t") not in seen:
            seen.add(c["t"])
            uniq.append(c)
    return uniq


def _to_daily(rows):
    """Суточные агрегаты из 1h-строк: OI на конец дня, тейкеры, средний фандинг."""
    by_day = {}
    for d in rows:
        t = int(float(d.get("time") or 0))
        if not t:
            continue
        day = t // DAY
        rec = by_day.setdefault(day, {"oi": None, "tl": 0.0, "ts": 0.0,
                                      "f": [], "n": 0})
        oi = float(d.get("open_interest_usd") or d.get("open_interest") or 0)
        if oi > 0:
            rec["oi"] = oi            # последняя валидная за день
        rec["tl"] += float(d.get("long_taker_size") or 0)
        rec["ts"] += float(d.get("short_taker_size") or 0)
        fr = d.get("last_funding_rate")
        if fr not in (None, ""):
            try:
                rec["f"].append(float(fr))
            except (ValueError, TypeError):
                pass
        rec["n"] += 1
    return by_day


def _stat(vals):
    if not vals:
        return None
    n = len(vals)
    m = sum(vals) / n
    se = (statistics.pstdev(vals) / (n ** 0.5)) if n > 1 else 0.0
    return n, m, 1.96 * se


def _line(vals, label):
    st = _stat(vals)
    if not st:
        return f"  {label}: циклов нет"
    n, m, ci = st
    mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
    return f"  {mark} {label}: <b>{m:+.2f}%</b> за цикл (±{ci:.2f}), циклов {n}"


def run():
    pairs = B.UPSCALE_PAIRS
    need = DAYS + MOM_D + OI_D + HOLD2_D + 3
    print(f"[PULSE] качаю stats+свечи: {len(pairs)} пар × {need} дн (offset {OFFSET}д)")
    t0 = time.time()

    D = {}    # sym -> {"c": {t: candle}, "s": {day: agg}}
    for i, sym in enumerate(pairs, 1):
        try:
            st = _fetch_stats(sym, need)
            ch = _fetch_1h(sym, need)
        except Exception:
            continue
        if len(ch) < (MOM_D + HOLD2_D + 3) * 24:
            continue
        ch = ch[:-1]                          # незакрытая свеча не нужна
        agg = _to_daily(st)
        min_days = (need - OI_D - 1) * 0.85
        if len(agg) < min_days:
            continue
        D[sym] = {"c": {c["t"]: c for c in ch}, "s": agg}
        if i % 20 == 0:
            print(f"[PULSE] {i}/{len(pairs)} | пар {len(D)} | {time.time() - t0:.0f}с")

    print(f"[PULSE] пар с полной историей: {len(D)}")
    if len(D) < 20:
        B.send_telegram("⚠️ OI-PULSE: мало пар с полной историей (<20)")
        return

    ref = max(D.values(), key=lambda v: len(v["c"]))["c"]
    times = sorted(ref.keys())
    days_all = sorted({t // DAY for t in times})
    days_all = days_all[MOM_D + OI_D:]
    days_all = [d for d in days_all if d + HOLD2_D < days_all[-1] + 1]

    def day_close(sym, d):
        """Цена закрытия дня d: последняя часовая свеча."""
        m = D[sym]["c"]
        best = None
        for h in range(d * DAY, (d + 1) * DAY, 3600):
            if h in m:
                best = m[h]
        return best

    def ret_nd(sym, d):
        """Доходность за MOM_D дней на момент d. None если данных нет."""
        c_now = day_close(sym, d)
        c_old = day_close(sym, d - MOM_D)
        if not c_now or not c_old or c_old["c"] <= 0:
            return None
        return c_now["c"] / c_old["c"] - 1

    def filters_full(sym, d):
        """Все условия позиционирования. Возвращает ret или None."""
        r = ret_nd(sym, d)
        if r is None or r <= 0:         # условие 1: тренд
            return None
        s = D[sym]["s"]
        oi_now = s.get(d, {}).get("oi")
        oi_old = s.get(d - OI_D, {}).get("oi")
        if not oi_now or not oi_old or oi_old <= 0:
            return None
        if (oi_now / oi_old - 1) * 100 < OI_K:     # условие 2: OI-пульс
            return None
        agg = s.get(d, {})
        if agg.get("ts", 0) <= 0:
            return None
        if agg["tl"] / agg["ts"] < TAKER_K:         # условие 3: тейкеры
            return None
        fr_list = agg.get("f", [])
        fr = sum(fr_list) / len(fr_list) * 100 if fr_list else 0.0
        if fr > FUND_MAX:                            # условие 4: фандинг-sanity
            return None
        return r

    def first_open(sym, d):
        """Open первой часовой свечи дня d."""
        m = D[sym]["c"]
        for h in range(d * DAY, (d + 1) * DAY, 3600):
            if h in m:
                return m[h]["o"]
        return None

    def entry_exit(sym, d, hold):
        en = first_open(sym, d + 1)
        ex = first_open(sym, d + 1 + hold)
        if not en or not ex or en <= 0:
            return None
        return (ex / en - 1) - COST

    rnd = random.Random(777)

    def cycle(hold, mode="full"):
        """Один проход по дням.
        mode: full = полные фильтры, price = только тренд, random = случайные лонги."""
        rets = []
        for d in days_all[:-hold]:
            cands = []
            if mode == "full":
                for sym in D:
                    r = filters_full(sym, d)
                    if r is not None:
                        cands.append((r, sym))
            elif mode == "price":
                # только условие тренда: close > close_5d, без OI/тейкеров/фандинга
                for sym in D:
                    r = ret_nd(sym, d)
                    if r is not None and r > 0:
                        cands.append((r, sym))
            else:  # random
                pool = [s for s in D if ret_nd(s, d) is not None]
                cands = [(0.0, s) for s in pool]

            if len(cands) < TOP_N:
                continue

            if mode == "random":
                pick = rnd.sample([s for _, s in cands], TOP_N)
            else:
                cands.sort(key=lambda x: -x[0])
                pick = [s for _, s in cands[:TOP_N]]

            ps = [entry_exit(s, d, hold) for s in pick]
            ps = [x for x in ps if x is not None]
            if len(ps) >= TOP_N - 1:
                rets.append(sum(ps) / len(ps) * 100)
        return rets

    cov = (times[-1] - times[0]) / DAY
    L = [f"🫀 <b>OI-PULSE: тренд на новых деньгах</b> (~{cov:.0f} дн, {len(D)} пар)"
         + (f"\n⏪ <b>ПЕРИОД СДВИНУТ НА {OFFSET} ДНЕЙ</b>" if OFFSET else ""),
         f"<i>лонг топ-{TOP_N}: ret{MOM_D}д&gt;0 + OI{OI_D}д ≥+{OI_K}% + тейкеры ≥{TAKER_K} "
         f"+ funding ≤{FUND_MAX}%/8ч. Удержание {HOLD_D} дн, слот ${SLOT:.0f}, "
         f"издержки {COST * 100:.2f}% на круг</i>",
         "<i>решение по закрытому дню, вход по open следующего. Лонг-only, нет сигналов → кэш</i>",
         ""]

    for hold, tag in ((HOLD_D, f"УДЕРЖАНИЕ {HOLD_D} ДН (основное)"),
                      (HOLD2_D, f"УДЕРЖАНИЕ {HOLD2_D} ДН (вторая точка, фикс заранее)")):
        full = cycle(hold, "full")
        pric = cycle(hold, "price")
        rann = cycle(hold, "random")
        L.append(f"═══ <b>{tag}</b> ═══")
        L.append(_line(full, "OI-PULSE (полные фильтры)"))
        L.append(_line(pric, f"price-only (ret{MOM_D}д&gt;0, без OI/тейкеров/фандинга)"))
        L.append(_line(rann, "random (случайные 5 лонгов, бета рынка)"))
        sf, sp, sr = _stat(full), _stat(pric), _stat(rann)
        if sf and sp:
            diff = sf[1] - sp[1]
            n = min(sf[0], sp[0])
            ci = 1.96 * ((statistics.pstdev(full) ** 2 + statistics.pstdev(pric) ** 2) / n) ** 0.5 if n > 1 else 0
            v = ("вклад позиционирования ЕСТЬ" if diff - ci > 0 else
                 "фильтры ВРЕДЯТ" if diff + ci < 0 else "разница в шуме")
            L.append(f"  → <b>OI-слой против price-only: {diff:+.2f}%/цикл (±{ci:.2f}) — {v}</b>")
        if sf and sr:
            L.append(f"  → против случайных лонгов: {sf[1] - sr[1]:+.2f}%/цикл (бета вычтена)")
        if full:
            half = len(full) // 2
            if half >= 4:
                a = statistics.mean(full[:half])
                b = statistics.mean(full[half:])
                L.append(f"  → половины: {a:+.2f}% | {b:+.2f}% — "
                         + ("держится" if a > 0 and b > 0 else "⚠️ разваливается"))
            wins = sum(1 for x in full if x > 0)
            usd = sum(x / 100 * SLOT * TOP_N for x in full)
            L.append(f"  → прибыльных циклов {wins}/{len(full)}, итого ${usd:+,.0f}")
        L.append("")

    L.append("<i>критерий запуска: OI-PULSE ≥ price-only на +0.4%/цикл, оба удержания "
             "согласны, половины в плюсе. Офсет невозможен (история 60 дней), замена: "
             "обе половины + согласованность двух удержаний. Демо-форвард обязателен.</i>")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
          .replace("&gt;", ">"))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[PULSE] отправка: {e}")


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ OI-PULSE упал: {traceback.format_exc()[-400:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
