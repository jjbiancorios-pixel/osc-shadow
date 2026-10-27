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

# Mismas reglas que el simulador validado (osc.py). SL 3% desde el promedio recién tras la última entrada.
BASE = dict(sl_dist=3.0, tp_off=0.5, tp_min=0.25, tp_fixed=1.0, fee=0.0005)
CONFIGS = {
    "A": dict(BASE, nombre="A actual ETH (ADX<22, RSI 32/68, DCA1.5, 5 ent.)", adx_th=22, rl=32, rs=68, dca=1.5, max_e=5),
    "B": dict(BASE, nombre="B (ADX<25, RSI 38/62, DCA2.0, 6 ent.)",            adx_th=25, rl=38, rs=62, dca=2.0, max_e=6),
    "C": dict(BASE, nombre="C (ADX<30, RSI 40/60, DCA1.5, 6 ent.)",            adx_th=30, rl=40, rs=60, dca=1.5, max_e=6),
    "D": dict(BASE, nombre="D = B + TP con trailing 1xATR",                     adx_th=25, rl=38, rs=62, dca=2.0, max_e=6, trail_x=1.0),
    # E y F: mismas reglas de entrada que el Bot BingX (fix22) pero con apalancamiento POR CICLO = mín(lev_max, lev_k / ATR%), mínimo 3x.
    # Su PnL se informa con el apalancamiento real de cada ciclo (las columnas 3x y 5x muestran el mismo valor).
    "E": dict(BASE, nombre="E fix22 + apal. variable mín(20, 8/ATR%)",         adx_th=30, rl=38, rs=62, dca=2.0, max_e=6, lev_k=8,  lev_max=20),
    "F": dict(BASE, nombre="F fix22 + apal. variable mín(20, 10/ATR%)",        adx_th=30, rl=38, rs=62, dca=2.0, max_e=6, lev_k=10, lev_max=20),
}
L_FLUJO = 5            # apalancamiento con el que se simula el flujo (liquidación a -95% de margen)
L_REPORTE = (3, 5)     # apalancamientos para los que se informa el PnL
F_MARGEN = 0.01        # 1% del capital como margen por entrada (para el reporte de cartera)
K_SIMULTANEOS = 8      # tope de ciclos simultáneos en el reporte de cartera
VELAS_1H = 700         # ~29 días: calentamiento ADX/RSI + 100 velas de 4h para VPVR
HORA_RESUMEN_ARG = 21  # resumen diario por Telegram (hora Argentina)

PAUSA_ENTRE_MONEDAS_S = 0.8          # BingX limita consultas: más espacio entre monedas
REINTENTOS_ESPERA_S = (2, 5, 10)     # esperas de los reintentos por moneda (BingX código 100410)
PAUSA_SEGUNDA_RONDA_S = 20           # espera antes de reintentar las monedas que fallaron en la pasada
ALERTA_FALLOS_SEGUIDOS = 2           # aviso por Telegram solo si una moneda falla en este nº de pasadas seguidas
