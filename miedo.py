"""miedo.py — "ACUMULADOR POR MIEDO": prueba VIRTUAL (sin dinero, sin órdenes) para los bots acumuladores ETH y BTC.
Aprobado por Juanjo el 25/11/2026. Ver claude/Investigacion_Alternativas_30k_2026-11-24.md del proyecto.

Qué hace:
  1) Registra a diario el índice Fear & Greed (API pública de alternative.me, sin clave).
  2) Simula, para BTC y ETH, tres formas de gastar el mismo presupuesto virtual (1.000 USDT por moneda):
       MIEDO : compra 1 tramo (100 USDT) cuando el F&G del día es <= 10 Y la moneda lleva 3 días sin hacer
               un mínimo nuevo de 30 días. Entre compras, al menos 7 días. Máximo 10 tramos.
       DCA   : compra 100 USDT cada 7 días desde el arranque (referencia justa).
       TODO  : compra los 1.000 USDT el día del arranque.
     Se mide en MONEDA (objetivo de los acumuladores): moneda comprada + USDT sin usar pasado a moneda al precio de hoy.
  3) /miedo_hist: prueba histórica con todo el F&G (desde 2018) y velas diarias, para tener evidencia ya.

Sin mirar el futuro: la decisión del día D usa el F&G publicado a las 00:00 UTC del día D y la vela diaria del día D
ya cerrada; la compra se hace al cierre de D (+0,1% de comisión de spot).
Costo: 1 consulta diaria a alternative.me + 2 de velas 1d. Todo error se registra y NO afecta al Shadow."""
import io, csv, requests, pandas as pd
from datetime import datetime, timezone
from html import escape
import config, db

FNG_URL = "https://api.alternative.me/fng/"
SPOT_URL = "https://data-api.binance.vision"      # espejo público de datos de mercado de Binance (sin bloqueo geográfico)
MONEDAS = ("BTC", "ETH")
UMBRAL = 10          # F&G <= UMBRAL
DIAS_SIN_MIN = 3     # la moneda no hizo mínimo nuevo en los últimos N días cerrados
VENTANA_MIN = 30     # mínimo nuevo = mínimo de los últimos 30 días
ESPERA = 7           # días mínimos entre compras
TRAMO = 100.0        # USDT por compra
TRAMOS = 10          # máximo de tramos (presupuesto 1.000 USDT por moneda)
FEE = 0.001          # comisión spot por compra (0,1%)

# ---------------------------------------------------------------- base de datos
def init_tabla():
    with db._lock, db._con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS fng(fecha TEXT PRIMARY KEY, valor INTEGER, clase TEXT);
        CREATE TABLE IF NOT EXISTS miedo_velas(sym TEXT, fecha TEXT, h REAL, l REAL, c REAL, PRIMARY KEY(sym, fecha));
        CREATE TABLE IF NOT EXISTS miedo_ops(id INTEGER PRIMARY KEY AUTOINCREMENT, sym TEXT, estr TEXT, fecha TEXT,
            precio REAL, usdt REAL, moneda REAL, fng INTEGER);
        """)

# ---------------------------------------------------------------- datos
def _bajar_fng():
    r = requests.get(FNG_URL, params={"limit": 0, "format": "json"}, timeout=20).json()
    out = []
    for x in r.get("data") or []:
        f = datetime.fromtimestamp(int(x["timestamp"]), timezone.utc).strftime("%Y-%m-%d")
        out.append((f, int(x["value"]), x.get("value_classification", "")))
    if len(out) < 30: raise ValueError(f"F&G sin datos: {str(r)[:120]}")
    return out

def _velas_spot(sym):
    """Velas 1d desde 2018 (paginado de a 1000)."""
    rows, desde = [], int(pd.Timestamp("2018-01-01").value // 10**6)
    for _ in range(4):
        r = requests.get(f"{SPOT_URL}/api/v3/klines", params={"symbol": f"{sym}USDT", "interval": "1d", "startTime": desde, "limit": 1000}, timeout=20).json()
        if not isinstance(r, list) or not r: break
        rows += [(k[0], k[2], k[3], k[4]) for k in r]
        if len(r) < 1000: break
        desde = r[-1][0] + 1
    if len(rows) < 100: raise ValueError(f"spot sin datos {sym}")
    return rows

def _velas_bingx(sym):
    r = requests.get(f"{config.BINGX_URL}/openApi/swap/v3/quote/klines", params={"symbol": f"{sym}-USDT", "interval": "1d", "limit": 1000}, timeout=20).json()
    data = r.get("data") or []
    rows = [(k['time'], k['high'], k['low'], k['close']) if isinstance(k, dict) else (k[0], k[2], k[3], k[4]) for k in data]
    if len(rows) < 100: raise ValueError(f"BingX sin datos {sym}: {str(r)[:120]}")
    return rows

def _velas(sym):
    try: rows = _velas_spot(sym)
    except Exception: rows = _velas_bingx(sym)
    df = pd.DataFrame(rows, columns=["t", "h", "l", "c"]).astype({"t": "int64", "h": float, "l": float, "c": float}).drop_duplicates("t").sort_values("t")
    df.index = pd.to_datetime(df.t, unit="ms")
    ahora = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    return df[df.index + pd.Timedelta(days=1) <= ahora][["h", "l", "c"]]   # solo velas diarias cerradas

def fng_mapa():
    with db._lock, db._con() as con:
        return {r[0]: r[1] for r in con.execute("SELECT fecha, valor FROM fng").fetchall()}

def velas_db(sym):
    with db._lock, db._con() as con:
        rows = con.execute("SELECT fecha, h, l, c FROM miedo_velas WHERE sym=? ORDER BY fecha", (sym,)).fetchall()
    df = pd.DataFrame([tuple(r) for r in rows], columns=["fecha", "h", "l", "c"])
    if df.empty: return df
    df.index = pd.to_datetime(df.fecha); return df[["h", "l", "c"]]

# ---------------------------------------------------------------- regla
def senal(df, i, fng, umbral=UMBRAL, filtro=True):
    """True si el día i (vela cerrada) cumple la regla. df con columnas h, l, c e índice diario."""
    f = str(df.index[i].date()); v = fng.get(f)
    if v is None or v > umbral: return False
    if not filtro: return True
    if i < VENTANA_MIN: return False
    rec = df.l.iloc[i - DIAS_SIN_MIN + 1: i + 1].min()
    previo = df.l.iloc[i - VENTANA_MIN + 1: i - DIAS_SIN_MIN + 1].min()
    return rec > previo

def _compra(usdt, precio): return usdt * (1 - FEE) / precio

# ---------------------------------------------------------------- simulación en vivo (desde el arranque)
def _ops(sym=None):
    q = "SELECT * FROM miedo_ops" + (" WHERE sym=?" if sym else "") + " ORDER BY fecha, id"
    with db._lock, db._con() as con: return [dict(r) for r in con.execute(q, (sym,) if sym else ()).fetchall()]

def _guardar_op(sym, estr, fecha, precio, usdt, fng):
    with db._lock, db._con() as con:
        con.execute("INSERT INTO miedo_ops(sym,estr,fecha,precio,usdt,moneda,fng) VALUES(?,?,?,?,?,?,?)",
                    (sym, estr, fecha, precio, usdt, _compra(usdt, precio), fng))

def _vivo(sym, df, fng, enviar):
    inicio = db.meta_get("miedo_inicio")
    if not inicio:
        inicio = str(df.index[-1].date()); db.meta_set("miedo_inicio", inicio)
    if not any(o['estr'] == "TODO" for o in _ops(sym)):          # arranque de esta moneda: TODO y 1ª compra del DCA
        d0 = df[df.index >= pd.Timestamp(inicio)]
        if d0.empty: return
        f0 = str(d0.index[0].date()); p0 = float(d0.c.iloc[0])
        _guardar_op(sym, "TODO", f0, p0, TRAMO * TRAMOS, fng.get(f0))
        _guardar_op(sym, "DCA", f0, p0, TRAMO, fng.get(f0))
    hechos = {(o['estr'], o['fecha']) for o in _ops(sym)}
    for i in range(len(df)):
        f = str(df.index[i].date())
        if f < inicio: continue
        ops = _ops(sym); p = float(df.c.iloc[i])
        dca = [o for o in ops if o['estr'] == "DCA"]
        if len(dca) < TRAMOS and ("DCA", f) not in hechos and (pd.Timestamp(f) - pd.Timestamp(dca[-1]['fecha'])).days >= ESPERA:
            _guardar_op(sym, "DCA", f, p, TRAMO, fng.get(f)); hechos.add(("DCA", f))
        mie = [o for o in ops if o['estr'] == "MIEDO"]
        ok_espera = not mie or (pd.Timestamp(f) - pd.Timestamp(mie[-1]['fecha'])).days >= ESPERA
        if len(mie) < TRAMOS and ok_espera and ("MIEDO", f) not in hechos and senal(df, i, fng):
            _guardar_op(sym, "MIEDO", f, p, TRAMO, fng.get(f)); hechos.add(("MIEDO", f))
            if f == str(df.index[-1].date()):
                enviar(f"🟣 SHADOW MIEDO: compra VIRTUAL de {sym} ({len(mie)+1}/{TRAMOS}) a {p:,.2f}. "
                       f"F&G {fng.get(f)} y {DIAS_SIN_MIN} días sin mínimo nuevo. Sin dinero, solo observación.")

def actualizar(enviar=lambda m: None):
    """Llamar una vez por hora; trabaja una vez por día (UTC), después de las 01:00 UTC."""
    init_tabla()
    ahora = datetime.now(timezone.utc); hoy = ahora.strftime("%Y-%m-%d")
    if ahora.hour < 1 or db.meta_get("miedo_dia") == hoy: return
    fn = _bajar_fng()
    with db._lock, db._con() as con:
        con.executemany("INSERT OR REPLACE INTO fng VALUES(?,?,?)", fn)
    fng = fng_mapa()
    for sym in MONEDAS:
        df = _velas(sym)
        with db._lock, db._con() as con:
            con.executemany("INSERT OR REPLACE INTO miedo_velas VALUES(?,?,?,?,?)",
                            [(sym, str(t.date()), float(h), float(l), float(c)) for t, h, l, c in zip(df.index, df.h, df.l, df.c)])
        _vivo(sym, df, fng, enviar)
    db.meta_set("miedo_dia", hoy)

# ---------------------------------------------------------------- reportes
def fng_hoy():
    with db._lock, db._con() as con:
        r = con.execute("SELECT fecha, valor, clase FROM fng ORDER BY fecha DESC LIMIT 1").fetchone()
    return tuple(r) if r else None

def _cartera(ops, precio):
    gast = sum(o['usdt'] for o in ops); mon = sum(o['moneda'] for o in ops)
    resto = TRAMO * TRAMOS - gast
    return mon, resto, (gast / mon if mon else None), mon + resto / precio

def texto():
    l = ["🟣 <b>ACUMULADOR POR MIEDO</b> (virtual, sin dinero)"]
    h = fng_hoy()
    if not h: return l[0] + "\nTodavía sin datos del Fear &amp; Greed."
    l.append(f"Fear &amp; Greed {h[0]}: <b>{h[1]}</b> ({escape(h[2])})")
    l.append(f"Regla: F&amp;G ≤{UMBRAL} + {DIAS_SIN_MIN} días sin mínimo de {VENTANA_MIN} días · {TRAMO:.0f} USDT por compra · espera {ESPERA} días · máx. {TRAMOS}")
    l.append(f"Desde {db.meta_get('miedo_inicio', '—')} · se mide en moneda (comprada + USDT sin usar pasado a moneda hoy)")
    for sym in MONEDAS:
        df = velas_db(sym)
        if df.empty: continue
        p = float(df.c.iloc[-1]); ops = _ops(sym); l.append(""); l.append(f"<b>{sym}</b> hoy {p:,.2f}")
        res = {}
        for e in ("MIEDO", "DCA", "TODO"):
            oe = [o for o in ops if o['estr'] == e]; mon, resto, pm, eq = _cartera(oe, p); res[e] = eq
            l.append(f"{e}: {len(oe)} compras · {mon:.6f} {sym} · {resto:.0f} USDT sin usar · prom {pm:,.2f} · equiv. {eq:.6f}" if pm
                     else f"{e}: sin compras · equiv. {eq:.6f} {sym}")
        if res.get("DCA"): l.append(f"MIEDO vs DCA: {100*(res['MIEDO']/res['DCA']-1):+.1f}% de moneda")
    return "\n".join(l)

def historico(sym, umbral=UMBRAL, filtro=True):
    """Prueba sobre toda la historia guardada. Devuelve dict con compras de la regla y referencia DCA semanal (sin tope)."""
    df = velas_db(sym); fng = fng_mapa()
    if df.empty or not fng: return None
    compras, ult = [], None
    for i in range(len(df)):
        if ult is not None and (df.index[i] - df.index[ult]).days < ESPERA: continue
        if senal(df, i, fng, umbral, filtro):
            p = df.c.iloc[i]; fut = df.c.iloc[i + 1: i + 181]
            r = lambda n: (df.c.iloc[i + n] / p - 1) * 100 if i + n < len(df) else None
            compras.append({"fecha": str(df.index[i].date()), "precio": float(p), "fng": fng.get(str(df.index[i].date())),
                            "r30": r(30), "r90": r(90), "r180": r(180), "caida": float((fut.min() / p - 1) * 100) if len(fut) else None})
            ult = i
    ini = min(f for f in fng); d = df[df.index >= pd.Timestamp(ini)]
    dca = d.c.iloc[::ESPERA]
    return {"compras": compras, "prom_regla": (len(compras) / sum(1 / c['precio'] for c in compras)) if compras else None,
            "prom_dca": len(dca) / float((1 / dca).sum()), "desde": ini, "n_dca": len(dca)}

def _med(v):
    v = sorted(x for x in v if x is not None)
    return v[len(v) // 2] if v else None

def texto_hist(sym="BTC"):
    sym = sym.upper()
    if sym not in MONEDAS: return f"Uso: /miedo_hist BTC o /miedo_hist ETH"
    l = [f"🟣 <b>MIEDO histórico {sym}</b> (precio promedio de compra: más bajo = más moneda por USDT)"]
    for u, fl in ((10, True), (10, False), (15, True), (20, True)):
        h = historico(sym, u, fl)
        if not h: return l[0] + "\nTodavía sin datos (se cargan en la primera pasada diaria)."
        cs = h['compras']; nom = f"F&amp;G ≤{u}" + (" + filtro" if fl else " sin filtro")
        if not cs: l.append(f"{nom}: 0 compras"); continue
        dif = 100 * (h['prom_regla'] / h['prom_dca'] - 1)
        fmt = lambda x: "—" if x is None else f"{x:+.0f}%"
        l.append(f"{nom}: {len(cs)} compras · prom {h['prom_regla']:,.0f} vs DCA {h['prom_dca']:,.0f} ({dif:+.1f}%) · "
                 f"mediana 90d {fmt(_med([c['r90'] for c in cs]))} · 180d {fmt(_med([c['r180'] for c in cs]))} · "
                 f"peor caída posterior {fmt(min((c['caida'] for c in cs if c['caida'] is not None), default=None))}")
    l.append(f"DCA = 1 compra cada {ESPERA} días desde {h['desde']}. Muestra chica: pocos episodios de miedo extremo.")
    return "\n".join(l)

def csv_todo():
    b = io.StringIO(); w = csv.writer(b)
    w.writerow(["tipo", "sym", "estrategia", "fecha", "precio", "usdt", "moneda", "fng", "r30", "r90", "r180", "caida_max_180d"])
    for o in _ops(): w.writerow(["vivo", o['sym'], o['estr'], o['fecha'], o['precio'], o['usdt'], round(o['moneda'], 8), o['fng'], "", "", "", ""])
    for sym in MONEDAS:
        h = historico(sym)
        for c in (h or {}).get("compras", []):
            w.writerow(["historico", sym, f"MIEDO<={UMBRAL}+filtro", c['fecha'], c['precio'], TRAMO, round(_compra(TRAMO, c['precio']), 8), c['fng'],
                        *("" if c[k] is None else round(c[k], 2) for k in ("r30", "r90", "r180", "caida"))])
    w.writerow([]); w.writerow(["fecha", "fng"])
    for f, v in sorted(fng_mapa().items()): w.writerow([f, v])
    return b.getvalue().encode()
