"""
bt_entry.py — новая схема входа с ПОДТВЕРЖДЕНИЕМ.

Старый бэктест был испорчен подглядыванием: заряд считался по полной часовой
свече, а вход брался по цене на 15-й минуте этого же часа — то есть ДО движения,
о котором мы уже знали. После исправления прежний результат рассыпался.

Здесь тайминг честный:
  час закрылся в T  →  ждём до T+30мин  →  проверяем подтверждение
  по ЗАКРЫТЫМ 15м свечам  →  входим по открытию свечи T+30мин.

ПОДТВЕРЖДЕНИЕ (всё по закрытым данным):
  1. последняя закрытая 15м свеча ЗАКРЫЛАСЬ выше дневного VWAP (для лонга)
     или ниже (для шорта);
  2. цена шла в нашу сторону последние N минут (15 / 30 / 45);
  3. до дальней границы коридора ≥ порога (1.0% или 1.5%).

Если уклон заряда «both» — направление берём из самих фильтров: куда идёт цена
и с какой стороны от VWAP, туда и входим.

Стоп и цели не меняются: стоп 1%, три цели по трети (1% → граница → 2%),
стоп подтягивается после первой и второй цели.

Запуск: RUN_BACKTEST=entry
Настройки: BE_DAYS (60), BE_OFFSET (0), BE_PAIRS (0=все), BE_SLIP (0.25)
"""
import os
import time
import statistics
from datetime import datetime, timezone

import bot as B

DAYS    = int(os.environ.get("BE_DAYS", "60"))
OFFSET  = int(os.environ.get("BE_OFFSET", "0"))
PAIRS_N = int(os.environ.get("BE_PAIRS", "0"))
SLIP    = float(os.environ.get("BE_SLIP", "0.25"))
FEE_PCT = float(os.environ.get("BE_FEE", "0.05"))
FUND_PCT = float(os.environ.get("BE_FUND", "0.015"))
STOP_SLIP = float(os.environ.get("BE_STOP_SLIP", "0.15"))
HOLD_H  = int(os.environ.get("BE_HOLD_H", "12"))

STEP = 900                      # 15м — на них считаем вход и ведение
ENTRY_OFFSET_MIN = int(os.environ.get("BE_ENTRY_MIN", "30"))   # скан на :30
STOP, TP1, TP3 = 1.0, 1.0, 2.0
PARTS = (1 / 3, 1 / 3, 1 / 3)

RISE_GRID = [0, 15, 30, 45]     # сколько минут цена должна идти в нашу сторону (0 = не требуем)
DIST_GRID = [1.0, 1.5]          # ход до дальней границы
VWAP_MODES = ["строго", "не требуем"]   # закрытая 15м свеча по нужную сторону VWAP


def _fetch(sym, tf, days):
    sec = {"1h": 3600, "15m": 900}[tf]
    now = int(time.time()) - OFFSET * 86400
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


def _vwap_at(fine, k, bars_day):
    """Дневной VWAP по свечам ДО k включительно. Только прошлое."""
    lo = max(0, k - bars_day)
    seg = fine[lo:k + 1]
    vv = sum(c["v"] for c in seg)
    if not seg or vv <= 0:
        return None
    return sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in seg) / vv


def _sim(bars, side, entry, stop, t1, t2, t3):
    """Три цели по трети; стоп после TP1 в безубыток, после TP2 на цену TP1."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == "long"
    tg = [t1, t2, t3]
    for i in range(1, 3):
        if is_long and tg[i] <= tg[i - 1]:
            tg[i] = tg[i - 1] * 1.001
        if not is_long and tg[i] >= tg[i - 1]:
            tg[i] = tg[i - 1] * 0.999
    done, cur_stop, acc = 0, stop, 0.0
    cost = (FEE_PCT + FUND_PCT) / 100 * entry / risk
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


def _line(rs, label):
    if not rs:
        return f"  {label}: сделок нет"
    n = len(rs)
    exp = sum(rs) / n
    wins = sum(1 for r in rs if r > 0)
    gl = abs(sum(r for r in rs if r <= 0)) or 0.0
    pf = (sum(r for r in rs if r > 0) / gl) if gl > 0 else float("inf")
    se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
    mark = "✅" if exp - 1.96 * se > 0 else "❌" if exp + 1.96 * se < 0 else "  "
    return (f"  {mark} {label}: {n:4} сд, ВР {wins/n*100:3.0f}%, <b>{exp:+.3f}R</b> "
            f"(±{1.96*se:.3f}), ПФ {pf:.2f}, {sum(rs):+.0f}R")


def run():
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / STEP))
    bars_day = int(86400 / STEP)
    off_bars = ENTRY_OFFSET_MIN * 60 // STEP          # сколько 15м свечей ждём после часа

    res = {}            # (vwap_mode, rise, dist) -> [R]
    ctl_coin, ctl_time = [], []
    import random as _rnd
    _rnd.seed(777)
    n_ch = n_both = 0
    t0 = time.time()
    cov = 0.0

    for i, sym in enumerate(pairs, 1):
        try:
            fine = _fetch(sym, "15m", DAYS)
            base = _fetch(sym, "1h", DAYS + 5)
        except Exception:
            continue
        if len(fine) < 500 or len(base) < 150:
            continue
        fine, base = fine[:-1], base[:-1]
        if not cov:
            cov = (fine[-1].get("t", 0) - fine[0].get("t", 0)) / 86400
        idx = {c.get("t"): k for k, c in enumerate(fine)}
        day_syms = {}
        last_end = 0

        for e in range(B.BASE_FROM + B.ACC_WINDOW, len(base)):
            upto = base[:e]
            sl = upto[-B.BASE_FROM:-B.BASE_TO]
            if len(sl) < 10:
                continue
            vb = B.trimmed_mean([c["v"] for c in sl])
            ab = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
            if not vb or not ab or vb <= 0 or ab <= 0:
                continue
            cts = upto[-1].get("t", 0)
            sig_ts = cts + 3600                        # час закрылся — сигнал появился
            ent_ts = sig_ts + ENTRY_OFFSET_MIN * 60    # ждём до :30
            if ent_ts <= last_end:
                continue
            c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                {"btc_chg_win": 0.0, "do_charge": True},
                                {"funding": 0.0, "change_24h": 0.0},
                                lambda s: None, P=B.ALT_P)
            if not c:
                continue
            n_ch += 1
            if c["side"] == "both":
                n_both += 1

            k = idx.get(ent_ts)
            if k is None or k < 5 or k + hold + 2 >= len(fine):
                continue
            px = fine[k]["o"]                          # цена на момент скана
            prev = fine[k - 1]                         # последняя ЗАКРЫТАЯ 15м свеча
            vw = _vwap_at(fine, k - 1, bars_day)
            if vw is None:
                continue

            _day = datetime.fromtimestamp(ent_ts + 3 * 3600, timezone.utc).strftime("%Y-%m-%d")
            if sym in day_syms.setdefault(_day, set()):
                continue

            # сторона VWAP по ЗАКРЫТОЙ свече
            vw_side = "long" if prev["c"] > vw else "short"
            fut = fine[k:k + hold]
            if len(fut) < 4:
                continue

            took = False
            for vm in VWAP_MODES:
                for rise in RISE_GRID:
                    # движение за последние rise минут — по закрытым свечам
                    if rise:
                        back = rise * 60 // STEP
                        if k - 1 - back < 0:
                            continue
                        was = fine[k - 1 - back]["c"]
                        mv_side = "long" if prev["c"] > was else "short"
                    else:
                        mv_side = None

                    # направление: из заряда, а при «both» — из фильтров
                    if c["side"] in ("long", "short"):
                        side = c["side"]
                    else:
                        if mv_side is None or mv_side != vw_side:
                            continue           # фильтры не согласны — пропускаем
                        side = vw_side

                    if vm == "строго" and vw_side != side:
                        continue
                    if rise and mv_side != side:
                        continue

                    is_l = side == "long"
                    bnd = c["hi"] if is_l else c["lo"]
                    room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
                    for dist in DIST_GRID:
                        if room < dist:
                            continue
                        ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
                        stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
                        y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
                        y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
                        r = _sim(fut, side, ent, stp, y1, bnd, y3)
                        if r is not None:
                            res.setdefault((vm, rise, dist), []).append(r)
                            took = True
            if took:
                day_syms[_day].add(sym)
                last_end = fut[-1].get("t", 0)

                # КОНТРОЛЬ: то же, но направление монеткой и момент случайный
                sd_r = _rnd.choice(("long", "short"))
                isr = sd_r == "long"
                bnd_r = c["hi"] if isr else c["lo"]
                rr = _sim(fut, sd_r,
                          px * (1 + SLIP / 100) if isr else px * (1 - SLIP / 100),
                          px * (1 - STOP / 100) if isr else px * (1 + STOP / 100),
                          px * (1 + TP1 / 100) if isr else px * (1 - TP1 / 100),
                          bnd_r,
                          px * (1 + TP3 / 100) if isr else px * (1 - TP3 / 100))
                if rr is not None:
                    ctl_coin.append(rr)
                kr = _rnd.randrange(bars_day, max(bars_day + 1, len(fine) - hold - 2))
                pr = fine[kr]["o"]
                frr = fine[kr:kr + hold]
                if len(frr) >= 4:
                    isx = c["side"] == "long"
                    rt = _sim(frr, c["side"] if c["side"] != "both" else "long",
                              pr * (1 + SLIP / 100) if isx else pr * (1 - SLIP / 100),
                              pr * (1 - STOP / 100) if isx else pr * (1 + STOP / 100),
                              pr * (1 + TP1 / 100) if isx else pr * (1 - TP1 / 100),
                              c["hi"] if isx else c["lo"],
                              pr * (1 + TP3 / 100) if isx else pr * (1 - TP3 / 100))
                    if rt is not None:
                        ctl_time.append(rt)

        if i % 20 == 0:
            print(f"[ENTRY] {i}/{len(pairs)} | зарядов {n_ch} | {time.time()-t0:.0f}с")

    took_min = time.time() - t0
    L = [f"🚪 <b>НОВАЯ СХЕМА ВХОДА</b> (заряд 1h, вход на :{ENTRY_OFFSET_MIN:02d} после "
         f"закрытия часа, ~{cov:.0f} дн, {len(pairs)} пар)"
         + (f"\n⏪ период сдвинут назад на {OFFSET} дней" if OFFSET else ""),
         "Тайминг честный: час закрылся → ждём 30 мин → подтверждение по ЗАКРЫТЫМ "
         "15м свечам → вход по открытию следующей",
         f"Стоп {STOP}%, три цели по трети ({TP1}% → граница → {TP3}%), стоп подтягивается",
         f"Проскальзывание {SLIP}%, комиссия {FEE_PCT}%, фандинг {FUND_PCT}%, "
         f"стоп исполняется хуже на {STOP_SLIP}%",
         f"Зарядов найдено: {n_ch} (из них без направления: {n_both})",
         f"Время {took_min/60:.1f} мин", ""]

    for vm in VWAP_MODES:
        L.append(f"<b>VWAP: {vm}</b> (закрытая 15м свеча по нужную сторону)")
        for dist in DIST_GRID:
            for rise in RISE_GRID:
                rt = "без требования к движению" if rise == 0 else f"цена идёт {rise} мин"
                L.append(_line(res.get((vm, rise, dist), []),
                               f"ход ≥{dist}% | {rt}"))
        L.append("")

    L += ["═══ <b>КОНТРОЛЬ</b> ═══",
          "  <i>если наш отбор не лучше случайного — схема не работает</i>"]
    best = max((v for v in res.values() if len(v) >= 50),
               key=lambda v: sum(v) / len(v), default=[])
    L.append(_line(best, "ЛУЧШИЙ вариант схемы"))
    L.append(_line(ctl_coin, "то же, но направление МОНЕТКОЙ"))
    L.append(_line(ctl_time, "то же направление, вход в СЛУЧАЙНЫЙ момент"))
    if best and ctl_coin:
        d1 = sum(best) / len(best) - sum(ctl_coin) / len(ctl_coin)
        L.append(f"  → схема лучше монетки на <b>{d1:+.3f}R</b>")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[ENTRY] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон схемы входа упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
