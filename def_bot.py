"""
KIBITO BOT — dos sistemas probados y poco correlacionados (correlación mensual 0,13):

  DEF   (5m) : día extremo + retroceso RSI + SuperTrend. Escanea TODOS los perpetuos USDT
               cada 5 min. Motor def_core = script "KIBITO DEFINITIVO".
  TREND (4h) : seguidor de tendencia solo largos (EMA20/100 + ruptura 20 velas + BTC a favor).
               Revisa TODAS las monedas USDT de BingX con volumen >= UNIVERSE_MIN_VOL_USDT
               (TREND_TOP > 0 limita a las N más líquidas). Motor trend_core = "KIBITO TREND 4H".
  REBOTE (1d): compra al cierre diario (00:00 UTC) de la moneda que cae > 10% en el día,
               stop 3 ATR(20), vende al cierre 2 días después. = estrategia de "KIBITO RADAR 1D".
               Mismo universo que TREND (todas las monedas líquidas).

Una moneda solo puede estar en UN sistema a la vez (compatible con modo one-way de BingX).

MODE=SIGNAL -> papel: registra entradas/salidas y avisa por Telegram (por defecto)
MODE=LIVE   -> órdenes reales (requiere también CONFIRM_LIVE=YES)

- SL en el exchange (STOP_MARKET closePosition) nada más entrar; el bot lo mueve a BE y
  lo va subiendo con la línea SuperTrend al cierre de cada vela de 5m.
- Estado persistente en STATE_FILE (volumen de Railway en /data).
"""
import os
import time
import json
import hmac
import math
import hashlib
import logging

import requests

import def_core as D
import trend_core as T

CODE_VERSION = "kibito-bot 3.1.0"


# ───────────────────────── config ─────────────────────────
def env(name, default, cast=str):
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip().strip('"').strip("'").strip()
    if raw == "":
        return default
    if cast is bool:
        return raw.lower() in ("1", "true", "yes", "si", "on")
    return cast(raw)


MODE          = env("MODE", "SIGNAL").upper()
CONFIRM_LIVE  = env("CONFIRM_LIVE", "NO").upper()
LIVE          = MODE == "LIVE" and CONFIRM_LIVE == "YES"
API_KEY       = env("BINGX_API_KEY", "")
API_SECRET    = env("BINGX_API_SECRET", "")
BASE_URL      = env("BINGX_BASE_URL", "https://open-api.bingx.com")
TG_TOKEN      = env("TELEGRAM_TOKEN", "")
TG_CHAT       = env("TELEGRAM_CHAT_ID", "")
MIN_VOL       = env("MIN_VOL_USDT", 2_000_000, float)
MAX_SCAN      = env("MAX_SCAN", 0, int)                  # 0 = todas las candidatas
EXCLUDE       = {s.strip().upper() for s in env("EXCLUDE", "BTC,USDC").split(",") if s.strip()}
ALLOW_LONG    = env("ALLOW_LONG", True, bool)
ALLOW_SHORT   = env("ALLOW_SHORT", True, bool)
RISK_PCT      = env("RISK_PCT", 0.5, float)
MAX_LEV       = env("MAX_NOTIONAL_X", 2.0, float)
LEVERAGE      = env("LEVERAGE", 5, int)
MAX_POS       = env("MAX_POSITIONS", 3, int)
MAX_DD_DAY    = env("MAX_DD_DAY_PCT", 3.0, float)
PAPER_EQUITY  = env("PAPER_EQUITY", 10_000, float)
FEE           = env("FEE_PCT", 0.07, float) / 100        # por lado (comisión + deslizamiento)
STATE_FILE    = env("STATE_FILE", "/data/def_state.json")
REQ_PAUSE_S   = env("REQ_PAUSE_S", 0.3, float)
LOOP_DELAY_S  = env("LOOP_DELAY_S", 8, int)
NOTIFY_EXT    = env("NOTIFY_EXTREME", True, bool)
DEF_ON        = env("DEF_ENABLED", True, bool)
TREND_ON      = env("TREND_ENABLED", True, bool)
TREND_TOP     = env("TREND_TOP", 0, int)                 # 0 = TODAS las monedas de BingX
UNI_MIN_VOL   = env("UNIVERSE_MIN_VOL_USDT", 2_000_000, float)   # liquidez mínima TREND/REBOTE
TREND_NEAR_HI = env("TREND_NEAR_HIGH_PCT", 15.0, float)  # prefiltro: precio a menos de x% del máximo 24h
TREND_BATCH   = env("TREND_BATCH", 150, int)             # monedas TREND por ciclo de 5 min
TREND_SYMBOLS = [s.strip().upper() for s in env("TREND_SYMBOLS", "").split(",") if s.strip()]  # vacío = AUTO
TREND_EXCL    = {s.strip().upper() for s in env("TREND_EXCLUDE", "USDC").split(",") if s.strip()}
TREND_RISK    = env("TREND_RISK_PCT", 0.5, float)
TREND_MAX_POS = env("TREND_MAX_POSITIONS", 6, int)
TREND_MAX_LEV = env("TREND_MAX_NOTIONAL_X", 1.0, float)
TREND_BTC     = env("TREND_BTC_FILTER", True, bool)
REB_ON        = env("REBOTE_ENABLED", True, bool)
REB_DROP      = env("REBOTE_DROP_PCT", 10.0, float)
REB_HOLD      = env("REBOTE_HOLD_DAYS", 2, int)
REB_STOP_ATR  = env("REBOTE_STOP_ATR", 3.0, float)
REB_RISK      = env("REBOTE_RISK_PCT", 0.5, float)
REB_MAX_POS   = env("REBOTE_MAX_POSITIONS", 3, int)
REB_MAX_LEV   = env("REBOTE_MAX_NOTIONAL_X", 1.0, float)
D1_MS         = 86_400_000

P = dict(D.P)
P["M"] = env("DAY_MOVE_PCT", 6.0, float)
P["RS"] = env("RS_MIN", 4.0, float)
P["exitMode"] = "ATR" if env("EXIT_MODE", "ST").upper() == "ATR" else "ST"

BAR_MS = 300_000
H4_MS = 4 * 3_600_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("def-bot")
S = requests.Session()
S.headers.update({"X-SOURCE-KEY": "BX-AI-SKILL"})


# ───────────────────────── BingX ─────────────────────────
class BingXError(Exception):
    pass


def _check(js):
    if str(js.get("code", 0)) != "0":
        raise BingXError(f"{js.get('code')}: {js.get('msg')}")
    return js.get("data")


def public(path, params=None, retries=3):
    for k in range(retries):
        try:
            r = S.get(BASE_URL + path, params=params, timeout=15)
            r.raise_for_status()
            return _check(r.json())
        except Exception as e:
            log.warning("GET %s %s (%d): %s", path, params, k + 1, e)
            time.sleep(2 + 2 * k)
    return None


def signed(method, path, params=None):
    """Firma BingX: parámetros ordenados, HMAC-SHA256 hex; la MISMA cadena firmada se envía."""
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    for k, v in params.items():
        if any(ch in str(v) for ch in "&=?#\r\n"):
            raise BingXError(f"valor no permitido en {k}")
    canonical = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sig = hmac.new(API_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    payload = f"{canonical}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    url = BASE_URL + path
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        r = S.post(url, data=payload, headers=headers, timeout=15)
    elif method == "DELETE":
        r = S.delete(f"{url}?{payload}", headers=headers, timeout=15)
    else:
        r = S.get(f"{url}?{payload}", headers=headers, timeout=15)
    return _check(r.json())


CONTRACTS = {}


def load_contracts():
    data = public("/openApi/swap/v2/quote/contracts") or []
    for c in data:
        CONTRACTS[c["symbol"]] = dict(
            qp=int(c.get("quantityPrecision", 3)), pp=int(c.get("pricePrecision", 4)),
            minq=float(c.get("tradeMinQuantity") or 0), minusdt=float(c.get("tradeMinUSDT") or 0))
    log.info("contratos cargados: %d", len(CONTRACTS))


def rnd_qty(sym, q):
    f = 10 ** CONTRACTS.get(sym, {}).get("qp", 3)
    return math.floor(q * f) / f


def qty_str(sym, q):
    return f"{q:.{CONTRACTS.get(sym, {}).get('qp', 3)}f}"


def rnd_px(sym, x):
    return round(x, CONTRACTS.get(sym, {}).get("pp", 6))


def klines(sym, interval, limit):
    data = public("/openApi/swap/v3/quote/klines", {"symbol": sym, "interval": interval, "limit": limit}) or []
    time.sleep(REQ_PAUSE_S)
    return sorted((D.parse_kline(k) for k in data), key=lambda b: b.t)


def bars5(sym, limit=600):
    now_ms = int(time.time() * 1000)
    return [b for b in klines(sym, "5m", limit) if b.t + BAR_MS <= now_ms]


def last_price(sym):
    d = public("/openApi/swap/v2/quote/price", {"symbol": sym})
    try:
        return float(d["price"] if isinstance(d, dict) else d[0]["price"])
    except Exception:
        return None


HEDGE = None


def detect_mode():
    global HEDGE
    d = signed("GET", "/openApi/swap/v1/positionSide/dual")
    v = d.get("dualSidePosition") if isinstance(d, dict) else d
    HEDGE = str(v).lower() == "true"
    log.info("modo de posición: %s", "HEDGE" if HEDGE else "ONE-WAY")


def live_equity():
    d = signed("GET", "/openApi/swap/v3/user/balance")
    for a in d if isinstance(d, list) else [d]:
        if a.get("asset") == "USDT":
            return float(a.get("equity") or a.get("balance"))
    raise BingXError("sin saldo USDT")


def live_position(sym, side):
    d = signed("GET", "/openApi/swap/v2/user/positions", {"symbol": sym}) or []
    want = "LONG" if side == 1 else "SHORT"
    for p in d:
        amt = float(p.get("positionAmt") or 0)
        ps = p.get("positionSide", "BOTH")
        if amt == 0:
            continue
        if ps == want or (ps == "BOTH" and (amt > 0) == (side == 1)):
            return abs(amt), float(p.get("avgPrice") or 0)
    return 0.0, 0.0


def pos_side(side):
    return ("LONG" if side == 1 else "SHORT") if HEDGE else "BOTH"


def set_leverage(sym):
    for s in (["LONG", "SHORT"] if HEDGE else ["BOTH"]):
        try:
            signed("POST", "/openApi/swap/v2/trade/leverage", {"symbol": sym, "side": s, "leverage": LEVERAGE})
        except Exception as e:
            log.warning("leverage %s %s: %s", sym, s, e)


def place_sl(sym, side, amt, stop):
    sl = {"symbol": sym, "side": "SELL" if side == 1 else "BUY", "positionSide": pos_side(side),
          "type": "STOP_MARKET", "stopPrice": rnd_px(sym, stop), "quantity": qty_str(sym, amt),
          "closePosition": "true", "workingType": "MARK_PRICE"}
    signed("POST", "/openApi/swap/v2/trade/order", sl)


def cancel_all(sym):
    try:
        signed("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": sym})
    except Exception as e:
        log.warning("cancelar órdenes %s: %s", sym, e)


def live_open(sym, side, qty, stop):
    set_leverage(sym)
    o = {"symbol": sym, "side": "BUY" if side == 1 else "SELL", "positionSide": pos_side(side),
         "type": "MARKET", "quantity": qty_str(sym, qty)}
    signed("POST", "/openApi/swap/v2/trade/order", o)
    time.sleep(1.0)
    amt, avg = live_position(sym, side)
    if amt <= 0:
        raise BingXError("la posición no aparece tras la orden")
    try:
        place_sl(sym, side, amt, stop)
    except Exception as e:
        log.error("SL falló en %s: %s -> cierro la posición por seguridad", sym, e)
        live_close(sym, side)
        raise
    return amt, avg


def live_move_sl(sym, side, stop):
    amt, _ = live_position(sym, side)
    if amt <= 0:
        return False
    cancel_all(sym)
    try:
        place_sl(sym, side, amt, stop)
    except Exception as e:
        log.error("no se pudo mover el SL de %s (%s) -> cierro por seguridad", sym, e)
        live_close(sym, side)
        return False
    return True


def live_close(sym, side):
    amt, _ = live_position(sym, side)
    if amt > 0:
        o = {"symbol": sym, "side": "SELL" if side == 1 else "BUY", "positionSide": pos_side(side),
             "type": "MARKET", "quantity": qty_str(sym, amt)}
        if not HEDGE:
            o["reduceOnly"] = "true"
        signed("POST", "/openApi/swap/v2/trade/order", o)
    cancel_all(sym)


# ───────────────────────── Telegram ─────────────────────────
def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        log.info("[TG] %s", text.replace("\n", " | "))
        return
    try:
        S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
               data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                     "disable_web_page_preview": "true"}, timeout=15)
    except Exception as e:
        log.warning("telegram: %s", e)


# ───────────────────────── estado ─────────────────────────
def new_state():
    return dict(version=CODE_VERSION, equity=PAPER_EQUITY, positions={}, sym={}, trades=[],
                day=None, day_start_eq=None, day_block=False, notified={},
                trend_last_bar=0, trend_q=[], trend_btc_up=True, rebote_last_day=0)


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        for k, v in new_state().items():
            st.setdefault(k, v)
        # migración v1 -> v2: claves de posición con prefijo de sistema
        for k in list(st["positions"]):
            if ":" not in k:
                p = st["positions"].pop(k)
                p.setdefault("sym", k)
                p.setdefault("sys", "DEF")
                st["positions"]["DEF:" + k] = p
        for t in st["trades"]:
            t.setdefault("sys", "DEF")
        log.info("estado cargado: %d posiciones, %d trades", len(st["positions"]), len(st["trades"]))
        return st
    except FileNotFoundError:
        return new_state()
    except Exception as e:
        log.error("estado corrupto (%s); empiezo de cero", e)
        return new_state()


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error("no se pudo guardar el estado: %s", e)


def symst(st, sym):
    return st["sym"].setdefault(sym, dict(last_exit=0, trades_day=0, streak=0, pause_until=0, day=None))


def fmt(x):
    return f"{x:.6g}"


def busy(st, sym):
    """¿La moneda ya está en algún sistema?"""
    return any(p["sym"] == sym for p in st["positions"].values())


def count(st, sys_):
    return sum(1 for p in st["positions"].values() if p["sys"] == sys_)


# ───────────────────────── cierre común ─────────────────────────
def record_close(st, key, exit_px, reason, bar_t):
    pos = st["positions"][key]
    sym, sys_, side = pos["sym"], pos["sys"], pos["side"]
    pnl = (exit_px - pos["entry"]) * pos["qty"] * side - (pos["entry"] + exit_px) * pos["qty"] * FEE
    if not LIVE:
        st["equity"] += pnl
    if sys_ == "DEF":
        ss = symst(st, sym)
        ss["last_exit"] = bar_t
        if pnl > 0:
            ss["streak"] = 0
        else:
            ss["streak"] += 1
            if ss["streak"] >= P["lossStreak"]:
                ss["pause_until"] = bar_t + P["pauseBars"] * BAR_MS
                ss["streak"] = 0
                tg(f"⏸ <b>{sym}</b> (DEF) en pausa 12 h tras {P['lossStreak']} pérdidas seguidas")
    r_mult = pnl / pos["risk_usdt"] if pos.get("risk_usdt") else 0
    st["trades"].append(dict(sys=sys_, sym=sym, side=side, entry=pos["entry"], exit=exit_px, qty=pos["qty"],
                             pnl=pnl, r=r_mult, reason=reason, t_in=pos["t"], t_out=bar_t))
    st["trades"] = st["trades"][-3000:]
    del st["positions"][key]
    tg(f"{'✅' if pnl > 0 else '❌'} <b>[{sys_}] {'LONG' if side == 1 else 'SHORT'} {sym}</b> cerrado ({reason})\n"
       f"entrada {fmt(pos['entry'])} → salida {fmt(exit_px)}\n"
       f"PnL {pnl:+.2f} USDT ({r_mult:+.2f}R) · {'REAL' if LIVE else 'PAPEL'}")


def open_position(st, sys_, sym, side, stop, risk_pct, max_lev, t_next, extra, msg, ref_px=None):
    """Abre (real o papel) y registra. Devuelve True si abrió."""
    px = last_price(sym) or ref_px
    if px is None:
        return False
    if side * (px - stop) <= 0:
        log.info("%s precio ya más allá del stop, se descarta", sym)
        return False
    dist = abs(px - stop)
    equity = live_equity() if LIVE else st["equity"]
    qty = rnd_qty(sym, min(equity * risk_pct / 100 / dist, equity * max_lev / px))
    c = CONTRACTS.get(sym, {})
    if qty <= 0 or qty < c.get("minq", 0) or qty * px < max(c.get("minusdt", 0), 2):
        log.info("%s tamaño demasiado pequeño (%s)", sym, qty)
        return False
    entry = px
    if LIVE:
        try:
            qty, avg = live_open(sym, side, qty, stop)
            entry = avg or px
        except Exception as e:
            tg(f"⚠️ [{sys_}] No se pudo abrir {'LONG' if side == 1 else 'SHORT'} {sym}: {e}")
            return False
    r = abs(entry - stop)
    pos = dict(sys=sys_, sym=sym, side=side, entry=entry, stop=stop, r=r, qty=qty, t=t_next,
               risk_usdt=r * qty)
    pos.update(extra)
    st["positions"][f"{sys_}:{sym}"] = pos
    tg(msg.format(entry=fmt(entry), stop=fmt(stop), pct=r / entry * 100, risk=r * qty,
                  mode="REAL" if LIVE else "PAPEL"))
    return True


# ───────────────────────── sistema DEF (5m) ─────────────────────────
def def_manage(st, key, bars, F):
    pos = st["positions"][key]
    sym, side = pos["sym"], pos["side"]
    if LIVE:
        amt, _ = live_position(sym, side)
        if amt <= 0:
            cancel_all(sym)
            record_close(st, key, last_price(sym) or pos["stop"],
                         D.exit_reason(pos), bars[-1].t)
            return
    old_stop = pos["stop"]
    for i, b in enumerate(bars):
        if b.t < pos["t"] or b.t <= pos.get("last_t", 0):
            continue
        pos["last_t"] = b.t
        pos["bars"] = pos.get("bars", 0) + 1
        exit_px, reason = D.manage(pos, F, i, P)
        if exit_px is not None:
            if LIVE:
                live_close(sym, side)
                exit_px = last_price(sym) or exit_px
            record_close(st, key, exit_px, reason, b.t)
            return
    if pos["stop"] != old_stop:
        if LIVE and not live_move_sl(sym, side, pos["stop"]):
            record_close(st, key, last_price(sym) or pos["stop"], "SL-ERROR", bars[-1].t)
            return
        log.info("DEF %s stop %s -> %s", sym, fmt(old_stop), fmt(pos["stop"]))


def def_entry(st, sym, bars, F, s):
    b = bars[-1]
    ss = symst(st, sym)
    day = b.t // D.DAY_MS
    if ss["day"] != day:
        ss.update(day=day, trades_day=0)
    side_txt = "LONG" if s["side"] == 1 else "SHORT"
    reasons = []
    if (s["side"] == 1 and not ALLOW_LONG) or (s["side"] == -1 and not ALLOW_SHORT):
        reasons.append("lado desactivado")
    if (b.t - ss["last_exit"]) // BAR_MS <= P["cooldown"]:
        reasons.append("cooldown")
    if b.t <= ss["pause_until"]:
        reasons.append("pausa")
    if ss["trades_day"] >= P["maxDay"]:
        reasons.append("máx diario")
    if st["day_block"]:
        reasons.append("corte diario global")
    if count(st, "DEF") >= MAX_POS:
        reasons.append("máx posiciones DEF")
    if busy(st, sym):
        reasons.append("moneda ya en TREND")
    if reasons:
        tg(f"⚪ [DEF] Señal {side_txt} {sym} descartada: {', '.join(reasons)}")
        return
    cd = s["cond"]
    msg = (f"{'🟢' if s['side'] == 1 else '🔴'} <b>[DEF] {side_txt} {sym}</b> · retroceso en día extremo\n"
           "entrada {entry} · SL {stop} ({pct:.2f}%) · "
           f"{'salida por línea SuperTrend' if P['exitMode'] == 'ST' else 'TP ' + fmt(s['tp'])}\n"
           f"día {cd['day_pct']:+.1f}% · RS {cd['rs']:+.1f} · RSI {cd['rsi']:.0f} · "
           "riesgo {risk:.2f} USDT · {mode}")
    ok = open_position(st, "DEF", sym, s["side"], s["stop"], RISK_PCT, MAX_LEV, b.t + BAR_MS,
                       dict(tp=s.get("tp"), ext=0.0, be=False, trail=False, last_t=b.t, bars=0), msg, b.c)
    if ok:
        p = st["positions"]["DEF:" + sym]
        p["ext"] = p["entry"]
        ss["trades_day"] += 1


def candidates():
    tick = public("/openApi/swap/v2/quote/ticker") or []
    out = []
    for t in tick if isinstance(tick, list) else [tick]:
        sym = str(t.get("symbol", ""))
        base = sym.split("-")[0]
        if not sym.endswith("-USDT") or base in EXCLUDE or (base.startswith("NC") and base.endswith("USD")):
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            hi, lo = float(t.get("highPrice") or 0), float(t.get("lowPrice") or 0)
        except ValueError:
            continue
        if qv >= MIN_VOL and lo > 0 and (hi - lo) / lo * 100 >= P["M"]:
            out.append((sym, qv))
    out.sort(key=lambda x: -x[1])
    if MAX_SCAN > 0:
        out = out[:MAX_SCAN]
    return [s for s, _ in out]


def btc_ref():
    bb = bars5("BTC-USDT", 300)
    if not bb:
        return None, None
    day = bb[-1].t // D.DAY_MS
    today = [b for b in bb if b.t // D.DAY_MS == day]
    return (today[0].o if today else bb[-1].o), bb[-1].c


def def_step(st, now_ms):
    btc_open, btc_close = btc_ref()
    if btc_open is None:
        log.warning("DEF: sin datos de BTC, salto")
        return
    cands = candidates()
    mine = [p["sym"] for p in st["positions"].values() if p["sys"] == "DEF"]
    n_ext = 0
    for sym in dict.fromkeys(mine + cands):
        try:
            bars = bars5(sym, 600)
            if len(bars) < 300:
                continue
            key = "DEF:" + sym
            in_pos = key in st["positions"]
            b = bars[-1]
            today = [x for x in bars if x.t // D.DAY_MS == b.t // D.DAY_MS]
            day_pct = (b.c - today[0].o) / today[0].o * 100 if today else 0.0
            if not in_pos and abs(day_pct) < P["M"]:
                continue
            hd = D.htf_closed_dir(klines(sym, "4h", 150), now_ms, P, H4_MS)
            F = D.Features(bars, hd, btc_open, btc_close, P)
            if in_pos:
                def_manage(st, key, bars, F)
                save_state(st)
                continue
            s = D.signal(F, None, P)
            if s is None:
                continue
            cd = s["cond"]
            if cd["top"] or cd["bot"]:
                n_ext += 1
                if NOTIFY_EXT and not st["notified"].get(sym):
                    st["notified"][sym] = True
                    tg(f"🔥 <b>{sym}</b> día extremo {cd['day_pct']:+.1f}% (RS {cd['rs']:+.1f}) · "
                       f"esperando retroceso RSI · ST 4h {'▲' if hd == 1 else '▼' if hd == -1 else '?'}")
            if s["side"] != 0:
                def_entry(st, sym, bars, F, s)
                save_state(st)
        except Exception as e:
            log.exception("DEF %s: %s", sym, e)
    log.info("DEF: %d candidatas · %d en día extremo · %d posiciones", len(cands), n_ext, count(st, "DEF"))


# ───────────────────────── sistema TREND (4h) ─────────────────────────
def universe_rows():
    """Todas las monedas USDT de BingX con liquidez suficiente (una sola consulta)."""
    tick = public("/openApi/swap/v2/quote/ticker") or []
    rows = []
    for t in tick if isinstance(tick, list) else [tick]:
        sym = str(t.get("symbol", ""))
        base = sym.split("-")[0]
        if not sym.endswith("-USDT") or base in TREND_EXCL or (base.startswith("NC") and base.endswith("USD")):
            continue
        if TREND_SYMBOLS and sym not in TREND_SYMBOLS:
            continue
        try:
            r = dict(sym=sym, qv=float(t.get("quoteVolume") or 0), last=float(t.get("lastPrice") or 0),
                     high=float(t.get("highPrice") or 0), open=float(t.get("openPrice") or 0))
        except ValueError:
            continue
        if r["last"] > 0 and (TREND_SYMBOLS or r["qv"] >= UNI_MIN_VOL):
            rows.append(r)
    rows.sort(key=lambda r: -r["qv"])
    if TREND_TOP > 0 and not TREND_SYMBOLS:
        rows = rows[:TREND_TOP]
    return rows


def trend_manage(st, key, bars):
    pos = st["positions"][key]
    sym = pos["sym"]
    exit_px, reason = None, None
    if LIVE:
        amt, _ = live_position(sym, 1)
        if amt <= 0:
            cancel_all(sym)
            exit_px, reason = last_price(sym) or pos["stop"], "SL"
    if exit_px is None:
        for b in bars:                             # stop tocado en alguna vela desde la última revisión
            if b.t >= pos["t"] and b.t > pos.get("last_t", 0) and b.l <= pos["stop"]:
                exit_px, reason = min(b.o, pos["stop"]), "SL"
                break
    if exit_px is None and not T.trend_up(bars):
        exit_px, reason = bars[-1].c, "CRUCE EMA"
        if LIVE:
            live_close(sym, 1)
            exit_px = last_price(sym) or exit_px
    pos["last_t"] = bars[-1].t
    if exit_px is not None:
        record_close(st, key, exit_px, reason, bars[-1].t)


def trend_step(st, now_ms):
    # 1) ¿nueva vela 4h cerrada? -> preparar la cola de monedas a revisar
    btc4 = T.closed(klines("BTC-USDT", "4h", 400), now_ms)
    if len(btc4) < T.TP["minBars"]:
        log.warning("TREND: sin datos suficientes de BTC")
        return
    if st["trend_last_bar"] != btc4[-1].t:
        st["trend_last_bar"] = btc4[-1].t
        st["trend_btc_up"] = bool(T.trend_up(btc4)) if TREND_BTC else True
        rows = universe_rows()
        # prefiltro de entrada: para romper el máximo de 20 velas el precio tiene que estar cerca del máximo 24h
        near = [r["sym"] for r in rows if r["high"] > 0 and r["last"] >= r["high"] * (1 - TREND_NEAR_HI / 100)]
        mine = [p["sym"] for p in st["positions"].values() if p["sys"] == "TREND"]
        st["trend_q"] = list(dict.fromkeys(mine + (near if st["trend_btc_up"] else [])))
        log.info("TREND vela nueva · BTC %s · %d monedas líquidas · %d a revisar", "▲" if st["trend_btc_up"] else "▼",
                 len(rows), len(st["trend_q"]))
    # 2) procesar un lote de la cola (así un universo grande no bloquea el ciclo de 5 min)
    batch, st["trend_q"] = st["trend_q"][:TREND_BATCH], st["trend_q"][TREND_BATCH:]
    btc_up = st["trend_btc_up"]
    for sym in batch:
        try:
            bars = T.closed(klines(sym, "4h", 400), now_ms)
            if len(bars) < T.TP["minBars"]:
                continue
            key = "TREND:" + sym
            if key in st["positions"]:
                trend_manage(st, key, bars)
                save_state(st)
                continue
            s = T.entry_signal(bars, btc_up)
            if s is None:
                continue
            reasons = []
            if count(st, "TREND") >= TREND_MAX_POS:
                reasons.append("máx posiciones TREND")
            if busy(st, sym):
                reasons.append("moneda ya en otro sistema")
            if st["day_block"]:
                reasons.append("corte diario global")
            if reasons:
                tg(f"⚪ [TREND] Señal LONG {sym} descartada: {', '.join(reasons)}")
                continue
            msg = ("🟢 <b>[TREND] LONG " + sym + "</b> · ruptura en tendencia 4h\n"
                   "entrada {entry} · SL {stop} ({pct:.1f}%) · salida: cruce EMA20/100\n"
                   "riesgo {risk:.2f} USDT · {mode}\n"
                   "Acierto esperado ~28%: dejar correr, no cerrar a mano")
            open_position(st, "TREND", sym, 1, s["stop"], TREND_RISK, TREND_MAX_LEV, bars[-1].t + T.H4_MS,
                          dict(last_t=bars[-1].t), msg, bars[-1].c)
            save_state(st)
        except Exception as e:
            log.exception("TREND %s: %s", sym, e)
    if batch:
        log.info("TREND lote %d · quedan %d · posiciones %d", len(batch), len(st["trend_q"]), count(st, "TREND"))


# ───────────────────────── sistema REBOTE (1d) ─────────────────────────
def closed_d1(bars, now_ms):
    return [b for b in bars if b.t + D1_MS <= now_ms]


def rebote_step(st, now_ms):
    btc = closed_d1(klines("BTC-USDT", "1d", 60), now_ms)
    if len(btc) < 25:
        return
    last_day = btc[-1].t
    if st["rebote_last_day"] == last_day:
        return                                   # este cierre diario ya se procesó
    st["rebote_last_day"] = last_day
    rows = universe_rows()
    vol = {r["sym"]: r["qv"] for r in rows}
    # prefiltro: variación 24h del ticker (a las 00:00 UTC coincide con la vela diaria)
    cands = [r["sym"] for r in rows if r["open"] > 0 and (r["last"] / r["open"] - 1) * 100 <= -(REB_DROP - 3)]
    universe = cands
    mine = [p["sym"] for p in st["positions"].values() if p["sys"] == "REBOTE"]
    drops = []
    for sym in dict.fromkeys(mine + universe):
        try:
            bars = btc if sym == "BTC-USDT" else closed_d1(klines(sym, "1d", 60), now_ms)
            if len(bars) < 25 or bars[-1].t != last_day:
                continue
            key = "REBOTE:" + sym
            if key in st["positions"]:
                pos = st["positions"][key]
                exit_px, reason = None, None
                if LIVE:
                    amt, _ = live_position(sym, 1)
                    if amt <= 0:
                        cancel_all(sym)
                        exit_px, reason = last_price(sym) or pos["stop"], "SL"
                if exit_px is None:
                    after = [b for b in bars if b.t >= pos["t"]]
                    for b in after:
                        if b.l <= pos["stop"]:
                            exit_px, reason = min(b.o, pos["stop"]), "SL"
                            break
                    if exit_px is None and len(after) >= REB_HOLD:
                        exit_px, reason = bars[-1].c, f"FIN {REB_HOLD}d"
                        if LIVE:
                            live_close(sym, 1)
                            exit_px = last_price(sym) or exit_px
                if exit_px is not None:
                    record_close(st, key, exit_px, reason, bars[-1].t)
                save_state(st)
                if key in st["positions"]:
                    continue                      # sigue abierta
            ret = (bars[-1].c / bars[-2].c - 1) * 100
            if ret <= -REB_DROP:
                drops.append((ret, sym, bars))
        except Exception as e:
            log.exception("REBOTE %s: %s", sym, e)
    drops.sort(key=lambda x: -vol.get(x[1], 0))   # con plazas limitadas, primero las más líquidas
    for ret, sym, bars in drops:
        reasons = []
        if count(st, "REBOTE") >= REB_MAX_POS:
            reasons.append("máx posiciones REBOTE")
        if busy(st, sym):
            reasons.append("moneda ya en otro sistema")
        if st["day_block"]:
            reasons.append("corte diario global")
        if reasons:
            tg(f"⚪ [REBOTE] {sym} ({ret:+.1f}%) descartado: {', '.join(reasons)}")
            continue
        a = D.atr(bars, 20)[-1]
        stop = bars[-1].c - REB_STOP_ATR * a
        msg = ("🟢 <b>[REBOTE] LONG " + sym + f"</b> · desplome de {ret:+.1f}% ayer\n"
               "entrada {entry} · SL {stop} ({pct:.1f}%) · venta en " + str(REB_HOLD) + " días\n"
               "riesgo {risk:.2f} USDT · {mode}")
        open_position(st, "REBOTE", sym, 1, stop, REB_RISK, REB_MAX_LEV, bars[-1].t + D1_MS,
                      dict(drop=ret), msg, bars[-1].c)
        save_state(st)
    log.info("REBOTE día %s · %d desplomes · %d posiciones", last_day, len(drops), count(st, "REBOTE"))


# ───────────────────────── informe y bucle ─────────────────────────
def _summary(trs):
    gp = sum(t["pnl"] for t in trs if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trs if t["pnl"] <= 0)
    pf = gp / gl if gl > 0 else float("inf") if gp > 0 else 0
    wins = sum(1 for t in trs if t["pnl"] > 0)
    return (f"{len(trs)} trades · acierto {wins / len(trs) * 100 if trs else 0:.0f}% · "
            f"PF {'∞' if pf == float('inf') else f'{pf:.2f}'} · {sum(t['r'] for t in trs):+.1f}R · {gp - gl:+.2f} USDT")


def daily_report(st, day):
    t0 = day * D.DAY_MS
    tr = [t for t in st["trades"] if t0 - D.DAY_MS <= t["t_out"] < t0]
    lines = [f"📊 <b>Resumen KIBITO BOT</b> · {'REAL' if LIVE else 'PAPEL'}",
             f"Ayer: {len(tr)} trades · PnL {sum(t['pnl'] for t in tr):+.2f} USDT"]
    for sys_, ref in [("DEF", "ref. PF 1,47 · ~35%"), ("TREND", "ref. PF 2,87 · ~28%"), ("REBOTE", "ref. PF 1,99 · ~59%")]:
        trs = [t for t in st["trades"] if t.get("sys", "DEF") == sys_]
        opn = [p["sym"].replace("-USDT", "") for p in st["positions"].values() if p["sys"] == sys_]
        lines.append(f"<b>{sys_}</b>: {_summary(trs)} ({ref})")
        if opn:
            lines.append(f"   abiertas: {', '.join(opn)}")
    lines.append(f"Equity {'papel ' + format(st['equity'], '.2f') if not LIVE else 'ver BingX'}")
    tg("\n".join(lines))


def cycle(st):
    t0 = time.time()
    now_ms = int(t0 * 1000)
    day = now_ms // D.DAY_MS
    if st["day"] != day:
        if st["day"] is not None:
            daily_report(st, day)
        eq = live_equity() if LIVE else st["equity"]
        st.update(day=day, day_start_eq=eq, day_block=False, notified={})
    if REB_ON:
        try:
            rebote_step(st, now_ms)
        except Exception as e:
            log.exception("REBOTE: %s", e)
    if TREND_ON:
        try:
            trend_step(st, now_ms)
        except Exception as e:
            log.exception("TREND: %s", e)
    if DEF_ON:
        try:
            def_step(st, now_ms)
        except Exception as e:
            log.exception("DEF: %s", e)
    eq = live_equity() if LIVE else st["equity"]
    if st["day_start_eq"] and (st["day_start_eq"] - eq) / st["day_start_eq"] * 100 >= MAX_DD_DAY and not st["day_block"]:
        st["day_block"] = True
        tg(f"🛑 Corte diario: pérdida ≥ {MAX_DD_DAY}% · sin nuevas entradas hasta mañana (UTC)")
    save_state(st)
    dt_s = time.time() - t0
    log.info("ciclo %.0fs · posiciones %d", dt_s, len(st["positions"]))
    if dt_s > 240:
        log.warning("el ciclo tarda %.0fs: sube MIN_VOL_USDT o pon MAX_SCAN", dt_s)


def main():
    log.info("arrancando %s · modo %s · %s", CODE_VERSION, "REAL" if LIVE else "PAPEL", STATE_FILE)
    if MODE == "LIVE" and not LIVE:
        log.warning("MODE=LIVE sin CONFIRM_LIVE=YES -> funciono en PAPEL")
    load_contracts()
    if LIVE:
        if not API_KEY or not API_SECRET:
            raise SystemExit("faltan BINGX_API_KEY / BINGX_API_SECRET para modo REAL")
        detect_mode()
    st = load_state()
    tg(f"🤖 <b>{CODE_VERSION}</b> iniciado · {'🔴 REAL' if LIVE else '📝 PAPEL'}\n"
       f"DEF {'ON' if DEF_ON else 'OFF'}: día ≥ {P['M']}% · RS ≥ {P['RS']} · riesgo {RISK_PCT}% · máx {MAX_POS}\n"
       f"TREND {'ON' if TREND_ON else 'OFF'}: {'todas las monedas' if TREND_TOP == 0 else f'top {TREND_TOP}'} con vol ≥ {UNI_MIN_VOL / 1e6:.0f}M · riesgo {TREND_RISK}% · máx {TREND_MAX_POS} · "
       f"filtro BTC {'ON' if TREND_BTC else 'OFF'}\n"
       f"REBOTE {'ON' if REB_ON else 'OFF'}: caída ≥ {REB_DROP}% · {REB_HOLD} días · riesgo {REB_RISK}% · máx {REB_MAX_POS}\n"
       f"Corte diario {MAX_DD_DAY}%")
    while True:
        now = time.time()
        nxt = (math.floor(now / 300) + 1) * 300 + LOOP_DELAY_S
        time.sleep(max(1, nxt - now))
        try:
            cycle(st)
        except Exception as e:
            log.exception("ciclo: %s", e)
            tg(f"⚠️ Error en ciclo: {e}")


if __name__ == "__main__":
    main()
