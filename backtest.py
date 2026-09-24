"""
Бэктест Upscale Bot: ЗАРЯД → ПРОБОЙ на истории Gate.io.

Что проверяет:
  • таймфрейм заряда (15m / 30m / 1h) и окно сжатия;
  • подтверждение пробоя: касание цены / закрытие 1m / 5m / 15m свечи;
  • требование к объёму свечи пробоя;
  • стоп (множитель ATR, потолок) и цели (в стопах или по структуре);
  • окна времени МСК (весь день / окна v8.2);
  • обе стороны отдельно, с учётом комиссии.

Чего НЕ проверяет: OI, фандинг, taker L/S, дельту — Gate не отдаёт их историю.
Эти показатели проверяются только вперёд, на живой работе бота.

Запуск: python backtest.py
Переменные окружения: TELEGRAM_TOKEN, CHAT_ID (необязательно — тогда только в лог),
  BT_DAYS (по умолчанию 45), BT_PAIRS (сколько пар, по умолчанию 40),
  BT_CONFIRM_TF (1m|5m — таймфрейм подтверждения; 1m точнее, но качает в 5 раз больше данных).
Зависимости: только requests.
"""

import os
import time

import statistics
import itertools
import threading
from datetime import datetime, timezone, timedelta

import requests

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
DAYS           = int(os.environ.get("BT_DAYS", "45"))
N_PAIRS        = int(os.environ.get("BT_PAIRS", "40"))
CONFIRM_TF     = os.environ.get("BT_CONFIRM_TF", "5m")     # 1m — точнее, но тяжелее
FEE_PCT        = 0.10        # комиссия вход+выход, % от объёма (Gate taker ~0.05% × 2)
MSK            = timezone(timedelta(hours=3))

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

# ─── ПЕРЕБИРАЕМЫЕ НАСТРОЙКИ ───────────────────────────────────────────────────
# Каждая комбинация прогоняется по всем парам и всей истории.

GRID = {
    "charge_tf":    ["15m", "30m", "1h"],      # таймфрейм заряда
    "squeeze_pctl": [25, 35, 50],              # порог сжатия (процентиль ширины BB)
    "confirm":      ["touch", "close"],        # касание цены или закрытие свечи подтверждения
    "min_bar_rvol": [0.0, 1.2, 2.0],           # объём свечи пробоя к норме
    "stop_atr":     [1.0, 1.5],                # стоп = N × ATR за уровнем
    "targets":      ["1R/2R", "1.5R/3R", "struct"],  # цели
    "session":      ["all", "v82"],            # всё время или окна 10:00–11:30 и 14:30–21:00 МСК
}

ACC_WINDOW      = 12      # свечей в окне заряда
ACC_FLAT_ATR    = 2.0     # |ход за окно| ≤ N × нормальный ATR
ACC_MAX_RANGE   = 5.0     # % — шире не считаем зарядом
ACC_RVOL_MIN    = 1.3     # объём второй половины окна к норме
BASE_FROM, BASE_TO = 84, 12   # база объёма/ATR: закрытые свечи [-84:-12]
SWING_LOOKBACK  = 144     # свечей для процентиля BB и свингов
BREAK_BUFFER    = 0.001   # 0.1% за уровень
STOP_MIN, STOP_MAX = 0.8, 3.0      # % границы стопа
HOLD_BARS_MAX   = 48      # сколько свечей подтверждения держим сделку (48×5м = 4ч)
WATCH_TTL_MULT  = 16      # заряд живёт N свечей своего ТФ

SESSIONS = {
    "all": None,
    "v82": [(10 * 60, 11 * 60 + 30), (14 * 60 + 30, 21 * 60)],
}

# ─── СЕТЬ ─────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_last = [0.0]

def api_get(path, params, tries=3):
    url = f"https://api.gateio.ws/api/v4/futures/usdt/{path}"
    for a in range(tries):
        with _lock:                       # не чаще 10 запросов в секунду
            wait = 0.1 - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (a + 1)); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if a == tries - 1:
                print(f"  [ERR] {path} {params.get('contract')}: {e}")
                return None
            time.sleep(1 + a)
    return None

def get_candles(sym, tf, frm, to):
    """Свечи за период с постраничной догрузкой (Gate отдаёт максимум 2000 за раз)."""
    step = TF_SEC[tf]
    out, cur = [], frm
    while cur < to:
        chunk_to = min(cur + step * 1900, to)
        raw = api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": tf,
                                       "from": int(cur), "to": int(chunk_to)})
        if not raw:
            break
        for c in raw:
            out.append((int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]),
                        float(c["c"]), float(c["v"])))
        cur = chunk_to + step
        if len(raw) < 2:
            break
    out.sort()
    ded, seen = [], set()
    for c in out:
        if c[0] not in seen:
            seen.add(c[0]); ded.append(c)
    return ded

# ─── ВСПОМОГАТЕЛЬНОЕ ──────────────────────────────────────────────────────────

T, O, H, L, C, V = 0, 1, 2, 3, 4, 5

def trimmed_mean(vals):
    if not vals:
        return 0.0
    s = sorted(vals)
    cut = max(1, len(s) // 10)
    s = s[:-cut] if len(s) > 2 * cut else s
    return sum(s) / len(s) if s else 0.0

def true_ranges(cs):
    out = [cs[0][H] - cs[0][L]]
    for i in range(1, len(cs)):
        p = cs[i - 1][C]
        out.append(max(cs[i][H] - cs[i][L], abs(cs[i][H] - p), abs(p - cs[i][L])))
    return out

def bb_width(cs):
    """Ширина полос Боллинджера (20) в % от средней — для каждой свечи с 20-й."""
    out = [None] * len(cs)
    for i in range(19, len(cs)):
        w = [c[C] for c in cs[i - 19:i + 1]]
        mean = sum(w) / 20
        sd = statistics.pstdev(w)
        out[i] = (4 * sd / mean * 100) if mean else None
    return out

def swings(cs, left=2, right=2):
    hi, lo = [], []
    for i in range(left, len(cs) - right):
        if all(cs[i][H] >= cs[j][H] for j in range(i - left, i + right + 1) if j != i):
            hi.append((i, cs[i][H]))
        if all(cs[i][L] <= cs[j][L] for j in range(i - left, i + right + 1) if j != i):
            lo.append((i, cs[i][L]))
    return hi, lo

def in_session(ts, sess):
    if sess is None:
        return True
    mins = datetime.fromtimestamp(ts, MSK).hour * 60 + datetime.fromtimestamp(ts, MSK).minute
    return any(a <= mins < b for a, b in sess)

# ─── ПОИСК ЗАРЯДОВ ────────────────────────────────────────────────────────────

def find_charges(cs, squeeze_pctl):
    """Возвращает список зарядов: (индекс последней свечи окна, hi, lo, atr).
    Логика повторяет бота, но без OI/taker — их истории нет."""
    n = len(cs)
    if n < BASE_FROM + ACC_WINDOW + 5:
        return []
    trs = true_ranges(cs)
    bbw = bb_width(cs)
    res = []
    for i in range(BASE_FROM + ACC_WINDOW, n):
        win = cs[i - ACC_WINDOW:i]
        base = cs[i - BASE_FROM:i - BASE_TO]
        if len(base) < 20:
            continue
        baseline = trimmed_mean([c[V] for c in base])
        if baseline <= 0:
            continue
        tr_base = trs[i - BASE_FROM:i - BASE_TO]
        atr_norm = sum(tr_base) / len(tr_base) if tr_base else 0
        if atr_norm <= 0:
            continue
        hi = max(c[H] for c in win); lo = min(c[L] for c in win)
        if lo <= 0:
            continue
        rng_pct = (hi - lo) / lo * 100
        if rng_pct > ACC_MAX_RANGE:
            continue
        if abs(win[-1][C] - win[0][O]) > ACC_FLAT_ATR * atr_norm:
            continue                                     # цена не стоит
        # сжатие: ширина BB в нижних N% за SWING_LOOKBACK свечей ИЛИ узкие свечи
        hist = [x for x in bbw[max(0, i - SWING_LOOKBACK):i] if x is not None]
        cur_bb = bbw[i - 1]
        squeezed = False
        if hist and cur_bb is not None:
            pctl = sum(1 for x in hist if x < cur_bb) / len(hist) * 100
            squeezed = pctl <= squeeze_pctl
        tr_win = sum(trs[i - ACC_WINDOW:i]) / ACC_WINDOW
        if not squeezed and not (tr_win <= 0.75 * atr_norm):
            continue
        half = ACC_WINDOW // 2
        vol_half = sum(c[V] for c in win[-half:]) / half
        if vol_half / baseline < ACC_RVOL_MIN:
            continue                                     # объём не поджимается
        res.append({"i": i, "hi": hi, "lo": lo, "atr": atr_norm,
                    "ts": cs[i - 1][T] + (cs[1][T] - cs[0][T]),
                    "baseline": baseline, "rng": hi - lo})
    return res

# ─── СИМУЛЯЦИЯ СДЕЛОК ─────────────────────────────────────────────────────────

def simulate(charges, conf, cfg, tf_sec, sw_hi, sw_lo, cs_tf):
    """conf — свечи подтверждения (1m/5m). Возвращает список сделок."""
    trades = []
    if not conf:
        return trades
    conf_sec = TF_SEC[CONFIRM_TF]
    idx_by_ts = {c[T]: k for k, c in enumerate(conf)}
    ttl = WATCH_TTL_MULT * tf_sec
    base_bar = None
    for ch in charges:
        start_ts = ch["ts"]
        k = None
        for off in range(0, 6):                    # ищем первую свечу подтверждения после заряда
            k = idx_by_ts.get(start_ts + off * conf_sec)
            if k is not None:
                break
        if k is None:
            continue
        base_bar = ch["baseline"] * conf_sec / tf_sec     # норма объёма на свечу подтверждения
        hi, lo, atr = ch["hi"], ch["lo"], ch["atr"]
        end_ts = start_ts + ttl
        j = k
        while j < len(conf) and conf[j][T] <= end_ts:
            c = conf[j]
            buf = max(BREAK_BUFFER, 0.1 * atr / c[C]) if c[C] > 0 else BREAK_BUFFER
            up = dn = False
            if cfg["confirm"] == "touch":
                up = c[H] > hi * (1 + buf); dn = c[L] < lo * (1 - buf)
                entry = hi * (1 + buf) if up else lo * (1 - buf)
            else:
                up = c[C] > hi * (1 + buf); dn = c[C] < lo * (1 - buf)
                entry = c[C]
            if not up and not dn:
                j += 1; continue
            if cfg["min_bar_rvol"] and base_bar > 0 and c[V] / base_bar < cfg["min_bar_rvol"]:
                j += 1; continue
            if not in_session(c[T], SESSIONS[cfg["session"]]):
                j += 1; continue
            side = "long" if up else "short"
            level = hi if up else lo
            # стоп за уровнем на N×ATR, в границах
            raw_stop = level - cfg["stop_atr"] * atr if up else level + cfg["stop_atr"] * atr
            dist = abs(entry - raw_stop) / entry * 100
            dist = min(max(dist, STOP_MIN), STOP_MAX)
            stop = entry * (1 - dist / 100) if up else entry * (1 + dist / 100)
            # цели
            if cfg["targets"] == "struct":
                cands = [p for _, p in (sw_hi if up else sw_lo)]
                cands = sorted([p for p in cands if p > entry * (1 + dist / 100)]) if up else \
                        sorted([p for p in cands if 0 < p < entry * (1 - dist / 100)], reverse=True)
                h = ch["rng"]
                mm1 = level + h if up else level - h
                mm2 = level + 2 * h if up else level - 2 * h
                pool = (cands + [mm1, mm2]) if up else (cands + [mm1, mm2])
                pool = sorted(pool) if up else sorted(pool, reverse=True)
                tp1 = pool[0] if pool else (entry * (1 + dist / 100) if up else entry * (1 - dist / 100))
                nxt = [p for p in pool if (p > tp1 * 1.002 if up else p < tp1 * 0.998)]
                tp2 = nxt[0] if nxt else (tp1 * 1.005 if up else tp1 * 0.995)
            else:
                r1, r2 = (1.0, 2.0) if cfg["targets"] == "1R/2R" else (1.5, 3.0)
                tp1 = entry * (1 + r1 * dist / 100) if up else entry * (1 - r1 * dist / 100)
                tp2 = entry * (1 + r2 * dist / 100) if up else entry * (1 - r2 * dist / 100)
            # ведём сделку по свечам подтверждения
            res, hit1 = None, False
            for q in range(j + 1, min(j + 1 + HOLD_BARS_MAX, len(conf))):
                b = conf[q]
                stop_hit = b[L] <= stop if up else b[H] >= stop
                t1_hit   = b[H] >= tp1 if up else b[L] <= tp1
                t2_hit   = b[H] >= tp2 if up else b[L] <= tp2
                if stop_hit and not hit1:
                    res = -dist; break                      # стоп первым (консервативно)
                if t1_hit and not hit1:
                    hit1 = True
                    if t2_hit:                              # обе цели в одной свече
                        res = (abs(tp1 - entry) / entry * 50 + abs(tp2 - entry) / entry * 50); break
                    continue
                if hit1:
                    if t2_hit:
                        res = (abs(tp1 - entry) / entry * 50 + abs(tp2 - entry) / entry * 50); break
                    if stop_hit:                            # половина по TP1, вторая — в безубыток
                        res = abs(tp1 - entry) / entry * 50; break
            if res is None:
                last = conf[min(j + HOLD_BARS_MAX, len(conf) - 1)][C]
                chg = (last - entry) / entry * 100 * (1 if up else -1)
                res = (abs(tp1 - entry) / entry * 50 + chg * 0.5) if hit1 else chg
            trades.append({"side": side, "pnl": res - FEE_PCT, "hour": datetime.fromtimestamp(c[T], MSK).hour,
                           "dist": dist, "hit1": hit1})
            break       # один заряд — одна сделка
    return trades

# ─── ОТЧЁТ ────────────────────────────────────────────────────────────────────

def stats(trades):
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    pnl = [t["pnl"] for t in trades]
    total = sum(pnl)
    eq, peak, dd = 0, 0, 0
    for p in pnl:
        eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
    gross_p = sum(p for p in pnl if p > 0)
    gross_l = -sum(p for p in pnl if p < 0)
    return {"n": n, "wr": len(wins) / n * 100, "avg": total / n, "total": total,
            "dd": dd, "pf": (gross_p / gross_l) if gross_l else float("inf")}

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

# ─── ГЛАВНОЕ ──────────────────────────────────────────────────────────────────

def main():
    pairs = ALL_PAIRS[:N_PAIRS]
    now = int(time.time())
    frm = now - DAYS * 86400
    print(f"Бэктест: {len(pairs)} пар, {DAYS} дней, подтверждение по {CONFIRM_TF}, комиссия {FEE_PCT}%")
    send_telegram(f"🔬 <b>Бэктест запущен</b>\nПар: {len(pairs)} | дней: {DAYS} | "
                  f"подтверждение: {CONFIRM_TF} | комиссия {FEE_PCT}%\nЭто займёт 10–30 минут…")

    tfs = sorted(set(GRID["charge_tf"]))
    data = {}          # sym -> {tf: candles}
    t0 = time.time()
    for idx, sym in enumerate(pairs, 1):
        d = {}
        ok = True
        for tf in tfs + [CONFIRM_TF]:
            cs = get_candles(sym, tf, frm, now)
            if len(cs) < 200:
                ok = False; break
            d[tf] = cs
        if ok:
            data[sym] = d
        print(f"  [{idx}/{len(pairs)}] {sym}: {'ok' if ok else 'мало данных'} "
              f"({int(time.time() - t0)}с)")
    print(f"Данные загружены за {int(time.time() - t0)}с, пар в работе: {len(data)}")

    # заряды считаем один раз на каждую пару/ТФ/порог сжатия
    charges = {}
    for sym, d in data.items():
        for tf in tfs:
            for sq in GRID["squeeze_pctl"]:
                charges[(sym, tf, sq)] = find_charges(d[tf], sq)
    tot_ch = sum(len(v) for v in charges.values())
    print(f"Зарядов найдено (по всем комбинациям): {tot_ch}")

    keys = list(GRID)
    results = []
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    for ci, combo in enumerate(combos, 1):
        cfg = dict(zip(keys, combo))
        trades = []
        for sym, d in data.items():
            tf = cfg["charge_tf"]
            ch = charges[(sym, tf, cfg["squeeze_pctl"])]
            if not ch:
                continue
            sw_hi, sw_lo = swings(d[tf])
            trades += simulate(ch, d[CONFIRM_TF], cfg, TF_SEC[tf], sw_hi, sw_lo, d[tf])
        st = stats(trades)
        if st:
            results.append((cfg, st, trades))
        if ci % 20 == 0:
            print(f"  протестировано комбинаций: {ci}/{len(combos)}")

    results.sort(key=lambda r: -r[1]["avg"])
    lines = [f"🔬 <b>Бэктест готов</b> — {len(data)} пар, {DAYS} дней, подтверждение {CONFIRM_TF}",
             f"Комбинаций: {len(results)}\n",
             "<b>Лучшие 12 (по прибыли на сделку, после комиссии):</b>"]
    for cfg, st, _ in results[:12]:
        lines.append(f"{cfg['charge_tf']:>3} | сжатие {cfg['squeeze_pctl']:>2} | {cfg['confirm']:>5} | "
                     f"объём {cfg['min_bar_rvol']:.1f}× | стоп {cfg['stop_atr']}ATR | {cfg['targets']:>7} | "
                     f"{cfg['session']:>3} → n={st['n']:<5} winrate {st['wr']:.0f}% "
                     f"на сделку {st['avg']:+.3f}% всего {st['total']:+.0f}% PF {st['pf']:.2f}")
    lines.append("\n<b>Худшие 3:</b>")
    for cfg, st, _ in results[-3:]:
        lines.append(f"{cfg['charge_tf']} | {cfg['confirm']} | {cfg['targets']} | {cfg['session']} → "
                     f"n={st['n']} на сделку {st['avg']:+.3f}%")

    def summarize(param):
        agg = {}
        for cfg, st, _ in results:
            agg.setdefault(cfg[param], []).append(st)
        out = [f"\n<b>Влияние «{param}»</b> (среднее по остальным настройкам):"]
        for val, sts in sorted(agg.items(), key=lambda x: -sum(s["avg"] for s in x[1]) / len(x[1])):
            avg = sum(s["avg"] for s in sts) / len(sts)
            n = sum(s["n"] for s in sts) // len(sts)
            wr = sum(s["wr"] for s in sts) / len(sts)
            out.append(f"   {str(val):>8}: на сделку {avg:+.3f}% | winrate {wr:.0f}% | сделок ~{n}")
        return out
    for p in keys:
        lines += summarize(p)

    best_cfg, best_st, best_tr = results[0]
    by_side, by_hour = {}, {}
    for t in best_tr:
        by_side.setdefault(t["side"], []).append(t["pnl"])
        by_hour.setdefault(t["hour"], []).append(t["pnl"])
    lines.append("\n<b>Лучшая комбинация — детали:</b>")
    for s, v in by_side.items():
        lines.append(f"   {s}: n={len(v)}, на сделку {sum(v)/len(v):+.3f}%, всего {sum(v):+.0f}%")
    lines.append("   по часам МСК: " + ", ".join(
        f"{h}ч {sum(v)/len(v):+.2f}%" for h, v in sorted(by_hour.items())))
    lines.append(f"\nПросадка лучшей: {best_st['dd']:.0f}% | Profit factor {best_st['pf']:.2f}")
    lines.append("\n⚠️ OI, фандинг, taker L/S и дельта в бэктесте НЕ участвуют — Gate не отдаёт их историю.")
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
