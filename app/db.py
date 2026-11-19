"""Couche SQLite minimaliste (stdlib) pour YT Cortex."""
import json
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "cortex.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  yt_id TEXT UNIQUE NOT NULL,
  url TEXT NOT NULL,
  title TEXT DEFAULT '',
  channel TEXT DEFAULT '',
  channel_id TEXT DEFAULT '',
  published TEXT DEFAULT '',
  duration INTEGER DEFAULT 0,
  thumbnail TEXT DEFAULT '',
  description TEXT DEFAULT '',
  chapters_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'queued',      -- queued|fetching|transcribing|transcribed|summarizing|done|error
  step TEXT DEFAULT '',
  source TEXT DEFAULT '',            -- subs|whisper|manual
  lang TEXT DEFAULT '',
  transcript TEXT DEFAULT '',
  summary_md TEXT DEFAULT '',
  links_json TEXT DEFAULT '[]',
  error TEXT DEFAULT '',
  attempts INTEGER DEFAULT 0,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  yt_channel_id TEXT UNIQUE NOT NULL,
  name TEXT DEFAULT '',
  url TEXT DEFAULT '',
  added_at REAL,
  last_sync REAL DEFAULT 0,
  sync_status TEXT DEFAULT '',
  sync_error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS channel_videos (
  yt_id TEXT NOT NULL,
  channel_id INTEGER NOT NULL,
  title TEXT DEFAULT '',
  published TEXT DEFAULT '',
  duration INTEGER DEFAULT 0,
  thumbnail TEXT DEFAULT '',
  PRIMARY KEY (yt_id, channel_id)
);
CREATE TABLE IF NOT EXISTS skills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  video_ids_json TEXT DEFAULT '[]',
  files_json TEXT DEFAULT '{}',      -- {"SKILL.md": "...", "references/x.md": "..."}
  status TEXT DEFAULT 'queued',      -- queued|generating|done|error
  error TEXT DEFAULT '',
  created_at REAL,
  pushed_at REAL DEFAULT 0,
  push_path TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def q(sql, args=()):
    return conn().execute(sql, args).fetchall()


def one(sql, args=()):
    return conn().execute(sql, args).fetchone()


def run(sql, args=()):
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    return cur


# ---- settings -------------------------------------------------------------
def get_setting(key, default=""):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    if row and row["value"] is not None:
        return row["value"]
    return os.environ.get(key.upper(), default)


def set_setting(key, value):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


# ---- videos ---------------------------------------------------------------
def update_video(vid, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE videos SET {cols} WHERE id=?", (*fields.values(), vid))


def jloads(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default
