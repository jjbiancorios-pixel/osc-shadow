"""patrones.py — registro de REFERENCIA (sin operar) de patrones de BTC en velas 1h cerradas (datos que ya baja mstrx_datos).
Detecta: doble piso/W, triple piso, banderín alcista y ruptura de rango de 48 h. Guarda cada señal y, pasadas 24 h, lo que pasó después.
No abre ni cierra nada: solo cuenta cuántas veces BTC subió o cayó tras cada patrón. Todo error se registra y NO afecta al Shadow."""
import numpy as np, db

PAR, TF, K = "BTC_USDT", "60M", 6
PATRONES = ("doble piso/W", "triple piso", "banderín alcista", "ruptura 48h")
VENTANA = 400      # velas 1h que se miran en cada pasada
SEP_MIN = 12       # horas mínimas entre dos señales del mismo patrón

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS patrones_btc(patron TEXT, ts INTEGER, precio REAL, origen TEXT,
                     up4 REAL, dd24 REAL, r24 REAL, ok INTEGER, PRIMARY KEY(patron, ts))""")

def _velas(limit):
    with db._lock, db._con() as c:
        r = c.execute("SELECT ts,o,h,l,c FROM mstrx_velas WHERE par=? AND tf=? ORDER BY ts DESC LIMIT ?", (PAR, TF, limit)).fetchall()
    r = r[::-1]
    if not r: return None
    a = np.array(r, dtype=float)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]

def _pivotes(l):
    n = len(l)
    return [i for i in range(K, n - K) if l[i] == l[i-K:i+K+1].min()]

def _doble(piv, h, l, c, t, tol=0.01, minsep=8, maxsep=96, triple=False):
    ps = [i for i in piv if i + K <= t and t - (i + K) <= 24]
    if not ps: return False
    i2 = ps[-1]
    for i1 in reversed([i for i in piv if i + K <= t and minsep <= i2 - i <= maxsep]):
        if abs(l[i2] - l[i1]) / l[i1] <= tol:
            neck = h[i1:i2+1].max()
            if neck >= max(l[i1], l[i2]) * 1.015 and c[t] > neck * 0.998 and c[t-1] <= neck * 0.998:
                if not triple: return True
                if any(i + K <= t and minsep <= i1 - i <= maxsep and abs(l[i] - l[i1]) / l[i1] <= tol * 1.2 for i in piv): return True
    return False

def _banderin(h, l, c, t):
    for pole in (6, 12, 24):
        for fl in (6, 12, 24):
            a = t - fl
            if a - pole < 0: continue
            base = l[a-pole:a].min(); top = h[a-pole:a].max()
            if top / base - 1 < 0.03: continue
            hh = h[a:t].max(); ll = l[a:t].min(); ph = top - base
            if (hh - ll) <= 0.4 * ph and (top - ll) <= 0.5 * ph and c[t] > hh and c[t-1] <= hh: return True
    return False

def _ruptura(h, c, t):
    return c[t] > h[t-48:t].max() and c[t-1] <= h[t-49:t-1].max()

def _senales(ts, o, h, l, c, desde_idx):
    piv = _pivotes(l); out = []
    for t in range(max(desde_idx, 220), len(c)):
        for nom, f in (("doble piso/W", lambda: _doble(piv, h, l, c, t)), ("triple piso", lambda: _doble(piv, h, l, c, t, triple=True)),
                       ("banderín alcista", lambda: _banderin(h, l, c, t)), ("ruptura 48h", lambda: _ruptura(h, c, t))):
            try:
                if f(): out.append((nom, int(ts[t]), float(c[t])))
            except Exception: pass
    return out

def _resultado(h, l, c, t):
    up4 = (h[t+1:t+5].max() / c[t] - 1) * 100
    dd24 = (l[t+1:t+25].min() / c[t] - 1) * 100
    r24 = (c[t+24] / c[t] - 1) * 100
    u = np.where(h[t+1:t+25] >= c[t] * 1.01)[0]; d = np.where(l[t+1:t+25] <= c[t] * 0.985)[0]
    ok = 1 if (len(u) and (not len(d) or u[0] < d[0])) else 0
    return up4, dd24, r24, ok

def actualizar():
    """Una pasada horaria: agrega señales nuevas (la primera vez rellena el historial disponible) y completa resultados de las de hace más de 24 h."""
    init_tabla(); v = _velas(3000)
    if v is None or len(v[0]) < 260: return 0
    ts, o, h, l, c = v; n = len(c); nuevas = 0
    ult = db.meta_get("pat_ult_ts")
    primera = ult is None
    desde = 220 if primera else int(np.searchsorted(ts, int(ult), side="right"))
    if desde < n:
        sig = _senales(ts, o, h, l, c, desde)
        with db._lock, db._con() as con:
            for nom in PATRONES:
                ult_ts = con.execute("SELECT MAX(ts) FROM patrones_btc WHERE patron=?", (nom,)).fetchone()[0]
                for p, t_, pr in sig:
                    if p != nom: continue
                    if ult_ts is not None and t_ - ult_ts < SEP_MIN * 3600 * 1000: continue
                    con.execute("INSERT OR IGNORE INTO patrones_btc(patron,ts,precio,origen) VALUES(?,?,?,?)", (nom, t_, pr, "historial" if primera else "vivo"))
                    ult_ts = t_; nuevas += 1
    db.meta_set("pat_ult_ts", int(ts[-1]))
    # resultados de señales con 24 h cumplidas
    idx = {int(x): i for i, x in enumerate(ts)}
    with db._lock, db._con() as con:
        pend = con.execute("SELECT patron,ts FROM patrones_btc WHERE up4 IS NULL").fetchall()
        for p, t_ in pend:
            i = idx.get(int(t_))
            if i is None or i + 24 >= n: continue
            up4, dd24, r24, ok = _resultado(h, l, c, i)
            con.execute("UPDATE patrones_btc SET up4=?,dd24=?,r24=?,ok=? WHERE patron=? AND ts=?", (up4, dd24, r24, ok, p, t_))
    return nuevas

def texto():
    init_tabla(); v = _velas(3000)
    out = ["<b>Patrones de BTC (solo referencia, no operan)</b>"]
    if v is not None and len(v[0]) > 300:
        ts, o, h, l, c = v; n = len(c); idx = range(220, n - 25)
        r = np.array([_resultado(h, l, c, t) for t in idx])
        out.append(f"Referencia, todas las horas ({len(r)}): sube &gt;0,5% en 4h {np.mean(r[:,0]>0.5)*100:.0f}% | cae &gt;2% en 24h {np.mean(r[:,1]<-2)*100:.0f}% | sube 1% antes de caer 1,5%: {np.mean(r[:,3])*100:.0f}%")
    with db._lock, db._con() as con:
        for nom in PATRONES:
            rows = con.execute("SELECT origen,up4,dd24,r24,ok FROM patrones_btc WHERE patron=? ORDER BY ts", (nom,)).fetchall()
            t = len(rows); done = [x for x in rows if x[1] is not None]; vivo = sum(1 for x in rows if x[0] == "vivo")
            if not done: out.append(f"<b>{nom}</b>: {t} señales ({vivo} en vivo), sin resultados aún"); continue
            up = sum(1 for x in done if x[1] > 0.5) / len(done) * 100; dd = sum(1 for x in done if x[2] < -2) / len(done) * 100
            ok = sum(x[4] for x in done) / len(done) * 100
            out.append(f"<b>{nom}</b>: {t} señales ({vivo} en vivo, {len(done)} con resultado) | sube &gt;0,5%/4h {up:.0f}% | cae &gt;2%/24h {dd:.0f}% | +1% antes de −1,5%: {ok:.0f}%")
    return "\n".join(out)
