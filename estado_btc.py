"""estado_btc.py — ESTADO de BTC (alcista / rango / bajista) para la config JR del Shadow. Aprobado por Juanjo el 22/11/2026.
Con el cierre diario de BTC (velas 1d cerradas):
  ALCISTA = cierre sobre su media exponencial de 50 días Y +5% o más en 30 días.
  BAJISTA = cierre bajo su media de 50 días Y −5% o más en 30 días.
  RANGO   = el resto.
El estado calculado con el cierre del día D rige durante el día D+1 (UTC): nunca mira el futuro.
JR usa: bajista = sin largos y cortos con RSI 58 · rango = largos con RSI 42 · alcista = RSI largo 42 y ADX hasta 30. Control: J7.
Ver claude/OSC_por_Estado_BTC_2026-11-22.md del proyecto. Solo observación: todo error se registra y NO afecta al Shadow."""
import pandas as pd
from datetime import datetime, timezone
from html import escape
import config, db, engine

EMA_N, DIAS, UMBRAL = 50, 30, 0.05
NOMBRE = {"alc": "🟢 ALCISTA", "rango": "🟡 RANGO", "baj": "🔴 BAJISTA"}
COMPARAR = ("JR", "J7")

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("CREATE TABLE IF NOT EXISTS estado_btc(fecha TEXT PRIMARY KEY, estado TEXT, cierre_ant REAL, ema50 REAL, chg30 REAL)")

def calcular(df):
    """df: velas diarias CERRADAS con columna c e índice de fecha. Devuelve filas (fecha_vigente, estado, cierre, ema50, chg30)."""
    c = df.c.astype(float)
    ema = c.ewm(span=EMA_N, adjust=False).mean(); chg = c / c.shift(DIAS) - 1
    out = []
    for i in range(len(c)):
        if i < EMA_N or chg.iloc[i] != chg.iloc[i]: continue
        if c.iloc[i] > ema.iloc[i] and chg.iloc[i] > UMBRAL: e = "alc"
        elif c.iloc[i] < ema.iloc[i] and chg.iloc[i] < -UMBRAL: e = "baj"
        else: e = "rango"
        vig = str((c.index[i] + pd.Timedelta(days=1)).date())
        out.append((vig, e, float(c.iloc[i]), float(ema.iloc[i]), float(chg.iloc[i] * 100)))
    return out

def _cargar():
    with db._lock, db._con() as con:
        engine.ESTADO_BTC = {r[0]: r[1] for r in con.execute("SELECT fecha, estado FROM estado_btc").fetchall()}

def actualizar(enviar=None):
    """Llamar al inicio de cada pasada horaria (antes de procesar monedas). Consulta velas 1d solo una vez por día UTC."""
    init_tabla()
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if db.meta_get("estado_btc_dia") != hoy:
        import regimen
        rows = regimen._velas_1d()
        df = pd.DataFrame(rows, columns=["t", "c"]).astype({"t": "int64", "c": float}).drop_duplicates("t").sort_values("t")
        df.index = pd.to_datetime(df.t, unit="ms")
        ahora = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
        df = df[df.index + pd.Timedelta(days=1) <= ahora]          # solo velas diarias cerradas
        filas = calcular(df)
        if filas:
            with db._lock, db._con() as con:
                con.executemany("INSERT OR REPLACE INTO estado_btc VALUES(?,?,?,?,?)", filas)
            db.meta_set("estado_btc_dia", hoy)
    if not engine.ESTADO_BTC or db.meta_get("estado_btc_dia") == hoy: _cargar()
    if not db.meta_get("jr_inicio"):
        db.meta_set("jr_inicio", str(pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None)).floor("h")))
    e = engine.ESTADO_BTC.get(hoy)
    prev = db.meta_get("estado_btc_hoy")
    if e and e != prev:
        db.meta_set("estado_btc_hoy", e)
        if prev and enviar:
            enviar(f"🌡️ SHADOW: BTC pasó de {NOMBRE.get(prev, prev)} a <b>{NOMBRE[e]}</b> (cierre diario de ayer).\n"
                   f"La config JR ahora usa: {_reglas(e)}. Solo observación.")

def _reglas(e):
    return {"baj": "sin largos, cortos con RSI 58", "rango": "largos con RSI 42, cortos RSI 62",
            "alc": "largos con RSI 42, cortos RSI 62, ADX hasta 30"}.get(e, "reglas de J7")

def _stats(cs):
    n = len(cs)
    if not n: return "—"
    w = sum(1 for c in cs if c['pnl5'] > 0); sl = sum(1 for c in cs if c['res'] in ('sl', 'liq'))
    cap = sum(c['pnl5'] / 100 * c['n_ent'] * config.F_MARGEN * 100 for c in cs)
    return f"{n} ciclos · win {100*w/n:.0f}% · SL {sl} · {cap:+.2f}% cap."

def texto():
    try: init_tabla(); _cargar()
    except Exception: pass
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    l = ["🌡️ <b>ESTADO DE BTC y config JR</b> (solo observación)"]
    with db._lock, db._con() as con:
        r = con.execute("SELECT fecha, estado, cierre_ant, ema50, chg30 FROM estado_btc ORDER BY fecha DESC LIMIT 1").fetchone()
    if not r: return l[0] + "\nTodavía sin datos diarios de BTC."
    l.append(f"Hoy ({r[0]}): <b>{NOMBRE[r[1]]}</b> · BTC {r[2]:,.0f} vs media 50d {r[3]:,.0f} · {r[4]:+.1f}% en 30 días")
    l.append(f"JR hoy: {_reglas(r[1])}.")
    # cambios de estado de los últimos 90 días
    fechas = sorted(engine.ESTADO_BTC)[-90:]; cambios = []
    for i, f in enumerate(fechas):
        if i == 0 or engine.ESTADO_BTC[f] != engine.ESTADO_BTC[fechas[i-1]]: cambios.append(f"{f[5:]} {NOMBRE[engine.ESTADO_BTC[f]].split()[1].lower()}")
    l.append("Cambios (90 días): " + " → ".join(cambios[-6:]))
    l.append("")
    l.append("Resultado a 5x, 1% de margen por entrada, desde que corre JR:")
    ini = db.meta_get("jr_inicio")
    for k in COMPARAR:
        if k not in config.CONFIGS: continue
        cs = db.ciclos(k)
        if ini: cs = [c for c in cs if c['ini'] >= ini]
        l.append(f"<b>{escape(k)}</b> total: {_stats(cs)}")
        for e in ("baj", "rango", "alc"):
            sub = [c for c in cs if engine.estado_btc_de(c['ini']) == e]
            if sub: l.append(f"  {NOMBRE[e]}: {_stats(sub)}")
    return "\n".join(l)
