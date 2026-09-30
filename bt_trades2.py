"""
bt_trades2.py — бэктест СДЕЛОК по мелким свечам (v3).

Чем отличается от bt_trades.py (v2) и почему тот дал неверный минус:
  v2 вёл сделку по ЧАСОВЫМ свечам и входил, когда ФИТИЛЬ часовой свечи задевал
  триггер (`bar["h"] >= trig`). То есть заходил в каждый прокол уровня, включая
  свипы, которые живой бот игнорирует: он ждёт ЗАКРЫТИЯ минутной свечи за уровнем.
  Плюс в часовой свече часто задеты и стоп, и цель — порядок неизвестен.

Здесь:
  • заряд ищется на 1h (как в живом боте);
  • вход — по ЗАКРЫТИЮ мелкой свечи (5m/15m) за триггером + объём на ней;
  • сделка ведётся по тем же мелким свечам — меньше спорных баров;
  • подглядывания в будущее нет: решение принимается на закрытии свечи,
    цели берутся из свингов, посчитанных ТОЛЬКО по прошлым данным.

Запуск: RUN_BACKTEST=trades2
Настройки: BT2_TF (5m), BT2_PAIRS (0=все), BT2_FEE (0.05), BT2_SLIP (0.03)
Глубина ограничена API: 2000 свечей 5m ≈ 6.9 дней, 15m ≈ 20.8 дней.
"""
import os
import time
import statistics

import bot as B

TF        = os.environ.get("BT2_TF", "5m")
PAIRS_N   = int(os.environ.get("BT2_PAIRS", "0"))
FEE_PCT   = float(os.environ.get("BT2_FEE", "0.05"))
SLIP_PCT  = float(os.environ.get("BT2_SLIP", "0.03"))
MIN_SCORE = int(os.environ.get("BT2_MIN_SCORE", str(max(1, B.ACC_MIN_SCORE - 2))))
BREAK_RVOL = float(os.environ.get("BT2_BREAK_RVOL", "2.0"))   # на мелкой свече, как живой бот
MAX_HOLD_H = int(os.environ.get("BT2_MAX_HOLD_H", "12"))
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def _ts(c):
    return c.get("t", 0)


def _simulate(bars, side, entry, stop, tp1, tp2, amb):
    """Ведение сделки по мелким свечам. Половина на TP1, стоп в безубыток, остаток на TP2."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == "long"
    half_done, cur_stop, realized = False, stop, 0.0
    for c in bars:
        hit_stop = (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop)
        tgt = tp1 if not half_done else tp2
        hit_tp = (c["h"] >= tgt) if is_long else (c["l"] <= tgt)
        if hit_stop and hit_tp:
            amb.append(1)                      # спорная свеча — считаем стоп (пессимизм)
            part = 0.5 if half_done else 1.0
            r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            return realized + r * part - FEE_PCT / 100 * entry / risk
        if hit_stop:
            part = 0.5 if half_done else 1.0
            r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            return realized + r * part - FEE_PCT / 100 * entry / risk
        if hit_tp:
            if not half_done:
                realized += 0.5 * (abs(tp1 - entry) / risk)
                half_done, cur_stop = True, entry
            else:
                realized += 0.5 * (abs(tp2 - entry) / risk)
                return realized - FEE_PCT / 100 * entry / risk
    last = bars[-1]["c"] if bars else entry
    part = 0.5 if half_done else 1.0
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return realized + r * part - FEE_PCT / 100 * entry / risk


def _stats(rs, label):
    if not rs:
        return [f"  {label}: сделок нет"]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    exp = sum(rs) / n
    gp = sum(wins) or 0.0
    gl = abs(sum(r for r in rs if r <= 0)) or 0.0
    pf = (gp / gl) if gl > 0 else float("inf")
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n else 0.0
    lo, hi = exp - 1.96 * se, exp + 1.96 * se
    return [
        f"  <b>{label}</b>: сделок {n}, винрейт {len(wins)/n*100:.0f}%",
        f"    матожидание <b>{exp:+.3f}R</b> (95%: {lo:+.3f}…{hi:+.3f})",
        f"    профит-фактор {pf:.2f}, итого {sum(rs):+.1f}R, просадка {dd:.1f}R",
        "    → " + ("<b>ПЛЮС</b>" if lo > 0 else "минус" if hi < 0
                    else "неотличимо от нуля, данных мало"),
    ]


def run():
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    W = B.ACC_WINDOW
    step_sec = TF_SEC[TF]
    hold_bars = max(6, int(MAX_HOLD_H * 3600 / step_sec))
    watch_bars = max(6, int(B.WATCH_TTL_HOURS * 3600 / step_sec))

    rs_close, rs_wick = [], []          # вход по закрытию (как живой бот) и по фитилю (как v2)
    amb = []
    n_ch = n_sig = n_novol = 0
    saved = B.ACC_MIN_SCORE
    B.ACC_MIN_SCORE = MIN_SCORE
    t0 = time.time()
    days_cov = 0.0

    for i, sym in enumerate(pairs, 1):
        try:
            h1 = B.get_candles(sym, B.CHARGE_TF, 700)
            fine = B.get_candles(sym, TF, 1990)
        except Exception:
            continue
        if not h1 or not fine or len(h1) < B.BASE_FROM + W + 5 or len(fine) < 200:
            continue
        h1 = h1[:-1]
        fine = fine[:-1]
        if not days_cov:
            days_cov = (_ts(fine[-1]) - _ts(fine[0])) / 86400
        fine_from = _ts(fine[0])
        # норма объёма мелкой свечи — по первой трети ряда (прошлое, без подглядывания)
        base_n = max(50, len(fine) // 3)
        vol_fine = B.trimmed_mean([c["v"] for c in fine[:base_n]])
        if not vol_fine or vol_fine <= 0:
            continue

        fine_idx = {_ts(c): k for k, c in enumerate(fine)}
        last_exit_ts = 0

        for e in range(B.BASE_FROM + W, len(h1)):
            upto = h1[:e]
            sl = upto[-B.BASE_FROM:-B.BASE_TO]
            if len(sl) < 10:
                continue
            vb = B.trimmed_mean([c["v"] for c in sl])
            ab = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
            if not vb or not ab or vb <= 0 or ab <= 0:
                continue
            ch_ts = _ts(upto[-1])
            if ch_ts < fine_from or ch_ts <= last_exit_ts:
                continue
            c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                {"btc_chg_win": 0.0, "do_charge": True},
                                {"funding": 0.0, "change_24h": 0.0}, lambda s: None, P=B.ALT_P)
            if not c:
                continue
            n_ch += 1

            # ищем стартовую мелкую свечу
            k0 = None
            for off in range(0, int(3600 / step_sec) + 1):
                k0 = fine_idx.get(ch_ts + off * step_sec)
                if k0 is not None:
                    break
            if k0 is None:
                continue

            hi_lvl = B.order_trigger(c["hi"], True, c["atr"])
            lo_lvl = B.order_trigger(c["lo"], False, c["atr"])
            got_close = got_wick = False
            for k in range(k0 + 1, min(k0 + watch_bars, len(fine) - 2)):
                bar, prev = fine[k], fine[k - 1]
                for side, trig in (("long", hi_lvl), ("short", lo_lvl)):
                    inside_before = (prev["c"] <= trig) if side == "long" else (prev["c"] >= trig)
                    touch = (bar["h"] >= trig) if side == "long" else (bar["l"] <= trig)
                    closed_beyond = (bar["c"] > trig) if side == "long" else (bar["c"] < trig)
                    vol_ok = (bar["v"] / vol_fine) >= BREAK_RVOL
                    w = dict(c)
                    for kk, vv in (("created", 0), ("funding", 0.0), ("change_24h", 0.0),
                                   ("oi_win", None), ("oi_15m", None), ("P", B.ALT_P)):
                        w.setdefault(kk, vv)

                    # B) вход ПО КАСАНИЮ: только на свече, где цена ПЕРЕСЕКАЕТ триггер
                    # (иначе вошли бы по устаревшей цене уровня, пробитого часом раньше).
                    # Свеча входа включена в ведение: разворот внутри неё должен считаться.
                    if not got_wick and inside_before and touch and vol_ok:
                        entw = trig * (1 + SLIP_PCT / 100) if side == "long" else trig * (1 - SLIP_PCT / 100)
                        bow = B.build_breakout(dict(w), side, entw, 0.0, None)
                        rw = _simulate(fine[k:k + 1 + hold_bars], side, entw, bow["stop"],
                                       bow["tp1_price"], bow["tp2_price"], [])
                        if rw is not None:
                            rs_wick.append(rw)
                            got_wick = True

                    # A) вход КАК ЖИВОЙ БОТ: свеча ЗАКРЫЛАСЬ за триггером, входим по закрытию
                    if not got_close and closed_beyond:
                        n_sig += 1
                        if not vol_ok:
                            n_novol += 1
                            continue
                        fut = fine[k + 1:k + 1 + hold_bars]
                        if len(fut) < 4:
                            continue
                        ent = bar["c"] * (1 + SLIP_PCT / 100) if side == "long" \
                              else bar["c"] * (1 - SLIP_PCT / 100)
                        bo = B.build_breakout(dict(w), side, ent, 0.0, None)
                        r = _simulate(fut, side, ent, bo["stop"], bo["tp1_price"], bo["tp2_price"], amb)
                        if r is not None:
                            rs_close.append(r)
                            last_exit_ts = _ts(fut[-1])
                            got_close = True
                if got_close:
                    break
        if i % 20 == 0:
            print(f"[BT2] {i}/{len(pairs)} | зарядов {n_ch} | сделок {len(rs_close)} | {time.time()-t0:.0f}с")

    B.ACC_MIN_SCORE = saved
    took = time.time() - t0
    lines = [
        f"💰 <b>Бэктест СДЕЛОК v3</b> (заряд 1h, сделка по {TF})",
        f"{len(pairs)} пар, глубина ~{days_cov:.1f} дн (предел API), время {took/60:.1f} мин",
        f"Комиссия {FEE_PCT}%, проскальзывание {SLIP_PCT}%",
        f"<b>Воронка:</b> зарядов {n_ch} → касаний уровня {n_sig} → "
        f"отсеяно объёмом {n_novol} → сделок {len(rs_close)}",
        "",
        "<b>Главное сравнение — как входить:</b>",
    ]
    lines += _stats(rs_close, f"ПО ЗАКРЫТИЮ {TF} за уровнем (как живой бот)")
    lines.append("")
    lines += _stats(rs_wick, "ПО КАСАНИЮ уровня (так считал старый бэктест — ловит свипы)")
    if rs_close and rs_wick:
        d = sum(rs_close) / len(rs_close) - sum(rs_wick) / len(rs_wick)
        lines.append("")
        lines.append(f"Ожидание закрытия вместо касания даёт <b>{d:+.3f}R</b> на сделку")
    if amb:
        tot = len(rs_close) or 1
        lines.append(f"Спорных свечей (задеты стоп и цель): {len(amb)/tot*100:.0f}% — "
                     f"на {TF} их должно быть мало")
    if rs_close:
        exp = sum(rs_close) / len(rs_close)
        lines.append("")
        lines.append(f"При риске $20: {exp*20:+.2f}$ на сделку")
    lines += ["", "<i>Подглядывания в будущее нет: вход решается на закрытии свечи, "
              "свинги для целей — только из прошлых данных. Глубина ограничена API "
              "(2000 свечей).</i>"]

    msg = "\n".join(lines)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[BT2] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Бэктест v3 упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
