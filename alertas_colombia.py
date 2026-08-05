#!/usr/bin/env python3
"""
Sistema de alertas de noticias — Colombia  (v4)
================================================
Novedades v4:
- CAF ahora es DISPARADOR GARANTIZADO: cualquier noticia que mencione a CAF genera alerta
  SIEMPRE, saltándose el filtro de importancia (antes se perdía si el filtro la descartaba).
- Ventana ampliada a 90 min para no perder notas que Google News indexa con retraso.
- Detección de CAF también en el resumen del artículo, no solo en el titular.
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
    return f"https://news.google.com/rss/search?q=site:{dominio}&hl=es-419&gl=CO&ceid=CO:es-419"

FEEDS = {
    # — Nativos confirmados (rápidos, en tiempo real) —
    "El Tiempo":        "https://www.eltiempo.com/rss/colombia.xml",
    "La República":     "https://www.larepublica.co/rss",
    "Minuto30":         "https://www.minuto30.com/feed/",
    "Kienyke":          "https://www.kienyke.com/feed/",
    # — Vía Google News —
    "El Espectador":    gnews("elespectador.com"),
    "Semana":           gnews("semana.com"),
    "Caracol Radio":    gnews("caracol.com.co"),
    "RCN Radio":        gnews("rcnradio.com"),
    "Blu Radio":        gnews("bluradio.com"),
    "Portafolio":       gnews("portafolio.co"),
    "La FM":            gnews("lafm.com.co"),
    "Infobae Colombia": gnews("infobae.com/colombia"),
    "Pulzo":            gnews("pulzo.com"),
    "La Silla Vacía":   gnews("lasillavacia.com"),
    "Noticias Caracol": gnews("noticiascaracol.com"),
    "El Colombiano":    gnews("elcolombiano.com"),
    "Valora Analitik":  gnews("valoraanalitik.com"),   # fuerte en CAF y banca multilateral
}

WINDOW_MINUTES  = 90      # ampliada para no perder notas que Google News indexa con retraso
MAX_POR_MEDIO   = 8       # tope de títulos por medio por ronda (evita que uno solo inunde)
MEMORIA_HORAS   = 12
STATE_FILE      = "alertas_estado.json"
CLAUDE_MODEL    = "claude-haiku-4-5-20251001"

# Detección de menciones a CAF (Banco de Desarrollo de América Latina y el Caribe)
CAF_PATRONES = [
    re.compile(r'\bCAF\b'),
    re.compile(r'corporaci[oó]n andina de fomento', re.I),
    re.compile(r'banco de desarrollo de am[eé]rica latina', re.I),
]

def menciona_caf(textos):
    return any(p.search(t) for t in textos if t for p in CAF_PATRONES)

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
                "medio":   medio,
                "titulo":  html.unescape(getattr(e, "title", "").strip()),
                "resumen": html.unescape(getattr(e, "summary", "")[:300].strip()),
                "url":     getattr(e, "link", ""),
            })
            if len(items) - antes >= MAX_POR_MEDIO:   # tope por medio: que ninguno inunde
                break
        conteo[medio] = len(items) - antes

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
  empresas grandes), decisiones de instituciones (CNE, cortes, Congreso, gobierno), desastres
  de impacto, y hechos que afecten al país o a mucha gente.
- VIOLENCIA Y CRIMEN — sé MUY ESTRICTO: alerta SOLO si el hecho tiene alcance o connotación
  NACIONAL: masacres (varias víctimas), atentados, ataques a la fuerza pública, captura de
  cabecillas o narcos de peso, hechos que generen conmoción nacional o respuesta del gobierno,
  o cifras/políticas de seguridad de impacto nacional.
- NO alertes CRÓNICA ROJA ni sucesos locales, aunque sean violentos o tristes: homicidios
  individuales, riñas, sicariato puntual, capturas rutinarias, extorsión o "gota a gota" de un
  caso, hurtos, accidentes locales. Un caso aislado sin relevancia nacional NO es alerta.
- Un feminicidio individual NO se alerta, SALVO que tenga connotación nacional (conmoción,
  marchas, caso emblemático que domina la agenda del país).
- Prioriza lo que afecta a Colombia. Incluye internacionales SOLO si tienen impacto directo y
  claro en Colombia (no farándula internacional, no deportes de otras ligas).
- NO alertes: farándula, chismes, horóscopos, deportes rutinarios, notas de color, clickbait.

Para cada evento entrega:
- "titular": el hecho concreto en UNA frase con datos reales (quién, qué, dónde). Nunca vago.
- "clave": identificador CORTO y ESTABLE del tema (minúsculas, con guion bajo), IGUAL cada vez
  que aparezca el mismo hecho aunque cambie la redacción. Si ya está en YA ALERTADAS, reutiliza
  su misma clave.
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
    # Claude a veces agrega explicaciones tras el JSON; extraemos solo el objeto JSON.
    try:
        return json.loads(texto)["eventos"]
    except Exception:
        m = re.search(r'\{.*"eventos".*?\]\s*\}', texto, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))["eventos"]
            except Exception:
                pass
        print(f"[WARN] No se pudo parsear la respuesta de Claude:\n{texto[:300]}")
        return []

# ─────────────────── PASO 3: MEMORIA ───────────────────

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
    claves_previas  = {norm(e.get("clave")) for e in previos if e.get("clave")}
    huellas_previas = {huella(e.get("resumen")) for e in previos if e.get("resumen")}
    urls_previas    = {huella(e.get("url")) for e in previos if e.get("url")}
    nuevas = 0

    # ─── PASO CAF: cualquier mención a CAF alerta SIEMPRE (se salta el filtro) ───
    for it in items:
        if not menciona_caf([it["titulo"], it.get("resumen", "")]):
            continue
        hu_url = huella(it["url"] or it["titulo"])
        hu_tit = huella(it["titulo"])
        if hu_url in urls_previas or hu_tit in huellas_previas:
            continue
        t = html.escape(it["titulo"][:110])
        msg = ("🟣🟣 <b>ALERTA · MENCIÓN A CAF</b> 🟣🟣\n\n"
               f"{html.escape(it['titulo'])}\n\n"
               f"<b>Fuente:</b>\n• <b>{it['medio']}</b>: <a href=\"{it['url']}\">{t}</a>")
        enviar_telegram(msg)
        estado["eventos"].append({"clave": "caf_" + hu_url, "resumen": it["titulo"],
                                  "url": it["url"], "ts": time.time()})
        urls_previas.add(hu_url); huellas_previas.add(hu_tit)
        nuevas += 1

    # ─── FLUJO NORMAL: noticias importantes por relevancia ───
    ya_alertadas = "\n".join(f"- [{e.get('clave','')}] {e.get('resumen','')}" for e in previos)
    eventos = analizar_con_claude(items, ya_alertadas)

    for ev in eventos:
        indices = [i for i in ev.get("indices", []) if 0 <= i < len(items)]
        if not indices:
            continue
        # Si el evento incluye una nota de CAF, ya se alertó arriba: saltar
        if any(menciona_caf([items[i]["titulo"], items[i].get("resumen", "")]) for i in indices):
            continue

        titular = ev.get("titular", "").strip()
        clave   = ev.get("clave", "").strip()
        if not titular:
            continue
        if (clave and norm(clave) in claves_previas) or huella(titular) in huellas_previas:
            continue

        vistos, fuentes, medios = set(), [], set()
        for i in indices:
            m = items[i]["medio"]
            medios.add(m)
            if m in vistos:
                continue
            vistos.add(m)
            t = html.escape(items[i]["titulo"][:110])
            fuentes.append(f'• <b>{m}</b>: <a href="{items[i]["url"]}">{t}</a>')
            if len(fuentes) >= 5:
                break

        emoji = {"alta": "🔴", "media": "🟠", "baja": "🟡"}.get(ev.get("importancia"), "🔵")
        cabecera = f"{emoji} <b>ALERTA</b>"
        if len(medios) > 1:
            cabecera += f" — {len(medios)} medios"
        msg = (f"{cabecera}\n\n{html.escape(titular)}\n\n"
               f"<b>Fuentes:</b>\n" + "\n".join(fuentes))
        enviar_telegram(msg)
        estado["eventos"].append({"clave": clave, "resumen": titular,
                                  "url": items[indices[0]]["url"], "ts": time.time()})
        claves_previas.add(norm(clave)); huellas_previas.add(huella(titular))
        nuevas += 1

    guardar_estado(estado)
    print(f"  → {nuevas} alerta(s) enviada(s)")

if __name__ == "__main__":
    main()

