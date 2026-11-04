"""config.py — Bot Oscilación SHADOW (sin dinero, sin órdenes, sin claves de exchange)."""
import os
DB_PATH = os.environ.get("OSC_DB_PATH", "/data/osc_shadow.db" if os.path.isdir("/data") else "osc_shadow.db")
TELEGRAM_TOKEN = os.environ.get("OSC_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
# Si OSC_TELEGRAM_TOKEN está definido (bot propio de BotFather) se aceptan comandos.
# Si se reutiliza un token que ya escucha otro servicio, SOLO se envía (evita conflicto de getUpdates).
ACEPTA_COMANDOS = bool(os.environ.get("OSC_TELEGRAM_TOKEN"))

BINGX_URL = "https://open-api.bingx.com"
BINANCE_URL = "https://fapi.binance.com"

MONEDAS = ["AAVE","ACE","ADA","APT","ARB","AVAX","BAT","BCH","BNB","BTC","DASH","DOGE","ENA","ETH","FET","FIL",
           "HBAR","ICP","INJ","LINK","LTC","NEAR","ONDO","PENGU","PYTH","SAND","SEI","SOL","STRK","SUI","TAO",
           "TIA","TRX","UNI","VIRTUAL","WLD","W","XLM","XRP","ZEC"]

# 02/11 — MONEDAS CANDIDATAS (solo observación). Se simulan SOLO con la config JC (= J: la estrategia del OSC REAL) para ver
# si alguna merece sumarse a la lista. No se mezclan con las 40 de arriba (JC es una config aparte en los resúmenes).
# Las que no existan como perpetuo en BingX se detectan solas y se vuelven a probar una vez por día.
CANDIDATAS = ["OP","ATOM","DOT","ETC","GALA","IMX","GRT","STX","TON","JUP","PENDLE","ENS","NEO","LPT","WIF","DYDX","MINA","CYBER",
              "EIGEN","ORDI","JTO","RENDER","XMR","CRV","LDO","RUNE","ALGO","EGLD","ZRO","ZK","POL","COMP","SNX","TRUMP"]
CANDIDATAS = [m for m in CANDIDATAS if m not in MONEDAS]
REINTENTO_AUSENTE_H = 24   # una candidata sin datos se vuelve a probar cada tantas horas (para no frenar el ciclo horario)

# Mismas reglas que el simulador validado (osc.py). SL 3% desde el promedio recién tras la última entrada.
BASE = dict(sl_dist=3.0, tp_off=0.5, tp_min=0.25, tp_fixed=1.0, fee=0.0005)
# Protección por BTC (config I, J, K): sin largos nuevos si BTC cae >=5% desde su máx. de 7 días (se libera a -4%);
# STOP total si BTC supera los extremos de los 200 días analizados. Los ciclos ya abiertos siguen su curso.
BTC_PROT = dict(btc_dd=5.0, btc_dd_rel=4.0, btc_stop_rel=0.8, btc_stop_dd=20.0, btc_stop_up=27.0, btc_stop_r24=7.5, btc_stop_r24_up=12.0)
CONFIGS = {
    "A": dict(BASE, nombre="A actual ETH (ADX<22, RSI 32/68, DCA1.5, 5 ent.)", adx_th=22, rl=32, rs=68, dca=1.5, max_e=5),
    "B": dict(BASE, nombre="B (ADX<25, RSI 38/62, DCA2.0, 6 ent.)",            adx_th=25, rl=38, rs=62, dca=2.0, max_e=6),
    "C": dict(BASE, nombre="C (ADX<30, RSI 40/60, DCA1.5, 6 ent.)",            adx_th=30, rl=40, rs=60, dca=1.5, max_e=6),
    "D": dict(BASE, nombre="D = B + TP con trailing 1xATR",                     adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0),
    # E y F: mismas reglas de entrada que el Bot BingX (fix22) pero con apalancamiento POR CICLO = mín(lev_max, lev_k / ATR%), mínimo 3x.
    # Su PnL se informa con el apalancamiento real de cada ciclo (las columnas 3x y 5x muestran el mismo valor).
    "E": dict(BASE, nombre="E fix22 + apal. variable mín(20, 8/ATR%)",         adx_th=30, rl=38, rs=62, dca=2.0, max_e=6, lev_k=8,  lev_max=20),
    "F": dict(BASE, nombre="F fix22 + apal. variable mín(20, 10/ATR%)",        adx_th=30, rl=38, rs=62, dca=2.0, max_e=6, lev_k=10, lev_max=20),
    # G y H: F con salida modificada (solo cambia el SL y/o el TP mínimo; la entrada es idéntica a F).
    "G": dict(BASE, nombre="G = F con SL 1,5% (desde el promedio)",             adx_th=30, rl=38, rs=62, dca=2.0, max_e=6, lev_k=10, lev_max=20, sl_dist=1.5),
    "H": dict(BASE, nombre="H = F con SL 2,0% y TP mín. 0,5%",                  adx_th=30, rl=38, rs=62, dca=2.0, max_e=6, lev_k=10, lev_max=20, sl_dist=2.0, tp_min=0.5),
    # I: B (5x fijo) + protección por BTC. Sin largos nuevos si BTC cae >=5% desde su máx. de 7 días; STOP total (ni largos ni cortos nuevos)
    # si BTC supera los extremos del período de análisis (200 días): caída 7d >=20%, suba 7d >=27%, 24h <=-7,5% o >=+12%.
    # Los ciclos ya abiertos siguen su curso (TP/SL normales). Se reanuda solo cuando BTC vuelve a las condiciones normales.
    "I": dict(BASE, nombre="I = B + protección BTC (sin largos si BTC -5% de máx. 7d; STOP en extremos)", adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, **BTC_PROT),
    # J y K: mismas entradas/salidas que D y G pero con la protección BTC de I (histéresis: sin largos desde -5%, se libera a -4%).
    "J": dict(BASE, nombre="J = D (B + trailing 1xATR) + protección BTC",                adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0, **BTC_PROT),
    "K": dict(BASE, nombre="K = G (F con SL 1,5%) + protección BTC",                     adx_th=30, rl=38, rs=62, dca=2.0, max_e=6, lev_k=10, lev_max=20, sl_dist=1.5, **BTC_PROT),
    # JF: igual que J + FRENO: con 2 SL (de cualquier moneda) en 24 h, no abre ciclos nuevos durante 24 h. En observación (decisión 2/11/2026).
    "JF": dict(BASE, nombre="JF = J + freno (2 SL en 24h -> pausa 24h)",                  adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0, freno_n=2, freno_h=24, freno_key="JF", **BTC_PROT),
    # JT: igual que J pero con piso de TP 0,40% (en J es 0,25%). En observación (decisión 2/11/2026).
    "JT": dict(dict(BASE, tp_min=0.40), nombre="JT = J con piso de TP 0,40%",                   adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0, **BTC_PROT),
    # JX: igual que J pero SIN las 6 monedas con más pérdidas por SL en la simulación de 197 días (WLD, ACE, ARB, NEAR, FET, TIA).
    "JX": dict(BASE, nombre="JX = J sin las 6 monedas con más SL",                              adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0, excluir=("WLD","ACE","ARB","NEAR","FET","TIA"), **BTC_PROT),
    # JS: igual que J pero con SL 1,5% desde el promedio (en J es 3%). En observación (decisión 3/11/2026).
    "JS": dict(dict(BASE, sl_dist=1.5), nombre="JS = J con SL 1,5%",                           adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0, **BTC_PROT),
    # JZ: las tres juntas: freno (2 SL/24h) + piso de TP 0,40% + sin las 6 monedas.
    "JZ": dict(dict(BASE, tp_min=0.40), nombre="JZ = J + freno + TP 0,40% + sin 6 monedas",     adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0, freno_n=2, freno_h=24, freno_key="JZ", excluir=("WLD","ACE","ARB","NEAR","FET","TIA"), **BTC_PROT),
    # JC: la misma estrategia que J (la del OSC REAL), pero SOLO para las monedas candidatas (observación).
    "JC": dict(BASE, nombre="JC = J en monedas CANDIDATAS (solo observación)",           adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0, **BTC_PROT),
}
L_FLUJO = 5            # apalancamiento con el que se simula el flujo (liquidación a -95% de margen)
L_REPORTE = (3, 5)     # apalancamientos para los que se informa el PnL
F_MARGEN = 0.01        # 1% del capital como margen por entrada (para el reporte de cartera)
K_SIMULTANEOS = 8      # tope de ciclos simultáneos en el reporte de cartera
VELAS_1H = 700         # ~29 días: calentamiento ADX/RSI + 100 velas de 4h para VPVR
HORA_RESUMEN_ARG = (23, 55)  # resumen diario por Telegram (hora y minuto, hora Argentina)

PAUSA_ENTRE_MONEDAS_S = 0.8          # BingX limita consultas: más espacio entre monedas
REINTENTOS_ESPERA_S = (2, 5, 10)     # esperas de los reintentos por moneda (BingX código 100410)
PAUSA_SEGUNDA_RONDA_S = 20           # espera antes de reintentar las monedas que fallaron en la pasada
ALERTA_FALLOS_SEGUIDOS = 2           # aviso por Telegram solo si una moneda falla en este nº de pasadas seguidas

def configs_de(m):
    """Configs que corren para una moneda: las candidatas solo JC; las 40 de siempre, todas menos JC."""
    return {k: v for k, v in CONFIGS.items() if (k == "JC") == (m in CANDIDATAS) and m not in v.get("excluir", ())}
