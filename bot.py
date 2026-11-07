"""bot.py — lógica de ciclos del bot real (estrategia J = D + protección BTC).

Réplica de engine.paso() pero contra un exchange (real o simulado):
 - señal al cierre de la vela 1h  → entrada a mercado (la vela siguiente abre en ese instante)
 - DCA: orden límite en  última_entrada − lado·dca·ATR  (se recalcula con cada vela, como en el motor)
 - TP (modo "trailing"): al tocarse el TP se coloca un stop con PISO en el TP; al cierre horario, si la vela cerró más allá del TP, el stop
   sigue al máximo con  max(TP, pico − 1×ATR)   (idéntico a la regla del motor, con el stop resting en el exchange)
 - SL 3% del promedio una vez hechas las 6 entradas; antes, stop de emergencia a 12% del promedio
Nada de esto envía órdenes si el exchange es PaperExchange (modo simulado).
"""
import time, json
import numpy as np
import config, engine

class Bot:
    def __init__(self, ex, store, say=print, cfg=None):
        self.ex, self.db, self.say = ex, store, say
        self.cfg = dict(cfg or config.CFG)
        self.P = {}; self.J = {}                 # velas preparadas por moneda (último cierre) y su índice
        self.ultimo_monitor = time.time()
        self.reloj = time_utc; self.hoy = time_arg      # inyectables (las pruebas usan reloj simulado)

    # ── utilidades ──
    @staticmethod
    def _full(sym): return f"{sym}-USDT"
    @staticmethod
    def _avg(ent): return len(ent) / sum(1 / p for p in ent)
    def _ids(self, st): return st.setdefault("ids", {})
    def pausado(self): return self.db.meta_get("pausado", "0") == "1"

    def _cancelar(self, sym, st, clave):
        oid = self._ids(st).pop(clave, None)
        if oid:
            try: self.ex.cancelar(sym, oid)
            except Exception as e: self.db.evento("error_cancelar", sym, f"{clave} {oid}: {e}")

    def _cancelar_dca_seguro(self, sym, st):
        """Cancela la orden de DCA SOLO si sigue sin ejecutarse. Devuelve True si quedó cancelada (o no había).
        Si la orden ya se ejecutó (total o parcial) devuelve False y CONSERVA su id, para que el monitor registre la entrada.
        (Bug 7/11 ENA: se cancelaba una orden que justo se llenaba, se perdía su id y se colocaba otra que se ejecutaba también.)"""
        oid = self._ids(st).get("dca")
        if not oid: return True
        try:
            o = self.ex.orden(sym, oid)
            if o["estado"] in ("FILLED", "PARTIALLY_FILLED"): return False
            self.ex.cancelar(sym, oid)
            o = self.ex.orden(sym, oid)   # re-chequeo: pudo llenarse entre la consulta y la cancelación
            if o["estado"] in ("FILLED", "PARTIALLY_FILLED"): return False
        except Exception as e:
            self.db.evento("error_cancelar_dca", sym, f"{oid}: {str(e)[:100]}"); return False
        self._ids(st).pop("dca", None); return True

    def _stop(self, sym, st, precio):
        """Coloca un stop de cierre. Si el precio de marca ya cruzó el nivel, cierra a mercado (devuelve None)."""
        sd = st["side"]
        try:
            m = self.ex.mark([sym]).get(self._full(sym))
        except Exception: m = None
        if m and ((sd == 1 and m <= precio) or (sd == -1 and m >= precio)):
            self._cerrar_ahora(sym, st, "stop_cruzado"); return None
        try: return self.ex.stop(sym, sd, precio, st["qty"])
        except Exception as e:
            self.db.evento("error_stop", sym, f"{precio}: {e}")
            if m and ((sd == 1 and m <= precio * 1.0005) or (sd == -1 and m >= precio * 0.9995)):
                self._cerrar_ahora(sym, st, "stop_rechazado"); return None
            self.say(f"⚠️ <b>{sym}</b>: no pude colocar el stop en {precio:.6g} ({str(e)[:80]}). Revisar la posición.")
            return None

    def _cerrar_ahora(self, sym, st, motivo):
        """Cierre inmediato a mercado de la posición del ciclo. El registro del ciclo se completa en el monitor."""
        try:
            r = self.ex.cerrar_market(sym, st["side"], st["qty"])
            st["cierre_manual"] = dict(motivo=motivo, avg=r["avg"])
            if st.get("cid"): self.db.guardar(st["cid"], st)     # persistir: el cierre se registra en el próximo monitoreo
            self.db.evento("cierre_mercado", sym, f"{motivo} @ {r['avg']}")
        except Exception as e:
            self.say(f"🚨 <b>{sym}</b>: falló el cierre a mercado ({motivo}): {str(e)[:100]}. CERRAR A MANO.")
            self.db.evento("error_cierre", sym, f"{motivo}: {e}")

    # ── apertura de un ciclo ──
    def puede_abrir(self):
        """(ok, motivo). Límites de cartera y protecciones de capital."""
        if self.pausado(): return False, "pausado"
        ab = self.db.abiertos()
        if len(ab) >= config.MAX_CICLOS: return False, "máx. ciclos"
        eq = self.ex.equity()
        usado = sum(len(st["ent"]) * st["m_ent"] for _, _, st in ab)
        if usado + eq * config.MARGEN_POR_ENTRADA > eq * config.MAX_MARGEN_USADO: return False, "margen usado"
        hoy = self.hoy()
        if self.db.meta_get("dia") != hoy: self.db.meta_set("dia", hoy); self.db.meta_set("eq_dia", eq)
        eq_dia = float(self.db.meta_get("eq_dia", eq))
        if eq < eq_dia * (1 - config.PERDIDA_DIA_MAX):
            if self.db.meta_get("aviso_dia") != hoy:
                self.db.meta_set("aviso_dia", hoy); self.say(f"🟠 Pérdida del día > {config.PERDIDA_DIA_MAX*100:.0f}% del capital: no se abren ciclos nuevos hasta mañana.")
            return False, "pérdida diaria"
        pico = max(float(self.db.meta_get("eq_pico", eq)), eq); self.db.meta_set("eq_pico", pico)
        if eq < pico * (1 - config.DD_MAX_PAUSA):
            self.db.meta_set("pausado", "1"); self.db.meta_set("pausa_motivo", f"caída {100*(1-eq/pico):.1f}% desde el máximo")
            self.say(f"🛑 Capital {100*(1-eq/pico):.1f}% bajo su máximo: <b>ciclos nuevos PAUSADOS</b>. Los abiertos siguen su curso. /reanudar para volver a operar.")
            return False, "caída máx."
        return True, ""

    @staticmethod
    def impulso_contra(P, j, side):
        """Movimiento de la última hora EN CONTRA de `side`, en unidades de ATR(14, media simple del rango verdadero) / precio."""
        if j < 15: return 0.0
        h, l, c = P["h"], P["l"], P["c"]
        tr = [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1])) for k in range(j - 13, j + 1)]
        atrp = (sum(tr) / 14) / c[j]
        if not atrp > 0: return 0.0
        return -side * (c[j] / c[j - 1] - 1) / atrp

    def lado_senal(self, P, j, btc_est, sym=""):
        cfg = self.cfg; a = P["adx"][j]; r = P["rsi4"][j]; side = 0
        if a == a and r == r and a < cfg["adx_th"]:
            if btc_est == "stop": return 0
            if r <= cfg["rl"]: side = 0 if btc_est == "sin_largos" else 1
            elif r >= cfg["rs"]: side = -1
        if side and config.IMPULSO_MAX_ATR and self.impulso_contra(P, j, side) >= config.IMPULSO_MAX_ATR:
            self.db.evento("omitido_impulso", sym, f"señal {'L' if side==1 else 'C'} descartada: última hora ≥{config.IMPULSO_MAX_ATR} ATR en contra")
            return 0
        return side

    def abrir(self, sym, side, P, j):
        cfg = self.cfg; L = config.APALANCAMIENTO
        info = self.ex.info(sym)
        if not info.get("abierto", True): self.db.evento("omitido", sym, "contrato sin trading API"); return None
        eq = self.ex.equity(); m_ent = eq * config.MARGEN_POR_ENTRADA
        px = self.ex.mark([sym])[self._full(sym)]
        qty = m_ent * L / px
        if qty < info["min_qty"] or m_ent * L < info["min_usdt"] * 1.2:
            self.db.evento("omitido_minimo", sym, f"qty {qty:.6g} < mín {info['min_qty']} o nocional {m_ent*L:.2f} bajo mínimo"); return None
        self.ex.preparar(sym, side)
        f = self.ex.abrir_market(sym, side, qty)
        st = dict(sym=sym, side=side, L=L, m_ent=m_ent, ent=[float(f["avg"])], qty=float(f["qty"]), t0=self.reloj(), equity_ini=eq,
                  tp=None, tocado=False, trail=False, peak=None, stop=None, ids={}, dca_lvl=None, j_senal=str(P["idx"][j]))
        st["tp"] = float(engine._tp_price(P, j, st["ent"], side, cfg))
        st["cid"] = self.db.nuevo(sym, st)
        self.P[sym] = P; self.J[sym] = j
        self.reponer(sym, st)
        self.db.evento("abre", sym, f"{'L' if side==1 else 'C'} @ {f['avg']} qty {f['qty']} margen {m_ent:.2f}")
        self.say(f"🟢 <b>{sym}</b> {'LARGO' if side==1 else 'CORTO'} abierto @ {f['avg']:.6g} · {L}x · margen {m_ent:.2f} USDT ({config.MARGEN_POR_ENTRADA*100:.0f}% del capital) · TP {st['tp']:.6g}"
                 + (" · <i>(simulado)</i>" if self.ex.modo() != "real" else ""))
        return st

    # ── órdenes en reposo de un ciclo abierto ──
    def reponer(self, sym, st):
        """Recoloca SL/emergencia, DCA y (modo límite) TP según el estado actual. Usa la última vela cerrada (self.P/self.J)."""
        cfg = self.cfg; sd = st["side"]; n = len(st["ent"]); P = self.P[sym]; j = self.J[sym]
        ap = self._avg(st["ent"])
        self._cancelar(sym, st, "sl")
        pct = cfg["sl_dist"] if n >= cfg["max_e"] else config.SL_EMERGENCIA_PCT
        oid = self._stop(sym, st, ap * (1 - sd * pct / 100))
        if oid: self._ids(st)["sl"] = oid
        else:
            if st.get("cierre_manual"): return
        dca_libre = self._cancelar_dca_seguro(sym, st)
        if dca_libre and n < cfg["max_e"] and not st["tocado"] and not st["trail"]:
            lvl = st["ent"][-1] - sd * cfg["dca"] * P["atr"][j]
            q = st["m_ent"] * st["L"] / lvl
            try:
                self._ids(st)["dca"] = self.ex.limite(sym, sd, q, lvl); st["dca_lvl"] = float(lvl)
            except Exception as e:
                self.db.evento("error_dca", sym, f"{lvl}: {e}"); self.say(f"⚠️ <b>{sym}</b>: no pude colocar la orden de DCA ({str(e)[:80]}).")
        if config.TP_MODO == "limite":
            self._cancelar(sym, st, "tp")
            try: self._ids(st)["tp"] = self.ex.limite(sym, sd, st["qty"], st["tp"], cierre=True)
            except Exception as e: self.db.evento("error_tp", sym, str(e))
        self.db.guardar(st["cid"], st)

    # ── monitor (cada POLL_S): fills, toque del TP, cierres ──
    def monitorear(self):
        self.ultimo_monitor = time.time()
        ab = self.db.abiertos()
        if not ab: return
        pos = self.ex.posiciones(); abiertas = {o["id"] for o in self.ex.ordenes_abiertas()}; marks = self.ex.mark()
        for cid, sym, st in ab:
            st["cid"] = cid
            try: self._monitor_ciclo(sym, st, pos, abiertas, marks)
            except Exception as e:
                self.db.evento("error_monitor", sym, repr(e)); print("error monitor", sym, repr(e), flush=True)

    def _monitor_ciclo(self, sym, st, pos, abiertas, marks):
        cfg = self.cfg; sd = st["side"]; full = self._full(sym)
        p = pos.get((full, sd))
        if p is None: self._registrar_cierre(sym, st); return
        # ¿se ejecutó la orden de DCA?
        did = self._ids(st).get("dca")
        if did and did not in abiertas:
            o = self.ex.orden(sym, did)
            if o["estado"] == "FILLED":
                st["ent"].append(float(o["avg"] or st["dca_lvl"])); st["qty"] = float(p["qty"]); self._ids(st).pop("dca", None)
                self.db.guardar(st["cid"], st)   # se guarda YA: si algo falla después, el fill no se vuelve a procesar en cada vuelta del monitor
                P = self.P.get(sym); j = self.J.get(sym)
                if P is None:   # tras un reinicio aún no hay velas cargadas: se piden ahora
                    try:
                        import data
                        df, _ = data.velas_cerradas(sym); P = engine.prep(df); j = len(df) - 1; self.P[sym] = P; self.J[sym] = j
                    except Exception as e: self.db.evento("error_velas", sym, str(e)[:100])
                if P is not None: st["tp"] = float(engine._tp_price(P, j, st["ent"], sd, cfg))
                self.db.evento("dca", sym, f"entrada {len(st['ent'])} @ {st['ent'][-1]}")
                self.say(f"➕ <b>{sym}</b> entrada {len(st['ent'])}/{cfg['max_e']} @ {st['ent'][-1]:.6g} · promedio {self._avg(st['ent']):.6g} · TP {st['tp']:.6g}")
                if P is not None: self.reponer(sym, st)
                else: self.say(f"⚠️ <b>{sym}</b>: sin velas para recolocar stops; se recolocan en la próxima gestión horaria.")
                self.db.guardar(st["cid"], st)
                return
            elif o["estado"] in ("CANCELED", "FAILED"): self._ids(st).pop("dca", None); self.db.guardar(st["cid"], st)
        # recuento: las entradas que implica la posición real (cantidad x promedio / nocional por entrada) vs las registradas
        if not (did and did in abiertas) and self._recontar(sym, st, p): return
        # cantidad real distinta de la registrada (p. ej. fill parcial): se sincroniza y se recolocan los stops
        if abs(p["qty"] - st["qty"]) > 1e-9 and not (did and did in abiertas):
            st["qty"] = float(p["qty"]); self.db.guardar(st["cid"], st)
        # toque del TP (modo trailing): se coloca el stop con piso en el TP
        if config.TP_MODO == "trailing" and not st["tocado"]:
            m = marks.get(full)
            if m and ((sd == 1 and m >= st["tp"]) or (sd == -1 and m <= st["tp"])):
                if not self._cancelar_dca_seguro(sym, st): return   # DCA llenándose: se procesa primero en la próxima vuelta
                st["tocado"] = True
                self._cancelar(sym, st, "trl")
                oid = self._stop(sym, st, st["tp"])
                if oid: self._ids(st)["trl"] = oid; st["stop"] = float(st["tp"])
                self.db.guardar(st["cid"], st); self.db.evento("toca_tp", sym, f"mark {m} ≥ TP {st['tp']}")

    def _recontar(self, sym, st, p):
        """Si BingX tiene más entradas que las registradas (fill perdido), completa las que faltan con el promedio real. True si corrigió."""
        cfg = self.cfg; sd = st["side"]
        try:
            nocional = st["m_ent"] * st["L"]
            x = p["qty"] * p["avg"] / nocional if nocional > 0 and p.get("avg", 0) > 0 else 0
            n_real = int(round(x)); n = len(st["ent"])
            if n_real <= n or abs(x - n_real) > 0.3: return False
            k = n_real - n
            inv = n_real / p["avg"] - sum(1 / e for e in st["ent"])
            if inv <= 0: return False
            px = 1 / (inv / k)
            if not (0.5 * p["avg"] < px < 1.5 * p["avg"]): return False
            st["ent"] += [float(px)] * k; st["qty"] = float(p["qty"])
            P = self.P.get(sym); j = self.J.get(sym)
            if P is not None: st["tp"] = float(engine._tp_price(P, j, st["ent"], sd, cfg))
            self.db.guardar(st["cid"], st)
            self.db.evento("recuento_corregido", sym, f"entradas {n}→{n_real} (posición real {p['qty']} @ {p['avg']})")
            self.say(f"🛠 <b>{sym}</b>: el recuento no coincidía con BingX. Entradas {n}→{n_real} · promedio real {p['avg']:.6g} · TP {st['tp']:.6g}")
            if P is not None: self.reponer(sym, st)
            return True
        except Exception as e:
            self.db.evento("error_recuento", sym, repr(e)[:100]); return False

    def _registrar_cierre(self, sym, st):
        """La posición desapareció: se averigua cómo cerró, se registra y se limpian las órdenes."""
        cfg = self.cfg; sd = st["side"]; res = None; salida = None
        man = st.get("cierre_manual")
        if man: res, salida = man["motivo"], man["avg"]
        else:
            for clave, nombre in (("trl", "trail" if st["trail"] else "tp"), ("tp", "tp"), ("sl", "sl" if len(st["ent"]) >= cfg["max_e"] else "sl_emerg")):
                oid = self._ids(st).get(clave)
                if not oid: continue
                try: o = self.ex.orden(sym, oid)
                except Exception: continue
                if o["estado"] == "FILLED":
                    res, salida = nombre, float(o["avg"] or 0)
                    if salida <= 0:   # BingX a veces devuelve avgPrice 0 en stops ejecutados: se usa el precio del stop (o el TP) como estimación
                        est = float(o.get("stop") or 0) or float(st.get("stop") or 0) or float(st.get("tp") or 0)
                        if est > 0:
                            salida = est; self.db.evento("salida_estimada", sym, f"avgPrice=0 en {nombre}; se usa {est}")
                    break
        if res is None:
            try: m = self.ex.mark([sym])[self._full(sym)]
            except Exception: m = None
            w1 = None if not m else float(np.mean([(m / e - 1) * sd for e in st["ent"]]) * 100)
            res = "liq" if (w1 is not None and w1 * st["L"] <= -90) else "externo"; salida = m
        try: self.ex.cancelar_todas(sym)
        except Exception as e: self.db.evento("error_cancelar", sym, str(e))
        n = len(st["ent"])
        if res == "liq": pnl = -100.0
        else: pnl = float(engine.pnl_puntos(st["ent"], salida, sd, st["L"], cfg["fee"])) if salida else 0.0
        usdt = pnl / 100 * n * st["m_ent"]
        dur = (np.datetime64(self.reloj()) - np.datetime64(st["t0"])) / np.timedelta64(1, "h")
        self.db.cerrar(st["cid"], st, salida, res, pnl, usdt, float(dur))
        self.db.evento("cierra", sym, f"{res} salida {salida} pnl {pnl:+.2f}% margen")
        ic = {"tp": "✅", "trail": "✅", "sl": "🔴", "sl_emerg": "🔴", "liq": "💥"}.get(res, "⚪")
        self.say(f"{ic} <b>{sym}</b> cerró por <b>{res.upper()}</b> · {n} entradas · {pnl:+.2f}% del margen ({usdt:+.2f} USDT)"
                 + (" · <i>(simulado)</i>" if self.ex.modo() != "real" else ""))

    # ── gestión al cierre de cada vela 1h ──
    def gestion_hora(self, sym, st, P, j):
        cfg = self.cfg; sd = st["side"]; self.P[sym] = P; self.J[sym] = j
        c, h, l = P["c"][j], P["h"][j], P["l"][j]
        if config.TP_MODO == "trailing":
            if not st["tocado"] and ((sd == 1 and h >= st["tp"]) or (sd == -1 and l <= st["tp"])):
                # el monitor no vio el toque (pico más corto que POLL_S): se procesa con la vela cerrada
                if (sd == 1 and c > st["tp"]) or (sd == -1 and c < st["tp"]):
                    if not self._cancelar_dca_seguro(sym, st): self.db.guardar(st["cid"], st); return   # DCA llenándose: el monitor lo procesa
                    st["tocado"] = True
                else:
                    self._cerrar_ahora(sym, st, "tp_tardio"); return
            if st["tocado"]:
                if not st["trail"]:
                    if (sd == 1 and c > st["tp"]) or (sd == -1 and c < st["tp"]): st["trail"] = True; st["peak"] = float(c)
                else:
                    st["peak"] = float(max(st["peak"], h) if sd == 1 else min(st["peak"], l))
                if st["trail"]:
                    atr = P["atr"][j - 1]; tx = cfg["trail_x"]
                    nuevo = max(st["tp"], st["peak"] - tx * atr) if sd == 1 else min(st["tp"], st["peak"] + tx * atr)
                    if st["stop"] is None or abs(nuevo - st["stop"]) / st["stop"] > 1e-4:
                        self._cancelar(sym, st, "trl")
                        oid = self._stop(sym, st, nuevo)
                        if oid: self._ids(st)["trl"] = oid; st["stop"] = float(nuevo)
                self.db.guardar(st["cid"], st); return
        # sin TP tocado: la orden de DCA sigue la regla del motor (nivel con el ATR de la última vela)
        if len(st["ent"]) < cfg["max_e"]:
            lvl = st["ent"][-1] - sd * cfg["dca"] * P["atr"][j]
            if st.get("dca_lvl") is None or abs(lvl - st["dca_lvl"]) / lvl > 1e-5 or not self._ids(st).get("dca"):
                if self._cancelar_dca_seguro(sym, st):
                    q = st["m_ent"] * st["L"] / lvl
                    try: self._ids(st)["dca"] = self.ex.limite(sym, sd, q, lvl); st["dca_lvl"] = float(lvl)
                    except Exception as e: self.db.evento("error_dca", sym, str(e))
        self.db.guardar(st["cid"], st)

    @staticmethod
    def atr_pct(P, j):
        """ATR(14, media simple del rango verdadero) como % del precio en la vela j (mismo criterio que el filtro anti-impulso)."""
        if j < 15: return 0.0
        h, l, c = P["h"], P["l"], P["c"]
        tr = [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1])) for k in range(j - 13, j + 1)]
        return (sum(tr) / 14) / c[j] * 100

    def evaluar_vela(self, sym, P, j, btc_est):
        """Cierre de vela de `sym`: gestiona el ciclo abierto y, si no lo hay, devuelve la señal candidata (sym, side, atr%, P, j) o None.
        La apertura se decide después, con todas las candidatas de la hora juntas (ver abrir_candidatas)."""
        ab = self.db.abierto_de(sym)
        if ab:
            cid, st = ab; st["cid"] = cid; self.gestion_hora(sym, st, P, j); return None
        side = self.lado_senal(P, j, btc_est, sym)
        if not side: return None
        # potencial de la señal = volatilidad (ATR%) x qué tan extremo está el RSI 4h (|RSI-50|): más recorrido esperado y más sobreextensión
        return (sym, side, self.atr_pct(P, j) * abs(P["rsi4"][j] - 50), P, j)

    def abrir_candidatas(self, cands):
        """Abre como máximo MAX_APERTURAS_HORA ciclos, empezando por las de mayor potencial (ATR% x |RSI4h-50|). Devuelve la cantidad abierta."""
        cands = sorted(cands, key=lambda x: -x[2]); lim = config.MAX_APERTURAS_HORA or 10 ** 6; n = 0
        for sym, side, pot, P, j in cands:
            ok, motivo = self.puede_abrir()
            if not ok: break
            if n >= lim:
                self.db.evento("omitido_cupo_hora", sym, f"señal {'L' if side==1 else 'C'} potencial {pot:.1f} sin cupo (máx {lim}/hora)"); continue
            if self.abrir(sym, side, P, j): n += 1
        return n

    def procesar_vela(self, sym, P, j, btc_est):
        """Llamar al cierre de la vela j de `sym` (después de actualizar BTC_CTX). Devuelve 'abre' si abrió un ciclo."""
        ab = self.db.abierto_de(sym)
        if ab:
            cid, st = ab; st["cid"] = cid; self.gestion_hora(sym, st, P, j); return "gestion"
        ok, _ = self.puede_abrir()
        if not ok: return None
        side = self.lado_senal(P, j, btc_est, sym)
        if side: return "abre" if self.abrir(sym, side, P, j) else None
        return None

def time_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
def time_arg():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")