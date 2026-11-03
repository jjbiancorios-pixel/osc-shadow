"""main.py — Bot Oscilación SHADOW. Solo observa y registra ciclos simulados (4 configuraciones en paralelo). No opera."""
import time, traceback
from datetime import datetime, timezone, timedelta
import pandas as pd
import config, db, data, engine, reporte, tg

def log(*a): print(datetime.now(db.TZ_ARG).strftime("%d/%m %H:%M:%S"), *a, flush=True)

def procesar_moneda(m):
    df, fuente = data.velas_cerradas(m)
    P = engine.prep(df)
    try:
        if m in config.CANDIDATAS: raise StopIteration
        import franco
        x = engine.BTC_CTX.get(str(df.index[-1])); franco.actualizar(m, df, P['rsi4'], x[0] if x else None, tg.enviar)
    except StopIteration: pass
    except Exception: log("franco ERROR (no afecta al Shadow):", traceback.format_exc()[-300:])
    n = len(df)
    ult = db.ultima_vela(m)
    ts_last = str(df.index[-1])
    if ult is None:
        # arranque: no se reconstruye el pasado (evita ciclos falsos); se empieza desde la última vela cerrada
        db.set_ultima_vela(m, ts_last, fuente)
        for k in config.configs_de(m): db.estado_set(m, k, engine.nuevo_estado())
        return 0
    nuevas = [j for j in range(n) if str(df.index[j]) > ult]
    if not nuevas: return 0
    if str(df.index[nuevas[0]]) != str(pd.Timestamp(ult) + pd.Timedelta(hours=1)):
        log(f"⚠️ {m}: hueco entre {ult} y {df.index[nuevas[0]]} — se reinician sus ciclos abiertos sin contarlos")
        for k in config.configs_de(m): db.estado_set(m, k, engine.nuevo_estado())
    cerrados = 0
    for k, cfg in config.configs_de(m).items():
        st = db.estado_get(m, k) or engine.nuevo_estado()
        for j in nuevas:
            c = engine.paso(st, P, j, cfg, config.L_FLUJO)
            if c:
                if c.get('L'):   # apalancamiento variable: el PnL se calcula con el apalancamiento real del ciclo
                    pv = -100.0 if c['res'] == 'liq' else float(engine.pnl_puntos(c['ent'], c['salida'], c['side'], c['L'], cfg['fee']))
                    pn = {L: pv for L in config.L_REPORTE}
                else:
                    pn = {L: (-100.0 if c['res'] == 'liq' else float(engine.pnl_puntos(c['ent'], c['salida'], c['side'], L, cfg['fee']))) for L in config.L_REPORTE}
                db.guardar_ciclo(k, m, c, pn[3], pn[5]); cerrados += 1
                if cfg.get('freno_n') and c['res'] in ('sl', 'liq'):
                    try:
                        tf = pd.Timestamp(c['fin']) + pd.Timedelta(hours=1); engine.FRENO_SL.setdefault(k, []).append(tf)
                        hasta = str(tf + pd.Timedelta(hours=cfg['freno_h']))
                        if engine.freno_activo(tf, cfg) and db.meta_get("freno_hasta_" + k) != hasta:
                            db.meta_set("freno_hasta_" + k, hasta)
                            tg.enviar(f"🟡 SHADOW {k}: FRENO activado ({cfg['freno_n']} SL en {cfg['freno_h']} h). No abre ciclos nuevos hasta {hasta[5:16]} UTC (los abiertos siguen).")
                    except Exception: log("freno ERROR (no afecta al Shadow):", traceback.format_exc()[-200:])
                if c['res'] in ('sl', 'liq'):
                    tg.enviar(f"🔴 SHADOW {k} {m} {'LARGO' if c['side']==1 else 'CORTO'} cerró por {c['res'].upper()} con {c['n_ent']} entradas: {pn[5]:+.1f}% margen a {c.get('L') or 5:.0f}x")
        db.estado_set(m, k, st)
    db.set_ultima_vela(m, ts_last, fuente)
    return cerrados

def actualizar_btc():
    """Carga el contexto de BTC (caída/suba 7d, 24h) para la protección de la config I y avisa por Telegram si cambia el estado."""
    try:
        df, _ = data.velas_cerradas("BTC")
        c = df["c"]
        dd = (c / c.rolling(168).max() - 1) * 100
        up = (c / c.rolling(168).min() - 1) * 100
        r24 = c.pct_change(24) * 100
        cfg = config.CONFIGS["I"]
        engine.BTC_CTX = engine.btc_contexto(df.index, dd, up, r24, cfg)
        est = engine.btc_estado(df.index[-1], cfg)
        prev = db.meta_get("btc_estado", "ok")
        if est != prev:
            db.meta_set("btc_estado", est)
            txt = {"ok": "✅ BTC en condiciones normales: la config I vuelve a abrir largos y cortos.",
                   "sin_largos": "🟠 BTC cayó ≥5% desde su máx. de 7 días: la config I NO abre largos nuevos (cortos sí) hasta que BTC recupere a −4% o menos de caída.",
                   "stop": "🛑 BTC en condiciones EXTREMAS (fuera de lo visto en 200 días): la config I NO abre ciclos nuevos."}[est]
            tg.enviar(f"{txt}\nBTC: {dd.iloc[-1]:+.1f}% vs máx 7d · {up.iloc[-1]:+.1f}% vs mín 7d · {r24.iloc[-1]:+.1f}% en 24h")
    except Exception as e:
        log("⚠️ no se pudo actualizar el contexto de BTC (la protección de I queda sin bloquear):", str(e)[:100])

def cargar_freno():
    """Reconstruye desde la base la lista de SL de la config con freno (JF), para que el freno sobreviva a reinicios."""
    try:
        for k, cfg in config.CONFIGS.items():
            if cfg.get('freno_n'):
                engine.FRENO_SL[k] = sorted(pd.Timestamp(r['fin']) for r in db.ciclos(k) if r['res'] in ('sl', 'liq'))
    except Exception: log("freno carga ERROR:", traceback.format_exc()[-200:])

def ciclo_horario():
    actualizar_btc(); cargar_freno()
    ok = cerrados = 0; fallidas = {}
    def intentar(mon):
        nonlocal ok, cerrados
        try: cerrados += procesar_moneda(mon); ok += 1; fallidas.pop(mon, None); return True
        except Exception as e: fallidas[mon] = str(e)[:90]; return False
    for mon in config.MONEDAS:
        intentar(mon); time.sleep(config.PAUSA_ENTRE_MONEDAS_S)
    # candidatas (solo observación): nunca generan alertas ni segunda ronda; las ausentes se reintentan 1 vez por día
    cand_ok = 0; cand_ausentes = []
    for mon in config.CANDIDATAS:
        try:
            k = "cand_ausente_" + mon; t = db.meta_get(k)
            if t and (datetime.now(timezone.utc) - datetime.fromisoformat(t)).total_seconds() < config.REINTENTO_AUSENTE_H * 3600:
                cand_ausentes.append(mon); continue
            try:
                cerrados += procesar_moneda(mon); cand_ok += 1; db.meta_set(k, "")
            except Exception:
                db.meta_set(k, datetime.now(timezone.utc).isoformat()); cand_ausentes.append(mon)
        except Exception: log("candidata ERROR (no afecta al Shadow):", mon, traceback.format_exc()[-200:])
        time.sleep(config.PAUSA_ENTRE_MONEDAS_S)
    db.meta_set("cand_ok", cand_ok); db.meta_set("cand_ausentes", ",".join(cand_ausentes))
    if fallidas:
        log(f"primera ronda: {len(fallidas)} monedas con error, se reintentan en {config.PAUSA_SEGUNDA_RONDA_S}s")
        time.sleep(config.PAUSA_SEGUNDA_RONDA_S)
        for mon in list(fallidas):
            intentar(mon); time.sleep(config.PAUSA_ENTRE_MONEDAS_S)
    # contador de pasadas seguidas con fallo por moneda; aviso solo al llegar al umbral (una vez)
    nuevas_alertas = []
    for mon in config.MONEDAS:
        k = "fallos_" + mon
        if mon in fallidas:
            n = int(db.meta_get(k, 0)) + 1; db.meta_set(k, n)
            if n == config.ALERTA_FALLOS_SEGUIDOS: nuevas_alertas.append(f"{mon}: {fallidas[mon]}")
        elif int(db.meta_get(k, 0)) > 0: db.meta_set(k, 0)
    if nuevas_alertas:
        tg.enviar("⚠️ SHADOW: sin datos de BingX en %d pasadas seguidas para:\n%s" % (config.ALERTA_FALLOS_SEGUIDOS, "\n".join(nuevas_alertas[:10])))
    log(f"ciclo: {ok} monedas ok, {len(fallidas)} con error, {cerrados} ciclos cerrados · candidatas: {cand_ok} ok, {len(cand_ausentes)} sin datos")
    if fallidas: log("errores:", " | ".join(f"{k}: {v}" for k, v in list(fallidas.items())[:10]))
    return ok, len(fallidas), [f"{k}: {v}" for k, v in fallidas.items()]

def recolectar_mstrx():
    """Recolector aparte (solo lectura). Cualquier error se registra y NO afecta al Shadow."""
    try:
        import mstrx_datos
        avisos = mstrx_datos.recolectar()
        try:
            import mstrx_sim
            nc = mstrx_sim.procesar()
            if nc: log(f"mstrx_sim: {nc} ciclos cerrados")
        except Exception:
            log("mstrx_sim ERROR (no afecta al Shadow):", traceback.format_exc()[-400:])
        try:
            import patrones
            patrones.actualizar()
        except Exception:
            log("patrones ERROR (no afecta al Shadow):", traceback.format_exc()[-400:])
        if avisos:
            k = "mstrx_aviso"; hoy = datetime.now(db.TZ_ARG).strftime("%Y%m%d%H")
            if db.meta_get(k) != hoy[:8]:
                db.meta_set(k, hoy[:8]); tg.enviar("⚠️ MSTRX datos: " + " | ".join(avisos[:4]))
        log("mstrx:", "ok" if not avisos else "; ".join(avisos[:3]))
    except Exception:
        log("mstrx ERROR (no afecta al Shadow):", traceback.format_exc()[-400:])

def manejar(cmd):
    p = cmd.split(); c = p[0].lower().split("@")[0]
    if c in ("/estado", "/resumen"):
        d = None
        if len(p) > 1 and p[1].isdigit(): d = str(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(p[1])))
        return reporte.texto_resumen(d, f"últimos {p[1]} días" if d else "acumulado")
    if c == "/abiertos": return reporte.texto_abiertos()
    if c == "/csv": tg.enviar_archivo("ciclos_shadow.csv", reporte.csv_ciclos(), "Ciclos cerrados"); return "CSV enviado."
    if c == "/mstrx":
        import mstrx_datos; return mstrx_datos.resumen()
    if c == "/patrones":
        import patrones; return patrones.texto()
    if c == "/mstrx_res":
        import mstrx_sim; return mstrx_sim.texto_resumen()
    if c == "/mstrx_ciclos":
        import mstrx_sim; tg.enviar_archivo("mstrx_ciclos.csv", mstrx_sim.csv_ciclos(), "Ciclos simulados MSTRX/BTC"); return "CSV de ciclos MSTRX enviado."
    if c == "/mstrx_csv":
        import mstrx_datos
        for par, tf in (("MSTRX_BTC", "15M"), ("MSTRX_BTC", "60M"), ("MSTRX_BTC", "4H"), ("BTC_USDT", "60M"), ("BTC_USDT", "4H")):
            tg.enviar_archivo(f"{par}_{tf}.csv", mstrx_datos.csv_velas(par, tf), f"{par} {tf}")
        return "CSV de MSTRX enviados."
    if c == "/fr":
        import franco; return franco.texto()
    if c == "/fr_abiertos":
        import franco; return franco.texto_abiertos()
    if c == "/fr_csv":
        import franco; tg.enviar_archivo("francotirador.csv", franco.csv_senales(), "Señales del Francotirador"); return "CSV del Francotirador enviado."
    if c == "/cand":
        return texto_candidatas()
    if c == "/cand_csv":
        tg.enviar_archivo("candidatas.csv", reporte.csv_ciclos_cfg("JC"), "Ciclos de las monedas candidatas (JC)"); return "CSV de candidatas enviado."
    if c == "/ayuda": return "/cand · /cand_csv · /resumen [días] · /abiertos · /csv · /mstrx · /mstrx_csv · /mstrx_res · /mstrx_ciclos · /patrones · /fr · /fr_abiertos · /fr_csv"
    return None

def texto_candidatas():
    """Resultado por moneda candidata (config JC, % del margen a 5x). Solo observación."""
    cs = db.ciclos("JC")
    por = {}
    for c in cs: por.setdefault(c['sym'], []).append(c)
    l = ["🔭 <b>Monedas candidatas</b> (J simulada, solo observación)"]
    aus = [x for x in (db.meta_get("cand_ausentes", "") or "").split(",") if x]
    l.append(f"Con datos: {db.meta_get('cand_ok', 0)} de {len(config.CANDIDATAS)} · sin datos de BingX: {', '.join(aus) if aus else 'ninguna'}")
    if not por: l.append("Todavía sin ciclos cerrados."); return "\n".join(l)
    tot = len(cs); win = sum(1 for c in cs if c['pnl5'] > 0); sl = sum(1 for c in cs if c['res'] in ('sl', 'liq'))
    l.append(f"Total: {tot} ciclos · win {100*win/tot:.0f}% · SL/liq {sl} · margen prom {sum(c['pnl5'] for c in cs)/tot:+.1f}%")
    l.append("")
    for s, v in sorted(por.items(), key=lambda x: -sum(c['pnl5'] for c in x[1])):
        w = sum(1 for c in v if c['pnl5'] > 0); sls = sum(1 for c in v if c['res'] in ('sl', 'liq'))
        l.append(f"{s}: {len(v)} ciclos · win {100*w/len(v):.0f}% · SL {sls} · {sum(c['pnl5'] for c in v)/len(v):+.1f}% margen prom")
    return "\n".join(l[:60])

def resumen_diario():
    hoy = datetime.now(db.TZ_ARG).strftime("%Y%m%d")
    if datetime.now(db.TZ_ARG).hour >= config.HORA_RESUMEN_ARG and db.meta_get("resumen_enviado") != hoy:
        d = str(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24))
        tg.enviar(reporte.texto_resumen(d, "últimas 24h") + "\n\n" + reporte.texto_resumen(None, "acumulado"))
        tg.enviar_archivo(f"ciclos_shadow_{hoy}.csv", reporte.csv_ciclos(), "Ciclos cerrados (acumulado)")
        try:
            import mstrx_sim; tg.enviar(mstrx_sim.texto_resumen())
        except Exception: log("resumen mstrx ERROR:", traceback.format_exc()[-300:])
        try:
            import franco; tg.enviar(franco.texto())
        except Exception: log("resumen franco ERROR:", traceback.format_exc()[-300:])
        db.meta_set("resumen_enviado", hoy)

def main():
    db.init()
    log("Bot Oscilación SHADOW arrancando. DB:", config.DB_PATH)
    ok, fallos, errores = ciclo_horario()
    tg.enviar(f"🧪 <b>Bot Oscilación SHADOW</b> arrancó (sin dinero, sin órdenes).\nMonedas leídas: {ok}/{len(config.MONEDAS)}. Candidatas (solo observación, config JC): {len(config.CANDIDATAS)} monedas en prueba, /cand para verlas.\nConfigs: A–K en paralelo (D = B + trailing; E y F = fix22 con apalancamiento variable por ATR%; G y H = F con otra salida; I, J y K = B, D y G + protección por BTC; JF = J + freno (2 SL en 24h → pausa 24h); JT = J con piso de TP 0,40%; JX = J sin las 6 monedas con más SL; JZ = freno + TP 0,40% + sin esas 6 monedas; JS = J con SL 1,5%; todas en observación).\n"
              + ("Con error: " + "; ".join(errores[:8]) if errores else "")
              + ("\nComandos: /resumen [días] /abiertos /csv" if config.ACEPTA_COMANDOS else "\nSolo envía (resumen diario 21:00 + CSV). Para comandos definí OSC_TELEGRAM_TOKEN."))
    recolectar_mstrx()
    proximo = None
    while True:
        try:
            ahora = datetime.now(timezone.utc)
            objetivo = ahora.replace(minute=0, second=25, microsecond=0)
            if objetivo <= ahora: objetivo += timedelta(hours=1)
            while datetime.now(timezone.utc) < objetivo:
                for cmd in tg.comandos():
                    r = manejar(cmd)
                    if r: tg.enviar(r)
                time.sleep(3 if config.ACEPTA_COMANDOS else 20)
            ciclo_horario(); resumen_diario(); recolectar_mstrx()
        except Exception:
            log("ERROR en bucle:", traceback.format_exc()[-600:]); time.sleep(30)

if __name__ == "__main__":
    main()
