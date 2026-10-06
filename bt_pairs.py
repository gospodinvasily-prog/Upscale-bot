"""
bt_pairs.py — RANGE IGNITION v1.2 (после аудита): интрадей-расширение волатильности BTC/ETH.

Запуск: RUN_BACKTEST=pairs   (бот менять не нужно)

ЛОГИКА. Рынок сжался (диапазон последних 6ч узкий) → закрытие 15м свечи за границу
диапазона + буфер, объём ≥ K × медианы суток → вход по ОТКРЫТИЮ следующей свечи по ходу
пробоя → TP1 1R (50%), TP2 2R (50%), после TP1 стоп в безубыток → всё закрывается не позже
ближайших 22:00 МСК и не позже чем через 8ч. Окно сигналов 14:00–21:30 МСК.

ЧТО ИСПРАВЛЕНО ПРИ АУДИТЕ v1.1 (до запуска, до цифр):
 1. КОНТРОЛЬ «МОНЕТКА» БЫЛ СЛОМАН. Он разворачивал направление, но оставлял стоп основной
    сделки. У шорта стоп оказывался НИЖЕ входа и «выбивался» на первой же свече как +0.9R.
    На стоящем рынке контроль давал +0.74R. Теперь у каждой стороны стоп на своей стороне.
 2. СЖАТИЕ «6ч ≤ 2×ATR15» ПОЧТИ НЕ БЫВАЕТ: диапазон 6ч у BTC в норме 4–6 ATR15, условие
    выполняется в ~0.07% баров, сделок не набралось бы вообще. Теперь по умолчанию сжатие =
    диапазон 6ч в нижних IG_SQ_PCT % за последние 7 суток. Прежнее определение осталось
    (IG_SQ_MODE=atr).
 3. ГЛУБИНА ИСТОРИИ. 15м свечи Gate доступны ~100 дней, а не 540. Если у Gate меньше
    IG_MIN_DAYS, берутся публичные свечи Binance (BTC и ETH на разных биржах движутся
    практически одинаково). Источник и охват печатаются в отчёте.
 4. ВХОД по открытию следующей свечи (в v1.1 — по закрытию сигнальной).
 5. ДОВЕРИТЕЛЬНЫЕ ИНТЕРВАЛЫ ПО ДНЯМ. Сделки одного дня делят рынок и перекрываются по времени
    удержания (до 8ч при паузе 2ч), поэтому обычный интервал был бы слишком узким.
 6. КРИТЕРИЙ ① ПО ОБЪЕДИНЁННОЙ ВЫБОРКЕ BTC+ETH: по одному инструменту n≥300 недостижимо.

КОНТРОЛИ (каждый отвечает на свой вопрос):
  NOVOL  — те же сжатие и пробой, но слабый объём: даёт ли объём что-то?
  NOSQ   — пробой с объёмом, но БЕЗ сжатия: даёт ли что-то сжатие? (в v1.1 контроля не было)
  OUTWIN — те же пробои вне окна 14:00–21:30: даёт ли что-то сессия?
  COIN   — случайное направление на тех же входах; ANTI — против пробоя (фейд).
  Половины периода по порядку сделок + худший месяц.

КРИТЕРИИ (фиксированы до прогона): ① MAIN ≥ +0.08R значимо при n ≥ IG_N_MIN (300);
② MAIN ≥ NOVOL + 0.05R; ③ MAIN не хуже OUTWIN; ④ обе половины в плюсе;
⑤ знак держится при IG_OFFSET=180. ①+② обязательны.
"""
import os
import time
import random
import traceback
import statistics
from datetime import datetime, timezone, timedelta

import requests

import bot as B

DAYS       = int(os.environ.get("IG_DAYS", "540"))
OFFSET     = int(os.environ.get("IG_OFFSET", "0"))
PAIRS      = [s.strip().upper() for s in os.environ.get("IG_PAIRS", "BTC,ETH").split(",") if s.strip()]
SOURCE     = os.environ.get("IG_SOURCE", "auto").lower()          # auto | gate | binance
MIN_DAYS   = int(os.environ.get("IG_MIN_DAYS", "150"))            # меньше — пробуем Binance
SQ_H       = int(os.environ.get("IG_SQ_H", "6"))
SQ_MODE    = os.environ.get("IG_SQ_MODE", "pct").lower()          # pct | atr
SQ_PCT     = float(os.environ.get("IG_SQ_PCT", "25"))             # сжатие: нижние N% диапазонов за 7 суток
SQ_K       = float(os.environ.get("IG_SQ_K", "2.0"))              # для режима atr
VOL_K      = float(os.environ.get("IG_VOL_K", "2.0"))
BUF_K      = float(os.environ.get("IG_BUF_K", "0.10"))
STOP_MIN   = float(os.environ.get("IG_STOP_MIN", "0.5"))
STOP_ATR   = float(os.environ.get("IG_STOP_ATR", "1.2"))
TP1R, TP2R = 1.0, 2.0
FEE        = float(os.environ.get("IG_FEE", "0.05"))
SLIP       = float(os.environ.get("IG_SLIP", "0.03"))
STOP_SLIP  = float(os.environ.get("IG_STOP_SLIP", "0.03"))
MAX_HOLD_H = float(os.environ.get("IG_MAX_HOLD_H", "8"))
SKIP_BARS  = int(os.environ.get("IG_SKIP_BARS", "8"))
N_MIN      = int(os.environ.get("IG_N_MIN", "300"))
MIN_BARS_PATH = 4                                                 # меньше часа до выхода — не считаем
WIN        = (14 * 60, 21 * 60 + 30)
EOD_HOUR   = 22
MSK, SEC   = timezone(timedelta(hours=3)), 900
SQ_BARS    = SQ_H * 4
TRAIL      = 7 * 96                                               # окно для процентиля сжатия
BINANCE    = "https://data-api.binance.vision/api/v3/klines"


# ═════════════ данные ═════════════

def fetch_gate(sym, days, offset):
    now = int(time.time()) - offset * 86400
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "15m",
                                         "from": cur, "to": min(now, cur + 1900 * SEC)})
        part = B.parse_candles(raw) if raw else []
        if not part:
            probes += 1
            if probes > 25:
                break
            cur += 5 * 86400
            continue
        out.extend(part)
        nxt = part[-1].get("t", 0) + SEC
        if nxt <= cur:
            break
        cur = nxt
    return _dedupe(out)


def fetch_binance(sym, days, offset, get=None):
    """Публичные свечи Binance (спот): пагинация по startTime, до 1000 свечей за запрос.
    Объём берётся как есть: нужны только ОТНОШЕНИЯ внутри одного ряда, единицы не важны."""
    get = get or requests.get
    now_ms = (int(time.time()) - offset * 86400) * 1000
    start = now_ms - days * 86400 * 1000
    out, note = [], ""
    while start < now_ms:
        try:
            r = get(BINANCE, params={"symbol": f"{sym}USDT", "interval": "15m",
                                     "startTime": start, "endTime": now_ms, "limit": 1000},
                    timeout=15)
        except Exception as e:
            note = f"Binance недоступен: {e}"
            break
        if r.status_code != 200:
            note = f"Binance ответил {r.status_code}"
            break
        rows = r.json()
        if not rows:
            break
        for k in rows:
            try:
                out.append({"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
                            "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
            except (TypeError, ValueError, IndexError):
                continue
        nxt = int(rows[-1][0]) + SEC * 1000
        if nxt <= start:
            break
        start = nxt
        time.sleep(0.05)
    return _dedupe(out), note


def _dedupe(rows):
    seen, u = set(), []
    for c in sorted(rows, key=lambda x: x.get("t", 0)):
        if c.get("t") not in seen:
            seen.add(c["t"])
            u.append(c)
    return u


def fetch(sym):
    """Возвращает (свечи, источник, примечание). Последняя (незакрытая) свеча отбрасывается."""
    notes = []
    gate = []
    if SOURCE in ("auto", "gate"):
        gate = fetch_gate(sym, DAYS, OFFSET)
    cov = (gate[-1]["t"] - gate[0]["t"]) / 86400 if len(gate) > 1 else 0.0
    if SOURCE == "gate" or (SOURCE == "auto" and cov >= MIN_DAYS):
        return gate[:-1], "Gate", ""
    if SOURCE == "auto":
        notes.append(f"у Gate только {cov:.0f} дн 15м-свечей")
    bn, note = fetch_binance(sym, DAYS, OFFSET)
    if note:
        notes.append(note)
    bcov = (bn[-1]["t"] - bn[0]["t"]) / 86400 if len(bn) > 1 else 0.0
    if bcov > cov:
        return bn[:-1], "Binance (спот)", "; ".join(notes)
    return gate[:-1], "Gate", "; ".join(notes + ["Binance не дал больше данных"])


# ═════════════ сделка ═════════════

def sim(fut, is_long, ent, stp):
    """TP1/TP2 по половине, после TP1 стоп в безубыток. В спорной свече — стоп."""
    risk = abs(ent - stp)
    if risk <= 0 or not fut:
        return None
    t1 = ent + (risk * TP1R if is_long else -risk * TP1R)
    t2 = ent + (risk * TP2R if is_long else -risk * TP2R)
    fee, acc, done, cs = FEE / 100 * ent / risk, 0.0, 0, stp
    for b in fut:
        if (b["l"] <= cs) if is_long else (b["h"] >= cs):
            px = cs * (1 - STOP_SLIP / 100) if is_long else cs * (1 + STOP_SLIP / 100)
            r = (px - ent) / risk if is_long else (ent - px) / risk
            left = 1.0 if done == 0 else 0.5
            return acc + r * left - fee - FEE / 100 * px * left / risk
        while done < 2:
            t = t1 if done == 0 else t2
            if (b["h"] >= t) if is_long else (b["l"] <= t):
                acc += 0.5 * (abs(t - ent) / risk)
                fee += FEE / 100 * t * 0.5 / risk
                done += 1
                if done == 1:
                    cs = ent
            else:
                break
        if done >= 2:
            return acc - fee
    last = fut[-1]["c"]
    r = (last - ent) / risk if is_long else (ent - last) / risk
    left = 1.0 if done == 0 else 0.5
    return acc + r * left - fee - FEE / 100 * last * left / risk


def trade(fut, px, is_long, stp_pct):
    """Сделка в заданную сторону от цены px: проскальзывание входа и СТОП НА СВОЕЙ СТОРОНЕ.
    Одна и та же функция для основной сделки и контролей — иначе контроль нечестный."""
    ent = px * (1 + SLIP / 100) if is_long else px * (1 - SLIP / 100)
    stp = ent * (1 - stp_pct / 100) if is_long else ent * (1 + stp_pct / 100)
    return sim(fut, is_long, ent, stp)


# ═════════════ сигналы ═════════════

def next_eod_ts(close_ts):
    dt = datetime.fromtimestamp(close_ts, MSK)
    eod = dt.replace(hour=EOD_HOUR, minute=0, second=0, microsecond=0)
    if eod <= dt:
        eod += timedelta(days=1)
    return eod.timestamp()


def find_events(c):
    """Все пробои, прошедшие проверки, разложенные по категориям. Только данные ДО сигнала:
    сигнальная свеча закрыта, вход по открытию следующей."""
    n = len(c)
    if n < TRAIL + SQ_BARS + 200:
        return [], {}
    # диапазон 6ч «до» каждого бара: считается только по предыдущим барам
    rng = [None] * n
    for j in range(SQ_BARS, n):
        w = c[j - SQ_BARS:j]
        rng[j] = (max(x["h"] for x in w) - min(x["l"] for x in w)) / c[j - 1]["c"] * 100
    gaps = [0] * n
    for j in range(1, n):
        gaps[j] = gaps[j - 1] + (1 if c[j]["t"] - c[j - 1]["t"] != SEC else 0)

    funnel = {"bars": 0, "breakouts": 0, "squeezed": 0, "squeezed_atr": 0, "vol_ok": 0,
              "in_window": 0}
    events = []
    last_i = {}
    start = TRAIL + SQ_BARS
    for i in range(start, n - MIN_BARS_PATH - 2):
        if gaps[i + 1] - gaps[i - 100] > 0:
            continue                                  # дыра в данных рядом
        funnel["bars"] += 1
        sig = c[i]
        atr = B.trimmed_mean(B.true_ranges(c[i - 1 - 96:i]))
        if not atr or atr <= 0:
            continue
        atr_pct = atr / sig["c"] * 100
        win = c[i - SQ_BARS:i]
        hi, lo = max(x["h"] for x in win), min(x["l"] for x in win)
        up = sig["c"] > hi * (1 + BUF_K * atr_pct / 100)
        dn = sig["c"] < lo * (1 - BUF_K * atr_pct / 100)
        if not (up or dn):
            continue
        funnel["breakouts"] += 1
        r6 = rng[i]
        sq_atr = r6 <= SQ_K * atr_pct
        funnel["squeezed_atr"] += 1 if sq_atr else 0
        if SQ_MODE == "atr":
            squeezed = sq_atr
        else:
            past = sorted(x for x in rng[i - TRAIL:i] if x is not None)
            squeezed = bool(past) and r6 <= past[int(SQ_PCT / 100 * (len(past) - 1))]
        funnel["squeezed"] += 1 if squeezed else 0
        vn = statistics.median([x.get("v", 0) or 0 for x in c[i - 96:i]]) or 0
        volok = vn > 0 and (sig.get("v", 0) or 0) >= VOL_K * vn
        funnel["vol_ok"] += 1 if volok else 0
        close_ts = sig["t"] + SEC
        dt = datetime.fromtimestamp(close_ts, MSK)
        mins = dt.hour * 60 + dt.minute
        inwin = WIN[0] <= mins < WIN[1]
        funnel["in_window"] += 1 if inwin else 0

        if squeezed and volok and inwin:
            cat = "main"
        elif squeezed and not volok and inwin:
            cat = "novol"
        elif squeezed and volok and not inwin:
            cat = "outwin"
        elif (not squeezed) and volok and inwin:
            cat = "nosq"
        else:
            continue
        if i - last_i.get(cat, -10 ** 9) < SKIP_BARS:
            continue                                  # одна волна — один сигнал
        t_limit = min(next_eod_ts(close_ts), close_ts + MAX_HOLD_H * 3600)
        j = i + 1
        while j < n and c[j]["t"] + SEC <= t_limit:
            j += 1
        fut = c[i + 1:j]
        if len(fut) < MIN_BARS_PATH or fut[0]["t"] != sig["t"] + SEC:
            continue
        stp_pct = max(STOP_MIN, STOP_ATR * atr_pct)
        px = fut[0]["o"]                              # вход по ОТКРЫТИЮ следующей свечи
        r = trade(fut, px, up, stp_pct)
        if r is None:
            continue
        last_i[cat] = i
        events.append({"cat": cat, "r": r, "up": up, "t": close_ts, "px": px, "fut": fut,
                       "stp_pct": stp_pct, "day": dt.strftime("%Y-%m-%d"),
                       "month": dt.strftime("%Y-%m")})
    return events, funnel


def add_controls(events, rng):
    """COIN (случайное направление) и ANTI (против пробоя) на тех же входах MAIN."""
    for e in events:
        if e["cat"] != "main":
            continue
        e["r_anti"] = trade(e["fut"], e["px"], not e["up"], e["stp_pct"])
        e["r_coin"] = trade(e["fut"], e["px"], rng.random() < 0.5, e["stp_pct"])


# ═════════════ статистика ═════════════

def cluster_ci(rs, days):
    """(n, среднее, полуширина 95%-интервала). Сделки одного дня — один кластер:
    они делят рынок и перекрываются по времени."""
    n = len(rs)
    if n == 0:
        return None
    m = sum(rs) / n
    by = {}
    for r, d in zip(rs, days):
        by[d] = by.get(d, 0.0) + (r - m)
    se = (sum(v * v for v in by.values())) ** 0.5 / n
    return n, m, 1.96 * se


def _line(evs, label, key="r"):
    vals = [(e[key], e["day"]) for e in evs if e.get(key) is not None]
    if not vals:
        return f"  {label}: сделок нет"
    rs, days = [v for v, _ in vals], [d for _, d in vals]
    n, m, ci = cluster_ci(rs, days)
    wr = sum(1 for x in rs if x > 0) / n * 100
    mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
    return f"  {mark} {label}: {n:4} сд, ВР {wr:3.0f}%, <b>{m:+.3f}R</b> (±{ci:.3f}), {sum(rs):+.0f}R"


def _stat(evs, key="r"):
    vals = [(e[key], e["day"]) for e in evs if e.get(key) is not None]
    return cluster_ci([v for v, _ in vals], [d for _, d in vals]) if vals else None


def evaluate_criteria(ev):
    """Критерии ①–④ по объединённой выборке.
    ① различает ТРИ исхода: эффект есть / эффекта нет / сделок мало для вывода.
    Возвращает (строки, статус① 'ok'|'no'|'few', ②)."""
    main = [e for e in ev if e["cat"] == "main"]
    novol = [e for e in ev if e["cat"] == "novol"]
    outw = [e for e in ev if e["cat"] == "outwin"]
    sm, sn, so = _stat(main), _stat(novol), _stat(outw)
    if not sm:
        st1 = "few"
    elif sm[0] < N_MIN:
        st1 = "few"
    else:
        st1 = "ok" if sm[1] - sm[2] > 0.08 else "no"
    ok2 = bool(sm and sn and sm[1] - sn[1] >= 0.05)
    ok3 = bool(sm and (not so or sm[1] >= so[1]))
    ok4 = False
    if len(main) >= 20:
        ms = sorted(main, key=lambda e: e["t"])
        h = len(ms) // 2
        a, b = _stat(ms[:h]), _stat(ms[h:])
        ok4 = bool(a and b and a[1] > 0 and b[1] > 0)
    mark1 = {"ok": "✅", "no": "❌", "few": "⚠️"}[st1]
    tail1 = (f" (n={sm[0]}, {sm[1]:+.3f}R, ±{sm[2]:.3f})" if sm else "")
    if st1 == "few" and sm:
        tail1 += f" — сделок меньше {N_MIN}, вывод по этому пункту невозможен"
    rows = [f"  {mark1} ① MAIN ≥ +0.08R значимо при n ≥ {N_MIN}" + tail1,
            f"  {'✅' if ok2 else '❌'} ② MAIN ≥ NOVOL + 0.05R"
            + (f" ({sm[1] - sn[1]:+.3f}R)" if sm and sn else ""),
            f"  {'✅' if ok3 else '❌'} ③ MAIN не хуже OUTWIN"
            + (f" ({sm[1] - so[1]:+.3f}R)" if sm and so else ""),
            f"  {'✅' if ok4 else '❌'} ④ обе половины периода в плюсе"]
    return rows, st1, ok2


# ═════════════ отчёт ═════════════

def run():
    rnd = random.Random(2024)
    L = [f"🔥 <b>RANGE IGNITION v1.2</b>: {', '.join(PAIRS)}, запрошено {DAYS} дн 15м"
         + (f"\n⏪ <b>СДВИНУТ НА {OFFSET} ДН</b> — проверка на чужом периоде" if OFFSET else ""),
         f"<i>сжатие 6ч: "
         + (f"диапазон в нижних {SQ_PCT:.0f}% за 7 суток" if SQ_MODE == "pct"
            else f"диапазон ≤ {SQ_K}×ATR15")
         + f" → пробой +{BUF_K}×ATR, объём ≥{VOL_K}× медианы суток, окно "
         f"{WIN[0] // 60:02d}:{WIN[0] % 60:02d}–{WIN[1] // 60:02d}:{WIN[1] % 60:02d} МСК</i>",
         f"<i>вход по открытию следующей свечи | стоп max({STOP_MIN}%, {STOP_ATR}×ATR15) | "
         f"TP {TP1R}R/{TP2R}R 50/50, безубыток после TP1 | выход не позже 22:00 МСК и +{MAX_HOLD_H:.0f}ч | "
         f"издержки {(FEE + SLIP) * 2:.2f}% на круг + проскальзывание стопа {STOP_SLIP}%</i>", ""]
    all_ev = []
    for sym in PAIRS:
        c, src, note = fetch(sym)
        cov = (c[-1]["t"] - c[0]["t"]) / 86400 if len(c) > 1 else 0.0
        gaps = sum(1 for a, b in zip(c, c[1:]) if b["t"] - a["t"] != SEC)
        L.append(f"── <b>{sym}</b> ── источник: {src}, охват {cov:.0f} дн, {len(c)} свечей, "
                 f"пропусков {gaps}" + (f" <i>({note})</i>" if note else ""))
        ev, fn = find_events(c)
        if not fn:
            L.append("  ⚠️ истории слишком мало для расчёта")
            L.append("")
            continue
        add_controls(ev, rnd)
        for e in ev:
            e["sym"] = sym
        all_ev += ev
        L.append(f"  воронка: баров {fn['bars']} → пробоев {fn['breakouts']} → "
                 f"со сжатием {fn['squeezed']} → с объёмом {fn['vol_ok']} (среди всех пробоев) "
                 f"→ в окне {fn['in_window']}")
        L.append(f"  <i>для справки: прежнее сжатие «≤{SQ_K}×ATR15» выполнено у {fn['squeezed_atr']} "
                 f"пробоев из {fn['breakouts']}</i>")
        L.append(_line([e for e in ev if e["cat"] == "main"], "MAIN"))
        L.append(_line([e for e in ev if e["cat"] == "novol"], "NOVOL (слабый объём)"))
        L.append(_line([e for e in ev if e["cat"] == "nosq"], "NOSQ  (без сжатия)"))
        L.append(_line([e for e in ev if e["cat"] == "outwin"], "OUTWIN (вне окна)"))
        mains = [e for e in ev if e["cat"] == "main"]
        L.append(_line(mains, "COIN (случайное направление)", "r_coin"))
        L.append(_line(mains, "ANTI (против пробоя)", "r_anti"))
        L.append(_line([e for e in mains if e["up"]], "  MAIN лонги"))
        L.append(_line([e for e in mains if not e["up"]], "  MAIN шорты"))
        L.append("")

    if all_ev:
        L.append("── <b>ОБЪЕДИНЕНО</b> ──")
        mains = [e for e in all_ev if e["cat"] == "main"]
        for cat, lbl in (("main", "MAIN"), ("novol", "NOVOL"), ("nosq", "NOSQ"),
                         ("outwin", "OUTWIN")):
            L.append(_line([e for e in all_ev if e["cat"] == cat], lbl))
        L.append(_line(mains, "COIN", "r_coin"))
        L.append(_line(mains, "ANTI", "r_anti"))
        if len(mains) >= 20:
            ms = sorted(mains, key=lambda e: e["t"])
            h = len(ms) // 2
            a, b = _stat(ms[:h]), _stat(ms[h:])
            if a and b:
                L.append(f"  половины MAIN по порядку: {a[1]:+.3f}R ({a[0]} сд) | "
                         f"{b[1]:+.3f}R ({b[0]} сд)")
            by_m = {}
            for e in mains:
                by_m.setdefault(e["month"], []).append(e["r"])
            if len(by_m) >= 3:
                w = min(by_m.items(), key=lambda kv: sum(kv[1]))
                L.append(f"  худший месяц: {w[0]} ({sum(w[1]):+.1f}R, {len(w[1])} сд)")
        L.append("")
        rows, st1, ok2 = evaluate_criteria(all_ev)
        L.append("<b>КРИТЕРИИ</b> (интервалы по дням, объединённая выборка)")
        L += rows
        L.append("  ⑤ повторить с IG_OFFSET=180 — знак должен совпасть (не автоматизируется)")
        sm_ = _stat([e for e in all_ev if e["cat"] == "main"])
        if st1 == "ok" and ok2:
            verdict = "КРИТЕРИИ ①+② ВЫПОЛНЕНЫ — смотреть ③–⑤"
        elif st1 == "few" and sm_ and sm_[1] + sm_[2] < 0:
            verdict = "ВЫБОРКА МАЛА, НО MAIN ЗНАЧИМО В МИНУСЕ — архив"
        elif st1 == "few":
            verdict = "ВЫВОД НЕВОЗМОЖЕН: СДЕЛОК МАЛО — нужна длиннее история или мягче сжатие"
        else:
            verdict = "КРИТЕРИИ НЕ ВЫПОЛНЕНЫ — архив"
        L.append("  → <b>" + verdict + "</b>")
        L.append("<i>если MAIN в плюсе, а NOVOL или NOSQ такой же — эдж не в объёме или не в "
                 "сжатии, а в самом пробое; это другая гипотеза, не эта</i>")
    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[IG] отправка: {e}")


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ ignition упал: {traceback.format_exc()[-400:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
