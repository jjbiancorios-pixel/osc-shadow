"""liquid.py — GRABADORA de liquidaciones, interés abierto (OI) y funding (solo observación, no opera). Aprobado por Juanjo el 22/11/2026.
Objetivo: en 2–4 semanas probar si las zonas con muchas liquidaciones de largos/cortos anticipan hacia dónde va el precio
(el canal "El Rey Rentable" las menciona como imán del precio). Binance bloquea a Railway (EE. UU.), por eso:
  - Liquidaciones: OKX público /api/v5/public/liquidation-orders (perpetuos USDT), cada 5 min, las 39 monedas del Shadow.
    Lado: "long" = se liquidó un LARGO (presión vendedora); "short" = se liquidó un CORTO (presión compradora). usd = sz × ctVal × precio.
  - OI y funding: BingX público (openInterest y premiumIndex) cada 15 min; si falla, OKX.
Hilo propio, independiente del ciclo horario. Todo error se registra y NO afecta al Shadow.
Comandos: /liq (resumen 24 h) · /liq MONEDA (zonas de precio) · /liq_csv (todo en ZIP)."""
import io, time, zipfile, threading, traceback, requests, pandas as pd
from datetime import datetime, timezone
import config, db

OKX = "https://www.okx.com"
CADA_LIQ_S = 300
CADA_OI_S = 900
_ctval = {}
_estado = {"liq_ok": None, "liq_err": "", "oi_ok": None, "oi_err": "", "arranque": None}
_hilo = None

def _monedas(): return [m for m in config.MONEDAS]

def init_tabla():
    with db._lock, db._con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS liq(ts INTEGER, par TEXT, lado TEXT, px REAL, usd REAL, fuente TEXT,
                     UNIQUE(ts, par, lado, px, usd))""")
        c.execute("""CREATE TABLE IF NOT EXISTS oi(ts INTEGER, par TEXT, oi_usd REAL, funding REAL, px REAL, fuente TEXT, UNIQUE(ts, par))""")

def _get(url, params=None):
    r = requests.get(url, params=params, timeout=15)
    try: return r.json()
    except Exception: raise ValueError(f"HTTP {r.status_code}: {r.text[:120]}")

def _cargar_ctval():
    j = _get(f"{OKX}/api/v5/public/instruments", {"instType": "SWAP"})
    if str(j.get("code")) != "0": raise ValueError(f"OKX instruments: {str(j)[:120]}")
    for x in j.get("data", []):
        iid = x.get("instId", "")
        if iid.endswith("-USDT-SWAP"):
            try: _ctval[iid.split("-")[0]] = float(x.get("ctVal") or 0)
            except Exception: pass

def _liq_okx(m):
    """Devuelve lista de (ts_ms, lado, px, usd) de las liquidaciones recientes de m en OKX."""
    if m not in _ctval: return []
    j = _get(f"{OKX}/api/v5/public/liquidation-orders", {"instType": "SWAP", "uly": f"{m}-USDT", "state": "filled", "limit": "100"})
    if str(j.get("code")) != "0": raise ValueError(f"OKX {m}: {str(j)[:120]}")
    out = []
    for blk in j.get("data", []):
        for d in blk.get("details", []):
            try:
                px = float(d.get("bkPx")); sz = float(d.get("sz")); ts = int(d.get("ts"))
                ps = str(d.get("posSide", "")).lower()
                lado = ps if ps in ("long", "short") else ("long" if str(d.get("side", "")).lower() == "sell" else "short")
                out.append((ts, lado, px, sz * _ctval[m] * px))
            except Exception: continue
    return out

def _oi_bingx(m):
    s = f"{m}-USDT"
    j = _get(f"{config.BINGX_URL}/openApi/swap/v2/quote/openInterest", {"symbol": s})
    d = j.get("data") or {}
    if isinstance(d, list): d = d[0] if d else {}
    oi = float(d.get("openInterest"))
    p = _get(f"{config.BINGX_URL}/openApi/swap/v2/quote/premiumIndex", {"symbol": s}).get("data") or {}
    if isinstance(p, list): p = p[0] if p else {}
    return oi, float(p.get("lastFundingRate") or 0), float(p.get("markPrice") or 0), "bingx"

def _oi_okx(m):
    iid = f"{m}-USDT-SWAP"
    d = (_get(f"{OKX}/api/v5/public/open-interest", {"instType": "SWAP", "instId": iid}).get("data") or [{}])[0]
    f = (_get(f"{OKX}/api/v5/public/funding-rate", {"instId": iid}).get("data") or [{}])[0]
    t = (_get(f"{OKX}/api/v5/market/ticker", {"instId": iid}).get("data") or [{}])[0]
    px = float(t.get("last") or 0)
    oi_usd = float(d.get("oiUsd") or 0) or float(d.get("oiCcy") or 0) * px
    return oi_usd, float(f.get("fundingRate") or 0), px, "okx"

def ronda_liq():
    if not _ctval: _cargar_ctval()
    n = 0; errs = []
    for m in _monedas():
        try:
            filas = _liq_okx(m)
            if filas:
                with db._lock, db._con() as c:
                    c.executemany("INSERT OR IGNORE INTO liq VALUES(?,?,?,?,?,'okx')", [(ts, m, l, px, u) for ts, l, px, u in filas])
                n += len(filas)
        except Exception as e: errs.append(f"{m}: {str(e)[:60]}")
        time.sleep(0.25)
    _estado["liq_ok"] = datetime.now(timezone.utc); _estado["liq_err"] = "; ".join(errs[:3]) + (f" (+{len(errs)-3})" if len(errs) > 3 else "")
    return n

def ronda_oi():
    ts = int(time.time() // 900 * 900 * 1000); errs = []; filas = []
    for m in _monedas():
        r = None
        for f in (_oi_bingx, _oi_okx):
            try: r = f(m); break
            except Exception as e: err = str(e)[:60]
        if r: filas.append((ts, m) + r)
        else: errs.append(f"{m}: {err}")
        time.sleep(0.25)
    with db._lock, db._con() as c:
        c.executemany("INSERT OR IGNORE INTO oi VALUES(?,?,?,?,?,?)", filas)
    _estado["oi_ok"] = datetime.now(timezone.utc); _estado["oi_err"] = "; ".join(errs[:3]) + (f" (+{len(errs)-3})" if len(errs) > 3 else "")
    return len(filas)

def _bucle(log):
    t_liq = t_oi = 0
    while True:
        ahora = time.time()
        try:
            if ahora - t_liq >= CADA_LIQ_S: t_liq = ahora; ronda_liq()
        except Exception: _estado["liq_err"] = traceback.format_exc()[-150:]; log("liquid liq ERROR (no afecta al Shadow):", _estado["liq_err"])
        try:
            if ahora - t_oi >= CADA_OI_S: t_oi = ahora; ronda_oi()
        except Exception: _estado["oi_err"] = traceback.format_exc()[-150:]; log("liquid oi ERROR (no afecta al Shadow):", _estado["oi_err"])
        time.sleep(20)

def arrancar(log=print):
    global _hilo
    if _hilo: return
    init_tabla(); _estado["arranque"] = datetime.now(timezone.utc)
    _hilo = threading.Thread(target=_bucle, args=(log,), daemon=True); _hilo.start()

def _df(sql, args=()):
    with db._lock, db._con() as c:
        cur = c.execute(sql, args); cols = [x[0] for x in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)

def _fmt(u): return f"{u/1e6:.2f} M" if u >= 1e6 else (f"{u/1e3:.0f} k" if u >= 1e4 else f"{u:,.0f}")

def texto(moneda=None):
    init_tabla()
    desde = int((time.time() - 86400) * 1000)
    tot = _df("SELECT COUNT(*) n, MIN(ts) t0 FROM liq")
    est = (f"Grabando desde {_estado['arranque']:%d/%m %H:%M} UTC" if _estado["arranque"] else "Grabadora no iniciada")
    est += f" · liquidaciones guardadas: {int(tot.n[0])}"
    if _estado["liq_err"]: est += f"\n⚠️ Liq: {_estado['liq_err']}"
    if _estado["oi_err"]: est += f"\n⚠️ OI: {_estado['oi_err']}"
    if moneda:
        m = moneda.upper().replace("USDT", "").replace("-", "")
        d = _df("SELECT * FROM liq WHERE par=? AND ts>=?", (m, desde))
        if d.empty: return f"💧 <b>Liquidaciones {m}</b> (24 h): sin datos todavía.\n{est}"
        px = d.px.median(); paso = px * 0.005
        d["zona"] = (d.px / paso).round() * paso
        z = d.groupby(["zona", "lado"]).usd.sum().unstack(fill_value=0).sort_index(ascending=False)
        z["tot"] = z.sum(axis=1); z = z.sort_values("tot", ascending=False).head(8).sort_index(ascending=False)
        lin = [f"{i:.6g}: L {_fmt(r.get('long', 0))} · C {_fmt(r.get('short', 0))}" for i, r in z.iterrows()]
        o = _df("SELECT * FROM oi WHERE par=? ORDER BY ts DESC LIMIT 97", (m,))
        oi = ""
        if len(o) > 1: oi = f"\nOI {_fmt(o.oi_usd.iloc[0])} ({100*(o.oi_usd.iloc[0]/o.oi_usd.iloc[-1]-1):+.1f}% vs ~24 h) · funding {100*o.funding.iloc[0]:+.4f}%"
        return (f"💧 <b>Liquidaciones {m}</b> (24 h, zonas de 0,5%)\nLargos liquidados {_fmt(d[d.lado=='long'].usd.sum())} · cortos {_fmt(d[d.lado=='short'].usd.sum())}\n"
                + "\n".join(lin) + oi + f"\n{est}")
    d = _df("SELECT par, lado, SUM(usd) usd, COUNT(*) n FROM liq WHERE ts>=? GROUP BY par, lado", (desde,))
    if d.empty: return f"💧 <b>Liquidaciones</b> (24 h): sin datos todavía.\n{est}"
    p = d.pivot_table(index="par", columns="lado", values="usd", aggfunc="sum", fill_value=0)
    for k in ("long", "short"):
        if k not in p: p[k] = 0.0
    p["tot"] = p.long + p.short; p = p.sort_values("tot", ascending=False).head(10)
    lin = [f"{i}: L {_fmt(r.long)} · C {_fmt(r.short)}" for i, r in p.iterrows()]
    return (f"💧 <b>Liquidaciones</b> (24 h, OKX) — L = largos liquidados · C = cortos\nTotal L {_fmt(d[d.lado=='long'].usd.sum())} · C {_fmt(d[d.lado=='short'].usd.sum())}\n"
            + "\n".join(lin) + f"\n{est}\n/liq MONEDA para ver las zonas de precio.")

def zip_todo():
    init_tabla()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("liquidaciones.csv", _df("SELECT * FROM liq ORDER BY ts").to_csv(index=False))
        z.writestr("oi_funding.csv", _df("SELECT * FROM oi ORDER BY ts").to_csv(index=False))
        z.writestr("LEEME.txt", "ts en milisegundos UTC. liq: lado long = largo liquidado; usd = tamaño en USDT. oi: oi_usd = interés abierto en USDT; funding = tasa (0.0001 = 0,01%).")
    return buf.getvalue()
