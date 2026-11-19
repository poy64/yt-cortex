import base64, io, json, os, sys, zipfile
import pytest

os.environ["DATA_DIR"] = "/tmp/ytc_test_data"
os.environ["APP_USER"] = "iban"
os.environ["APP_PASSWORD"] = "pw"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import shutil
shutil.rmtree("/tmp/ytc_test_data", ignore_errors=True)
from fastapi.testclient import TestClient
from app import db, main, worker, youtube, llm, skills

AUTH = {"Authorization": "Basic " + base64.b64encode(b"iban:pw").decode()}

VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000 align:start position:0%
bonjour à tous<00:00:01.500><c> et bienvenue</c>

00:00:03.000 --> 00:00:05.000
bonjour à tous et bienvenue
aujourd'hui on parle de n8n

00:00:50.000 --> 00:00:52.000
voici l'outil &amp; ses limites
"""

@pytest.fixture(scope="module")
def client():
    db.init()
    with TestClient(main.app) as c:
        yield c

def test_parse_ids():
    assert youtube.parse_video_id("https://youtu.be/dQw4w9WgXcQ?t=3") == "dQw4w9WgXcQ"
    assert youtube.parse_video_id("https://www.youtube.com/watch?feature=x&v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.parse_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.parse_video_id("nope") is None
    assert youtube.parse_urls("https://youtu.be/dQw4w9WgXcQ\nhttps://youtu.be/dQw4w9WgXcQ\n\nabcdefghijk") == ["dQw4w9WgXcQ", "abcdefghijk"]

def test_vtt_dedup_and_blocks():
    segs = youtube.parse_vtt(VTT)
    texts = [t for _, t in segs]
    assert texts == ["bonjour à tous et bienvenue", "aujourd'hui on parle de n8n", "voici l'outil & ses limites"]
    out = youtube.segments_to_text(segs)
    assert out.startswith("[00:01] bonjour") and "[00:50] voici" in out

def test_links():
    ls = youtube.extract_links("Outil : https://n8n.io.\nSuis-moi https://x.com/a, ok", "")
    assert [l["url"] for l in ls] == ["https://n8n.io", "https://x.com/a"]
    assert ls[0]["label"].startswith("Outil")

def test_skill_md_normalize():
    md, name, desc = skills.normalize_skill_md("```markdown\n---\nname: Mon Skill N8N!\ndescription: Utiliser <ici> quand\n---\n# T\n```", "x", "fb")
    assert name == "mon-skill-n8n" and "<" not in desc and md.startswith("---\nname: mon-skill-n8n")
    md2, n2, _ = skills.normalize_skill_md("# sans frontmatter", "Titre Vidéo", "fb desc")
    assert n2 == "titre-video" and "description:" in md2

def test_auth_and_csrf(client):
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200
    assert client.get("/", headers=AUTH).status_code == 200
    r = client.post("/videos/add", data={"urls": "x"}, headers={**AUTH, "Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403

def test_full_pipeline(client, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_metadata", lambda url: {"id": "dQw4w9WgXcQ", "title": "Test n8n", "channel": "Chan", "channel_id": "UCx", "upload_date": "20261101", "duration": 600,
        "description": "Site : https://n8n.io\nChapitres", "chapters": [{"start_time": 0, "title": "Intro"}]})
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda yid, langs=("fr", "en"): ("[00:01] bonjour", "fr", "auto"))
    monkeypatch.setattr(llm, "api_key", lambda: "k")
    monkeypatch.setattr(llm, "summarize", lambda v: "# Fiche\n\nVoir [n8n](https://n8n.io) <script>alert(1)</script>")
    monkeypatch.setattr(llm, "resolve_tool_links", lambda md, urls: [{"name": "Zapier", "url": "https://zapier.com"}])
    worker.start = lambda: None
    ids = worker.add_videos(["dQw4w9WgXcQ"])
    while not worker.Q.empty():
        worker.Q.get_nowait()
    worker.process_video(ids[0])
    v = db.one("SELECT * FROM videos WHERE id=?", (ids[0],))
    assert v["status"] == "done", v["error"]
    assert v["published"] == "2026-11-01" and v["source"] == "subs-auto"
    assert "Liens de la description" in v["summary_md"] and "zapier.com" in v["summary_md"]
    page = client.get(f"/video/{ids[0]}", headers=AUTH)
    assert page.status_code == 200 and "<script>alert" not in page.text and "&lt;script&gt;" in page.text
    assert 'target="_blank"' in page.text
    assert client.get("/", headers=AUTH).text.count("vcard") >= 1
    assert client.get("/partials/videos", headers=AUTH).status_code == 200

    # skill
    monkeypatch.setattr(llm, "make_skill_md", lambda hint, vids, names: "---\nname: n8n-expert\ndescription: Utiliser pour n8n\n---\n# n8n")
    r = client.post("/skills/create", data={"video_ids": [str(ids[0])], "name": ""}, headers=AUTH, follow_redirects=False)
    assert r.status_code == 303
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    while not worker.Q2.empty():
        worker.Q2.get_nowait()
    skills.build_skill(sid)
    s = db.one("SELECT * FROM skills WHERE id=?", (sid,))
    assert s["status"] == "done" and s["name"] == "n8n-expert"
    z = client.get(f"/skills/{sid}/download", headers=AUTH)
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    names = zf.namelist()
    assert "n8n-expert/SKILL.md" in names and any(n.startswith("n8n-expert/references/") for n in names)
    assert client.get(f"/skills/{sid}", headers=AUTH).status_code == 200
    assert client.get("/skills", headers=AUTH).status_code == 200

def test_skill_requires_done(client):
    r = client.post("/skills/create", data={"video_ids": ["999"]}, headers=AUTH)
    assert r.status_code == 400

def test_settings_and_pages(client):
    assert client.post("/settings", data={"anthropic_api_key": "sk-ant-abcdefgh1234", "llm_model": ""}, headers=AUTH, follow_redirects=False).status_code == 303
    page = client.get("/settings", headers=AUTH).text
    assert "sk-ant-abcdefgh1234" not in page and "1234" in page
    client.post("/settings", data={"llm_model": "m"}, headers=AUTH)
    assert db.get_setting("anthropic_api_key") == "sk-ant-abcdefgh1234"  # vide = conservé
    for p in ("/channels", "/skills", "/diag"):
        assert client.get(p, headers=AUTH).status_code == 200
    assert client.post("/settings/cookies", data={"cookies": "pas de tab"}, headers=AUTH, follow_redirects=False).status_code == 303

def test_channel_flow(client, monkeypatch):
    monkeypatch.setattr(youtube, "resolve_channel", lambda t: {"channel_id": "UC" + "a" * 22, "name": "Ma chaîne", "url": "u"})
    monkeypatch.setattr(youtube, "list_channel_recent", lambda cid, days=60, max_videos=150: [
        {"yt_id": "abcdefghijk", "title": "V1", "published": "2026-11-10", "duration": 100, "thumbnail": "t"}])
    r = client.post("/channels/add", data={"channel": "@x"}, headers=AUTH, follow_redirects=False)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    while not worker.Q2.empty():
        worker.Q2.get_nowait()
    worker.sync_channel(cid)
    page = client.get(f"/channels/{cid}", headers=AUTH).text
    assert "V1" in page and "abcdefghijk" in page
    while not worker.Q.empty():
        worker.Q.get_nowait()
    client.post(f"/channels/{cid}/process", data={"yt_ids": ["abcdefghijk", "zzzzzzzzzzz"]}, headers=AUTH)
    row = db.one("SELECT * FROM videos WHERE yt_id='abcdefghijk'")
    assert row and row["title"] == "V1" and row["published"] == "2026-11-10"
    assert db.one("SELECT 1 FROM videos WHERE yt_id='zzzzzzzzzzz'") is None

def test_vtt_without_hours():
    assert youtube.parse_vtt("WEBVTT\n\n00:01.000 --> 00:03.000\nsalut\n") == [(1.0, "salut")]

def test_yaml_backslash():
    md, _, desc = skills.normalize_skill_md("---\nname: a\ndescription: C:\\data\\new\n---\nx", "a", "fb")
    assert "\\" not in md.split("---")[1]

def test_channel_host_restricted():
    import pytest
    with pytest.raises(youtube.YTError):
        youtube.resolve_channel("http://169.254.169.254/x")
