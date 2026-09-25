"""
Сравнение внутридневных стратегий на одной истории Gate.io.
Все сделки закрываются в тот же день — ничего не переносится на ночь.

Стратегии:
  OURS  — наша: заряд 1ч (сжатие+объём при стоящей цене) → вход по уровню, фильтр BTC 12ч
  ORB   — пробой начального диапазона: первый час окна задаёт границы, торгуем их пробой
          (Zarattini/Barbon/Aziz: 7000 акций 2016–2023, лучше всего на бумагах с аномальным объёмом)
  FADE  — разворот после перебора: цена ушла далеко от дневного VWAP на N ATR — входим против,
          цель VWAP (документированный внутридневной разворот в крипте)
  VWAP  — тренд по VWAP: цена выше дневного VWAP, ждём откат К VWAP и входим по тренду

Считаются одинаково: те же издержки, те же окна времени, тот же движок ведения сделки,
проверка по половинам периода, по парам, по часам + корреляция дневных результатов.

Запуск: python bt_compare.py
Переменные: BT_DAYS (90), BT_PAIRS (103), BT_EXEC_TF (15m|5m — на чём исполняем;
  15m даёт больше истории, 5m точнее), TELEGRAM_TOKEN, CHAT_ID.
"""

import os
import time
import gc
import statistics
import threading
from datetime import datetime, timezone, timedelta

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
DAYS           = int(os.environ.get("BT_DAYS", "90"))
N_PAIRS        = int(os.environ.get("BT_PAIRS", "103"))
EXEC_TF        = os.environ.get("BT_EXEC_TF", "15m")     # на чём ведём сделку
FEE_PCT        = 0.10
SLIP_PCT       = float(os.environ.get("BT_SLIP", "0.05"))
COST_PCT       = FEE_PCT + 2 * SLIP_PCT
MSK            = timezone(timedelta(hours=3))

# окна отправки сигналов (МСК) — как в боте v8.2
WINDOWS = [(10 * 60, 11 * 60 + 30), (14 * 60 + 30, 21 * 60)]
DAY_END_MIN = 21 * 60          # все сделки закрываются к этому времени

ALL_PAIRS = [
    "ETH","BNB","XRP","SOL","AAVE","ADA","AERO","ALGO","APT","ARB",
    "ASTER","ATOM","AVAX","AXS","BCH","BERA","BONK","BRETT","BSV",
    "CAKE","CHZ","CRO","CRV","DASH","DATA","DEEP","DEXE","DOGE","DOT",
    "DYDX","EIGEN","ENA","ENS","ETC","FARTCOIN","FET","FIL","FLOKI",
    "GALA","GRAM","GRASS","GRT","HBAR","HYPE","ICP","IMX","INJ","IOTA",
    "JASMY","JTO","JUP","KAIA","KAITO","KAS","LDO","LINEA","LINK","LTC",
    "MANA","MNT","MORPHO","MOVE","NEAR","ONDO","OP","ORDI","PENDLE",
    "PENGU","PEPE","PNUT","POL","POPCAT","PUMP","PYTH","QNT","RAY",
    "RENDER","RUNE","S","SAND","SEI","SHIB","SKY","STRK","STX","SUI",
    "TAO","TIA","TRUMP","TRX","TURBO","UNI","VET","VIRTUAL","WAL",
    "WIF","WLD","XLM","XMR","XTZ","ZEC","ZRO","0G",
]

TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
T, O, H, L, C, V = 0, 1, 2, 3, 4, 5

# ─── параметры стратегий (небольшой перебор внутри каждой) ────────────────────
OURS_GRID = [{"squeeze": 25, "vol": 2.0, "stop_atr": 1.0},
             {"squeeze": 35, "vol": 2.0, "stop_atr": 1.0}]
ORB_GRID  = [{"or_min": 60, "stop_atr": 1.0, "rvol": 1.5},
             {"or_min": 30, "stop_atr": 1.0, "rvol": 1.5},
             {"or_min": 60, "stop_atr": 1.0, "rvol": 0.0}]
FADE_GRID = [{"dist_atr": 2.5, "stop_atr": 1.0},
             {"dist_atr": 3.5, "stop_atr": 1.0}]
VWAP_GRID = [{"stop_atr": 1.0, "trend_pct": 0.5},
             {"stop_atr": 1.5, "trend_pct": 1.0}]

ACC_WINDOW, BASE_FROM, BASE_TO, SWING_LOOKBACK = 12, 84, 12, 144
ACC_FLAT_ATR, ACC_MAX_RANGE, ACC_RVOL_MIN = 2.0, 5.0, 1.3
STOP_MIN, STOP_MAX, TP_MAX_R = 0.8, 3.0, 4.0
BTC_FLAT = 0.5           # флет BTC за 12ч — сделок нет (проверено бэктестом)

# ─── сеть ─────────────────────────────────────────────────────────────────────

_lock, _last = threading.Lock(), [0.0]

def api_get(path, params, tries=3, quiet=False):
    url = f"https://api.gateio.ws/api/v4/futures/usdt/{path}"
    for a in range(tries):
        with _lock:
            wait = 0.1 - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (a + 1)); continue
            if r.status_code == 400:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if a == tries - 1:
                if not quiet:
                    print(f"  [ERR] {path} {params.get('contract')}: {e}")
                return None
            time.sleep(1 + a)
    return None

def get_candles(sym, tf, frm, to):
    step, out, seen, cur_to = TF_SEC[tf], [], set(), int(to)
    for _ in range(60):
        raw = api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": tf,
                                       "to": cur_to, "limit": 1999}, quiet=True)
        if not raw:
            break
        page = []
        for c in raw:
            t = int(c["t"])
            if t in seen:
                continue
            seen.add(t)
            page.append((t, float(c["o"]), float(c["h"]), float(c["l"]),
                         float(c["c"]), float(c["v"])))
        if not page:
            break
        out.extend(page)
        oldest = min(p[0] for p in page)
        if oldest <= frm or len(raw) < 100:
            break
        cur_to = oldest - step
    out = [c for c in out if c[0] >= frm]
    out.sort()
    return out

# ─── общие вычисления ─────────────────────────────────────────────────────────

def trimmed_mean(vals):
    if not vals:
        return 0.0
    s = sorted(vals); cut = max(1, len(s) // 10)
    s = s[:-cut] if len(s) > 2 * cut else s
    return sum(s) / len(s) if s else 0.0

def true_ranges(cs):
    out = [cs[0][H] - cs[0][L]]
    for i in range(1, len(cs)):
        p = cs[i - 1][C]
        out.append(max(cs[i][H] - cs[i][L], abs(cs[i][H] - p), abs(p - cs[i][L])))
    return out

def atr_series(cs, n=14):
    trs = true_ranges(cs)
    out = [None] * len(cs)
    run = 0.0
    for i, tr in enumerate(trs):
        run += tr
        if i >= n:
            run -= trs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out

def bb_width(cs):
    out = [None] * len(cs)
    for i in range(19, len(cs)):
        w = [c[C] for c in cs[i - 19:i + 1]]
        mean = sum(w) / 20
        if mean:
            out[i] = 4 * statistics.pstdev(w) / mean * 100
    return out

def swings(cs, left=2, right=2):
    hi, lo = [], []
    for i in range(left, len(cs) - right):
        if all(cs[i][H] >= cs[j][H] for j in range(i - left, i + right + 1) if j != i):
            hi.append(cs[i][H])
        if all(cs[i][L] <= cs[j][L] for j in range(i - left, i + right + 1) if j != i):
            lo.append(cs[i][L])
    return hi, lo

def msk_min(ts):
    d = datetime.fromtimestamp(ts, MSK)
    return d.hour * 60 + d.minute

def msk_day(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d")

def in_window(ts):
    m = msk_min(ts)
    return any(a <= m < b for a, b in WINDOWS)

# ─── контекст BTC ─────────────────────────────────────────────────────────────

BTC = {"h1": [], "t0": 0}

def load_btc(frm, now):
    BTC["h1"] = get_candles("BTC", "1h", frm, now)
    BTC["t0"] = BTC["h1"][0][T] if BTC["h1"] else 0
    return bool(BTC["h1"])

def btc_chg12(ts):
    h = BTC["h1"]
    if not h:
        return 0.0
    i = min(max(int((ts - BTC["t0"]) // 3600), 0), len(h) - 1)
    c0 = h[max(i - 12, 0)][C]
    return (h[i][C] - c0) / c0 * 100 if c0 else 0.0

def btc_ok(ts, is_long):
    d = btc_chg12(ts)
    return (d > BTC_FLAT) if is_long else (d < -BTC_FLAT)

# ─── ведение сделки (одинаково для всех стратегий) ────────────────────────────

def run_trade(ex, j, side, entry, stop, tp1, tp2, day_key):
    """Ведёт сделку по свечам исполнения с j-й; закрывает к концу торгового дня."""
    up = side == "long"
    dist = abs(entry - stop) / entry * 100
    if dist <= 0:
        return None
    hit1, res, exit_ts = False, None, ex[-1][T]
    for q in range(j, len(ex)):
        b = ex[q]
        if msk_day(b[T]) != day_key or msk_min(b[T]) >= DAY_END_MIN:
            last = ex[q - 1][C] if q else entry
            chg = (last - entry) / entry * 100 * (1 if up else -1)
            res = (abs(tp1 - entry) / entry * 50 + chg * 0.5) if hit1 else chg
            exit_ts = b[T]
            break
        stop_hit = b[L] <= stop if up else b[H] >= stop
        t1 = b[H] >= tp1 if up else b[L] <= tp1
        t2 = b[H] >= tp2 if up else b[L] <= tp2
        if not hit1:
            if stop_hit:
                res = -dist; exit_ts = b[T]; break   # стоп раньше цели (консервативно)
            if t1:
                hit1 = True
                if t2:
                    res = abs(tp1 - entry) / entry * 50 + abs(tp2 - entry) / entry * 50; exit_ts = b[T]; break
                continue
        else:
            if t2:
                res = abs(tp1 - entry) / entry * 50 + abs(tp2 - entry) / entry * 50; exit_ts = b[T]; break
            if stop_hit:
                res = abs(tp1 - entry) / entry * 50; exit_ts = b[T]; break   # половина взята, остаток в ноль
    if res is None:
        last = ex[-1][C]
        chg = (last - entry) / entry * 100 * (1 if up else -1)
        res = (abs(tp1 - entry) / entry * 50 + chg * 0.5) if hit1 else chg
    return {"pnl": res - COST_PCT, "gross": res, "dist": dist, "side": side,
            "hour": datetime.fromtimestamp(ex[j][T], MSK).hour, "day": day_key,
            "ts": ex[j][T], "exit_ts": exit_ts}

def targets_struct(entry, level, height, dist, up, sw_hi, sw_lo):
    lim = entry * (1 + TP_MAX_R * dist / 100) if up else entry * (1 - TP_MAX_R * dist / 100)
    pool = [p for p in (sw_hi if up else sw_lo)]
    pool += [level + height, level + 2 * height] if up else [level - height, level - 2 * height]
    pool = [p for p in pool if (entry * (1 + dist / 100) < p <= lim) if up] if up else \
           [p for p in pool if (lim <= p < entry * (1 - dist / 100)) and p > 0]
    pool.sort(reverse=not up)
    tp1 = pool[0] if pool else entry * (1 + dist / 100 * (1 if up else -1))
    nxt = [p for p in pool if (p > tp1 * 1.002 if up else p < tp1 * 0.998)]
    tp2 = nxt[0] if nxt else entry * (1 + 2 * dist / 100 * (1 if up else -1))
    return tp1, tp2

# ─── СТРАТЕГИЯ 1: наша (заряд 1ч → вход по уровню) ────────────────────────────

def strat_ours(sym, ex, h1, cfg):
    trades = []
    if len(h1) < BASE_FROM + ACC_WINDOW + 5 or not ex:
        return trades
    vw = day_vwap(ex)
    atrs_ex = atr_series(ex, 14)
    trs, bbw = true_ranges(h1), bb_width(h1)
    sw_hi, sw_lo = swings(h1)
    ex_ts = {c[T]: i for i, c in enumerate(ex)}
    step = TF_SEC[EXEC_TF]
    for i in range(BASE_FROM + ACC_WINDOW, len(h1)):
        win, base = h1[i - ACC_WINDOW:i], h1[i - BASE_FROM:i - BASE_TO]
        baseline = trimmed_mean([c[V] for c in base])
        if baseline <= 0:
            continue
        tr_base = trs[i - BASE_FROM:i - BASE_TO]
        atr = sum(tr_base) / len(tr_base) if tr_base else 0
        if atr <= 0:
            continue
        hi, lo = max(c[H] for c in win), min(c[L] for c in win)
        if lo <= 0 or (hi - lo) / lo * 100 > ACC_MAX_RANGE:
            continue
        if abs(win[-1][C] - win[0][O]) > ACC_FLAT_ATR * atr:
            continue
        hist = [x for x in bbw[max(0, i - SWING_LOOKBACK):i] if x is not None]
        cur = bbw[i - 1]
        sq = bool(hist and cur is not None and
                  sum(1 for x in hist if x < cur) / len(hist) * 100 <= cfg["squeeze"])
        if not sq and not (sum(trs[i - ACC_WINDOW:i]) / ACC_WINDOW <= 0.75 * atr):
            continue
        half = ACC_WINDOW // 2
        if sum(c[V] for c in win[-half:]) / half / baseline < ACC_RVOL_MIN:
            continue
        # заряд найден: ждём выход за границу на свечах исполнения
        start = h1[i - 1][T] + 3600
        j = None
        for off in range(0, 5):
            j = ex_ts.get(start + off * step)
            if j is not None:
                break
        if j is None:
            continue
        end_ts = start + 16 * 3600
        base_bar = baseline * step / 3600
        while j < len(ex) and ex[j][T] <= end_ts:
            c = ex[j]
            buf = max(0.001, 0.1 * atr / c[C]) if c[C] else 0.001
            up = c[H] > hi * (1 + buf)
            dn = c[L] < lo * (1 - buf)
            if not up and not dn:
                j += 1; continue
            if base_bar > 0 and c[V] / base_bar < cfg["vol"]:
                j += 1; continue
            if not in_window(c[T]) or not btc_ok(c[T], up):
                j += 1; continue
            level = hi if up else lo
            entry = level * (1 + buf) if up else level * (1 - buf)
            raw = level - cfg["stop_atr"] * atr if up else level + cfg["stop_atr"] * atr
            dist = min(max(abs(entry - raw) / entry * 100, STOP_MIN), STOP_MAX)
            stop = entry * (1 - dist / 100) if up else entry * (1 + dist / 100)
            tp1, tp2 = targets_struct(entry, level, hi - lo, dist, up, sw_hi, sw_lo)
            t = run_trade(ex, j, "long" if up else "short", entry, stop, tp1, tp2, msk_day(c[T]))
            if t:
                v, a_ex = vw[j], atrs_ex[j]
                t["vwap_side"] = (entry >= v) if up else (entry <= v)     # вход по «правильную» сторону VWAP
                t["vwap_atr"] = abs(entry - v) / a_ex if a_ex else 0      # насколько далеко ушли от VWAP
                t["btc_abs"] = abs(btc_chg12(c[T]))
                t["sym"] = sym
                trades.append(t)
            break
    return trades

# ─── СТРАТЕГИЯ 2: ORB — пробой начального диапазона окна ──────────────────────

def strat_orb(sym, ex, h1, cfg):
    """Первые N минут торгового окна задают диапазон; торгуем его пробой до конца окна."""
    trades = []
    atrs = atr_series(ex, 14)
    by_day = {}
    for i, c in enumerate(ex):
        by_day.setdefault(msk_day(c[T]), []).append(i)
    step = TF_SEC[EXEC_TF]
    for day, idxs in by_day.items():
        for w_start, w_end in WINDOWS:
            rng = [i for i in idxs if w_start <= msk_min(ex[i][T]) < w_start + cfg["or_min"]]
            if len(rng) < max(1, cfg["or_min"] // (step // 60) // 2):
                continue
            hi = max(ex[i][H] for i in rng)
            lo = min(ex[i][L] for i in rng)
            if lo <= 0:
                continue
            # «монета в игре»: объём диапазона выше обычного
            if cfg["rvol"] > 0:
                base = [ex[i][V] for i in idxs[:max(4, len(idxs) // 3)]]
                norm = trimmed_mean(base)
                cur = sum(ex[i][V] for i in rng) / len(rng)
                if norm > 0 and cur / norm < cfg["rvol"]:
                    continue
            after = [i for i in idxs if w_start + cfg["or_min"] <= msk_min(ex[i][T]) < w_end]
            for j in after:
                c = ex[j]
                up = c[H] > hi
                dn = c[L] < lo
                if not up and not dn:
                    continue
                if not btc_ok(c[T], up):
                    break
                entry = hi if up else lo
                a = atrs[j] or (hi - lo)
                raw = entry - cfg["stop_atr"] * a if up else entry + cfg["stop_atr"] * a
                # стоп не ближе половины диапазона и не дальше его целиком
                raw = min(raw, entry - (hi - lo) * 0.5) if up else max(raw, entry + (hi - lo) * 0.5)
                dist = min(max(abs(entry - raw) / entry * 100, STOP_MIN), STOP_MAX)
                stop = entry * (1 - dist / 100) if up else entry * (1 + dist / 100)
                tp1 = entry * (1 + dist / 100) if up else entry * (1 - dist / 100)
                tp2 = entry * (1 + 2 * dist / 100) if up else entry * (1 - 2 * dist / 100)
                t = run_trade(ex, j, "long" if up else "short", entry, stop, tp1, tp2, day)
                if t:
                    trades.append(t)
                break
    return trades

# ─── VWAP дня (нужен двум стратегиям) ─────────────────────────────────────────

def day_vwap(ex):
    """Накопительный VWAP от начала суток МСК для каждой свечи."""
    out = [None] * len(ex)
    cur_day, pv, vol = None, 0.0, 0.0
    for i, c in enumerate(ex):
        d = msk_day(c[T])
        if d != cur_day:
            cur_day, pv, vol = d, 0.0, 0.0
        tp = (c[H] + c[L] + c[C]) / 3
        pv += tp * c[V]; vol += c[V]
        out[i] = pv / vol if vol else c[C]
    return out

# ─── СТРАТЕГИЯ 3: FADE — разворот после перебора от VWAP ──────────────────────

def strat_fade(sym, ex, h1, cfg):
    """Цена ушла от дневного VWAP дальше N ATR — входим против движения, цель VWAP."""
    trades = []
    atrs = atr_series(ex, 14)
    vw = day_vwap(ex)
    last_day = None
    for j in range(20, len(ex)):
        c, a, v = ex[j], atrs[j], vw[j]
        if not a or not v or not in_window(c[T]):
            continue
        d = msk_day(c[T])
        if d != last_day:
            traded_today = False
            last_day = d
        dist_atr = (c[C] - v) / a
        if abs(dist_atr) < cfg["dist_atr"] or traded_today:
            continue
        up = dist_atr < 0            # цена ниже VWAP — покупаем возврат
        # ВАЖНО: одного расстояния мало — без подтверждения разворота это ловля ножа
        # (проверено тестом: 25% побед даже там, где разворот заложен специально).
        prev = ex[j - 1]
        turn = (c[C] > c[O] and c[C] > prev[C]) if up else (c[C] < c[O] and c[C] < prev[C])
        if not turn:
            continue
        if not btc_ok(c[T], up):
            continue
        entry = c[C]
        raw = (min(x[L] for x in ex[max(0, j - 4):j + 1]) - 0.2 * a) if up else \
              (max(x[H] for x in ex[max(0, j - 4):j + 1]) + 0.2 * a)
        dist = min(max(abs(entry - raw) / entry * 100, STOP_MIN), STOP_MAX)
        stop = entry * (1 - dist / 100) if up else entry * (1 + dist / 100)
        tp1 = v                                            # цель — возврат к VWAP
        if (tp1 - entry) * (1 if up else -1) <= 0:
            continue
        tp2 = entry + (v - entry) * 1.5                    # с запасом за VWAP
        t = run_trade(ex, j, "long" if up else "short", entry, stop, tp1, tp2, d)
        if t:
            trades.append(t); traded_today = True
    return trades

# ─── СТРАТЕГИЯ 4: VWAP-тренд — откат к VWAP по тренду дня ─────────────────────

def strat_vwap(sym, ex, h1, cfg):
    """Цена уверенно выше дневного VWAP → ждём откат к VWAP и входим по тренду."""
    trades = []
    atrs = atr_series(ex, 14)
    vw = day_vwap(ex)
    armed_day, armed_side = None, None
    for j in range(20, len(ex)):
        c, a, v = ex[j], atrs[j], vw[j]
        if not a or not v:
            continue
        d = msk_day(c[T])
        if armed_day != d:
            armed_day, armed_side = d, None
        above = (c[C] - v) / v * 100
        if abs(above) >= cfg["trend_pct"]:
            armed_side = "long" if above > 0 else "short"   # тренд дня определён
            continue
        if armed_side is None or not in_window(c[T]):
            continue
        up = armed_side == "long"
        touched = (c[L] <= v <= c[H])                       # откат к VWAP
        if not touched or not btc_ok(c[T], up):
            continue
        entry = v
        raw = entry - cfg["stop_atr"] * a if up else entry + cfg["stop_atr"] * a
        dist = min(max(abs(entry - raw) / entry * 100, STOP_MIN), STOP_MAX)
        stop = entry * (1 - dist / 100) if up else entry * (1 + dist / 100)
        tp1 = entry * (1 + dist / 100) if up else entry * (1 - dist / 100)
        tp2 = entry * (1 + 2 * dist / 100) if up else entry * (1 - 2 * dist / 100)
        t = run_trade(ex, j, armed_side, entry, stop, tp1, tp2, d)
        if t:
            trades.append(t)
        armed_side = None
    return trades

STRATS = {"OURS": (strat_ours, OURS_GRID), "ORB": (strat_orb, ORB_GRID),
          "FADE": (strat_fade, FADE_GRID), "VWAP": (strat_vwap, VWAP_GRID)}

# ─── накопление результатов ───────────────────────────────────────────────────

class Agg:
    __slots__ = ("n", "wins", "total", "gp", "gl", "eq", "peak", "dd",
                 "dists", "side", "hour", "half", "pair", "daily", "gross")

    def __init__(self):
        self.n = self.wins = 0
        self.total = self.gp = self.gl = self.eq = self.peak = self.dd = self.gross = 0.0
        self.dists, self.side, self.hour = [], {}, {}
        self.half = {0: [0, 0.0], 1: [0, 0.0]}
        self.pair, self.daily = {}, {}

    def add(self, t, sym, half):
        p = t["pnl"]
        self.n += 1; self.total += p; self.gross += t["gross"]
        if p > 0:
            self.wins += 1; self.gp += p
        else:
            self.gl -= p
        self.eq += p; self.peak = max(self.peak, self.eq); self.dd = min(self.dd, self.eq - self.peak)
        if len(self.dists) < 3000:
            self.dists.append(t["dist"])
        s = self.side.setdefault(t["side"], [0, 0.0]); s[0] += 1; s[1] += p
        h = self.hour.setdefault(t["hour"], [0, 0.0]); h[0] += 1; h[1] += p
        q = self.half[half]; q[0] += 1; q[1] += p
        pr = self.pair.setdefault(sym, [0, 0.0]); pr[0] += 1; pr[1] += p
        self.daily[t["day"]] = self.daily.get(t["day"], 0.0) + p

    def result(self):
        if not self.n:
            return None
        return {"n": self.n, "wr": self.wins / self.n * 100, "avg": self.total / self.n,
                "total": self.total, "dd": self.dd, "gross_avg": self.gross / self.n,
                "pf": (self.gp / self.gl) if self.gl else float("inf"),
                "stop": statistics.median(self.dists) if self.dists else 0,
                "side": self.side, "hour": self.hour, "half": self.half,
                "pair": self.pair, "daily": self.daily}

def corr(a, b):
    keys = sorted(set(a) | set(b))
    if len(keys) < 5:
        return None
    x = [a.get(k, 0.0) for k in keys]; y = [b.get(k, 0.0) for k in keys]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = sum((v - mx) ** 2 for v in x) ** 0.5
    sy = sum((v - my) ** 2 for v in y) ** 0.5
    if not sx or not sy:
        return None
    return sum((x[i] - mx) * (y[i] - my) for i in range(len(x))) / (sx * sy)

def filter_lab(trades):
    """Прогоняет уже собранные сделки через разные фильтры и сравнивает итог.
    Сделки не пересчитываются — меняется только то, какие из них мы берём."""
    trades = sorted(trades, key=lambda t: t["ts"])

    def run(keep, cap_day=None, stop_after_losses=None, one_per_sym=False, max_side_30m=None):
        taken, per_day, sym_day, recent = [], {}, set(), []
        closed = []          # (время закрытия, день, убыток?) — только уже известные на момент входа
        for t in trades:
            if not keep(t):
                continue
            d = t["day"]
            # ВАЖНО: убытки дня считаем по времени ЗАКРЫТИЯ сделок, а не открытия —
            # иначе получилось бы подглядывание в будущее (исход ещё не известен).
            losses_known = sum(1 for (ets, dd, lost) in closed
                               if lost and dd == d and ets <= t["ts"])
            if one_per_sym and (t["sym"], d) in sym_day:
                continue
            if cap_day and per_day.get(d, 0) >= cap_day:
                continue
            if stop_after_losses and losses_known >= stop_after_losses:
                continue
            if max_side_30m:
                recent = [r for r in recent if t["ts"] - r[0] <= 1800]
                if sum(1 for r in recent if r[1] == t["side"]) >= max_side_30m:
                    continue
                recent.append((t["ts"], t["side"]))
            taken.append(t)
            per_day[d] = per_day.get(d, 0) + 1
            sym_day.add((t["sym"], d))
            closed.append((t.get("exit_ts", t["ts"]), d, t["pnl"] <= 0))
        if len(taken) < 50:
            return None
        pnl = [t["pnl"] for t in taken]
        eq = peak = dd = 0.0
        day_pnl = {}
        for t in taken:
            eq += t["pnl"]; peak = max(peak, eq); dd = min(dd, eq - peak)
            day_pnl[t["day"]] = day_pnl.get(t["day"], 0) + t["pnl"]
        wr = sum(1 for p in pnl if p > 0) / len(pnl) * 100
        worst_day = min(day_pnl.values()) if day_pnl else 0
        return {"n": len(taken), "wr": wr, "avg": sum(pnl) / len(pnl), "total": sum(pnl),
                "dd": dd, "worst_day": worst_day, "per_day": len(taken) / max(len(day_pnl), 1)}

    tests = [
        ("без фильтров (как есть)", dict(keep=lambda t: True)),
        ("вход по нужную сторону VWAP", dict(keep=lambda t: t.get("vwap_side", True))),
        ("не дальше 2 ATR от VWAP", dict(keep=lambda t: t.get("vwap_atr", 0) <= 2)),
        ("VWAP: сторона + не дальше 2 ATR", dict(keep=lambda t: t.get("vwap_side", True) and t.get("vwap_atr", 0) <= 2)),
        ("BTC двигался сильно (≥1.5% за 12ч)", dict(keep=lambda t: t.get("btc_abs", 0) >= 1.5)),
        ("одна сделка на монету в день", dict(keep=lambda t: True, one_per_sym=True)),
        ("не больше 2 в одну сторону за 30 мин", dict(keep=lambda t: True, max_side_30m=2)),
        ("максимум 8 сделок в день", dict(keep=lambda t: True, cap_day=8)),
        ("стоп дня: после 3 убытков не торгуем", dict(keep=lambda t: True, stop_after_losses=3)),
        ("8 в день + одна на монету", dict(keep=lambda t: True, cap_day=8, one_per_sym=True)),
        ("8 в день + одна на монету + стоп дня 3", dict(keep=lambda t: True, cap_day=8, one_per_sym=True, stop_after_losses=3)),
        ("VWAP-сторона + 8 в день + одна на монету", dict(keep=lambda t: t.get("vwap_side", True), cap_day=8, one_per_sym=True)),
        ("≤2 ATR от VWAP + одна на монету", dict(keep=lambda t: t.get("vwap_atr", 0) <= 2, one_per_sym=True)),
        ("≤2 ATR от VWAP + 8 в день", dict(keep=lambda t: t.get("vwap_atr", 0) <= 2, cap_day=8)),
        ("≤2 ATR + 8/день + 1/монету", dict(keep=lambda t: t.get("vwap_atr", 0) <= 2, cap_day=8, one_per_sym=True)),
        ("≤2 ATR + 8/день + 1/монету + ≤2 в сторону/30мин",
         dict(keep=lambda t: t.get("vwap_atr", 0) <= 2, cap_day=8, one_per_sym=True, max_side_30m=2)),
        ("≤2 ATR + 8/день + 1/монету + стоп дня 3",
         dict(keep=lambda t: t.get("vwap_atr", 0) <= 2, cap_day=8, one_per_sym=True, stop_after_losses=3)),
        ("ВСЁ: ≤2 ATR + 8/день + 1/монету + ≤2/30мин + стоп дня 3",
         dict(keep=lambda t: t.get("vwap_atr", 0) <= 2, cap_day=8, one_per_sym=True,
              max_side_30m=2, stop_after_losses=3)),
    ]
    out = ["\n<b>ЛАБОРАТОРИЯ ФИЛЬТРОВ (наша стратегия)</b>",
           "Что будет с просадкой и прибылью, если отбирать сделки по-разному:"]
    base = None
    for name, kw in tests:
        r = run(**kw)
        if not r:
            out.append(f"   {name}: сделок мало"); continue
        if base is None:
            base = r
        d_avg = r["avg"] - base["avg"]
        d_dd = r["dd"] - base["dd"]
        mark = "✅" if (r["dd"] > base["dd"] * 0.75 and r["avg"] >= base["avg"] - 0.02) else (
                "🟡" if r["dd"] > base["dd"] else "⚪")
        out.append(f"   {mark} {name}")
        out.append(f"        n={r['n']:<5} ({r['per_day']:.1f}/день) | WR {r['wr']:.0f}% | "
                   f"на сделку {r['avg']:+.3f}% ({d_avg:+.3f}) | всего {r['total']:+.0f}% | "
                   f"просадка {r['dd']:.0f}% ({d_dd:+.0f}) | худший день {r['worst_day']:.1f}%")
    out.append("   ✅ — просадка заметно меньше без потери прибыли на сделку")
    return out

def send_telegram(text):
    print(text)
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    for i in range(0, len(text), 3800):
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text[i:i + 3800], "parse_mode": "HTML"},
                          timeout=20)
        except Exception as e:
            print(f"[TG ERR] {e}")
        time.sleep(0.5)

# ─── главное ──────────────────────────────────────────────────────────────────

def main():
    pairs = ALL_PAIRS[:N_PAIRS]
    now = int(time.time()); frm = now - DAYS * 86400
    half_ts = frm + DAYS * 86400 // 2
    combos = [(name, i, cfg) for name, (_, grid) in STRATS.items() for i, cfg in enumerate(grid)]
    acc = {(n, i): Agg() for n, i, _ in combos}
    print(f"Сравнение стратегий: {len(pairs)} пар, {DAYS} дней, исполнение {EXEC_TF}, "
          f"издержки {COST_PCT}% на сделку")
    send_telegram(f"🔬 <b>Сравнение стратегий запущено</b>\nПар: {len(pairs)} | дней: {DAYS} | "
                  f"исполнение: {EXEC_TF} | издержки {COST_PCT}%\n"
                  f"Стратегии: {', '.join(STRATS)} — все внутридневные, закрытие в тот же день.")
    if not load_btc(frm, now):
        print("[BTC] не загружен — фильтр BTC работать не будет")

    ours_trades = []
    used, depth_note, t0 = 0, "", time.time()
    for idx, sym in enumerate(pairs, 1):
        ex = get_candles(sym, EXEC_TF, frm, now)
        h1 = get_candles(sym, "1h", frm, now)
        if len(ex) < 500 or len(h1) < 200:
            print(f"  [{idx}/{len(pairs)}] {sym}: мало данных")
            continue
        used += 1
        if not depth_note:
            depth_note = (f"{(ex[-1][T]-ex[0][T])/86400:.0f} дн по {EXEC_TF}, "
                          f"{(h1[-1][T]-h1[0][T])/86400:.0f} дн по 1h")
        n_all = 0
        for name, i, cfg in combos:
            fn = STRATS[name][0]
            try:
                trs = fn(sym, ex, h1, cfg)
            except Exception as e:
                print(f"  [ERR] {name} {sym}: {e}"); continue
            for t in trs:
                ts_day = datetime.strptime(t["day"], "%Y-%m-%d").replace(tzinfo=MSK).timestamp()
                acc[(name, i)].add(t, sym, 0 if ts_day < half_ts else 1)
            if name == "OURS" and i == 0:          # сделки основного варианта — для лаборатории фильтров
                ours_trades.extend(trs)
            n_all += len(trs)
        del ex, h1; gc.collect()
        el = int(time.time() - t0)
        print(f"  [{idx}/{len(pairs)}] {sym}: сделок {n_all} | {el}с, осталось ~{int(el/idx*(len(pairs)-idx))}с")

    # ── отчёт ──
    lines = ["🔬 <b>Сравнение стратегий — готово</b>",
             f"Пар: {used} | период: {depth_note} | исполнение {EXEC_TF} | издержки {COST_PCT}%",
             "Все стратегии внутридневные: сделка закрывается в тот же день.\n"]
    best_of = {}
    for name in STRATS:
        rows = []
        for i, cfg in enumerate(STRATS[name][1]):
            st = acc[(name, i)].result()
            if st and st["n"] >= 30:
                rows.append((cfg, st))
        if not rows:
            lines.append(f"<b>{name}</b>: сделок слишком мало"); continue
        rows.sort(key=lambda r: -r[1]["avg"])
        best_of[name] = rows[0]
        lines.append(f"<b>{name}</b> (лучший вариант из {len(STRATS[name][1])}):")
        for cfg, st in rows:
            h0, h1_ = st["half"][0], st["half"][1]
            a0 = h0[1] / h0[0] if h0[0] else 0
            a1 = h1_[1] / h1_[0] if h1_[0] else 0
            ok = "✅" if a0 > 0 and a1 > 0 else "⚠️"
            lines.append(f"   {cfg} → n={st['n']:<5} WR {st['wr']:.0f}% | на сделку {st['avg']:+.3f}% | "
                         f"всего {st['total']:+.0f}% | PF {st['pf']:.2f} | просадка {st['dd']:.0f}% | "
                         f"стоп {st['stop']:.2f}% | половины {a0:+.2f}/{a1:+.2f} {ok}")

    if best_of:
        lines.append("\n<b>ИТОГ — лучшие варианты рядом:</b>")
        order = sorted(best_of.items(), key=lambda kv: -kv[1][1]["avg"])
        for name, (cfg, st) in order:
            sd = st["side"]
            lo = sd.get("long", [0, 0]); sh = sd.get("short", [0, 0])
            lines.append(f"   {name:5s} n={st['n']:<5} на сделку {st['avg']:+.3f}% | WR {st['wr']:.0f}% | "
                         f"PF {st['pf']:.2f} | просадка {st['dd']:.0f}% | "
                         f"сделок в день ~{st['n'] / max(DAYS,1):.1f}")
            if lo[0] and sh[0]:
                lines.append(f"          лонг {lo[1]/lo[0]:+.3f}% ({lo[0]}) | шорт {sh[1]/sh[0]:+.3f}% ({sh[0]})")

        lines.append("\n<b>Корреляция дневных результатов</b> (0 — независимы, 1 — одно и то же):")
        names = [n for n, _ in order]
        for a in range(len(names)):
            for b in range(a + 1, len(names)):
                c = corr(best_of[names[a]][1]["daily"], best_of[names[b]][1]["daily"])
                if c is not None:
                    mark = "🟢 независимы" if abs(c) < 0.3 else ("🟡 похожи" if abs(c) < 0.6 else "🔴 дублируют друг друга")
                    lines.append(f"   {names[a]} ↔ {names[b]}: {c:+.2f} {mark}")

        lines.append("\n<b>По часам МСК (лучшая стратегия)</b>:")
        bn, (bc, bst) = order[0]
        lines.append(f"   {bn}: " + ", ".join(f"{h}ч {v[1]/v[0]:+.2f}%" for h, v in sorted(bst["hour"].items())))
    if ours_trades:
        lines += filter_lab(ours_trades)
    lines.append("\n⚠️ OI, фандинг и дельта не участвуют — у Gate нет их истории. "
                 "Все стратегии считаны одним движком с одинаковыми издержками.")
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
