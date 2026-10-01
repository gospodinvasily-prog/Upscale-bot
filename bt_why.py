"""
bt_why.py — почему монета НЕ стала зарядом.

Заряд проходит шесть ворот подряд, и любое отсеивает молча. Этот прогон считает,
какое именно ворото режет чаще всего, и что даст его ослабление.

Два режима:
  1) ВОРОНКА по всем парам: сколько проверок прошло каждое условие и на каком
     шаге теряется основная масса.
  2) РАЗБОР КОНКРЕТНЫХ МОНЕТ (WHY_SYMS): для каждого часа перед движением —
     фактические значения против порогов. Видно, чего именно не хватило.

Плюс перебор ослаблений: что будет, если снизить порог объёма, порог силы,
убрать требование роста объёма и т.д. — сколько зарядов добавится.

Запуск: RUN_BACKTEST=why
Настройки: WHY_DAYS (14), WHY_SYMS ("PENDLE,ENA,TRUMP,AAVE,JASMY,KAIA,MOVE"), WHY_PAIRS (0=все)
"""
import os
import time
import statistics

import bot as B

DAYS   = int(os.environ.get("WHY_DAYS", "14"))
PAIRS_N = int(os.environ.get("WHY_PAIRS", "0"))
SYMS   = [s.strip().upper() for s in os.environ.get(
    "WHY_SYMS", "PENDLE,ENA,TRUMP,AAVE,JASMY,KAIA,MOVE").split(",") if s.strip()]

GATES = ["данных мало", "цена стоит", "сжатие", "объём/OI", "сила заряда", "уклон ясен", "ПРОШЁЛ"]


def _fetch(sym, tf, days):
    sec = {"1h": 3600, "15m": 900}[tf]
    now = int(time.time())
    out, cur = [], now - days * 86400
    while cur < now:
        to = min(now, cur + 1900 * sec)
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": tf,
                                         "from": cur, "to": to})
        part = B.parse_candles(raw) if raw else []
        if not part:
            break
        out.extend(part)
        nxt = part[-1].get("t", 0) + sec
        if nxt <= cur:
            break
        cur = nxt
    seen, uniq = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t") not in seen:
            seen.add(c.get("t"))
            uniq.append(c)
    return uniq


def _check(closed, price, baseline, atr_norm, W):
    """Повторяет ворота detect_charge по шагам. Возвращает (номер_ворот, значения)."""
    v = {}
    if len(closed) < 60 or atr_norm <= 0 or baseline <= 0:
        return 0, v
    win = closed[-W:]
    hi = max(c["h"] for c in win)
    lo = min(c["l"] for c in win)
    if lo <= 0:
        return 0, v
    height = hi - lo
    rng_pct = height / lo * 100
    move = win[-1]["c"] - win[0]["o"]
    atr_pct = atr_norm / price * 100 if price > 0 else 0
    exp_range = B.ACC_RANGE_ATR_K * atr_pct * (W ** 0.5)
    max_range = min(B.ACC_MAX_RANGE_ABS, max(B.ACC_RANGE_FLOOR_PCT, exp_range))
    v.update({"move_atr": abs(move) / atr_norm if atr_norm else 99,
              "flat_lim": B.ALT_P["flat_atr_eff"], "rng": rng_pct, "rng_lim": max_range})
    if abs(move) > B.ALT_P["flat_atr_eff"] * atr_norm or rng_pct > max_range:
        return 1, v

    closes = [c["c"] for c in closed]
    sq = B.bb_width_percentile(closes)
    trs = B.true_ranges(closed)
    tr_ratio = (sum(trs[-W:]) / W) / atr_norm if atr_norm else 99
    v.update({"sq": sq, "sq_lim": B.ACC_SQUEEZE_PCTL, "tr": tr_ratio, "tr_lim": B.ACC_TR_RATIO_MAX})
    squeezed = (sq is not None and sq <= B.ACC_SQUEEZE_PCTL) or tr_ratio <= B.ACC_TR_RATIO_MAX
    if not squeezed:
        return 2, v

    half = W // 2
    vol_half = sum(c["v"] for c in win[-half:]) / half
    vol_prev = sum(c["v"] for c in win[:half]) / half
    rvol = vol_half / baseline if baseline else 0
    v.update({"rvol": rvol, "rvol_lim": B.ACC_RVOL_MIN,
              "vol_rising": vol_half > vol_prev * 1.15})
    # OI за историю не восстановить — считаем только по объёму (как и живой бот без OI)
    if rvol < B.ACC_RVOL_MIN:
        return 3, v

    # приблизительная сила (без OI, тейкеров, фандинга — их в истории нет)
    sc = 0
    if sq is not None and sq <= 15:
        sc += 2
    elif sq is not None and sq <= B.ACC_SQUEEZE_PCTL:
        sc += 1
    if tr_ratio <= 0.6:
        sc += 1
    if rvol >= 2:
        sc += 2
    elif rvol >= B.ACC_RVOL_MIN:
        sc += 1
    if v["vol_rising"]:
        sc += 1
    v["score"] = sc
    v["score_lim"] = B.ACC_MIN_SCORE
    if sc < B.ACC_MIN_SCORE:
        return 4, v

    pos = min(max((price - lo) / height, 0.0), 1.0) if height > 0 else 0.5
    v["pos"] = pos
    return 6, v


def run():
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else list(B.UPSCALE_PAIRS)
    for s_ in SYMS:                      # разбираемые монеты сканируем всегда
        if s_ not in pairs:
            pairs.append(s_)
    W = B.ACC_WINDOW
    funnel = {i: 0 for i in range(7)}
    checks = 0
    loose = {"объём ≥1.0 вместо 1.3": 0, "объём ≥0.8": 0, "сила ≥5": 0, "сила ≥4": 0,
             "сжатие: процентиль ≤35": 0, "размах: предел 12%": 0, "всё вместе": 0}
    detail = {s: [] for s in SYMS}
    t0 = time.time()

    for i, sym in enumerate(pairs, 1):
        try:
            h1 = _fetch(sym, "1h", DAYS + 5)
        except Exception:
            continue
        if len(h1) < B.BASE_FROM + W + 5:
            continue
        h1 = h1[:-1]
        for e in range(B.BASE_FROM + W, len(h1)):
            upto = h1[:e]
            sl = upto[-B.BASE_FROM:-B.BASE_TO]
            if len(sl) < 10:
                continue
            vb = B.trimmed_mean([c["v"] for c in sl])
            ab = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
            if not vb or not ab:
                continue
            px = upto[-1]["c"]
            g, v = _check(upto, px, vb, ab, W)
            funnel[g] += 1
            checks += 1

            # что дало бы ослабление
            if g == 3 and v.get("rvol", 0) >= 1.0:
                loose["объём ≥1.0 вместо 1.3"] += 1
            if g == 3 and v.get("rvol", 0) >= 0.8:
                loose["объём ≥0.8"] += 1
            if g == 4 and v.get("score", 0) >= 5:
                loose["сила ≥5"] += 1
            if g == 4 and v.get("score", 0) >= 4:
                loose["сила ≥4"] += 1
            if g == 2 and v.get("sq") is not None and v["sq"] <= 35:
                loose["сжатие: процентиль ≤35"] += 1
            if g == 1 and v.get("rng", 99) <= 12 and v.get("move_atr", 99) <= v.get("flat_lim", 0):
                loose["размах: предел 12%"] += 1
            if g in (1, 2, 3, 4):
                ok = (v.get("rng", 99) <= 12 and v.get("move_atr", 99) <= v.get("flat_lim", 0)
                      and (v.get("sq") is None or v["sq"] <= 35 or v.get("tr", 9) <= 0.75)
                      and v.get("rvol", 0) >= 0.8 and v.get("score", 9) >= 4)
                if ok:
                    loose["всё вместе"] += 1

            if sym in detail and len(detail[sym]) < 400:
                detail[sym].append((upto[-1].get("t", 0), g, dict(v)))
        if i % 20 == 0:
            print(f"[WHY] {i}/{len(pairs)} | проверок {checks} | {time.time()-t0:.0f}с")

    took = time.time() - t0
    L = [f"🔍 <b>Почему монета не стала зарядом</b> ({DAYS} дн, {len(pairs)} пар)",
         f"Проверок (монета × час): {checks}, время {took/60:.1f} мин",
         "<i>OI, тейкеры и фандинг за историю не восстановить — сила считается "
         "без них и выходит ниже живой на 2-3</i>",
         "", "<b>ВОРОНКА — на каком условии теряем:</b>"]
    for i_, name in enumerate(GATES):
        n = funnel.get(i_, 0)
        if not checks:
            continue
        bar = "█" * max(0, round(n / checks * 30))
        L.append(f"  {name:14} {n:6} ({n/checks*100:5.1f}%) {bar}")
    L.append(f"  <i>последняя строка — сколько дошло до заряда</i>")

    L += ["", "<b>ЧТО ДАСТ ОСЛАБЛЕНИЕ</b> (сколько проверок добавилось бы):"]
    base = funnel.get(6, 0)
    for k, n in sorted(loose.items(), key=lambda kv: -kv[1]):
        extra = f" (к нынешним {base}, это +{n/base*100:.0f}%)" if base else " (сейчас зарядов 0)"
        L.append(f"  {k:26} +{n:5}{extra}")

    L += ["", "<b>РАЗБОР МОНЕТ</b> (последние часы перед сейчас):"]
    for s in SYMS:
        rows = detail.get(s) or []
        if not rows:
            L.append(f"  {s}: данных нет")
            continue
        passed = sum(1 for _, g, _ in rows if g == 6)
        from collections import Counter
        cnt = Counter(GATES[g] for _, g, _ in rows)
        top = ", ".join(f"{k} {v}" for k, v in cnt.most_common(3))
        L.append(f"  <b>{s}</b>: зарядов {passed} из {len(rows)} часов | чаще всего: {top}")
        last = rows[-1]
        v = last[2]
        if v:
            L.append(f"     последний час: размах {v.get('rng', 0):.1f}% (предел {v.get('rng_lim', 0):.1f}), "
                     f"ход {v.get('move_atr', 0):.1f} ATR (предел {v.get('flat_lim', 0):.1f})")
            if "sq" in v:
                L.append(f"     сжатие: процентиль {v.get('sq') if v.get('sq') is None else round(v['sq'])}"
                         f" (нужно ≤{v.get('sq_lim')}), свечи {v.get('tr', 0):.2f} (нужно ≤{v.get('tr_lim')})")
            if "rvol" in v:
                L.append(f"     объём {v['rvol']:.2f}× (нужно ≥{v.get('rvol_lim')}), "
                         f"растёт: {'да' if v.get('vol_rising') else 'нет'}")
            if "score" in v:
                L.append(f"     сила {v['score']} (нужно ≥{v.get('score_lim')})")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[WHY] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Диагностика упала: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
