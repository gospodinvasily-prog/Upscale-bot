"""
bt_pairs.py — X-MOM: кросс-секционный моментум.

Запускается привычным RUN_BACKTEST=pairs — менять bot.py не нужно.

Гипотеза: монеты, обгонявшие рынок за MOM_H часов, продолжают обгонять следующие
HOLD_D дней. Лонг топ-N по доходности, шорт боттом-N, равные слоты.

Почему это может работать там, где не сработало всё прежнее. Горизонт дневной,
а не часовой: издержки (комиссия + проскальзывание ≈0.2% на круг) размазываются
по движению в несколько процентов, а не съедают его целиком. Все прошлые системы
упирались ровно в это — найденное преимущество было меньше стоимости входа.

Тайминг честный: ранжирование считается по свече, ЗАКРЫТОЙ до момента входа;
вход по открытию следующей свечи; выход по открытию свечи через HOLD_D дней.

КОНТРОЛЬ встроен: тот же портфель из СЛУЧАЙНЫХ пар. Если наш не лучше случайного —
ранжирование не работает, и в живой режим это не идёт.

Настройки (переменные Render):
  XM_DAYS (90)      — сколько дней истории
  XM_OFFSET (0)     — сдвиг окна назад; 90 даст предыдущие 90 дней — проверка на подгонку
  XM_MOM_H (72)     — окно доходности для ранжирования, часов
  XM_TOP (5)        — сколько пар в каждую сторону
  XM_HOLD_D (3)     — длина цикла, дней
  XM_POSITION (500) — размер слота, $
  XM_FEE (0.05), XM_SLIP (0.05) — издержки на сторону, %
"""
import os
import time
import random
import statistics

import bot as B

DAYS      = int(os.environ.get("XM_DAYS", "90"))
OFFSET    = int(os.environ.get("XM_OFFSET", "0"))
MOM_H     = int(os.environ.get("XM_MOM_H", "168"))
TOP_N     = int(os.environ.get("XM_TOP", "5"))
HOLD_D    = int(os.environ.get("XM_HOLD_D", "5"))
POS_USD   = float(os.environ.get("XM_POSITION", "500"))
FEE_PCT   = float(os.environ.get("XM_FEE", "0.05"))
SLIP_PCT  = float(os.environ.get("XM_SLIP", "0.05"))

# Дополнительные окна доходности и длины цикла — заодно смотрим, не лучше ли другое.
MOM_GRID  = [int(x) for x in os.environ.get("XM_MOM_GRID", "120,168,240,336").split(",")]
HOLD_GRID = [int(x) for x in os.environ.get("XM_HOLD_GRID", "3,5,7,10").split(",")]


def _fetch_1h(sym, days):
    """Часовые свечи за days дней. Если данных с начала окна нет, не сдаёмся,
    а щупаем вперёд: Gate хранит историю не для всех пар одинаково."""
    sec = 3600
    now = int(time.time()) - OFFSET * 86400
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        to = min(now, cur + 900 * sec)
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1h",
                                         "from": cur, "to": to})
        part = B.parse_candles(raw) if raw else []
        if not part:
            probes += 1
            if probes > 30:
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
    need = DAYS + max(MOM_GRID) // 24 + 5
    print(f"[XMOM] качаю 1ч свечи: {len(pairs)} пар × {need} дн (сдвиг назад {OFFSET} дн)")
    t0 = time.time()

    data = {}
    for i, sym in enumerate(pairs, 1):
        try:
            ch = _fetch_1h(sym, need)
        except Exception:
            continue
        if len(ch) > (max(MOM_GRID) // 24 + max(HOLD_GRID) + 2) * 24:
            data[sym] = {c["t"]: c for c in ch[:-1]}      # последняя свеча незакрыта
        if i % 20 == 0:
            print(f"[XMOM] {i}/{len(pairs)} | пар с данными {len(data)} | "
                  f"{time.time() - t0:.0f}с")

    print(f"[XMOM] пар с данными: {len(data)}")
    if len(data) < 20:
        B.send_telegram("⚠️ X-MOM: данных не хватило (менее 20 пар)")
        return

    ref = max(data.values(), key=len)
    times = sorted(ref.keys())
    cost = (FEE_PCT + SLIP_PCT) / 100 * 2        # вход и выход
    rnd = random.Random(12345)

    def slot(sym, side, t_in, t_out):
        en, ex = data[sym].get(t_in), data[sym].get(t_out)
        if not en or not ex or en["o"] <= 0:
            return None
        raw = (ex["o"] / en["o"] - 1) * (1 if side == "long" else -1)
        return raw - cost

    def ranking(t_close, mom_h):
        """Доходность за mom_h часов по ЗАКРЫТЫМ свечам. Подглядывания нет."""
        t_old = t_close - mom_h * 3600
        rets = {}
        for sym, m in data.items():
            a, b = m.get(t_old), m.get(t_close)
            if a and b and a["c"] > 0:
                rets[sym] = b["c"] / a["c"] - 1
        if len(rets) < TOP_N * 4:
            return None
        srt = sorted(rets.items(), key=lambda kv: kv[1], reverse=True)
        return [s for s, _ in srt[:TOP_N]], [s for s, _ in srt[-TOP_N:]], rets

    def cycles_for(mom_h, hold_d, with_control=False):
        """Прогон по циклам. Возвращает (портфель, лонги, шорты, случайный)."""
        cyc, lon, sho, rc = [], [], [], []
        cycle_sec = hold_d * 86400
        t = times[0] + (mom_h + 2) * 3600
        while t + cycle_sec <= times[-1]:
            r = ranking(t - 3600, mom_h)
            t_out = t + cycle_sec
            if r is None or t_out not in ref:
                t += cycle_sec
                continue
            longs, shorts, _ = r
            pls = [x for x in (slot(s, "long", t, t_out) for s in longs) if x is not None]
            pss = [x for x in (slot(s, "short", t, t_out) for s in shorts) if x is not None]
            ps = pls + pss
            if len(ps) >= TOP_N * 2 - 2:
                cyc.append(sum(ps) / len(ps) * 100)
                if pls:
                    lon.append(sum(pls) / len(pls) * 100)
                if pss:
                    sho.append(sum(pss) / len(pss) * 100)
                if with_control:
                    pool = [s for s in data if t in data[s] and t_out in data[s]]
                    if len(pool) >= TOP_N * 2:
                        pick = rnd.sample(pool, TOP_N * 2)
                        rs = [x for x in ([slot(s, "long", t, t_out) for s in pick[:TOP_N]]
                                          + [slot(s, "short", t, t_out) for s in pick[TOP_N:]])
                              if x is not None]
                        if rs:
                            rc.append(sum(rs) / len(rs) * 100)
            t += cycle_sec
        return cyc, lon, sho, rc

    def cycles_long(mom_h, hold_d, rng):
        """Long-only: топ-N по рангу против СЛУЧАЙНЫХ N лонгов.
        Зачем отдельно: лонг топ-N несёт в себе общий рост рынка. Сравнение со
        случайными лонгами вычитает этот общий рост и оставляет вклад самого
        ранжирования. Общий контроль (лонги+шорты вместе) этого не показывает."""
        cyc, rc = [], []
        cycle_sec = hold_d * 86400
        t = times[0] + (mom_h + 2) * 3600
        while t + cycle_sec <= times[-1]:
            r = ranking(t - 3600, mom_h)
            t_out = t + cycle_sec
            if r is None or t_out not in ref:
                t += cycle_sec
                continue
            longs, _, _ = r
            pls = [x for x in (slot(s_, "long", t, t_out) for s_ in longs) if x is not None]
            pool = [s_ for s_ in data if t in data[s_] and t_out in data[s_]]
            if len(pls) >= TOP_N - 1 and len(pool) >= TOP_N:
                pick = rng.sample(pool, TOP_N)
                rs = [x for x in (slot(s_, "long", t, t_out) for s_ in pick) if x is not None]
                if rs:                      # пара считается только когда есть ОБЕ половины
                    cyc.append(sum(pls) / len(pls) * 100)
                    rc.append(sum(rs) / len(rs) * 100)
            t += cycle_sec
        return cyc, rc

    def alpha_of(cyc, rc):
        """Разница «наши лонги минус случайные» с доверительным интервалом.
        Считается по ПАРАМ циклов, поэтому общий рост рынка вычитается честно."""
        if not cyc or len(cyc) != len(rc) or len(cyc) < 5:
            return None
        d = [a - b for a, b in zip(cyc, rc)]
        n = len(d)
        m = sum(d) / n
        se = (statistics.pstdev(d) / (n ** 0.5)) if n > 1 else 0.0
        return n, m, 1.96 * se

    cov = (times[-1] - times[0]) / 86400
    L = [f"📈 <b>X-MOM: кросс-секционный моментум</b> (~{cov:.0f} дн, {len(data)} пар)"
         + (f"\n⏪ <b>ПЕРИОД СДВИНУТ НАЗАД НА {OFFSET} ДНЕЙ</b> — проверка на чужих данных"
            if OFFSET else ""),
         f"<i>лонг топ-{TOP_N} / шорт боттом-{TOP_N} по доходности за {MOM_H}ч, "
         f"цикл {HOLD_D} дн, слот ${POS_USD:.0f}</i>",
         f"Издержки {(FEE_PCT + SLIP_PCT):.2f}% на круг. Решение по закрытой свече, "
         f"вход по открытию следующей", ""]

    # ── основная настройка ──
    cyc, lon, sho, rc = cycles_for(MOM_H, HOLD_D, with_control=True)
    if not cyc:
        L.append("⚠️ Циклов не набралось")
        B.send_blocks(L)
        return

    L.append(f"<b>ОСНОВНАЯ НАСТРОЙКА</b> (ret{MOM_H}ч, цикл {HOLD_D} дн)")
    L.append(_line(cyc, "портфель лонг+шорт"))
    eq, peak, dd, usd = 0.0, 0.0, 0.0, 0.0
    for c in cyc:
        step = c / 100 * POS_USD * TOP_N * 2
        usd += step
        eq += step
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    wins = sum(1 for c in cyc if c > 0)
    L.append(f"     итого ${usd:+,.0f} | прибыльных циклов {wins}/{len(cyc)} "
             f"({wins / len(cyc) * 100:.0f}%) | макс. просадка ${dd:,.0f}")
    half = len(cyc) // 2
    if half >= 5:
        a = statistics.mean(cyc[:half])
        b = statistics.mean(cyc[half:])
        L.append(f"     по половинам: первая {a:+.2f}% | вторая {b:+.2f}% — "
                 + ("держится" if a > 0 and b > 0 else "разваливается"))
    L.append(_line(lon, "только лонги (топ)"))
    L.append(_line(sho, "только шорты (боттом)"))
    L.append("")

    # ── контроль ──
    L.append("<b>КОНТРОЛЬ: случайные пары вместо ранжированных</b>")
    L.append("  <i>если наш портфель не лучше случайного — ранжирование не работает</i>")
    L.append(_line(rc, "случайный портфель"))
    if rc and cyc:
        diff = statistics.mean(cyc) - statistics.mean(rc)
        n = min(len(cyc), len(rc))
        ci = 1.96 * ((statistics.pstdev(cyc) ** 2 + statistics.pstdev(rc) ** 2) / n) ** 0.5
        verdict = ("ранжирование работает" if diff - ci > 0 else
                   "ранжирование ВРЕДИТ" if diff + ci < 0 else
                   "разница в пределах шума — преимущества не видно")
        L.append(f"  → <b>наш сигнал против случайного: {diff:+.2f}% за цикл</b> "
                 f"(±{ci:.2f}) — {verdict}")
    L.append("")

    # ── сетка: окно доходности × длина цикла ──
    L.append("<b>ПЕРЕБОР: окно доходности × длина цикла</b>")
    L.append("  <i>ячейка — средний результат портфеля за цикл, %</i>")
    L.append("   ret\\цикл | " + " | ".join(f" {h}д  " for h in HOLD_GRID))
    best = None
    for mh in MOM_GRID:
        row = []
        for hd in HOLD_GRID:
            c2, _, _, _ = cycles_for(mh, hd)
            st = _stat(c2)
            if st and st[0] >= 8:
                row.append(f"{st[1]:+.2f}")
                if best is None or st[1] > best[1]:
                    best = ((mh, hd), st[1], st[0], st[2])
            else:
                row.append("  —  ")
        L.append(f"   {mh:4}ч    | " + " | ".join(row))
    if best:
        L.append(f"  → лучшее: ret{best[0][0]}ч, цикл {best[0][1]}д → {best[1]:+.2f}% "
                 f"за цикл (±{best[3]:.2f}), циклов {best[2]}")
        L.append("  <i>лучшая ячейка выбрана задним числом из 20 — верить ей можно только "
                 "если она подтвердится на сдвинутом периоде (XM_OFFSET=90)</i>")
    L.append("")
    L.append("<b>LONG-ONLY: альфа над СЛУЧАЙНЫМИ лонгами</b>")
    L.append("  <i>лонг топ-N несёт общий рост рынка. Сравнение со случайными лонгами "
             "вычитает его и оставляет вклад ранжирования. Ячейка — альфа, %/цикл</i>")
    L.append("   ret\\цикл | " + " | ".join(f" {h}д  " for h in HOLD_GRID))
    rnd2 = random.Random(54321)
    best_a = None
    for mh in MOM_GRID:
        row = []
        for hd in HOLD_GRID:
            cl, rl = cycles_long(mh, hd, rnd2)
            a = alpha_of(cl, rl)
            if a and a[0] >= 8:
                row.append(f"{a[1]:+.2f}")
                if best_a is None or a[1] > best_a[1][1]:
                    best_a = ((mh, hd), a)
            else:
                row.append("  —  ")
        L.append(f"   {mh:4}ч    | " + " | ".join(row))
    if best_a:
        (mh, hd), (n, m, ci) = best_a
        verdict = ("альфа значима" if m - ci > 0 else
                   "в пределах шума — ранжирование ничего не добавляет")
        L.append(f"  → лучшая альфа: ret{mh}ч, цикл {hd}д → <b>{m:+.2f}%/цикл</b> "
                 f"(±{ci:.2f}, {n} циклов) — {verdict}")
        L.append("  <i>критерий: альфа ≥ +0.3%/цикл, значима, держится у СОСЕДНИХ ячеек "
                 "и повторяется при XM_OFFSET=90. Одиночный пик — артефакт перебора: "
                 "максимум из 20 ячеек при нулевом эдже сам по себе даёт ~1.4–1.9σ</i>")
    L.append("")
    L.append("<i>фандинг не учтён: шорты обычно получают платёж, поэтому реальность "
             "скорее чуть лучше цифр</i>")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[XMOM] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ X-MOM упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
