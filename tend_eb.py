"""tend_eb.py — TENDENCIA en ETH y BTC para los bots acumuladores (solo observación, no opera). Aprobado por Juanjo el 24/11/2026.
Idea: los bots ETH y BTC operan SOLO con USDT, cada uno contra su moneda, siguiendo la tendencia (la moneda dura queda quieta).
Velas 4h armadas con las de 1h del Shadow (bloques UTC 00-04-08-12-16-20; solo bloques completos).
  Entrada (solo largos): cierre 4h > máximo de las 40 velas 4h anteriores, y estado de BTC ALCISTA o RANGO (estado_btc.py, cierre diario
  anterior). Con BTC BAJISTA la señal se registra pero no entra. Entra al cierre con TODO el USDT de la cartera (1x, sin apalancamiento).
  Stop: 4×ATR(14, 4h) al entrar; luego sube (nunca baja) con máximo anterior − 4×ATR y mínimo de las 20 velas 4h anteriores.
  Salida al tocar el stop (si la vela abre por debajo, sale a la apertura). Costo 0,15% ida y vuelta.
Cartera virtual: 1.000 USDT por moneda, interés compuesto. También muestra cuánta moneda compraría ese USDT (vs USDT quieto).
Prueba 03/2023–11/2026: ETH +72% en USDT (precio +18%), peor caída −28%; BTC +109% (precio +174%), peor caída −18%; ~1,5 ops/mes, 35% acierto.
Ver claude/Alternativas_Acumular_ETH_BTC_2026-11-24.md. Todo error se registra y NO afecta al Shadow."""
import io, csv, pandas as pd, db, respiro, engine

MONEDAS = ("ETH", "BTC")
ENT, EX, K, FEE, CAP0 = 40, 20, 4.0, 0.0015, 1000.0
ESTADOS_OK = ("alc", "rango")

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tend_eb(id INTEGER PRIMARY KEY AUTOINCREMENT, par TEXT, ts TEXT, precio REAL, stp REAL,
                     estado TEXT, entra INTEGER, cap_in REAL, cerrada INTEGER DEFAULT 0, ts_fin TEXT, px_fin REAL, ret REAL, cap_out REAL,
                     UNIQUE(par, ts))""")

def _v4(df):
    g = df[["o", "h", "l", "c"]].resample("4h")
    v = g.agg({"o": "first", "h": "max", "l": "min", "c": "last"})
    n = df["c"].resample("4h").count()
    return v[n == 4].dropna()

def _atr(v, n=14):
    pc = v["c"].shift()
    tr = pd.concat([v["h"] - v["l"], (v["h"] - pc).abs(), (v["l"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def _estado(t):
    return engine.ESTADO_BTC.get(str((t + pd.Timedelta(hours=4)).date()))

def _cap(con, m):
    x = con.execute("SELECT cap_out FROM tend_eb WHERE par=? AND entra=1 AND cerrada=1 ORDER BY ts_fin DESC, id DESC LIMIT 1", (m,)).fetchone()
    return float(x[0]) if x else CAP0

def actualizar(avisar=None):
    """Llamar una vez por hora, después de procesar las monedas (usa respiro.CACHE y engine.ESTADO_BTC)."""
    init_tabla()
    for m in MONEDAS:
        df = respiro.CACHE.get(m)
        if df is None or len(df) < 300: continue
        v = _v4(df)
        if len(v) < ENT + 20: continue
        v = v.assign(atr=_atr(v), hi=v["h"].rolling(ENT).max().shift(), xl=v["l"].rolling(EX).min().shift())
        ult = db.meta_get("tend_eb_ult_" + m, None)
        nuevas = [t for t in v.index if ult is None or str(t) > ult]
        if ult is None: nuevas = nuevas[-1:]
        if not nuevas: continue
        msgs = []
        with db._lock, db._con() as con:
            for t in nuevas:
                i = v.index.get_loc(t)
                # 1) posición abierta
                r = con.execute("SELECT * FROM tend_eb WHERE par=? AND cerrada=0 AND entra=1", (m,)).fetchone()
                if r is not None and str(t) > r["ts"]:
                    p = v.iloc[i - 1]
                    stp = max(r["stp"], float(p["h"]) - K * float(p["atr"]), float(v["xl"].iloc[i]))
                    if float(v["l"].iloc[i]) <= stp:
                        px = min(stp, float(v["o"].iloc[i])); ret = px / r["precio"] - 1 - FEE
                        cap = r["cap_in"] * (1 + ret)
                        con.execute("UPDATE tend_eb SET cerrada=1, ts_fin=?, px_fin=?, ret=?, cap_out=?, stp=? WHERE id=?",
                                    (str(t), px, ret, cap, stp, r["id"]))
                        msgs.append(f"📉 TEND {m}: SALE @ {px:,.2f} · {100*ret:+.1f}% · cartera {cap:,.0f} USDT")
                    else:
                        con.execute("UPDATE tend_eb SET stp=? WHERE id=?", (stp, r["id"]))
                    continue
                if r is not None: continue
                # 2) señal nueva al cierre de t
                c = float(v["c"].iloc[i]); hi = v["hi"].iloc[i]; a = float(v["atr"].iloc[i])
                if hi != hi or a != a or c <= hi: continue
                est = _estado(t); entra = 1 if est in ESTADOS_OK else 0
                cap = _cap(con, m) if entra else None
                try:
                    con.execute("INSERT INTO tend_eb(par,ts,precio,stp,estado,entra,cap_in,cerrada) VALUES(?,?,?,?,?,?,?,?)",
                                (m, str(t), c, c - K * a, est, entra, cap, 0 if entra else 1))
                    if entra: msgs.append(f"📈 TEND {m}: ENTRA (simulado) @ {c:,.2f} · stop {c - K*a:,.2f} · BTC {est} · cartera {cap:,.0f} USDT")
                except Exception: pass
            con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("tend_eb_ult_" + m, str(nuevas[-1])))
        if avisar:
            for x in msgs[-3:]: avisar(x + "\n(Shadow, sin dinero)")

def _filas():
    with db._lock, db._con() as c: return [dict(r) for r in c.execute("SELECT * FROM tend_eb ORDER BY ts, id").fetchall()]

def texto():
    init_tabla(); F = _filas()
    l = ["📈 <b>TENDENCIA ETH/BTC</b> (bots acumuladores, solo USDT, 1x, observación)"]
    if not F: return l[0] + "\nTodavía sin señales (necesita una ruptura del máximo de 40 velas 4h con BTC alcista o en rango)."
    for m in MONEDAS:
        X = [x for x in F if x["par"] == m]
        if not X: l.append(f"\n<b>{m}</b>: sin señales todavía."); continue
        E = [x for x in X if x["entra"]]; C = [x for x in E if x["cerrada"]]; ab = [x for x in E if not x["cerrada"]]
        cap = C[-1]["cap_out"] if C else CAP0
        px = respiro.CACHE.get(m)
        pnow = float(px["c"].iloc[-1]) if px is not None and len(px) else None
        flo = ""
        if ab and pnow:
            capf = ab[0]["cap_in"] * (pnow / ab[0]["precio"]); flo = f" · abierta desde {ab[0]['ts'][5:16]} @ {ab[0]['precio']:,.2f} (stop {ab[0]['stp']:,.2f}, ahora {100*(pnow/ab[0]['precio']-1):+.1f}%)"
            cap_v = capf
        else: cap_v = cap
        w = sum(1 for x in C if x["ret"] > 0)
        l.append(f"\n<b>{m}</b>: cartera {cap_v:,.0f} USDT ({100*(cap_v/CAP0-1):+.1f}%) · {len(C)} cerradas ({w} ganan){flo}"
                 f" · señales con BTC bajista (no entró): {sum(1 for x in X if not x['entra'])}")
        if pnow: l.append(f"  En {m}: compraría {cap_v/pnow:.4f} {m} (con 1.000 USDT hoy: {CAP0/pnow:.4f})")
    return "\n".join(l)

def csv_senales():
    init_tabla(); o = io.StringIO(); w = csv.writer(o)
    w.writerow(["par", "ts_utc", "precio", "stop", "estado_btc", "entra", "cap_in", "cerrada", "ts_fin", "px_fin", "ret_%", "cap_out"])
    for x in _filas():
        w.writerow([x["par"], x["ts"], x["precio"], x["stp"], x["estado"], x["entra"], x["cap_in"], x["cerrada"], x["ts_fin"], x["px_fin"],
                    None if x["ret"] is None else round(100 * x["ret"], 2), x["cap_out"]])
    return o.getvalue().encode()
