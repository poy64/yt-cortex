"""Accès YouTube via yt-dlp (métadonnées, sous-titres, audio, listing de chaîne)."""
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import db

COOKIES_PATH = lambda: os.path.join(db.DATA_DIR, "cookies.txt")  # noqa: E731
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


class YTError(Exception):
    pass


# --------------------------------------------------------------------------- ids
def parse_video_id(text: str):
    """Retourne l'ID vidéo (11 car.) depuis une URL YouTube ou un ID nu, sinon None."""
    text = (text or "").strip()
    if not text:
        return None
    if YT_ID_RE.match(text):
        return text
    m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/|v/))([A-Za-z0-9_-]{11})", text)
    return m.group(1) if m else None


def parse_urls(blob: str):
    """Extrait des IDs uniques (dans l'ordre) d'un texte collé (une URL par ligne, ou mélange)."""
    seen, out = set(), []
    for token in re.split(r"[\s,;]+", blob or ""):
        vid = parse_video_id(token)
        if vid and vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out


# --------------------------------------------------------------------------- yt-dlp
def _base_args(client_attempt=None):
    args = ["yt-dlp", "--no-warnings", "--no-playlist", "--ignore-config"]
    if shutil.which("deno"):
        args += ["--js-runtimes", "deno"]
    cp = COOKIES_PATH()
    if os.path.exists(cp) and os.path.getsize(cp) > 20:
        args += ["--cookies", cp]
    proxy = db.get_setting("proxy_url", "").strip()
    if proxy:
        args += ["--proxy", proxy]
    bg = db.get_setting("bgutil_url", "").strip()
    if bg:
        args += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={bg}"]
    if client_attempt:
        for ea in ([client_attempt] if isinstance(client_attempt, str) else client_attempt):
            args += ["--extractor-args", ea]
    extra = db.get_setting("ytdlp_extra_args", "").strip()
    if extra:
        args += extra.split()
    return args


# Stratégies successives (la 1re = comportement par défaut de yt-dlp).
CLIENT_ATTEMPTS = [None, "youtube:player_client=tv", "youtube:player_client=web_embedded,mweb"]


def _run(cmd, timeout=180):
    # chaque appel travaille sur sa propre copie des cookies (yt-dlp réécrit le fichier en fin d'exécution)
    tmpc = None
    if "--cookies" in cmd:
        i = cmd.index("--cookies")
        fd, tmpc = tempfile.mkstemp(prefix="ck_", suffix=".txt")
        os.close(fd)
        shutil.copyfile(cmd[i + 1], tmpc)
        cmd = cmd[:i + 1] + [tmpc] + cmd[i + 2:]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise YTError("Délai dépassé (yt-dlp)")
    except FileNotFoundError:
        raise YTError("yt-dlp introuvable sur le serveur")
    finally:
        if tmpc:
            try:
                os.remove(tmpc)
            except OSError:
                pass
    return p


def explain_error(stderr: str) -> str:
    s = (stderr or "").strip().splitlines()
    last = " | ".join(s[-3:]) if s else "erreur inconnue"
    low = (stderr or "").lower()
    if "sign in to confirm" in low or "not a bot" in low or "po token" in low or "cookies" in low and "sign in" in low:
        return ("YouTube bloque l'IP du serveur (anti-bot). Solution : Réglages → coller un cookies.txt YouTube "
                "(ou un proxy résidentiel). Détail : " + last)
    if "private video" in low or "video unavailable" in low:
        return "Vidéo privée ou indisponible. Détail : " + last
    return last[:600]


def fetch_metadata(url: str) -> dict:
    """Métadonnées complètes (titre, description, chapitres…). Lève YTError si tout échoue."""
    last_err = ""
    for attempt in CLIENT_ATTEMPTS:
        p = _run(_base_args(attempt) + ["-J", "--skip-download", url], timeout=120)
        if p.returncode == 0 and p.stdout.strip():
            try:
                return json.loads(p.stdout)
            except json.JSONDecodeError:
                last_err = "JSON illisible"
                continue
        last_err = p.stderr
    # repli : oEmbed (titre + chaîne seulement)
    vid = parse_video_id(url)
    try:
        r = httpx.get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, timeout=15)
        if r.status_code == 200:
            j = r.json()
            return {"id": vid, "title": j.get("title", ""), "channel": j.get("author_name", ""),
                    "thumbnail": j.get("thumbnail_url", ""), "description": "", "_partial": True,
                    "_error": explain_error(last_err)}
    except Exception:
        pass
    raise YTError(explain_error(last_err))


def meta_to_fields(m: dict) -> dict:
    ud = m.get("upload_date") or ""
    pub = f"{ud[0:4]}-{ud[4:6]}-{ud[6:8]}" if len(ud) == 8 else ""
    chapters = [{"start": int(c.get("start_time", 0)), "title": c.get("title", "")} for c in (m.get("chapters") or [])]
    return {
        "title": m.get("title", "") or "",
        "channel": m.get("channel") or m.get("uploader") or "",
        "channel_id": m.get("channel_id") or "",
        "published": pub,
        "duration": int(m.get("duration") or 0),
        "thumbnail": m.get("thumbnail") or f"https://i.ytimg.com/vi/{m.get('id','')}/hqdefault.jpg",
        "description": m.get("description") or "",
        "chapters_json": json.dumps(chapters, ensure_ascii=False),
    }


# --------------------------------------------------------------------------- sous-titres
_TS = re.compile(r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d{3})\s+-->\s+(?:(\d+):)?(\d\d):(\d\d)[.,](\d{3})")
_TAG = re.compile(r"<[^>]+>")


def parse_vtt(text: str):
    """VTT/SRT -> liste de (start_sec, texte), en supprimant les répétitions des sous-titres auto 'roulants'."""
    segs, last_line = [], ""
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [l for l in block.split("\n") if l.strip()]
        start = None
        body = []
        for l in lines:
            m = _TS.search(l)
            if m:
                h, mi, s, ms = (int(x or 0) for x in m.groups()[:4])
                start = h * 3600 + mi * 60 + s + ms / 1000
            elif start is not None:
                body.append(l)
        if start is None:
            continue
        for l in body:
            l = _TAG.sub("", l).replace("&amp;", "&").replace("&gt;", ">").replace("&lt;", "<").replace("&#39;", "'").replace("&quot;", '"').strip()
            l = re.sub(r"\s+", " ", l)
            if not l or l == last_line:
                continue
            segs.append((start, l))
            last_line = l
    return segs


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def segments_to_text(segs, block_seconds=45):
    """Regroupe en paragraphes horodatés : '[mm:ss] texte…'."""
    out, cur, cur_start = [], [], None
    for start, txt in segs:
        if cur and start - cur_start >= block_seconds:
            out.append(f"[{fmt_ts(cur_start)}] " + " ".join(cur))
            cur, cur_start = [], None
        if cur_start is None:
            cur_start = start
        cur.append(txt)
    if cur:
        out.append(f"[{fmt_ts(cur_start or 0)}] " + " ".join(cur))
    return "\n\n".join(out)


def fetch_subtitles(yt_id: str, prefer_langs=("fr", "en")):
    """Retourne (texte_horodaté, langue, 'manual'|'auto') ou None si aucun sous-titre."""
    url = f"https://www.youtube.com/watch?v={yt_id}"
    langs = ",".join(x for l in prefer_langs for x in (l, f"{l}-.*", f"{l}.*"))
    last_err = ""
    for attempt in CLIENT_ATTEMPTS:
        tmp = tempfile.mkdtemp(prefix="subs_")
        try:
            # on ignore les traductions automatiques : on veut la langue d'origine de la vidéo
            ea = (attempt + ";skip=translated_subs") if attempt else "youtube:skip=translated_subs"
            cmd = _base_args(ea) + ["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", langs,
                                         "--sub-format", "vtt/srt/best", "--convert-subs", "vtt",
                                         "-o", os.path.join(tmp, "%(id)s.%(ext)s"), url]
            p = _run(cmd, timeout=180)
            files = glob.glob(os.path.join(tmp, "*.vtt"))
            if files:
                best = _pick_sub_file(files, yt_id, prefer_langs)
                text = open(best[0], encoding="utf-8", errors="replace").read()
                segs = parse_vtt(text)
                if segs:
                    return segments_to_text(segs), best[1], best[2]
            if p.returncode != 0:
                last_err = p.stderr
            else:
                last_err = ""  # pas d'erreur mais pas de sous-titres : inutile de réessayer d'autres clients
                return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if last_err:
        raise YTError(explain_error(last_err))
    return None


def _pick_sub_file(files, yt_id, prefer_langs):
    # nom: <id>.<lang>.vtt ; on ne peut pas distinguer manuel/auto par le nom -> heuristique: un fichier 'orig' ou sans -auto
    def lang_of(f):
        return os.path.basename(f)[len(yt_id) + 1:-4]

    def rank(f):
        lg = lang_of(f)
        for i, pl in enumerate(prefer_langs):
            if lg == pl:
                return (i, 0)
            if lg.startswith(pl):
                return (i, 1)
        return (len(prefer_langs), 2)

    files = sorted(files, key=rank)
    f = files[0]
    lg = lang_of(f)
    kind = "auto" if _looks_auto(f) else "manual"
    return f, lg, kind


def _looks_auto(path):
    try:
        head = open(path, encoding="utf-8", errors="replace").read(2000)
    except Exception:
        return False
    return "<c>" in head or re.search(r"<\d\d:\d\d:\d\d\.\d{3}>", head) is not None


# --------------------------------------------------------------------------- audio
def download_audio(yt_id: str, outdir: str) -> str:
    url = f"https://www.youtube.com/watch?v={yt_id}"
    last_err = ""
    for attempt in CLIENT_ATTEMPTS:
        cmd = _base_args(attempt) + ["-f", "bestaudio[abr<=96]/bestaudio/worst", "-o", os.path.join(outdir, "audio.%(ext)s"), url]
        p = _run(cmd, timeout=1500)
        files = [f for f in glob.glob(os.path.join(outdir, "audio.*")) if not f.endswith(".part")]
        if p.returncode == 0 and files:
            return files[0]
        last_err = p.stderr
    raise YTError("Téléchargement audio impossible : " + explain_error(last_err))


# --------------------------------------------------------------------------- liens
def extract_links(description: str, transcript: str = ""):
    """Liens trouvés dans la description (avec leur ligne de contexte) et la transcription."""
    out, seen = [], set()
    for src, text in (("description", description or ""), ("transcription", transcript or "")):
        for line in text.splitlines():
            for m in URL_RE.finditer(line):
                u = m.group(0).rstrip(".,;:!?»”")
                if u in seen:
                    continue
                seen.add(u)
                label = (line.replace(m.group(0), "").strip(" -–—:•*|>") or "")[:140]
                out.append({"url": u, "label": label, "source": src})
    return out


# --------------------------------------------------------------------------- chaînes
def _chan_url(text: str) -> str:
    t = (text or "").strip()
    if re.match(r"^UC[A-Za-z0-9_-]{22}$", t):
        return f"https://www.youtube.com/channel/{t}"
    if t.startswith("@"):
        return f"https://www.youtube.com/{t}"
    if not t.startswith("http"):
        t = "https://" + t
    host = (re.match(r"https?://([^/:?#]+)", t) or [None, ""])[1].lower()
    if host not in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        raise YTError("Seules les chaînes youtube.com sont acceptées.")
    return t


def resolve_channel(text: str) -> dict:
    """-> {'channel_id','name','url'}"""
    base = _chan_url(text).rstrip("/")  # peut lever YTError
    base = re.sub(r"/(videos|featured|streams|shorts|playlists|community|about)$", "", base)
    last_err = ""
    for attempt in CLIENT_ATTEMPTS[:2]:
        p = _run(_base_args(attempt) + ["--flat-playlist", "--playlist-end", "1", "-J", base + "/videos"], timeout=120)
        if p.returncode == 0 and p.stdout.strip():
            try:
                j = json.loads(p.stdout)
            except json.JSONDecodeError:
                last_err = "réponse illisible"
                continue
            cid = j.get("channel_id") or j.get("uploader_id") or ""
            if not cid.startswith("UC"):
                m = re.search(r"channel/(UC[A-Za-z0-9_-]{22})", j.get("channel_url") or j.get("webpage_url") or "")
                cid = m.group(1) if m else cid
            name = j.get("channel") or j.get("uploader") or (j.get("title") or "").replace(" - Videos", "")
            if cid:
                return {"channel_id": cid, "name": name or cid, "url": j.get("channel_url") or base}
        last_err = p.stderr
    raise YTError("Chaîne introuvable : " + explain_error(last_err))


def _rss_dates(channel_id: str) -> dict:
    try:
        r = httpx.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id}, timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return {}
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        root = ET.fromstring(r.text)
        return {e.find("yt:videoId", ns).text: e.find("a:published", ns).text[:10] for e in root.findall("a:entry", ns)}
    except Exception:
        return {}


def _video_date(yt_id: str):
    p = _run(_base_args() + ["--skip-download", "--print", "%(upload_date)s|%(duration)s", f"https://www.youtube.com/watch?v={yt_id}"], timeout=90)
    if p.returncode == 0:
        line = (p.stdout.strip().splitlines() or [""])[-1]
        ud, _, dur = line.partition("|")
        date = f"{ud[0:4]}-{ud[4:6]}-{ud[6:8]}" if len(ud) == 8 and ud.isdigit() else ""
        return date, int(dur) if dur.isdigit() else 0
    return "", 0


def list_channel_recent(channel_id: str, days: int = 60, max_videos: int = 150):
    """Vidéos publiées dans les `days` derniers jours (liste de dicts, du + récent au + ancien)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    last_err = ""
    entries = None
    for attempt in CLIENT_ATTEMPTS[:2]:
        ea = ["youtubetab:approximate_date"] + ([attempt] if attempt else [])
        p = _run(_base_args(ea) + ["--flat-playlist", "--playlist-end", str(max_videos), "-J", url], timeout=240)
        if p.returncode == 0 and p.stdout.strip():
            entries = json.loads(p.stdout).get("entries") or []
            break
        last_err = p.stderr
    if entries is None:
        raise YTError(explain_error(last_err))
    rss = _rss_dates(channel_id)
    items = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        ud = e.get("upload_date") or ""
        date = f"{ud[0:4]}-{ud[4:6]}-{ud[6:8]}" if len(ud) == 8 else ""
        if not date and e.get("timestamp"):
            date = dt.datetime.utcfromtimestamp(e["timestamp"]).date().isoformat()
        items.append({"yt_id": e["id"], "title": e.get("title") or "", "published": date or rss.get(e["id"], ""),
                      "duration": int(e.get("duration") or 0),
                      "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"})
    # complète les dates manquantes par blocs (la liste est triée du + récent au + ancien)
    i, done = 0, False
    while i < len(items) and not done:
        chunk = items[i:i + 8]
        todo = [it for it in chunk if not it["published"]]
        if todo:
            with ThreadPoolExecutor(max_workers=4) as ex:
                for it, (d, dur) in zip(todo, ex.map(lambda x: _video_date(x["yt_id"]), todo)):
                    it["published"] = d
                    it["duration"] = it["duration"] or dur
        for it in chunk:
            if it["published"] and it["published"] < cutoff:
                done = True
        i += 8
    return [it for it in items if it["published"] and it["published"] >= cutoff]


def version() -> str:
    try:
        p = _run(["yt-dlp", "--version"], timeout=20)
        return p.stdout.strip() or p.stderr.strip()
    except YTError as e:
        return f"absent ({e})"
