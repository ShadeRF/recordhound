"""SQLite storage. Artists are keyed by a normalized name (see clients.artist_key)."""
import json
import os
import sqlite3
import threading
import time

from . import config

_local = threading.local()
_gen = 0
_soft = 0      # counts partial clears, so a read that was being built during one isn't stored
_cache = {}
_cache_lock = threading.Lock()

# Cached reads that your own actions (hide, save, don't-learn) can't change: they hold what a sync wrote.
# Anything not named here is dropped on such an action, so a new cached read is safe by default.
SYNC_DATA = ("library", "library_map", "history", "recs", "recs_map", "meta")


def bump(keep=(), only=()):
    """Drop cached reads after a write. keep names the ones this write can't have changed;
    only names the exact keys it did change. With neither, everything goes."""
    global _gen, _soft
    with _cache_lock:
        if only:
            _soft += 1
            for k in only:
                _cache.pop(k, None)
        elif keep:
            _soft += 1
            for k in [k for k in _cache if k.split(":", 1)[0] not in keep]:
                del _cache[k]
        else:
            _gen += 1
            _cache.clear()


def bump_user():
    """After a change to feedback, saved artists or don't-learn: picks, library, history and the big lists stay loaded."""
    bump(keep=SYNC_DATA)


def cached(key, fn):
    hit = _cache.get(key)
    if hit is not None and hit[0] == _gen:
        return hit[1]
    stamp = (_gen, _soft)
    val = fn()
    with _cache_lock:
        if stamp == (_gen, _soft):   # nothing was written while this was being read
            _cache[key] = (_gen, val)
    return val


SCHEMA = """
CREATE TABLE IF NOT EXISTS library (          -- artists in your Plex music libraries
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mbid TEXT,
    plex_key TEXT,
    thumb TEXT,
    section TEXT,
    plays INTEGER DEFAULT 0,
    tracks INTEGER DEFAULT 0,
    albums TEXT,                              -- JSON list of owned album titles
    rating REAL,                              -- 0-10, from artist/album/track stars
    last_played INTEGER,
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS history (          -- listening outside Plex: lastfm, listenbrainz, spotify, spotify_export
    key TEXT NOT NULL,
    source TEXT NOT NULL,
    name TEXT,
    plays INTEGER DEFAULT 0,
    rank INTEGER,
    saved INTEGER DEFAULT 0,                  -- followed / saved albums on Spotify
    PRIMARY KEY (key, source)
);
CREATE TABLE IF NOT EXISTS cache (            -- Last.fm / Deezer / MusicBrainz responses
    k TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    key TEXT PRIMARY KEY,
    action TEXT NOT NULL,                     -- dismissed | liked | added | known
    name TEXT,
    at INTEGER NOT NULL,
    rating REAL                               -- 1-10, for an artist you know but don't own (rated on the Rate page)
);
CREATE TABLE IF NOT EXISTS saved (
    key TEXT PRIMARY KEY,
    name TEXT,
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS noseed (
    key TEXT PRIMARY KEY,
    name TEXT,
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recs (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                       -- new | stream (you play it elsewhere but don't own it)
    score REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recs_kind ON recs(kind, score DESC);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def path():
    return os.path.join(config.DATA_DIR, "recordhound.db")


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        c = sqlite3.connect(path(), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    if "rating" not in {r["name"] for r in c.execute("PRAGMA table_info(feedback)")}:   # added with the Rate page
        c.execute("ALTER TABLE feedback ADD COLUMN rating REAL")
    c.commit()


def commit():
    conn().commit()


# ---------------------------------------------------------------- meta

def get_meta(k, default=None):
    def load():
        r = conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else None
    v = cached("meta:" + k, load)
    return default if v is None else v


def set_meta(k, v):
    conn().execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, json.dumps(v)))
    conn().commit()
    bump(only=("meta:" + k,))   # nothing else cached is built from a meta value


# ---------------------------------------------------------------- API cache

def cache_get(k, max_age_days=None):
    r = conn().execute("SELECT json, at FROM cache WHERE k=?", (k,)).fetchone()
    if not r:
        return None
    if max_age_days is not None and time.time() - r["at"] > max_age_days * 86400:
        return None
    return json.loads(r["json"])


def cache_get_many(keys, max_age_days=None):
    out, keys = {}, list(keys)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        q = f"SELECT k, json, at FROM cache WHERE k IN ({','.join('?' * len(chunk))})"
        for r in conn().execute(q, chunk):
            if max_age_days is None or time.time() - r["at"] <= max_age_days * 86400:
                out[r["k"]] = json.loads(r["json"])
    return out


def cache_rows(keys):
    """{key: (value, saved_at)} whatever its age, for callers that want to reuse parts of an out-of-date entry."""
    out, keys = {}, list(keys)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        q = f"SELECT k, json, at FROM cache WHERE k IN ({','.join('?' * len(chunk))})"
        for r in conn().execute(q, chunk):
            out[r["k"]] = (json.loads(r["json"]), r["at"])
    return out


def cache_put(k, v):
    conn().execute("INSERT OR REPLACE INTO cache(k, json, at) VALUES (?, ?, ?)", (k, json.dumps(v), int(time.time())))


# ---------------------------------------------------------------- library & history

def set_library_rating(key, rating):
    """A rating just saved to Plex, so it counts before the next full sync reads it back."""
    conn().execute("UPDATE library SET rating=? WHERE key=?", (rating, key))
    conn().commit()
    bump()


def replace_library(rows):
    c = conn()
    c.execute("DELETE FROM library")
    c.executemany("""INSERT OR REPLACE INTO library(key, name, mbid, plex_key, thumb, section, plays, tracks, albums, rating, last_played, added_at)
                     VALUES (:key, :name, :mbid, :plex_key, :thumb, :section, :plays, :tracks, :albums, :rating, :last_played, :added_at)""",
                  [{**r, "albums": json.dumps(r.get("albums", []))} for r in rows])
    c.commit()
    bump()


def library():
    def load():
        out = []
        for r in conn().execute("SELECT * FROM library"):
            d = dict(r)
            d["albums"] = json.loads(d["albums"] or "[]")
            out.append(d)
        return out
    return cached("library", load)


def library_map():
    return cached("library_map", lambda: {r["key"]: r for r in library()})


def replace_history(source, rows):
    c = conn()
    c.execute("DELETE FROM history WHERE source=?", (source,))
    c.executemany("INSERT OR REPLACE INTO history(key, source, name, plays, rank, saved) VALUES (?, ?, ?, ?, ?, ?)",
                  [(r["key"], source, r["name"], r.get("plays", 0), r.get("rank"), int(bool(r.get("saved")))) for r in rows])
    c.commit()
    bump()


def history():
    """{key: {"name", "sources": {source: {...}}, "plays": total}}"""
    def load():
        out = {}
        for r in conn().execute("SELECT * FROM history"):
            h = out.setdefault(r["key"], {"key": r["key"], "name": r["name"], "sources": {}, "plays": 0})
            h["sources"][r["source"]] = {"plays": r["plays"], "rank": r["rank"], "saved": bool(r["saved"])}
            h["plays"] += r["plays"] or 0
        return out
    return cached("history", load)


def history_counts():
    return {r["source"]: r["n"] for r in conn().execute("SELECT source, COUNT(*) n FROM history GROUP BY source")}


# ---------------------------------------------------------------- feedback, saved, noseed

def _keyed(table):
    return {r["key"]: dict(r) for r in conn().execute(f"SELECT * FROM {table} ORDER BY at DESC")}


def feedback():
    return cached("feedback", lambda: _keyed("feedback"))


def set_feedback(key, action, name, rating=None):
    conn().execute("INSERT OR REPLACE INTO feedback(key, action, name, at, rating) VALUES (?, ?, ?, ?, ?)",
                   (key, action, name, int(time.time()), rating))
    conn().commit()
    bump_user()


def clear_feedback(key=None):
    if key is None:
        conn().execute("DELETE FROM feedback")
    else:
        conn().execute("DELETE FROM feedback WHERE key=?", (key,))
    conn().commit()
    bump_user()


def saved():
    return cached("saved", lambda: _keyed("saved"))


def set_saved(key, name, on=True):
    if on:
        conn().execute("INSERT OR REPLACE INTO saved(key, name, at) VALUES (?, ?, ?)", (key, name, int(time.time())))
    else:
        conn().execute("DELETE FROM saved WHERE key=?", (key,))
    conn().commit()
    bump_user()


def noseed():
    return cached("noseed", lambda: _keyed("noseed"))


def set_noseed(key, name, on=True):
    if on:
        conn().execute("INSERT OR REPLACE INTO noseed(key, name, at) VALUES (?, ?, ?)", (key, name, int(time.time())))
    else:
        conn().execute("DELETE FROM noseed WHERE key=?", (key,))
    conn().commit()
    bump_user()


# ---------------------------------------------------------------- picks

def replace_recs(kind, rows):
    c = conn()
    c.execute("DELETE FROM recs WHERE kind=?", (kind,))
    c.executemany("INSERT OR REPLACE INTO recs(key, kind, score, data) VALUES (?, ?, ?, ?)",
                  [(r["key"], kind, r["score"], json.dumps(r)) for r in rows])
    c.commit()
    bump()


def recs(kind="new"):
    return cached("recs:" + kind, lambda: [json.loads(r["data"]) for r in
                                           conn().execute("SELECT data FROM recs WHERE kind=? ORDER BY score DESC", (kind,))])


def recs_map():
    return cached("recs_map", lambda: {r["key"]: r for k in ("new", "stream") for r in recs(k)})


def stats():
    c = conn()
    one = lambda q: c.execute(q).fetchone()[0]
    return {"artists": one("SELECT COUNT(*) FROM library"), "cached": one("SELECT COUNT(*) FROM cache"),
            "feedback": one("SELECT COUNT(*) FROM feedback"), "history": history_counts(),
            "db_bytes": os.path.getsize(path()) if os.path.exists(path()) else 0}


def close():
    """Close this thread's connection (sync threads call this when they finish)."""
    c = getattr(_local, "c", None)
    if c is not None:
        c.close()
        _local.c = None
