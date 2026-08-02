#!/usr/bin/env python3
"""
Sistema de alertas de noticias — Colombia
==========================================
Fuente : RSS de medios colombianos (gratis)
Cerebro: Claude API (agrupa por tema, evalúa importancia)  -> única parte que cuesta (centavos/día)
Salida : Telegram (gratis)

Lógica: cada ciclo lee los titulares recientes de todos los medios, se los pasa a
Claude para que los agrupe por TEMA (semánticamente, no por palabra exacta), y si un
mismo tema aparece en >= UMBRAL medios distintos dentro de la ventana de tiempo, se
dispara la alerta y se envía a Telegram. Guarda estado para no repetir alertas.

Cómo correrlo cada 15 min (cron en Linux/Mac):
    */15 * * * * /usr/bin/python3 /ruta/alertas_colombia.py >> /ruta/alertas.log 2>&1

Requisitos:
    pip install feedparser requests anthropic
Variables de entorno necesarias:
    ANTHROPIC_API_KEY   -> tu llave de la API de Claude
    TELEGRAM_TOKEN      -> token del bot (te lo da @BotFather en Telegram)
    TELEGRAM_CHAT_ID    -> id del chat/canal donde recibes las alertas
"""

import os
import json
import time
import html
import hashlib
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from anthropic import Anthropic

# ─────────────────────────── CONFIGURACIÓN ───────────────────────────

# Medios colombianos (verifica cada URL abriéndola en el navegador; algunos medios
# tienen feeds por sección — economía, política, etc. — que puedes añadir).
#
# TRUCO para medios sin RSS nativo: Google News genera un feed de CUALQUIER sitio con
#   https://news.google.com/rss/search?q=site:DOMINIO&hl=es-419&gl=CO&ceid=CO:es-419
# Es gratis, siempre funciona y ya viene filtrado a Colombia. Lo usamos para Pulzo.
FEEDS = {
    # — Nativos digitales / solo web (rápidos para último minuto) —
    "Pulzo":            "https://news.google.com/rss/search?q=site:pulzo.com&hl=es-419&gl=CO&ceid=CO:es-419",
    "Minuto30":         "https://www.minuto30.com/feed/",          # Medellín, muy veloz
    "Kienyke":          "https://www.kienyke.com/feed/",           # nativo digital ágil
    # — Medios tradicionales —
    "El Tiempo":        "https://www.eltiempo.com/rss/colombia.xml",
    "El Espectador":    "https://www.elespectador.com/arcio/rss/",
    "Semana":           "https://www.semana.com/rss/",
    "La República":     "https://www.larepublica.co/rss",          # buena para dólar/economía
    "Portafolio":       "https://www.portafolio.co/rss",           # economía
    "Blu Radio":        "https://www.bluradio.com/rss.xml",
    "La FM":            "https://www.lafm.com.co/rss.xml",
    "Caracol Radio":    "https://caracol.com.co/rss/",
    "RCN Radio":        "https://www.rcnradio.com/rss.xml",
    "Infobae Colombia": "https://www.infobae.com/colombia/feeds/rss/",
}

WINDOW_MINUTES = 60      # solo se consideran titulares de la última hora
THRESHOLD      = 4       # cuántos MEDIOS distintos deben tocar un tema para alertar
                         # (baja a 3 para más sensibilidad; sube a 5 para menos ruido)
STATE_FILE     = "alertas_estado.json"   # para no repetir alertas ya enviadas
CLAUDE_MODEL   = "claude-haiku-4-5-20251001"   # el modelo barato; agrupa de sobra

# ─────────────────────────── PASO 1: LEER RSS ───────────────────────────

def leer_titulares():
    """Devuelve lista de dicts {medio, titulo, resumen, url, fecha} de la ventana."""
    limite = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
    items = []
    for medio, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] No se pudo leer {medio}: {e}")
            continue
        for e in feed.entries:
            # fecha del item (si el feed no la trae, lo incluimos igual)
            fecha = None
            if getattr(e, "published_parsed", None):
                fecha = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            elif getattr(e, "updated_parsed", None):
                fecha = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
            if fecha and fecha < limite:
                continue
            items.append({
                "medio":   medio,
                "titulo":  html.unescape(getattr(e, "title", "").strip()),
                "resumen": html.unescape(getattr(e, "summary", "")[:300].strip()),
                "url":     getattr(e, "link", ""),
                "fecha":   fecha.isoformat() if fecha else "s/f",
            })
    return items

# ─────────────────────── PASO 2: CLAUDE AGRUPA Y EVALÚA ───────────────────────

def agrupar_con_claude(items):
    """Le pasa los titulares a Claude para que los agrupe por tema y evalúe importancia."""
    client = Anthropic()  # lee ANTHROPIC_API_KEY del entorno

    # Numeramos los titulares para que Claude los referencie sin reescribirlos
    lista = "\n".join(
        f"{i}. [{it['medio']}] {it['titulo']}"
        for i, it in enumerate(items)
    )

    prompt = f"""Eres un editor de mesa de noticias en Colombia. Abajo hay titulares
recientes de varios medios, cada uno con su índice y medio entre corchetes.

Agrúpalos por TEMA/EVENTO (por significado, no por palabras iguales: "el dólar baja de
3.000", "el peso se fortalece" y "COP rompe soporte" son el MISMO tema). Para cada tema
cuenta cuántos MEDIOS DISTINTOS lo cubren.

Devuelve SOLO un JSON válido, sin texto extra ni ```:
{{
  "temas": [
    {{
      "titular": "resumen del tema en una frase clara y neutral",
      "medios": ["El Tiempo", "Semana"],
      "indices": [0, 3, 7],
      "importancia": "alta|media|baja"
    }}
  ]
}}

Titulares:
{lista}"""

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    texto = resp.content[0].text.strip()
    texto = texto.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(texto)["temas"]
    except Exception as e:
        print(f"[WARN] No se pudo parsear la respuesta de Claude: {e}\n{texto[:400]}")
        return []

# ─────────────────────── PASO 3: FILTRAR Y EVITAR REPETIDOS ───────────────────────

def cargar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"enviadas": {}}

def guardar_estado(estado):
    # limpia huellas de más de 24 h para que el archivo no crezca
    corte = time.time() - 86400
    estado["enviadas"] = {k: v for k, v in estado["enviadas"].items() if v > corte}
    with open(STATE_FILE, "w") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)

def huella(titular):
    """ID estable de un tema para detectar duplicados entre ciclos."""
    return hashlib.md5(titular.lower().encode()).hexdigest()[:12]

# ─────────────────────────── PASO 4: TELEGRAM ───────────────────────────

def enviar_telegram(texto):
    token = os.environ["TELEGRAM_TOKEN"]
    # Acepta uno o varios destinos separados por comas.
    # Ejemplos válidos para TELEGRAM_CHAT_ID:
    #   "123456789"                        -> un chat individual
    #   "-1001234567890"                   -> un grupo o canal (los IDs de grupo/canal son negativos)
    #   "@mi_canal_publico"                -> un canal público, por su usuario
    #   "123456789,-1001234567890,987654"  -> varios destinos a la vez
    destinos = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
    for chat in destinos:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": texto, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=15,
        )
        if not r.ok:
            print(f"[ERROR] Telegram ({chat}): {r.status_code} {r.text}")

# ─────────────────────────── ORQUESTACIÓN ───────────────────────────

def main():
    items = leer_titulares()
    print(f"[{datetime.now():%H:%M}] {len(items)} titulares en la ventana de {WINDOW_MINUTES} min")
    if len(items) < THRESHOLD:
        return

    temas   = agrupar_con_claude(items)
    estado  = cargar_estado()
    nuevas  = 0

    for tema in temas:
        medios_distintos = set(tema.get("medios", []))
        if len(medios_distintos) < THRESHOLD:
            continue

        h = huella(tema["titular"])
        if h in estado["enviadas"]:      # ya la mandamos antes
            continue

        # Arma el mensaje con enlaces a las fuentes
        fuentes = []
        for idx in tema.get("indices", [])[:5]:
            if 0 <= idx < len(items):
                it = items[idx]
                fuentes.append(f'• <a href="{it["url"]}">{it["medio"]}</a>')

        emoji = {"alta": "🔴", "media": "🟠", "baja": "🟡"}.get(tema.get("importancia"), "🔵")
        msg = (
            f"{emoji} <b>ALERTA — {len(medios_distintos)} medios</b>\n\n"
            f"{html.escape(tema['titular'])}\n\n"
            f"<b>Fuentes:</b>\n" + "\n".join(fuentes)
        )
        enviar_telegram(msg)
        estado["enviadas"][h] = time.time()
        nuevas += 1

    guardar_estado(estado)
    print(f"  → {nuevas} alerta(s) enviada(s)")

if __name__ == "__main__":
    main()
