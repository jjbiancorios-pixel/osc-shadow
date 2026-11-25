"""regimen.py — "RÉGIMEN": etiqueta cada ciclo del Shadow según el mercado de BTC (solo observación, no opera).
Aprobado por Juanjo el 22/11/2026. Ver claude/Investigacion_Alternativas_30k_2026-11-22.md del proyecto.
  Régimen BAJISTA = cierre diario de BTC del día anterior al inicio del ciclo, contra el de 60 días antes, cae más de 15%.
  (umbral tomado del paper AdaptiveTrend, Bui y Nguyen 2026, donde la tendencia pierde en ese régimen).
Se compara, por config, cuántos ciclos hubo en cada régimen, % ganadores, SL y resultado en % del capital (1% margen × 5x, sin tope).
Objetivo: ver si conviene un filtro de régimen único para los 3 bots. Costo: 1 consulta diaria de velas 1d de BTC.
Todo error se registra y NO afecta al Shadow."""
import io, csv, requests, pandas as pd
from datetime import datetime, timezone
from html import escape
import config, db

DIAS = 60
UMBRAL = -15.0
MOSTRAR = ("J", "J7", "JD", "JF", "JX", "JZ", "JC", "BT1", "BT2", "BX")

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("CREATE TABLE IF NOT EXISTS regimen(fecha TEXT PRIMARY KEY, cierre REAL, chg60 REAL)")

def _velas_1d():
    try:
        r = requests.get(f"{config.BINGX_URL}/openApi/swap/v3/quote/klines", params={"symbol": "BTC-USDT", "interval": "1d", "limit": 500}, timeout=15).json()
        data = r.get("data") or []
        rows = [(k['time'], k['close']) if isinstance(k, dict) else (k[0], k[4]) for k in data]
        if len(rows) > DIAS: return rows
    except Exception: pass
    r = requests.get(f"{config.BINANCE_URL}/fapi/v1/klines", params={"symbol": "BTCUSDT", "interval": "1d", "limit": 500}, timeout=15).json()
    return [(k[0], k[4]) for k in r]

def actualizar():
    """Llamar una vez por hora; solo consulta a BingX una vez por día (UTC)."""
    init_tabla()
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if db.meta_get("regimen_dia") == hoy: return
    rows = _velas_1d()
    df = pd.DataFrame(rows, columns=["t", "c"]).astype({"t": "int64", "c": float}).drop_duplicates("t").sort_values("t")
    df.index = pd.to_datetime(df.t, unit="ms")
    ahora = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    df = df[df.index + pd.Timedelta(days=1) <= ahora]            # solo velas diarias cerradas
    if len(df) <= DIAS: return
    chg = (df.c / df.c.shift(DIAS) - 1) * 100
    with db._lock, db._con() as con:
        for t, c, g in zip(df.index, df.c, chg):
            if g == g: con.execute("INSERT OR REPLACE INTO regimen VALUES(?,?,?)", (str(t.date()), float(c), float(g)))
    db.meta_set("regimen_dia", hoy)

def _mapa():
    with db._lock, db._con() as con:
        return {r[0]: r[1] for r in con.execute("SELECT fecha, chg60 FROM regimen").fetchall()}

def etiqueta(ini, mapa):
    """'bajista' | 'normal' | None (sin dato). Usa el cierre diario del día ANTERIOR al inicio del ciclo (sin mirar el futuro)."""
    d = str((pd.Timestamp(ini) - pd.Timedelta(days=1)).date())
    g = mapa.get(d)
    if g is None: return None
    return "bajista" if g < UMBRAL else "normal"

def _stats(cs):
    n = len(cs)
    if not n: return "—"
    w = sum(1 for c in cs if c['pnl5'] > 0); sl = sum(1 for c in cs if c['res'] in ('sl', 'liq'))
    cap = sum(c['pnl5'] / 100 * c['n_ent'] * config.F_MARGEN * 100 for c in cs)
    return f"{n} ciclos · win {100*w/n:.0f}% · SL {sl} · {cap:+.2f}% cap. ({cap/n:+.3f}%/ciclo)"

def texto(todas=False):
    mapa = _mapa()
    l = [f"🌡️ <b>RÉGIMEN de BTC</b> (bajista = BTC cae más de {abs(UMBRAL):.0f}% en {DIAS} días) — solo observación"]
    if mapa:
        f = max(mapa); g = mapa[f]
        l.append(f"Hoy (cierre {f}): BTC {g:+.1f}% en {DIAS} días → <b>{'BAJISTA' if g < UMBRAL else 'normal'}</b>")
        try:
            import miedo; h = miedo.fng_hoy()
            if h: l.append(f"Fear &amp; Greed {h[0]}: <b>{h[1]}</b> ({escape(h[2])})")
        except Exception: pass
    else:
        l.append("Todavía sin datos de BTC diarios."); return "\n".join(l)
    l.append("Resultado a 5x con 1% de margen por entrada, sin tope de ciclos.")
    for k in (config.CONFIGS if todas else MOSTRAR):
        if k not in config.CONFIGS: continue
        cs = db.ciclos(k); b = [c for c in cs if etiqueta(c['ini'], mapa) == "bajista"]; nrm = [c for c in cs if etiqueta(c['ini'], mapa) == "normal"]
        if not cs: continue
        l.append("")
        l.append(f"<b>{escape(k)}</b>")
        l.append(f"🔻 bajista: {_stats(b)}")
        l.append(f"▫️ normal: {_stats(nrm)}")
    return "\n".join(l)

def csv_todo():
    mapa = _mapa(); b = io.StringIO(); w = csv.writer(b)
    try:
        import miedo; fng = miedo.fng_mapa()
    except Exception: fng = {}
    w.writerow(["cfg", "sym", "side", "ini_utc", "fin_utc", "n_ent", "res", "pnl5", "cap_pct", "btc_chg60_dia_anterior", "regimen", "fng_dia_anterior"])
    for k in config.CONFIGS:
        for c in db.ciclos(k):
            d = str((pd.Timestamp(c['ini']) - pd.Timedelta(days=1)).date())
            w.writerow([k, c['sym'], c['side'], c['ini'], c['fin'], c['n_ent'], c['res'], round(c['pnl5'], 3),
                        round(c['pnl5'] / 100 * c['n_ent'] * config.F_MARGEN * 100, 4), None if mapa.get(d) is None else round(mapa[d], 2), etiqueta(c['ini'], mapa), fng.get(d)])
    return b.getvalue().encode()
