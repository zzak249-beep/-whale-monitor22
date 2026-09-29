"""
Motor KIBITO TREND 4H — igual que el script de TradingView "KIBITO TREND 4H" y que el backtest
(10 monedas, 2021-2026, 542 ops, PF 2,87, 10/10 monedas en positivo).

LARGO (solo largos: los cortos no tuvieron ventaja, PF 0,96):
  1. EMA20 > EMA100 en 4h
  2. Cierre > máximo de las 20 velas 4h anteriores
  3. BTC con EMA20 > EMA100 en 4h
Stop inicial 3 ATR(20) (en el exchange). Salida al cierre de la vela 4h en que EMA20 < EMA100.
"""
from def_core import atr, Bar  # noqa: F401  (Bar se reexporta para los tests)

TP = dict(fast=20, slow=100, don=20, atrLen=20, stopAtr=3.0, minBars=300)
H4_MS = 4 * 3_600_000


def ema(xs, n):
    out, e, k = [], None, 2 / (n + 1)
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def closed(bars, now_ms):
    return [b for b in bars if b.t + H4_MS <= now_ms]


def trend_up(bars, p=TP):
    c = [b.c for b in bars]
    return ema(c, p["fast"])[-1] > ema(c, p["slow"])[-1]


def entry_signal(bars, btc_up, p=TP):
    """bars: velas 4h CERRADAS. Devuelve dict(stop, atr, close) si hay entrada en la última vela."""
    if len(bars) < p["minBars"] or not btc_up:
        return None
    if not trend_up(bars, p):
        return None
    prev_hi = max(b.h for b in bars[-p["don"] - 1:-1])
    last = bars[-1]
    if last.c <= prev_hi:
        return None
    a = atr(bars, p["atrLen"])[-1]
    return dict(stop=last.c - p["stopAtr"] * a, atr=a, close=last.c, brk=prev_hi)


def status(bars, btc_up, p=TP):
    """Para el informe: tendencia, distancia a la ruptura."""
    if len(bars) < p["minBars"]:
        return None
    up = trend_up(bars, p)
    prev_hi = max(b.h for b in bars[-p["don"]:])
    return dict(up=up, btc_up=btc_up, brk=prev_hi, dist=(prev_hi - bars[-1].c) / bars[-1].c * 100)
