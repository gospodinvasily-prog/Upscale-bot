"""
bt_pairs.py — POSITIONING SCAN (lsr/топ-трейдеры/киты) + FLUSH-60 + PULSE-D + IGNITION и SWEEP.

ПО УМОЛЧАНИЮ запускается POSITIONING SCAN, оба универсума сразу (PAIRS_RUN=pos). Остальное: PAIRS_RUN=flush|pulsed|ignition|sweep|both|all.

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


# ═════════════════════════════════════════════════════════════════════════════
#  FLUSH-60 — откат после каскада ликвидаций на 60 днях истории (все альты Upscale)
# ═════════════════════════════════════════════════════════════════════════════
FL_DAYS       = int(os.environ.get("FL_DAYS", "60"))
FL_PAIRS_N    = int(os.environ.get("FL_PAIRS_N", "0"))
FL_ALIGN      = os.environ.get("FL_ALIGN", "auto").lower()      # auto | 0 | -1
FL_LIQ_MULT   = float(os.environ.get("FL_LIQ_MULT", "5"))
FL_LIQ_MIN    = float(os.environ.get("FL_LIQ_MIN", "3000"))
FL_OI_DROP    = float(os.environ.get("FL_OI_DROP", "0.8"))
FL_MOVE       = float(os.environ.get("FL_MOVE", "2.5"))
FL_MAX_STOP   = float(os.environ.get("FL_MAX_STOP", "3.0"))
FL_BUF        = float(os.environ.get("FL_BUF", "0.1"))
FL_HOLD_H     = int(os.environ.get("FL_HOLD_H", "12"))
FL_FEE        = float(os.environ.get("FL_FEE", "0.05"))
FL_SLIP       = float(os.environ.get("FL_SLIP", "0.10"))
FL_STOP_SLIP  = float(os.environ.get("FL_STOP_SLIP", "0.15"))
FL_COOLDOWN_H = int(os.environ.get("FL_COOLDOWN_H", "6"))
FL_N_MIN      = int(os.environ.get("FL_N_MIN", "100"))
FL_TP_K       = (0.4, 0.7, 1.0)
HOUR          = 3600


def fl_sim(bars, is_long, ent, stop, tps, scale=1.0, optimistic=False):
    """Три цели по трети; после TP1 стоп в безубыток, после TP2 — на TP1.
    В спорной свече первым считается СТОП; optimistic=True — тейки первыми (верхняя граница)."""
    fee_pct, stop_slip = FL_FEE * scale, FL_STOP_SLIP * scale
    risk = abs(ent - stop)
    if risk <= 0 or not bars:
        return None
    fee, acc, done, cs = fee_pct / 100 * ent / risk, 0.0, 0, stop
    parts = (1 / 3, 1 / 3, 1 / 3)

    def stop_out(left_parts):
        px = cs * (1 - stop_slip / 100) if is_long else cs * (1 + stop_slip / 100)
        r = (px - ent) / risk if is_long else (ent - px) / risk
        left = sum(left_parts)
        return acc + r * left - fee - fee_pct / 100 * px * left / risk

    for c in bars:
        if optimistic:
            hit = False
            while done < 3:
                t = tps[done]
                if (c["h"] >= t) if is_long else (c["l"] <= t):
                    acc += parts[done] * (abs(t - ent) / risk)
                    fee += fee_pct / 100 * t * parts[done] / risk
                    done += 1
                    hit = True
                    cs = ent if done == 1 else (tps[0] if done == 2 else cs)
                else:
                    break
            if done >= 3:
                return acc - fee
            if not hit and ((c["l"] <= cs) if is_long else (c["h"] >= cs)):
                return stop_out(parts[done:])
        else:
            if (c["l"] <= cs) if is_long else (c["h"] >= cs):
                return stop_out(parts[done:])
            while done < 3:
                t = tps[done]
                if (c["h"] >= t) if is_long else (c["l"] <= t):
                    acc += parts[done] * (abs(t - ent) / risk)
                    fee += fee_pct / 100 * t * parts[done] / risk
                    done += 1
                    cs = ent if done == 1 else (tps[0] if done == 2 else cs)
                else:
                    break
            if done >= 3:
                return acc - fee
    last = bars[-1]["c"]
    left = sum(parts[done:])
    r = (last - ent) / risk if is_long else (ent - last) / risk
    return acc + r * left - fee - fee_pct / 100 * last * left / risk


def fl_trade(bars, is_long, entry, stop, tps, scale=1.0, optimistic=False):
    """Вход по рынку со ПРОСКАЛЬЗЫВАНИЕМ (в присланной версии FL_SLIP нигде не применялся).
    Уровни стопа и целей — рыночные, поэтому от проскальзывания не меняются."""
    ent = entry * (1 + FL_SLIP * scale / 100) if is_long else entry * (1 - FL_SLIP * scale / 100)
    if (is_long and stop >= ent) or ((not is_long) and stop <= ent):
        return None                                      # цена открылась за стопом
    return fl_sim(bars, is_long, ent, stop, tps, scale, optimistic)


def fl_fetch_stats(sym, days):
    """contract_stats по часам: {начало_часа: {ll, ls, oi, oiu}}."""
    now = int(time.time())
    cur, out, probes = now - days * 86400, [], 0
    while cur < now:
        raw = B.api_get("contract_stats", {"contract": f"{sym}_USDT", "interval": "1h",
                                           "from": cur, "to": min(now, cur + 100 * HOUR),
                                           "limit": 100})
        rows = raw if isinstance(raw, list) else []
        if not rows:
            probes += 1
            if probes > 20:
                break
            cur += 5 * 86400
            continue
        out.extend(rows)
        nxt = max(int(B.fnum(r.get("time", 0))) for r in rows) + HOUR
        if nxt <= cur:
            break
        cur = nxt
    res = {}
    for r in out:
        t = int(B.fnum(r.get("time", 0)))
        if t > 0:
            res[t - t % HOUR] = {"ll": B.fnum(r.get("long_liq_usd", 0)),
                                 "ls": B.fnum(r.get("short_liq_usd", 0)),
                                 "oi": B.fnum(r.get("open_interest", 0)),
                                 "oiu": B.fnum(r.get("open_interest_usd", 0))}
    return res


def fl_load(sym, days):
    rows = fl_fetch_stats(sym, days)
    c = fetch_gate(sym, days, 0)
    if len(rows) < 240 or len(c) < 1000:
        return None
    c = c[:-1]
    use_c = all(r["oi"] > 0 for r in rows.values())          # единица OI одна на весь ряд
    for r in rows.values():
        r["o"] = r["oi"] if use_c else r["oiu"]
    return {"rows": rows, "c": c, "idx": {x["t"]: k for k, x in enumerate(c)}}


def fl_pick_align(data):
    """К какому часу относится строка статистики с временем t: [t, t+1ч) или [t−1ч, t)?
    От этого зависит всё: если ошибиться, ликвидации сопоставятся с чужим ходом цены.
    Определяем по данным: часы с самыми крупными ликвидациями лонгов должны совпадать с
    падением цены, шортов — с ростом. Берём окно, где этот контраст сильнее."""
    acc = {0: {"L": [], "S": []}, -HOUR: {"L": [], "S": []}}
    for d in data.values():
        rows, c, idx = d["rows"], d["c"], d["idx"]
        for key, tag in (("ll", "L"), ("ls", "S")):
            vals = sorted(r[key] for r in rows.values() if r[key] > 0)
            if len(vals) < 30:
                continue
            thr = vals[int(0.97 * (len(vals) - 1))]
            for t, r in rows.items():
                if r[key] < thr:
                    continue
                for sh in acc:
                    a, b = idx.get(t + sh), idx.get(t + sh + 3 * SEC)
                    if a is None or b is None or c[a]["o"] <= 0:
                        continue
                    acc[sh][tag].append((c[b]["c"] / c[a]["o"] - 1) * 100)
    score = {}
    for sh, v in acc.items():
        if len(v["L"]) >= 20 and len(v["S"]) >= 20:
            score[sh] = (sum(v["S"]) / len(v["S"]) - sum(v["L"]) / len(v["L"]), len(v["L"]), len(v["S"]))
    if FL_ALIGN in ("0", "-1"):
        sh = 0 if FL_ALIGN == "0" else -HOUR
        return sh, [f"выравнивание задано вручную: FL_ALIGN={FL_ALIGN}"]
    if not score:
        return 0, ["⚠️ выравнивание определить не удалось (мало крупных ликвидаций) — принято [t, t+1ч)"]
    best = max(score, key=lambda k: score[k][0])
    txt = []
    for sh in (0, -HOUR):
        if sh in score:
            txt.append(f"{'[t, t+1ч)' if sh == 0 else '[t−1ч, t)'}: контраст шорт-лонг "
                       f"{score[sh][0]:+.2f}% ({score[sh][1]}+{score[sh][2]} часов)")
    note = ""
    if score[best][0] < 0.3:
        note = " ⚠️ контраст слабый — выравнивание ненадёжно"
    lbl = "строка t описывает час [t, t+1ч)" if best == 0 else "строка t описывает час [t−1ч, t)"
    return best, [f"выравнивание статистики: {'; '.join(txt)} → <b>{lbl}</b>{note}"]


def fl_scan_pair(sym, d, shift, rng):
    """События двух видов: flush (цена + ликвидации + падение OI) и price (та же ценовая картина
    БЕЗ ликвидационных условий — контроль: нужны ли вообще данные о ликвидациях)."""
    rows, c, idx = d["rows"], d["c"], d["idx"]
    out, last_ev = [], {"flush": -10 ** 12, "price": -10 ** 12}
    for t in sorted(t for t, r in rows.items() if r["o"] > 0):
        hs = t + shift                                    # начало каскадного часа
        a = idx.get(hs)
        if a is None or a + 3 >= len(c):
            continue
        hb = c[a:a + 4]
        if hb[3]["t"] != hs + 3 * SEC or hb[0]["o"] <= 0:
            continue
        move = (hb[3]["c"] / hb[0]["o"] - 1) * 100
        last = hb[3]
        # ПОСЛЕДНЯЯ свеча часа — встречная (в присланной версии сравнивались закрытие и открытие
        # всего часа, из-за чего отсекался любой настоящий каскад)
        if move <= -FL_MOVE and last["c"] > last["o"]:
            is_long = True
        elif move >= FL_MOVE and last["c"] < last["o"]:
            is_long = False
        else:
            continue
        r, prev = rows[t], rows.get(t - HOUR)
        hist = [rows[t - k * HOUR] for k in range(1, 25) if (t - k * HOUR) in rows]
        if not prev or prev["o"] <= 0 or len(hist) < 12:
            continue
        key = "ll" if is_long else "ls"
        other = "ls" if is_long else "ll"
        med = statistics.median([x[key] for x in hist])
        oi_chg = (r["o"] / prev["o"] - 1) * 100
        liq = r[key]
        is_flush = (liq >= max(FL_LIQ_MULT * med, FL_LIQ_MIN) and liq >= 2 * r[other]
                    and oi_chg <= -FL_OI_DROP)
        kind = "flush" if is_flush else "price"
        if t <= last_ev[kind] + FL_COOLDOWN_H * HOUR:
            continue
        k0 = None
        for off in range(5):                              # вход строго ПОСЛЕ закрытия часа
            k0 = idx.get(hs + HOUR + off * SEC)
            if k0 is not None:
                break
        if k0 is None or k0 + FL_HOLD_H * 4 > len(c):
            continue                                      # нужен полный горизонт удержания
        bars = c[k0:k0 + FL_HOLD_H * 4]
        entry = bars[0]["o"]
        ext = min(x["l"] for x in hb) if is_long else max(x["h"] for x in hb)
        stop = ext * (1 - FL_BUF / 100) if is_long else ext * (1 + FL_BUF / 100)
        if entry <= 0 or abs(entry - stop) / entry * 100 > FL_MAX_STOP:
            continue
        mv = abs(move)
        tps = [entry * (1 + mv * k / 100) if is_long else entry * (1 - mv * k / 100) for k in FL_TP_K]
        r_main = fl_trade(bars, is_long, entry, stop, tps)
        if r_main is None:
            continue
        r_opt = fl_trade(bars, is_long, entry, stop, tps, optimistic=True)
        # зеркальная сделка: тот же риск, развёрнутая геометрия, СВОЁ проскальзывание
        r_anti = fl_trade(bars, not is_long, entry, 2 * entry - stop, [2 * entry - x for x in tps])
        if r_anti is None:
            continue
        drift = {}
        for hh in (1, 2, 4, 8, 12, 24):
            kk = None
            for off in range(5):
                kk = idx.get(hs + HOUR + hh * HOUR + off * SEC)
                if kk is not None:
                    break
            if kk is not None:
                drift[hh] = (c[kk]["o"] / entry - 1) * 100 * (1 if is_long else -1)
        oiu = prev["oiu"] if prev["oiu"] > 0 else None
        dt = datetime.fromtimestamp(hs, timezone.utc)
        out.append({"kind": kind, "sym": sym, "t": hs, "is_long": is_long, "r": r_main,
                    "r_opt": r_opt, "r_anti": r_anti,
                    "r_coin": r_main if rng.random() < 0.5 else r_anti,
                    "drift": drift, "move": mv, "mult": (liq / med) if med > 0 else None,
                    "med0": med <= 0, "liq_oi": (liq / oiu * 100) if oiu else None,
                    "day": dt.strftime("%Y-%m-%d"), "month": dt.strftime("%Y-%m"),
                    "bars": bars, "entry": entry, "stop": stop, "tps": tps})
        last_ev[kind] = t
    return out


def _fl_line(evs, label, key="r"):
    vals = [(e[key], e["day"]) for e in evs if e.get(key) is not None]
    if not vals:
        return f"  {label}: сделок нет"
    n, m, ci = cluster_ci([v for v, _ in vals], [d for _, d in vals])
    wr = sum(1 for v, _ in vals if v > 0) / n * 100
    mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
    return f"  {mark} {label}: {n:4} сд, ВР {wr:3.0f}%, <b>{m:+.3f}R</b> (±{ci:.3f})"


def _fl_drift(evs, hh):
    vals = [(e["drift"][hh], e["day"]) for e in evs if hh in e["drift"]]
    if len(vals) < 5:
        return None
    return cluster_ci([v for v, _ in vals], [d for _, d in vals])


def run_flush():
    rnd = random.Random(60)
    pairs = B.UPSCALE_PAIRS[:FL_PAIRS_N] if FL_PAIRS_N else B.UPSCALE_PAIRS
    t0 = time.time()
    data = {}
    for i, sym in enumerate(pairs, 1):
        try:
            d = fl_load(sym, FL_DAYS)
        except Exception as e:
            print(f"[FL60] {sym}: {e}")
            d = None
        if d:
            data[sym] = d
        if i % 20 == 0:
            print(f"[FL60] {i}/{len(pairs)} | с данными {len(data)} | {time.time() - t0:.0f}с")
    if not data:
        _send(["⚠️ FLUSH-60: данных нет ни по одной паре"])
        return
    shift, align_txt = fl_pick_align(data)
    ev = []
    for sym, d in data.items():
        ev += fl_scan_pair(sym, d, shift, rnd)
    flush = [e for e in ev if e["kind"] == "flush"]
    ponly = [e for e in ev if e["kind"] == "price"]

    L = [f"💥 <b>FLUSH-60 v1.2: откат после каскада ликвидаций</b> — {len(data)} пар, {FL_DAYS} дн",
         f"<i>каскад: ликвидации одной стороны ≥ max({FL_LIQ_MULT}×медианы 24ч, ${FL_LIQ_MIN:.0f}) и ≥2× "
         f"другой, OI за час ≤ −{FL_OI_DROP}%, ход часа ≥{FL_MOVE}% и ПОСЛЕДНЯЯ 15м свеча встречная | "
         f"вход по открытию следующей свечи после часа | стоп за экстремум часа +{FL_BUF}% (≤{FL_MAX_STOP}%) | "
         f"цели {', '.join(f'{int(k * 100)}%' for k in FL_TP_K)} каскадного хода по трети, "
         f"безубыток после TP1, держим {FL_HOLD_H}ч</i>",
         f"<i>издержки: комиссия {FL_FEE}% за сторону, проскальзывание входа {FL_SLIP}%, "
         f"стопа {FL_STOP_SLIP}% (проскальзывание входа теперь реально применяется)</i>"]
    L += align_txt
    L += [f"Событий: <b>{len(flush)}</b> с ликвидациями | {len(ponly)} с той же ценовой картиной "
          f"БЕЗ ликвидационных условий (контроль)", ""]
    if not flush:
        L.append("⚠️ Каскадов с ликвидациями не найдено — вывод невозможен")
        _send(L)
        return

    L.append("<b>1. ДРЕЙФ ПОСЛЕ КАСКАДА</b> (без стопов и целей, в сторону отката, %)")
    L.append("  <i>главное измерение. Рядом тот же ход цены БЕЗ данных о ликвидациях: если они "
             "равны, ликвидации ничего не добавляют</i>")
    for hh in (1, 2, 4, 8, 12, 24):
        a, b = _fl_drift(flush, hh), _fl_drift(ponly, hh)
        if not a:
            continue
        ma = "✅" if a[1] - a[2] > 0 else "❌" if a[1] + a[2] < 0 else "  "
        row = f"  {ma} +{hh:2}ч: каскад {a[1]:+.3f}% (±{a[2]:.3f}, n={a[0]})"
        if b:
            row += f" | только цена {b[1]:+.3f}% (±{b[2]:.3f}, n={b[0]})"
        L.append(row)
    L.append("")

    L.append("<b>2. СИЛА КАСКАДА</b>")
    z = sum(1 for e in flush if e["med0"])
    L.append(f"  у {z} из {len(flush)} событий ({z / len(flush) * 100:.0f}%) медиана часовых ликвидаций "
             f"за сутки равна НУЛЮ — порог «{FL_LIQ_MULT}× медианы» там не работает и остаётся "
             f"только ${FL_LIQ_MIN:.0f}")
    lo = sorted(e["liq_oi"] for e in flush if e["liq_oi"] is not None)
    if len(lo) >= 30:
        q = lambda p: lo[int(p * (len(lo) - 1))]
        L.append(f"  ликвидации как доля OI за час: p25 {q(.25):.2f}% | медиана {q(.5):.2f}% | "
                 f"p75 {q(.75):.2f}% | p90 {q(.9):.2f}%")
        t1, t2 = q(1 / 3), q(2 / 3)
        for name, sel in ((f"слабые (≤{t1:.2f}% OI)", [e for e in flush if e["liq_oi"] is not None and e["liq_oi"] <= t1]),
                          ("средние", [e for e in flush if e["liq_oi"] is not None and t1 < e["liq_oi"] <= t2]),
                          (f"сильные (>{t2:.2f}% OI)", [e for e in flush if e["liq_oi"] is not None and e["liq_oi"] > t2])):
            d4 = _fl_drift(sel, 4)
            L.append(_fl_line(sel, name) + (f" | дрейф +4ч {d4[1]:+.3f}%" if d4 else ""))
        L.append("  <i>настоящий эффект растёт с долей OI. Если слабые и сильные одинаковы — дело не "
                 "в ликвидациях</i>")
    L.append("")

    L.append("<b>3. СДЕЛКИ</b> (три цели по трети)")
    L.append(_fl_line(flush, "MAIN (стоп первым — нижняя граница)"))
    L.append(_fl_line(flush, "BOX: тейки первыми (верхняя граница)", "r_opt"))
    L.append(_fl_line(flush, "ANTI (зеркальная сделка против)", "r_anti"))
    L.append(_fl_line(flush, "COIN (случайное направление)", "r_coin"))
    L.append(_fl_line(ponly, "ТОЛЬКО ЦЕНА (те же правила, без ликвидаций)"))
    pairs_ = [((e["r"] - e["r_anti"]) / 2, e["day"]) for e in flush]
    if len(pairs_) >= 30:
        n_, m_, ci_ = cluster_ci([v for v, _ in pairs_], [d for _, d in pairs_])
        cost = -sum((e["r"] + e["r_anti"]) / 2 for e in flush) / len(flush)
        mk = "✅" if m_ - ci_ > 0 else "❌" if m_ + ci_ < 0 else "  "
        L.append(f"  {mk} валовый эдж направления (MAIN−ANTI)/2: <b>{m_:+.3f}R</b> (±{ci_:.3f}) при "
                 f"издержках {cost:.3f}R → чистый {m_ - cost:+.3f}R")
    cells = []
    for sc in (1.0, 0.7, 0.4):
        rs = [(fl_trade(e["bars"], e["is_long"], e["entry"], e["stop"], e["tps"], scale=sc), e["day"])
              for e in flush]
        rs = [(r, d) for r, d in rs if r is not None]
        st = cluster_ci([r for r, _ in rs], [d for _, d in rs]) if rs else None
        if st:
            cells.append(f"издержки ×{sc:g} → <b>{st[1]:+.3f}R</b> (±{st[2]:.3f})")
    if cells:
        L.append("  MAIN при сниженных издержках: " + " | ".join(cells))
    L.append("")

    months = {}
    for e in flush:
        months.setdefault(e["month"], []).append(e["r"])
    L.append("<b>4. РАЗБРОС</b>")
    L.append("  по месяцам: " + " | ".join(f"{m}: {len(v)} соб., {sum(v):+.1f}R" for m, v in sorted(months.items())))
    by_sym, by_day = {}, {}
    for e in flush:
        by_sym[e["sym"]] = by_sym.get(e["sym"], 0) + 1
        by_day[e["day"]] = by_day.get(e["day"], 0) + 1
    t5 = sorted(by_sym.items(), key=lambda kv: -kv[1])[:5]
    d5 = sorted(by_day.items(), key=lambda kv: -kv[1])[:3]
    L.append("  монеты-лидеры: " + ", ".join(f"{s} ({n})" for s, n in t5)
             + f" — {sum(n for _, n in t5) / len(flush) * 100:.0f}% событий")
    L.append("  дни-лидеры (каскады идут по всему рынку сразу): " + ", ".join(f"{d} ({n})" for d, n in d5)
             + f" — {sum(n for _, n in d5) / len(flush) * 100:.0f}% событий")
    L.append("")

    st = cluster_ci([e["r"] for e in flush], [e["day"] for e in flush])
    if not st or st[0] < FL_N_MIN:
        verdict = f"⚠️ ВЫВОД НЕВОЗМОЖЕН: событий {st[0] if st else 0} < {FL_N_MIN}"
    elif st[1] - st[2] > 0.15:
        verdict = "✅ ПРОЙДЕН — нижняя граница выше +0.15R, можно на demo"
    else:
        verdict = "❌ ПРОВАЛЕН — нижняя граница не выше +0.15R"
    L.append(f"<b>КРИТЕРИЙ</b> (нижняя граница MAIN > +0.15R, n ≥ {FL_N_MIN}, интервал по дням): {verdict}")
    L.append("<i>интервалы по дням: каскады происходят на всём рынке сразу, поэтому события одного "
             "дня зависимы, и обычный интервал был бы слишком узким</i>")
    _send(L)


# ═════════════════════════════════════════════════════════════════════════════
#  POSITIONING SCAN — три семейства позиционирования из contract_stats (дневной горизонт)
# ═════════════════════════════════════════════════════════════════════════════
POS_DAYS      = int(os.environ.get("POS_DAYS", "60"))
POS_OFFSET    = int(os.environ.get("POS_OFFSET", "0"))
POS_UNI       = os.environ.get("POS_UNI", "both").lower()         # both | liq | alts
POS_PCT_HI    = float(os.environ.get("POS_PCT_HI", "80"))
POS_PCT_LO    = float(os.environ.get("POS_PCT_LO", "20"))
POS_TRAIL     = int(os.environ.get("POS_TRAIL", "30"))
POS_COST_LIQ  = (0.05 + 0.03) * 2                                 # 0.16% на круг
POS_COST_ALT  = float(os.environ.get("POS_COST_ALT", "0.60"))     # 0.60% на круг для остальных альтов
POS_FUND      = float(os.environ.get("POS_FUND", "0.03"))         # %/день: лонг платит, шорт получает
POS_EDGE_MIN  = float(os.environ.get("POS_EDGE_MIN", "0.10"))
POS_Z         = float(os.environ.get("POS_Z", "2.64"))            # 6 ног: порог 0.05/6 на ногу
POS_LIQ10     = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "LTC", "AVAX"]
POS_DAY       = 86400


def pos_fetch_stats(sym, days, offset):
    now = int(time.time()) - offset * POS_DAY
    out, cur, probes = [], now - (days + 2) * POS_DAY, 0
    while cur < now:
        raw = B.api_get("contract_stats", {"contract": f"{sym}_USDT", "interval": "1h",
                                           "from": cur, "to": min(now, cur + 100 * HOUR), "limit": 100})
        rows = raw if isinstance(raw, list) else []
        if not rows:
            probes += 1
            if probes > 20:
                break
            cur += 5 * POS_DAY
            continue
        out.extend(rows)
        nxt = max(int(B.fnum(r.get("time", 0))) for r in rows) + HOUR
        if nxt <= cur:
            break
        cur = nxt
    seen, res = set(), []
    for r in sorted(out, key=lambda r: int(B.fnum(r.get("time", 0)))):
        t = int(B.fnum(r.get("time", 0)))
        if t and t not in seen:
            seen.add(t)
            res.append({"t": t, "lsr": B.fnum(r.get("lsr_account", 0)),
                        "top": B.fnum(r.get("top_lsr_size", 0)),
                        "lu": B.fnum(r.get("long_users", 0)), "su": B.fnum(r.get("short_users", 0)),
                        "tl": B.fnum(r.get("top_long_size", 0)), "ts": B.fnum(r.get("top_short_size", 0))})
    return res


def pos_fetch_1h(sym, days, offset):
    now = int(time.time()) - offset * POS_DAY
    out, cur, probes = [], now - (days + 3) * POS_DAY, 0
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1h",
                                         "from": cur, "to": min(now, cur + 1900 * HOUR)})
        part = B.parse_candles(raw) if raw else []
        if not part:
            probes += 1
            if probes > 6:
                break
            cur += 20 * POS_DAY
            continue
        out.extend(part)
        nxt = part[-1].get("t", 0) + HOUR
        if nxt <= cur:
            break
        cur = nxt
    return _dedupe(out)


def pos_daily_bars(c1h):
    """Дневные бары по UTC из ЧАСОВЫХ свечей: открытие первого и закрытие последнего часа.
    Не зависит от того, где Gate проводит границу своих дневных свечей. Неполные дни выбрасываются."""
    by = {}
    for c in c1h:
        by.setdefault(c["t"] // POS_DAY, []).append(c)
    bars = {}
    for d, v in by.items():
        v.sort(key=lambda x: x["t"])
        if len(v) == 24 and all(b["t"] - a["t"] == HOUR for a, b in zip(v, v[1:])) and v[0]["o"] > 0:
            bars[d] = (v[0]["o"], v[-1]["c"])
    return bars


def pos_snapshots(rows):
    """Позиционирование на конец UTC-дня: последняя строка дня."""
    last = {}
    for r in rows:
        last[r["t"] // POS_DAY] = r
    snap = {}
    for d, r in last.items():
        crowd = r["lu"] / (r["lu"] + r["su"]) if r["lu"] + r["su"] > 0 else None
        whale = r["tl"] / (r["tl"] + r["ts"]) if r["tl"] + r["ts"] > 0 else None
        snap[d] = {"lsr": r["lsr"] if r["lsr"] > 0 else None,
                   "top": r["top"] if r["top"] > 0 else None,
                   "crowd": crowd,
                   "div": (whale - crowd) if (whale is not None and crowd is not None) else None}
    return snap


def pos_pctl(snap, d, key):
    """Перцентиль значения дня d среди предыдущих POS_TRAIL дней той же пары (день d не входит)."""
    cur = snap.get(d, {}).get(key)
    vals = [snap[x][key] for x in range(d - POS_TRAIL, d) if x in snap and snap[x].get(key) is not None]
    if cur is None or len(vals) < 15:
        return None
    return sum(1 for v in vals if v <= cur) / len(vals) * 100


def pos_records(sym, days, offset):
    """Записи «пара-день»: позиционирование на конец дня D и ход дня D+1 (открытие → закрытие)."""
    rows = pos_fetch_stats(sym, days, offset)
    c1h = pos_fetch_1h(sym, days, offset)
    if len(rows) < 200 or len(c1h) < 24 * 30:
        return None
    snap, bars = pos_snapshots(rows), pos_daily_bars(c1h)
    recs = []
    for d in sorted(snap):
        if d not in bars or (d + 1) not in bars:        # следующий день должен быть СЛЕДУЮЩИМ, без дыр
            continue
        o, c = bars[d + 1]
        recs.append({"d": d, "sym": sym, "nd": (c / o - 1) * 100,
                     "pA": pos_pctl(snap, d, "lsr"), "pB": pos_pctl(snap, d, "top"),
                     "pC": pos_pctl(snap, d, "crowd"), "pD": pos_pctl(snap, d, "div")})
    return recs


def pos_edge(recs, elig, sel, sign):
    """Эдж ноги относительно ОСТАЛЬНЫХ пар ТОГО ЖЕ ДНЯ. Общий ход рынка за день вычитается, поэтому
    тренд выборки (рынок падал/рос) не может ни создать эдж, ни скрыть его.
    Возвращает dict или None: n, дни, эдж (%), SE, эдж по половинам."""
    by = {}
    for r in recs:
        if not elig(r):
            continue
        by.setdefault(r["d"], {"leg": [], "rest": []})["leg" if sel(r) else "rest"].append(sign * r["nd"])
    days = sorted(d for d, v in by.items() if v["leg"] and v["rest"])
    if len(days) < 8:
        return None

    def agg(ds):
        diffs = [sum(by[d]["leg"]) / len(by[d]["leg"]) - sum(by[d]["rest"]) / len(by[d]["rest"]) for d in ds]
        ws = [len(by[d]["leg"]) for d in ds]
        W = sum(ws)
        e = sum(w * x for w, x in zip(ws, diffs)) / W
        se = (sum(w * w * (x - e) ** 2 for w, x in zip(ws, diffs))) ** 0.5 / W
        return e, se, W

    e, se, n = agg(days)
    h = len(days) // 2
    e1, e2 = agg(days[:h])[0], agg(days[h:])[0]
    return {"n": n, "days": len(days), "e": e, "se": se, "e1": e1, "e2": e2}


POS_FAMILIES = (
    ("A", "A РОЗНИЦА-CONTRA (фэйд lsr_account)",
     lambda r: r["pA"] is not None, lambda r: r["pA"] <= POS_PCT_LO, lambda r: r["pA"] >= POS_PCT_HI),
    ("B", "B ТОП-ТРЕЙДЕРЫ (следуем top_lsr_size)",
     lambda r: r["pB"] is not None, lambda r: r["pB"] >= POS_PCT_HI, lambda r: r["pB"] <= POS_PCT_LO),
    ("C", "C КИТЫ-vs-ТОЛПА (встаём с китами)",
     lambda r: r["pC"] is not None and r["pD"] is not None,
     lambda r: r["pC"] <= POS_PCT_LO and r["pD"] >= POS_PCT_HI,
     lambda r: r["pC"] >= POS_PCT_HI and r["pD"] <= POS_PCT_LO),
)


def pos_eval(recs, cost):
    """Результаты по семействам и ногам. Возвращает список строк отчёта и список прошедших ног."""
    L, passed, ses = [], [], []
    for fam, name, elig, lg, sh in POS_FAMILIES:
        n_el = sum(1 for r in recs if elig(r))
        L.append(f"── <b>{name}</b> ── доступно пар-дней: {n_el}")
        if n_el < 100:
            L.append("  данных почти нет — семейство пропущено")
            L.append("")
            continue
        for leg, sel, sign, lbl in (("long", lg, 1, "лонг-нога"), ("short", sh, -1, "шорт-нога")):
            res = pos_edge(recs, elig, sel, sign)
            net_vals = [(sign * r["nd"] - cost - sign * POS_FUND, r["d"]) for r in recs if elig(r) and sel(r)]
            if not res or res["n"] < 30 or len(net_vals) < 30:
                L.append(f"  {lbl}: мало сделок ({len(net_vals)})")
                continue
            ses.append(res["se"] * POS_Z)
            _, mnet, cinet = cluster_ci([v for v, _ in net_vals], [d for _, d in net_vals])
            strict = POS_Z * res["se"]
            ok = (res["e"] >= POS_EDGE_MIN and res["e"] - strict > 0 and res["e1"] > 0 and res["e2"] > 0)
            mark = "✅" if res["e"] - strict > 0 else "❌" if res["e"] + strict < 0 else "  "
            row = (f"  {mark} {lbl}: {res['n']} сд / {res['days']} дн | эдж над днём "
                   f"<b>{res['e']:+.3f}%</b> (±{strict:.3f}) | половины {res['e1']:+.3f} | {res['e2']:+.3f} | "
                   f"чистый {mnet:+.3f}%")
            if ok:
                row += "  ← ПЛАНКА ✅" + ("" if mnet > 0 else " (но чистый ≤0 при этих издержках)")
                passed.append(f"{fam}-{leg}")
            L.append(row)
        L.append("")
    return L, passed, ses


def pos_dose(recs):
    """Дневной избыточный ход по квинтилям lsr_account (A): монотонность — признак настоящего эффекта."""
    el = [r for r in recs if r["pA"] is not None]
    by_day = {}
    for r in el:
        by_day.setdefault(r["d"], []).append(r["nd"])
    mean_day = {d: sum(v) / len(v) for d, v in by_day.items()}
    q = {k: [] for k in range(1, 6)}
    for r in el:
        k = min(5, max(1, int(r["pA"] // 20) + 1))
        q[k].append((r["nd"] - mean_day[r["d"]], r["d"]))
    out = []
    for k in range(1, 6):
        if len(q[k]) >= 20:
            n, m, ci = cluster_ci([v for v, _ in q[k]], [d for _, d in q[k]])
            out.append(f"Q{k}: {m:+.3f}% (±{ci:.3f}, n={n})")
    return out


def run_pos():
    pairs_all = B.UPSCALE_PAIRS
    universes = []
    if POS_UNI in ("both", "liq"):
        universes.append(("10 ЛИКВИДНЫХ (главный скрин)", POS_LIQ10, POS_COST_LIQ))
    if POS_UNI in ("both", "alts"):
        universes.append(("ОСТАЛЬНЫЕ АЛЬТЫ (проверка вторым универсумом)",
                          [s for s in pairs_all if s not in POS_LIQ10], POS_COST_ALT))
    L = [f"👥 <b>POSITIONING SCAN v1.2</b> — {POS_DAYS} дн, позиционирование на конец дня D → ход дня D+1 "
         f"(открытие → закрытие)",
         f"<i>перцентили внутри пары за {POS_TRAIL} дн | эдж считается ОТНОСИТЕЛЬНО остальных пар того же дня "
         f"(общий ход рынка вычитается) | интервалы строгие: {POS_Z}σ, поправка на 6 ног | издержки "
         f"информационно | планка: эдж ≥{POS_EDGE_MIN}% значимо, обе половины в плюсе</i>", ""]
    results = []
    for uname, syms, cost in universes:
        t0 = time.time()
        recs, n_ok = [], 0
        for i, sym in enumerate(syms, 1):
            try:
                rr = pos_records(sym, POS_DAYS, POS_OFFSET)
            except Exception as e:
                print(f"[POS] {sym}: {e}")
                rr = None
            if rr:
                recs += rr
                n_ok += 1
            if i % 20 == 0:
                print(f"[POS] {uname[:12]} {i}/{len(syms)} | {time.time() - t0:.0f}с")
        days = len({r["d"] for r in recs})
        L.append(f"══ <b>{uname}</b> ══ пар с данными {n_ok}/{len(syms)}, пар-дней {len(recs)}, "
                 f"дней {days}, издержки {cost:.2f}% на круг")
        if not recs:
            L.append("  ⚠️ данных нет")
            L.append("")
            results.append((uname, []))
            continue
        rows, passed, ses = pos_eval(recs, cost)
        L += rows
        dz = pos_dose(recs)
        if dz:
            L.append("<b>Доза A</b> — избыточный ход следующего дня по квинтилям lsr_account "
                     "(Q1 = толпа в шортах, Q5 = в лонгах; фэйд толпы ждёт Q1 > Q5):")
            L.append("  " + " | ".join(dz))
        if ses:
            L.append(f"  <i>минимально различимый эдж на ногу в этом универсуме ≈ ±{statistics.median(ses):.2f}% "
                     f"в день: всё меньше неотличимо от шума</i>")
        L.append("")
        results.append((uname, passed))
    if len(results) == 2:
        both = set(results[0][1]) & set(results[1][1])
        L.append("<b>ИТОГ</b>")
        for uname, passed in results:
            L.append(f"  {uname}: " + (", ".join(passed) if passed else "ни одна нога не прошла планку"))
        if both:
            L.append(f"  → <b>ПОВТОРИЛОСЬ В ОБОИХ УНИВЕРСУМАХ: {', '.join(sorted(both))}</b> — кандидат на форвард "
                     f"2–3 недели (≥30 сигналов)")
        else:
            L.append("  → <b>ни одна нога не повторилась в обоих универсумах: семейства lsr/top/киты-vs-толпа "
                     "эджа не дают</b>")
    elif results:
        L.append("<b>ИТОГ</b>: " + (", ".join(results[0][1]) if results[0][1] else "ни одна нога не прошла планку"))
    _send(L)


# ═════════════ запуск ═════════════

PAIRS_RUN = os.environ.get("PAIRS_RUN", "pos").lower()   # pos | flush | pulsed | ignition | sweep | both | all | revert | revert2


def run():
    if PAIRS_RUN in ("both", "all", "ignition"):
        run_ignition()
    if PAIRS_RUN in ("both", "all", "sweep"):
        run_sweep()
    if PAIRS_RUN in ("all", "pulsed"):
        run_pulsed()
    if PAIRS_RUN in ("all", "flush"):
        run_flush()
    if PAIRS_RUN in ("all", "pos"):
        run_pos()
    if PAIRS_RUN in ("all", "revert"):
        run_revert()
    if PAIRS_RUN in ("all", "revert2"):
        run_revert2()


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


# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# REVERT-SCAN v1.1: фэйд дневного экстремума — taker-скрин + maker-разведка
# ══════════════════════════════════════════════════════════════════════════════
#
# ЗАПУСК: RUN_BACKTEST=pairs  PAIRS_RUN=revert
# Env:    RV_DAYS=1825 RV_OFFSET=0 RV_K_GRID=2,3,4,5,6 RV_D_GRID=0.5,1.0,1.5
#
# NET и EXCESS раздельно; разрез по ногам (лонг/шорт); NET по годам (k=3%).
# КРИТЕРИЙ: excess ≥+0.15% значимо (2.64σ), NET > 0, половины NET одного знака,
#           реверсия > моментума. Прошедший → RV_OFFSET=365 → maker → форвард.
#

RV_DAYS   = int(os.environ.get("RV_DAYS", "1825"))
RV_OFFSET = int(os.environ.get("RV_OFFSET", "0"))
RV_K_GRID = [float(x) for x in os.environ.get("RV_K_GRID", "2,3,4,5,6").split(",")]
RV_D_GRID = [float(x) for x in os.environ.get("RV_D_GRID", "0.5,1.0,1.5").split(",")]
RV_COST_TK = (0.05 + 0.03) * 2      # 0.16% круг: taker обе стороны + slip
RV_COST_MK = 0.02 + 0.05            # maker вход + taker выход, slip 0
RV_FUND_D  = 0.03                    # фандинг/день: лонг платит, шорт получает
RV_Z = 2.64                          # поправка на скрининг (~10 ног)
RV_MSK = timezone(timedelta(hours=3))
RV_DAY = 86400
RV_LIQ10 = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "LTC", "AVAX"]


def _rv_fetch_daily(sym):
    now = int(time.time()) - RV_OFFSET * RV_DAY
    out, cur = [], now - (RV_DAYS + 40) * RV_DAY
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1d",
                                         "from": cur, "to": min(now, cur + 1000 * RV_DAY)})
        part = B.parse_candles(raw) if raw else []
        if not part:
            break
        out.extend(part)
        nxt = part[-1].get("t", 0) + RV_DAY
        if nxt <= cur:
            break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"])
            u.append(c)
    return u[:-1]                    # незакрытая не нужна


def run_revert():
    print(f"[RV] {len(RV_LIQ10)} пар × {RV_DAYS} дн (offset {RV_OFFSET})")
    sig = {}                         # d -> {sym: (ret, o1, h1, l1, c1)}
    for sym in RV_LIQ10:
        c = _rv_fetch_daily(sym)
        if len(c) < RV_DAYS - 30:
            print(f"  {sym}: история короткая ({len(c)} дн) — пропуск")
            continue
        for i in range(1, len(c) - 1):
            if c[i - 1]["c"] <= 0 or c[i + 1]["o"] <= 0:
                continue
            d = c[i]["t"] // RV_DAY
            ret = (c[i]["c"] / c[i - 1]["c"] - 1) * 100
            sig.setdefault(d, {})[sym] = (ret, c[i + 1]["o"], c[i + 1]["h"],
                                          c[i + 1]["l"], c[i + 1]["c"])
    days = sorted(sig)
    # рынок дня: средний ход open->close всех пар на D+1
    day_mean = {d: sum((v[4] / v[1] - 1) * 100 for v in sig[d].values()) / len(sig[d])
                for d in days if len(sig[d]) >= 5}

    def collect(k):
        rev, mom = [], []            # (dt, net, ex, leg)
        rev_l, rev_s = [], []
        mk = {dv: [] for dv in RV_D_GRID}          # (dt, net) maker-филлы
        mkn = {dv: [0, 0] for dv in RV_D_GRID}     # филлы / сигналы
        for d in days:
            if d not in day_mean:
                continue
            dm = day_mean[d]
            dt = datetime.fromtimestamp(d * RV_DAY, RV_MSK).strftime("%Y-%m-%d")
            for sym, (ret, o1, h1, l1, c1) in sig[d].items():
                if o1 <= 0:
                    continue
                nd = (c1 / o1 - 1) * 100
                for direction, trig in (("long", ret <= -k), ("short", ret >= k)):
                    if not trig:
                        continue
                    sgn = 1 if direction == "long" else -1
                    # taker: вход open(D+1), выход close(D+1)
                    net = sgn * nd - RV_COST_TK - RV_FUND_D * sgn
                    ex = sgn * (nd - dm)          # над рынком дня (без издержек — диагностика)
                    row = (dt, net, ex, direction)
                    rev.append(row)
                    (rev_l if direction == "long" else rev_s).append(row)
                    # momentum-контроль: та же механика, противоположный отбор
                    mom.append((dt, -sgn * nd - RV_COST_TK + RV_FUND_D * sgn, -ex, direction))
                    # maker: лимитка на dv% глубже open, филл по low/high, выход close
                    for dv in RV_D_GRID:
                        mkn[dv][1] += 1
                        if direction == "long":
                            limit = o1 * (1 - dv / 100)
                            if l1 <= limit:
                                mkn[dv][0] += 1
                                mk[dv].append((dt, (c1 / limit - 1) * 100 - RV_COST_MK - RV_FUND_D))
                        else:
                            limit = o1 * (1 + dv / 100)
                            if h1 >= limit:
                                mkn[dv][0] += 1
                                mk[dv].append((dt, (1 - c1 / limit) * 100 - RV_COST_MK + RV_FUND_D))
        return rev, mom, rev_l, rev_s, mk, mkn

    def day_stats(tr):
        """tr: [(dt, val)] -> (сделок, дней, среднее, RV_Z*se) — кластеризация по дням."""
        by_d = {}
        for dt, v in tr:
            by_d.setdefault(dt, []).append(v)
        dm = [sum(v) / len(v) for v in by_d.values()]
        if not dm:
            return None
        m = sum(dm) / len(dm)
        se = statistics.pstdev(dm) / len(dm) ** 0.5 if len(dm) > 1 else 0.0
        return sum(len(v) for v in by_d.values()), len(dm), m, RV_Z * se

    def halves(tr):
        ds = sorted({dt for dt, _ in tr})
        if len(ds) < 10:
            return None, None
        mid = ds[len(ds) // 2]
        h1 = [v for dt, v in tr if dt < mid]
        h2 = [v for dt, v in tr if dt >= mid]
        return (sum(h1) / len(h1) if h1 else 0.0), (sum(h2) / len(h2) if h2 else 0.0)

    L = [f"↩️ <b>REVERT-SCAN v1.1</b>: {len(RV_LIQ10)} ликвидных, ~{RV_DAYS} дн"
         + (f"\n⏪ СДВИНУТ НА {RV_OFFSET} ДН" if RV_OFFSET else ""),
         "<i>реверсия: лонг после дня ≤−k% / шорт после ≥+k%; ход D+1 open→close.",
         "NET — торговый результат (издержки+фандинг вычтены); EXCESS — над рынком дня",
         "(диагностика информации). Планка 2.64σ (скрининг): excess ≥+0.15% значимо,",
         "NET > 0, половины NET одного знака, реверсия > моментума. Прошедший порог →",
         "offset → maker-модель → форвард 2-3 недели. НЕ demo сразу</i>", ""]

    passed = []
    for k in RV_K_GRID:
        rev, mom, rev_l, rev_s, mk, mkn = collect(k)
        st = day_stats([(r[0], r[1]) for r in rev])
        ste = day_stats([(r[0], r[2]) for r in rev])
        sm = day_stats([(r[0], r[1]) for r in mom])
        if not st or st[0] < 30:
            continue
        n, nd_, m, ci = st
        h1, h2 = halves([(r[0], r[1]) for r in rev])
        ex_m = ste[2] if ste else 0.0
        ex_ci = ste[3] if ste else 0.0
        mom_m = sm[2] if sm else 0.0
        rev_beats = m > mom_m
        ok = (ex_m - ex_ci > 0.15) and (m > 0) and (h1 is not None) and \
             ((h1 > 0) == (h2 > 0)) and rev_beats
        if ok:
            passed.append(k)
        mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
        L.append(f"── k=±{k:.0f}% ──")
        L.append(f"  {mark} NET taker: {n} сд / {nd_} дн, <b>{m:+.3f}%</b> (±{ci:.3f})"
                 + (f", половины {h1:+.3f}|{h2:+.3f}" if h1 is not None else ""))
        L.append(f"      EXCESS над рынком: {ex_m:+.3f}% (±{ex_ci:.3f}) | "
                 f"momentum-контроль: {mom_m:+.3f}% → реверсия "
                 f"{'>' if rev_beats else '<'} моментума"
                 + ("  ← ПЛАНКА ✅" if ok else ""))
        for leg_tr, lbl in ((rev_l, "после падения (лонг-триг)"),
                            (rev_s, "после роста (шорт-триг)")):
            stl = day_stats([(r[0], r[1]) for r in leg_tr])
            if stl and stl[0] >= 30:
                L.append(f"      {lbl}: {stl[0]} сд, NET {stl[2]:+.3f}% (±{stl[3]:.3f})")
        for dv in RV_D_GRID:
            tr = mk[dv]
            fills, total = mkn[dv]
            if len(tr) < 30:
                continue
            vals = [v for _, v in tr]
            mm_ = sum(vals) / len(vals)
            se_ = statistics.pstdev(vals) / len(vals) ** 0.5 if len(vals) > 1 else 0.0
            L.append(f"      maker d={dv:.1f}%: филл {fills}/{total} "
                     f"({fills / total * 100:.0f}%), NET {mm_:+.3f}% (±{RV_Z * se_:.3f})")
        L.append("")

    rev3, _, _, _, _, _ = collect(3.0)
    by_year = {}
    for dt, net, _ex3, _leg3 in rev3:
        by_year.setdefault(dt[:4], []).append(net)
    if len(by_year) >= 3:
        L.append("<b>NET по годам (k=3%)</b>: " +
                 " | ".join(f"{y}: {sum(v) / len(v):+.2f}% ({len(v)})"
                            for y, v in sorted(by_year.items())))
        L.append("")

    if passed:
        L.append("<b>ПЛАНКУ ПРОШЛИ пороги</b>: " + ", ".join(f"±{k:.0f}%" for k in passed))
        L.append("  → шаги: RV_OFFSET=365 (знак совпал?) → maker-модель отдельно → форвард")
    else:
        L.append("<i>Планка taker не пройдена. Решение по maker-блоку: если NET-maker "
                 "систематически выше NET-taker и в плюсе при филле >40% — отдельный "
                 "maker-бэктест оправдан. Если maker тоже в минусе — реверсия на дневном "
                 "горизонте не монетизируется, сырых сигналов в наших данных не осталось</i>")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[RV] отправка: {e}")
# ══════════════════════════════════════════════════════════════════════════════
# MAKER DEEP-DIVE (revert2): разложение BETA → UNCOND → COND
# ══════════════════════════════════════════════════════════════════════════════
#
# ЗАПУСК: RUN_BACKTEST=pairs  PAIRS_RUN=revert2
# Env:    RV2_DAYS=1825 RV2_OFFSET=0 RV2_K_COND=3 RV2_D_GRID=0.5,1.0,1.5
#         RV2_QUEUE_BPS=5  RV2_SLOT=500
#

RV2_DAYS   = int(os.environ.get("RV2_DAYS", "1825"))
RV2_OFFSET = int(os.environ.get("RV2_OFFSET", "0"))
RV2_K_COND = float(os.environ.get("RV2_K_COND", "3"))
RV2_D_GRID = [float(x) for x in os.environ.get("RV2_D_GRID", "0.5,1.0,1.5").split(",")]
RV2_QUEUE_BPS = float(os.environ.get("RV2_QUEUE_BPS", "5"))     # прокол через лимит, б.п.
RV2_SLOT   = float(os.environ.get("RV2_SLOT", "500"))
RV2_COST_TK = (0.05 + 0.03) * 2      # 0.16%
RV2_COST_MK = 0.02 + 0.05            # maker вход + taker выход
RV2_FUND_D  = 0.03
RV2_Z = 2.64
RV2_MSK = timezone(timedelta(hours=3))
RV2_DAY = 86400
RV2_LIQ10 = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "LTC", "AVAX"]


def _rv2_fetch_daily(sym):
    now = int(time.time()) - RV2_OFFSET * RV2_DAY
    out, cur = [], now - (RV2_DAYS + 40) * RV2_DAY
    while cur < now:
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": "1d",
                                         "from": cur, "to": min(now, cur + 1000 * RV2_DAY)})
        part = B.parse_candles(raw) if raw else []
        if not part:
            break
        out.extend(part)
        nxt = part[-1].get("t", 0) + RV2_DAY
        if nxt <= cur:
            break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"])
            u.append(c)
    return u[:-1]


def _rv2__rv2_day_ci(pairs_list):
    """pairs_list: [(date, value)] -> (сделок, дней, mean, RV2_Z*se) — кластер по дням."""
    by_d = {}
    for dt, v in pairs_list:
        by_d.setdefault(dt, []).append(v)
    dm = [sum(v) / len(v) for v in by_d.values()]
    if not dm:
        return None
    m = sum(dm) / len(dm)
    se = statistics.pstdev(dm) / len(dm) ** 0.5 if len(dm) > 1 else 0.0
    return sum(len(v) for v in by_d.values()), len(dm), m, RV2_Z * se


def run_revert2():
    print(f"[RV2] {len(RV2_LIQ10)} пар × {RV2_DAYS} дн, очередь {RV2_QUEUE_BPS:.0f} б.п., offset {RV2_OFFSET}")
    rows = []            # (date, sym, retD, o1, h1, l1, c1)
    for sym in RV2_LIQ10:
        c = _rv2_fetch_daily(sym)
        if len(c) < RV2_DAYS - 30:
            continue
        for i in range(1, len(c) - 1):
            if c[i - 1]["c"] <= 0 or c[i + 1]["o"] <= 0:
                continue
            dt = datetime.fromtimestamp(c[i]["t"], RV2_MSK).strftime("%Y-%m-%d")
            rows.append((dt, sym, (c[i]["c"] / c[i - 1]["c"] - 1) * 100,
                         c[i + 1]["o"], c[i + 1]["h"], c[i + 1]["l"], c[i + 1]["c"]))

    qb = RV2_QUEUE_BPS / 10000
    res = {"beta": [], "uncond": {d: [] for d in RV2_D_GRID},
           "cond": {d: [] for d in RV2_D_GRID}}
    for dt, sym, ret, o1, h1, l1, c1 in rows:
        if o1 <= 0:
            continue
        nd = (c1 / o1 - 1) * 100
        res["beta"].append((dt, nd - RV2_COST_TK - RV2_FUND_D))          # бета: каждый день тейкером
        for dv in RV2_D_GRID:
            limit = o1 * (1 - dv / 100)
            if l1 <= limit * (1 - qb):                           # прокол сквозь лимит
                net = (c1 / limit - 1) * 100 - RV2_COST_MK - RV2_FUND_D
                res["uncond"][dv].append((dt, net))
                if ret <= -RV2_K_COND:
                    res["cond"][dv].append((dt, net))

    L = [f"🧪 <b>MAKER DEEP-DIVE</b>: {len(RV2_LIQ10)} пар, ~{RV2_DAYS} дн, лимитка ниже open, "
         f"выход close | очередь {RV2_QUEUE_BPS:.0f} б.п., издержки maker {RV2_COST_MK:.2f}%",
         "<i>Разложение: BETA (каждый день тейкером) → UNCOND (+премия за пассивность) → "
         "COND (+сигнал «вчера был сильный минус»). CI по дням. Лонг-only: шортить "
         "дневной дрейф лимиткой сверху — структурный минус, не тестируем</i>", ""]

    st = _rv2_day_ci(res["beta"])
    L.append(f"<b>BETA</b> (buy&sell daily, тейкер): {st[0]} сд / {st[1]} дн, "
             f"NET {st[2]:+.3f}% (±{st[3]:.3f})" if st else "BETA: нет данных")
    L.append("")

    for dv in RV2_D_GRID:
        su = _rv2_day_ci(res["uncond"][dv])
        sc = _rv2_day_ci(res["cond"][dv])
        n_days = su[1] if su else 0
        if not su or su[0] < 200:
            continue
        L.append(f"── глубина −{dv:.1f}% ──")
        L.append(f"  UNCOND: {su[0]} филлов / {su[1]} дн, NET <b>{su[2]:+.3f}%</b> (±{su[3]:.3f})")
        if sc and sc[0] >= 100:
            L.append(f"  COND (после ≤−{RV2_K_COND:.0f}%): {sc[0]} филлов, NET {sc[2]:+.3f}% "
                     f"(±{sc[3]:.3f}) | вклад сигнала {sc[2] - su[2]:+.3f}%")
        # хвост: 5 худших дней по средней NET дня
        by_d = {}
        for dt, v in res["uncond"][dv]:
            by_d.setdefault(dt, []).append(v)
        worst = sorted(((sum(v) / len(v), dt, len(v)) for dt, v in by_d.items()))[:5]
        L.append("  худшие дни: " + " | ".join(
            f"{dt}: {m:+.2f}% ({n} филлов)" for m, dt, n in worst))
        usd = worst[0][0] / 100 * RV2_SLOT * min(10, max(1, len(by_d[worst[0][1]])))
        L.append(f"  худший день в $ (слоты ${RV2_SLOT:.0f}): ~${usd:+.0f} на задействованный капитал")
        L.append("")

    # годы для UNCOND d=1.0
    by_year = {}
    for dt, v in res["uncond"].get(1.0, []):
        by_year.setdefault(dt[:4], []).append(v)
    if len(by_year) >= 3:
        L.append("<b>UNCOND −1.0% по годам</b>: " +
                 " | ".join(f"{y}: {sum(v) / len(v):+.2f}% ({len(v)})"
                            for y, v in sorted(by_year.items())))
        L.append("")

    su = _rv2_day_ci(res["uncond"].get(1.0, []))
    ok1 = bool(su and su[2] - su[3] > 0.10)
    L.append("<i>Критерии форварда: ① UNCOND(−1%) ≥ +0.10% значимо ② offset: NET > 0 "
             "③ хвост ≤ −3% капитала/день ④ вклад сигнала — диагностика. Если сигнал "
             "~0, а UNCOND жив — это бета-жатва с пассивной премией: deployment возможен, "
             "но имя ему «умный DCA», и REGIME-фильтр обязателен. Реальные заливки будут "
             "хуже бэктеста (очередь, частичные филлы) — форвард обязателен перед деньгами</i>")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[RV2] отправка: {e}")
