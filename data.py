"""data.py — velas 1h públicas (sin firma). BingX primero, Binance como respaldo. Devuelve solo velas CERRADAS."""
import time, requests, pandas as pd
from datetime import datetime, timezone
import config

def _a_df(rows):
    df = pd.DataFrame(rows, columns=['t', 'o', 'h', 'l', 'c', 'v']).astype({'t': 'int64', 'o': float, 'h': float, 'l': float, 'c': float, 'v': float})
    df = df.drop_duplicates('t').sort_values('t')
    df.index = pd.to_datetime(df.t, unit='ms')          # UTC naive = hora de APERTURA de la vela
    return df[['o', 'h', 'l', 'c', 'v']]

def _bingx(moneda, limit):
    r = requests.get(f"{config.BINGX_URL}/openApi/swap/v3/quote/klines",
                     params={"symbol": f"{moneda}-USDT", "interval": "1h", "limit": limit}, timeout=15).json()
    data = r.get("data") or []
    if not data: raise ValueError(f"BingX sin datos {moneda}: {str(r)[:120]}")
    rows = []
    for k in data:
        if isinstance(k, dict): rows.append((k['time'], k['open'], k['high'], k['low'], k['close'], k['volume']))
        else: rows.append((k[0], k[1], k[2], k[3], k[4], k[5]))
    return _a_df(rows)

def _binance(moneda, limit):
    r = requests.get(f"{config.BINANCE_URL}/fapi/v1/klines", params={"symbol": f"{moneda}USDT", "interval": "1h", "limit": min(limit, 1500)}, timeout=15).json()
    if not isinstance(r, list) or not r: raise ValueError(f"Binance sin datos {moneda}: {str(r)[:120]}")
    return _a_df([(k[0], k[1], k[2], k[3], k[4], k[5]) for k in r])

def velas_cerradas(moneda, limit=config.VELAS_1H):
    """Devuelve (df, fuente). Quita la vela en formación."""
    err = []
    for fuente, f in (("bingx", _bingx), ("binance", _binance)):
        try:
            df = f(moneda, limit)
            ahora = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
            df = df[df.index + pd.Timedelta(hours=1) <= ahora + pd.Timedelta(seconds=2)]
            if len(df) < 300: raise ValueError(f"pocas velas ({len(df)})")
            # huecos: la serie debe ser continua en las últimas 100 velas
            if (df.index[-100:].to_series().diff().dropna() != pd.Timedelta(hours=1)).any(): raise ValueError("huecos en la serie")
            return df, fuente
        except Exception as e:
            err.append(f"{fuente}: {str(e)[:100]}")
    raise RuntimeError(" | ".join(err))
