"""
bt_pairs.py — PULSE-D (дневной импульс) + IGNITION и SWEEP (интрадей) на BTC/ETH.

ПО УМОЛЧАНИЮ запускается PULSE-D (PAIRS_RUN=pulsed). Интрадей-части: PAIRS_RUN=ignition|sweep|both|all.

IGNITION + SWEEP: две интрадей-проверки на BTC/ETH.

Запуск: RUN_BACKTEST=pairs    (бот менять не нужно; прогоняются ОБЕ стратегии подряд)
  PAIRS_RUN=both|ignition|sweep   — что именно гонять (по умолчанию both)

Общее: свечи 15м BTC/ETH. Gate отдаёт ~100 дней, поэтому при нехватке берутся публичные
свечи Binance (источник и охват печатаются). Вход по ОТКРЫТИЮ следующей свечи, выход не позже
ближайших 22:00 МСК и не позже чем через 8ч, пауза после сигнала, контроли «монетка» и «против»
со СТОПОМ НА СВОЕЙ СТОРОНЕ, интервалы по дням, критерии по объединённой выборке BTC+ETH.

IGNITION — сжатие диапазона 6ч → пробой с объёмом (настройки IG_*).
SWEEP    — снятие суточных экстремумов (настройки SW_*):
  CONT — закрытие за экстремум 24ч, диапазон свечи >= K×ATR, объём >= K×медианы → по ходу;
  FAKE — прокол экстремума и закрытие обратно внутрь → против выноса.
  Контроли: COIN (случайное направление), ANTI (против), NOVOL (слабый объём), OUTWIN
  (вне окна), разрез EQ/одиночные экстремумы (касания считаются раздельно, не соседние бары).

ЧТО ИСПРАВЛЕНО ПРИ АУДИТЕ SWEEP v1.1:
 1. Свеча, проколовшая ОБА суточных экстремума, давала «лонг» со стопом ВЫШЕ входа и
    записывалась как +0.92R. Такие свечи теперь пропускаются.
 2. Вход по закрытию сигнальной свечи → по открытию следующей.
 3. sim() падал на пустом пути (ветка NOVOL без проверки длины).
 4. EQ-разрез считал соседние бары у вершины как разные касания — теперь касания должны
    быть разделены минимум SW_EQ_GAP барами.
 5. История 540 дней у Gate недоступна → запасной источник Binance.
 6. Контроль считался по входу со сдвигом в другую сторону проскальзывания; теперь та же
    функция trade() и для основной сделки, и для контролей.
 7. Критерии по объединённой выборке с тремя исходами: есть эффект / нет / мало сделок.
"""
IGNITION_DOC = """
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


def fetch_binance(sym, days, offset, get=None, interval="15m"):
    """Публичные свечи Binance (спот): пагинация по startTime, до 1000 свечей за запрос.
    Объём берётся как есть: нужны только ОТНОШЕНИЯ внутри одного ряда, единицы не важны."""
    get = get or requests.get
    step = {"15m": SEC, "1d": 86400}[interval]
    now_ms = (int(time.time()) - offset * 86400) * 1000
    start = now_ms - days * 86400 * 1000
    out, note = [], ""
    while start < now_ms:
        try:
            r = get(BINANCE, params={"symbol": f"{sym}USDT", "interval": interval,
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
        nxt = int(rows[-1][0]) + step * 1000
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


_CACHE = {}


def fetch_for(sym, days, offset):
    """Возвращает (свечи, источник, примечание). Последняя (незакрытая) свеча отбрасывается.
    Результат кэшируется: IGNITION и SWEEP на одном периоде качают данные один раз."""
    key = (sym, days, offset, SOURCE)
    if key in _CACHE:
        return _CACHE[key]
    notes = []
    gate = []
    if SOURCE in ("auto", "gate"):
        gate = fetch_gate(sym, days, offset)
    cov = (gate[-1]["t"] - gate[0]["t"]) / 86400 if len(gate) > 1 else 0.0
    if SOURCE == "gate" or (SOURCE == "auto" and cov >= MIN_DAYS):
        res = (gate[:-1], "Gate", "")
    else:
        if SOURCE == "auto":
            notes.append(f"у Gate только {cov:.0f} дн 15м-свечей")
        bn, note = fetch_binance(sym, days, offset)
        if note:
            notes.append(note)
        bcov = (bn[-1]["t"] - bn[0]["t"]) / 86400 if len(bn) > 1 else 0.0
        if bcov > cov:
            res = (bn[:-1], "Binance (спот)", "; ".join(notes))
        else:
            res = (gate[:-1], "Gate", "; ".join(notes + ["Binance не дал больше данных"]))
    _CACHE[key] = res
    return res


def fetch(sym):
    return fetch_for(sym, DAYS, OFFSET)


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


# ═════════════ разложение результата: валовый эдж и издержки ═════════════

def edge_block(evs, label):
    """MAIN и ANTI торгуют ОДНИ И ТЕ ЖЕ события в противоположные стороны с одинаковым
    риском, поэтому издержки у них общие, а эдж направления входит с разным знаком:
        MAIN = +e − c,  ANTI = −e − c   →   e = (MAIN − ANTI)/2,  c = −(MAIN + ANTI)/2.
    Это сравнение без случайности контроля «монетка» и без предположения об издержках."""
    pairs = [(e["r"], e["r_anti"], e["day"]) for e in evs if e.get("r_anti") is not None]
    if len(pairs) < 30:
        return []
    edge = [(r - a) / 2 for r, a, _ in pairs]
    n, m, ci = cluster_ci(edge, [d for *_, d in pairs])
    cost = -sum((r + a) / 2 for r, a, _ in pairs) / len(pairs)
    mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
    return [f"  {mark} {label}: валовый эдж направления <b>{m:+.3f}R</b> (±{ci:.3f}) при "
            f"издержках {cost:.3f}R → чистый {m - cost:+.3f}R"]


COST_SCALES = (1.0, 0.7, 0.4)


def cost_table(evs, label, risk_key):
    """Чистый результат при сниженных издержках: пересчёт тех же сделок. Показывает, при каких
    издержках система вообще могла бы работать. Реальные комиссии и проскальзывание Upscale
    нужно взять из истории сделок — это решающий параметр."""
    global FEE, SLIP, STOP_SLIP
    if len(evs) < 30:
        return []
    save = (FEE, SLIP, STOP_SLIP)
    cells = []
    try:
        for k in COST_SCALES:
            FEE, SLIP, STOP_SLIP = save[0] * k, save[1] * k, save[2] * k
            rs, ds = [], []
            for e in evs:
                r = trade(e["fut"], e["px"], e["up"], e[risk_key])
                if r is not None:
                    rs.append(r)
                    ds.append(e["day"])
            st = cluster_ci(rs, ds)
            if st:
                cells.append(f"издержки круга {2 * (save[0] + save[1]) * k:.3f}% → "
                             f"<b>{st[1]:+.3f}R</b> (±{st[2]:.3f})")
    finally:
        FEE, SLIP, STOP_SLIP = save
    return [f"  {label} при разных издержках: " + " | ".join(cells)] if cells else []


def overlap_note(offset, days):
    if offset and offset < days:
        return [f"⚠️ <i>период пересекается с основным на {days - offset} дн — для независимой "
                f"проверки нужен сдвиг не меньше {days} дн</i>"]
    return []


# ═════════════ отчёт ═════════════

def run_ignition():
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
         f"издержки {(FEE + SLIP) * 2:.2f}% на круг + проскальзывание стопа {STOP_SLIP}%</i>"]
    L += overlap_note(OFFSET, DAYS)
    L.append("")
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
        L += edge_block(mains, "MAIN")
        L += cost_table(mains, "MAIN", "stp_pct")
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



# ═════════════════════════════════════════════════════════════════════════════
#  SWEEP — разгон и ложный вынос при снятии суточных экстремумов (BTC/ETH, интрадей)
# ═════════════════════════════════════════════════════════════════════════════
SW_DAYS      = int(os.environ.get("SW_DAYS", str(DAYS)))
SW_OFFSET    = int(os.environ.get("SW_OFFSET", str(OFFSET)))
SW_LOOK      = int(os.environ.get("SW_LOOK", "96"))               # сутки по 15м
SW_RNG_K     = float(os.environ.get("SW_RNG_K", "1.3"))           # диапазон сигнальной >= K×ATR
SW_VOL_K     = float(os.environ.get("SW_VOL_K", "1.5"))           # объём >= K×медианы
SW_EQ_TOL    = float(os.environ.get("SW_EQ_TOL", "0.12"))         # % допуск касания экстремума
SW_EQ_GAP    = int(os.environ.get("SW_EQ_GAP", "8"))              # касания раздельны, если >= N баров
SW_STOP_F    = float(os.environ.get("SW_STOP_F", "0.4"))          # % пол стопа
SW_MAX_HOLD_H = float(os.environ.get("SW_MAX_HOLD_H", "8"))
SW_SKIP_BARS = int(os.environ.get("SW_SKIP_BARS", "8"))
SW_WIN       = (7 * 60, 21 * 60)                                  # закрытие сигнала, МСК
SW_N_CONT    = int(os.environ.get("SW_N_MIN", "300"))
SW_N_FAKE    = int(os.environ.get("SW_N_MIN_FAKE", "200"))


def sweep_entry_stop(px, is_long, ext, atr):
    """Вход по px со сдвигом, стоп за экстремум сигнальной свечи ±0.05×ATR, пол SW_STOP_F %.
    Если структурный стоп оказался не с той стороны (цена открылась за ним) — тоже пол."""
    ent = px * (1 + SLIP / 100) if is_long else px * (1 - SLIP / 100)
    stp = ext - 0.05 * atr if is_long else ext + 0.05 * atr
    wrong = (stp >= ent) if is_long else (stp <= ent)
    if wrong or abs(ent - stp) / ent * 100 < SW_STOP_F:
        stp = ent * (1 - SW_STOP_F / 100) if is_long else ent * (1 + SW_STOP_F / 100)
    return ent, stp


def count_touches(prev, level, is_high):
    """Сколько РАЗДЕЛЬНЫХ касаний экстремума было за сутки. Соседние бары у вершины —
    одно касание, иначе почти любая вершина считалась бы «двойной»."""
    tol = SW_EQ_TOL / 100
    idx = [k for k, x in enumerate(prev)
           if (x["h"] >= level * (1 - tol) if is_high else x["l"] <= level * (1 + tol))]
    if not idx:
        return 0
    n = 1
    for a, b in zip(idx, idx[1:]):
        if b - a >= SW_EQ_GAP:
            n += 1
    return n


def find_sweep(c):
    """События CONT / FAKE и контроли NOVOL / OUTWIN. Только данные ДО сигнала."""
    n = len(c)
    if n < SW_LOOK + 400:
        return [], {}
    gaps = [0] * n
    for j in range(1, n):
        gaps[j] = gaps[j - 1] + (1 if c[j]["t"] - c[j - 1]["t"] != SEC else 0)
    fn = {"bars": 0, "beyond": 0, "beyond_range": 0, "vol_ok": 0, "fake_raw": 0, "fake_both": 0}
    events, last_i = [], {}
    for i in range(SW_LOOK + 2, n - MIN_BARS_PATH - 2):
        if gaps[i + 1] - gaps[i - 100] > 0:
            continue
        fn["bars"] += 1
        s = c[i]
        atr = B.trimmed_mean(B.true_ranges(c[i - 1 - 96:i]))
        if not atr or atr <= 0:
            continue
        prev = c[i - SW_LOOK:i]
        hi, lo = max(x["h"] for x in prev), min(x["l"] for x in prev)
        if hi <= 0 or lo <= 0:
            continue
        close_ts = s["t"] + SEC
        dt = datetime.fromtimestamp(close_ts, MSK)
        mins = dt.hour * 60 + dt.minute
        inwin = SW_WIN[0] <= mins < SW_WIN[1]
        beyond_up, beyond_dn = s["c"] > hi, s["c"] < lo
        fake_up = s["h"] > hi and s["c"] < hi
        fake_dn = s["l"] < lo and s["c"] > lo
        up = False
        eq = None
        if beyond_up or beyond_dn:
            fn["beyond"] += 1
            if (s["h"] - s["l"]) < SW_RNG_K * atr:
                continue
            fn["beyond_range"] += 1
            vmed = statistics.median([x.get("v", 0) or 0 for x in prev]) or 0
            volok = vmed > 0 and (s.get("v", 0) or 0) >= SW_VOL_K * vmed
            fn["vol_ok"] += 1 if volok else 0
            up = beyond_up
            ext = s["l"] if up else s["h"]
            cat = ("cont" if inwin else "outwin") if volok else "novol"
            if cat == "novol" and not inwin:
                continue
            eq = count_touches(prev, hi if up else lo, up) >= 2
        elif fake_up or fake_dn:
            fn["fake_raw"] += 1
            if fake_up and fake_dn:
                fn["fake_both"] += 1                    # свеча шире суточного диапазона — не считаем
                continue
            if not inwin:
                continue
            cat = "fake"
            up = fake_up                                # вынос вверх → ШОРТ
            ext = s["h"] if fake_up else s["l"]
        else:
            continue
        if i - last_i.get(cat, -10 ** 9) < SW_SKIP_BARS:
            continue
        t_limit = min(next_eod_ts(close_ts), close_ts + SW_MAX_HOLD_H * 3600)
        j = i + 1
        while j < n and c[j]["t"] + SEC <= t_limit:
            j += 1
        fut = c[i + 1:j]
        if len(fut) < MIN_BARS_PATH or fut[0]["t"] != s["t"] + SEC:
            continue
        px = fut[0]["o"]                                # вход по ОТКРЫТИЮ следующей свечи
        is_long = (not up) if cat == "fake" else up     # FAKE — против выноса
        ent, stp = sweep_entry_stop(px, is_long, ext, atr)
        r = sim(fut, is_long, ent, stp)
        if r is None:
            continue
        last_i[cat] = i
        events.append({"cat": cat, "r": r, "up": is_long, "t": close_ts, "px": px, "fut": fut,
                       "risk_pct": abs(ent - stp) / ent * 100, "eq": eq,
                       "day": dt.strftime("%Y-%m-%d"), "month": dt.strftime("%Y-%m")})
    return events, fn


def add_sweep_controls(events, rng):
    """COIN и ANTI на тех же входах CONT и FAKE. Риск тот же, стоп на своей стороне."""
    for e in events:
        if e["cat"] not in ("cont", "fake"):
            continue
        e["r_anti"] = trade(e["fut"], e["px"], not e["up"], e["risk_pct"])
        e["r_coin"] = trade(e["fut"], e["px"], rng.random() < 0.5, e["risk_pct"])
        if e["r_coin"] is not None:
            e["d_coin"] = e["r"] - e["r_coin"]


def _status(evs, n_min, thr):
    """Исход критерия ①: ok / no / few (мало сделок для вывода)."""
    st = _stat(evs)
    if not st or st[0] < n_min:
        return "few", st
    return ("ok" if st[1] - st[2] > thr else "no"), st


def sweep_verdict(ev, kind):
    """Критерии одной формы (cont или fake) по объединённой выборке."""
    main = [e for e in ev if e["cat"] == kind]
    n_min, thr = (SW_N_CONT, 0.05) if kind == "cont" else (SW_N_FAKE, 0.05)
    st1, sm = _status(main, n_min, thr)
    dc = _stat(main, "d_coin")
    ok2 = bool(dc and dc[1] >= 0.05)
    mark1 = {"ok": "✅", "no": "❌", "few": "⚠️"}[st1]
    rows = [f"  {mark1} ① {kind.upper()} ≥ +0.05R значимо при n ≥ {n_min}"
            + (f" (n={sm[0]}, {sm[1]:+.3f}R, ±{sm[2]:.3f})" if sm else "")
            + (" — сделок мало, вывод по пункту невозможен" if st1 == "few" and sm else "")]
    rows.append(f"  {'✅' if ok2 else '❌'} ② {kind.upper()} − COIN ≥ +0.05R"
                + (f" ({dc[1]:+.3f}R, ±{dc[2]:.3f})" if dc else ""))
    ok3 = None
    if kind == "cont":
        nv = [e for e in ev if e["cat"] == "novol"]
        snv = _stat(nv)
        ok3 = bool(sm and snv and sm[1] - snv[1] >= 0.03)
        rows.append(f"  {'✅' if ok3 else '❌'} ③ CONT − NOVOL ≥ +0.03R"
                    + (f" ({sm[1] - snv[1]:+.3f}R)" if sm and snv else ""))
    ok4 = False
    if len(main) >= 20:
        ms = sorted(main, key=lambda e: e["t"])
        h = len(ms) // 2
        a, b = _stat(ms[:h]), _stat(ms[h:])
        ok4 = bool(a and b and a[1] > 0 and b[1] > 0)
        rows.append(f"  {'✅' if ok4 else '❌'} ⑤ обе половины в плюсе"
                    + (f" ({a[1]:+.3f}R | {b[1]:+.3f}R)" if a and b else ""))
    if st1 == "ok" and ok2:
        verdict = "①+② ВЫПОЛНЕНЫ — смотреть ③ и ⑤, потом offset"
    elif st1 == "few" and sm and sm[1] + sm[2] < 0:
        verdict = "сделок мало, но значимо в минусе — закрывать"
    elif st1 == "few":
        verdict = "ВЫВОД НЕВОЗМОЖЕН: сделок мало"
    else:
        verdict = "КРИТЕРИИ НЕ ВЫПОЛНЕНЫ"
    return rows, verdict


def run_sweep():
    rnd = random.Random(777)
    L = [f"⚡ <b>SWEEP v1.2</b>: {', '.join(PAIRS)}, запрошено {SW_DAYS} дн 15м"
         + (f"\n⏪ <b>СДВИНУТ НА {SW_OFFSET} ДН</b> — проверка на чужом периоде" if SW_OFFSET else ""),
         f"<i>CONT: закрытие за экстремум {SW_LOOK // 4}ч, диапазон свечи ≥{SW_RNG_K}×ATR, "
         f"объём ≥{SW_VOL_K}× медианы → по ходу | FAKE: прокол и закрытие внутрь → против</i>",
         f"<i>вход по открытию следующей свечи | стоп за свечу ±0.05×ATR, пол {SW_STOP_F}% | "
         f"TP {TP1R}R/{TP2R}R 50/50, безубыток после TP1 | выход не позже 22:00 МСК и "
         f"+{SW_MAX_HOLD_H:.0f}ч | окно {SW_WIN[0] // 60:02d}:00–{SW_WIN[1] // 60:02d}:00 МСК | "
         f"издержки {(FEE + SLIP) * 2:.2f}% на круг</i>"]
    L += overlap_note(SW_OFFSET, SW_DAYS)
    L.append("")
    all_ev = []
    for sym in PAIRS:
        c, src, note = fetch_for(sym, SW_DAYS, SW_OFFSET)
        cov = (c[-1]["t"] - c[0]["t"]) / 86400 if len(c) > 1 else 0.0
        gaps = sum(1 for a, b in zip(c, c[1:]) if b["t"] - a["t"] != SEC)
        L.append(f"── <b>{sym}</b> ── источник: {src}, охват {cov:.0f} дн, {len(c)} свечей, "
                 f"пропусков {gaps}" + (f" <i>({note})</i>" if note else ""))
        ev, fn = find_sweep(c)
        if not fn:
            L.append("  ⚠️ истории слишком мало для расчёта")
            L.append("")
            continue
        add_sweep_controls(ev, rnd)
        for e in ev:
            e["sym"] = sym
        all_ev += ev
        L.append(f"  воронка: баров {fn['bars']} → закрытий за экстремум {fn['beyond']} → "
                 f"с размахом ≥{SW_RNG_K}×ATR {fn['beyond_range']} → с объёмом {fn['vol_ok']} | "
                 f"проколов с возвратом {fn['fake_raw']} (двусторонних отброшено {fn['fake_both']})")
        for kind, name in (("cont", "CONT"), ("fake", "FAKE")):
            m = [e for e in ev if e["cat"] == kind]
            L.append(_line(m, name))
            L.append(_line(m, f"  {name} COIN (случайно)", "r_coin"))
            L.append(_line(m, f"  {name} ANTI (против)", "r_anti"))
            L.append(_line([e for e in m if e["up"]], f"  {name} лонги"))
            L.append(_line([e for e in m if not e["up"]], f"  {name} шорты"))
            if kind == "cont":
                L.append(_line([e for e in ev if e["cat"] == "novol"], "  NOVOL (слабый объём)"))
                L.append(_line([e for e in ev if e["cat"] == "outwin"], "  OUTWIN (вне окна)"))
                L.append(_line([e for e in m if e["eq"]], "  CONT на двойных экстремумах"))
                L.append(_line([e for e in m if e["eq"] is False], "  CONT на одиночных"))
        L.append("")

    if all_ev:
        L.append("── <b>ОБЪЕДИНЕНО</b> ──")
        for kind, name in (("cont", "CONT"), ("fake", "FAKE")):
            m = [e for e in all_ev if e["cat"] == kind]
            L.append(_line(m, name))
            L.append(_line(m, f"  {name} COIN", "r_coin"))
            L.append(_line(m, f"  {name} ANTI", "r_anti"))
            L += edge_block(m, name)
            L += cost_table(m, name, "risk_pct")
            by_m = {}
            for e in m:
                by_m.setdefault(e["month"], []).append(e["r"])
            if len(by_m) >= 3:
                w = min(by_m.items(), key=lambda kv: sum(kv[1]))
                L.append(f"  худший месяц {name}: {w[0]} ({sum(w[1]):+.1f}R, {len(w[1])} сд)")
        L.append("")
        L.append("<b>КРИТЕРИИ</b> (интервалы по дням, объединённая выборка)")
        for kind in ("cont", "fake"):
            rows, verdict = sweep_verdict(all_ev, kind)
            L += rows
            L.append(f"  → <b>{kind.upper()}: {verdict}</b>")
        L.append("  ⑤ повторить со сдвигом (SW_OFFSET=180) — знак должен совпасть")
        L.append("<i>CONT и FAKE оцениваются независимо — может выжить один. Если провал ① у "
                 "обоих, ускорение при снятии стопов после издержек не монетизируется входом "
                 "по закрытию 15м</i>")
    _send(L)


def _send(L):
    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[PAIRS] отправка: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  PULSE-D — дневной импульс: сильный день → лонг на следующие 24ч (BTC/ETH + расширенный набор)
# ═════════════════════════════════════════════════════════════════════════════
PD_DAYS   = int(os.environ.get("PD_DAYS", "1825"))
PD_OFFSET = int(os.environ.get("PD_OFFSET", "0"))
PD_PAIRS  = [x.strip().upper() for x in os.environ.get("PD_PAIRS", "BTC,ETH").split(",") if x.strip()]
PD_EXT    = [x.strip().upper() for x in os.environ.get(
    "PD_EXT", "SOL,XRP,BNB,ADA,DOGE,LINK,LTC,AVAX").split(",") if x.strip()]
PD_RET_K  = float(os.environ.get("PD_RET_K", "1.5"))
PD_CLV_K  = float(os.environ.get("PD_CLV_K", "0.6"))
PD_VOL_K  = float(os.environ.get("PD_VOL_K", "1.2"))
PD_WEAK_K = float(os.environ.get("PD_WEAK_K", "0.5"))
PD_RIDE_MAX = int(os.environ.get("PD_RIDE_MAX", "10"))
PD_FUND   = float(os.environ.get("PD_FUND", "0.03"))        # %/день: лонг платит, шорт получает
PD_N_MIN  = int(os.environ.get("PD_N_MIN", "150"))
PD_COST   = (FEE + SLIP) * 2                                # % на круг, как в интрадей-частях
_DCACHE = {}


def fetch_gate_daily(sym, days, offset):
    now = int(time.time()) - offset * 86400
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1d",
                                         "from": cur, "to": min(now, cur + 900 * 86400)})
        part = B.parse_candles(raw) if raw else []
        if not part:
            probes += 1
            if probes > 6:
                break
            cur += 200 * 86400
            continue
        out.extend(part)
        nxt = part[-1].get("t", 0) + 86400
        if nxt <= cur:
            break
        cur = nxt
    return _dedupe(out)


def fetch_daily_for(sym, days, offset):
    """(свечи, источник, примечание). Gate, если дал почти весь период, иначе Binance."""
    key = (sym, days, offset, SOURCE)
    if key in _DCACHE:
        return _DCACHE[key]
    need = days + 30
    gate = fetch_gate_daily(sym, need, offset) if SOURCE in ("auto", "gate") else []
    notes = []
    if SOURCE == "gate" or (SOURCE == "auto" and len(gate) >= 0.95 * need):
        res = (gate[:-1], "Gate", "")
    else:
        if SOURCE == "auto":
            notes.append(f"у Gate {len(gate)} дн из {need}")
        bn, note = fetch_binance(sym, need, offset, interval="1d")
        if note:
            notes.append(note)
        if len(bn) > len(gate):
            res = (bn[:-1], "Binance (спот)", "; ".join(notes))
        else:
            res = (gate[:-1], "Gate", "; ".join(notes + ["Binance не дал больше данных"]))
    _DCACHE[key] = res
    return res


def pd_events(c):
    """Категории дневных сделок. Решение по ЗАКРЫТОМУ дню D, вход по ОТКРЫТИЮ дня D+1, выход
    по закрытию D+1 (1-day) либо по закрытию первого «красного» дня, максимум PD_RIDE_MAX.
    Возвращает {категория: [(нетто %, дата, месяц, год)]}. Все цифры уже за вычетом издержек
    и фандинга."""
    cat = {k: [] for k in ("long", "short", "ride", "ride_s", "weak", "ret_only", "clv_only",
                           "vol_only", "base", "rest")}
    for i in range(21, len(c) - 1):
        d, prev, nx = c[i], c[i - 1], c[i + 1]
        rng = d["h"] - d["l"]
        if rng <= 0 or prev["c"] <= 0 or nx["o"] <= 0:
            continue
        clv = (d["c"] - d["l"]) / rng
        ret = (d["c"] / prev["c"] - 1) * 100
        vmed = statistics.median([x.get("v", 0) or 0 for x in c[i - 20:i]]) or 0
        volok = vmed > 0 and (d.get("v", 0) or 0) >= PD_VOL_K * vmed
        nd = (nx["c"] / nx["o"] - 1) * 100                  # следующие 24ч: открытие → закрытие
        dt = datetime.fromtimestamp(d["t"], timezone.utc)
        tag = (dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m"), dt.year)
        net_l = nd - PD_COST - PD_FUND
        cat["base"].append((net_l,) + tag)
        up_strong = ret >= PD_RET_K and clv >= PD_CLV_K and volok
        dn_strong = ret <= -PD_RET_K and clv <= 1 - PD_CLV_K and volok
        if up_strong:
            cat["long"].append((net_l,) + tag)
        else:
            cat["rest"].append((net_l,) + tag)                # честная база: дни БЕЗ сигнала
        if dn_strong:
            cat["short"].append((-nd - PD_COST + PD_FUND,) + tag)
        if ret >= PD_RET_K:
            cat["ret_only"].append((net_l,) + tag)
        if clv >= PD_CLV_K and ret > 0:
            cat["clv_only"].append((net_l,) + tag)
        if volok and ret > 0:
            cat["vol_only"].append((net_l,) + tag)
        if PD_WEAK_K <= ret < PD_RET_K and clv >= PD_CLV_K and volok:
            cat["weak"].append((net_l,) + tag)
        # RIDE (описательно): держим до первого дня, закрывшегося ниже/выше предыдущего
        for kind, sign in (("ride", 1), ("ride_s", -1)):
            if (kind == "ride" and not up_strong) or (kind == "ride_s" and not dn_strong):
                continue
            exit_px, held = None, 0
            for k in range(i + 1, min(i + 1 + PD_RIDE_MAX, len(c))):
                held += 1
                if (c[k]["c"] < c[k - 1]["c"]) if sign == 1 else (c[k]["c"] > c[k - 1]["c"]):
                    exit_px = c[k]["c"]
                    break
            if exit_px is None:
                exit_px = c[min(i + PD_RIDE_MAX, len(c) - 1)]["c"]
                held = min(PD_RIDE_MAX, len(c) - 1 - i)
            r = (exit_px / nx["o"] - 1) * 100 * sign - PD_COST - sign * PD_FUND * held
            cat[kind].append((r,) + tag)
    return cat


def pd_merge(cats):
    out = {}
    for cat in cats:
        for k, v in cat.items():
            out.setdefault(k, []).extend(v)
    return out


def pd_stat(items):
    """(n, среднее %, полуширина 95%). Кластеры — МЕСЯЦЫ: сильные дни собираются в одних
    режимах рынка, а BTC и ETH ходят вместе, так что обычный интервал был бы слишком узким."""
    if not items:
        return None
    return cluster_ci([x[0] for x in items], [x[2] for x in items])


def pd_line(items, label):
    st = pd_stat(items)
    if not st:
        return f"  {label}: сделок нет"
    n, m, ci = st
    wr = sum(1 for x in items if x[0] > 0) / n * 100
    mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
    return f"  {mark} {label}: {n:5} сд, ВР {wr:3.0f}%, <b>{m:+.3f}%</b> (±{ci:.3f})"


def pd_criteria(pool):
    """Критерии по объединённой выборке. ② — против дней БЕЗ сигнала, а не против шорта:
    у шорта на растущем рынке разница равна удвоенному дрейфу и проходит сама по себе."""
    main, rest, weak = pool.get("long", []), pool.get("rest", []), pool.get("weak", [])
    sm, sr, sw = pd_stat(main), pd_stat(rest), pd_stat(weak)
    if not sm or sm[0] < PD_N_MIN:
        st1 = "few"
    else:
        st1 = "ok" if (sm[1] >= 0.15 and sm[1] - sm[2] > 0) else "no"
    d = ci_d = None
    ok2 = False
    if sm and sr:
        d = sm[1] - sr[1]
        ci_d = (sm[2] ** 2 + sr[2] ** 2) ** 0.5
        ok2 = d >= 0.10 and d - ci_d > 0
    ok3 = bool(sm and sw and sm[1] > sw[1])
    ok4, halves = False, ""
    if len(main) >= 20:
        ms = sorted(main, key=lambda x: x[1])
        h = len(ms) // 2
        a, b = pd_stat(ms[:h]), pd_stat(ms[h:])
        if a and b:
            ok4 = a[1] > 0 and b[1] > 0
            halves = f" ({a[1]:+.3f}% | {b[1]:+.3f}%)"
    mark1 = {"ok": "✅", "no": "❌", "few": "⚠️"}[st1]
    rows = [f"  {mark1} ① LONG нетто ≥ +0.15% значимо при n ≥ {PD_N_MIN}"
            + (f" (n={sm[0]}, {sm[1]:+.3f}%, ±{sm[2]:.3f})" if sm else "")
            + (" — сделок мало" if st1 == "few" and sm else ""),
            f"  {'✅' if ok2 else '❌'} ② LONG − дни без сигнала ≥ +0.10% значимо"
            + (f" ({d:+.3f}%, ±{ci_d:.3f})" if d is not None else ""),
            f"  {'✅' if ok3 else '❌'} ③ доза: сильные дни лучше слабых"
            + (f" ({sm[1]:+.3f}% против {sw[1]:+.3f}%)" if sm and sw else ""),
            f"  {'✅' if ok4 else '❌'} ④ обе половины периода в плюсе{halves}"]
    if st1 == "ok" and ok2:
        verdict = "①+② ВЫПОЛНЕНЫ — смотреть ③, ④ и чужой период"
    elif st1 == "few":
        verdict = "ВЫВОД НЕВОЗМОЖЕН: сделок мало"
    else:
        verdict = "КРИТЕРИИ НЕ ВЫПОЛНЕНЫ"
    return rows, verdict, sm


def pd_pool_block(title, pool):
    L = [f"── <b>{title}</b> ──",
         pd_line(pool.get("long", []), "LONG 24ч после сильного дня (основная)"),
         pd_line(pool.get("rest", []), "ДНИ БЕЗ СИГНАЛА (честная база: дрейф рынка)"),
         pd_line(pool.get("base", []), "все дни (для справки)"),
         pd_line(pool.get("weak", []), "WEAK: ret 0.5–1.5% (доза)"),
         pd_line(pool.get("ret_only", []), "ret-only (без CLV/объёма)"),
         pd_line(pool.get("clv_only", []), "clv-only (без ret/объёма)"),
         pd_line(pool.get("vol_only", []), "vol-only (без ret/CLV)"),
         pd_line(pool.get("ride", []), "RIDE: до первого красного дня (перекрываются, описательно)"),
         pd_line(pool.get("short", []), "SHORT после сильного падения (справочно)")]
    by_year = {}
    for x in pool.get("long", []):
        by_year.setdefault(x[3], []).append(x[0])
    if len(by_year) >= 3:
        L.append("  по годам LONG: " + " | ".join(
            f"{y}: {sum(v) / len(v):+.2f}% ({len(v)})" for y, v in sorted(by_year.items())))
    rows, verdict, sm = pd_criteria(pool)
    L.append("  <b>критерии</b>")
    L += rows
    if sm:
        L.append(f"  <i>минимально различимый эффект при этой выборке ≈ ±{sm[2]:.2f}% на сделку: "
                 f"всё меньше неотличимо от шума</i>")
    L.append(f"  → <b>{verdict}</b>")
    return L


def run_pulsed():
    L = [f"🌀 <b>PULSE-D v1.1: дневной импульс</b> — запрошено {PD_DAYS} дн"
         + (f"\n⏪ <b>СДВИНУТ НА {PD_OFFSET} ДН</b>" if PD_OFFSET else ""),
         f"<i>сигнал по закрытию дня: ret ≥{PD_RET_K}% + CLV ≥{PD_CLV_K} + объём ≥{PD_VOL_K}× "
         f"медианы 20д → вход по ОТКРЫТИЮ следующего дня, выход по его закрытию | издержки "
         f"{PD_COST:.2f}% + фандинг {PD_FUND}%/день (лонг платит)</i>"]
    L += overlap_note(PD_OFFSET, PD_DAYS)
    L.append("")
    per, ext = {}, {}
    for sym in PD_PAIRS + [x for x in PD_EXT if x not in PD_PAIRS]:
        c, src, note = fetch_daily_for(sym, PD_DAYS, PD_OFFSET)
        yrs = (c[-1]["t"] - c[0]["t"]) / 86400 / 365 if len(c) > 1 else 0.0
        ok = len(c) >= 400
        L.append(f"  {sym}: источник {src}, {len(c)} дн ({yrs:.1f} л)"
                 + (f" <i>({note})</i>" if note else "") + ("" if ok else " ⚠️ истории мало"))
        if not ok:
            continue
        ev = pd_events(c)
        (per if sym in PD_PAIRS else ext)[sym] = ev
    L.append("")
    if per:
        pool = pd_merge(per.values())
        L += pd_pool_block(f"{'+'.join(per)} (основная проверка по пре-регу)", pool)
        L.append("")
    if ext and per:
        big = pd_merge(list(per.values()) + list(ext.values()))
        L += pd_pool_block(f"РАСШИРЕННЫЙ НАБОР: {'+'.join(list(per) + list(ext))}", big)
        L.append("  <i>на альтах издержки обычно выше заложенных 0.16%, поэтому набор нужен "
                 "для мощности, а не как готовая цена торговли</i>")
        L.append("")
    L.append("<i>проверка на чужом периоде: PD_OFFSET = PD_DAYS (данные Binance с 2017 г., "
             "период будет короче). Сдвиг на 365 дн пересекается с основным на 4 года из 5</i>")
    _send(L)


# ═════════════ запуск ═════════════

PAIRS_RUN = os.environ.get("PAIRS_RUN", "pulsed").lower()    # pulsed | ignition | sweep | both | all


def run():
    if PAIRS_RUN in ("both", "all", "ignition"):
        run_ignition()
    if PAIRS_RUN in ("both", "all", "sweep"):
        run_sweep()
    if PAIRS_RUN in ("all", "pulsed"):
        run_pulsed()


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ pairs (ignition/sweep) упал: {traceback.format_exc()[-400:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
