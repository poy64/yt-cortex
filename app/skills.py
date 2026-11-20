"""Génération, export (.skill = zip) et dépôt Dropbox des skills."""
import io
import json
import re
import time
import unicodedata
import zipfile

import httpx

from . import db, llm


def slugify(text: str, maxlen=50) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return (t[:maxlen].strip("-")) or "skill"


def _clean_md(md: str) -> str:
    md = md.strip()
    md = re.sub(r"^```(?:markdown|md)?\s*\n", "", md)
    md = re.sub(r"\n```\s*$", "", md)
    return md


def normalize_skill_md(md: str, fallback_name: str, fallback_desc: str) -> (str, str, str):
    """Garantit un frontmatter valide ; retourne (md, name, description)."""
    md = _clean_md(md)
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", md, re.S)
    fm, body = (m.group(1), m.group(2)) if m else ("", md)
    name_m = re.search(r"^name:\s*(.+)$", fm, re.M)
    desc_m = re.search(r"^description:\s*(.+?)(?=\n\w+:|\Z)", fm, re.M | re.S)
    name = slugify(name_m.group(1).strip().strip("\"'")) if name_m else slugify(fallback_name)
    desc = re.sub(r"\s+", " ", desc_m.group(1).strip().strip("\"'")) if desc_m else fallback_desc
    desc = desc.replace("<", "").replace(">", "")[:900] or fallback_desc
    desc_yaml = desc.replace("\\", "/").replace('"', "'")
    out = f'---\nname: {name}\ndescription: "{desc_yaml}"\n---\n\n{body.strip()}\n'
    return out, name, desc


def build_skill(skill_id: int):
    """Tâche de fond : génère SKILL.md + references/*.md."""
    s = db.one("SELECT * FROM skills WHERE id=?", (skill_id,))
    vids = [db.one("SELECT * FROM videos WHERE id=?", (i,)) for i in db.jloads(s["video_ids_json"], [])]
    vids = [v for v in vids if v and v["summary_md"]]
    db.run("UPDATE skills SET status='generating' WHERE id=?", (skill_id,))
    try:
        if not vids:
            raise llm.LLMError("Aucune vidéo résumée dans la sélection.")
        refs = {}
        names = []
        for v in vids:
            base = slugify(v["title"], 60)
            nm = f"{base}.md"
            k = 2
            while nm in names:
                nm = f"{base}-{k}.md"
                k += 1
            names.append(nm)
            head = f"<!-- Source : {v['title']} — {v['channel']} — {v['url']} -->\n\n"
            refs[f"references/{nm}"] = head + v["summary_md"].strip() + "\n"
        hint = s["name"] or vids[0]["title"]
        md = llm.make_skill_md(hint, vids, names)
        fb_desc = f"Savoir-faire tiré de vidéos YouTube : {', '.join(v['title'] for v in vids)[:300]}"
        md, name, desc = normalize_skill_md(md, hint, fb_desc)
        files = {"SKILL.md": md, **refs}
        db.run("UPDATE skills SET name=?, description=?, files_json=?, status='done', error='' WHERE id=?",
               (name, desc, json.dumps(files, ensure_ascii=False), skill_id))
    except Exception as e:  # noqa: BLE001
        db.run("UPDATE skills SET status='error', error=? WHERE id=?", (str(e)[:800], skill_id))


def zip_bytes(skill) -> bytes:
    files = db.jloads(skill["files_json"], {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(f"{skill['name']}/{path}", content)
    return buf.getvalue()


# ------------------------------------------------------------------ Dropbox
DBX_TOKEN = "https://api.dropboxapi.com/oauth2/token"


def dropbox_configured() -> bool:
    return all(db.get_setting(k, "") for k in ("dropbox_app_key", "dropbox_app_secret", "dropbox_refresh_token"))


def dropbox_authorize_url() -> str:
    key = db.get_setting("dropbox_app_key", "")
    return ("https://www.dropbox.com/oauth2/authorize?client_id=" + key +
            "&response_type=code&token_access_type=offline")


def dropbox_exchange_code(code: str):
    r = httpx.post(DBX_TOKEN, data={"code": code.strip(), "grant_type": "authorization_code"},
                   auth=(db.get_setting("dropbox_app_key", ""), db.get_setting("dropbox_app_secret", "")), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Dropbox : {r.text[:300]}")
    rt = r.json().get("refresh_token")
    if not rt:
        raise RuntimeError("Dropbox n'a pas renvoyé de refresh_token (vérifie token_access_type=offline).")
    db.set_setting("dropbox_refresh_token", rt)


def _dbx_access_token() -> str:
    r = httpx.post(DBX_TOKEN, data={"grant_type": "refresh_token", "refresh_token": db.get_setting("dropbox_refresh_token", "")},
                   auth=(db.get_setting("dropbox_app_key", ""), db.get_setting("dropbox_app_secret", "")), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Dropbox auth : {r.text[:300]}")
    return r.json()["access_token"]


def push_to_dropbox(skill_id: int) -> str:
    s = db.one("SELECT * FROM skills WHERE id=?", (skill_id,))
    if not s or s["status"] != "done":
        raise RuntimeError("Skill pas prête.")
    if not dropbox_configured():
        raise RuntimeError("Dropbox non connecté (Réglages).")
    base = (db.get_setting("dropbox_skills_path", "/-=AIOS=-/.claude/skills") or "").rstrip("/")
    tok = _dbx_access_token()
    for path, content in db.jloads(s["files_json"], {}).items():
        full = f"{base}/{s['name']}/{path}"
        r = httpx.post("https://content.dropboxapi.com/2/files/upload",
                       headers={"Authorization": f"Bearer {tok}",
                                "Dropbox-API-Arg": json.dumps({"path": full, "mode": "overwrite", "mute": True}, ensure_ascii=True),
                                "Content-Type": "application/octet-stream"},
                       content=content.encode("utf-8"), timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Dropbox upload {path} : {r.text[:300]}")
    where = f"{base}/{s['name']}"
    db.run("UPDATE skills SET pushed_at=?, push_path=? WHERE id=?", (time.time(), where, skill_id))
    return where


# ------------------------------------------------------------------ export NotebookLM
def notebooklm_md(v, with_transcript=False) -> str:
    """Un fichier Markdown autonome par vidéo : en-tête visible (titre, chaîne, URL) + fiche (+ transcription nettoyée)."""
    head = [f"# {v['title'] or v['url']}", "",
            f"- Chaîne : {v['channel'] or '—'}", f"- Vidéo : {v['url']}"]
    if v["published"]:
        head.append(f"- Publiée le : {v['published']}")
    body = (v["summary_md"] or "").strip()
    if body.startswith("# "):  # le titre de la fiche remplace celui de l'en-tête : pas de doublon
        body = body.split("\n", 1)[1].strip() if "\n" in body else ""
    out = "\n".join(head) + "\n\n" + body + "\n"
    if with_transcript and (v["transcript_clean"] or v["transcript"]):
        out += "\n\n## Transcription nettoyée\n\n" + (v["transcript_clean"] or v["transcript"]).strip() + "\n"
    return out


def notebooklm_zip(videos, with_transcript=False) -> bytes:
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for v in videos:
            base = slugify(v["title"] or v["yt_id"], 80)
            nm, k = f"{base}.md", 2
            while nm in used:
                nm, k = f"{base}-{k}.md", k + 1
            used.add(nm)
            z.writestr(nm, notebooklm_md(v, with_transcript))
    return buf.getvalue()
