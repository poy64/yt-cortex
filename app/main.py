"""YT Cortex — interface web (FastAPI)."""
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from . import db, llm, skills, transcribe, worker, youtube

BASE = os.path.dirname(__file__)
@asynccontextmanager
async def lifespan(_app):
    db.init()
    worker.start()
    yield


app = FastAPI(title="YT Cortex", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

APP_USER = os.environ.get("APP_USER", "iban")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").enable("strikethrough")


# ------------------------------------------------------------------ filtres Jinja
def md_filter(text):
    html = _md.render(text or "")
    html = re.sub(r'<a href="(https?://)', r'<a target="_blank" rel="noopener noreferrer" href="\1', html)
    return Markup(html)


def dur_filter(sec):
    sec = int(sec or 0)
    if not sec:
        return ""
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}" if h else f"{m}:{s:02d}"


def ts_filter(t):
    if not t:
        return "—"
    return datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M")


STATUS_FR = {"queued": "En file", "fetching": "Lecture…", "transcribing": "Transcription…", "transcribed": "Transcrit",
             "summarizing": "Résumé…", "done": "Terminé", "error": "Erreur"}
ACTIVE = ("queued", "fetching", "transcribing", "summarizing")

templates.env.filters.update(md=md_filter, dur=dur_filter, ts=ts_filter, jl=lambda s: db.jloads(s, []))
templates.env.globals.update(STATUS_FR=STATUS_FR, ACTIVE=ACTIVE)


def render(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------------------------ sécurité
COOKIE = "ytc_session"
SESSION_TTL = 30 * 24 * 3600
_SKEY = hashlib.sha256(("ytcortex|" + APP_USER + "|" + APP_PASSWORD).encode()).digest()
_fails = {}  # ip -> (nombre d'échecs, instant du dernier échec)


def _sign(payload):
    return hmac.new(_SKEY, payload.encode(), hashlib.sha256).hexdigest()


def make_session():
    exp = str(int(time.time()) + SESSION_TTL)
    return exp + "." + _sign(exp)


def valid_session(tok):
    exp, _, sig = (tok or "").partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp))


def _safe_next(n):
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def _authed(request):
    if valid_session(request.cookies.get(COOKIE)):
        return True
    h = request.headers.get("authorization", "")  # accès scripts (curl -u), sans fenêtre navigateur
    if h.lower().startswith("basic "):
        try:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
            return hmac.compare_digest(u, APP_USER) and hmac.compare_digest(p, APP_PASSWORD)
        except Exception:
            return False
    return False


def _login_page(request, error="", nxt="/", status=200):
    return templates.TemplateResponse(request, "login.html", {"error": error, "next": nxt}, status_code=status)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path == "/healthz":
        return PlainTextResponse("ok")
    if not APP_PASSWORD:
        return PlainTextResponse("APP_PASSWORD non configuré : service verrouillé.", status_code=503)
    if request.method not in ("GET", "HEAD", "OPTIONS"):  # anti-CSRF : l'origine doit être l'hôte lui-même
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse("Origine refusée", status_code=403)
    if path not in ("/login", "/static/style.css") and not _authed(request):
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login?next=" + quote(request.url.path + ("?" + request.url.query if request.url.query else "")), status_code=303)
        return Response("Authentification requise", status_code=401)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store" if path == "/login" else resp.headers.get("Cache-Control", "no-cache")
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    if _authed(request):
        return back(_safe_next(next))
    return _login_page(request, nxt=_safe_next(next))


@app.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    n, last = _fails.get(ip, (0, 0))
    if n >= 5 and time.time() - last < 300:
        return _login_page(request, "Trop d'essais, réessaie dans quelques minutes.", _safe_next(next), 429)
    if hmac.compare_digest(username.strip(), APP_USER) and hmac.compare_digest(password, APP_PASSWORD):
        _fails.pop(ip, None)
        resp = back(_safe_next(next))
        resp.set_cookie(COOKIE, make_session(), max_age=SESSION_TTL, httponly=True, samesite="lax",
                        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
        return resp
    _fails[ip] = (n + 1 if time.time() - last < 300 else 1, time.time())
    return _login_page(request, "Identifiant ou mot de passe incorrect.", _safe_next(next), 401)


@app.post("/logout")
async def logout():
    resp = back("/login")
    resp.delete_cookie(COOKIE)
    return resp


def back(url="/"):
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ tableau de bord
def _videos(q="", limit=300):
    rows = db.q("SELECT * FROM videos ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r["title"] or "").lower() or ql in (r["channel"] or "").lower() or ql in (r["summary_md"] or "").lower()]
    return rows


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = ""):
    rows = _videos(q)
    return render(request, "index.html", videos=rows, q=q, llm_ok=llm.configured(),
                  whisper_ok=transcribe.available(), active=any(r["status"] in ACTIVE for r in rows))


@app.get("/partials/videos", response_class=HTMLResponse)
def partial_videos(request: Request, q: str = ""):
    rows = _videos(q)
    return render(request, "_videos.html", videos=rows)


@app.get("/api/active")
def api_active():
    n = db.one("SELECT COUNT(*) c FROM videos WHERE status IN ('queued','fetching','transcribing','summarizing')")["c"]
    return {"active": n}


@app.post("/videos/add")
def videos_add(urls: str = Form("")):
    ids = youtube.parse_urls(urls)
    if ids:
        worker.add_videos(ids)
    return back("/")


# ------------------------------------------------------------------ page vidéo
@app.get("/video/{vid}", response_class=HTMLResponse)
def video_page(request: Request, vid: int):
    v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
    if not v:
        return PlainTextResponse("Introuvable", status_code=404)
    return render(request, "video.html", v=v, chapters=db.jloads(v["chapters_json"], []), links=db.jloads(v["links_json"], []),
                  llm_ok=llm.configured(), whisper_ok=transcribe.available())


@app.get("/video/{vid}/status")
def video_status(vid: int):
    v = db.one("SELECT status, step, error FROM videos WHERE id=?", (vid,))
    return dict(v) if v else JSONResponse({}, status_code=404)


@app.post("/video/{vid}/retry")
def video_retry(vid: int, full: int = Form(0)):
    cur = db.one("SELECT status FROM videos WHERE id=?", (vid,))
    if not cur or cur["status"] in ACTIVE:
        return back(f"/video/{vid}")
    db.update_video(vid, status="queued", error="", step="")
    worker.enqueue("video", vid, force_transcript=bool(full))
    return back(f"/video/{vid}")


@app.post("/video/{vid}/summarize")
def video_summarize(vid: int):
    cur = db.one("SELECT status, transcript FROM videos WHERE id=?", (vid,))
    if not cur or not cur["transcript"] or cur["status"] in ACTIVE:
        return back(f"/video/{vid}")
    db.update_video(vid, status="summarizing", step="En file…", error="")
    worker.enqueue("summarize", vid)
    return back(f"/video/{vid}")


@app.post("/video/{vid}/delete")
def video_delete(vid: int):
    db.run("DELETE FROM videos WHERE id=?", (vid,))
    return back("/")


@app.post("/video/{vid}/transcript")
async def video_transcript(vid: int, text: str = Form(""), file: UploadFile = File(None)):
    """Transcription manuelle : texte collé, fichier .txt/.srt/.vtt, ou fichier audio/vidéo passé à Whisper."""
    v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
    if not v:
        return PlainTextResponse("Introuvable", status_code=404)
    content = text.strip()
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > 500 * 1024 * 1024:
            return PlainTextResponse("Fichier trop volumineux (500 Mo max).", status_code=413)
        ext = os.path.splitext(file.filename)[1].lower()
        if ext in (".txt", ".md", ".srt", ".vtt"):
            raw = data.decode("utf-8", errors="replace")
            if ext in (".srt", ".vtt"):
                content = youtube.segments_to_text(youtube.parse_vtt(raw))
            else:
                content = raw.strip()
        elif not transcribe.available():
            return PlainTextResponse("Fichier audio/vidéo non pris en charge : Whisper est indisponible.", status_code=400)
        else:
            tmp = tempfile.mkdtemp(prefix="up_")
            path = os.path.join(tmp, "audio" + (ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".bin"))
            with open(path, "wb") as f:
                f.write(data)
            db.update_video(vid, status="queued", step="Fichier audio en file pour Whisper…", error="")
            worker.enqueue("upload", vid, path=path, tmp=tmp)
            return back(f"/video/{vid}")
    if not content:
        return PlainTextResponse("Aucune transcription exploitable dans ce texte/fichier.", status_code=400)
    if content:
        db.update_video(vid, transcript=content, transcript_clean="", source="manual",
                        links_json=json.dumps(youtube.extract_links(v["description"], content), ensure_ascii=False),
                        status="transcribed", error="", step="Transcription fournie")
        worker.enqueue("summarize", vid)
    return back(f"/video/{vid}")


# ------------------------------------------------------------------ skills
@app.post("/skills/create")
def skills_create(video_ids: list[int] = Form([]), name: str = Form("")):
    ids = [i for i in video_ids if db.one("SELECT 1 FROM videos WHERE id=? AND summary_md!=''", (i,))]
    if not ids:
        return PlainTextResponse("Sélectionne au moins une vidéo déjà résumée (statut « Terminé »).", status_code=400)
    cur = db.run("INSERT INTO skills(name,video_ids_json,status,created_at) VALUES(?,?,?,?)",
                 (skills.slugify(name) if name.strip() else "", json.dumps(ids), "queued", time.time()))
    worker.enqueue("skill", cur.lastrowid)
    return back(f"/skills/{cur.lastrowid}")


@app.get("/skills", response_class=HTMLResponse)
def skills_list(request: Request):
    return render(request, "skills.html", items=db.q("SELECT * FROM skills ORDER BY id DESC"),
                  dbx=skills.dropbox_configured(), vcount=lambda s: len(db.jloads(s["video_ids_json"], [])))


@app.get("/skills/{sid}", response_class=HTMLResponse)
def skill_page(request: Request, sid: int):
    s = db.one("SELECT * FROM skills WHERE id=?", (sid,))
    if not s:
        return PlainTextResponse("Introuvable", status_code=404)
    vids = [db.one("SELECT id,title FROM videos WHERE id=?", (i,)) for i in db.jloads(s["video_ids_json"], [])]
    return render(request, "skill.html", s=s, files=db.jloads(s["files_json"], {}), vids=[v for v in vids if v],
                  dbx=skills.dropbox_configured(), dbx_path=db.get_setting("dropbox_skills_path", "/-=AIOS=-/.claude/skills"))


@app.get("/skills/{sid}/download")
def skill_download(sid: int):
    s = db.one("SELECT * FROM skills WHERE id=?", (sid,))
    if not s or s["status"] != "done":
        return PlainTextResponse("Skill pas prête", status_code=404)
    return Response(skills.zip_bytes(s), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{s["name"]}.skill"'})


@app.post("/skills/{sid}/push")
def skill_push(request: Request, sid: int):
    try:
        skills.push_to_dropbox(sid)
        return back(f"/skills/{sid}?ok=1")
    except Exception as e:  # noqa: BLE001
        db.run("UPDATE skills SET error=? WHERE id=?", (str(e)[:600], sid))
        return back(f"/skills/{sid}?err=1")


@app.post("/skills/{sid}/delete")
def skill_delete(sid: int):
    db.run("DELETE FROM skills WHERE id=?", (sid,))
    return back("/skills")


# ------------------------------------------------------------------ chaînes
@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, err: str = ""):
    rows = db.q("""SELECT c.*, (SELECT COUNT(*) FROM channel_videos cv WHERE cv.channel_id=c.id) n
                   FROM channels c ORDER BY c.name COLLATE NOCASE""")
    return render(request, "channels.html", channels=rows, err=err)


@app.post("/channels/add")
def channels_add(channel: str = Form("")):
    try:
        info = youtube.resolve_channel(channel)
    except youtube.YTError as e:
        from urllib.parse import quote
        return back("/channels?err=" + quote(str(e)[:500]))
    row = db.one("SELECT id FROM channels WHERE yt_channel_id=?", (info["channel_id"],))
    if row:
        cid = row["id"]
    else:
        cid = db.run("INSERT INTO channels(yt_channel_id,name,url,added_at,sync_status) VALUES(?,?,?,?,?)",
                     (info["channel_id"], info["name"], info["url"], time.time(), "running")).lastrowid
    worker.enqueue("channel", cid)
    return back(f"/channels/{cid}")


@app.get("/channels/{cid}", response_class=HTMLResponse)
def channel_page(request: Request, cid: int):
    ch = db.one("SELECT * FROM channels WHERE id=?", (cid,))
    if not ch:
        return PlainTextResponse("Introuvable", status_code=404)
    vids = db.q("""SELECT cv.*, v.id AS vid, v.status AS vstatus FROM channel_videos cv
                   LEFT JOIN videos v ON v.yt_id=cv.yt_id WHERE cv.channel_id=? ORDER BY cv.published DESC""", (cid,))
    return render(request, "channel.html", ch=ch, vids=vids, days=db.get_setting("channel_days", "60"))


@app.post("/channels/{cid}/sync")
def channel_sync(cid: int):
    db.run("UPDATE channels SET sync_status='running', sync_error='' WHERE id=?", (cid,))
    worker.enqueue("channel", cid)
    return back(f"/channels/{cid}")


@app.post("/channels/{cid}/process")
def channel_process(cid: int, yt_ids: list[str] = Form([])):
    meta = {r["yt_id"]: dict(r) for r in db.q("SELECT * FROM channel_videos WHERE channel_id=?", (cid,))}
    valid = [y for y in yt_ids if y in meta]
    worker.add_videos(valid, meta)
    return back("/")


@app.post("/channels/{cid}/delete")
def channel_delete(cid: int):
    db.run("DELETE FROM channel_videos WHERE channel_id=?", (cid,))
    db.run("DELETE FROM channels WHERE id=?", (cid,))
    return back("/channels")


# ------------------------------------------------------------------ réglages
SECRET = {"anthropic_api_key", "dropbox_app_secret", "dropbox_refresh_token", "proxy_url"}
TEXT_FIELDS = ["anthropic_api_key", "llm_model", "output_language", "whisper_model", "sub_langs", "channel_days",
               "proxy_url", "bgutil_url", "dropbox_app_key", "dropbox_app_secret", "dropbox_skills_path"]


def _mask(v):
    return ("•" * 8 + v[-4:]) if v and len(v) > 8 else ("•" * len(v))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    vals = {k: db.get_setting(k, "") for k in TEXT_FIELDS + ["resolve_tool_links", "auto_sync"]}
    masked = {k: _mask(vals[k]) for k in SECRET if vals.get(k)}
    cookies = os.path.exists(youtube.COOKIES_PATH())
    return render(request, "settings.html", vals=vals, masked=masked, cookies=cookies, msg=msg,
                  default_model=llm.DEFAULT_MODEL, dbx=skills.dropbox_configured(),
                  dbx_url=skills.dropbox_authorize_url() if vals.get("dropbox_app_key") else "")


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for k in TEXT_FIELDS:
        v = (form.get(k) or "").strip()
        if k in SECRET and not v:
            continue  # vide = conserver
        if k == "dropbox_app_secret" and not v:
            continue
        if v or k not in SECRET:
            db.set_setting(k, v)
    db.set_setting("resolve_tool_links", "1" if form.get("resolve_tool_links") else "0")
    db.set_setting("auto_sync", "1" if form.get("auto_sync") else "0")
    for k in form.getlist("clear"):
        if k in SECRET:
            db.set_setting(k, "")
    return back("/settings?msg=Réglages enregistrés")


@app.post("/settings/cookies")
def settings_cookies(cookies: str = Form("")):
    p = youtube.COOKIES_PATH()
    c = cookies.strip()
    if not c:
        if os.path.exists(p):
            os.remove(p)
        return back("/settings?msg=Cookies supprimés")
    if "\t" not in c:
        return back("/settings?msg=Format invalide : colle un fichier cookies.txt (format Netscape, colonnes séparées par des tabulations)")
    with open(p, "w") as f:
        f.write(c + "\n")
    os.chmod(p, 0o600)
    return back("/settings?msg=Cookies enregistrés")


@app.post("/settings/dropbox")
def settings_dropbox(code: str = Form("")):
    try:
        skills.dropbox_exchange_code(code)
        return back("/settings?msg=Dropbox connecté")
    except Exception as e:  # noqa: BLE001
        from urllib.parse import quote
        return back("/settings?msg=" + quote(str(e)[:300]))


# ------------------------------------------------------------------ diagnostic
@app.get("/diag", response_class=HTMLResponse)
def diag(request: Request, test: str = ""):
    info = {"yt-dlp": youtube.version(), "deno": shutil.which("deno") or "absent", "ffmpeg": shutil.which("ffmpeg") or "absent",
            "faster-whisper": "ok" if transcribe.available() else "absent", "clé Anthropic": "ok" if llm.configured() else "absente",
            "modèle LLM": llm.model(), "cookies": "oui" if os.path.exists(youtube.COOKIES_PATH()) else "non",
            "bgutil": db.get_setting("bgutil_url", "") or "—", "proxy": "oui" if db.get_setting("proxy_url", "") else "non"}
    result = None
    vid = youtube.parse_video_id(test)
    if vid:
        t0 = time.time()
        try:
            m = youtube.fetch_metadata(f"https://www.youtube.com/watch?v={vid}")
            subs = None
            try:
                subs = youtube.fetch_subtitles(vid)
                sub_info = f"{subs[1]} ({subs[2]}), {len(subs[0])} caractères" if subs else "aucun sous-titre"
            except youtube.YTError as e:
                sub_info = "ÉCHEC : " + str(e)
            result = {"titre": m.get("title"), "partiel": bool(m.get("_partial")), "sous-titres": sub_info,
                      "durée de l'essai": f"{time.time()-t0:.1f}s"}
        except youtube.YTError as e:
            result = {"ÉCHEC": str(e)}
    return render(request, "diag.html", info=info, result=result, test=test)
