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
    if c == "/velas_rey":
        threading.Thread(target=_correr_rey, daemon=True).start()
        return f"Bajando velas 4h/1h/15m de {len(_rey_pedidos())} señales del canal Rey (Bitunix → BingX → Binance). Tarda 5–10 minutos; llega un ZIP."
    if c == "/velas_hist":
        if len(p) < 3: return "Uso: /velas_hist MONEDA AAAA-MM-DD [días]   (ej: /velas_hist NEAR 2026-05-12 2)"
        m = p[1].upper().replace("USDT", "").replace("-", "")
        try: d0 = pd.Timestamp(p[2]) + ART
        except Exception: return "Fecha inválida. Formato AAAA-MM-DD."
        dias = int(p[3]) if len(p) > 3 and p[3].isdigit() else 1
        threading.Thread(target=_correr, args=({m: (d0, d0 + pd.Timedelta(days=dias))}, f"velas_{m}_{p[2]}.zip", f"Velas {m} {p[2]}"), daemon=True).start()
        return f"Bajando velas 15m/1h/4h de {m} alrededor del {p[2]} ({dias} día/s). Llega un ZIP."
    return None


# ───────── /velas_rey: señales del canal "El Rey Rentable" (125, del 01/05 al 21/11/2026; hora Argentina) ─────────
# n,moneda,lado,fecha,hora   (hora "22:2" o fecha con "?" = dato incompleto: se usa lo que se pueda)
REY = """1,QNT,L,2026-11-21,11:23
2,KERNEL,C,2026-11-20,10:33
3,NAORIS,L,2026-11-19,12:52
4,PUMPFUN,L,2026-11-18,22:2
5,EVAA,L,2026-11-15,17:42
6,NEAR,L,2026-11-16,11:38
7,STRK,L,2026-11-13,11:27
8,INJ,L,2026-11-12,11:43
9,1000PEPE,L,2026-11-11,11:18
10,NEAR,L,2026-11-08,11:08
11,BCH,L,2026-11-08,18:25
12,ASTER,L,2026-11-09,13:46
13,DOT,L,2026-11-06,12:19
14,ZEC,L,2026-11-05,12:52
15,WLD,L,2026-11-03,10:43
16,ENA,L,2026-11-01,18:21
17,LDO,C,2026-11-02,22:08
18,APT,L,2026-10-31,12:39
19,TAO,L,2026-10-30,14:18
20,AKE,L,2026-10-27,11:54
21,ENA,L,2026-10-26,13:07
22,SUI,L,2026-10-25,17:34
23,ORDER,L,2026-10-25,10:54
24,WOO,L,2026-10-24,10:59
25,TAO,C,2026-10-22,11:01
26,STRK,L,2026-10-21,16:26
27,SUI,L,2026-10-21,11:06
28,LSK,L,2026-10-20,23:21
29,ETC,L,2026-10-19,22:33
30,ENA,L,2026-10-10,11:14
31,WLD,L,2026-10-11,12:15
32,SUI,L,2026-10-11,17:34
33,GRASS,L,2026-10-11,20:35
34,UNI,L,2026-10-12,13:13
35,ENA,L,2026-10-13,16:17
36,OP,L,2026-10-15,11:47
37,STRK,L,2026-10-16,10:34
38,ASTER,L,2026-10-17,11:20
39,TIA,C,2026-10-17,12:02
40,NEAR,L,2026-10-18,10:37
41,TAO,L,2026-10-19,19:42
42,STRK,C,2026-10-02,16:37
43,WLD,L,2026-10-01,19:54
44,XPL,L,2026-10-03,11:06
45,ASTER,L,2026-10-04,12:02
46,ARB,L,2026-10-05,16:52
47,PUMPFUN,L,2026-10-07,22:21
48,TIA,L,2026-10-09,12:52
49,MINA,L,2026-10-08,21:48
50,ZEC,L,2026-10-08,11:03
51,ARB,L,2026-09-17,18:16
52,HEMI,L,2026-09-18,20:45
53,USELESS,C,2026-09-20,11:14
54,VVV,C,2026-09-21,00:10
55,NEAR,L,2026-09-21,17:21
56,SUI,C,2026-09-21,22:23
57,TAO,C,2026-09-22,13:02
58,ASTER,L,2026-09-23,12:39
59,FARTCOIN,L,2026-09-24,21:09
60,XPL,L,2026-09-25,21:34
61,TST,L,2026-09-26,11:18
62,NEAR,C,2026-09-27,13:55
63,HBAR,C,2026-09-28,09:55
64,NEAR,C,2026-09-29,12:10
65,QNT,C,2026-09-30,16:15
66,AAVE,C,2026-08-24,12:41
67,ICP,L,2026-08-25,19:58
68,FARTCOIN,C,2026-08-26,15:52
69,WIF,L,2026-08-27,14:00
70,ENA,L,2026-08-29,12:08
71,NEAR,L,2026-08-30,21:38
72,FARTCOIN,L,2026-08-31,12:03
73,ARB,L,2026-09-02,12:37
74,BTR,L,2026-09-02,20:02
75,CAP,L,2026-09-03,20:40
76,USELESS,C,2026-09-04,11:59
77,ASTER,L,2026-09-04,17:42
78,TRUMP,L,2026-09-05,13:30
79,WLD,L,2026-09-10,11:00
80,ENA,L,2026-09-10,19:30
81,PUMPFUN,L,2026-09-12,11:52
82,SUI,C,2026-09-13,11:09
83,ARK,L,2026-09-15,12:42
84,AIN,C,2026-09-15,14:54
85,TIA,L,2026-09-16,18:05
86,1000PEPE,C,2026-07-17,19:25
87,NEAR,L,2026-07-18,12:50
88,BASED,L,2026-07-19,15:17
89,APT,L,2026-07-20,18:50
90,ENA,L,2026-07-22,11:13
91,ICP,L,2026-07-23,15:27
92,PUMP,L,2026-07-25,21:10
93,HYPE,L,2026-07-28,13:24
94,POL,L,2026-07-29,11:27
95,ADA,L,2026-07-31,12:58
96,OP,L,2026-08-06,15:26
97,HFT,L,2026-08-06,22:41
98,NEAR,L,2026-08-11,17:48
99,AVAAI,L,2026-08-12,19:56
100,XRP,L,2026-08-14,11:24
101,LTC,L,2026-08-14,21:20
102,NEAR,L,2026-08-17,17:47
103,HEMI,L,2026-08-21,19:12
104,ONDO,L,2026-08-22,11:24
105,PENGU,L,2026-08-22,14:41
106,ORCA,C,2026-05-01,21:11
107,ALCH,L,2026-05-03,21:13
108,CHIP,L,2026-05-09,14:01
109,SAHARA,C,2026-05-10,12:40
110,USELESS,L,2026-05-10,15:23
111,BILL,L,2026-05-10,17:01
112,KERNEL,L,2026-05-15,12:57
113,BSB,L,2026-05-19,16:31
114,NEAR,C,2026-05-23,12:48
115,EIGEN,L,2026-05-?,11:13
116,ENA,L,2026-06-14,12:05
117,RE,L,2026-06-22,19:55
118,BOME,L,2026-06-27,11:52
119,LAB,L,2026-06-28,11:15
120,NEAR,L,2026-06-28,15:07
121,LAB,C,2026-06-29,19:22
122,SUI,L,2026-07-11,12:12
123,VELVET,L,2026-07-12,12:10
124,BCH,L,2026-07-15,16:08
125,ENA,L,2026-07-17,11:02"""
ALIAS = {"PUMPFUN": ["PUMP"], "PUMP": ["PUMPFUN"], "1000PEPE": ["PEPE"]}
VENT_REY = {"4h": (pd.Timedelta(days=10), pd.Timedelta(days=4)),
            "1h": (pd.Timedelta(days=3), pd.Timedelta(days=4)),
            "15m": (pd.Timedelta(hours=12), pd.Timedelta(days=2))}

def _rey_pedidos():
    out = []
    for ln in REY.strip().splitlines():
        n, m, lado, f, h = [x.strip() for x in ln.split(",")]
        if "?" in f: continue
        try:
            hh = h if len(h) == 5 else "12:00"
            t = pd.Timestamp(f"{f} {hh}") + ART
        except Exception: continue
        out.append((n, m, t))
    return out

def _bajar_alias(m, tf, a, b):
    try: return bajar(m, tf, a, b) + (m,)
    except Exception as e:
        err = str(e)
        for alt in ALIAS.get(m, []):
            try: return bajar(alt, tf, a, b) + (alt,)
            except Exception: pass
        raise ValueError(err)

def _armar_rey():
    buf = io.BytesIO(); log = []; ok = 0; peds = _rey_pedidos()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, m, t in peds:
            for tf, (antes, despues) in VENT_REY.items():
                try:
                    df, fuente, usado = _bajar_alias(m, tf, t - antes, t + despues)
                    z.writestr(f"{int(n):03d}_{m}_{tf}.csv", df.to_csv(index=False)); ok += 1
                    log.append(f"{n} {m} {tf}: {len(df)} velas ({fuente}{'' if usado == m else ' como ' + usado})")
                except Exception as e:
                    log.append(f"{n} {m} {tf}: FALLÓ → {str(e)[:160]}")
        z.writestr("fuentes.txt", "Horas en UTC; t = apertura de la vela (ms).\n" + "\n".join(log))
        z.writestr("senales_rey.csv", "n,moneda,lado,fecha_ART,hora_ART\n" + REY.strip() + "\n")
    fallas = sum(1 for x in log if "FALLÓ" in x)
    tg.enviar_archivo("velas_rey.zip", buf.getvalue(), f"Velas señales Rey: {ok} archivos de {len(peds)} señales" + (f" · {fallas} fallaron (ver fuentes.txt)" if fallas else ""))

def _correr_rey():
    if not _ocupado.acquire(blocking=False): tg.enviar("⏳ Ya estoy bajando velas; esperá a que llegue el archivo anterior."); return
    try: _armar_rey()
    except Exception as e: tg.enviar(f"❌ Error bajando velas Rey: {str(e)[:200]}")
    finally: _ocupado.release()
