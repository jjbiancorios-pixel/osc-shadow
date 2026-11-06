"""eth_cartera.py — seguimiento VIRTUAL (sin dinero, sin órdenes) del reparto ETH/USDT del Bot BingX.
Compara dos carteras que parten del saldo real del Bot BingX al cierre del 5/11/2026:
  FIJA  : 50/50 sin tocar (como hoy).
  REGLA : 75% ETH / 25% USDT con ETH por encima de su EMA200 (4h) y 25/75 por debajo, con banda de +-2% y mínimo 7 días entre cambios.
Los ciclos son los de la config BX (oscilación fix22 en ETH, 20x, 2,5% de margen por entrada en cada billetera; largos en Coin-M y USDT-M, cortos solo USDT-M).
Todo error se registra y NO afecta al resto del Shadow."""
import json
from datetime import datetime, timezone
import numpy as np, pandas as pd
import config, db, data, engine, tg

L = 20; M = 0.025; HI = 0.75; LO = 0.25; HIST = 0.02; MIN_DIAS = 7; COSTO = 0.001
INICIO_ETH = 0.23923560; INICIO_USDT = 614.4429     # saldos reales del Bot BingX al cierre del 5/11/2026
KEY = "eth_cartera"

def _ema_regimen(df):
    c4 = df['c'][df.index.hour % 4 == 3]                  # cierre de cada vela de 4h completa
    ema = c4.ewm(span=200, adjust=False).mean()
    return float(df['c'].iloc[-1]), float(ema.iloc[-1]), len(c4)

def _aplicar_ciclo(st, c):
    ent = json.loads(c['entradas']); n = len(ent); sd = c['side']; liq = (c['res'] == 'liq'); ex = c['salida']; fee = config.BASE['fee']
    if liq or not ex: pn = -100.0; pi = -100.0
    else:
        pn = float(engine.pnl_puntos(ent, ex, sd, L, fee))
        pi = float(L * np.mean([1 - p / ex for p in ent]) * 100 - fee * 200 * L)
    for s in ('f', 'r'):
        st['U_' + s] += st['U_' + s] * M * n * pn / 100
        if sd == 1: st['E_' + s] += st['E_' + s] * M * n * pi / 100
    st['n_ciclos'] += 1; st['n_sl'] += 1 if c['res'] in ('sl', 'liq') else 0

def _objetivo(modo): return HI if modo == 'hi' else LO

def actualizar():
    df, _ = data.velas_cerradas("ETH", 1400)
    p, ema, n4 = _ema_regimen(df)
    if n4 < 220: raise ValueError(f"pocas velas de 4h para la EMA200 ({n4})")
    raw = db.meta_get(KEY); ahora = datetime.now(timezone.utc)
    r = p / ema - 1
    if raw: st = json.loads(raw)
    else:
        cs = db.ciclos("BX")
        st = dict(inicio=str(ahora), p0=p, E_f=INICIO_ETH, U_f=INICIO_USDT, E_r=INICIO_ETH, U_r=INICIO_USDT, modo=None, ult_cambio=None,
                  ult_id=max([c['id'] for c in cs], default=0), n_ciclos=0, n_sl=0, cambios=0)
        st['eq0_usd'] = INICIO_ETH * p + INICIO_USDT; st['eq0_eth'] = INICIO_ETH + INICIO_USDT / p
    # 1) ciclos nuevos de BX
    for c in db.ciclos("BX"):
        if c['id'] > st['ult_id']: _aplicar_ciclo(st, c); st['ult_id'] = c['id']
    # 2) régimen con histéresis y mínimo de días entre cambios
    modo = st['modo']; nuevo = modo
    if modo is None: nuevo = 'hi' if r > 0 else 'lo'
    elif modo == 'lo' and r > HIST: nuevo = 'hi'
    elif modo == 'hi' and r < -HIST: nuevo = 'lo'
    if nuevo != modo:
        dias = 999 if not st['ult_cambio'] else (ahora - datetime.fromisoformat(st['ult_cambio'])).days
        if modo is None or dias >= MIN_DIAS:
            tot = st['E_r'] * p + st['U_r']; tg_ = _objetivo(nuevo); tot -= abs(st['E_r'] * p - tot * tg_) * COSTO
            st['E_r'] = tot * tg_ / p; st['U_r'] = tot * (1 - tg_); st['modo'] = nuevo; st['ult_cambio'] = str(ahora); st['cambios'] += 1
            txt = (f"🔄 <b>ETH vs EMA200 (4h)</b>: {'+' if r>=0 else ''}{r*100:.1f}% → régimen {'ALCISTA' if nuevo=='hi' else 'BAJISTA'}. "
                   f"Reparto de la regla: {int(tg_*100)}% ETH / {int((1-tg_)*100)}% USDT. (Seguimiento virtual; el Bot BingX real no se toca.)")
            tg.enviar(txt)
    db.meta_set(KEY, json.dumps(st))
    return st, p, ema

def texto():
    raw = db.meta_get(KEY)
    if not raw: return "ETH cartera: todavía sin datos (se inicia en la próxima pasada)."
    st = json.loads(raw)
    try:
        df, _ = data.velas_cerradas("ETH", 1400); p, ema, _ = _ema_regimen(df)
    except Exception: p = st['p0']; ema = None
    def linea(nom, E, U):
        usd = E * p + U; eth = E + U / p
        return (f"{nom}: {E:.4f} ETH + {U:.1f} USDT = {usd:,.0f} USD ({(usd/st['eq0_usd']-1)*100:+.1f}%) · en ETH {(eth/st['eq0_eth']-1)*100:+.1f}% "
                f"(ETH-equiv {eth:.4f})")
    l = [f"💼 <b>ETH cartera (virtual)</b> desde {st['inicio'][:10]} · ETH {p:,.0f}" + (f" · EMA200 4h {ema:,.0f} ({(p/ema-1)*100:+.1f}%)" if ema else ""),
         f"Régimen: {'ALCISTA (75/25)' if st['modo']=='hi' else 'BAJISTA (25/75)'} · cambios {st['cambios']} · ciclos BX contados {st['n_ciclos']} · SL {st['n_sl']}",
         linea("FIJA 50/50", st['E_f'], st['U_f']), linea("REGLA     ", st['E_r'], st['U_r'])]
    return "\n".join(l)


# ───────────── BTC (bot nuevo en Binance, solo seguimiento virtual) ─────────────
# BT1 = oscilación fix22 en BTC; BT2 = BT1 + trailing 1xATR. Cartera virtual 50% BTC (COIN-M) / 50% USDT (USDT-M), 20x, 2,5% por entrada en cada billetera.
BTC_VAR = ("BT1", "BT2")

def _aplicar_ciclo_btc(st, c):
    ent = json.loads(c['entradas']); n = len(ent); sd = c['side']; liq = (c['res'] == 'liq'); ex = c['salida']; fee = config.BASE['fee']
    if liq or not ex: pn = -100.0; pi = -100.0
    else:
        pn = float(engine.pnl_puntos(ent, ex, sd, L, fee)); pi = float(L * np.mean([1 - p / ex for p in ent]) * 100 - fee * 200 * L)
    st['U'] += st['U'] * M * n * pn / 100
    if sd == 1: st['E'] += st['E'] * M * n * pi / 100
    st['n_ciclos'] += 1; st['n_sl'] += 1 if c['res'] in ('sl', 'liq') else 0

def actualizar_btc():
    df, _ = data.velas_cerradas("BTC", 1400); p = float(df['c'].iloc[-1]); ahora = datetime.now(timezone.utc)
    for k in BTC_VAR:
        key = "btc_cartera_" + k; raw = db.meta_get(key); cs = db.ciclos(k)
        if raw: st = json.loads(raw)
        else:
            st = dict(inicio=str(ahora), p0=p, E=500.0 / p, U=500.0, ult_id=max([c['id'] for c in cs], default=0), n_ciclos=0, n_sl=0)
            st['eq0_usd'] = 1000.0; st['eq0_btc'] = st['E'] + st['U'] / p
        for c in cs:
            if c['id'] > st['ult_id']: _aplicar_ciclo_btc(st, c); st['ult_id'] = c['id']
        db.meta_set(key, json.dumps(st))

def texto_btc():
    try: df, _ = data.velas_cerradas("BTC", 1400); p = float(df['c'].iloc[-1])
    except Exception: p = None
    l = ["₿ <b>BTC cartera (virtual, bot nuevo en Binance)</b> — 50% BTC / 50% USDT, 20x, 2,5% por entrada"]
    for k, nom in (("BT1", "BT1 oscilación fix22"), ("BT2", "BT2 fix22 + trailing 1xATR")):
        raw = db.meta_get("btc_cartera_" + k)
        if not raw: l.append(f"{nom}: sin datos todavía"); continue
        st = json.loads(raw); pp = p or st['p0']; usd = st['E'] * pp + st['U']; btc = st['E'] + st['U'] / pp
        l.append(f"{nom}: {st['E']:.5f} BTC + {st['U']:.1f} USDT = {usd:,.0f} USD ({(usd/st['eq0_usd']-1)*100:+.1f}%) · en BTC {(btc/st['eq0_btc']-1)*100:+.1f}% · ciclos {st['n_ciclos']} · SL {st['n_sl']} (desde {st['inicio'][:10]})")
    return "\n".join(l)
