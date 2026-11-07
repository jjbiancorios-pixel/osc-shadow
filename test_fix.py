"""Prueba con un exchange simulado que reproduce la carrera del 7/11 (orden de DCA que se llena justo al cancelarla)."""
import sys, importlib, numpy as np, pandas as pd
import config, engine

class Store:
    def __init__(s): s.m = {}; s.ev = []; s.cy = {}
    def meta_get(s, k, d=None): return s.m.get(k, d)
    def meta_set(s, k, v): s.m[k] = v
    def evento(s, t, sym, d): s.ev.append((t, sym, d))
    def nuevo(s, sym, st): s.cy[1] = (sym, st); return 1
    def guardar(s, cid, st): s.cy[cid] = (st["sym"], st)
    def abiertos(s): return [(c, a, b) for c, (a, b) in s.cy.items()]
    def abierto_de(s, sym): return next(((c, b) for c, (a, b) in s.cy.items() if a == sym), None)

class Ex:
    """Exchange simulado de una sola moneda (ENA, largo). Posición = lista de fills (precio, qty)."""
    def __init__(s): s.fills = []; s.ords = {}; s.n = 0; s.px = 0.1640; s.race = None
    def modo(s): return "real"
    def equity(s): return 743.0
    def mark(s, syms=None): return {"ENA-USDT": s.px}
    def info(s, sym): return dict(min_qty=1, min_usdt=2, abierto=True)
    def preparar(s, *a): pass
    def _fill(s, o, precio): o["estado"] = "FILLED"; o["avg"] = precio; s.fills.append((precio, o["qty"]))
    def limite(s, sym, sd, q, lvl, cierre=False):
        s.n += 1; o = dict(id=str(s.n), qty=q, lvl=lvl, estado="NEW", avg=0.0); s.ords[o["id"]] = o
        if s.px <= lvl: s._fill(o, lvl)           # limit de compra por encima del mercado: se ejecuta ya (taker)
        return o["id"]
    def cancelar(s, sym, oid):
        o = s.ords[oid]
        if s.race == oid and o["estado"] == "NEW": s._fill(o, o["lvl"])   # carrera: se llena justo antes de cancelar
        elif o["estado"] == "NEW": o["estado"] = "CANCELED"             # (BingX 'ya ejecutada' no es error)
    def orden(s, sym, oid): return dict(s.ords[oid])
    def ordenes_abiertas(s): return [dict(id=i) for i, o in s.ords.items() if o["estado"] == "NEW"]
    def stop(s, sym, sd, precio, qty): s.n += 1; return str(s.n)
    def cancelar_todas(s, sym): pass
    def posiciones(s):
        q = sum(x[1] for x in s.fills)
        return {("ENA-USDT", 1): dict(qty=q, avg=q / sum(x[1] / x[0] for x in s.fills), mark=s.px, upnl=0)} if q else {}

def velas():
    r = np.random.RandomState(3); n = 700
    c = np.exp(np.cumsum(r.randn(n) * 0.004)); c = c * (0.1645 / c[-1]); idx = pd.date_range("2026-10-10", periods=n, freq="h")
    return pd.DataFrame(dict(o=c, h=c * 1.004, l=c * 0.996, c=c, v=r.rand(n) * 1e5 + 1e4), index=idx)

def escenario(modulo):
    bot = importlib.import_module(modulo); importlib.reload(bot)
    ex, db, msgs = Ex(), Store(), []
    b = bot.Bot(ex, db, say=msgs.append, cfg=config.CFG)
    P = engine.prep(velas()); j = len(P["c"]) - 1; b.P["ENA"] = P; b.J["ENA"] = j
    m = 743 * 0.01; L = 6
    ex.fills = [(0.1668, m * L / 0.1668), (0.1637, m * L / 0.1637)]          # entradas 1 y 2 ya ejecutadas
    st = dict(sym="ENA", side=1, L=L, m_ent=m, ent=[0.1668, 0.1637], qty=sum(q for _, q in ex.fills), t0="x", equity_ini=743,
              tp=0.1707, tocado=False, trail=False, peak=None, stop=None, ids={}, dca_lvl=None, j_senal="x")
    st["cid"] = db.nuevo("ENA", st); st["ids"]["sl"] = ex.stop("ENA", 1, 0.14, st["qty"])
    lvl0 = 0.16083; st["ids"]["dca"] = ex.limite("ENA", 1, m * L / lvl0, lvl0); st["dca_lvl"] = lvl0
    ex.race = st["ids"]["dca"]; ex.px = 0.1600       # el precio cae por debajo del nivel justo en la gestión horaria
    b.gestion_hora("ENA", st, P, j)                  # gestión horaria: nivel del DCA cambia -> cancela y repone
    for _ in range(3): b.monitorear()                 # el monitor corre cada 15 s
    cid, sym, st2 = db.abiertos()[0]
    pos = ex.posiciones()[("ENA-USDT", 1)]
    n_real = round(pos["qty"] * pos["avg"] / (m * L))
    print(f"[{modulo}] entradas registradas: {len(st2['ent'])} · reales en el exchange: {n_real} · promedio bot {b._avg(st2['ent']):.6f} vs real {pos['avg']:.6f} · qty bot {st2['qty']:.2f} vs real {pos['qty']:.2f}")
    print("   eventos:", [e[0] for e in db.ev]); print("   avisos:", [x[:90] for x in msgs])
    return len(st2["ent"]) == n_real and abs(st2["qty"] - pos["qty"]) < 1e-6

def escenario_recuento_ENA():
    """Caso vivo: bot con 3 entradas registradas y BingX con 4 (qty 1094.52, promedio 0.163)."""
    import bot
    ex, db, msgs = Ex(), Store(), []
    b = bot.Bot(ex, db, say=msgs.append, cfg=config.CFG)
    P = engine.prep(velas()); b.P["ENA"] = P; b.J["ENA"] = len(P["c"]) - 1
    m = 743 * 0.01; L = 6; ex.px = 0.1622
    ex.fills = [(0.1668, 267.51), (0.1637, 272.5), (0.16083, 277.36), (0.16085, 277.15)]
    st = dict(sym="ENA", side=1, L=L, m_ent=7.43, ent=[0.1668, 0.1637, 0.16085], qty=1094.52, t0="x", equity_ini=743, tp=0.169957,
              tocado=False, trail=False, peak=None, stop=None, ids={}, dca_lvl=None, j_senal="x")
    st["cid"] = db.nuevo("ENA", st)
    b.monitorear(); st2 = db.abiertos()[0][2]
    print(f"[recuento ENA] entradas {len(st2['ent'])} · promedio {b._avg(st2['ent']):.5f} (BingX 0.16300) · eventos {[e[0] for e in db.ev]}")
    b.monitorear(); print("   segunda vuelta, sin cambios:", len(db.abiertos()[0][2]["ent"]) == 4)
    return len(st2["ent"]) == 4 and abs(b._avg(st2["ent"]) - 0.1630) < 5e-4

if __name__ == "__main__":
    import shutil; shutil.copy("bot_orig.py", "bot_orig_mod.py")
    a = escenario("bot_orig_mod"); b = escenario("bot"); c = escenario_recuento_ENA()
    print("ORIGINAL reproduce el bug:", not a, "| PARCHADO OK:", b, "| recuento ENA OK:", c)
