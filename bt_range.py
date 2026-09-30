"""
bt_range.py — проверка адаптивного потолка диапазона (v8.8) на реальной истории.

Отвечает на один вопрос: сколько ЗАРЯДов теряется из-за жёсткого потолка «диапазон ≤ 5%»
и что даёт замена его на адаптивный (k × ATR × √окно).

Запуск на Render: переменная окружения RUN_BACKTEST=range
Результат приходит в Telegram и печатается в лог.

Считает по каждой паре за BT_DAYS дней:
  • сколько раз окно прошло все фильтры заряда со старым потолком;
  • сколько — с новым;
  • какие заряды новый потолок добавил (и что с ними было дальше: вышла ли цена
    из коридора и куда — это честная проверка, что добавленные заряды не мусор).

Ничего не торгует и ничего не меняет в боте.
"""
import os
import time
import math
import statistics
from datetime import datetime

import bot as B


BT_DAYS   = int(os.environ.get("BT_DAYS", "30"))
BT_PAIRS  = int(os.environ.get("BT_PAIRS", "0"))      # 0 = все пары из UPSCALE_PAIRS
BT_STEP   = int(os.environ.get("BT_STEP", "4"))       # шаг проверки окон, в свечах
OLD_FIXED_CAP = 5.0                                   # жёсткий потолок v8.3, с которым сравниваем


def _fetch(sym: str, need: int):
    """История свечей под таймфрейм заряда. Gate отдаёт максимум 2000 за раз."""
    out = []
    try:
        out = B.get_candles(sym, B.CHARGE_TF, min(need, 1990))
    except Exception as e:
        print(f"[BT] {sym}: свечи не получены ({e})")
    return out or []


def _range_caps(win, atr_norm, price, W):
    """Старый и новый потолок размаха для окна."""
    hi = max(c["h"] for c in win)
    lo = min(c["l"] for c in win)
    if lo <= 0 or price <= 0:
        return None
    rng_pct = (hi - lo) / lo * 100
    atr_pct = atr_norm / price * 100
    exp_range = B.ACC_RANGE_ATR_K * atr_pct * (W ** 0.5)
    new_cap = min(B.ACC_MAX_RANGE_ABS, max(B.ACC_RANGE_FLOOR_PCT, exp_range))
    return {"hi": hi, "lo": lo, "rng_pct": rng_pct, "atr_pct": atr_pct,
            "old_cap": OLD_FIXED_CAP, "new_cap": new_cap}


def _passes_core(win, closed_upto, atr_norm, baseline, W):
    """Остальные условия заряда, кроме потолка размаха: цена стоит, сжатие, объём.
    OI не берём — за историю его не восстановить, поэтому ветка объёма только по rvol."""
    move = win[-1]["c"] - win[0]["o"]
    if abs(move) > B.ALT_P["flat_atr_eff"] * atr_norm:
        return False
    closes = [c["c"] for c in closed_upto]
    sq = B.bb_width_percentile(closes)
    trs = B.true_ranges(closed_upto)
    if len(trs) < W:
        return False
    tr_ratio = (sum(trs[-W:]) / W) / atr_norm if atr_norm > 0 else 99
    squeezed = (sq is not None and sq <= B.ACC_SQUEEZE_PCTL) or tr_ratio <= B.ACC_TR_RATIO_MAX
    if not squeezed:
        return False
    half = W // 2
    vol_half = sum(c["v"] for c in win[-half:]) / half
    return baseline > 0 and (vol_half / baseline) >= B.ACC_RVOL_MIN


def _outcome(future, hi, lo):
    """Что было после заряда: вышла ли цена из коридора и куда."""
    if not future:
        return "нет данных"
    for c in future:
        if c["h"] > hi:
            return "вверх"
        if c["l"] < lo:
            return "вниз"
    return "осталась внутри"


def run():
    pairs = B.UPSCALE_PAIRS[:BT_PAIRS] if BT_PAIRS else B.UPSCALE_PAIRS
    W = B.ACC_WINDOW
    per_day = 24 * 60 // B.TF_MIN
    need = min(1990, BT_DAYS * per_day + B.BASE_FROM + W + 10)
    follow = max(4, int(B.FOLLOW_HOURS * 60 / B.TF_MIN))

    only_old = only_new = both = 0
    added = []          # заряды, которые добавил новый потолок
    caps_seen = []
    checked = 0
    t0 = time.time()

    for i, sym in enumerate(pairs, 1):
        candles = _fetch(sym, need)
        if len(candles) < B.BASE_FROM + W + follow + 5:
            continue
        closed = candles[:-1]           # последняя свеча может быть незакрытой
        start = B.BASE_FROM + W
        for end in range(start, len(closed) - follow, BT_STEP):
            upto = closed[:end]
            win = upto[-W:]
            base_slice = upto[-B.BASE_FROM:-B.BASE_TO]
            if len(base_slice) < 10:
                continue
            baseline = B.trimmed_mean([c["v"] for c in base_slice])
            trs = B.true_ranges(upto)
            atr_norm = B.trimmed_mean(trs[-B.BASE_FROM:-B.BASE_TO])
            if atr_norm <= 0 or baseline <= 0:
                continue
            price = win[-1]["c"]
            caps = _range_caps(win, atr_norm, price, W)
            if not caps:
                continue
            checked += 1
            if not _passes_core(win, upto, atr_norm, baseline, W):
                continue
            ok_old = caps["rng_pct"] <= caps["old_cap"]
            ok_new = caps["rng_pct"] <= caps["new_cap"]
            caps_seen.append(caps["new_cap"])
            if ok_old and ok_new:
                both += 1
            elif ok_old:
                only_old += 1
            elif ok_new:
                only_new += 1
                added.append({
                    "sym": sym, "rng": caps["rng_pct"], "cap": caps["new_cap"],
                    "atr_pct": caps["atr_pct"],
                    "out": _outcome(closed[end:end + follow], caps["hi"], caps["lo"]),
                })
        if i % 10 == 0:
            print(f"[BT] {i}/{len(pairs)} пар, окон проверено {checked}, "
                  f"добавлено новым потолком {only_new}")

    took = time.time() - t0
    out_stats = {}
    for a in added:
        out_stats[a["out"]] = out_stats.get(a["out"], 0) + 1
    n_add = len(added)

    lines = [
        f"🧪 <b>Бэктест потолка диапазона</b> ({B.CHARGE_TF}, {BT_DAYS} дн, {len(pairs)} пар)",
        f"Окон проверено: {checked}, время {took/60:.1f} мин",
        "",
        f"Проходят оба варианта: <b>{both}</b>",
        f"Только старый (жёсткие {OLD_FIXED_CAP}%): <b>{only_old}</b>  ← их новый потолок отсекает",
        f"Только новый (адаптивный): <b>{n_add}</b>",
    ]
    old_total, new_total = both + only_old, both + n_add
    if old_total > 0:
        lines.append(f"Итого зарядов: старый потолок {old_total} → новый {new_total} "
                     f"(<b>{(new_total/old_total-1)*100:+.0f}%</b>)")
    if caps_seen:
        lines.append(f"Медианный новый потолок: {statistics.median(caps_seen):.1f}% "
                     f"(старый всегда {OLD_FIXED_CAP}%)")
    if n_add:
        lines.append("")
        lines.append("<b>Что стало с добавленными зарядами:</b>")
        for k, v in sorted(out_stats.items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: {v} ({v/n_add*100:.0f}%)")
        wide = [a for a in added if a["rng"] > 7]
        lines.append(f"Из них с размахом >7%: {len(wide)} — если их много, "
                     f"стоит снизить ACC_MAX_RANGE_ABS")
        lines.append("")
        lines.append("Примеры:")
        for a in added[:8]:
            lines.append(f"  {a['sym']}: размах {a['rng']:.1f}% при потолке {a['cap']:.1f}% "
                         f"(ATR {a['atr_pct']:.2f}%/св) → {a['out']}")
    else:
        lines.append("")
        lines.append("Новый потолок ничего не добавил — значит рынок сейчас спокойный "
                     "и дело не в потолке.")

    msg = "\n".join(lines)
    print(msg.replace("<b>", "").replace("</b>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[BT] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Бэктест упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
