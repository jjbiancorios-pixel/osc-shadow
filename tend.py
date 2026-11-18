"""tend.py — "TEND": seguimiento de tendencia, solo largos (solo observación, no opera). Aprobado por Juanjo el 17/11/2026.
Velas de 4h armadas con las de 1h del Shadow (bloques UTC 00-04-08-12-16-20; solo bloques completos).
  Entrada: cierre 4h > máximo de las 40 velas 4h anteriores y BTC con cambio de 7 días (42 velas 4h) > 0. Entra al cierre.
  Stop inicial: 4×ATR(14, 4h). Luego sube (nunca baja) con: máximo anterior − 4×ATR, y mínimo de las 20 velas 4h anteriores.
  Salida al tocar el stop (si la vela abre por debajo, sale a la apertura). Costo 0,15% ida y vuelta (restado en R).
Cartera virtual: capital inicial 1.000 USDT, riesgo 1% del capital por operación, tope 20 abiertas a la vez (≈10% del capital por posición).
Freno semanal: si lo realizado en la semana (lunes 00 UTC) llega a −5% del capital de inicio de semana, no abre nuevas hasta el lunes.
Las señales sin cupo o con freno se registran igual (cartera=0) para medir la estrategia completa. ETH excluido (igual que el Shadow).
Prueba 219 días (39 monedas): 342 ops, 42% acierto, +0,36 R/op; cartera 1%/20 ≈ +2,2%/semana, peor caída −25% (sim optimista).
Pendiente: filtro de funding extremo (el Shadow no guarda funding todavía).
Todo error se registra y NO afecta al Shadow. Ver claude/Plan_30000_y_Tendencia_2026-11-17.md del proyecto."""
import io, csv, numpy as np, pandas as pd, db, respiro

ENT = 40
EX = 20
K = 4.0
FEE = 0.0015
CAP0 = 1000.0
RIESGO = 0.01
TOPE = 20
FRENO = -0.05
EXCLUIR = ("ETH",)

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tend(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, par TEXT, precio REAL, r0 REAL, stp REAL,
                     cartera INTEGER, motivo TEXT, sz REAL, cerrada INTEGER DEFAULT 0, ts_fin TEXT, px_fin REAL, r REAL, usdt REAL,
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

def _capital(con):
    x = con.execute("SELECT COALESCE(SUM(usdt),0) FROM tend WHERE cerrada=1 AND cartera=1").fetchone()[0]
    return CAP0 + float(x)

def _freno(con, t, cap):
    lunes = (t - pd.Timedelta(days=t.weekday())).normalize()
    k = "tend_sem_" + str(lunes.date())
    x = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    if x is None: con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(cap))); ini = cap
    else: ini = float(x[0])
    sem = con.execute("SELECT COALESCE(SUM(usdt),0) FROM tend WHERE cerrada=1 AND cartera=1 AND ts_fin>=?", (str(lunes),)).fetchone()[0]
    return float(sem) <= FRENO * float(ini)

def actualizar():
    """Llamar una vez por hora, después de procesar todas las monedas (usa respiro.CACHE)."""
    init_tabla()
    btc = respiro.CACHE.get("BTC")
    if btc is None or len(btc) < 300: return
    b4 = _v4(btc)
    if len(b4) < 50: return
    ult = db.meta_get("tend_ult4", None)
    nuevas = [t for t in b4.index if ult is None or str(t) > ult]
    if ult is None: nuevas = nuevas[-1:]          # arranque: solo la última vela 4h cerrada
    if not nuevas: return
    V = {}
    for m, df in respiro.CACHE.items():
        if m in EXCLUIR or df is None or len(df) < 300: continue
        try:
            v = _v4(df)
            if len(v) < ENT + 20: continue
            v = v.assign(atr=_atr(v), hi=v["h"].rolling(ENT).max().shift(), xl=v["l"].rolling(EX).min().shift())
            V[m] = v
        except Exception: pass
    b7 = b4["c"] / b4["c"].shift(42) - 1
    with db._lock, db._con() as con:
        for t in nuevas:
            # 1) gestionar abiertas en la vela t
            for r in con.execute("SELECT id, par, ts, stp FROM tend WHERE cerrada=0").fetchall():
                v = V.get(r["par"])
                if v is None or t not in v.index or str(t) <= r["ts"]: continue
                i = v.index.get_loc(t); stp = r["stp"]
                p = v.iloc[i - 1]
                stp = max(stp, float(p["h"]) - K * float(p["atr"]), float(v["xl"].iloc[i]))
                if float(v["l"].iloc[i]) <= stp:
                    px = min(stp, float(v["o"].iloc[i]))
                    x = con.execute("SELECT precio, r0, sz FROM tend WHERE id=?", (r["id"],)).fetchone()
                    R = ((px / x["precio"] - 1) - FEE) / x["r0"]
                    con.execute("UPDATE tend SET cerrada=1, ts_fin=?, px_fin=?, r=?, usdt=?, stp=? WHERE id=?",
                                (str(t), px, R, (x["sz"] or 0) * R, stp, r["id"]))
                else:
                    con.execute("UPDATE tend SET stp=? WHERE id=?", (stp, r["id"]))
            # 2) señales nuevas al cierre de t
            if not (b7.get(t, 0) > 0): continue
            cap = _capital(con); frenado = _freno(con, t, cap)
            abiertas = con.execute("SELECT COUNT(*) FROM tend WHERE cerrada=0 AND cartera=1").fetchone()[0]
            con_pos = {x[0] for x in con.execute("SELECT par FROM tend WHERE cerrada=0").fetchall()}
            for m, v in V.items():
                if m in con_pos or t not in v.index: continue
                i = v.index.get_loc(t); c = float(v["c"].iloc[i]); hi = v["hi"].iloc[i]; a = float(v["atr"].iloc[i])
                if hi != hi or a != a or c <= hi: continue
                r0 = K * a / c
                if frenado: en, mot = 0, "freno_semanal"
                elif abiertas >= TOPE: en, mot = 0, "sin_cupo"
                else: en, mot = 1, "ok"
                try:
                    con.execute("INSERT INTO tend(ts,par,precio,r0,stp,cartera,motivo,sz) VALUES(?,?,?,?,?,?,?,?)",
                                (str(t), m, c, r0, c * (1 - r0), en, mot, cap * RIESGO if en else None))
                    abiertas += en
                except Exception: pass
        con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("tend_ult4", str(nuevas[-1])))

def _filas():
    with db._lock, db._con() as c: return [dict(r) for r in c.execute("SELECT * FROM tend ORDER BY ts, id").fetchall()]

def texto():
    init_tabla(); F = _filas()
    if not F: return "📈 TEND (tendencia, solo largos): todavía sin señales."
    dias = max(1.0, (pd.Timestamp.utcnow().tz_localize(None) - pd.Timestamp(F[0]["ts"])).total_seconds() / 86400)
    R = [x for x in F if x["cerrada"]]; ab = [x for x in F if not x["cerrada"]]
    l = [f"📈 <b>TEND</b> (tendencia, solo largos, observación) · desde {F[0]['ts'][:10]} · {len(F)} señales"]
    if R:
        w = sum(1 for x in R if x["r"] > 0); tot = sum(x["r"] for x in R)
        l.append(f"\nTodas: {len(R)} cerradas ({w} ganan, {100*w/len(R):.0f}%), {len(ab)} abiertas · {tot/len(R):+.2f} R por op. · total {tot:+.1f} R")
    C = [x for x in F if x["cartera"] == 1]; Cc = [x for x in C if x["cerrada"]]
    cap = CAP0 + sum(x["usdt"] for x in Cc); eq = CAP0; pk = CAP0; dd = 0.0
    for x in sorted(Cc, key=lambda y: y["ts_fin"]): eq += x["usdt"]; pk = max(pk, eq); dd = min(dd, eq / pk - 1)
    sem = (cap / CAP0) ** (7 / dias) - 1 if cap > 0 else -1
    l.append(f"\n<b>Cartera</b> (1.000 USDT, riesgo 1%, tope {TOPE}): {len(C)} ops, {sum(1 for x in C if not x['cerrada'])} abiertas · "
             f"capital {cap:.1f} ({100*(cap/CAP0-1):+.1f}%; ≈{100*sem:+.2f}%/semana) · peor caída {100*dd:.1f}% · "
             f"sin cupo {sum(1 for x in F if x['motivo']=='sin_cupo')} · frenadas {sum(1 for x in F if x['motivo']=='freno_semanal')}")
    if ab: l.append("Abiertas: " + ", ".join(f"{x['par']} @{x['precio']:.4g} (stop {x['stp']:.4g})" for x in ab[:20]))
    return "\n".join(l)

def csv_senales():
    init_tabla(); o = io.StringIO(); w = csv.writer(o)
    w.writerow(["ts_utc", "par", "precio", "riesgo_%", "stop_actual", "en_cartera", "motivo", "riesgo_usdt", "cerrada", "ts_fin", "px_fin", "R", "usdt"])
    for x in _filas():
        w.writerow([x["ts"], x["par"], x["precio"], round(100 * x["r0"], 2), x["stp"], x["cartera"], x["motivo"], x["sz"], x["cerrada"],
                    x["ts_fin"], x["px_fin"], None if x["r"] is None else round(x["r"], 3), None if x["usdt"] is None else round(x["usdt"], 2)])
    return o.getvalue().encode()
