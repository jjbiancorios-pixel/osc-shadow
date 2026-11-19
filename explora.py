"""explora.py — BOT EXPLORADOR (solo avisa y mide, no opera). Aprobado por Juanjo el 19/11/2026.
1) Alertas de figuras candidatas en velas de 4h (armadas con las de 1h del Shadow; solo bloques completos), SOLO LARGOS:
   - HCH invertido (hombro-cabeza-hombro invertido) y DOBLE PISO, detectados con pivotes ZigZag de 2×ATR(14, 4h) ya confirmados.
   - Confirmación: cierre de 4h por encima de la línea de cuello / pico intermedio (la primera vela que la cruza).
   - Filtros: volumen de la vela de ruptura >= 1,5× la media de 20 velas, y BTC 4h por encima de su EMA50 (no aplica a BTC).
   - Stop: 0,5 ATR bajo el último mínimo (HCH: hombro derecho; doble piso: el menor de los dos). Objetivo: el menor entre
     el 70% de la altura de la figura y 2R. Salida por tiempo a las 24 h (6 velas de 4h). Costo 0,15% (en R).
   Prueba 219 días (39 monedas): HCH invertido 4h con estos filtros 13 casos, 62% acierto, +0,25R; doble piso 4h ~0R. Muestra chica.
   Cada alerta sugiere el tamaño para arriesgar 1% del capital y el apalancamiento máximo prudente (liquidación ≥3× más lejos que el stop).
2) /op: registro de las operaciones / ideas de Juanjo, seguidas con velas de 1h:
   /op SOL L 145 140 152 [texto]  → moneda, L/C, entrada, stop, objetivo, comentario libre.
   Se activa cuando el precio toca la entrada (hasta 48 h); luego stop / objetivo / 72 h. Se mide en R y en % del precio.
Todo error se registra y NO afecta al Shadow. Ver claude/Prueba_Bot_Senales_8_Estrategias_y_Figuras_2026-11-19.md."""
import io, csv, bisect, numpy as np, pandas as pd, db, respiro

COSTO = 0.0015
HOLD4 = 6
RVOL_MIN = 1.5
EXCLUIR = ("ETH",)
OP_ACTIVAR_H = 48
OP_MAX_H = 72

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS explora(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, par TEXT, fig TEXT, precio REAL, stop REAL,
                     objetivo REAL, rvol REAL, cerrada INTEGER DEFAULT 0, res TEXT, r REAL, ts_fin TEXT, UNIQUE(par, ts))""")
        c.execute("""CREATE TABLE IF NOT EXISTS ops(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, par TEXT, lado INTEGER, entrada REAL, stop REAL,
                     objetivo REAL, nota TEXT, activa INTEGER DEFAULT 0, ts_act TEXT, cerrada INTEGER DEFAULT 0, res TEXT, salida REAL, r REAL, ts_fin TEXT)""")

def _v4(df):
    v = df[["o", "h", "l", "c", "v"]].resample("4h").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"})
    n = df["c"].resample("4h").count()
    return v[n == 4].dropna()

def _atr(v, n=14):
    pc = v["c"].shift()
    tr = pd.concat([v["h"] - v["l"], (v["h"] - pc).abs(), (v["l"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def _zigzag(h, l, a, k=2.0):
    """Pivotes causales: (i_extremo, precio, +1 máximo / -1 mínimo, i_confirmación)."""
    piv = []; modo = 0; ei = 0; ep = 0.0
    for i in range(len(h)):
        if not a[i] == a[i]: continue
        if modo == 0: ei, ep, modo = i, h[i], 1; continue
        if modo == 1:
            if h[i] > ep: ei, ep = i, h[i]
            elif ep - l[i] >= k * a[i]: piv.append((ei, ep, 1, i)); modo = -1; ei, ep = i, l[i]
        else:
            if l[i] < ep: ei, ep = i, l[i]
            elif h[i] - ep >= k * a[i]: piv.append((ei, ep, -1, i)); modo = 1; ei, ep = i, h[i]
    return piv

def _figura(v):
    """Evalúa la ÚLTIMA vela 4h cerrada. Devuelve (fig, stop, objetivo, rvol) o None."""
    h, l, c, vol = v["h"].values, v["l"].values, v["c"].values, v["v"].values
    a = _atr(v).values; i = len(c) - 1
    rv = vol[i] / np.mean(vol[i - 20:i]) if i >= 21 else 0
    piv = _zigzag(h, l, a); conf = [p[3] for p in piv]
    m = bisect.bisect_right(conf, i)
    if m < 3 or rv < RVOL_MIN: return None
    ai = a[i]; P = piv[:m]
    t = [p[2] for p in P[-5:]]; pr = [p[1] for p in P[-5:]]
    if len(P) >= 5 and t == [-1, 1, -1, 1, -1]:
        L1, H1, L2, H2, L3 = pr
        if L2 < min(L1, L3) - ai and abs(L1 - L3) < ai:
            neck = max(H1, H2)
            if c[i] > neck >= c[i - 1]:
                st = L3 - 0.5 * ai
                return ("HCH invertido", st, c[i] + min(0.7 * (neck - L2), 2 * (c[i] - st)), rv)
    if t[-3:] == [-1, 1, -1]:
        L1, H, L2 = pr[-3:]; dist = P[-1][0] - P[-3][0]
        if abs(L1 - L2) < 0.5 * ai and H - L1 >= 3 * ai and 10 <= dist <= 60 and c[i] > H >= c[i - 1]:
            lo = min(L1, L2); st = lo - 0.5 * ai
            return ("Doble piso", st, c[i] + min(0.7 * (H - lo), 2 * (c[i] - st)), rv)
    return None

def _resolver4(v, ts, e, st, tg):
    t0 = pd.Timestamp(ts)
    if t0 not in v.index: return None
    i = v.index.get_loc(t0); d = (e - st) / e
    O, H, L, C = v["o"].values, v["h"].values, v["l"].values, v["c"].values
    for k in range(i + 1, min(i + 1 + HOLD4, len(v))):
        if L[k] <= st: return "stop", ((min(st, O[k]) / e - 1) - COSTO) / d, str(v.index[k])
        if H[k] >= tg: return "objetivo", ((max(tg, O[k]) / e - 1) - COSTO) / d, str(v.index[k])
    if i + HOLD4 < len(v): k = i + HOLD4; return "24h", ((C[k] / e - 1) - COSTO) / d, str(v.index[k])
    return None

def _sugerencia(e, st):
    d = abs(e - st) / e
    lev10 = 0.10 / d                       # apalancamiento con 10% de margen para arriesgar 1% del capital
    levmax = 1 / (3 * d)                   # liquidación al menos 3 veces más lejos que el stop
    return d, lev10, levmax

def actualizar(enviar=None):
    """Llamar una vez por hora, después de procesar las monedas (usa respiro.CACHE)."""
    init_tabla()
    btc = respiro.CACHE.get("BTC")
    if btc is None or len(btc) < 300: return
    b4 = _v4(btc); bema = b4["c"].ewm(span=50, adjust=False).mean()
    V = {}
    for m, df in respiro.CACHE.items():
        if m in EXCLUIR or df is None or len(df) < 300: continue
        try: V[m] = _v4(df)
        except Exception: pass
    with db._lock, db._con() as con:
        # 1) resolver alertas abiertas
        for r in con.execute("SELECT id, ts, par, precio, stop, objetivo FROM explora WHERE cerrada=0").fetchall():
            v = V.get(r["par"])
            if v is None: continue
            x = _resolver4(v, r["ts"], r["precio"], r["stop"], r["objetivo"])
            if x: con.execute("UPDATE explora SET cerrada=1, res=?, r=?, ts_fin=? WHERE id=?", (x[0], x[1], x[2], r["id"]))
        # 2) seguir las operaciones de Juanjo (/op) con velas de 1h
        for r in con.execute("SELECT * FROM ops WHERE cerrada=0").fetchall():
            df = respiro.CACHE.get(r["par"])
            if df is None: continue
            x = _seguir_op(df, dict(r))
            if x: con.execute("UPDATE ops SET activa=?, ts_act=?, cerrada=?, res=?, salida=?, r=?, ts_fin=? WHERE id=?",
                              (x["activa"], x["ts_act"], x["cerrada"], x["res"], x["salida"], x["r"], x["ts_fin"], r["id"]))
        # 3) nueva vela 4h cerrada → buscar figuras
        ult = con.execute("SELECT v FROM meta WHERE k='expl_ult4'").fetchone()
        t4 = str(b4.index[-1])
        if ult is not None and ult[0] >= t4: return
        con.execute("INSERT OR REPLACE INTO meta VALUES('expl_ult4', ?)", (t4,))
        btc_ok = b4["c"].iloc[-1] > bema.iloc[-1]
        abiertas = {x[0] for x in con.execute("SELECT par FROM explora WHERE cerrada=0").fetchall()}
        avisos = []
        for m, v in V.items():
            if m in abiertas or len(v) < 60 or str(v.index[-1]) != t4: continue
            if m != "BTC" and not btc_ok: continue
            try: f = _figura(v)
            except Exception: f = None
            if not f: continue
            fig, st, tg, rv = f; e = float(v["c"].iloc[-1])
            if not (st < e < tg): continue
            try:
                con.execute("INSERT INTO explora(ts,par,fig,precio,stop,objetivo,rvol) VALUES(?,?,?,?,?,?,?)", (t4, m, fig, e, st, tg, rv))
            except Exception: continue
            d, lev10, levmax = _sugerencia(e, st)
            avisos.append(f"🔎 <b>{m}</b> · {fig} (4h) · LARGO\nEntrada ≈ {e:.6g} · stop {st:.6g} (−{100*d:.1f}%) · objetivo {tg:.6g} (+{100*(tg/e-1):.1f}%)\n"
                          f"Volumen {rv:.1f}× · BTC sobre EMA50 4h · salida a las 24 h\n"
                          f"Para arriesgar 1% del capital con 10% de margen: ≈{lev10:.1f}x (máx. prudente {levmax:.0f}x)\n"
                          f"<i>Solo aviso (prueba): en 219 días esta figura dio pocos casos; decidís vos.</i>")
    if enviar:
        for a in avisos: enviar(a)

def _seguir_op(df, r):
    sd = r["lado"]; e, st, tg = r["entrada"], r["stop"], r["objetivo"]
    t0 = pd.Timestamp(r["ts"]); idx = df.index
    i0 = idx.searchsorted(t0)          # primera vela que abre después del registro (la vela del registro se toma completa)
    H, L, C, O = df["h"].values, df["l"].values, df["c"].values, df["o"].values
    out = dict(activa=r["activa"], ts_act=r["ts_act"], cerrada=0, res=None, salida=None, r=None, ts_fin=None)
    ia = None
    if r["activa"] and r["ts_act"]: ia = idx.searchsorted(pd.Timestamp(r["ts_act"]))
    else:
        for k in range(max(i0 - 1, 0), len(df)):
            if (idx[k] - t0).total_seconds() > OP_ACTIVAR_H * 3600:
                out.update(cerrada=1, res="no_entró", ts_fin=str(idx[k])); return out
            if L[k] <= e <= H[k]: ia = k; out.update(activa=1, ts_act=str(idx[k])); break
        if ia is None: return out
    d = abs(e - st) / e
    for k in range(ia, len(df)):
        if (sd == 1 and L[k] <= st) or (sd == -1 and H[k] >= st):      # en la misma vela se toma primero el stop (criterio prudente)
            px = st; out.update(cerrada=1, res="stop", salida=px, r=(sd * (px / e - 1) - COSTO) / d, ts_fin=str(idx[k])); return out
        if (sd == 1 and H[k] >= tg) or (sd == -1 and L[k] <= tg):
            px = tg; out.update(cerrada=1, res="objetivo", salida=px, r=(sd * (px / e - 1) - COSTO) / d, ts_fin=str(idx[k])); return out
        if (idx[k] - idx[ia]).total_seconds() >= OP_MAX_H * 3600:
            px = C[k]; out.update(cerrada=1, res="72h", salida=px, r=(sd * (px / e - 1) - COSTO) / d, ts_fin=str(idx[k])); return out
    return out

def registrar_op(partes):
    """/op SOL L 145 140 152 texto libre"""
    init_tabla()
    if len(partes) < 6:
        return "Formato: /op MONEDA L|C entrada stop objetivo [comentario]\nEj.: /op SOL L 145 140 152 doble piso en 4h"
    par = partes[1].upper().replace("USDT", "").replace("-", "")
    lado = 1 if partes[2].upper() in ("L", "LARGO", "LONG") else -1 if partes[2].upper() in ("C", "CORTO", "SHORT") else 0
    try: e, st, tg = (float(x.replace(",", ".")) for x in partes[3:6])
    except Exception: return "No entendí los precios. Ej.: /op SOL L 145 140 152"
    if not lado: return "Lado: L (largo) o C (corto)."
    if not ((lado == 1 and st < e < tg) or (lado == -1 and tg < e < st)): return "Revisá los niveles: en largo stop < entrada < objetivo; en corto al revés."
    if par not in respiro.CACHE: return f"{par} no está en las monedas del Shadow: no la puedo seguir con velas (anotala igual en el chat de Claude)."
    nota = " ".join(partes[6:])[:200]
    ts = str(pd.Timestamp.utcnow().tz_localize(None).floor("h"))
    with db._lock, db._con() as c:
        c.execute("INSERT INTO ops(ts,par,lado,entrada,stop,objetivo,nota) VALUES(?,?,?,?,?,?,?)", (ts, par, lado, e, st, tg, nota))
    d = abs(e - st) / e; rr = abs(tg - e) / abs(e - st)
    return (f"📝 Registrada: {par} {'LARGO' if lado==1 else 'CORTO'} · entrada {e:g} · stop {st:g} (−{100*d:.1f}%) · objetivo {tg:g} · relación {rr:.1f}R"
            f"{' · ' + nota if nota else ''}\nLa sigo con velas de 1h (se activa si el precio toca la entrada en 48 h; cierre por stop/objetivo/72 h).")

def _resumen(rows, nombre):
    R = [x["r"] for x in rows if x["cerrada"] and x["r"] is not None]
    ab = sum(1 for x in rows if not x["cerrada"])
    if not R: return f"{nombre}: {len(rows)} registradas, {ab} en curso, sin resultados aún."
    w = sum(1 for r in R if r > 0)
    return f"{nombre}: {len(R)} cerradas ({w} ganan, {100*w/len(R):.0f}%), {ab} en curso · {sum(R)/len(R):+.2f} R por op. · total {sum(R):+.1f} R"

def texto():
    init_tabla()
    with db._lock, db._con() as c:
        E = [dict(x) for x in c.execute("SELECT * FROM explora ORDER BY id").fetchall()]
        O = [dict(x) for x in c.execute("SELECT * FROM ops ORDER BY id").fetchall()]
    l = ["🔎 <b>Explorador de figuras</b> (4h, solo largos, observación)"]
    for fig in ("HCH invertido", "Doble piso"):
        l.append(_resumen([x for x in E if x["fig"] == fig], fig))
    ab = [x for x in E if not x["cerrada"]]
    if ab: l.append("En curso: " + ", ".join(f"{x['par']} {x['fig']} @{x['precio']:.4g}" for x in ab))
    l.append("\n📝 <b>Operaciones de Juanjo (/op)</b>")
    l.append(_resumen(O, "Todas"))
    for x in O[-5:]:
        estado = x["res"] or ("activa" if x["activa"] else "esperando entrada")
        rr = "" if x["r"] is None else f" ({x['r']:+.2f}R)"
        l.append(f"· {x['ts'][5:16]} {x['par']} {'L' if x['lado']==1 else 'C'} {x['entrada']:g} → {estado}{rr}")
    return "\n".join(l)

def csv_todo():
    init_tabla(); o = io.StringIO(); w = csv.writer(o)
    with db._lock, db._con() as c:
        w.writerow(["tipo", "ts_utc", "par", "figura/nota", "lado", "entrada", "stop", "objetivo", "rvol", "activa", "cerrada", "resultado", "R", "ts_fin"])
        for x in c.execute("SELECT * FROM explora ORDER BY id").fetchall():
            w.writerow(["explorador", x["ts"], x["par"], x["fig"], "L", x["precio"], x["stop"], x["objetivo"], round(x["rvol"] or 0, 2), 1, x["cerrada"], x["res"],
                        None if x["r"] is None else round(x["r"], 3), x["ts_fin"]])
        for x in c.execute("SELECT * FROM ops ORDER BY id").fetchall():
            w.writerow(["op", x["ts"], x["par"], x["nota"], "L" if x["lado"] == 1 else "C", x["entrada"], x["stop"], x["objetivo"], None, x["activa"], x["cerrada"],
                        x["res"], None if x["r"] is None else round(x["r"], 3), x["ts_fin"]])
    return o.getvalue().encode()
