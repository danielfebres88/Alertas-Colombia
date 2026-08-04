#!/usr/bin/env python3
"""
Sistema de alertas de noticias — Colombia  (v3)
================================================
Novedades v3:
- Feeds arreglados: los medios sin RSS nativo confiable entran vía Google News (site:dominio).
- Diagnóstico de salud por feed en el log (cuántos títulos trae cada uno / cuáles vienen vacíos).
- Anti-duplicados reforzado con una "clave de tema" estable (p. ej. el dólar deja de repetirse).
- Alerta especial cuando se menciona a CAF (color/encabezado distinto).
"""

import os
import re
import json
import time
import html
import hashlib
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from anthropic import Anthropic

# ─────────────────────────── CONFIGURACIÓN ───────────────────────────

def gnews(dominio):
    """Genera un RSS de cualquier medio vía Google News, ya filtrado a Colombia."""
    return f"https://news.google.com/rss/search?q=site:{dominio}&hl=es-419&gl=CO&ceid=CO:es-419"

FEEDS = {
    # — Nativos confirmados (rápidos, en tiempo real) —
    "El Tiempo":        "https://www.eltiempo.com/rss/colombia.xml",
    "La República":     "https://www.larepublica.co/rss",
    "Minuto30":         "https://www.minuto30.com/feed/",
    "Kienyke":          "https://www.kienyke.com/feed/",
    # — Vía Google News (su RSS nativo estaba caído; así SÍ entran) —
    "El Espectador":    gnews("elespectador.com"),
    "Semana":           gnews("semana.com"),
    "Caracol Radio":    gnews("caracol.com.co"),
    "RCN Radio":        gnews("rcnradio.com"),
    "Blu Radio":        gnews("bluradio.com"),
    "Portafolio":       gnews("portafolio.co"),
    "La FM":            gnews("lafm.com.co"),
    "Infobae Colombia": gnews("infobae.com/colombia"),
    "Pulzo":            gnews("pulzo.com"),
    # — Nuevos medios —
    "La Silla Vacía":   gnews("lasillavacia.com"),
    "Noticias Caracol": gnews("noticiascaracol.com"),
    "El Colombiano":    gnews("elcolombiano.com"),
}

WINDOW_MINUTES  = 45      # titulares de los últimos 45 min
MEMORIA_HORAS   = 12      # cuánto recuerda lo ya alertado
STATE_FILE      = "alertas_estado.json"
CLAUDE_MODEL    = "claude-haiku-4-5-20251001"   # barato; si el criterio falla, subir a "claude-sonnet-4-6"

# Detección de menciones a CAF (Banco de Desarrollo de América Latina y el Caribe)
CAF_PATRONES = [
    re.compile(r'\bCAF\b'),                                   # sigla exacta en mayúsculas (no "café")
    re.compile(r'corporaci[oó]n andina de fomento', re.I),
    re.compile(r'banco de desarrollo de am[eé]rica latina', re.I),
]

def menciona_caf(textos):
    return any(p.search(t) for t in textos for p in CAF_PATRONES)

# ─────────────────────────── PASO 1: LEER RSS ───────────────────────────

def leer_titulares():
    limite = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
    items, conteo = [], {}
    for medio, url in FEEDS.items():
        antes = len(items)
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] No se pudo leer {medio}: {e}")
            conteo[medio] = 0
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
        conteo[medio] = len(items) - antes

    # Diagnóstico de salud de los feeds (para ver de un vistazo cuáles fallan)
    con_items = [f"{m}({n})" for m, n in conteo.items() if n > 0]
    vacios    = [m for m, n in conteo.items() if n == 0]
    print("  Feeds con títulos:", ", ".join(con_items) if con_items else "NINGUNO")
    if vacios:
        print("  Feeds vacíos:", ", ".join(vacios))
    return items

# ─────────────────── PASO 2: CLAUDE SELECCIONA LO IMPORTANTE ───────────────────

def analizar_con_claude(items, ya_alertadas):
    client = Anthropic()
    lista = "\n".join(f"{i}. [{it['medio']}] {it['titulo']}" for i, it in enumerate(items))

    prompt = f"""Eres el editor de última hora de una mesa de noticias en Colombia. Avisas
RÁPIDO de las noticias IMPORTANTES del país apenas aparecen, aunque las dé un solo medio.

Abajo hay titulares recientes (con índice y medio) y una lista de noticias YA alertadas.

Selecciona los HECHOS que merezcan alerta:
- IMPORTANTES = relevancia nacional: política y gobierno, economía (dólar, precios, empleo,
  empresas grandes), orden público y seguridad de impacto, decisiones de instituciones
  (CNE, cortes, Congreso, gobierno), desastres, hechos que afecten al país o a mucha gente.
- Prioriza lo que afecta a Colombia. Incluye noticias internacionales SOLO si tienen impacto
  directo y claro en Colombia (no farándula internacional, no deportes de otras ligas).
- NO alertes: farándula, chismes, horóscopos, deportes rutinarios, sucesos locales menores
  sin relevancia nacional, notas de color, clickbait.
- Agrupa en UN evento los titulares del MISMO hecho (aunque sean de medios distintos).
  Hechos diferentes = eventos diferentes: nunca juntes por categoría.
- EXCLUYE lo que ya esté en "YA ALERTADAS", aunque esté redactado distinto: es la misma noticia.

Para cada evento entrega:
- "titular": el hecho concreto en UNA frase con datos reales (quién, qué, dónde). Nunca vago.
- "clave": un identificador CORTO y ESTABLE del tema (minúsculas, con guion bajo), que sea
  IGUAL cada vez que aparezca el mismo hecho aunque cambie la redacción. Ej: la cotización de
  apertura del dólar de hoy siempre es "dolar_apertura"; utilidades de Ecopetrol del 2T,
  "ecopetrol_utilidades_2t". Si el hecho ya está en YA ALERTADAS, REUTILIZA su misma clave.
- "importancia": "alta" | "media" | "baja".

YA ALERTADAS (no repetir; reutiliza su clave si es el mismo hecho):
{ya_alertadas or "(ninguna todavía)"}

Devuelve SOLO JSON válido, sin texto extra ni ```:
{{"eventos":[{{"titular":"...","clave":"...","indices":[0,3],"importancia":"alta"}}]}}
Si no hay nada nuevo que merezca alerta, devuelve {{"eventos":[]}}.

Titulares:
{lista}"""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=2000,
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

def norm(s):
    return (s or "").lower().strip()

def huella(texto):
    return hashlib.md5(norm(texto).encode()).hexdigest()[:12]

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
    previos = estado.get("eventos", [])
    claves_previas   = {norm(e.get("clave")) for e in previos if e.get("clave")}
    huellas_previas  = {huella(e.get("resumen")) for e in previos if e.get("resumen")}
    ya_alertadas = "\n".join(f"- [{e.get('clave','')}] {e.get('resumen','')}" for e in previos)

    eventos = analizar_con_claude(items, ya_alertadas)
    nuevas = 0

    for ev in eventos:
        indices = [i for i in ev.get("indices", []) if 0 <= i < len(items)]
        if not indices:
            continue
        titular = ev.get("titular", "").strip()
        clave   = ev.get("clave", "").strip()
        if not titular:
            continue

        # Anti-duplicados: por clave de tema Y por titular (doble red de seguridad)
        if (clave and norm(clave) in claves_previas) or huella(titular) in huellas_previas:
            continue

        # Fuentes: una por medio, con su titular real
        vistos, fuentes, medios, textos = set(), [], set(), [titular]
        for i in indices:
            m = items[i]["medio"]
            medios.add(m)
            textos.append(items[i]["titulo"])
            if m in vistos:
                continue
            vistos.add(m)
            t = html.escape(items[i]["titulo"][:110])
            fuentes.append(f'• <b>{m}</b>: <a href="{items[i]["url"]}">{t}</a>')
            if len(fuentes) >= 5:
                break

        # ¿Menciona a CAF? -> encabezado especial
        if menciona_caf(textos):
            cabecera = "🟣🟣 <b>ALERTA · MENCIÓN A CAF</b> 🟣🟣"
        else:
            emoji = {"alta": "🔴", "media": "🟠", "baja": "🟡"}.get(ev.get("importancia"), "🔵")
            cabecera = f"{emoji} <b>ALERTA</b>"
            if len(medios) > 1:
                cabecera += f" — {len(medios)} medios"

        msg = (f"{cabecera}\n\n{html.escape(titular)}\n\n"
               f"<b>Fuentes:</b>\n" + "\n".join(fuentes))
        enviar_telegram(msg)

        estado["eventos"].append({"clave": clave, "resumen": titular, "ts": time.time()})
        claves_previas.add(norm(clave))
        huellas_previas.add(huella(titular))
        nuevas += 1

    guardar_estado(estado)
    print(f"  → {nuevas} alerta(s) enviada(s)")

if __name__ == "__main__":
    main()
