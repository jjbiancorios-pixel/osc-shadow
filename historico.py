"""historico.py — velas HISTÓRICAS (15m, 1h, 4h) para el proyecto "Huellas" (operaciones manuales de Juanjo en Bitunix).
Fuentes, en orden: Bitunix (donde operó), BingX, Binance. Solo lectura de datos públicos: no opera ni toca dinero.
Comandos: /velas_huellas  ·  /velas_hist MONEDA AAAA-MM-DD [días]
Horas de las operaciones: hora Argentina (UTC−3). Los CSV salen en UTC (t = apertura de la vela, en ms)."""
import io, time, zipfile, threading, requests, pandas as pd
import config, tg

TF_MS = {"15m": 15 * 60_000, "1h": 3_600_000, "4h": 4 * 3_600_000}
# cuánto antes de la apertura y después del cierre se baja, por temporalidad
VENTANA = {"4h": (pd.Timedelta(days=12), pd.Timedelta(days=2)),
           "1h": (pd.Timedelta(days=4), pd.Timedelta(days=1)),
           "15m": (pd.Timedelta(days=1), pd.Timedelta(hours=8))}
ART = pd.Timedelta(hours=3)   # hora Argentina + 3 h = UTC

# Operaciones de Juanjo (tandas 1 y 2, 20/11/2026): moneda, apertura, cierre (hora Argentina)
OPERACIONES = """LTC,2026-05-18 14:25:01,2026-05-19 11:44:36
FIL,2026-05-15 19:41:36,2026-05-17 20:41:13
ONDO,2026-05-19 16:37:47,2026-05-19 20:17:57
INJ,2026-05-19 13:26:11,2026-05-19 13:32:50
BSB,2026-05-19 20:48:39,2026-05-19 22:06:01
BSB,2026-05-19 18:39:57,2026-05-19 20:32:51
BSB,2026-05-19 22:08:00,2026-05-19 22:16:35
INJ,2026-05-19 15:30:34,2026-05-19 22:15:30
NFP,2026-05-18 19:43:00,2026-05-19 23:13:36
HYPE,2026-05-18 09:35:22,2026-05-19 22:46:34
SAFE,2026-05-20 14:19:26,2026-05-20 14:57:48
BSB,2026-05-19 22:36:41,2026-05-19 23:54:30
BSB,2026-05-20 17:50:00,2026-05-20 23:41:53
BSB,2026-05-20 14:32:10,2026-05-20 15:21:21
BSB,2026-05-21 10:43:59,2026-05-21 11:29:53
BSB,2026-05-21 00:03:37,2026-05-21 10:14:25
BILL,2026-05-21 15:10:23,2026-05-22 02:30:30
ONDO,2026-05-21 12:32:34,2026-05-21 13:53:39
HYPE,2026-05-21 18:23:12,2026-05-22 10:11:18
YGG,2026-04-25 20:30:26,2026-04-26 12:15:12
INJ,2026-04-24 21:18:14,2026-04-25 16:31:17
PENGU,2026-04-28 22:19:53,2026-04-28 22:43:46
PENGU,2026-04-28 13:44:14,2026-04-28 20:15:13
POL,2026-05-09 12:20:04,2026-05-09 12:35:28
POL,2026-05-08 13:48:20,2026-05-09 11:32:32
CHIP,2026-05-09 14:08:46,2026-05-09 19:41:08
ATOM,2026-05-09 13:15:38,2026-05-09 13:27:21
BILL,2026-05-10 17:08:24,2026-05-10 20:47:02
ATOM,2026-05-09 15:26:25,2026-05-10 07:16:59
NEAR,2026-05-12 12:23:18,2026-05-13 02:20:16
SAHARA,2026-05-10 13:04:29,2026-05-11 01:21:04
ADA,2026-05-16 13:15:50,2026-05-17 02:07:43
CRV,2026-05-14 14:27:31,2026-05-14 15:19:56"""

_ocupado = threading.Lock()

def _ms(ts): return int((ts - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1))

def _fila(k):
    if isinstance(k, dict):
        t = k.get("time", k.get("t", k.get("openTime")))
        v = k.get("baseVol", k.get("volume", k.get("vol", k.get("v", 0))))
        return (int(float(t)), float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]), float(v or 0))
    return (int(float(k[0])), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))

def _bitunix(m, tf, a, b, lim=200):
    r = requests.get("https://fapi.bitunix.com/api/v1/futures/market/kline",
                     params={"symbol": f"{m}USDT", "interval": tf, "startTime": a, "endTime": b, "limit": lim}, timeout=20).json()
    d = r.get("data") if isinstance(r, dict) else None
    if isinstance(d, dict): d = d.get("list") or d.get("data")
    if not d: raise ValueError(f"Bitunix sin datos: {str(r)[:100]}")
    return [_fila(k) for k in d], lim

def _bingx(m, tf, a, b, lim=1000):
    r = requests.get(f"{config.BINGX_URL}/openApi/swap/v3/quote/klines",
                     params={"symbol": f"{m}-USDT", "interval": tf, "startTime": a, "endTime": b, "limit": lim}, timeout=20).json()
    d = r.get("data") or []
    if not d: raise ValueError(f"BingX sin datos: {str(r)[:100]}")
    return [_fila(k) for k in d], lim

def _binance(m, tf, a, b, lim=1500):
    r = requests.get(f"{config.BINANCE_URL}/fapi/v1/klines",
                     params={"symbol": f"{m}USDT", "interval": tf, "startTime": a, "endTime": b, "limit": lim}, timeout=20).json()
    if not isinstance(r, list) or not r: raise ValueError(f"Binance sin datos: {str(r)[:100]}")
    return [_fila(k) for k in r], lim

def bajar(m, tf, ini, fin):
    """ini/fin: Timestamps UTC. Devuelve (DataFrame t,o,h,l,c,v ordenado, fuente) o lanza error con el motivo de cada fuente."""
    a0, b0 = _ms(ini), _ms(fin); paso = TF_MS[tf]; errores = []
    for fuente, f in (("bitunix", _bitunix), ("bingx", _bingx), ("binance", _binance)):
        try:
            filas = []; a = a0
            while a < b0:
                try: lote, _ = f(m, tf, a, min(b0, a + paso * lim_de(f)))
                except Exception:
                    if filas: break          # ya hay velas: una página vacía al final del rango no es error
                    raise
                lote = [x for x in lote if a0 <= x[0] <= b0]
                if not lote: break
                filas += lote
                ult = max(x[0] for x in lote)
                if ult < a: break
                a = ult + 1   # la API incluye startTime: la próxima vela empieza después de la última recibida
                time.sleep(0.25)
            if not filas: raise ValueError("sin velas en el rango")
            df = pd.DataFrame(filas, columns=["t", "o", "h", "l", "c", "v"]).drop_duplicates("t").sort_values("t")
            return df, fuente
        except Exception as e:
            errores.append(f"{fuente}: {str(e)[:70]}")
    raise ValueError(" | ".join(errores))

def lim_de(f): return {_bitunix: 200, _bingx: 1000, _binance: 1500}[f] - 1

def _ventanas():
    """Por moneda: primera apertura y último cierre (UTC)."""
    v = {}
    for ln in OPERACIONES.strip().splitlines():
        m, ap, ci = [x.strip() for x in ln.split(",")]
        ap = pd.Timestamp(ap) + ART; ci = pd.Timestamp(ci) + ART
        a, b = v.get(m, (ap, ci)); v[m] = (min(a, ap), max(b, ci))
    return v

def _armar_zip(pedidos, nombre, titulo):
    """pedidos: {moneda: (ini_utc, fin_utc)} → zip con 3 temporalidades por moneda + registro de fuentes."""
    buf = io.BytesIO(); log = []; ok = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for m, (ap, ci) in pedidos.items():
            for tf, (antes, despues) in VENTANA.items():
                try:
                    df, fuente = bajar(m, tf, ap - antes, ci + despues)
                    z.writestr(f"{m}USDT_{tf}.csv", df.to_csv(index=False)); ok += 1
                    log.append(f"{m} {tf}: {len(df)} velas ({fuente})")
                except Exception as e:
                    log.append(f"{m} {tf}: FALLÓ → {e}")
        z.writestr("fuentes.txt", "Horas en UTC; t = apertura de la vela (ms).\n" + "\n".join(log))
        z.writestr("operaciones.csv", "moneda,apertura_ART,cierre_ART\n" + OPERACIONES.strip() + "\n")
    fallas = [x for x in log if "FALLÓ" in x]
    tg.enviar_archivo(nombre, buf.getvalue(), f"{titulo}: {ok} archivos" + (f" · {len(fallas)} fallaron (ver fuentes.txt)" if fallas else ""))

def _correr(pedidos, nombre, titulo):
    if not _ocupado.acquire(blocking=False): tg.enviar("⏳ Ya estoy bajando velas; esperá a que llegue el archivo anterior."); return
    try: _armar_zip(pedidos, nombre, titulo)
    except Exception as e: tg.enviar(f"❌ Error bajando velas: {str(e)[:200]}")
    finally: _ocupado.release()

def comando(p):
    """p = palabras del comando. Devuelve el texto de respuesta inmediata."""
    c = p[0].lower().split("@")[0]
    if c == "/velas_huellas":
        v = _ventanas()
        threading.Thread(target=_correr, args=(v, "velas_huellas.zip", "Velas Huellas"), daemon=True).start()
        return f"Bajando velas 15m/1h/4h de {len(v)} monedas de tus operaciones (Bitunix → BingX → Binance). Tarda unos minutos; llega un ZIP."
    if c == "/velas_hist":
        if len(p) < 3: return "Uso: /velas_hist MONEDA AAAA-MM-DD [días]   (ej: /velas_hist NEAR 2026-05-12 2)"
        m = p[1].upper().replace("USDT", "").replace("-", "")
        try: d0 = pd.Timestamp(p[2]) + ART
        except Exception: return "Fecha inválida. Formato AAAA-MM-DD."
        dias = int(p[3]) if len(p) > 3 and p[3].isdigit() else 1
        threading.Thread(target=_correr, args=({m: (d0, d0 + pd.Timedelta(days=dias))}, f"velas_{m}_{p[2]}.zip", f"Velas {m} {p[2]}"), daemon=True).start()
        return f"Bajando velas 15m/1h/4h de {m} alrededor del {p[2]} ({dias} día/s). Llega un ZIP."
    return None
