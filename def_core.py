"""
Motor KIBITO DEFINITIVO (5m) — mismo cálculo que el script de TradingView "KIBITO DEFINITIVO"
y que el backtest de investigación (9 monedas, 2021-2026, PF 1,47).

Reglas LARGO (corto = espejo):
  1. Día extremo: moneda >= +M% desde la apertura 00:00 UTC y >= RS puntos más que BTC
  2. RSI(14) tocó <= PULL en las últimas K velas (incluida la actual)
  3. RSI cruza por encima de 50 en la vela cerrada
  4. SuperTrend(3,10) 5m alcista y SuperTrend 4h (última vela 4h CERRADA) alcista
  5. Cierre > VWAP diario (hlc3, desde 00:00 UTC)
Stop inicial = línea SuperTrend 5m (si no es válida, 4 ATR). BE a 1R. El stop sigue la línea.
Cierre si la línea gira. Sin objetivo fijo.
"""
import math
from dataclasses import dataclass

P = dict(M=6.0, RS=4.0, rsiLen=14, pull=40.0, K=8, mid=50.0, stMult=3.0, stLen=10,
         atrLen=14, fallbackAtr=4.0, useVwap=True, useHtf=True,
         cooldown=6, maxDay=3, lossStreak=3, pauseBars=144, maxBars=288,
         exitMode="ST", atrSL=3.0, atrTP=6.0, atrTrail=2.5)

DAY_MS = 86_400_000


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


def parse_kline(k):
    if isinstance(k, dict):
        return Bar(int(k["time"]), float(k["open"]), float(k["high"]), float(k["low"]),
                   float(k["close"]), float(k.get("volume", 0)))
    return Bar(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))


def rma(xs, n):
    out, a, al = [], None, 1.0 / n
    for x in xs:
        a = x if a is None else a + al * (x - a)
        out.append(a)
    return out


def atr(bars, n):
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b.h - b.l)
        else:
            pc = bars[i - 1].c
            tr.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    return rma(tr, n)


def rsi(closes, n):
    up, dn = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        up.append(max(d, 0.0))
        dn.append(max(-d, 0.0))
    ru, rd = rma(up, n), rma(dn, n)
    return [100.0 if d == 0 else 100 - 100 / (1 + u / d) for u, d in zip(ru, rd)]


def supertrend(bars, m, n):
    """Igual que ta.supertrend de Pine. Devuelve (linea, dir) con dir +1 alcista / -1 bajista."""
    a = atr(bars, n)
    line, dirs = [], []
    lo_p = up_p = st_p = None
    for i, b in enumerate(bars):
        hl2 = (b.h + b.l) / 2
        lo, up = hl2 - m * a[i], hl2 + m * a[i]
        if i > 0:
            if not (lo > lo_p or bars[i - 1].c < lo_p):
                lo = lo_p
            if not (up < up_p or bars[i - 1].c > up_p):
                up = up_p
        if i == 0:
            d = 1
        elif st_p == up_p:
            d = -1 if b.c > up else 1
        else:
            d = 1 if b.c < lo else -1
        s = lo if d == -1 else up
        line.append(s)
        dirs.append(-d)
        lo_p, up_p, st_p = lo, up, s
    return line, dirs


class Features:
    """Indicadores de la moneda en velas de 5m cerradas (todas causales)."""

    def __init__(self, bars, htf_dir, btc_open, btc_close, p=P):
        self.bars, self.p, self.n = bars, p, len(bars)
        c = [b.c for b in bars]
        self.atr = atr(bars, p["atrLen"])
        self.rsi = rsi(c, p["rsiLen"])
        self.st, self.st_dir = supertrend(bars, p["stMult"], p["stLen"])
        self.htf_dir = htf_dir
        # VWAP diario y apertura del día (UTC)
        self.vwap = [0.0] * self.n
        self.day_open = [0.0] * self.n
        cur, sv, spv, do = None, 0.0, 0.0, 0.0
        for i, b in enumerate(bars):
            d = b.t // DAY_MS
            if d != cur:
                cur, sv, spv, do = d, 0.0, 0.0, b.o
            sv += b.v
            spv += (b.h + b.l + b.c) / 3 * b.v
            self.vwap[i] = spv / sv if sv > 0 else b.c
            self.day_open[i] = do
        self.btc_open, self.btc_close = btc_open, btc_close

    def day_pct(self, i):
        return (self.bars[i].c - self.day_open[i]) / self.day_open[i] * 100

    def btc_pct(self):
        return (self.btc_close - self.btc_open) / self.btc_open * 100 if self.btc_open else 0.0


def signal(F: Features, i=None, p=P):
    """Señal en la vela cerrada i (por defecto la última). Devuelve dict o None.
    También devuelve el estado de cada condición (para diagnósticos)."""
    if i is None:
        i = F.n - 1
    if i < max(p["K"], 30):
        return None
    b = F.bars[i]
    cp = F.day_pct(i)
    rs = cp - F.btc_pct()
    top = cp >= p["M"] and rs >= p["RS"]
    bot = cp <= -p["M"] and rs <= -p["RS"]
    win = F.rsi[i - p["K"] + 1: i + 1]
    rmin, rmax = min(win), max(win)
    r, r1 = F.rsi[i], F.rsi[i - 1]
    up_x = r > p["mid"] and r1 <= p["mid"]
    dn_x = r < p["mid"] and r1 >= p["mid"]
    st5 = F.st_dir[i]
    vw = F.vwap[i]
    cond = dict(day_pct=cp, rs=rs, top=top, bot=bot, rsi=r, rmin=rmin, rmax=rmax, st5=st5, htf=F.htf_dir, vwap=vw)
    L = top and rmin <= p["pull"] and up_x and st5 == 1 and (not p["useHtf"] or F.htf_dir == 1) and (not p["useVwap"] or b.c > vw)
    S = bot and rmax >= 100 - p["pull"] and dn_x and st5 == -1 and (not p["useHtf"] or F.htf_dir == -1) and (not p["useVwap"] or b.c < vw)
    if not (L or S):
        return dict(side=0, cond=cond)
    side = 1 if L else -1
    a = F.atr[i]
    if p["exitMode"] == "ST":
        line = F.st[i]
        stop = line if side * (b.c - line) > 0 else b.c - side * p["fallbackAtr"] * a
        tp = None
    else:
        stop = b.c - side * p["atrSL"] * a
        tp = b.c + side * p["atrTP"] * a
    return dict(side=side, stop=stop, tp=tp, atr=a, close=b.c, cond=cond)


def exit_reason(pos):
    s = pos["side"]
    if pos.get("trail") and s * (pos["stop"] - pos["entry"]) > 0:
        return "TRAIL"
    return "BE" if pos.get("be") and s * (pos["stop"] - pos["entry"]) >= 0 else "SL"


def manage(pos, F: Features, i=None, p=P):
    """Gestión al cierre de la vela i para una posición abierta.
    pos: dict(side, entry, stop, r, ext, be, t_bar). Actualiza pos y devuelve
    (exit_px, motivo) si hay que salir, o (None, None)."""
    if i is None:
        i = F.n - 1
    b = F.bars[i]
    s = pos["side"]
    # ¿stop tocado dentro de la vela?
    if (s == 1 and b.l <= pos["stop"]) or (s == -1 and b.h >= pos["stop"]):
        return (min(b.o, pos["stop"]) if s == 1 else max(b.o, pos["stop"])), exit_reason(pos)
    if pos.get("tp") and ((s == 1 and b.h >= pos["tp"]) or (s == -1 and b.l <= pos["tp"])):
        return pos["tp"], "TP"
    pos["ext"] = max(pos["ext"], b.h) if s == 1 else min(pos["ext"], b.l)
    reached = s * (pos["ext"] - pos["entry"]) >= pos["r"]
    if reached and not pos["be"] and s * (pos["entry"] - pos["stop"]) > 0:
        pos["be"] = True
        pos["stop"] = pos["entry"]
    if p["exitMode"] == "ST":
        if F.st_dir[i] == s:
            if s * (F.st[i] - pos["stop"]) > 0:
                pos["stop"] = F.st[i]
                pos["trail"] = True
        else:
            return b.c, "GIRO ST"
    elif reached:
        ns = pos["ext"] - s * p["atrTrail"] * F.atr[i]
        if s * (ns - pos["stop"]) > 0:
            pos["stop"] = ns
            pos["trail"] = True
    if p["maxBars"] > 0 and pos.get("bars", 0) >= p["maxBars"]:
        return b.c, "TIEMPO"
    return None, None


def htf_closed_dir(bars4h, now_ms, p=P, tf_ms=4 * 3_600_000):
    """Dirección SuperTrend de la última vela 4h CERRADA (equivale a [1] + lookahead_on)."""
    closed = [b for b in bars4h if b.t + tf_ms <= now_ms]
    if len(closed) < p["stLen"] + 5:
        return 0
    _, d = supertrend(closed, p["stMult"], p["stLen"])
    return d[-1]
