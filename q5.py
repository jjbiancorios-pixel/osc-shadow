"""q5.py — "ENTRAR EN LA 5ª" (solo observación, no opera). Idea de Juanjo del 17/11/2026 (NEAR y DASH cerraron TP con 5 entradas).
Se sigue un ciclo "fantasma" de la estrategia J en cada moneda (sin tope de ciclos), SOLO LARGOS:
  señal = ADX(14,1h) < 25 y RSI(14, 4h) < 38 → precio inicial e0 al cierre, escalón g = 2×ATR(14,1h) de esa vela.
  Niveles de DCA: e0 - k·g. El fantasma termina si el precio toca su TP piso (promedio +0,45%) antes del nivel 5.
  Si el mínimo toca el nivel 5 (e0 - 4g) se registra UNA entrada a ese precio y se mide con dos variantes:
    A: TP +2,5% de precio (≈ +15% del margen a 6x) · SL 3 escalones abajo (p5 - 3g, ≈ -40% del margen)
    B: TP +3,7% de precio (≈ +22% del margen)     · SL -5% de precio (≈ -30% del margen)
  Plazo máximo 7 días (cierra al precio de ese momento). En la vela de entrada solo cuenta el SL. Comisión 0,05% por lado.
Prueba 219 días (sim): 30 largos, A ganó +6,4% del margen por op. (25 TP / 4 SL), pero el último tercio dio ~0. Muestra chica.
Todo error se registra y NO afecta al Shadow. Ver claude/Analisis_Entrada_en_5ta_2026-11-17.md del proyecto."""
import io, csv, pandas as pd, db, engine, respiro

LEV = 6
FEE = 0.0005
DIAS = 7
EXCLUIR = ("ETH", "BTC")
VAR = {"A": dict(tp=0.025, sl="3g", nombre="TP 2,5% · SL 3 escalones"), "B": dict(tp=0.037, sl=0.05, nombre="TP 3,7% · SL 5%")}

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS q5_fantasma(par TEXT PRIMARY KEY, ts0 TEXT, e0 REAL, g REAL, n INTEGER, ts_ult TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS q5(id INTEGER PRIMARY KEY AUTOINCREMENT, ts0 TEXT, ts TEXT, var TEXT, par TEXT, precio REAL,
                     tp REAL, sl REAL, horas_a_5 INTEGER, cerrada INTEGER DEFAULT 0, res TEXT, pct_margen REAL, ts_fin TEXT, horas INTEGER,
                     UNIQUE(var, par, ts))""")

def _resolver(df, ts, precio, tp, sl):
    t0 = pd.Timestamp(ts)
    if t0 not in df.index: return None
    i = df.index.get_loc(t0); H = df["h"].values; L = df["l"].values; O = df["o"].values; C = df["c"].values
    f = 2 * LEV * FEE
    if L[i] <= sl: return "sl", LEV * (sl / precio - 1) - f, str(df.index[i]), 0
    for j in range(i + 1, min(i + 1 + DIAS * 24, len(df))):
        if L[j] <= sl: return "sl", LEV * (min(sl, O[j]) / precio - 1) - f, str(df.index[j]), j - i
        if H[j] >= tp: return "tp", LEV * (tp / precio - 1) - f, str(df.index[j]), j - i
    if i + DIAS * 24 < len(df): return "tiempo", LEV * (C[i + DIAS * 24] / precio - 1) - f, str(df.index[i + DIAS * 24]), DIAS * 24
    return None

def actualizar():
    """Llamar una vez por hora, después de procesar todas las monedas (usa respiro.CACHE)."""
    init_tabla()
    with db._lock, db._con() as con:
        # 1) resolver entradas abiertas
        for r in con.execute("SELECT id, ts, par, precio, tp, sl FROM q5 WHERE cerrada=0").fetchall():
            df = respiro.CACHE.get(r["par"])
            if df is None: continue
            x = _resolver(df, r["ts"], r["precio"], r["tp"], r["sl"])
            if x: con.execute("UPDATE q5 SET cerrada=1, res=?, pct_margen=?, ts_fin=?, horas=? WHERE id=?", (x[0], x[1], x[2], x[3], r["id"]))
        abiertas_par = {r[0] for r in con.execute("SELECT DISTINCT par FROM q5 WHERE cerrada=0").fetchall()}
        F = {r["par"]: dict(r) for r in con.execute("SELECT * FROM q5_fantasma").fetchall()}
        for m, df in respiro.CACHE.items():
            if m in EXCLUIR or df is None or len(df) < 250: continue
            # 2) avanzar el fantasma vela por vela desde la última procesada
            f = F.get(m)
            if f:
                idx = df.index; H = df["h"].values; L = df["l"].values
                for j in range(len(df)):
                    if str(idx[j]) <= f["ts_ult"]: continue
                    n = f["n"]; e0 = f["e0"]; g = f["g"]
                    while n < 5 and L[j] <= e0 - n * g: n += 1
                    if n >= 5:
                        p5 = e0 - 4 * g; ts = str(idx[j])
                        h5 = int((pd.Timestamp(ts) - pd.Timestamp(f["ts0"])).total_seconds() // 3600)
                        for k, v in VAR.items():
                            tp = p5 * (1 + v["tp"]); sl = p5 - 3 * g if v["sl"] == "3g" else p5 * (1 - v["sl"])
                            con.execute("INSERT OR IGNORE INTO q5(ts0,ts,var,par,precio,tp,sl,horas_a_5) VALUES(?,?,?,?,?,?,?,?)",
                                        (f["ts0"], ts, k, m, p5, tp, sl, h5))
                        abiertas_par.add(m); con.execute("DELETE FROM q5_fantasma WHERE par=?", (m,)); f = None; break
                    prom = sum(e0 - k * g for k in range(n)) / n
                    if H[j] >= prom * 1.0045:
                        con.execute("DELETE FROM q5_fantasma WHERE par=?", (m,)); f = None; break
                    f["n"] = n; f["ts_ult"] = str(idx[j])
                if f: con.execute("UPDATE q5_fantasma SET n=?, ts_ult=? WHERE par=?", (f["n"], f["ts_ult"], m))
                continue
            # 3) señal nueva en la última vela (sin fantasma ni entrada abierta en esa moneda)
            if m in abiertas_par: continue
            P = engine.prep(df); a = P["adx"][-1]; r = P["rsi4"][-1]; atr = P["atr"][-1]; c = float(df["c"].iloc[-1])
            if a == a and r == r and atr == atr and a < 25 and r < 38:
                con.execute("INSERT OR REPLACE INTO q5_fantasma(par,ts0,e0,g,n,ts_ult) VALUES(?,?,?,?,1,?)",
                            (m, str(df.index[-1]), c, 2 * float(atr), str(df.index[-1])))

def _filas():
    with db._lock, db._con() as c: return [dict(r) for r in c.execute("SELECT * FROM q5 ORDER BY ts, id").fetchall()]

def texto():
    init_tabla(); F = _filas()
    with db._lock, db._con() as c: nf = c.execute("SELECT COUNT(*) FROM q5_fantasma").fetchone()[0]
    l = [f"5️⃣ <b>Entrar en la 5ª</b> (solo largos, observación) · {nf} ciclos fantasma en curso"]
    if not F: l.append("Todavía ninguna moneda llegó al nivel 5."); return "\n".join(l)
    l[0] += f" · desde {F[0]['ts'][:10]}"
    for k, v in VAR.items():
        R = [x for x in F if x["var"] == k and x["cerrada"]]; ab = [x for x in F if x["var"] == k and not x["cerrada"]]
        if R:
            w = sum(1 for x in R if x["pct_margen"] > 0); tot = sum(x["pct_margen"] for x in R)
            l.append(f"\n<b>{k}: {v['nombre']}</b> · {len(R)} cerradas ({w} ganan), {len(ab)} abiertas · "
                     f"{100*tot/len(R):+.1f}% del margen por op. · total {100*tot:+.0f}% de un margen")
        else: l.append(f"\n<b>{k}: {v['nombre']}</b> · sin cerradas, {len(ab)} abiertas")
    ab = [x for x in F if x["var"] == "A" and not x["cerrada"]]
    if ab: l.append("\nAbiertas: " + ", ".join(f"{x['par']} @{x['precio']:.4g}" for x in ab))
    return "\n".join(l)

def csv_senales():
    init_tabla(); o = io.StringIO(); w = csv.writer(o)
    w.writerow(["ts_senal_utc", "ts_entrada_utc", "variante", "par", "precio_5a", "tp", "sl", "horas_hasta_5a", "cerrada", "salida", "pct_margen", "ts_fin", "horas"])
    for x in _filas():
        w.writerow([x["ts0"], x["ts"], x["var"], x["par"], x["precio"], x["tp"], x["sl"], x["horas_a_5"], x["cerrada"], x["res"],
                    None if x["pct_margen"] is None else round(100 * x["pct_margen"], 2), x["ts_fin"], x["horas"]])
    return o.getvalue().encode()
