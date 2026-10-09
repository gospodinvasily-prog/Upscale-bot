# =====================================================================
#  ВАЛИДАЦИЯ (4 гейта, как в v1.0 / v4.7)
# =====================================================================

def validate(result, z=Z_SCORE):
    final     = result["final_equity"]
    total_pnl = final - INIT_CAPITAL

    daily_vals = [p[1] for p in result["daily_pnl"]]
    n = len(daily_vals)
    if n > 1 and statistics.pstdev(daily_vals) > 0:
        std = statistics.stdev(daily_vals)
        se  = std / math.sqrt(n)
        ci  = z * se
    else:
        ci = float("inf") if total_pnl > 0 else 0.0
    gate1 = (total_pnl - ci) > 0

    worst_day = min(daily_vals) if daily_vals else 0.0
    gate2 = worst_day >= WORST_DAY_LIMIT

    worst_day_ts = None
    if result["daily_pnl"]:
        worst_day_ts = min(result["daily_pnl"], key=lambda p: p[1])[0]

    losing_days_n = sum(1 for _, pnl in result["daily_pnl"] if pnl < 0)
    loss_streaks, cur_loss_streak = [], []
    for ts, pnl in sorted(result["daily_pnl"], key=lambda p: p[0]):
        if pnl < 0:
            cur_loss_streak.append((ts, pnl))
        else:
            if cur_loss_streak:
                loss_streaks.append(cur_loss_streak)
            cur_loss_streak = []
    if cur_loss_streak:
        loss_streaks.append(cur_loss_streak)
    longest_loss_streak = max((len(s) for s in loss_streaks), default=0)
    multi_loss_streaks = [s for s in loss_streaks if len(s) >= 2]

    worst_streak_loss = 0.0
    worst_streak_detail = None
    for s in loss_streaks:
        streak_total = sum(p for _, p in s)
        if streak_total < worst_streak_loss:
            worst_streak_loss = streak_total
            worst_streak_detail = {"from": s[0][0], "to": s[-1][0], "days": len(s)}

    eqs = [e for _, e in result["equity_curve"]]
    peak, max_dd = -math.inf, 0.0
    for e in eqs:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)
    gate3 = max_dd <= MAX_DD_LIMIT

    yearly = defaultdict(float)
    for ts, pnl in result["daily_pnl"]:
        y = dt.datetime.utcfromtimestamp(ts).year
        yearly[y] += pnl
    gate4 = all(v >= YEAR_LOSS_LIMIT for v in yearly.values())

    reasons = defaultdict(int)
    for t in result["trades"]:
        reasons[t["reason"]] += 1

    # Разбивка по парам (как в v1.0)
    by_pair = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "pnl": 0.0,
        "long_n": 0, "long_wins": 0, "short_n": 0, "short_wins": 0,
    })
    for t in result["trades"]:
        row = by_pair[t["contract"]]
        row["n"] += 1
        row["pnl"] += t["pnl"]
        win = t["pnl"] > 0
        if win:
            row["wins"] += 1
        else:
            row["losses"] += 1
        if t["side"] == +1:
            row["long_n"] += 1
            if win:
                row["long_wins"] += 1
        else:
            row["short_n"] += 1
            if win:
                row["short_wins"] += 1
    pair_stats = sorted(
        (
            {"pair": p, "n": r["n"], "pnl": r["pnl"],
             "winrate": (r["wins"] / r["n"] * 100) if r["n"] else 0.0,
             "wins": r["wins"], "losses": r["losses"],
             "long_n": r["long_n"], "long_wins": r["long_wins"],
             "short_n": r["short_n"], "short_wins": r["short_wins"]}
            for p, r in by_pair.items()
        ),
        key=lambda x: x["pnl"], reverse=True,
    )

    # Long/Short общая статистика (как в v1.0)
    all_trades = result["trades"]
    longs  = [t for t in all_trades if t["side"] == +1]
    shorts = [t for t in all_trades if t["side"] == -1]
    long_short_stats = {
        "long_n": len(longs),
        "long_wins": sum(1 for t in longs if t["pnl"] > 0),
        "long_losses": sum(1 for t in longs if t["pnl"] <= 0),
        "short_n": len(shorts),
        "short_wins": sum(1 for t in shorts if t["pnl"] > 0),
        "short_losses": sum(1 for t in shorts if t["pnl"] <= 0),
    }

    def _mfe_pct(t):
        return t["side"] * (t["max_favorable"] - t["entry"]) / t["entry"] * 100

    losing_longs  = [t for t in longs  if t["pnl"] <= 0]
    losing_shorts = [t for t in shorts if t["pnl"] <= 0]
    long_short_stats["long_losing_mfe_avg"]  = (
        sum(_mfe_pct(t) for t in losing_longs) / len(losing_longs) if losing_longs else 0.0)
    long_short_stats["long_losing_mfe_max"]  = (
        max((_mfe_pct(t) for t in losing_longs), default=0.0))
    long_short_stats["short_losing_mfe_avg"] = (
        sum(_mfe_pct(t) for t in losing_shorts) / len(losing_shorts) if losing_shorts else 0.0)
    long_short_stats["short_losing_mfe_max"] = (
        max((_mfe_pct(t) for t in losing_shorts), default=0.0))

    # Стоп-выходы (SL/TRAIL) — MAE + reversal (как в v1.0)
    def _mae_pct(t):
        return t["side"] * (t["entry"] - t["max_adverse"]) / t["entry"] * 100

    stop_losing = [
        t for t in result["trades"]
        if t["reason"] in ("SL", "TRAIL") and t["pnl"] <= 0
        and t.get("reversed_after_stop") is not None
    ]
    stop_reversed_n = sum(1 for t in stop_losing if t["reversed_after_stop"])
    stop_stats = {
        "n": len(stop_losing),
        "reversed_n": stop_reversed_n,
        "reversed_pct": (stop_reversed_n / len(stop_losing) * 100) if stop_losing else 0.0,
        "mae_avg": (sum(_mae_pct(t) for t in stop_losing) / len(stop_losing)) if stop_losing else 0.0,
        "mae_max": max((_mae_pct(t) for t in stop_losing), default=0.0),
        "lookforward_days": STOP_REVERSAL_LOOKFORWARD_DAYS,
    }

    # Daily stop events (как в v1.0)
    day_stop_events = result.get("day_stop_events", [])
    streaks = []
    cur_streak = []
    for ev in sorted(day_stop_events, key=lambda e: e["day"]):
        if cur_streak and ev["day"] - cur_streak[-1]["day"] == 86400:
            cur_streak.append(ev)
        else:
            if cur_streak:
                streaks.append(cur_streak)
            cur_streak = [ev]
    if cur_streak:
        streaks.append(cur_streak)
    multi_day_streaks = [s for s in streaks if len(s) >= 2]

    return {
        "final_equity": final, "total_pnl": total_pnl, "ci_z": ci,
        "n_trades": result["n_trades"], "n_days": n,
        "gate1": gate1, "gate2": gate2, "gate3": gate3, "gate4": gate4,
        "worst_day": worst_day, "worst_day_ts": worst_day_ts, "max_dd": max_dd,
        "losing_days_n": losing_days_n,
        "longest_loss_streak": longest_loss_streak,
        "multi_loss_streaks_n": len(multi_loss_streaks),
        "multi_loss_streaks_detail": [
            {"from": s[0][0], "to": s[-1][0], "days": len(s),
             "total_loss": sum(p for _, p in s)}
            for s in multi_loss_streaks
        ],
        "worst_streak_loss": worst_streak_loss,
        "worst_streak_detail": worst_streak_detail,
        "yearly_pnl": dict(yearly),
        "reasons": dict(reasons),
        "pair_stats": pair_stats,
        "long_short_stats": long_short_stats,
        "stop_reversal_stats": stop_stats,
        "day_stop_events": day_stop_events,
        "day_stop_streaks_multi": len(multi_day_streaks),
        "day_stop_streaks_multi_detail": [
            {"from": s[0]["day"], "to": s[-1]["day"], "days": len(s)}
            for s in multi_day_streaks
        ],
        "tp1_count": result.get("tp1_count", 0),
        "tp1_total_pnl": result.get("tp1_total_pnl", 0.0),
        "btc_blocked": result.get("btc_blocked", 0),
        "dd_brake_days": result.get("dd_brake_days", 0),
        "adx_filtered": result.get("adx_filtered", 0),
        "cooldown_blocked": result.get("cooldown_blocked", 0),
        "excluded_count": result.get("excluded_count", 0),
        "day_stop_triggered": result.get("day_stop_triggered", 0),
        "consec_loss_days_max": result.get("consec_loss_days_max", 0),
        "all_pass": gate1 and gate2 and gate3 and gate4,
    }


# =====================================================================
#  ОТЧЁТ
# =====================================================================

_PAIRS_USED = []


def format_report(result, val, n_pairs=None):
    if n_pairs is None:
        n_pairs = len(_PAIRS_USED)
    lines = []
    lines.append(f"📊 *{STRATEGY_NAME} {STRATEGY_VERSION} - РЕЗУЛЬТАТЫ*  [{STRATEGY_FILE}]")
    lines.append("")
    btc_label = f"BTC regime SMA({BTC_REGIME_SMA}) + " if USE_BTC_REGIME else f"ADX<{ADX_THRESHOLD:.0f} (sideways) + "
    lines.append(f"RSI({RSI_PERIOD})+BB({BB_PERIOD}, {BB_STD}σ) + {btc_label}Trailing {TRAIL_ATR_MULT}xATR + "
                 f"TP1 {TP1_FRACTION*100:.0f}%@{TP1_RETRACE_PCT*100:.0f}%retrace+breakeven + "
                 f"SL {ATR_STOP_MULT}xATR + Compound + Daily stop + Cooldown")
    lines.append(f"Капитал: ${INIT_CAPITAL:,.0f}  |  Пары: {n_pairs}  |  Excluded: {val['excluded_count']}")
    lines.append(f"Risk: {RISK_FRACTION*100:.1f}% от equity (floor ${SLOT_RISK_MIN:.0f}, cap ${SLOT_RISK_MAX:.0f}, brake x{DD_BRAKE_FACTOR})")
    lines.append(f"Max concurrent: {MAX_CONCURRENT} (per-side cap ОТКЛЮЧЁН) | Daily stop: ${DAILY_STOP_LOSS:.0f} / 2-й день подряд ${DAILY_STOP_LOSS_CONSEC:.0f}")
    lines.append(f"TP1: цена дошла до {TP1_RETRACE_PCT*100:.0f}% от BB band к mid → закрыть {TP1_FRACTION*100:.0f}%, остаток -> breakeven | Max hold: {MAX_HOLD_DAYS}д | New/day: {MAX_NEW_PER_DAY}")
    lines.append(f"Long: close≤BB_lower AND RSI≤{RSI_OVERSOLD} | Short: close≥BB_upper AND RSI≥{RSI_OVERBOUGHT} | SL: {ATR_STOP_MULT}×ATR")
    lines.append(f"Сделок: {val['n_trades']}  |  Дней: {val['n_days']}")
    if USE_BTC_REGIME:
        lines.append(f"BTC blocked: {val['btc_blocked']}  |  ADX filtered: {val['adx_filtered']}  |  DD brake days: {val['dd_brake_days']}")
    else:
        lines.append(f"ADX filtered (trend): {val['adx_filtered']}  |  DD brake days: {val['dd_brake_days']}")
    lines.append(f"Cooldown: {val['cooldown_blocked']}  |  Daily stop: {val['day_stop_triggered']}  |  "
                 f"TP1: {val['tp1_count']} (${val['tp1_total_pnl']:+,.0f})  |  "
                 f"Max consec loss days: {val.get('consec_loss_days_max', 0)}")
    if val.get("reasons"):
        r = val["reasons"]
        lines.append(f"Исходы: SL/TRAIL={r.get('TRAIL',0)+r.get('SL',0)} "
                     f"TP1={r.get('TP1',0)} TIME={r.get('TIME',0)} SIG={r.get('SIG',0)} DSTOP={r.get('DSTOP',0)} END={r.get('END',0)}")
    lines.append("")
    lines.append(f"Final equity : ${val['final_equity']:,.2f}")
    lines.append(f"Total P&L    : ${val['total_pnl']:,.2f}")
    lines.append(f"CI(Z={Z_SCORE}): ${val['ci_z']:,.2f}")
    lines.append("")
    lines.append("- ВАЛИДАЦИЯ -")
    lines.append(f"① Final - CI > 0     : {'✅ PASS' if val['gate1'] else '❌ FAIL'}"
                 f"  (edge = ${val['total_pnl']-val['ci_z']:,.2f})")
    worst_day_date = (dt.datetime.utcfromtimestamp(val["worst_day_ts"]).strftime("%Y-%m-%d")
                      if val.get("worst_day_ts") else "-")
    lines.append(f"② Worst day ≥ -$500   : {'✅ PASS' if val['gate2'] else '❌ FAIL'}"
                 f"  (worst = ${val['worst_day']:,.2f}, {worst_day_date})")
    lines.append(f"③ MaxDD ≤ $2,000      : {'✅ PASS' if val['gate3'] else '❌ FAIL'}"
                 f"  (MaxDD = ${val['max_dd']:,.2f})")
    lines.append(f"④ No year < -$500     : {'✅ PASS' if val['gate4'] else '❌ FAIL'}")
    for y in sorted(val["yearly_pnl"]):
        lines.append(f"   {y}: ${val['yearly_pnl'][y]:,.2f}")
    lines.append("")
    verdict = "✅✅✅✅ ALL PASS" if val["all_pass"] else "❌ НЕ ПРОШЁЛ"
    lines.append(f"ИТОГ: {verdict}")

    # Long/Short (как в v1.0)
    ls = val.get("long_short_stats")
    if ls:
        lines.append("")
        lines.append("- LONG / SHORT -")
        lines.append(f"Long : {ls['long_n']} сделок  (🟢 {ls['long_wins']} / 🔴 {ls['long_losses']})")
        lines.append(f"Short: {ls['short_n']} сделок  (🟢 {ls['short_wins']} / 🔴 {ls['short_losses']})")
        lines.append(
            f"Убыточные Long  - доходили в свою сторону в среднем на "
            f"{ls['long_losing_mfe_avg']:.2f}% (макс {ls['long_losing_mfe_max']:.2f}%)"
        )
        lines.append(
            f"Убыточные Short - доходили в свою сторону в среднем на "
            f"{ls['short_losing_mfe_avg']:.2f}% (макс {ls['short_losing_mfe_max']:.2f}%)"
        )

    # Стоп-выходы (как в v1.0)
    ss = val.get("stop_reversal_stats")
    if ss and ss["n"]:
        lines.append("")
        lines.append("- СТОП-ВЫХОДЫ (SL/TRAIL), убыточные -")
        lines.append(f"Всего: {ss['n']}  |  вернулись в сторону сделки "
                     f"в течение {ss['lookforward_days']}д после стопа: "
                     f"{ss['reversed_n']} ({ss['reversed_pct']:.0f}%)")
        lines.append(f"Просадка от входа (MAE%): в среднем {ss['mae_avg']:.2f}%  "
                     f"(макс {ss['mae_max']:.2f}%)")

    # Daily stop events (как в v1.0)
    events = val.get("day_stop_events") or []
    if events:
        lines.append("")
        lines.append(f"- DAILY STOP ${DAILY_STOP_LOSS:.0f} / 2-й день подряд ${DAILY_STOP_LOSS_CONSEC:.0f} -")
        lines.append(f"Сработал: {val['day_stop_triggered']} раз(а)  |  "
                     f"подряд (2+ дня): {val.get('day_stop_streaks_multi', 0)} раз(а)")
        for det in (val.get("day_stop_streaks_multi_detail") or []):
            d_from = dt.datetime.utcfromtimestamp(det["from"]).strftime("%Y-%m-%d")
            d_to   = dt.datetime.utcfromtimestamp(det["to"]).strftime("%Y-%m-%d")
            lines.append(f"   подряд {det['days']}д: {d_from} -> {d_to}")
        for ev in events[:30]:
            d = dt.datetime.utcfromtimestamp(ev["day"]).strftime("%Y-%m-%d")
            closed_mark = "все позиции закрылись" if ev["all_closed"] else f"осталось открыто {ev['open_left']}"
            lines.append(f"   {d}: открыто было {ev['open_before']}, {closed_mark}")
        if len(events) > 30:
            lines.append(f"   ... и ещё {len(events)-30} срабатываний")

    # Минусовые дни (как в v1.0)
    lines.append("")
    lines.append("- МИНУСОВЫЕ ДНИ -")
    lines.append(f"Макс. просадка за день: ${val['worst_day']:,.2f} ({worst_day_date})")
    lines.append(f"Всего дней в минусе: {val.get('losing_days_n', 0)} из {val['n_days']}")
    lines.append(f"Самая длинная серия подряд: {val.get('longest_loss_streak', 0)} дн.  |  "
                 f"серий из 2+ дней подряд: {val.get('multi_loss_streaks_n', 0)}")
    wsd = val.get("worst_streak_detail")
    if wsd:
        d_from = dt.datetime.utcfromtimestamp(wsd["from"]).strftime("%Y-%m-%d")
        d_to   = dt.datetime.utcfromtimestamp(wsd["to"]).strftime("%Y-%m-%d")
        lines.append(f"Макс. суммарный убыток за серию подряд: ${val.get('worst_streak_loss', 0.0):,.2f}  "
                     f"({wsd['days']}д: {d_from} -> {d_to})")
    for det in (val.get("multi_loss_streaks_detail") or [])[:15]:
        d_from = dt.datetime.utcfromtimestamp(det["from"]).strftime("%Y-%m-%d")
        d_to   = dt.datetime.utcfromtimestamp(det["to"]).strftime("%Y-%m-%d")
        lines.append(f"   подряд {det['days']}д: {d_from} -> {d_to}  (убыток за серию: "
                     f"${det.get('total_loss', 0.0):,.2f})")

    # Разбивка по парам (как в v1.0)
    pair_stats = val.get("pair_stats") or []
    if pair_stats:
        lines.append("")
        lines.append("- ПО ПАРАМ -")
        for ps in pair_stats:
            mark = "🟢" if ps["pnl"] > 0 else ("🔴" if ps["pnl"] < 0 else "⚪")
            lines.append(
                f"{mark} {ps['pair']}: {ps['n']} сделок (🟢{ps['wins']}/🔴{ps['losses']}), "
                f"PnL ${ps['pnl']:,.2f}, winrate {ps['winrate']:.0f}%, "
                f"L={ps['long_n']}(🟢{ps['long_wins']}) S={ps['short_n']}(🟢{ps['short_wins']})"
            )
    return lines


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================

def main():
    if B is None:
        print(f"[ERROR] bot.py недоступен: {_BOT_IMPORT_ERR}")
        print(f"Запускайте через диспетчер: RUN_BACKTEST=meanrev_rsi_bb_v11 python bot.py")
        sys.exit(1)

    global _PAIRS_USED
    pairs = list(B.UPSCALE_PAIRS)
    _PAIRS_USED = pairs

    start = os.environ.get("BT_START", BACKTEST_START_ISO)
    end   = os.environ.get("BT_END", BACKTEST_END_ISO)
    B.send_telegram(
        f"🚀 *{STRATEGY_NAME} {STRATEGY_VERSION}* [{STRATEGY_FILE}] старт: "
        f"{len(pairs)} пар, окно {start} -> {end or 'сегодня'}"
    )

    result = run_backtest(pairs, start_iso=start, end_iso=end, verbose=True)
    val    = validate(result)

    lines = format_report(result, val, n_pairs=len(pairs))
    B.send_blocks(lines)

    try:
        import json
        out_dir = "/home/z/my-project/download"
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/{STRATEGY_FILE}_result.json", "w") as f:
            json.dump({
                "strategy": STRATEGY_NAME,
                "version": STRATEGY_VERSION,
                "file": STRATEGY_FILE,
                "validation": {k: (v if not isinstance(v, bool) else int(v))
                               for k, v in val.items()},
                "trades": result["trades"][:200],
                "equity_curve_tail": result["equity_curve"][-60:],
            }, f, indent=2, default=str)
    except Exception as e:
        print(f"[warn] не удалось сохранить результат: {e}")

    return 0 if val["all_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
