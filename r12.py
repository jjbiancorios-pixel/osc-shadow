"""r12.py — estrategias "RIESGO 1 : GANANCIA 2" (solo observación, no opera). Pedido de Juanjo del 16/11/2026.
Stop a X y TP a 2X; máximo 72 h (si no toca nada, se cierra al precio de ese momento). Mismas monedas del Shadow (sin ETH).
  RET_C (corto en el rebote con tendencia bajista): precio y EMA50 bajo la EMA200 (1h), RSI(14) 1h > 65, BTC con cambio de 7 días negativo.
        Stop 2 ATR(14, 1h).  Prueba 219 días (10/04-15/11): 411 casos, 42% de acierto, +0,17 R por operación.
  RUP_L (largo en ruptura con BTC en suba): cierre sobre el máximo de las 48 velas anteriores, BTC con cambio de 7 días positivo.
        Stop 3 ATR.  Prueba: 1.509 casos, 41% de acierto, +0,13 R por operación.
Máx. una señal por moneda y estrategia cada 12 h. Entrada al cierre de la vela 1h de la señal. Si stop y TP caen en la misma vela, cuenta el stop.
Comisión 0,1% ida y vuelta (restada en R). Cartera virtual: tope 20 operaciones abiertas a la vez, riesgo 0,25% del capital por operación
(≈ margen 1% del capital a 10x). Las señales sin cupo se registran igual (cartera=0) para medir la estrategia completa.
Todo error se registra y NO afecta al Shadow. Ver claude/Estrategia_Riesgo1_Ganancia2_2026-11-16.md del proyecto."""
import io, csv, numpy as np, pandas as pd, db, respiro

TOPE = 20
RIESGO_PCT = 0.25
HOR = 72
SEP_H = 12
FEE = 0.001
EXCLUIR = ("ETH",)
FAMS = {"RET_C": dict(lado=-1, sl_atr=2.0, nombre="Corto en rebote bajista"), "RUP_L": dict(lado=1, sl_atr=3.0, nombre="Largo en ruptura alcista")}

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS r12(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, fam TEXT, par TEXT, lado INTEGER, precio REAL,
                     atrp REAL, slp REAL, cartera INTEGER, cerrada INTEGER DEFAULT 0, res TEXT, r REAL, ts_fin TEXT, horas INTEGER,
                     UNIQUE(fam, par, ts))""")

def _wilder(x, n=14):
    return pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().values

def _indicadores(df):
    c = df["c"].values; h = df["h"].values; l = df["l"].values
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = _wilder(tr)
    d = np.diff(c, prepend=c[0]); up = _wilder(np.maximum(d, 0)); dn = _wilder(np.maximum(-d, 0))
    rsi = np.where(dn == 0, 100.0, 100 - 100 / (1 + up / np.where(dn == 0, 1, dn)))
    s = pd.Series(c); e50 = s.ewm(span=50, adjust=False).mean().values; e200 = s.ewm(span=200, adjust=False).mean().values
    return atr, rsi, e50, e200

def _senal(df, btc7):
    """Señales en la última vela cerrada de df. Devuelve lista de (fam, atr%)."""
    if len(df) < 250: return []
    atr, rsi, e50, e200 = _indicadores(df)
    c = float(df["c"].iloc[-1]); out = []
    if c < e200[-1] and e50[-1] < e200[-1] and rsi[-1] > 65 and btc7 < 0: out.append(("RET_C", atr[-1] / c))
    if c > float(df["h"].iloc[-49:-1].max()) and btc7 > 0: out.append(("RUP_L", atr[-1] / c))
    return out

def _resolver(df, ts, lado, precio, slp):
    t0 = pd.Timestamp(ts)
    if t0 not in df.index: return None
    i = df.index.get_loc(t0)
    sl = precio * (1 - lado * slp); tp = precio * (1 + lado * 2 * slp)
    H = df["h"].values; L = df["l"].values; C = df["c"].values
    for j in range(i + 1, min(i + 1 + HOR, len(df))):
        if (L[j] <= sl if lado == 1 else H[j] >= sl): return "sl", -1 - FEE / slp, str(df.index[j]), j - i
        if (H[j] >= tp if lado == 1 else L[j] <= tp): return "tp", 2 - FEE / slp, str(df.index[j]), j - i
    if i + HOR < len(df):
        return "to", lado * (C[i + HOR] / precio - 1) / slp - FEE / slp, str(df.index[i + HOR]), HOR
    return None

def actualizar(avisar=None):
    """Llamar una vez por hora, después de procesar todas las monedas (usa las velas que guarda respiro.CACHE)."""
    init_tabla()
    CACHE = respiro.CACHE; btc = CACHE.get("BTC")
    if btc is None or len(btc) < 200: return
    ts = str(btc.index[-1]); bc = btc["c"]
    btc7 = float(bc.iloc[-1] / bc.iloc[-169] - 1) if len(bc) > 169 else 0.0
    with db._lock, db._con() as con:
        # 1) resolver abiertas
        for r in con.execute("SELECT id, ts, par, lado, precio, slp FROM r12 WHERE cerrada=0").fetchall():
            df = CACHE.get(r["par"])
            if df is None: continue
            x = _resolver(df, r["ts"], r["lado"], r["precio"], r["slp"])
            if x: con.execute("UPDATE r12 SET cerrada=1, res=?, r=?, ts_fin=?, horas=? WHERE id=?", (x[0], x[1], x[2], x[3], r["id"]))
        # 2) señales nuevas
        abiertas = con.execute("SELECT COUNT(*) FROM r12 WHERE cerrada=0 AND cartera=1").fetchone()[0]
        for m, df in CACHE.items():
            if m in EXCLUIR or df is None or len(df) == 0 or str(df.index[-1]) != ts: continue
            for fam, atrp in _senal(df, btc7):
                ult = con.execute("SELECT ts FROM r12 WHERE fam=? AND par=? ORDER BY ts DESC LIMIT 1", (fam, m)).fetchone()
                if ult and (pd.Timestamp(ts) - pd.Timestamp(ult[0])).total_seconds() < SEP_H * 3600: continue
                slp = max(FAMS[fam]["sl_atr"] * atrp, 0.003)
                en = 1 if abiertas < TOPE else 0
                try:
                    con.execute("INSERT INTO r12(ts,fam,par,lado,precio,atrp,slp,cartera) VALUES(?,?,?,?,?,?,?,?)",
                                (ts, fam, m, FAMS[fam]["lado"], float(df["c"].iloc[-1]), float(atrp), float(slp), en))
                    abiertas += en
                except Exception: pass

def _filas():
    with db._lock, db._con() as c: return [dict(r) for r in c.execute("SELECT * FROM r12 ORDER BY ts, id").fetchall()]

def texto():
    init_tabla(); F = _filas()
    if not F: return "⚖️ Riesgo 1 : Ganancia 2: todavía sin señales."
    t0 = pd.Timestamp(F[0]["ts"]); dias = max(1.0, (pd.Timestamp(F[-1]["ts"]) - t0).total_seconds() / 86400)
    l = [f"⚖️ <b>Riesgo 1 : Ganancia 2</b> (solo observación) · desde {F[0]['ts'][:10]} · {len(F)} señales"]
    for fam, v in FAMS.items():
        R = [x for x in F if x["fam"] == fam and x["cerrada"]]; ab = sum(1 for x in F if x["fam"] == fam and not x["cerrada"])
        if R:
            w = sum(1 for x in R if x["r"] > 0); tot = sum(x["r"] for x in R)
            l.append(f"\n<b>{v['nombre']}</b> ({fam}): {len(R)} cerradas, {ab} abiertas · acierto {100*w/len(R):.0f}% · {tot/len(R):+.2f} R por op. · total {tot:+.1f} R")
        else: l.append(f"\n<b>{v['nombre']}</b> ({fam}): sin cerradas todavía, {ab} abiertas")
    C = [x for x in F if x["cartera"] == 1]; Cc = [x for x in C if x["cerrada"]]
    totR = sum(x["r"] for x in Cc); eq = 0; pk = 0; dd = 0
    for x in sorted(Cc, key=lambda y: y["ts_fin"]): eq += x["r"]; pk = max(pk, eq); dd = min(dd, eq - pk)
    l.append(f"\n<b>Cartera</b> (tope {TOPE} abiertas, riesgo {RIESGO_PCT}% por op.): {len(C)} operaciones ({7*len(C)/dias:.0f}/semana), "
             f"{sum(1 for x in Cc if x['r'] > 0)} ganadoras, {sum(1 for x in C if not x['cerrada'])} abiertas · resultado {totR:+.1f} R = "
             f"{totR*RIESGO_PCT:+.1f}% del capital · peor racha {dd:.1f} R ({dd*RIESGO_PCT:.1f}%) · sin cupo: {sum(1 for x in F if x['cartera'] == 0)}")
    return "\n".join(l)

def csv_senales():
    init_tabla(); o = io.StringIO(); w = csv.writer(o)
    w.writerow(["ts_utc", "estrategia", "par", "lado", "precio", "atr_%", "stop_%", "en_cartera", "cerrada", "salida", "R", "ts_fin", "horas"])
    for x in _filas():
        w.writerow([x["ts"], x["fam"], x["par"], x["lado"], x["precio"], round(100 * x["atrp"], 3), round(100 * x["slp"], 3), x["cartera"], x["cerrada"], x["res"], x["r"], x["ts_fin"], x["horas"]])
    return o.getvalue().encode()
