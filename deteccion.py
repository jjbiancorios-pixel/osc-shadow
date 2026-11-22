"""deteccion.py — DETECTOR DE ENTRADAS PROPIAS (solo avisa y mide, no opera). Aprobado por Juanjo el 22/11/2026.
Ver claude/Detector_Entradas_Propias_2026-11-22.md. Gestión "estilo Juanjo": stop por estructura, objetivo lejano.
A) RUPTURA 4h con volumen (largos y cortos)
   - Vela 4h cerrada que rompe el máximo (mínimo) de las 30 velas 4h anteriores, con volumen >= 2× la media de 20 y rango previo < 12%.
   - Solo a favor de BTC (BTC 4h sobre su EMA50 → largos; debajo → cortos; BTC mismo sin filtro).
   - Entrada al cierre de la vela; stop 1 ATR(14, 4h) del otro lado del nivel roto; objetivo 4R; salida a los 5 días.
   - Prueba 219 días: +0,60R/op (n=589), +0,18R sin agosto. Rinde en tendencia, pierde en rango.
B) BANDERA 1h (largos y cortos)
   - Asta >= 4 ATR(14, 1h) en <= 12 velas; consolidación de 4–20 velas que retrocede < 50% del asta; cierre que rompe la bandera.
   - Solo a favor de BTC (mismo filtro). Entrada LÍMITE en el retesteo del techo (piso) de la bandera, hasta 12 h;
     stop 0,2 ATR del otro lado de la bandera; objetivo 3R; salida a las 72 h. Prueba: +0,61R (n=42, muestra chica).
Medición en R (costo 0,15%); si en la misma vela se tocan stop y objetivo, cuenta el stop. Todo error se registra y NO afecta al Shadow.
Comandos: /det (resultados) · /det_csv. Las alertas llegan por Telegram."""
import io, csv, bisect, numpy as np, pandas as pd, db, respiro

COSTO = 0.0015
EXCLUIR = ("ETH",)
R4_N, R4_RVOL, R4_RANGO, R4_RR, R4_HOLD_H = 30, 2.0, 0.12, 4.0, 120
B1_RR, B1_ESPERA_H, B1_HOLD_H = 3.0, 12, 72
D_MIN, D_MAX = 0.006, 0.08

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS det(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, par TEXT, setup TEXT, lado INTEGER,
                     entrada REAL, stop REAL, objetivo REAL, rvol REAL, estado TEXT, ts_ent TEXT, res TEXT, r REAL, ts_fin TEXT,
                     UNIQUE(par, ts, setup))""")

def _v4(df):
    v = df[["o", "h", "l", "c", "v"]].resample("4h").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"})
    n = df["c"].resample("4h").count()
    return v[n == 4].dropna()

def _atr(v, n=14):
    pc = v["c"].shift()
    tr = pd.concat([v["h"] - v["l"], (v["h"] - pc).abs(), (v["l"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def _zigzag(h, l, a, k=2.0):
    piv = []; modo = 0; ei = 0; ep = 0.0
    for i in range(len(h)):
        if not a[i] == a[i] or a[i] <= 0: continue
        if modo == 0: ei, ep, modo = i, h[i], 1; continue
        if modo == 1:
            if h[i] > ep: ei, ep = i, h[i]
            elif ep - l[i] >= k * a[i]: piv.append((ei, ep, 1, i)); modo = -1; ei, ep = i, l[i]
        else:
            if l[i] < ep: ei, ep = i, l[i]
            elif h[i] - ep >= k * a[i]: piv.append((ei, ep, -1, i)); modo = 1; ei, ep = i, h[i]
    return piv

def _ruptura4(v):
    """Última vela 4h cerrada. Devuelve (lado, entrada, stop, objetivo, rvol) o None."""
    h, l, c, vol = v["h"].values, v["l"].values, v["c"].values, v["v"].values
    i = len(c) - 1
    if i < R4_N + 21: return None
    a = _atr(v).values[i]
    rv = vol[i] / np.mean(vol[i - 20:i]) if np.mean(vol[i - 20:i]) > 0 else 0
    if rv < R4_RVOL: return None
    hi, lo = h[i - R4_N:i].max(), l[i - R4_N:i].min()
    if (hi - lo) / c[i] >= R4_RANGO: return None
    for sd, niv in ((1, hi), (-1, lo)):
        if sd * (c[i] - niv) > 0 and sd * (c[i - 1] - niv) <= 0:
            e = c[i]; st = niv - sd * a; d = sd * (e - st) / e
            if D_MIN <= d <= D_MAX: return sd, e, st, e + sd * R4_RR * abs(e - st), rv
    return None

def _bandera1(df):
    """Última vela 1h cerrada. Devuelve (lado, entrada_límite, stop, objetivo, rvol) o None."""
    h, l, c = df["h"].values, df["l"].values, df["c"].values
    a = _atr(df).values; i = len(c) - 1
    if i < 60: return None
    piv = _zigzag(h, l, a); conf = [p[3] for p in piv]
    m = bisect.bisect_right(conf, i)
    if m < 2: return None
    P = piv[m - 2:m]; ai = a[i]
    for sd in (1, -1):
        t = [p[2] * sd for p in P]
        if t != [-1, 1]: continue
        (bi, base, _, _), (ti, top, _, _) = P
        pole = abs(top - base)
        if not (pole >= 4 * ai and ti - bi <= 12 and 4 <= i - ti <= 20): continue
        fh = h[ti + 1:i].max() if sd == 1 else l[ti + 1:i].min()
        fl = l[ti + 1:i + 1].min() if sd == 1 else h[ti + 1:i + 1].max()
        if sd * (top - fl) < 0.5 * pole and sd * (c[i] - fh) > 0 and sd * (c[i - 1] - fh) <= 0:
            e = fh; st = fl - sd * 0.2 * ai; d = sd * (e - st) / e
            if D_MIN <= d <= D_MAX: return sd, e, st, e + sd * B1_RR * abs(e - st), 0.0
    return None

def _seguir(df, r):
    """Avanza una alerta con velas de 1h. Devuelve dict con cambios o None."""
    sd = r["lado"]; e, st, tg = r["entrada"], r["stop"], r["objetivo"]
    idx = df.index; H, L, C, O = df["h"].values, df["l"].values, df["c"].values, df["o"].values
    t0 = pd.Timestamp(r["ts"]); d = abs(e - st) / e
    out = {}
    if r["estado"] == "espera":                  # bandera: esperando el retesteo
        k0 = idx.searchsorted(t0, side="right")
        ia = None
        for k in range(k0, len(df)):
            if (idx[k] - t0).total_seconds() > B1_ESPERA_H * 3600: return dict(estado="cerrada", res="no_entró", r=None, ts_fin=str(idx[k]))
            if (sd == 1 and L[k] <= st) or (sd == -1 and H[k] >= st): return dict(estado="cerrada", res="invalidada", r=None, ts_fin=str(idx[k]))
            if (sd == 1 and L[k] <= e) or (sd == -1 and H[k] >= e): ia = k; break
        if ia is None: return None
        out = dict(estado="abierta", ts_ent=str(idx[ia])); ta = idx[ia]; k_ini = ia
        hold = B1_HOLD_H
    else:
        ta = pd.Timestamp(r["ts_ent"]); k_ini = idx.searchsorted(ta)   # Ruptura: primera vela 1h que abre al cierre de la 4h; Bandera: la vela del llenado
        hold = R4_HOLD_H if r["setup"] == "Ruptura 4h" else B1_HOLD_H
    for k in range(k_ini, len(df)):
        if (sd == 1 and L[k] <= st) or (sd == -1 and H[k] >= st):
            px = O[k] if sd * (O[k] - st) < 0 else st
            out.update(estado="cerrada", res="stop", r=(sd * (px / e - 1) - COSTO) / d, ts_fin=str(idx[k])); return out
        if (sd == 1 and H[k] >= tg) or (sd == -1 and L[k] <= tg):
            out.update(estado="cerrada", res="objetivo", r=(sd * (tg / e - 1) - COSTO) / d, ts_fin=str(idx[k])); return out
        if (idx[k] - ta).total_seconds() >= hold * 3600:
            out.update(estado="cerrada", res="tiempo", r=(sd * (C[k] / e - 1) - COSTO) / d, ts_fin=str(idx[k])); return out
    return out or None

def _aviso(m, setup, sd, e, st, tg, extra):
    d = abs(e - st) / e; lev10 = 0.10 / d; levmax = 1 / (3 * d)
    return (f"🎯 <b>{m}</b> · {setup} · {'LARGO' if sd == 1 else 'CORTO'}\n"
            f"{'Entrada ≈' if setup == 'Ruptura 4h' else 'Entrada LÍMITE (retesteo, 12 h)'} {e:.6g} · stop {st:.6g} (−{100*d:.1f}%) · objetivo {tg:.6g} ({100*abs(tg/e-1):.1f}%)\n"
            f"{extra}\nPara arriesgar 1% del capital con 10% de margen: ≈{lev10:.1f}x (máx. prudente {levmax:.0f}x)\n"
            f"<i>Solo aviso (prueba en vivo del detector propio); decidís vos.</i>")

def actualizar(enviar=None):
    """Llamar una vez por hora, después de procesar las monedas (usa respiro.CACHE)."""
    init_tabla()
    btc = respiro.CACHE.get("BTC")
    if btc is None or len(btc) < 300: return
    b4 = _v4(btc); btc_up = bool(b4["c"].iloc[-1] > b4["c"].ewm(span=50, adjust=False).mean().iloc[-1])
    avisos = []
    with db._lock, db._con() as con:
        for r in con.execute("SELECT * FROM det WHERE estado!='cerrada'").fetchall():
            df = respiro.CACHE.get(r["par"])
            if df is None: continue
            x = _seguir(df, dict(r))
            if x:
                cols = ", ".join(f"{k}=?" for k in x)
                con.execute(f"UPDATE det SET {cols} WHERE id=?", (*x.values(), r["id"]))
                if x.get("estado") == "abierta": avisos.append(f"✅ {r['par']} · Bandera 1h: se llenó la entrada límite {r['entrada']:.6g}.")
        abiertas = {(x[0], x[1]) for x in con.execute("SELECT par, setup FROM det WHERE estado!='cerrada'").fetchall()}
        ult1 = str(btc.index[-1])
        for m, df in respiro.CACHE.items():
            if m in EXCLUIR or df is None or len(df) < 300: continue
            # A) ruptura 4h (una vez por vela 4h nueva)
            try:
                v = _v4(df)
                t4 = str(v.index[-1])
                if (m, "Ruptura 4h") not in abiertas and len(v) > 60:
                    f = _ruptura4(v)
                    if f and (m == "BTC" or (f[0] == 1) == btc_up):
                        sd, e, st, tg, rv = f
                        t_ent = str(v.index[-1] + pd.Timedelta(hours=4))      # la entrada es al CIERRE de la vela 4h
                        cur = con.execute("INSERT OR IGNORE INTO det(ts,par,setup,lado,entrada,stop,objetivo,rvol,estado,ts_ent) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                          (t4, m, "Ruptura 4h", sd, e, st, tg, rv, "abierta", t_ent))
                        if cur.rowcount: avisos.append(_aviso(m, "Ruptura 4h", sd, e, st, tg, f"Rompe el rango de {R4_N} velas 4h con volumen {rv:.1f}× · objetivo 4R · salida a 5 días"))
            except Exception: pass
            # B) bandera 1h
            try:
                if (m, "Bandera 1h") not in abiertas and str(df.index[-1]) == ult1:
                    f = _bandera1(df)
                    if f and (m == "BTC" or (f[0] == 1) == btc_up):
                        sd, e, st, tg, _ = f
                        cur = con.execute("INSERT OR IGNORE INTO det(ts,par,setup,lado,entrada,stop,objetivo,rvol,estado) VALUES(?,?,?,?,?,?,?,?,?)",
                                          (ult1, m, "Bandera 1h", sd, e, st, tg, 0.0, "espera"))
                        if cur.rowcount: avisos.append(_aviso(m, "Bandera 1h", sd, e, st, tg, "Rompió la bandera: espero el retesteo · objetivo 3R · salida a 72 h"))
            except Exception: pass
    if enviar:
        for a in avisos[:8]: enviar(a)
        if len(avisos) > 8: enviar(f"… y {len(avisos) - 8} avisos más del detector (ver /det).")

def texto():
    init_tabla()
    with db._lock, db._con() as c:
        rows = [dict(x) for x in c.execute("SELECT * FROM det").fetchall()]
    out = ["🎯 <b>Detector propio</b> (solo observación)"]
    for s in ("Ruptura 4h", "Bandera 1h"):
        R = [x for x in rows if x["setup"] == s]
        cer = [x["r"] for x in R if x["estado"] == "cerrada" and x["r"] is not None]
        ab = sum(1 for x in R if x["estado"] == "abierta"); es = sum(1 for x in R if x["estado"] == "espera")
        ne = sum(1 for x in R if x["res"] in ("no_entró", "invalidada"))
        if not cer: out.append(f"{s}: {len(R)} alertas, {ab} abiertas, {es} esperando entrada, {ne} sin entrar · sin resultados aún."); continue
        w = sum(1 for r in cer if r > 0)
        out.append(f"{s}: {len(cer)} cerradas ({w} ganan, {100*w/len(cer):.0f}%) · {sum(cer)/len(cer):+.2f} R/op · total {sum(cer):+.1f} R · {ab} abiertas, {es} esperando, {ne} sin entrar")
    out.append("Referencia 219 días: Ruptura 4h +0,60R/op (sin agosto +0,18); Bandera 1h +0,61R (pocos casos).")
    return "\n".join(out)

def csv_todo():
    init_tabla()
    with db._lock, db._con() as c:
        cur = c.execute("SELECT * FROM det ORDER BY id"); cols = [x[0] for x in cur.description]; rows = cur.fetchall()
    f = io.StringIO(); w = csv.writer(f); w.writerow(cols); w.writerows([tuple(r) for r in rows])
    return f.getvalue().encode()
