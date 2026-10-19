"""engine.py — motor del ciclo de oscilación/DCA. Port EXACTO de bt/osc.py (cycle_sim), pero con estado,
para procesar vela cerrada por vela cerrada. Sin I/O."""
import numpy as np, pandas as pd

def adx_series(h, l, c, p=14):
    up = h.diff(); dn = -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0); mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/p, adjust=False).mean()
    pdi = 100 * pd.Series(pdm, index=h.index).ewm(alpha=1/p, adjust=False).mean() / atr.replace(0, np.nan)
    mdi = 100 * pd.Series(mdm, index=h.index).ewm(alpha=1/p, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/p, adjust=False).mean(), tr.rolling(14).mean()

def rsi_roll(c, p=14):
    d = c.diff(); g = d.clip(lower=0).rolling(p).mean(); l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def prep(d):
    """d: DataFrame 1h con columnas o,h,l,c,v e índice datetime = hora de APERTURA de cada vela (solo velas cerradas)."""
    h4 = d.resample('4h', label='left', closed='left').agg({'o':'first','h':'max','l':'min','c':'last','v':'sum'}).dropna()
    adx, atr = adx_series(d.h, d.l, d.c)
    rsi4 = rsi_roll(h4.c)
    close_t = d.index + pd.Timedelta(hours=1)
    k = h4.index.get_indexer(close_t.floor('4h') - pd.Timedelta(hours=4))
    r4 = np.where(k >= 0, rsi4.values[np.clip(k, 0, None)], np.nan)
    return dict(o=d.o.values, h=d.h.values, l=d.l.values, c=d.c.values, adx=adx.values, atr=atr.values, rsi4=r4,
                idx=d.index, h4c=h4.c.values, h4v=h4.v.values, k4=k)

def hvn(P, i, price, direccion, nb=24, look=100):
    k = P['k4'][i]
    if k < 20: return None
    c = P['h4c'][max(0, k-look+1):k+1]; v = P['h4v'][max(0, k-look+1):k+1]
    lo, hi = c.min(), c.max()
    if hi <= lo: return None
    w = (hi - lo) / nb; b = np.minimum(((c - lo) / w).astype(int), nb - 1); bins = np.bincount(b, weights=v, minlength=nb)
    left = np.r_[-1, bins[:-1]]; right = np.r_[bins[1:], -1]
    nodes = lo + (np.where((bins > left) & (bins > right) & (bins > 0))[0] + 0.5) * w
    cand = nodes[nodes > price] if direccion == 1 else nodes[nodes < price]
    if len(cand): return cand[np.argmin(np.abs(cand - price))]
    return hi if direccion == 1 else lo

def nuevo_estado():
    return {"fase": "idle", "side": 0, "ent": [], "t0": None, "mae1": 0.0, "tp": None}

def _avgp(ent): return len(ent) / sum(1 / p for p in ent)

def _tp_price(P, j, ent, sd, cfg):
    ref = _avgp(ent)
    hv = hvn(P, j - 1, ref, sd)
    if hv is None: return ref * (1 + sd * cfg['tp_fixed'] / 100)
    tp = hv * (1 - sd * cfg['tp_off'] / 100)
    return max(tp, ref * (1 + cfg['tp_min'] / 100)) if sd == 1 else min(tp, ref * (1 - cfg['tp_min'] / 100))

def pnl_puntos(ent, ex, sd, L, fee):
    """PnL en puntos de % de margen (con comisión taker en entradas+salida)."""
    return L * np.mean([(ex / p - 1) * sd for p in ent]) * 100 - fee * 2 * L * 100

def paso(st, P, j, cfg, L_flujo=5):
    """Procesa la vela cerrada j. Devuelve dict del ciclo si cerró en esta vela, si no None. Muta st."""
    o, h, l = P['o'], P['h'], P['l']
    if st['fase'] == 'pending':
        sd = st['side']
        st.update(fase='open', ent=[float(o[j])], t0=str(P['idx'][j]), mae1=0.0)
        st['tp'] = float(_tp_price(P, j, st['ent'], sd, cfg))
    if st['fase'] == 'open':
        sd = st['side']; ent = st['ent']
        atr = P['atr'][j - 1]
        changed = False
        while len(ent) < cfg['max_e']:
            lvl = ent[-1] - sd * cfg['dca'] * atr
            if (sd == 1 and l[j] <= lvl) or (sd == -1 and h[j] >= lvl): ent.append(float(lvl)); changed = True
            else: break
        if changed: st['tp'] = float(_tp_price(P, j, ent, sd, cfg))
        ap = _avgp(ent)
        worst = l[j] if sd == 1 else h[j]
        w1 = float(np.mean([(worst / p - 1) * sd for p in ent]) * 100)   # retorno SIN apalancar en el peor punto
        st['mae1'] = min(st['mae1'], w1)
        res = ex = None
        if len(ent) >= cfg['max_e'] and cfg['sl_dist'] is not None:
            slp = ap * (1 - sd * cfg['sl_dist'] / 100)
            if (sd == 1 and l[j] <= slp) or (sd == -1 and h[j] >= slp): ex, res = slp, 'sl'
        if res is None and L_flujo * w1 <= -95: res = 'liq'
        if res is None and ((sd == 1 and h[j] >= st['tp']) or (sd == -1 and l[j] <= st['tp'])): ex, res = st['tp'], 'tp'
        if res is not None:
            cyc = dict(side=sd, ini=st['t0'], fin=str(P['idx'][j]), ent=list(ent), n_ent=len(ent), salida=None if ex is None else float(ex),
                       res=res, mae1=st['mae1'])
            st.update(fase='idle', side=0, ent=[], t0=None, mae1=0.0, tp=None)
            return cyc
        return None
    # idle: evalúa señal al cierre de esta vela (la vela en que cerró un ciclo ya retornó arriba, igual que la sim)
    a = P['adx'][j]; r = P['rsi4'][j]
    if a == a and r == r and a < cfg['adx_th']:
        if r <= cfg['rl']: st.update(fase='pending', side=1)
        elif r >= cfg['rs']: st.update(fase='pending', side=-1)
    return None
