import requests, config
_off = 0
def _api(method, **p):
    try: return requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/{method}", json=p, timeout=20).json()
    except Exception as e: print("tg error", method, e, flush=True); return {}
def enviar(msg):
    if not config.TELEGRAM_TOKEN or not config.CHAT_ID: print("[tg off]", msg[:200], flush=True); return
    for i in range(0, len(msg), 3800):
        r = _api("sendMessage", chat_id=config.CHAT_ID, text=msg[i:i+3800], parse_mode="HTML")
        if not r.get("ok"): print("tg rechazó:", str(r)[:200], flush=True)
def enviar_archivo(nombre, contenido, caption=""):
    if not config.TELEGRAM_TOKEN or not config.CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendDocument", data={"chat_id": config.CHAT_ID, "caption": caption},
                      files={"document": (nombre, contenido)}, timeout=60)
    except Exception as e: print("tg doc error", e, flush=True)
def comandos():
    """Devuelve lista de textos '/cmd args' nuevos (solo si hay token propio)."""
    global _off
    if not config.ACEPTA_COMANDOS: return []
    d = _api("getUpdates", offset=_off + 1, timeout=1); out = []
    for u in d.get("result", []):
        _off = max(_off, u["update_id"]); m = u.get("message", {})
        if str(m.get("chat", {}).get("id", "")) == str(config.CHAT_ID) and m.get("text", "").startswith("/"): out.append(m["text"])
    return out
