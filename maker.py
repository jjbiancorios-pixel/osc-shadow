"""maker.py — "MAKER": ¿se habría llenado la 1ª entrada como orden LÍMITE (maker) en vez de a mercado? (solo observación, no opera).
Aprobado por Juanjo el 22/11/2026. Ver claude/Investigacion_Alternativas_30k_2026-11-21.md y _2026-11-22.md del proyecto.

Qué mide (configs J y J7, que son las más parecidas al OSC REAL):
  - En el bot real la 1ª entrada de cada ciclo va A MERCADO (taker 0,05%). Los DCA ya son órdenes límite y el TP sale por stop (taker).
    Por eso lo único que se puede pasar a maker (0,02%) es la 1ª entrada.
  - Precio límite = precio de la 1ª entrada del Shadow (apertura de la vela siguiente a la señal).
  - Con velas de 1 MINUTO de BingX se mira si en 5 y en 15 minutos el precio pasó MÁS ALLÁ del límite (no alcanza con tocarlo:
    puede haber otras órdenes antes en la fila). Se guarda cuánto pasó (pen5/pen15, en %) para poder cambiar el umbral después.
    Largo (compra límite): pasó = mínimo < límite × (1 − UMBRAL). Corto (venta límite): máximo > límite × (1 + UMBRAL).
  - También se guarda el cierre a los 15 min (c15) para la variante "límite 15 min y si no se llenó, a mercado".
Costo: 1 consulta pública a BingX por ciclo nuevo (~20–50 por día), con pausa entre consultas. Sin claves ni dinero.
Todo error se registra y NO afecta al Shadow."""
import io, csv, json, time, requests, pandas as pd
from datetime import datetime, timezone
import config, db

CONFIGS = ("J7", "J")
UMBRAL = 0.02          # % que el precio debe pasar más allá del límite para contarla llena
FEE_TAKER = 0.0005
FEE_MAKER = 0.0002
MAX_POR_HORA = 40      # tope de consultas por pasada (la primera vez rellena los ciclos viejos de a poco)
DIAS_ATRAS = 30        # no se miden ciclos más viejos que esto
CAP_REF = 640.0        # capital de referencia del OSC REAL para pasar % a USDT (aprox.)

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS maker(cfg TEXT, sym TEXT, ini TEXT, side INTEGER, px REAL, pen5 REAL, pen15 REAL,
                     c15 REAL, n1m INTEGER, ts_med TEXT, PRIMARY KEY(cfg, sym, ini))""")

def _ms(ts): return int(pd.Timestamp(ts).value // 1_000_000)

def velas_1m(sym, ini, minutos=15):
    """Velas de 1 min desde la apertura de la vela de entrada (UTC naive). BingX primero, Binance de respaldo. Devuelve DataFrame o None."""
    t0 = _ms(ini); t1 = t0 + minutos * 60_000 - 1
    for fuente in ("bingx", "binance"):
        try:
            if fuente == "bingx":
                r = requests.get(f"{config.BINGX_URL}/openApi/swap/v3/quote/klines",
                                 params={"symbol": f"{sym}-USDT", "interval": "1m", "startTime": t0, "endTime": t1, "limit": minutos}, timeout=15).json()
                data = r.get("data") or []
                rows = [(k['time'], k['high'], k['low'], k['close']) if isinstance(k, dict) else (k[0], k[2], k[3], k[4]) for k in data]
            else:
                r = requests.get(f"{config.BINANCE_URL}/fapi/v1/klines",
                                 params={"symbol": f"{sym}USDT", "interval": "1m", "startTime": t0, "endTime": t1, "limit": minutos}, timeout=15).json()
                rows = [(k[0], k[2], k[3], k[4]) for k in r] if isinstance(r, list) else []
            if not rows: continue
            df = pd.DataFrame(rows, columns=["t", "h", "l", "c"]).astype({"t": "int64", "h": float, "l": float, "c": float})
            df = df[(df.t >= t0) & (df.t <= t1)].drop_duplicates("t").sort_values("t")
            if len(df): return df
        except Exception:
            continue
    return None

def medir(side, px, df):
    """pen = cuánto pasó el precio más allá del límite (en %, positivo = pasó; negativo = no llegó)."""
    def pen(sub):
        if side == 1: return (px - sub.l.min()) / px * 100
        return (sub.h.max() - px) / px * 100
    t0 = int(df.t.iloc[0])
    p5 = pen(df[df.t < t0 + 5 * 60_000]) if len(df[df.t < t0 + 5 * 60_000]) else None
    return p5, pen(df), float(df.c.iloc[-1])

def _pendientes():
    with db._lock, db._con() as con:
        hechos = {(r[0], r[1], r[2]) for r in con.execute("SELECT cfg, sym, ini FROM maker").fetchall()}
    lim = str(datetime.now(timezone.utc).replace(tzinfo=None) - pd.Timedelta(days=DIAS_ATRAS))
    out = []
    for k in CONFIGS:
        for c in db.ciclos(k):
            if c['ini'] >= lim and (k, c['sym'], c['ini']) not in hechos:
                out.append((k, c['sym'], c['ini'], int(c['side']), float(json.loads(c['entradas'])[0])))
        for s, kk, st in db.estados_abiertos(k):
            if st.get('t0') and st['t0'] >= lim and (k, s, st['t0']) not in hechos and st.get('ent'):
                out.append((k, s, st['t0'], int(st['side']), float(st['ent'][0])))
    out.sort(key=lambda x: x[2], reverse=True)   # primero los más nuevos
    return out

def actualizar():
    """Llamar una vez por hora, después de procesar las monedas."""
    init_tabla()
    pend = _pendientes()   # (db.ciclos toma el lock por su cuenta: no anidar)
    ahora = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    cache = {}; n = 0; sin = 0
    for k, sym, ini, side, px in pend:
        if pd.Timestamp(ini) + pd.Timedelta(minutes=16) > ahora: continue
        if n >= MAX_POR_HORA: break
        clave = (sym, ini)
        if clave not in cache:
            cache[clave] = velas_1m(sym, ini); n += 1; time.sleep(config.PAUSA_ENTRE_MONEDAS_S)
        df = cache[clave]
        if df is None or len(df) < 10:
            sin += 1; continue   # sin datos: se reintenta en la próxima pasada
        p5, p15, c15 = medir(side, px, df)
        with db._lock, db._con() as con:
            con.execute("INSERT OR REPLACE INTO maker VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (k, sym, ini, side, px, p5, p15, c15, len(df), str(ahora)))
    db.meta_set("maker_sin_datos", sin)

def _filas(cfg):
    with db._lock, db._con() as con:
        m = [dict(r) for r in con.execute("SELECT * FROM maker WHERE cfg=?", (cfg,)).fetchall()]
    cs = {(c['sym'], c['ini']): c for c in db.ciclos(cfg)}
    for r in m: r['ciclo'] = cs.get((r['sym'], r['ini']))
    return m

def texto():
    l = ["🏷️ <b>MAKER</b> — ¿la 1ª entrada se habría llenado como orden límite? (solo observación)",
         f"Cuenta como llena si el precio pasó {UMBRAL:.2f}% más allá del límite. Ahorro maker vs taker: 0,03% de la 1ª entrada."]
    for k in CONFIGS:
        m = _filas(k)
        l.append("")
        l.append(f"<b>{k}</b>: {len(m)} entradas medidas")
        if not m: l.append("Todavía sin datos."); continue
        f5 = [r for r in m if r['pen5'] is not None and r['pen5'] > UMBRAL]
        f15 = [r for r in m if r['pen15'] > UMBRAL]
        l.append(f"Llenas en ≤5 min: {100*len(f5)/len(m):.0f}% · en ≤15 min: {100*len(f15)/len(m):.0f}%")
        # ahorro por entrada llena, en % del capital (margen 1% × 5x)
        ah = (FEE_TAKER - FEE_MAKER) * 5 * config.F_MARGEN * 100
        cerr = [r for r in m if r['ciclo']]
        no = [r for r in cerr if not (r['pen15'] > UMBRAL)]
        pnl_no = sum(r['ciclo']['pnl5'] / 100 * r['ciclo']['n_ent'] * config.F_MARGEN * 100 for r in no)
        ahorro = len([r for r in cerr if r['pen15'] > UMBRAL]) * ah
        l.append(f"Ciclos cerrados medidos: {len(cerr)} · no llenos en 15 min: {len(no)} (resultado de esos ciclos: {pnl_no:+.2f}% del capital)")
        # Variante A: si no se llena, se SALTEA el ciclo
        va = ahorro - pnl_no
        # Variante B: si no se llena en 15 min, entra a mercado al cierre del minuto 15 (paga taker + el desvío de precio)
        desv = sum(((r['c15'] / r['px'] - 1) * r['side']) * 5 * config.F_MARGEN * 100 for r in no)
        vb = ahorro - desv
        l.append(f"A) límite 15 min, si no se llena se saltea: <b>{va:+.3f}%</b> del capital (≈{va/100*CAP_REF:+.2f} USDT con {CAP_REF:.0f})")
        l.append(f"B) límite 15 min, si no se llena va a mercado: <b>{vb:+.3f}%</b> (≈{vb/100*CAP_REF:+.2f} USDT)")
    sd = db.meta_get("maker_sin_datos", "0")
    if sd and sd != "0": l.append(f"\n⚠️ Última pasada: {sd} entradas sin velas de 1 min (se reintentan).")
    l.append("")
    l.append("Positivo = mejor que entrar siempre a mercado. A 5x y sin tope de ciclos (aprox.).")
    return "\n".join(l)

def csv_todo():
    with db._lock, db._con() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM maker ORDER BY ini").fetchall()]
    b = io.StringIO(); w = csv.writer(b)
    w.writerow(["cfg", "sym", "ini_utc", "side", "px_limite", "pen5_pct", "pen15_pct", "cierre_15m", "velas_1m", "medido"])
    for r in rows: w.writerow([r['cfg'], r['sym'], r['ini'], r['side'], r['px'], r['pen5'], r['pen15'], r['c15'], r['n1m'], r['ts_med']])
    return b.getvalue().encode()
