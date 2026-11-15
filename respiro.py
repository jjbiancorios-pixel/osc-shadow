"""respiro.py — señal "RESPIRO DE BTC" (solo observación, no opera). Idea de Juanjo del 15/11/2026.
Cuando BTC viene de una caída fuerte y empieza a "respirar" (rebota sin hacer mínimos nuevos), se anotan las 3 monedas que
rebotan MÁS fuerte que BTC (y, para comparar, las 3 más débiles) como si se entrara LARGO al cierre de la vela 1h.
Después se mide qué pasó en los 7 días siguientes con varias combinaciones de TP y stop (stop = liquidación aprox. a 20x, 10x y 3x).
Todo error se registra y NO afecta al Shadow.

Variantes (BTC, velas 1h cerradas):
  R3: en las últimas 48 h BTC estuvo ≥7% bajo su máx. de 7 días; hoy rebota entre +3% y +6% desde su mínimo de 48 h;
      sin mínimo nuevo en las últimas 6 h. (La única versión que ganó en la prueba de 219 días, con solo 14 eventos.)
  R2: igual pero rebote entre +2% y +5% (versión que perdió en la prueba; se registra para comparar).
Máx. un evento por variante cada 24 h. Fuerza de una moneda = su rebote desde su mínimo (desde la hora del mínimo de BTC) − rebote de BTC.
Prueba previa (10/04–15/11, 39 monedas): R3 + 3 más fuertes, TP 2% a 20x: 79% de acierto, +0,5% del precio por operación (14 eventos,
13 de junio–julio: NO es evidencia suficiente). R2: perdió. Ver claude/Prueba_Estrategia_Respiro_BTC_2026-11-15.md del proyecto."""
import json, numpy as np, pandas as pd, db

VARIANTES = {"R3": dict(caida=7.0, reb_min=3.0, reb_max=6.0), "R2": dict(caida=7.0, reb_min=2.0, reb_max=5.0)}
SIN_MIN_H = 6          # horas sin mínimo nuevo de BTC
VENTANA_H = 48         # ventana para la caída y el mínimo de BTC
SEP_H = 24             # máx. un evento por variante cada tantas horas
TOP = 3
SALIDAS = [(1.0, 4.5), (2.0, 4.5), (3.0, 4.5), (2.0, 9.5), (3.0, 9.5), (2.0, 32.8), (3.0, 32.8)]   # (TP %, stop %): 4,5≈liq 20x · 9,5≈10x · 32,8≈3x
HORIZONTE = 168        # 7 días
EXCLUIR = ("BTC", "ETH")
FEE_IDA_VUELTA = 0.1   # % del precio (0,05% por lado)

CACHE = {}             # moneda -> DataFrame de velas 1h cerradas (lo llena main.procesar_moneda en cada pasada)

def guardar(m, df):
    CACHE[m] = df

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS respiro(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, variante TEXT, par TEXT, grupo TEXT,
                     rango INTEGER, precio REAL, fuerza REAL, btc_reb REAL, btc_caida REAL, cerrada INTEGER DEFAULT 0, mfe REAL, mae REAL,
                     res TEXT, UNIQUE(variante, par, ts))""")

def _evento_btc(btc, v):
    """Devuelve (ts_minimo, rebote %, caída máx. %) si la última vela de BTC cumple la variante; si no, None."""
    c = btc["c"]
    if len(c) < 168 + VENTANA_H: return None
    dd = (c / c.rolling(168).max() - 1) * 100
    w = c.iloc[-VENTANA_H:]
    caida = float(dd.iloc[-VENTANA_H:].min())
    if caida > -v["caida"]: return None
    t_min = w.idxmin(); lo = float(w.min())
    if (w.index[-1] - t_min).total_seconds() < SIN_MIN_H * 3600: return None
    reb = (float(c.iloc[-1]) / lo - 1) * 100
    if not (v["reb_min"] <= reb <= v["reb_max"]): return None
    return t_min, reb, caida

def _fuerzas(t_min, reb_btc, ts):
    out = []
    for m, df in CACHE.items():
        if m in EXCLUIR or df is None or len(df) == 0 or str(df.index[-1]) != ts: continue
        tramo = df[df.index >= t_min]
        if len(tramo) < 2: continue
        f = (float(df["c"].iloc[-1]) / float(tramo["l"].min()) - 1) * 100 - reb_btc
        out.append((f, m, float(df["c"].iloc[-1])))
    out.sort(reverse=True)
    return out

def resolver(df, i):
    """Resultado de entrar al cierre de la vela i, mirando hasta HORIZONTE velas. None si todavía no están todas.
    Si TP y stop caen en la misma vela se cuenta el stop (conservador). Valores en % del precio."""
    if i + HORIZONTE >= len(df): return None
    e = df["c"].iloc[i]; H = df["h"].values[i + 1:i + 1 + HORIZONTE]; L = df["l"].values[i + 1:i + 1 + HORIZONTE]; cf = df["c"].values[i + HORIZONTE]
    out = {}
    for t, s in SALIDAS:
        tp = np.where(H >= e * (1 + t / 100))[0]; sl = np.where(L <= e * (1 - s / 100))[0]
        ft = tp[0] if len(tp) else 10 ** 6; fs = sl[0] if len(sl) else 10 ** 6
        if ft < fs: out[f"{t}_{s}"] = ["tp", t, int(ft) + 1]
        elif fs < 10 ** 6: out[f"{t}_{s}"] = ["sl", -s, int(fs) + 1]
        else: out[f"{t}_{s}"] = ["to", float((cf / e - 1) * 100), HORIZONTE]
    out["mfe"] = float((H.max() / e - 1) * 100); out["mae"] = float((L.min() / e - 1) * 100)
    return out

def actualizar(avisar=None):
    """Llamar una vez por hora, después de procesar todas las monedas. Registra eventos nuevos y resuelve los abiertos."""
    init_tabla()
    btc = CACHE.get("BTC")
    if btc is None or len(btc) == 0: return
    ts = str(btc.index[-1])
    with db._lock, db._con() as c:
        for k, v in VARIANTES.items():
            ev = _evento_btc(btc, v)
            if not ev: continue
            ult = c.execute("SELECT ts FROM respiro WHERE variante=? ORDER BY ts DESC LIMIT 1", (k,)).fetchone()
            if ult and (pd.Timestamp(ts) - pd.Timestamp(ult[0])).total_seconds() < SEP_H * 3600: continue
            t_min, reb, caida = ev
            F = _fuerzas(t_min, reb, ts)
            if len(F) < 2 * TOP: continue
            filas = [("fuerte", i + 1, x) for i, x in enumerate(F[:TOP])] + [("debil", len(F) - TOP + i + 1, x) for i, x in enumerate(F[-TOP:])]
            for grupo, rango, (f, m, p) in filas:
                try:
                    c.execute("INSERT INTO respiro(ts,variante,par,grupo,rango,precio,fuerza,btc_reb,btc_caida) VALUES(?,?,?,?,?,?,?,?,?)",
                              (ts, k, m, grupo, rango, p, f, reb, caida))
                except Exception: pass
            if avisar and k == "R3":
                avisar(f"🌬️ RESPIRO BTC {k} (solo observación): BTC rebota {reb:+.1f}% desde su mínimo tras caer {caida:.1f}% · "
                       f"más fuertes: " + ", ".join(f"{m} @ {p:.6g} ({f:+.1f}%)" for f, m, p in F[:TOP]))
        for r in c.execute("SELECT id, ts, par FROM respiro WHERE cerrada=0").fetchall():
            df = CACHE.get(r["par"]); t0 = pd.Timestamp(r["ts"])
            if df is None or t0 not in df.index: continue
            res = resolver(df, df.index.get_loc(t0))
            if res is None: continue
            c.execute("UPDATE respiro SET cerrada=1, mfe=?, mae=?, res=? WHERE id=?", (res["mfe"], res["mae"], json.dumps(res), r["id"]))

def _filas():
    with db._lock, db._con() as c: return [dict(r) for r in c.execute("SELECT * FROM respiro ORDER BY ts, variante, grupo, rango").fetchall()]

def texto():
    init_tabla(); F = _filas()
    if not F: return "🌬️ Respiro de BTC: todavía sin eventos."
    ev = sorted(set((x["variante"], x["ts"]) for x in F))
    l = [f"🌬️ <b>Respiro de BTC</b> (solo observación) · {len(ev)} eventos, {sum(1 for x in F if x['cerrada'])} operaciones resueltas, {sum(1 for x in F if not x['cerrada'])} en curso (se resuelven a los 7 días)"]
    for v in VARIANTES:
        for g in ("fuerte", "debil"):
            R = [x for x in F if x["variante"] == v and x["grupo"] == g and x["cerrada"]]
            n_ev = len(set(x["ts"] for x in F if x["variante"] == v))
            l.append(f"\n<b>{v} · 3 más {'fuertes' if g == 'fuerte' else 'débiles'}</b>: {n_ev} eventos, {len(R)} resueltas")
            if not R: continue
            for t, s in SALIDAS:
                rs = [json.loads(x["res"])[f"{t}_{s}"] for x in R]; n = len(rs); w = sum(1 for a in rs if a[0] == "tp")
                ev_p = float(np.mean([a[1] for a in rs])) - FEE_IDA_VUELTA
                ap = {4.5: 20, 9.5: 10, 32.8: 3}.get(s, 1)
                l.append(f"  TP {t}% / stop {s}% ({ap}x): acierto {100*w/n:.0f}% · {ev_p:+.2f}% del precio por op. ({ev_p*ap:+.1f}% del margen)")
    ab = [x for x in F if not x["cerrada"] and x["grupo"] == "fuerte"]
    if ab: l.append("\nEn curso (fuertes): " + ", ".join(f"{x['par']}({x['variante']} {x['ts'][5:13]})" for x in ab[-9:]))
    return "\n".join(l)

def csv_senales():
    init_tabla(); import io, csv; o = io.StringIO(); w = csv.writer(o)
    w.writerow(["ts_utc", "variante", "par", "grupo", "rango", "precio", "fuerza_%", "btc_rebote_%", "btc_caida_%", "cerrada", "mfe_%", "mae_%"]
               + [f"res_{t}_{s}" for t, s in SALIDAS] + [f"pnl_{t}_{s}" for t, s in SALIDAS])
    for x in _filas():
        r = json.loads(x["res"]) if x["res"] else {}
        w.writerow([x["ts"], x["variante"], x["par"], x["grupo"], x["rango"], x["precio"], x["fuerza"], x["btc_reb"], x["btc_caida"], x["cerrada"], x["mfe"], x["mae"]]
                   + [(r.get(f"{t}_{s}") or ["", ""])[0] for t, s in SALIDAS] + [(r.get(f"{t}_{s}") or ["", ""])[1] for t, s in SALIDAS])
    return o.getvalue().encode()
