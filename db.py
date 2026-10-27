import sqlite3, json, threading
from datetime import datetime, timezone, timedelta
import config
TZ_ARG = timezone(timedelta(hours=-3))
_lock = threading.Lock()
def _con():
    c = sqlite3.connect(config.DB_PATH, timeout=30); c.row_factory = sqlite3.Row; return c
def init():
    with _lock, _con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS ultima_vela(sym TEXT PRIMARY KEY, ts TEXT, fuente TEXT);
        CREATE TABLE IF NOT EXISTS estado(sym TEXT, cfg TEXT, st TEXT, PRIMARY KEY(sym,cfg));
        CREATE TABLE IF NOT EXISTS ciclos(id INTEGER PRIMARY KEY AUTOINCREMENT, cfg TEXT, sym TEXT, side INTEGER, ini TEXT, fin TEXT,
            n_ent INTEGER, entradas TEXT, salida REAL, res TEXT, mae1 REAL, pnl3 REAL, pnl5 REAL, dur_h REAL);
        CREATE INDEX IF NOT EXISTS ix_c ON ciclos(cfg, ini);
        """)
        try: c.execute("ALTER TABLE ciclos ADD COLUMN lev REAL")   # apalancamiento del ciclo (solo configs con apal. variable)
        except sqlite3.OperationalError: pass
def meta_get(k, d=None):
    with _lock, _con() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone(); return r[0] if r else d
def meta_set(k, v):
    with _lock, _con() as c: c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
def ultima_vela(sym):
    with _lock, _con() as c:
        r = c.execute("SELECT ts FROM ultima_vela WHERE sym=?", (sym,)).fetchone(); return r[0] if r else None
def set_ultima_vela(sym, ts, fuente):
    with _lock, _con() as c: c.execute("INSERT OR REPLACE INTO ultima_vela VALUES(?,?,?)", (sym, str(ts), fuente))
def estado_get(sym, cfg):
    with _lock, _con() as c:
        r = c.execute("SELECT st FROM estado WHERE sym=? AND cfg=?", (sym, cfg)).fetchone(); return json.loads(r[0]) if r else None
def estado_set(sym, cfg, st):
    with _lock, _con() as c: c.execute("INSERT OR REPLACE INTO estado VALUES(?,?,?)", (sym, cfg, json.dumps(st)))
def estados_abiertos(cfg=None):
    with _lock, _con() as c:
        q = "SELECT sym,cfg,st FROM estado" + (" WHERE cfg=?" if cfg else "")
        rows = c.execute(q, (cfg,) if cfg else ()).fetchall()
    out = []
    for r in rows:
        st = json.loads(r['st'])
        if st.get('fase') == 'open': out.append((r['sym'], r['cfg'], st))
    return out
def guardar_ciclo(cfg, sym, cyc, pnl3, pnl5):
    ini = datetime.fromisoformat(cyc['ini']); fin = datetime.fromisoformat(cyc['fin']) + timedelta(hours=1)
    with _lock, _con() as c:
        c.execute("INSERT INTO ciclos(cfg,sym,side,ini,fin,n_ent,entradas,salida,res,mae1,pnl3,pnl5,dur_h,lev) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (cfg, sym, cyc['side'], cyc['ini'], str(fin), cyc['n_ent'], json.dumps(cyc['ent']), cyc['salida'], cyc['res'],
                   cyc['mae1'], pnl3, pnl5, (fin - ini).total_seconds() / 3600, cyc.get('L')))
def ciclos(cfg=None, desde=None):
    q = "SELECT * FROM ciclos WHERE 1=1"; a = []
    if cfg: q += " AND cfg=?"; a.append(cfg)
    if desde: q += " AND fin>=?"; a.append(desde)
    with _lock, _con() as c: return [dict(r) for r in c.execute(q + " ORDER BY ini", a).fetchall()]
