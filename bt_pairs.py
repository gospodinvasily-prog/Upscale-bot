"""
bt_pairs.py — FLUSH: разведка истории ликвидаций и бэктест на ней.

Запускается привычным RUN_BACKTEST=pairs — менять bot.py не нужно.

ЧАСТЬ 1 — РАЗВЕДКА. Отдаёт ли Gate историю contract_stats (открытый интерес и
ликвидации) вглубь по параметру from. Пробуем 1, 3, 7, 14, 30, 60 дней назад и
печатаем, что реально вернулось, вместе с именами полей.

ЧАСТЬ 2 — БЭКТЕСТ, запускается автоматически, если история нашлась хотя бы на
несколько дней. Гипотеза: каскад ликвидаций — это принудительные маркет-ордера,
которые уводят цену дальше равновесия; когда топливо кончается (OI упал, появилась
встречная свеча), цена откатывает.

Почему это стоит проверять после закрытия ценовых идей: здесь сигнал строится НЕ
на цене, а на данных о позициях. Все похороненные гипотезы были price-only.

Тайминг честный: событие определяется по ЗАКРЫТЫМ 5м данным, вход по открытию
следующей свечи. Рядом считается монетка.

Настройки:
  FL_PROBE_ONLY (0)   — 1 = только разведка, без бэктеста
  FL_DAYS (30)        — сколько дней истории брать для бэктеста
  FL_PAIRS (0)        — сколько пар (0 = все)
  FL_MULT (5)         — во сколько раз ликвидации выше медианы
  FL_MIN_USD (3000)   — минимальная сумма ликвидаций за час
  FL_OI_DROP (0.8)    — на сколько % должен упасть OI за час
  FL_MOVE (2.5)       — минимальный ход цены за час, %
  FL_MAX_STOP (3.0)   — максимальный стоп, % (иначе событие пропускаем)
  FL_HOLD_H (12)      — горизонт удержания
"""
import os
import time
import statistics

import bot as B

PROBE_ONLY = os.environ.get("FL_PROBE_ONLY", "0") == "1"
DAYS       = int(os.environ.get("FL_DAYS", "14"))
# contract_stats отдаёт по 100 строк = 8 часов пятиминуток. На 30 дней это ~90
# запросов НА ПАРУ, на 103 парах — тысячи запросов и часы ожидания. Поэтому по
# умолчанию берём 40 пар и 14 дней; расширять через FL_PAIRS=0 и FL_DAYS осознанно.
PAIRS_N    = int(os.environ.get("FL_PAIRS", "40"))
LIQ_MULT   = float(os.environ.get("FL_MULT", "5"))
LIQ_MIN    = float(os.environ.get("FL_MIN_USD", "3000"))
OI_DROP    = float(os.environ.get("FL_OI_DROP", "0.8"))
MOVE_PCT   = float(os.environ.get("FL_MOVE", "2.5"))
MAX_STOP   = float(os.environ.get("FL_MAX_STOP", "3.0"))
HOLD_H     = float(os.environ.get("FL_HOLD_H", "12"))
STOP_BUF   = float(os.environ.get("FL_STOP_BUF", "0.1"))
SLIP       = float(os.environ.get("FL_SLIP", "0.10"))
STOP_SLIP  = float(os.environ.get("FL_STOP_SLIP", "0.10"))
FEE_PCT    = float(os.environ.get("FL_FEE", "0.065"))

STEP = 300          # 5м
PARTS = (1 / 3, 1 / 3, 1 / 3)
TP_K = (0.4, 0.7, 1.0)      # доли каскадного хода


# ═════════ ЧАСТЬ 1: РАЗВЕДКА ═════════

def probe():
    """Пробуем contract_stats на разной глубине. Возвращает (глубина в днях, поля)."""
    L = ["🔍 <b>РАЗВЕДКА: отдаёт ли Gate историю ликвидаций и OI</b>",
         "<i>если история есть — все гипотезы на позициях становятся проверяемыми "
         "за один прогон вместо недель ожидания</i>", ""]
    now = int(time.time())
    depth_found = 0
    fields = []
    for d in (0, 1, 3, 7, 14, 30, 60):
        params = {"contract": "BTC_USDT", "interval": "5m", "limit": 100}
        if d:
            params["from"] = now - d * 86400
            params["to"] = now - d * 86400 + 100 * STEP
        raw = B.api_get("contract_stats", params)
        if not isinstance(raw, list) or not raw:
            L.append(f"  {d:2} дн назад: пусто")
            continue
        ts = [int(r.get("time") or 0) for r in raw if r.get("time")]
        if not ts:
            L.append(f"  {d:2} дн назад: {len(raw)} строк, но без времени")
            continue
        age = (now - max(ts)) / 86400
        L.append(f"  {d:2} дн назад: {len(raw)} строк, самая свежая {age:.1f} дн назад")
        if d and age > d * 0.5:
            depth_found = max(depth_found, d)
        if not fields:
            fields = sorted(raw[0].keys())
    L.append("")
    if fields:
        L.append(f"<b>Поля ответа:</b> {', '.join(fields)}")
    have_liq = any("liq" in f for f in fields)
    have_oi = any("open_interest" in f for f in fields)
    L.append(f"ликвидации в ответе: {'есть' if have_liq else '<b>НЕТ</b>'} | "
             f"открытый интерес: {'есть' if have_oi else '<b>НЕТ</b>'}")
    L.append(f"<b>Глубина истории: ~{depth_found} дн</b>" if depth_found else
             "<b>История вглубь не отдаётся — только форвард</b>")
    return depth_found, fields, L


# ═════════ ЧАСТЬ 2: БЭКТЕСТ ═════════

def _fetch_stats(sym, days):
    """contract_stats за days дней кусками по 100 строк."""
    now = int(time.time())
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        to = min(now, cur + 100 * STEP)
        raw = B.api_get("contract_stats", {"contract": f"{sym}_USDT", "interval": "5m",
                                           "from": cur, "to": to, "limit": 100})
        rows = raw if isinstance(raw, list) else []
        if not rows:
            probes += 1
            if probes > 20:
                break
            cur += 86400
            continue
        out.extend(rows)
        nxt = max(int(r.get("time") or 0) for r in rows) + STEP
        if nxt <= cur:
            break
        cur = nxt
    seen, uniq = set(), []
    for r in sorted(out, key=lambda x: int(x.get("time") or 0)):
        t = int(r.get("time") or 0)
        if t and t not in seen:
            seen.add(t)
            uniq.append(r)
    return uniq


def _fetch_candles(sym, days):
    now = int(time.time())
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        to = min(now, cur + 1900 * STEP)
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "5m",
                                         "from": cur, "to": to})
        part = B.parse_candles(raw) if raw else []
        if not part:
            probes += 1
            if probes > 20:
                break
            cur += 86400
            continue
        out.extend(part)
        nxt = part[-1].get("t", 0) + STEP
        if nxt <= cur:
            break
        cur = nxt
    seen, uniq = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t") not in seen:
            seen.add(c.get("t"))
            uniq.append(c)
    return uniq


def _sim(bars, is_long, entry, stop, tps):
    """Три цели по трети, после TP1 стоп в безубыток, после TP2 на TP1.
    В неоднозначной свече первым считается стоп — консервативно."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    tg = list(tps)
    for i in range(1, 3):
        if is_long and tg[i] <= tg[i - 1]:
            tg[i] = tg[i - 1] * 1.001
        if not is_long and tg[i] >= tg[i - 1]:
            tg[i] = tg[i - 1] * 0.999
    done, cur_stop, acc = 0, stop, 0.0
    cost = FEE_PCT / 100 * entry / risk
    for c in bars:
        if (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop):
            fill = cur_stop * (1 - STOP_SLIP / 100) if is_long else cur_stop * (1 + STOP_SLIP / 100)
            r = (fill - entry) / risk if is_long else (entry - fill) / risk
            return acc + r * sum(PARTS[done:]) - cost
        while done < 3:
            t = tg[done]
            if (c["h"] >= t) if is_long else (c["l"] <= t):
                acc += PARTS[done] * (abs(t - entry) / risk)
                done += 1
                if done == 1:
                    cur_stop = entry
                elif done == 2:
                    cur_stop = tg[0]
            else:
                break
        if done >= 3:
            return acc - cost
    last = bars[-1]["c"] if bars else entry
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return acc + r * sum(PARTS[done:]) - cost


def _stat(rs):
    if not rs:
        return None
    n = len(rs)
    m = sum(rs) / n
    se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
    return n, m, 1.96 * se, sum(1 for r in rs if r > 0) / n * 100


def _line(rs, label, ctl=None):
    st = _stat(rs)
    if not st:
        return f"  {label}: событий нет"
    n, m, ci, wr = st
    mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
    s = f"  {mark} {label}: {n:4} соб., в плюс {wr:3.0f}%, <b>{m:+.3f}R</b> (±{ci:.3f})"
    sc = _stat(ctl) if ctl else None
    if sc:
        s += f" | монетка {sc[1]:+.3f}R, эдж {m - sc[1]:+.3f}R"
    return s


def backtest(depth):
    import random as _rnd
    rng = _rnd.Random(777)
    days = min(DAYS, depth)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / STEP))
    t0 = time.time()

    events = []
    n_pairs = 0
    for i, sym in enumerate(pairs, 1):
        try:
            stats = _fetch_stats(sym, days)
            if len(stats) < 300:
                continue
            fine = _fetch_candles(sym, days)
        except Exception:
            continue
        if len(fine) < 300:
            continue
        n_pairs += 1
        idx = {c.get("t"): k for k, c in enumerate(fine)}
        oi_key = "open_interest"
        if not any(float(r.get(oi_key) or 0) > 0 for r in stats[:50]):
            oi_key = "open_interest_usd"
        ll = [float(r.get("long_liq_usd") or 0) for r in stats]
        ls = [float(r.get("short_liq_usd") or 0) for r in stats]
        ts = [int(r.get("time") or 0) for r in stats]
        oi = [float(r.get(oi_key) or 0) for r in stats]
        if sum(ll) + sum(ls) <= 0:
            continue
        # медиана часовой суммы — база для «спайка»
        hl = [sum(ll[j - 12:j]) for j in range(12, len(ll) + 1)]
        hs = [sum(ls[j - 12:j]) for j in range(12, len(ls) + 1)]
        med_l = statistics.median(hl) if hl else 0.0
        med_s = statistics.median(hs) if hs else 0.0
        last_end = 0
        for j in range(12, len(stats) - 1):
            t_sig = ts[j] + STEP            # час закончился на этой строке, входим со следующей
            if t_sig <= last_end:
                continue
            liq_l = sum(ll[j - 12:j])
            liq_s = sum(ls[j - 12:j])
            if liq_l >= max(LIQ_MULT * med_l, LIQ_MIN) and liq_l >= liq_s * 2:
                side, liq_usd, med = "long", liq_l, med_l
            elif liq_s >= max(LIQ_MULT * med_s, LIQ_MIN) and liq_s >= liq_l * 2:
                side, liq_usd, med = "short", liq_s, med_s
            else:
                continue
            if oi[j - 12] <= 0:
                continue
            oi_d = (oi[j] / oi[j - 12] - 1) * 100
            if oi_d > -OI_DROP:
                continue
            k = idx.get(t_sig)
            if k is None or k < 13 or k + hold + 2 >= len(fine):
                continue
            win = fine[k - 12:k]
            if len(win) < 12:
                continue
            is_l = side == "long"
            o0, c_last = win[0]["o"], win[-1]["c"]
            if o0 <= 0:
                continue
            move = (c_last / o0 - 1) * 100
            if is_l and (move > -MOVE_PCT or win[-1]["c"] <= win[-1]["o"]):
                continue
            if not is_l and (move < MOVE_PCT or win[-1]["c"] >= win[-1]["o"]):
                continue
            px = fine[k]["o"]
            ext = min(c["l"] for c in win) if is_l else max(c["h"] for c in win)
            stop = ext * (1 - STOP_BUF / 100) if is_l else ext * (1 + STOP_BUF / 100)
            stop_pct = abs(stop / px - 1) * 100
            if stop_pct > MAX_STOP or stop_pct < 0.1:
                continue
            ma = abs(move)
            events.append((sym, t_sig, side, px, stop, ma,
                           fine[k:k + hold], fine[k:k + 49], liq_usd, med, oi_d))
            last_end = fine[min(k + hold, len(fine) - 1)].get("t", 0)
        if i % 5 == 0:
            print(f"[FLUSH] {i}/{len(pairs)} | пар с данными {n_pairs} | "
                  f"событий {len(events)} | {time.time() - t0:.0f}с")

    L = ["", "═══ <b>FLUSH: откат после каскада ликвидаций</b> ═══",
         f"<i>период {days} дн, пар с данными {n_pairs}, событий {len(events)}</i>",
         f"пороги: ликвидации ≥{LIQ_MULT}× медианы (мин ${LIQ_MIN:,.0f}), "
         f"OI ≤ -{OI_DROP}%/ч, ход ≥{MOVE_PCT}%, стоп ≤{MAX_STOP}%",
         f"издержки {FEE_PCT}%, проскальзывание {SLIP}%", ""]
    if len(events) < 20:
        L.append("⚠️ Событий слишком мало для вывода. Либо пороги строгие, либо "
                 "история короткая. Попробуй FL_MULT=3, FL_MOVE=1.5, FL_MIN_USD=1000")
        return L

    def run(coin=False, mult=None, move_min=None):
        out = []
        for sym, t_sig, side, px, stop, ma, seg, tail, liq, med, oi_d in events:
            if mult is not None and med > 0 and liq < mult * med:
                continue
            if move_min is not None and ma < move_min:
                continue
            sd = rng.choice(("long", "short")) if coin else side
            is_l = sd == "long"
            st = stop if sd == side else (px * (1 - MAX_STOP / 100) if is_l
                                          else px * (1 + MAX_STOP / 100))
            ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
            tps = ([px * (1 + ma * k / 100) for k in TP_K] if is_l
                   else [px * (1 - ma * k / 100) for k in TP_K])
            r = _sim(seg, is_l, ent, st, tps)
            if r is not None:
                out.append(r)
        return out

    # чистое движение после каскада — без стопов и целей
    L.append("<b>1. ЕСТЬ ЛИ ОТКАТ ВООБЩЕ</b> (без стопов и целей)")
    L.append("  <i>средний ход в сторону отката через N часов, % от входа</i>")
    for hh, nb in ((1, 12), (2, 24), (4, 48)):
        mv = []
        for sym, t_sig, side, px, stop, ma, seg, tail, liq, med, oi_d in events:
            if len(tail) <= nb or px <= 0:
                continue
            m = (tail[nb]["c"] - px) / px * 100
            mv.append(m if side == "long" else -m)
        if len(mv) < 20:
            continue
        n = len(mv)
        avg = sum(mv) / n
        se = statistics.pstdev(mv) / (n ** 0.5) if n > 1 else 0
        mark = "✅" if avg - 1.96 * se > 0 else "❌" if avg + 1.96 * se < 0 else "  "
        L.append(f"  {mark} через {hh}ч: {n:4} соб., <b>{avg:+.3f}%</b> (±{1.96 * se:.3f}), "
                 f"в плюс {sum(1 for x in mv if x > 0) / n * 100:.0f}%")
    L.append("")

    L.append("<b>2. СДЕЛКИ</b> (цели — доли каскадного хода, стоп за экстремумом)")
    L.append(_line(run(), "все события", run(coin=True)))
    for m in (8, 12, 20):
        rs = run(mult=m)
        if len(rs) >= 20:
            L.append(_line(rs, f"ликвидации ≥{m}× медианы", run(coin=True, mult=m)))
    for mv_ in (3.5, 5.0):
        rs = run(move_min=mv_)
        if len(rs) >= 20:
            L.append(_line(rs, f"каскадный ход ≥{mv_}%", run(coin=True, move_min=mv_)))
    L.append("")
    L.append("<i>критерий: матожидание ≥ +0.15R, значимо, и эдж над монеткой "
             "заметно больше нуля</i>")
    return L


def run():
    depth, fields, L = probe()
    # Разведку шлём СРАЗУ, не дожидаясь бэктеста: ответ на главный вопрос (есть ли
    # история) уже получен, а бэктест на многих парах — это тысячи запросов и минуты.
    _send(L)

    out = []
    if PROBE_ONLY:
        out.append("ℹ️ FL_PROBE_ONLY=1 — бэктест пропущен")
    elif depth < 3:
        out.append("→ Истории нет, бэктест невозможен. Остаётся форвард: "
                   "flush_hunter копит сигналы в реальном времени")
    else:
        n_pairs = PAIRS_N or len(B.UPSCALE_PAIRS)
        print(f"[FLUSH] история {depth} дн → бэктест на {min(DAYS, depth)} дн, {n_pairs} пар")
        try:
            out = backtest(depth)
        except Exception as e:
            import traceback
            traceback.print_exc()
            out = [f"⚠️ Бэктест упал: {e}"]
    _send(out)


def _send(lines):
    if not lines:
        return
    msg = "\n".join(lines)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[FLUSH] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ FLUSH упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
