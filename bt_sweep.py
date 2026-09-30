"""
bt_sweep.py — перебор параметров на одной выборке (пункты 4-7 разбора).

Отвечает на четыре вопроса сразу, и главное — НА ОДНИХ И ТЕХ ЖЕ сделках,
поэтому варианты сравнимы между собой:
  4) порог VWAP: 2.0 / 2.5 / 3.0 / 3.5 / выключен
  5) фильтр объёма при пробое: 1.0 (выкл) / 1.5 / 2.0 / 2.5
  6) стоп УКЛОНА: 0.5 / 0.75 / 1.0 / 1.25 %
  7) таймфрейм заряда 1h против 15m — раздельно для ПРОБОЯ и УКЛОНА

Заряд ищется на своём таймфрейме, сделка ведётся по МЕЛКИМ свечам (5m/15m) —
иначе внутри часовой свечи не видно, что случилось раньше, стоп или цель
(на этом я уже один раз ошибся и получил ложный минус).

Запуск: RUN_BACKTEST=sweep
Настройки: SW_TF (5m — свечи ведения), SW_PAIRS (0=все), SW_FEE (0.05), SW_SLIP (0.03)
Глубина ограничена API: 2000 свечей 5m ≈ 6.9 дней, 15m ≈ 20.8 дней.

ЧЕСТНЫЕ ОГРАНИЧЕНИЯ:
  • OI за историю не восстановить — сила заряда занижена, порог снижен на 2;
  • в спорной свече (задеты стоп и цель) считаем стоп — пессимизм;
  • подглядывания в будущее нет: решения принимаются на закрытии свечи,
    свинги для целей берутся только из прошлых данных.
"""
import os
import time
import statistics

import bot as B

# 15m по умолчанию: Gate отдаёт максимум 2000 свечей за запрос, это 20.8 дня на
# пятнадцатиминутках против 6.9 на пятиминутках. Для перебора важнее размер выборки,
# точности 15м внутри свечи хватает (на часовых я уже ошибся, на 15м спорных баров мало).
# SW_TF=5m — контрольный прогон на 7 днях: если картина та же, результату можно верить.
FINE_TF   = os.environ.get("SW_TF", "15m")
PAIRS_N   = int(os.environ.get("SW_PAIRS", "0"))
FEE_PCT   = float(os.environ.get("SW_FEE", "0.05"))
SLIP_PCT  = float(os.environ.get("SW_SLIP", "0.03"))
SCORE_DROP = 2          # на столько занижена сила заряда из-за отсутствия истории OI
MIN_SCORE = int(os.environ.get("SW_MIN_SCORE", str(max(1, B.ACC_MIN_SCORE - SCORE_DROP))))
TILT_SCORE = int(os.environ.get("SW_TILT_SCORE", str(max(1, B.TILT_MIN_SCORE - SCORE_DROP))))
MAX_HOLD_H = int(os.environ.get("SW_MAX_HOLD_H", "12"))
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}

VWAP_GRID  = [2.0, 2.5, 3.0, 3.5, None]          # None = фильтр выключен
RVOL_GRID  = [1.0, 1.5, 2.0, 2.5]
TSTOP_GRID = [0.5, 0.75, 1.0, 1.25]
CHARGE_TFS = ["1h", "15m"]


def _ts(c):
    return c.get("t", 0)


def _sim(bars, side, entry, stop, tp1, tp2):
    """Ведение сделки по мелким свечам: половина на TP1, стоп в безубыток, остаток на TP2.
    В спорной свече считаем стоп. Результат в R после комиссии."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == "long"
    half, cur_stop, acc = False, stop, 0.0
    for c in bars:
        hit_s = (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop)
        tgt = tp1 if not half else tp2
        hit_t = (c["h"] >= tgt) if is_long else (c["l"] <= tgt)
        if hit_s:
            part = 0.5 if half else 1.0
            r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            return acc + r * part - FEE_PCT / 100 * entry / risk
        if hit_t:
            if not half:
                acc += 0.5 * (abs(tp1 - entry) / risk)
                half, cur_stop = True, entry
            else:
                acc += 0.5 * (abs(tp2 - entry) / risk)
                return acc - FEE_PCT / 100 * entry / risk
    last = bars[-1]["c"] if bars else entry
    part = 0.5 if half else 1.0
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return acc + r * part - FEE_PCT / 100 * entry / risk


def _stat_line(rs, label):
    if not rs:
        return f"  {label}: сделок нет"
    n = len(rs)
    exp = sum(rs) / n
    wins = sum(1 for r in rs if r > 0)
    gl = abs(sum(r for r in rs if r <= 0)) or 0.0
    pf = (sum(r for r in rs if r > 0) / gl) if gl > 0 else float("inf")
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n else 0.0
    mark = "✅" if exp - 1.96 * se > 0 else "❌" if exp + 1.96 * se < 0 else "  "
    return (f"  {mark} {label}: {n:4} сд, винрейт {wins/n*100:3.0f}%, "
            f"<b>{exp:+.3f}R</b> (±{1.96*se:.3f}), ПФ {pf:.2f}, итого {sum(rs):+.0f}R")


def _vwap_atr(fine, k, bars_day):
    """Насколько цена ушла от дневного VWAP, в ATR. Считается только по прошлым свечам."""
    lo = max(0, k - bars_day)
    seg = fine[lo:k + 1]
    if len(seg) < 10:
        return None
    pv = sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in seg)
    vv = sum(c["v"] for c in seg)
    if vv <= 0:
        return None
    vwap = pv / vv
    trs = B.true_ranges(seg)
    atr = B.trimmed_mean(trs) if trs else 0
    if not atr:
        return None
    return abs(fine[k]["c"] - vwap) / atr


def run():
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    step = TF_SEC[FINE_TF]
    hold = max(6, int(MAX_HOLD_H * 3600 / step))
    watch = max(6, int(B.WATCH_TTL_HOURS * 3600 / step))
    bars_day = int(24 * 3600 / step)

    # результаты: [charge_tf][ключ варианта] -> список R
    brk = {tf: {"vwap": {v: [] for v in VWAP_GRID}, "rvol": {v: [] for v in RVOL_GRID}} for tf in CHARGE_TFS}
    tlt = {tf: {"stop": {v: [] for v in TSTOP_GRID}} for tf in CHARGE_TFS}
    n_ch = {tf: 0 for tf in CHARGE_TFS}
    days = 0.0
    saved_score, saved_tf = B.ACC_MIN_SCORE, B.CHARGE_TF
    B.ACC_MIN_SCORE = MIN_SCORE
    t0 = time.time()

    for i, sym in enumerate(pairs, 1):
        try:
            fine = B.get_candles(sym, FINE_TF, 1990)
        except Exception:
            continue
        if not fine or len(fine) < 300:
            continue
        fine = fine[:-1]
        if not days:
            days = (_ts(fine[-1]) - _ts(fine[0])) / 86400
        fine_from = _ts(fine[0])
        idx = {_ts(c): k for k, c in enumerate(fine)}
        vol_fine = B.trimmed_mean([c["v"] for c in fine[:max(50, len(fine) // 3)]])
        if not vol_fine or vol_fine <= 0:
            continue

        for ctf in CHARGE_TFS:
            try:
                base = B.get_candles(sym, ctf, 1990)   # было 700: для заряда на 15m это
                #                                       всего 7 дней, и сравнение с 1h было нечестным
            except Exception:
                continue
            if not base or len(base) < 120:
                continue
            base = base[:-1]
            csec = TF_SEC[ctf]
            W = max(4, round(B.ACC_WINDOW_HOURS * 3600 / csec))
            bfrom = max(10, round(B.BASE_FROM_H * 3600 / csec))
            bto = max(2, round(B.BASE_TO_H * 3600 / csec))
            prof = dict(B.ALT_P)
            prof.update({"tf": ctf, "tf_min": csec // 60, "window": W,
                         "dir_swing_n": max(2, round(B.DIR_SWING_HOURS * 3600 / csec)),
                         "flat_atr_eff": B.ACC_FLAT_ATR * (B.REF_TF_MIN / (csec / 60)) ** 0.5})
            last_ts = 0

            for e in range(bfrom + W, len(base)):
                upto = base[:e]
                sl = upto[-bfrom:-bto]
                if len(sl) < 10:
                    continue
                vb = B.trimmed_mean([c["v"] for c in sl])
                ab = B.trimmed_mean(B.true_ranges(upto)[-bfrom:-bto])
                if not vb or not ab or vb <= 0 or ab <= 0:
                    continue
                cts = _ts(upto[-1])
                if cts < fine_from or cts <= last_ts:
                    continue
                c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                    {"btc_chg_win": 0.0, "do_charge": True},
                                    {"funding": 0.0, "change_24h": 0.0},
                                    lambda s: None, P=prof)
                if not c:
                    continue
                n_ch[ctf] += 1
                k0 = None
                for off in range(0, int(csec / step) + 1):
                    k0 = idx.get(cts + off * step)
                    if k0 is not None:
                        break
                if k0 is None:
                    continue

                # ── УКЛОН: вход сразу по заряду, перебор стопа ──
                if c["side"] in ("long", "short") and c["score"] >= TILT_SCORE:
                    px = fine[k0]["c"]
                    is_l = c["side"] == "long"
                    bnd = c["hi"] if is_l else c["lo"]
                    dist = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
                    if dist >= B.TILT_MIN_DIST_PCT:
                        ent = px * (1 + SLIP_PCT / 100) if is_l else px * (1 - SLIP_PCT / 100)
                        fut = fine[k0 + 1:k0 + 1 + hold]
                        if len(fut) >= 4:
                            for sp in TSTOP_GRID:
                                stp = ent * (1 - sp / 100) if is_l else ent * (1 + sp / 100)
                                t1 = ent * (1 + B.TILT_TP1_PCT / 100) if is_l else ent * (1 - B.TILT_TP1_PCT / 100)
                                m = ent * B.TILT_TP2_MARGIN / 100
                                t2 = (bnd - m) if is_l else (bnd + m)
                                if is_l and t2 <= t1:
                                    t2 = t1 * 1.001
                                if not is_l and t2 >= t1:
                                    t2 = t1 * 0.999
                                r = _sim(fut, c["side"], ent, stp, t1, t2)
                                if r is not None:
                                    tlt[ctf]["stop"][sp].append(r)

                # ── ПРОБОЙ: ждём закрытия за уровнем, перебор VWAP и объёма ──
                hi_t = B.order_trigger(c["hi"], True, c["atr"])
                lo_t = B.order_trigger(c["lo"], False, c["atr"])
                done = False
                for k in range(k0 + 1, min(k0 + watch, len(fine) - 2)):
                    bar = fine[k]
                    for side, trig in (("long", hi_t), ("short", lo_t)):
                        beyond = bar["c"] > trig if side == "long" else bar["c"] < trig
                        if not beyond:
                            continue
                        fut = fine[k + 1:k + 1 + hold]
                        if len(fut) < 4:
                            continue
                        w = dict(c)
                        for kk, vv in (("created", 0), ("funding", 0.0), ("change_24h", 0.0),
                                       ("oi_win", None), ("oi_15m", None), ("P", prof)):
                            w.setdefault(kk, vv)
                        ent = bar["c"] * (1 + SLIP_PCT / 100) if side == "long" else bar["c"] * (1 - SLIP_PCT / 100)
                        bo = B.build_breakout(dict(w), side, ent, 0.0, None)
                        r = _sim(fut, side, ent, bo["stop"], bo["tp1_price"], bo["tp2_price"])
                        if r is None:
                            continue
                        rv = bar["v"] / vol_fine
                        va = _vwap_atr(fine, k, bars_day)
                        for thr in VWAP_GRID:              # пункт 4: изолируем VWAP,
                            # фильтр объёма здесь НЕ применяем — иначе варианты
                            # окажутся пустыми и сравнивать будет нечего
                            if thr is None or (va is not None and va <= thr):
                                brk[ctf]["vwap"][thr].append(r)
                        for rthr in RVOL_GRID:             # пункт 5
                            if rv >= rthr and (va is None or va <= B.VWAP_MAX_ATR):
                                brk[ctf]["rvol"][rthr].append(r)
                        last_ts = _ts(fut[-1])
                        done = True
                        break
                    if done:
                        break
        if i % 20 == 0:
            print(f"[SWEEP] {i}/{len(pairs)} | зарядов 1h={n_ch['1h']} 15m={n_ch['15m']} | {time.time()-t0:.0f}с")

    B.ACC_MIN_SCORE, B.CHARGE_TF = saved_score, saved_tf
    took = time.time() - t0

    L = [f"🔬 <b>Перебор параметров</b> (сделки по {FINE_TF}, ~{days:.1f} дн — предел API 2000 свечей, "
         f"{len(pairs)} пар)",
         f"Комиссия {FEE_PCT}%, проскальзывание {SLIP_PCT}%, время {took/60:.1f} мин",
         f"Зарядов найдено: 1h — {n_ch['1h']}, 15m — {n_ch['15m']} "
         f"(порог силы {MIN_SCORE}, для УКЛОНА {TILT_SCORE} — снижены на {SCORE_DROP}, нет истории OI)",
         "<i>✅ = плюс уверенно, ❌ = минус уверенно, пусто = неотличимо от нуля</i>"]

    for ctf in CHARGE_TFS:
        L += ["", f"════ ЗАРЯД НА {ctf.upper()} ════", "", "<b>4) Порог VWAP (ПРОБОЙ):</b>"]
        for v in VWAP_GRID:
            L.append(_stat_line(brk[ctf]["vwap"][v], f"VWAP {'выкл' if v is None else f'≤{v} ATR'}"))
        L += ["", f"<b>5) Фильтр объёма (ПРОБОЙ), считается по свече {FINE_TF}:</b>",
              f"  <i>живой бот меряет объём на 1м свече, где всплеск ×2 обычен, "
              f"а на {FINE_TF} редок — сравнивай варианты между собой, а не с порогом бота</i>"]
        for v in RVOL_GRID:
            L.append(_stat_line(brk[ctf]["rvol"][v], f"объём ≥{v}×" + (" (выкл)" if v <= 1.0 else "")))
        L += ["", "<b>6) Стоп УКЛОНА:</b>"]
        for v in TSTOP_GRID:
            L.append(_stat_line(tlt[ctf]["stop"][v], f"стоп {v}%"))

    # 7) сравнение таймфреймов по лучшим вариантам
    L += ["", "════ 7) 1h ПРОТИВ 15m ════"]
    for nm, pick_ in (("ПРОБОЙ (VWAP 2.5)", lambda tf: brk[tf]["vwap"][2.5]),
                      ("УКЛОН (стоп 1.0%)", lambda tf: tlt[tf]["stop"][1.0])):
        a, b = pick_("1h"), pick_("15m")
        ea = sum(a) / len(a) if a else 0
        eb = sum(b) / len(b) if b else 0
        L.append(f"  {nm}: 1h — {len(a)} сд, {ea:+.3f}R | 15m — {len(b)} сд, {eb:+.3f}R")
        if a and b:
            L.append(f"     → {'15m лучше' if eb > ea else '1h лучше'} на {abs(eb-ea):.3f}R, "
                     f"сделок {'больше' if len(b) > len(a) else 'меньше'} в {max(len(b),len(a))/max(1,min(len(b),len(a))):.1f}×")

    L += ["", "<i>OI за историю не восстановить — сила заряда занижена, порог снижен на 2. "
          "В спорной свече считаем стоп. Подглядывания в будущее нет.</i>"]

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[SWEEP] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Перебор упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
