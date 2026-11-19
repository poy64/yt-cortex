"""Appels à l'API Anthropic (résumé « formation », liens/outils, génération de skill)."""
import json
import re
import time

import httpx

from . import db

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"


class LLMError(Exception):
    pass


def api_key() -> str:
    return (db.get_setting("anthropic_api_key", "") or "").strip()


def model() -> str:
    return (db.get_setting("llm_model", DEFAULT_MODEL) or DEFAULT_MODEL).strip()


def configured() -> bool:
    return bool(api_key())


def _call(system: str, user: str, max_tokens=8000, tools=None, retries=4) -> str:
    key = api_key()
    if not key:
        raise LLMError("Clé API Anthropic absente : renseigne-la dans Réglages.")
    payload = {"model": model(), "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user}]}
    if tools:
        payload["tools"] = tools
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = ""
    for i in range(retries):
        try:
            r = httpx.post(API, headers=headers, json=payload, timeout=600)
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(2 ** i * 3)
            continue
        if r.status_code == 200:
            j = r.json()
            return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text").strip()
        last = f"HTTP {r.status_code}: {r.text[:400]}"
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(2 ** i * 5)
            continue
        break
    raise LLMError(last)


# ------------------------------------------------------------------ résumé « formation »
SUMMARY_SYSTEM = """Tu es un ingénieur pédagogique. Tu transformes la transcription d'une vidéo YouTube en FICHE DE FORMATION \
exploitable sans revoir la vidéo, rédigée en {lang}.

Règles strictes :
- Fidélité : n'invente AUCUN fait, chiffre, outil ou URL. Si la transcription est ambiguë, dis-le.
- Les liens : n'utilise que des URL présentes dans la liste « LIENS CANDIDATS » fournie. Insère-les en Markdown [nom](url) là où \
  l'outil/la ressource est évoqué(e). Pour un outil cité sans URL fournie, écris son nom et ajoute « (lien non fourni dans la vidéo) ».
- Supprime les passages sans valeur : intro, demandes de like/abonnement, sponsors, digressions.
- Garde les chiffres, noms d'outils, paramètres, prompts et commandes EXACTS (en blocs de code si besoin).
- Appuie-toi sur les horodatages [mm:ss] de la transcription pour indiquer où trouver chaque module dans la vidéo.

Structure Markdown obligatoire :
# {{titre pédagogique}}
> Résumé en 3 à 5 lignes · Public visé · Niveau · Temps de mise en pratique estimé
## L'essentiel à retenir
(6 à 10 puces)
## Plan de la formation
(Liste des modules avec horodatage)
## Module 1 — … (puis 2, 3…)
Pour chaque module : explications structurées, exemples, chiffres clés, puis un encadré **À faire :** avec étapes numérotées.
## Outils, ressources et liens
(Tableau ou liste : outil/ressource — à quoi ça sert — lien)
## Pièges et bonnes pratiques
## Checklist de mise en application
## Glossaire
(seulement si des termes techniques le justifient)"""

PARTIAL_SYSTEM = """Tu résumes un EXTRAIT d'une transcription vidéo en notes détaillées en {lang} (garde horodatages [mm:ss], \
chiffres, noms d'outils, commandes, prompts exacts ; n'invente rien). Sortie : notes Markdown denses, pas d'introduction."""

CHUNK = 140_000


def _video_header(v) -> str:
    chapters = db.jloads(v["chapters_json"], [])
    ch = "\n".join(f"- {c['start']//60:02d}:{c['start']%60:02d} {c['title']}" for c in chapters) or "(aucun)"
    return (f"TITRE : {v['title']}\nCHAÎNE : {v['channel']}\nURL : {v['url']}\nPUBLIÉE : {v['published']}\n"
            f"DURÉE : {v['duration']//60} min\nCHAPITRES :\n{ch}\n")


def _links_block(v) -> str:
    links = db.jloads(v["links_json"], [])
    return "\n".join(f"- {l['url']}  ({l['label']})" if l.get("label") else f"- {l['url']}" for l in links[:80]) or "(aucun)"


def summarize(v) -> str:
    lang = db.get_setting("output_language", "français") or "français"
    sysmsg = SUMMARY_SYSTEM.format(lang=lang)
    transcript = v["transcript"]
    if len(transcript) > CHUNK * 2:  # très longue vidéo : map-reduce
        parts = [transcript[i:i + CHUNK] for i in range(0, len(transcript), CHUNK)]
        notes = [_call(PARTIAL_SYSTEM.format(lang=lang), f"EXTRAIT {i+1}/{len(parts)} :\n{p}", max_tokens=6000)
                 for i, p in enumerate(parts)]
        body = "NOTES DÉTAILLÉES (transcription trop longue, résumée par blocs) :\n\n" + "\n\n---\n\n".join(notes)
    else:
        body = "TRANSCRIPTION :\n" + transcript
    desc = (v["description"] or "")[:6000]
    user = f"{_video_header(v)}\nDESCRIPTION YOUTUBE :\n{desc}\n\nLIENS CANDIDATS :\n{_links_block(v)}\n\n{body}"
    return _call(sysmsg, user, max_tokens=10000)


# ------------------------------------------------------------------ résolution de liens d'outils (recherche web)
def resolve_tool_links(summary_md: str, known_urls) -> list:
    """Cherche (recherche web Anthropic) l'URL officielle des outils cités sans lien. Retourne [{name,url}] — best effort."""
    if db.get_setting("resolve_tool_links", "1") != "1":
        return []
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]
    system = ("Tu reçois une fiche de formation. Liste les outils/logiciels/services/sites CITÉS dont l'URL n'apparaît pas "
              "déjà dans la liste des liens connus (max 10). Pour chacun, trouve par recherche web son site OFFICIEL. "
              "Réponds UNIQUEMENT par un tableau JSON : [{\"name\":\"...\",\"url\":\"https://...\"}]. "
              "Si tu n'es pas certain de l'URL officielle, omets l'élément. Aucun texte hors JSON.")
    user = "LIENS CONNUS :\n" + "\n".join(known_urls) + "\n\nFICHE :\n" + summary_md[:30000]
    try:
        out = _call(system, user, max_tokens=2000, tools=tools)
    except LLMError:
        return []
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    return [{"name": str(a.get("name", ""))[:80], "url": a["url"]} for a in arr
            if isinstance(a, dict) and str(a.get("url", "")).startswith("http")]


# ------------------------------------------------------------------ skill
SKILL_SYSTEM = """Tu crées un SKILL pour un agent IA (format Claude « SKILL.md ») à partir de fiches de formation issues de vidéos.
Un skill transmet à l'agent un savoir-faire réutilisable. Sortie : UNIQUEMENT le contenu du fichier SKILL.md, commençant par :
---
name: <kebab-case, minuscules, chiffres et tirets, ≤ 50 car.>
description: <1 à 3 phrases, ≤ 400 car., sans chevrons : QUAND utiliser ce skill (déclencheurs concrets) et ce qu'il apporte>
---
Corps (français, ≤ 250 lignes) :
# Titre
## Quand l'utiliser
## Principes clés (règles et décisions à appliquer)
## Procédure (étapes numérotées, actionnables)
## Outils et liens (uniquement ceux présents dans les fiches)
## Pièges à éviter
## Références (liste des fichiers references/*.md fournis et ce que chacun contient)
Règles : n'invente rien, reste fidèle aux fiches ; synthétise (pas de copier-coller massif : le détail est dans references/) ; \
écris à l'impératif, pour un agent qui exécute une tâche."""


def make_skill_md(name_hint: str, videos, ref_names) -> str:
    fiches = []
    per = max(6000, min(40000, 300000 // max(1, len(videos))))  # plafond total ~300k caractères
    for v, ref in zip(videos, ref_names):
        fiches.append(f"=== FICHE references/{ref} — « {v['title']} » ({v['channel']}, {v['url']}) ===\n{v['summary_md'][:per]}")
    user = (f"Nom souhaité (indicatif) : {name_hint}\n\n" + "\n\n".join(fiches))
    return _call(SKILL_SYSTEM, user, max_tokens=6000)
