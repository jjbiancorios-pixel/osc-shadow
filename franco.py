"""franco.py — FRANCOTIRADOR (solo observación, no opera). Registra señales de alta probabilidad de UNA sola entrada (largo tras caída extrema)
y mide qué pasó en las 48 h siguientes con varias combinaciones de TP/SL. Todo error se registra y NO afecta al Shadow.
Variantes (todas largo, entrada al cierre de la vela 1h):  V1: RSI4h<=20 & z<=-2.5 & r12<=-3 · V2: RSI4h<=25 & z<=-2.5 & r12<=-4 · V3: RSI4h<=20 & z<=-3 & r12<=-3
  z = (cierre − media20)/desvío20 de velas 1h · r12 = caída de 12 h en unidades de ATR(14) (ATR% = media simple del rango verdadero / precio).
Mismo moneda y misma variante: máx. una señal cada 48 h. Hallazgo del 29/10 (200 días, 40 monedas): V1 ≈ 95% de acierto con TP 1,2% / SL 4%."""
import json, numpy as np, pandas as pd, db

VARIANTES = {"V1": (20, -2.5, -3.0), "V2": (25, -2.5, -4.0), "V3": (20, -3.0, -3.0)}
SALIDAS = [(0.8, 3.0), (1.2, 4.0), (1.6, 4.0), (2.0, 5.0), (3.0, 5.0)]   # (TP %, SL %)
HORIZONTE = 48
SEP_H = 48

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS franco(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, par TEXT, variante TEXT, precio REAL,
                     rsi4 REAL, z REAL, r12 REAL, btc_dd REAL, cerrada INTEGER DEFAULT 0, mfe REAL, mae REAL, res TEXT, UNIQUE(par, variante, ts))""")

def caracteristicas(df, rsi4):
    """df: velas 1h cerradas (o,h,l,c,v). rsi4: serie del RSI 4h alineada (engine.prep). Devuelve DataFrame con z, r12."""
    c = df["c"]; tr = pd.concat([df.h - df.l, (df.h - c.shift()).abs(), (df.l - c.shift()).abs()], axis=1).max(axis=1)
    atrp = tr.rolling(14).mean() / c * 100
    z = (c - c.rolling(20).mean()) / c.rolling(20).std()
    r12 = (c / c.shift(12) - 1) * 100 / atrp
    return pd.DataFrame({"rsi4": np.asarray(rsi4), "z": z.values, "r12": r12.values}, index=df.index)

def variantes_en(f, i):
    r, z, q = f["rsi4"].iloc[i], f["z"].iloc[i], f["r12"].iloc[i]
    if not (r == r and z == z and q == q): return []
    return [v for v, (a, b, d) in VARIANTES.items() if r <= a and z <= b and q <= d]

def resolver(df, i):
    """Resultado de entrar al cierre de la vela i, mirando las HORIZONTE velas siguientes. None si todavía no están todas.
    {'t_s': [res, pnl%]} con res 'tp'|'sl'|'to'; si TP y SL caen en la misma vela se cuenta SL (conservador). Más mfe/mae en %."""
    if i + HORIZONTE >= len(df): return None
    e = df["c"].iloc[i]; H = df["h"].values[i + 1:i + 1 + HORIZONTE]; L = df["l"].values[i + 1:i + 1 + HORIZONTE]; cf = df["c"].values[i + HORIZONTE]
    out = {}
    for t, s in SALIDAS:
        tp = np.where(H >= e * (1 + t / 100))[0]; sl = np.where(L <= e * (1 - s / 100))[0]
        ft = tp[0] if len(tp) else 10 ** 6; fs = sl[0] if len(sl) else 10 ** 6
        if ft < fs: out[f"{t}_{s}"] = ["tp", t]
        elif fs < 10 ** 6: out[f"{t}_{s}"] = ["sl", -s]
        else: out[f"{t}_{s}"] = ["to", float((cf / e - 1) * 100)]
    out["mfe"] = float((H.max() / e - 1) * 100); out["mae"] = float((L.min() / e - 1) * 100)
    return out

def actualizar(par, df, rsi4, btc_dd=None, avisar=None):
    """Llamar una vez por moneda y por hora con las velas cerradas. Registra señales nuevas (solo la última vela) y resuelve las abiertas."""
    init_tabla()
    f = caracteristicas(df, rsi4); i = len(df) - 1; ts = str(df.index[i])
    with db._lock, db._con() as c:
        for v in variantes_en(f, i):
            ult = c.execute("SELECT ts FROM franco WHERE par=? AND variante=? ORDER BY ts DESC LIMIT 1", (par, v)).fetchone()
            if ult and (pd.Timestamp(ts) - pd.Timestamp(ult[0])).total_seconds() < SEP_H * 3600: continue
            try:
                c.execute("INSERT INTO franco(ts,par,variante,precio,rsi4,z,r12,btc_dd) VALUES(?,?,?,?,?,?,?,?)",
                          (ts, par, v, float(df['c'].iloc[i]), float(f.rsi4.iloc[i]), float(f.z.iloc[i]), float(f.r12.iloc[i]), btc_dd))
                if avisar and v == "V1":
                    avisar(f"🎯 FRANCOTIRADOR {v} · {par} LARGO @ {df['c'].iloc[i]:.6g} · RSI4h {f.rsi4.iloc[i]:.0f} · z {f.z.iloc[i]:.1f} · caída12h {f.r12.iloc[i]:.1f} ATR (solo observación)")
            except Exception: pass
        for r in c.execute("SELECT id, ts, variante FROM franco WHERE par=? AND cerrada=0", (par,)).fetchall():
            t0 = pd.Timestamp(r["ts"])
            if t0 not in df.index: continue
            res = resolver(df, df.index.get_loc(t0))
            if res is None: continue
            c.execute("UPDATE franco SET cerrada=1, mfe=?, mae=?, res=? WHERE id=?", (res["mfe"], res["mae"], json.dumps(res), r["id"]))

def _filas():
    with db._lock, db._con() as c: return [dict(r) for r in c.execute("SELECT * FROM franco ORDER BY ts").fetchall()]

def texto():
    init_tabla(); F = _filas()
    if not F: return "🎯 Francotirador: todavía sin señales."
    l = [f"🎯 <b>Francotirador</b> (solo observación) · {len(F)} señales, {sum(1 for x in F if x['cerrada'])} resueltas, {sum(1 for x in F if not x['cerrada'])} en curso"]
    for v in VARIANTES:
        S = [x for x in F if x["variante"] == v]; R = [x for x in S if x["cerrada"]]
        l.append(f"\n<b>{v}</b> (RSI4h≤{VARIANTES[v][0]}, z≤{VARIANTES[v][1]}, caída12h≥{abs(VARIANTES[v][2])} ATR): {len(S)} señales, {len(R)} resueltas")
        if not R: continue
        for t, s in SALIDAS:
            rs = [json.loads(x["res"])[f"{t}_{s}"] for x in R]; n = len(rs); w = sum(1 for a in rs if a[0] == "tp"); sl = sum(1 for a in rs if a[0] == "sl")
            ev = np.mean([a[1] for a in rs]) - 0.1
            l.append(f"  TP {t}% / SL {s}%: acierto {100*w/n:.0f}% · stops {sl} · esperanza {ev:+.2f}% precio ({ev*10:+.1f}% del margen a 10x)")
    ab = [x for x in F if not x["cerrada"]]
    if ab: l.append("\nEn curso: " + ", ".join(f"{x['par']}({x['variante']})" for x in ab[-10:]))
    return "\n".join(l)

def texto_abiertos():
    init_tabla(); F = [x for x in _filas() if not x["cerrada"]]
    return "\n".join(f"{x['ts'][5:16]} UTC {x['variante']} {x['par']} @ {x['precio']:.6g} · RSI4h {x['rsi4']:.0f} z {x['z']:.1f} r12 {x['r12']:.1f}" for x in F) or "Sin señales en curso."

def csv_senales():
    init_tabla(); import io, csv; o = io.StringIO(); w = csv.writer(o)
    w.writerow(["ts_utc", "par", "variante", "precio", "rsi4", "z", "r12", "btc_dd", "cerrada", "mfe_%", "mae_%"] + [f"res_{t}_{s}" for t, s in SALIDAS])
    for x in _filas():
        r = json.loads(x["res"]) if x["res"] else {}
        w.writerow([x["ts"], x["par"], x["variante"], x["precio"], x["rsi4"], x["z"], x["r12"], x["btc_dd"], x["cerrada"], x["mfe"], x["mae"]] + [(r.get(f"{t}_{s}") or ["", ""])[0] for t, s in SALIDAS])
    return o.getvalue().encode()
