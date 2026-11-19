"""File de tâches en arrière-plan : vidéos, résumés, skills, synchronisation des chaînes."""
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import traceback

from . import db, llm, skills, transcribe, youtube

Q: "queue.Queue" = queue.Queue()
_started = False


Q2: "queue.Queue" = queue.Queue()  # skills, résumés (ne bloquent pas Whisper)
Q3: "queue.Queue" = queue.Queue()  # synchro de chaînes (lente)


def enqueue(kind: str, ident: int, **kw):
    (Q if kind in ("video", "upload") else Q3 if kind == "channel" else Q2).put((kind, ident, kw))


def process_upload(vid: int, path: str, tmp: str):
    """Transcrit un fichier audio/vidéo fourni par l'utilisateur, puis résume."""
    try:
        v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
        _set(vid, status="transcribing", step="Transcription Whisper du fichier…")
        text, lang = transcribe.transcribe_file(path)
        _set(vid, transcript=text, lang=lang, source="whisper",
             links_json=json.dumps(youtube.extract_links(v["description"], text), ensure_ascii=False), status="transcribed")
        summarize_video(vid)
    except Exception as e:  # noqa: BLE001
        _set(vid, status="error", error=str(e)[:800], step="")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ vidéo
def _set(vid, **f):
    db.update_video(vid, **f)


def add_videos(yt_ids, source_meta=None):
    """Crée (si besoin) les vidéos et les met en file. Retourne la liste des ids internes."""
    ids = []
    for yid in yt_ids:
        row = db.one("SELECT id, status FROM videos WHERE yt_id=?", (yid,))
        if row:
            ids.append(row["id"])
            if row["status"] == "error":
                _set(row["id"], status="queued", error="", step="")
                enqueue("video", row["id"])
            continue
        meta = (source_meta or {}).get(yid, {})
        now = time.time()
        cur = db.run(
            "INSERT OR IGNORE INTO videos(yt_id,url,title,published,duration,thumbnail,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (yid, f"https://www.youtube.com/watch?v={yid}", meta.get("title", ""), meta.get("published", ""),
             meta.get("duration", 0), meta.get("thumbnail", f"https://i.ytimg.com/vi/{yid}/hqdefault.jpg"), "queued", now, now))
        if cur.rowcount:
            ids.append(cur.lastrowid)
            enqueue("video", cur.lastrowid)
        else:
            r2 = db.one("SELECT id FROM videos WHERE yt_id=?", (yid,))
            if r2:
                ids.append(r2["id"])
    return ids


def process_video(vid: int, force_transcript=False):
    v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
    if not v:
        return
    try:
        # 1. métadonnées
        if not v["description"] or force_transcript:
            _set(vid, status="fetching", step="Lecture des métadonnées…", error="")
            m = youtube.fetch_metadata(v["url"])
            fields = youtube.meta_to_fields(m)
            if m.get("_partial"):  # oEmbed seulement : ne pas écraser avec du vide
                fields = {k: val for k, val in fields.items() if val}
            _set(vid, **fields)
            v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
        # 2. transcription
        if not v["transcript"] or force_transcript:
            _set(vid, status="transcribing", step="Recherche des sous-titres…")
            got = None
            sub_err = ""
            try:
                got = youtube.fetch_subtitles(v["yt_id"], tuple(x.strip() for x in db.get_setting("sub_langs", "fr,en").split(",") if x.strip()) or ("fr", "en"))
            except youtube.YTError as e:
                sub_err = str(e)
            if got:
                text, lang, kind = got
                _set(vid, transcript=text, lang=lang, source=f"subs-{kind}")
            else:
                if not transcribe.available():
                    raise youtube.YTError(sub_err or "Pas de sous-titres et Whisper indisponible.")
                if v["duration"] > 4 * 3600:
                    raise youtube.YTError((sub_err + " | " if sub_err else "") + "Vidéo de plus de 4 h : Whisper refusé (fournis la transcription à la main).")
                _set(vid, step="Téléchargement de l'audio pour Whisper…")
                tmp = tempfile.mkdtemp(prefix="aud_")
                try:
                    audio = youtube.download_audio(v["yt_id"], tmp)
                    _set(vid, step="Transcription Whisper (peut prendre quelques minutes)…")
                    text, lang = transcribe.transcribe_file(audio)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                _set(vid, transcript=text, lang=lang, source="whisper")
            v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
        links = youtube.extract_links(v["description"], v["transcript"])
        _set(vid, links_json=json.dumps(links, ensure_ascii=False),
             status="summarizing" if llm.configured() else "transcribed", step="Transcription prête")
        # 3. résumé
        summarize_video(vid)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set(vid, status="error", error=str(e)[:900], step="")
    finally:
        db.run("UPDATE videos SET attempts=0 WHERE id=?", (vid,))  # fini (ok ou erreur) : pas de plantage en cours


def summarize_video(vid: int):
    v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
    if not v or not v["transcript"]:
        return
    if not llm.configured():
        _set(vid, status="transcribed", step="Résumé en attente : clé API Anthropic à renseigner dans Réglages")
        return
    try:
        _set(vid, status="summarizing", step="Rédaction de la formation…", error="")
        md = llm.summarize(v)
        links = db.jloads(v["links_json"], [])
        extra = []
        if links:
            extra.append("## Liens de la description YouTube\n" + "\n".join(
                f"- [{l['label'] or l['url']}]({l['url']})" if l.get("label") else f"- <{l['url']}>"
                for l in links if l["source"] == "description"))
        _set(vid, step="Recherche des liens officiels des outils…")
        resolved = llm.resolve_tool_links(md, [l["url"] for l in links])
        if resolved:
            extra.append("## Liens officiels des outils cités (recherche web)\n" + "\n".join(
                f"- [{r['name']}]({r['url']})" for r in resolved))
        if extra:
            md = md.rstrip() + "\n\n" + "\n\n".join(e for e in extra if e.strip()) + "\n"
        _set(vid, summary_md=md, status="done", step="Terminé", error="")
    except Exception as e:  # noqa: BLE001
        _set(vid, status="error", error="Résumé : " + str(e)[:800], step="")


# ------------------------------------------------------------------ chaînes
def sync_channel(cid: int):
    ch = db.one("SELECT * FROM channels WHERE id=?", (cid,))
    if not ch:
        return
    db.run("UPDATE channels SET sync_status='running', sync_error='' WHERE id=?", (cid,))
    try:
        days = int(db.get_setting("channel_days", "60") or 60)
        items = youtube.list_channel_recent(ch["yt_channel_id"], days=days)
        db.run("DELETE FROM channel_videos WHERE channel_id=?", (cid,))
        for it in items:
            db.run("INSERT OR REPLACE INTO channel_videos(yt_id,channel_id,title,published,duration,thumbnail) VALUES(?,?,?,?,?,?)",
                   (it["yt_id"], cid, it["title"], it["published"], it["duration"], it["thumbnail"]))
        db.run("UPDATE channels SET last_sync=?, sync_status='ok' WHERE id=?", (time.time(), cid))
    except Exception as e:  # noqa: BLE001
        db.run("UPDATE channels SET sync_status='error', sync_error=? WHERE id=?", (str(e)[:600], cid))


# ------------------------------------------------------------------ boucle
def _loop(q):
    while True:
        kind, ident, kw = q.get()
        try:
            if kind == "video":
                process_video(ident, **kw)
            elif kind == "upload":
                process_upload(ident, **kw)
            elif kind == "summarize":
                summarize_video(ident)
            elif kind == "skill":
                skills.build_skill(ident)
            elif kind == "channel":
                sync_channel(ident)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            q.task_done()


def _auto_sync_loop():
    while True:
        time.sleep(6 * 3600)
        if db.get_setting("auto_sync", "1") == "1":
            for ch in db.q("SELECT id FROM channels"):
                enqueue("channel", ch["id"])


def start():
    global _started
    if _started:
        return
    _started = True
    # reprise après redémarrage
    for r in db.q("SELECT id, attempts FROM videos WHERE status IN ('queued','fetching','transcribing','summarizing')"):
        if (r["attempts"] or 0) >= 2:  # a déjà fait planter / interrompu le service 2 fois
            db.update_video(r["id"], status="error", error="Traitement interrompu plusieurs fois (mémoire insuffisante ?). Clique « Tout retraiter » pour réessayer.", step="")
            continue
        db.run("UPDATE videos SET attempts=attempts+1 WHERE id=?", (r["id"],))
        db.update_video(r["id"], status="queued")
        enqueue("video", r["id"])
    for r in db.q("SELECT id FROM skills WHERE status IN ('queued','generating')"):
        enqueue("skill", r["id"])
    for r in db.q("SELECT id FROM channels WHERE sync_status='running'"):
        enqueue("channel", r["id"])
    threading.Thread(target=_loop, args=(Q,), daemon=True, name="worker-video").start()
    threading.Thread(target=_loop, args=(Q2,), daemon=True, name="worker-misc").start()
    threading.Thread(target=_loop, args=(Q3,), daemon=True, name="worker-channels").start()
    threading.Thread(target=_auto_sync_loop, daemon=True, name="autosync").start()
