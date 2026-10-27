"""reporte.py — métricas de cartera idénticas a las del backtest (f=1% de margen por entrada, tope K ciclos simultáneos)."""
from datetime import datetime, timedelta
import config, db

def cartera(cfg, L, desde=None):
    """Aplica el tope de K ciclos simultáneos por orden de inicio (ciclos abiertos cuentan como ocupados)."""
    cs = db.ciclos(cfg)
    ab = [(st['t0'], s) for s, c, st in db.estados_abiertos(cfg)]
    ev = [(c['ini'], c['fin'], c) for c in cs] + [(t0, '9999', None) for t0, s in ab]
    ev.sort(key=lambda x: x[0]); ocupados = []; tomados = []
    for ini, fin, c in ev:
        ocupados = [x for x in ocupados if x > ini]
        if len(ocupados) >= config.K_SIMULTANEOS: continue
        ocupados.append(fin)
        if c is not None: tomados.append(c)
    if desde: tomados = [c for c in tomados if c['fin'] >= desde]
    key = 'pnl3' if L == 3 else 'pnl5'
    cap = [c[key] / 100 * c['n_ent'] * config.F_MARGEN * 100 for c in tomados]
    return tomados, cap

def resumen_cfg(cfg, desde=None):
    out = {}
    for L in config.L_REPORTE:
        t, cap = cartera(cfg, L, desde)
        out[L] = (len(t), sum(cap), sum(1 for c in t if (c['pnl3'] if L == 3 else c['pnl5']) > 0), t)
    t = out[5][3]
    n = len(t)
    sl = sum(1 for c in t if c['res'] == 'sl'); liq = sum(1 for c in t if c['res'] == 'liq')
    ab = len(db.estados_abiertos(cfg))
    lv = [c['lev'] for c in t if c.get('lev')]
    return dict(n=n, win=(out[5][2] / n * 100 if n else 0), sl=sl, liq=liq, ab=ab, cap3=out[3][1], cap5=out[5][1], lev=(sum(lv) / len(lv) if lv else None),
                dur=(sum(c['dur_h'] for c in t) / n if n else 0), ent=(sum(c['n_ent'] for c in t) / n if n else 0))

def texto_resumen(desde=None, titulo="Resumen"):
    l = [f"📊 <b>Oscilación SHADOW — {titulo}</b> (sin dinero)", "Capital % con 1% de margen por entrada, máx. 8 ciclos simultáneos", ""]
    for k, cfg in config.CONFIGS.items():
        r = resumen_cfg(k, desde)
        l.append(f"<b>{cfg['nombre']}</b>")
        l.append(f"ciclos {r['n']} | win {r['win']:.0f}% | SL {r['sl']} | liq {r['liq']} | abiertos {r['ab']}")
        if r['lev'] is not None:
            l.append(f"capital (apal. variable, prom {r['lev']:.1f}x): <b>{r['cap5']:+.2f}%</b> | dur {r['dur']:.1f}h | ent. prom {r['ent']:.1f}")
        else:
            l.append(f"capital 3x: <b>{r['cap3']:+.2f}%</b> | 5x: <b>{r['cap5']:+.2f}%</b> | dur {r['dur']:.1f}h | ent. prom {r['ent']:.1f}")
        l.append("")
    return "\n".join(l).strip()

def texto_abiertos():
    ab = db.estados_abiertos()
    if not ab: return "Sin ciclos abiertos."
    l = [f"📂 <b>Ciclos abiertos ({len(ab)})</b>"]
    for s, c, st in sorted(ab, key=lambda x: (x[1], x[0])):
        l.append(f"{c} {s} {'L' if st['side']==1 else 'C'} ent {len(st['ent'])} desde {st['t0'][5:16]}Z")
    return "\n".join(l[:60])

def csv_ciclos():
    import io, csv
    b = io.StringIO(); w = csv.writer(b)
    w.writerow(['cfg','sym','side','ini_utc','fin_utc','n_ent','salida','res','mae1_pct','pnl3','pnl5','dur_h','entradas','lev'])
    for c in db.ciclos():
        w.writerow([c['cfg'], c['sym'], c['side'], c['ini'], c['fin'], c['n_ent'], c['salida'], c['res'], round(c['mae1'], 3),
                    round(c['pnl3'], 3), round(c['pnl5'], 3), round(c['dur_h'], 1), c['entradas'], (round(c['lev'], 1) if c.get('lev') else '')])
    return b.getvalue().encode()
