#!/usr/bin/env python3
"""
Sistema de alertas de noticias — Colombia  (v2: por relevancia, no por consenso)
================================================================================
Fuente : RSS de medios colombianos (gratis)
Cerebro: Claude API (juzga qué titular es IMPORTANTE y evita repetir lo ya alertado)
Salida : Telegram (gratis)

Lógica v2: cada ronda lee los titulares recientes y le pide a Claude que seleccione los
HECHOS IMPORTANTES de relevancia nacional, apenas aparecen (aunque los dé un solo medio).
Para no repetir, el sistema recuerda lo que ya alertó (aunque otro medio lo redacte
distinto). Objetivo: rapidez y no perder señal, aceptando algo más de volumen.

Requisitos:  pip install feedparser requests anthropic
Variables:   ANTHROPIC_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
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

FEEDS = {
    # — Nativos digitales / solo web (rápidos para último minuto) —
    "Pulzo":            "https://news.google.com/rss/search?q=site:pulzo.com&hl=es-419&gl=CO&ceid=CO:es-419",
    "Minuto30":         "https://www.minuto30.com/feed/",
    "Kienyke":          "https://www.kienyke.com/feed/",
    # — Medios tradicionales —
    "El Tiempo":        "https://www.eltiempo.com/rss/colombia.xml",
    "El Espectador":    "https://www.elespectador.com/arcio/rss/",
    "Semana":           "https://www.semana.com/rss/",
    "La República":     "https://www.larepublica.co/rss",
    "Portafolio":       "https://www.portafolio.co/rss",
    "Blu Radio":        "https://www.bluradio.com/rss.xml",
    "La FM":            "https://www.lafm.com.co/rss.xml",
    "Caracol Radio":    "https://caracol.com.co/rss/",
    "RCN Radio":        "https://www.rcnradio.com/rss.xml",
    "Infobae Colombia": "https://www.infobae.com/colombia/feeds/rss/",
}

WINDOW_MINUTES  = 45      # titulares de los últimos 45 min (con dedup, no se pierde nada)
MEMORIA_HORAS   = 12      # cuánto tiempo recuerda lo ya alertado para no repetirlo
STATE_FILE      = "alertas_estado.json"
CLAUDE_MODEL    = "claude-haiku-4-5-20251001"   # barato; si la selección falla, subir a "claude-sonnet-4-6"

# ─────────────────────────── PASO 1: LEER RSS ───────────────────────────

def leer_titulares():
    limite = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
    items = []
    for medio, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] No se pudo leer {medio}: {e}")
            continue
        for e in feed.entries:
            fecha = None
            if getattr(e, "published_parsed", None):
                fecha = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            elif getattr(e, "updated_parsed", None):
                fecha = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
            if fecha and fecha < limite:
                continue
            items.append({
                "medio":  medio,
                "titulo": html.unescape(getattr(e, "title", "").strip()),
                "url":    getattr(e, "link", ""),
            })
    return items

# ─────────────────── PASO 2: CLAUDE SELECCIONA LO IMPORTANTE ───────────────────

def analizar_con_claude(items, ya_alertadas):
    """Pide a Claude los hechos IMPORTANTES nuevos (no repetidos)."""
    client = Anthropic()

    lista = "\n".join(f"{i}. [{it['medio']}] {it['titulo']}" for i, it in enumerate(items))

    prompt = f"""Eres el editor de última hora de una mesa de noticias en Colombia. Tu
trabajo es avisar RÁPIDO de las noticias IMPORTANTES del país apenas aparecen, aunque por
ahora las dé un solo medio.

Abajo hay titulares recientes (con índice y medio). También una lista de noticias que YA
alertamos (no las repitas).

Selecciona los HECHOS que merezcan una alerta:
- IMPORTANTES = relevancia nacional: política y gobierno, economía (dólar, precios, empleo),
  orden público y seguridad de impacto, decisiones de instituciones (CNE, cortes, Congreso,
  gobierno), desastres, hechos que afecten al país o a mucha gente, y primicias de peso.
- NO alertes: farándula, chismes, horóscopos, deportes rutinarios, sucesos locales menores
  sin relevancia nacional, notas de color, clickbait.
- Agrupa en UN solo evento los titulares que hablen del MISMO hecho (mismos protagonistas,
  mismo suceso), aunque sean de medios distintos. Hechos diferentes = eventos diferentes:
  NO los juntes por categoría (un homicidio en Barranquilla y otro en Medellín son DOS).
- EXCLUYE cualquier hecho que ya esté en "YA ALERTADAS", aunque esté redactado distinto:
  es la misma noticia.
- El "titular" debe contar el hecho concreto en UNA frase con datos reales (quién, qué,
  dónde). Nunca una categoría vaga como "violencia en Colombia".

YA ALERTADAS (no repetir):
{ya_alertadas or "(ninguna todavía)"}

Devuelve SOLO JSON válido, sin texto extra ni ```:
{{"eventos":[{{"titular":"...", "indices":[0,3], "importancia":"alta|media|baja"}}]}}
Si no hay nada nuevo que merezca alerta, devuelve {{"eventos":[]}}.

Titulares:
{lista}"""

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    texto = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(texto)["eventos"]
    except Exception as e:
        print(f"[WARN] No se pudo parsear la respuesta de Claude: {e}\n{texto[:400]}")
        return []

# ─────────────────── PASO 3: MEMORIA (evitar repetidos) ───────────────────

def cargar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            try:
                return json.load(f)
            except Exception:
                pass
    return {"eventos": []}

def guardar_estado(estado):
    corte = time.time() - MEMORIA_HORAS * 3600
    estado["eventos"] = [e for e in estado.get("eventos", []) if e.get("ts", 0) > corte]
    with open(STATE_FILE, "w") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)

def huella(texto):
    return hashlib.md5(texto.lower().strip().encode()).hexdigest()[:12]

# ─────────────────────────── PASO 4: TELEGRAM ───────────────────────────

def enviar_telegram(texto):
    token = os.environ["TELEGRAM_TOKEN"]
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
    if not items:
        return

    estado = cargar_estado()
    previos = [e["resumen"] for e in estado.get("eventos", [])]
    ya_alertadas = "\n".join(f"- {r}" for r in previos)
    huellas_previas = {huella(r) for r in previos}

    eventos = analizar_con_claude(items, ya_alertadas)
    nuevas = 0

    for ev in eventos:
        indices = [i for i in ev.get("indices", []) if 0 <= i < len(items)]
        if not indices:
            continue

        titular = ev.get("titular", "").strip()
        if not titular or huella(titular) in huellas_previas:   # respaldo anti-repetido
            continue

        # Una fuente por medio, con su titular real (para verificar de un vistazo)
        vistos, fuentes, medios = set(), [], set()
        for i in indices:
            medio = items[i]["medio"]
            medios.add(medio)
            if medio in vistos:
                continue
            vistos.add(medio)
            t = html.escape(items[i]["titulo"][:110])
            fuentes.append(f'• <b>{medio}</b>: <a href="{items[i]["url"]}">{t}</a>')
            if len(fuentes) >= 5:
                break

        emoji = {"alta": "🔴", "media": "🟠", "baja": "🟡"}.get(ev.get("importancia"), "🔵")
        cabecera = f"{emoji} <b>ALERTA</b>"
        if len(medios) > 1:
            cabecera += f" — {len(medios)} medios"
        msg = (
            f"{cabecera}\n\n"
            f"{html.escape(titular)}\n\n"
            f"<b>Fuentes:</b>\n" + "\n".join(fuentes)
        )
        enviar_telegram(msg)
        estado["eventos"].append({"resumen": titular, "ts": time.time()})
        huellas_previas.add(huella(titular))
        nuevas += 1

    guardar_estado(estado)
    print(f"  → {nuevas} alerta(s) enviada(s)")

if __name__ == "__main__":
    main()
