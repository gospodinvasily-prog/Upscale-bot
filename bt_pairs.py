"""
bt_magnet.py — КАРТА ЛИКВИДАЦИЙ: работает ли «магнит».

Идея: на графике есть уровни, где стоят чужие ликвидации. Цена тянется к крупному
скоплению, а у самого уровня идёт быстрый ход. Входим ДО того, как цена туда пришла,
и забираем тейк чуть раньше уровня — чтобы не опаздывать, как при импульсе.

ЧТО ЭТО ЗА КАРТА. Gate не отдаёт карту ликвидаций. Её строят расчётом — так же, как
Coinglass: из открытого интереса, цены и модели плеч. Здесь модель такая:
  1. Прирост OI на баре = новые позиции, вошедшие по цене этого бара.
  2. Позиция делится пополам на лонг и шорт и раскладывается по плечам
     (5x, 10x, 25x, 50x, 100x с весами). Для каждого плеча считается цена ликвидации.
  3. Уровень, который цена уже прошла, ВЫЧЁРКИВАЕТСЯ — позиции ликвидированы.
  4. Падение OI уменьшает все веса пропорционально, плюс постепенное затухание
     старых позиций (период полураспада PM_HALF_LIFE_D дней).
  5. Веса складываются по ценовым корзинам — получаются скопления выше и ниже цены.

ВАЖНО: это МОДЕЛЬ. Распределение плеч, доля лонгов/шортов и момент закрытия позиций
неизвестны и нигде не проверяются. Поэтому сначала проверяем САМ ЭФФЕКТ на истории
(она есть на ~60 дней): карта на момент T строится ТОЛЬКО по данным до T.

ЧТО МЕРИМ (главное — первое):
  1. ПЕРВЫЙ КАСАНИЕ. Берём скопление на расстоянии d%. Рядом — зеркальный уровень на
     том же расстоянии в ДРУГУЮ сторону. Какой уровень цена достигла первым?
     Если притяжения нет — 50/50. Это контроль на дрейф рынка.
  2. ДОЗА-ЭФФЕКТ. Сильнее скопление — чаще ли цена идёт к нему? Если нет зависимости
     от силы, то и эффекта «карты» нет.
  3. СДЕЛКИ. Вход к скоплению, тейк чуть раньше уровня, стоп фиксированный.
     Рядом монетка с теми же дистанциями.

Тайминг честный: карта по закрытым часовым барам, вход по открытию следующей 15м свечи.
Строки статистики берутся с лагом в один бар — чтобы не заглянуть вперёд при любой
трактовке поля time.

Настройки (переменные Render, все необязательные):
  PM_DAYS (65)  PM_WARM_D (12)  PM_PAIRS (0=все)  PM_SAMPLE_H (12)  PM_HORIZON_H (12)
  PM_MIN_D (1.0)  PM_MAX_D (6.0)  PM_BUCKET (0.25)  PM_HALF_LIFE_D (3)  PM_MMR (0.5)
  PM_LEV ("5:0.15,10:0.30,25:0.30,50:0.15,100:0.10")  PM_STOP (1.0)  PM_BUF (0.15)
"""
import os
import math
import time
import random
import statistics

import bot as B

DAYS      = int(os.environ.get("PM_DAYS", "65"))
WARM_D    = int(os.environ.get("PM_WARM_D", "12"))
PAIRS_N   = int(os.environ.get("PM_PAIRS", "0"))
SAMPLE_H  = int(os.environ.get("PM_SAMPLE_H", "12"))   # = горизонт: наблюдения не перекрываются
HORIZON_H = int(os.environ.get("PM_HORIZON_H", "12"))
MIN_D     = float(os.environ.get("PM_MIN_D", "1.0"))
MAX_D     = float(os.environ.get("PM_MAX_D", "6.0"))
BW        = float(os.environ.get("PM_BUCKET", "0.25")) / 100
HL_D      = float(os.environ.get("PM_HALF_LIFE_D", "3"))
MMR       = float(os.environ.get("PM_MMR", "0.5")) / 100
STOP_PCT  = float(os.environ.get("PM_STOP", "1.0"))
BUF_PCT   = float(os.environ.get("PM_BUF", "0.15"))
SLIP      = float(os.environ.get("PM_SLIP", "0.10"))
COST      = float(os.environ.get("PM_COST", "0.065"))
STOP_SLIP = float(os.environ.get("PM_STOP_SLIP", "0.05"))


def _parse_lev(s):
    out = []
    for part in s.split(","):
        part = part.strip()
        if ":" in part:
            a, b = part.split(":")
            out.append((float(a), float(b)))
    tot = sum(w for _, w in out) or 1.0
    return [(l, w / tot) for l, w in out]


LEV = _parse_lev(os.environ.get("PM_LEV", "5:0.15,10:0.30,25:0.30,50:0.15,100:0.10"))
LOGBW = math.log(1 + BW)


# ═════════════ данные ═════════════

def _fetch_candles(sym, tf, days):
    sec = {"15m": 900, "1h": 3600}[tf]
    now = int(time.time())
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        to = min(now, cur + 1900 * sec)
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": tf,
                                         "from": cur, "to": to})
        part = B.parse_candles(raw) if raw else []
        if not part:
            probes += 1
            if probes > 25:
                break
            cur += 5 * 86400
            continue
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


def _fetch_oi(sym, days):
    """Открытый интерес по часам: {начало_часа: OI}. Берём контракты, если они есть,
    иначе доллары — главное, чтобы ряд был один и тот же."""
    now = int(time.time())
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        to = min(now, cur + 100 * 3600)
        raw = B.api_get("contract_stats", {"contract": f"{sym}_USDT", "interval": "1h",
                                           "from": cur, "to": to, "limit": 100})
        rows = raw if isinstance(raw, list) else []
        if not rows:
            probes += 1
            if probes > 20:
                break
            cur += 5 * 86400
            continue
        out.extend(rows)
        nxt = max(int(B.fnum(r.get("time", 0))) for r in rows) + 3600
        if nxt <= cur:
            break
        cur = nxt
    use_contracts = out and all(B.fnum(r.get("open_interest", 0)) > 0 for r in out)
    res = {}
    for r in out:
        t = int(B.fnum(r.get("time", 0)))
        if t <= 0:
            continue
        v = B.fnum(r.get("open_interest", 0)) if use_contracts else \
            B.fnum(r.get("open_interest_usd", 0))
        if v > 0:
            res[t - t % 3600] = v
    return res


# ═════════════ карта ═════════════

def _typical(c):
    return (c["h"] + c["l"] + c["c"]) / 3


def build_clusters(c1h, oi_by_t, first_sample_i):
    """Идём по часовым барам, ведём карту и в выбранные моменты записываем
    самое сильное скопление ВЫШЕ и НИЖЕ цены. Карта на бар i использует данные
    только до бара i включительно, строку OI берём с этим же временем (лаг ≤ 1 бар)."""
    n = len(c1h)
    if n < 50:
        return {}
    ref = c1h[0]["c"]
    if ref <= 0:
        return {}

    def bidx(p):
        return int(math.floor(math.log(p / ref) / LOGBW))

    def bprice(b):
        return ref * math.exp((b + 0.5) * LOGBW)

    longs, shorts = {}, {}          # корзина -> вес (ликвидации лонгов / шортов)
    decay = 0.5 ** (1.0 / (HL_D * 24))
    prev_oi = None
    out = {}

    def best(dct, lo_p, hi_p):
        if not dct:
            return None
        b_lo, b_hi = bidx(lo_p), bidx(hi_p)
        top_w, top_b = 0.0, None
        for b in range(b_lo, b_hi + 1):
            w = dct.get(b - 1, 0.0) + dct.get(b, 0.0) + dct.get(b + 1, 0.0)
            if w > top_w:
                top_w, top_b = w, b
        if top_b is None:
            return None
        num = den = 0.0
        for bb in (top_b - 1, top_b, top_b + 1):
            ww = dct.get(bb, 0.0)
            num += ww * bprice(bb)
            den += ww
        return (num / den, den) if den > 0 else None

    for i in range(n):
        c = c1h[i]
        oi = oi_by_t.get(c["t"])
        d_oi, f_oi = 0.0, 1.0
        if oi and prev_oi:
            d_oi = oi - prev_oi
            if d_oi < 0:
                f_oi = oi / prev_oi
        f = decay * f_oi
        for dct in (longs, shorts):
            for k in list(dct):
                w = dct[k] * f
                if w < 1e-15:
                    del dct[k]
                else:
                    dct[k] = w
        if d_oi > 0 and i > 0:
            prev = c1h[i - 1]
            p = _typical(prev)                # позиции набирались на предыдущем баре
            # Если этот же бар уже задел уровень — позиция успела ликвидироваться, в карту
            # её не кладём. Раньше такие позиции попадали в карту и вычёркивались лишь
            # СЛЕДУЮЩИМ баром, а мёртвые уровни вблизи цены искажали скопления.
            lo_prev = bidx(prev["l"]) if prev["l"] > 0 else None
            hi_prev = bidx(prev["h"]) if prev["h"] > 0 else None
            for lev, wt in LEV:
                ql = p * (1 - 1 / lev + MMR)
                qs = p * (1 + 1 / lev - MMR)
                if ql > 0:
                    b = bidx(ql)
                    if lo_prev is None or b < lo_prev:
                        longs[b] = longs.get(b, 0.0) + d_oi * 0.5 * wt
                if qs > 0:
                    b = bidx(qs)
                    if hi_prev is None or b > hi_prev:
                        shorts[b] = shorts.get(b, 0.0) + d_oi * 0.5 * wt
        # уровни, которые цена прошла на этом баре, вычёркиваем: позиции ликвидированы
        if c["l"] > 0 and c["h"] > 0:
            lo_b, hi_b = bidx(c["l"]), bidx(c["h"])
            for k in [k for k in longs if k >= lo_b]:
                del longs[k]
            for k in [k for k in shorts if k <= hi_b]:
                del shorts[k]
        if oi:
            prev_oi = oi
        if i >= first_sample_i and (c["t"] // 3600) % SAMPLE_H == 0 and prev_oi:
            P = c["c"]
            up = best(shorts, P * (1 + MIN_D / 100), P * (1 + MAX_D / 100))
            dn = best(longs, P * (1 - MAX_D / 100), P * (1 - MIN_D / 100))
            out[i] = {"up": up, "dn": dn, "oi": prev_oi, "P": P}
    return out


# ═════════════ сделка и касания ═════════════

def sim_single(path, is_long, entry, stop, tp):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    cost = COST / 100 * entry / risk
    for c in path:
        if (c["l"] <= stop) if is_long else (c["h"] >= stop):       # в спорной свече — стоп
            fill = stop * (1 - STOP_SLIP / 100) if is_long else stop * (1 + STOP_SLIP / 100)
            r = (fill - entry) / risk if is_long else (entry - fill) / risk
            return r - cost
        if (c["h"] >= tp) if is_long else (c["l"] <= tp):
            r = (tp - entry) / risk if is_long else (entry - tp) / risk
            return r - cost
    last = path[-1]["c"] if path else entry
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return r - cost


def touch_result(direction, P, d_pct, path):
    """Какой уровень цена достигла первым: к скоплению или зеркальный на том же расстоянии.
    Возвращает (первый, достигнут_к_скоплению, достигнут_зеркальный)."""
    if direction == "up":
        lt, lo = P * (1 + d_pct / 100), P * (1 - d_pct / 100)
    else:
        lt, lo = P * (1 - d_pct / 100), P * (1 + d_pct / 100)
    hit_t = hit_o = False
    first = None
    for c in path:
        ht = (c["h"] >= lt) if direction == "up" else (c["l"] <= lt)
        ho = (c["l"] <= lo) if direction == "up" else (c["h"] >= lo)
        if first is None and (ht or ho):
            first = "both" if (ht and ho) else ("toward" if ht else "opp")
        hit_t = hit_t or ht
        hit_o = hit_o or ho
    return first, hit_t, hit_o


def _mean_ci(vals):
    if not vals:
        return None
    n = len(vals)
    m = sum(vals) / n
    se = (statistics.pstdev(vals) / (n ** 0.5)) if n > 1 else 0.0
    return n, m, 1.96 * se


Z99 = 2.58      # отметки ✅/❌ ставятся по 99%-интервалу: подгрупп много, при 95% часть
                # отметок возникает случайно (на случайном блуждании так и вышло)


def share_stats(obs):
    """(наблюдений, доля «первым к скоплению», полуширина интервала ПО ВРЕМЕНИ).
    Интервал по времени честнее: наблюдения одного момента связаны через общий рынок."""
    dec = [o for o in obs if o["first"] in ("toward", "opp")]
    if len(dec) < 30:
        return None
    by_t = {}
    for o in dec:
        by_t.setdefault(o["T"], []).append(1.0 if o["first"] == "toward" else 0.0)
    tm = [sum(v) / len(v) for v in by_t.values()]
    ci = Z99 * statistics.pstdev(tm) / (len(tm) ** 0.5) if len(tm) > 1 else 0.0
    p = sum(1 for o in dec if o["first"] == "toward") / len(dec)
    return len(dec), p, ci


def _share_line(label, obs):
    st = share_stats(obs)
    if not st:
        return f"  {label}: мало наблюдений"
    n, p, ci = st
    rt = sum(1 for o in obs if o["hit_t"]) / len(obs) * 100
    ro = sum(1 for o in obs if o["hit_o"]) / len(obs) * 100
    mark = "✅" if p - ci > 0.5 else "❌" if p + ci < 0.5 else "  "
    return (f"  {mark} {label}: {n:5} набл., первым к скоплению <b>{p * 100:.1f}%</b> "
            f"(±{ci * 100:.1f}) | достигнут: скопление {rt:.0f}% / зеркало {ro:.0f}%")


def verdict(obs, top, low, trades_top):
    """Три условия сразу — иначе это шум. Возвращает (подтверждено, строки)."""
    sa, st_, sl = share_stats(obs), share_stats(top), share_stats(low)
    c1 = bool(sa and sa[1] - sa[2] > 0.5)
    c2 = bool(st_ and sl and st_[1] > 0.5 and st_[1] - sl[1] >= 0.02)
    c3 = bool(trades_top and trades_top[1] - trades_top[2] > 0)
    rows = [f"  {'✅' if c1 else '❌'} 1) общая доля значимо выше 50%"
            + (f" ({sa[1] * 100:.1f}%, ±{sa[2] * 100:.1f})" if sa else ""),
            f"  {'✅' if c2 else '❌'} 2) у сильных скоплений доля выше, чем у слабых"
            + (f" ({st_[1] * 100:.1f}% против {sl[1] * 100:.1f}%)" if st_ and sl else ""),
            f"  {'✅' if c3 else '❌'} 3) сделки по сильным значимо в плюсе"
            + (f" ({trades_top[1]:+.3f}R, ±{trades_top[2]:.3f})" if trades_top else "")]
    return (c1 and c2 and c3), rows


# ═════════════ прогон ═════════════

def run():
    rng = random.Random(4242)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold_bars = HORIZON_H * 4
    t0 = time.time()
    obs = []
    n_pairs = 0

    for i, sym in enumerate(pairs, 1):
        try:
            c1h = _fetch_candles(sym, "1h", DAYS)
            f15 = _fetch_candles(sym, "15m", DAYS)
            oi = _fetch_oi(sym, DAYS)
        except Exception:
            continue
        if len(c1h) < (WARM_D + 8) * 24 or len(f15) < 800 or len(oi) < (WARM_D + 8) * 12:
            continue
        c1h, f15 = c1h[:-1], f15[:-1]
        n_pairs += 1
        idx15 = {c["t"]: k for k, c in enumerate(f15)}
        cl = build_clusters(c1h, oi, WARM_D * 24)
        for bi, m in cl.items():
            T = c1h[bi]["t"] + 3600
            k = idx15.get(T)
            if k is None or k + hold_bars >= len(f15):
                continue
            Pe = f15[k]["o"]
            if Pe <= 0 or m["oi"] <= 0:
                continue
            cand = []
            for side, key in (("up", "up"), ("dn", "dn")):
                cc = m[key]
                if not cc:
                    continue
                lvl, w = cc
                d = abs(lvl - Pe) / Pe * 100
                if MIN_D <= d <= MAX_D:
                    cand.append({"dir": side, "level": lvl, "d": d, "s": w / m["oi"]})
            if not cand:
                continue
            best_c = max(cand, key=lambda x: x["s"])
            path = f15[k:k + hold_bars]
            first, ht, ho = touch_result(best_c["dir"], Pe, best_c["d"], path)
            obs.append({"sym": sym, "T": T, "dir": best_c["dir"], "d": best_c["d"],
                        "level": best_c["level"], "s": best_c["s"], "Pe": Pe,
                        "path": path, "first": first, "hit_t": ht, "hit_o": ho})
        if i % 10 == 0:
            print(f"[MAGNET] {i}/{len(pairs)} | пар {n_pairs} | наблюдений {len(obs)} | "
                  f"{time.time() - t0:.0f}с")

    L = [f"🧲 <b>КАРТА ЛИКВИДАЦИЙ: работает ли магнит</b> ({n_pairs} пар, ~{DAYS - WARM_D} дн "
         f"после прогрева)",
         "<i>карта строится расчётом из OI и цены и на момент T использует только данные до T. "
         "Это МОДЕЛЬ, а не реальные чужие позиции</i>",
         f"Плечи: {', '.join(f'{int(l)}x:{w:.2f}' for l, w in LEV)} | полураспад {HL_D}д | "
         f"корзина {BW * 100:.2f}% | окно {MIN_D}–{MAX_D}% | горизонт {HORIZON_H}ч",
         f"Наблюдений: {len(obs)} (раз в {SAMPLE_H}ч на пару)", ""]
    if len(obs) < 200:
        L.append("⚠️ Наблюдений мало для выводов. Проверь PM_DAYS, PM_MIN_D, PM_PAIRS")
        _send(L)
        return

    # ── 1. первое касание ──
    L.append("<b>1. ЧТО ЦЕНА ДОСТИГАЕТ ПЕРВЫМ: СКОПЛЕНИЕ ИЛИ ЗЕРКАЛО</b>")
    L.append("  <i>зеркало — уровень на том же расстоянии в другую сторону. Без притяжения "
             "будет 50%. ✅/❌ — только если интервал по ВРЕМЕНИ не включает 50%</i>")
    L.append(_share_line("все наблюдения", obs))
    L.append(_share_line("магнит ВВЕРХ (шорты над ценой)", [o for o in obs if o["dir"] == "up"]))
    L.append(_share_line("магнит ВНИЗ (лонги под ценой)", [o for o in obs if o["dir"] == "dn"]))
    L.append("")

    # ── 2. доза-эффект ──
    ss = sorted(o["s"] for o in obs)
    q33, q66 = ss[len(ss) // 3], ss[2 * len(ss) // 3]
    L.append("<b>2. ЗАВИСИТ ЛИ ОТ СИЛЫ СКОПЛЕНИЯ</b> (сила — доля OI на уровне, по модели)")
    L.append("  <i>настоящий эффект растёт с силой. Если слабые скопления работают так же — "
             "дело не в карте</i>")
    low = [o for o in obs if o["s"] <= q33]
    mid = [o for o in obs if q33 < o["s"] <= q66]
    top = [o for o in obs if o["s"] > q66]
    L.append(_share_line(f"слабые (≤{q33 * 100:.2f}% OI)", low))
    L.append(_share_line(f"средние", mid))
    L.append(_share_line(f"сильные (>{q66 * 100:.2f}% OI)", top))
    L.append("")

    # ── 3. расстояние ──
    L.append("<b>3. ПО РАССТОЯНИЮ ДО СКОПЛЕНИЯ</b>")
    for a, b in ((MIN_D, 2.0), (2.0, 4.0), (4.0, MAX_D)):
        sel = [o for o in obs if a <= o["d"] < b]
        if sel:
            L.append(_share_line(f"{a:.0f}–{b:.0f}%", sel))
    L.append("")

    # ── 4. сделки ──
    L.append("<b>4. СДЕЛКИ</b> (вход к скоплению, тейк на "
             f"{BUF_PCT}% раньше уровня, стоп {STOP_PCT}% против)")

    def trades(sel, coin=False):
        out = []
        for o in sel:
            dtp = o["d"] - BUF_PCT
            if dtp < 0.8:
                continue
            up = o["dir"] == "up"
            if coin:
                up = rng.random() < 0.5
            is_long = up
            Pe = o["Pe"]
            ent = Pe * (1 + SLIP / 100) if is_long else Pe * (1 - SLIP / 100)
            stp = Pe * (1 - STOP_PCT / 100) if is_long else Pe * (1 + STOP_PCT / 100)
            tp = Pe * (1 + dtp / 100) if is_long else Pe * (1 - dtp / 100)
            r = sim_single(o["path"], is_long, ent, stp, tp)
            if r is not None:
                out.append(r)
        return out

    def trade_line(label, sel):
        rs, cs = trades(sel), trades(sel, coin=True)
        st, sc = _mean_ci(rs), _mean_ci(cs)
        if not st or st[0] < 30:
            return f"  {label}: мало сделок"
        n, m, ci = st
        wr = sum(1 for r in rs if r > 0) / n * 100
        mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
        tail = f" | монетка {sc[1]:+.3f}R, эдж {m - sc[1]:+.3f}R" if sc else ""
        return f"  {mark} {label}: {n:5} сд, ВР {wr:3.0f}%, <b>{m:+.3f}R</b> (±{ci:.3f}){tail}"

    L.append(trade_line("все наблюдения", obs))
    L.append(trade_line("сильные скопления", top))
    L.append(trade_line("слабые скопления", low))
    L.append(trade_line("сильные, магнит вверх (лонг)", [o for o in top if o["dir"] == "up"]))
    L.append(trade_line("сильные, магнит вниз (шорт)", [o for o in top if o["dir"] == "dn"]))
    L.append("  <i>по расстоянию до цели, только сильные — стоп 1% душит ближние цели:</i>")
    for a, b in ((MIN_D, 2.0), (2.0, 4.0), (4.0, MAX_D)):
        L.append(trade_line(f"сильные, {a:.0f}–{b:.0f}%", [o for o in top if a <= o["d"] < b]))
    L.append("")

    # ── вывод ──
    rs_top = trades(top)
    st_top = _mean_ci(rs_top) if len(rs_top) >= 30 else None
    ok, rows = verdict(obs, top, low, st_top)
    L.append("<b>ВЫВОД</b> (три условия СРАЗУ, интервалы 99%)")
    L += rows
    L.append("  → <b>" + ("ПРИТЯЖЕНИЕ ПОДТВЕРЖДЕНО" if ok else "ПРИТЯЖЕНИЕ НЕ ПОДТВЕРЖДЕНО") + "</b>")
    L.append("  <i>ОГОВОРКА: цена и без всякой карты любит возвращаться к уровням, где недавно "
             "торговалась. Даже при подтверждении карту надо сравнить с такой же, построенной "
             "по объёму, а не по OI — иначе неясно, что именно притягивает</i>")
    _send(L)


def _send(L):
    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[MAGNET] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон «магнит» упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
