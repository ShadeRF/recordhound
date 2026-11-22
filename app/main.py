import json
import logging
import os
import secrets
import urllib.parse
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import re

import requests
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, auth, security, config, db, engine
from .clients import (UA, APIError, Deezer, LastFM, LASTFM_PRO_NOTE, Lidarr, Plex, Spotify, SpotifyNoDevice, album_key, owned_track, special_release, artist_key, artist_source, credit_keys, song_key, song_keys, tracks_owned as _tracks_owned, owns_every_track as _owns_every_track,
                      listenbrainz_top_artists, parse_spotify_export, parse_youtube_music, spotify_auth_url, spotify_exchange, spotify_pasted, spotify_redirect,
                      SPOTIFY_HELPER, SPOTIFY_LOOPBACK)
from .sync import Syncer, friendly_error

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
security.install_log_redaction()
log = logging.getLogger("recordhound")

STATIC = os.path.join(os.path.dirname(__file__), "static")
@asynccontextmanager
async def lifespan(_app):
    security.install_log_redaction()   # uvicorn's own log handlers exist by now
    startup()
    yield


app = FastAPI(title="Recordhound", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.middleware("http")(auth.middleware)
app.middleware("http")(security.middleware)   # outermost: runs before sign-in
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.exception_handler(HTTPException)
async def _http_error(_request, exc):
    """Error messages often quote what a service said, which can include the address we called: strip keys from them."""
    detail = security.redact(exc.detail) if isinstance(exc.detail, str) else exc.detail
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(APIError)
@app.exception_handler(requests.exceptions.RequestException)
async def _service_error(_request, exc):
    """A service (Plex, TMDB and the rest) failed somewhere a route didn't expect: say so plainly instead of a bare server error."""
    return JSONResponse({"detail": friendly_error(exc)}, status_code=502)


@app.middleware("http")
async def cache_headers(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/static/") and request.url.query:
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif p.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=86400"
    elif p.startswith(("/api/art/", "/api/cover/")):
        if resp.status_code in (200, 302):
            resp.headers.setdefault("Cache-Control", "public, max-age=604800")
        else:   # a picture that failed to load once should be tried again, not remembered as broken for a week
            resp.headers["Cache-Control"] = "no-store"
    elif p == "/" or p.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp



def _fast(data):
    """Send data that is already plain JSON as it is. Returning it normally makes FastAPI walk every item of a big list first."""
    return Response(json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False), media_type="application/json")


syncer = Syncer()
_rescore_timer = None
_rescore_lock = threading.Lock()


def schedule_rescore(delay=45):
    """Re-score shortly after the last hide/like/save, so there's no button to remember."""
    global _rescore_timer

    def fire():
        if syncer.state["running"]:
            schedule_rescore(30)
        elif config.configured() and db.library():
            syncer.start(full=False)
    with _rescore_lock:
        if _rescore_timer:
            _rescore_timer.cancel()
        _rescore_timer = threading.Timer(delay, fire)
        _rescore_timer.daemon = True
        _rescore_timer.start()


def _scheduler():
    last_day = None
    while True:
        now = datetime.now()
        if config.SYNC_HOUR >= 0 and now.hour == config.SYNC_HOUR and last_day != now.date() and config.configured():
            if syncer.start(full=True):   # busy (a re-score is running): try again next minute
                last_day = now.date()
        time.sleep(60)


def _keep_old_spotify_redirect():
    """Spotify set up before one-click sign-in: keep the address that Spotify app knows, so Reconnect still works."""
    if config.SPOTIFY_CLIENT_ID and config.sources().get("SPOTIFY_REDIRECT_URI") == "default":
        config.save({"SPOTIFY_REDIRECT_URI": SPOTIFY_LOOPBACK})


def startup():
    db.init()
    _keep_old_spotify_redirect()
    if config.configured() and not db.get_meta("last_sync"):
        syncer.start(full=True)
    threading.Thread(target=_scheduler, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(STATIC, "favicon-32.png"), media_type="image/png")


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__}


# ======================================================================= status & sync

def _lidarr_on():
    return bool(config.LIDARR_URL and config.LIDARR_API_KEY)


@app.get("/api/status")
def status():
    return {
        **syncer.state, "last_sync": db.get_meta("last_sync"), "version": __version__,
        "artists": len(db.library()), "picks": len(db.recs("new")), "unplayed": len(db.get_meta("unplayed", [])), "streamed": len(db.recs("stream")),
        "configured": config.configured(), "artist_source": _source_name(), "lidarr": _lidarr_on(), "lidarr_url": config.LIDARR_URL,
        "plex_url": config.PLEX_URL, "plex_machine": db.get_meta("plex_machine"),
        "spotify": bool(config.SPOTIFY_REFRESH_TOKEN), "auth": auth.enabled(), "user": config.AUTH_USERNAME,
        "spotify_play": bool(config.SPOTIFY_REFRESH_TOKEN and config.SPOTIFY_PLAY and db.get_meta("spotify_can_play") is not False),
        "history": db.history_counts(),
    }


@app.post("/api/sync")
def sync(full: bool = True):
    if not config.configured():
        raise HTTPException(400, "Finish setup first: connect Plex in Settings.")
    return {"started": syncer.start(full=full)}


# ======================================================================= picks

def _flags(rows):
    fb, sv, lib = db.feedback(), db.saved(), db.library_map()
    for r in rows:
        f = fb.get(r["key"])
        r["feedback"] = f["action"] if f else None
        r["saved"] = r["key"] in sv
        r["owned"] = r["key"] in lib
    return rows


def _visible(kind):
    fb = db.feedback()
    return [r for r in db.recs(kind) if not (fb.get(r["key"]) and fb[r["key"]]["action"] in ("dismissed", "added", "known"))]


def _reach(r):
    """Audience size on Last.fm's scale. Without Last.fm we only have Deezer fans, which run about a
    fifth of Last.fm listeners for the same artist, so the deep/mid/known cut-offs still roughly hold."""
    if r.get("listeners") is not None:
        return r["listeners"] or 0
    return (r.get("fans") or 0) * 5


def _filter(rows, q="", tags="", exclude_tags="", tag_mode="any", seed="", pop="", sort="score"):
    inc = [engine.tag_key(t) for t in tags.split(",") if t.strip()]   # "singer/songwriter" finds "singer-songwriter" too
    exc = {engine.tag_key(t) for t in exclude_tags.split(",") if t.strip()}
    out = []
    for r in rows:
        rt = {engine.tag_key(t) for t in r.get("tags") or []}
        if inc and not (set(inc) <= rt if tag_mode == "all" else rt & set(inc)):
            continue
        if exc & rt:
            continue
        if q and q.lower() not in r["name"].lower():
            continue
        if seed and not any(b["key"] == seed for b in r.get("because") or []):
            continue
        n = _reach(r)
        if pop == "deep" and n > 150_000 or pop == "known" and n < 500_000 or pop == "mid" and not 150_000 <= n <= 1_500_000:
            continue
        out.append(r)
    if sort == "popular":
        out.sort(key=lambda r: -_reach(r))
    elif sort == "obscure":
        out.sort(key=lambda r: _reach(r))
    elif sort == "name":
        out.sort(key=lambda r: r["name"].lower())
    return out


@app.get("/api/recs")
def recs(kind: str = "new", offset: int = 0, limit: int = 60, q: str = "", tags: str = "", exclude_tags: str = "",
         tag_mode: str = "any", seed: str = "", pop: str = "", sort: str = "score", fresh: int = 0):
    rows = _filter(_visible(kind), q, tags, exclude_tags, tag_mode, seed, pop, sort)
    if fresh:   # "New for you this week"
        first, since = db.get_meta("first_seen", {}), time.time() - 7 * 86400
        rows = [r for r in rows if first.get(r["key"], 0) >= since]
    return _fast({"total": len(rows), "items": _flags([dict(r) for r in rows[offset:offset + limit]])})


@app.get("/api/tags")
def tag_facets(kind: str = "new"):
    from collections import Counter
    c, spell = Counter(), {}
    for r in _visible(kind):
        for t in {engine.tag_key(t): t for t in (r.get("tags") or [])[:6] if t not in engine.JUNK_TAGS}.items():
            c[t[0]] += 1
            spell.setdefault(t[0], Counter())[t[1]] += 1
    # spellings of one tag count together, shown the way most artists have it
    return {"tags": [{"tag": spell[k].most_common(1)[0][0], "n": n} for k, n in c.most_common(80) if n >= 3], "taste": db.get_meta("taste", [])}


@app.get("/api/home")
def home():
    new, stream = _visible("new"), _visible("stream")
    if not new and not stream:
        return _fast({"rows": []})
    rows = [{"id": "top", "title": "Top picks for you", "items": engine.varied(new, 24), "see_all": {}}]
    # this week: picks that appeared in the last seven days, releases out this week, and what's announced
    now, week = time.time(), 7 * 86400
    first = db.get_meta("first_seen", {})
    fresh = [r for r in new if first.get(r["key"], 0) >= now - week]
    today = datetime.now().date().isoformat()
    week_ago = datetime.fromtimestamp(now - week).date().isoformat()
    rel_all = (db.get_meta("releases") or {}).get("items", [])
    digest = {"new_picks": len(fresh), "releases": sum(1 for a in rel_all if week_ago <= (a.get("date") or "") <= today),
              "upcoming": sum(1 for a in rel_all if (a.get("date") or "") > today)}
    if len(fresh) >= 4:
        rows.append({"id": "fresh", "title": "New for you this week", "subtitle": "Picks that showed up in the last seven days",
                     "items": fresh[:24], "see_all": {"fresh": 1}})
    sv = db.saved()
    saved = [r for r in (db.recs_map().get(k) for k in sv) if r]
    if saved:
        rows.append({"id": "saved", "title": "Saved for later", "items": saved[:24], "see_all": {"view": "saved"}})
    rel = [a for a in (db.get_meta("releases") or {}).get("items", [])]
    if rel:
        rel = sorted(rel, key=lambda a: not a.get("cover"))   # albums with artwork first; each group keeps its date order
        rows.append({"id": "releases", "type": "albums", "title": "New from artists you love", "items": rel[:24], "see_all": {"view": "releases"}})
    # Because you play X: your most-played artists that point at several picks
    seeds = db.get_meta("seeds", {})
    index = {}
    for r in new:
        for b in r.get("because") or []:
            index.setdefault(b["key"], []).append(r)
    made = 0
    for k, s in seeds.items():
        if made >= 4:
            break
        items = index.get(k, [])
        if len(items) >= 6 and s["kind"] in ("played", "rated"):
            verb = "you play" if s["kind"] == "played" else "you love"
            rows.append({"id": f"seed:{k}", "title": f"Because {verb} {s['name']}", "items": items[:24],
                         "see_all": {"seed": k, "seed_label": f"Because {verb} {s['name']}"}})
            made += 1
    if stream:
        rows.append({"id": "stream", "title": "You stream these, but don't own them", "subtitle": "From your listening outside Plex",
                     "items": stream[:24], "see_all": {"view": "stream"}})
    gaps = _gap_groups()
    gap_albums = []
    for rnd in range(3):
        for g in gaps:
            if rnd < len(g["albums"]):
                gap_albums.append({**g["albums"][rnd], "gap": f"{g['owned']} of {g['total']} albums"})
    if len(gap_albums) >= 4:
        rows.append({"id": "gaps", "type": "albums", "title": "Albums you're missing", "subtitle": "From artists you already collect",
                     "items": gap_albums[:24], "see_all": {"view": "gaps"}})
    unplayed = _unplayed()
    if len(unplayed) >= 4:
        rows.append({"id": "unplayed", "type": "albums", "title": "In your library, never played", "subtitle": "Albums you own, ranked for you",
                     "items": [{**a, "gap": a["why"]} for a in unplayed[:24]], "see_all": {"view": "library"}})
    tag_rows = 0
    for t in db.get_meta("taste", [])[:8]:
        items = [r for r in new if t["tag"] in (r.get("tags") or [])[:5]]
        if len(items) >= 8 and tag_rows < 3:
            tag_rows += 1
            rows.append({"id": f"tag:{t['tag']}", "title": f"More {t['tag']}", "items": items[:24], "see_all": {"tags": t["tag"]}})
    deep = [r for r in new if _reach(r) < 100_000]
    if len(deep) >= 8:
        rows.append({"id": "deep", "title": "Deep cuts", "subtitle": "Strong matches with small audiences", "items": deep[:24],
                     "see_all": {"pop": "deep"}})
    for row in rows:
        if row.get("type") != "albums":
            row["items"] = _flags([dict(r) for r in row["items"]])
    return _fast({"rows": rows, "digest": digest})


def _unplayed():
    noseed = db.noseed()
    return [{**a, "cover": f"/api/cover/{a['rk']}", "date": str(a["year"]) if a.get("year") else ""}
            for a in db.get_meta("unplayed", []) if a.get("artist_key") not in noseed and (config.COMP_LIBRARY or not a.get("compilation"))]


@app.get("/api/library")
def your_library(offset: int = 0, limit: int = 120, q: str = ""):
    """Albums you own but have never played, best first."""
    rows = _unplayed()
    if q:
        ql = q.lower()
        rows = [a for a in rows if ql in (a.get("title") or "").lower() or ql in (a.get("artist") or "").lower()]
    return _fast({"total": len(rows), "all": len(_unplayed()), "items": rows[offset:offset + limit], "playlist": config.PLEX_PLAYLIST})


_album_thumbs = {"gen": None, "map": {}}


@app.get("/api/cover/{rk}")
def cover(rk: str):
    """Album art from Plex, fetched with the server's token so the browser never sees it."""
    if not rk.isdigit():
        raise HTTPException(404, "no cover")
    albums = db.get_meta("plex_albums", [])
    if _album_thumbs["gen"] is not albums:
        _album_thumbs.update(gen=albums, map={str(a.get("rk")): a.get("thumb") for a in albums})
    path = _album_thumbs["map"].get(rk)
    if not path or not config.PLEX_URL or not config.PLEX_TOKEN:
        raise HTTPException(404, "no cover")
    try:
        data, ctype = Plex(config.PLEX_URL, config.PLEX_TOKEN).thumb(path)
    except Exception:
        raise HTTPException(404, "no cover")
    return Response(data, media_type=ctype if ctype.startswith("image/") else "image/jpeg")


class PlaylistBody(BaseModel):
    count: int = 50


@app.post("/api/plex/playlist")
def make_playlist(body: PlaylistBody):
    """Put your top unplayed albums in a Plex playlist (replacing the one made last time)."""
    rks = [a["rk"] for a in _unplayed()[:max(1, min(body.count, 250))]]
    if not rks:
        raise HTTPException(400, "No unplayed albums yet. Run a sync first.")
    try:
        plex = Plex(config.PLEX_URL, config.PLEX_TOKEN)
        machine = db.get_meta("plex_machine") or plex.machine_id()
        # Only ever remove the playlist Recordhound made last time, never one that just shares the name.
        mine = db.get_meta("plex_playlist_rk")
        if mine:
            old = [mine]
        elif config.PLEX_PLAYLIST == config.FIELDS["PLEX_PLAYLIST"][1]:   # made before the key was remembered
            old = plex.playlists_titled(config.PLEX_PLAYLIST)
        else:
            old = []
        new = plex.replace_playlist(config.PLEX_PLAYLIST, rks, machine, old)
        if new:
            db.set_meta("plex_playlist_rk", new)
    except Exception as e:
        raise HTTPException(502, f"Plex couldn't make the playlist: {friendly_error(e)}")
    return {"ok": True, "title": config.PLEX_PLAYLIST, "albums": len(rks)}


# ======================================================================= Liked, not owned (Spotify Liked Songs vs Plex)

LIKED_PLAYLIST = "Recordhound: Liked on Spotify"


_owned_rk = owned_track


def _release(t, links):
    """(id, linked album or None) for a liked song: the album a single ended up on, when there is one."""
    link = ((links or {}).get(t.get("id")) or {}).get("album")
    if link:
        return f"dz:{link['id']}", link
    return t.get("album_id") or f"{t.get('album')}|{t.get('album_artist')}", None


def _liked():
    """(liked songs, Plex songs). Either is None until a sync has read it."""
    return db.get_meta("spotify_liked"), db.get_meta("plex_songs")


@app.get("/api/liked")
def liked():
    tracks, songs = _liked()
    out = {"connected": bool(config.SPOTIFY_REFRESH_TOKEN), "ready": tracks is not None and songs is not None,
           "total": 0, "owned": 0, "albums": [], "hidden": 0, "playlist": LIKED_PLAYLIST}
    if not out["ready"]:
        return out
    hidden, added = set(db.get_meta("liked_hidden", [])), set(db.get_meta("liked_lidarr", []))
    links = db.get_meta("single_links") or {}
    lidarr = set(db.get_meta("lidarr_keys", []))
    groups = {}
    for t in tracks:
        if _owned_rk(t, songs):
            out["owned"] += 1
            continue
        gid, link = _release(t, links)
        if gid in hidden or t.get("id") in hidden or (link and t.get("album_id") in hidden):   # a whole release, or just this song
            out["hidden"] += 1
            continue
        artist = t.get("album_artist") or (t.get("artists") or [""])[0]
        g = groups.setdefault(gid, {"id": gid, "title": link["title"] if link else t.get("album"), "artist": artist, "artist_key": artist_key(artist),
                                    "cover": t.get("cover") if not link else (link.get("cover") or t.get("cover")),
                                    "year": (link or t).get("year"), "type": link["type"] if link else t.get("album_type"),
                                    "linked": bool(link),
                                    "added_at": t.get("added_at"), "songs": [], "tracks": [], "requested": gid in added,
                                    "artist_in_lidarr": artist_key(artist) in lidarr})
        g["songs"].append(t.get("title"))
        g["tracks"].append({"id": t.get("id"), "title": t.get("title"), "added_at": t.get("added_at"), "single": t.get("album") if link else None})
    out["total"] = len(tracks)
    out["albums"] = sorted(groups.values(), key=lambda g: g["added_at"] or "", reverse=True)   # newest like first
    return _fast(out)


class LikedHide(BaseModel):
    id: str
    on: bool = True


@app.post("/api/liked/hide")
def liked_hide(body: LikedHide):
    hidden = set(db.get_meta("liked_hidden", []))
    hidden.add(body.id) if body.on else hidden.discard(body.id)
    db.set_meta("liked_hidden", sorted(hidden))
    return {"ok": True}


@app.post("/api/liked/unhide-all")
def liked_unhide_all():
    db.set_meta("liked_hidden", [])
    return {"ok": True}


class LikedLidarr(BaseModel):
    id: str
    quality_id: int
    metadata_id: int
    root: str


@app.post("/api/liked/lidarr")
def liked_lidarr(body: LikedLidarr):
    tracks, _ = _liked()
    links = db.get_meta("single_links") or {}
    t = next((t for t in tracks or [] if _release(t, links)[0] == body.id), None)
    if not t:
        raise HTTPException(404, "That album isn't in your liked songs any more. Refresh the page.")
    artist = t.get("album_artist") or (t.get("artists") or [""])[0]
    link = _release(t, links)[1]
    try:
        title = Lidarr(config.LIDARR_URL, config.LIDARR_API_KEY).add_album(artist, link["title"] if link else t.get("album"),
                                                                            body.quality_id, body.metadata_id, body.root)
    except Exception as e:
        raise HTTPException(502, friendly_error(e))
    added = set(db.get_meta("liked_lidarr", []))
    added.add(body.id)
    db.set_meta("liked_lidarr", sorted(added))
    last = db.get_meta("lidarr_last", {})
    db.set_meta("lidarr_last", {**last, "quality_id": body.quality_id, "metadata_id": body.metadata_id, "root": body.root})
    return {"ok": True, "title": title}


@app.post("/api/plex/liked-playlist")
def liked_playlist():
    """Your Spotify Liked Songs that you own, newest like first, as a Plex playlist (replacing the one made last time)."""
    tracks, songs = _liked()
    rks = list(dict.fromkeys(rk for rk in (_owned_rk(t, songs) for t in tracks or []) if rk)) if songs else []
    if not rks:
        raise HTTPException(400, "None of your liked songs are in Plex yet." if songs else "This works after your next sync.")
    try:
        plex = Plex(config.PLEX_URL, config.PLEX_TOKEN)
        machine = db.get_meta("plex_machine") or plex.machine_id()
        mine = db.get_meta("plex_liked_playlist_rk")   # only ever replace the playlist made here
        new = plex.replace_playlist(LIKED_PLAYLIST, rks, machine, [mine] if mine else [])
        if new:
            db.set_meta("plex_liked_playlist_rk", new)
    except Exception as e:
        raise HTTPException(502, f"Plex couldn't make the playlist: {friendly_error(e)}")
    return {"ok": True, "title": LIKED_PLAYLIST, "songs": len(rks)}


def _gap_groups():
    """Albums you're missing, by artist, as your settings want them: studio albums, plus EPs and remix, instrumental,
    live or acoustic releases if you've asked for those. Older syncs only kept studio albums, which still works."""
    out, lib = [], db.library_map()
    for g in (db.get_meta("gaps") or {}).get("groups", []):
        owned = (lib.get(g["key"]) or {}).get("albums", [])   # checked here too, so older syncs get the newer rules
        special = lambda a: a.get("special") or special_release(a.get("title"), owned)   # noqa: E731
        keep = [a for a in g["albums"] if (a.get("type") != "ep" or config.GAPS_EPS) and (not special(a) or config.GAPS_SPECIAL)]
        if keep:
            out.append({**g, "albums": keep[:12], "total": g["owned"] + len(keep)})
    return out


@app.get("/api/gaps")
def gaps():
    g = db.get_meta("gaps") or {}
    return _fast({"at": g.get("at"), "groups": _gap_groups(), "eps": config.GAPS_EPS, "special": config.GAPS_SPECIAL})


@app.get("/api/releases")
def releases():
    r = db.get_meta("releases") or {}
    return _fast({"at": r.get("at"), "items": r.get("items", [])})


# ======================================================================= one artist

_dz = Deezer()


def _artist_data():
    """(client, cache prefix) for artist lookups: Last.fm with a working key, free sources otherwise."""
    client, prefix, _ = artist_source(config.LASTFM_API_KEY, _dz)
    return client, prefix


def _source_name():
    return db.get_meta("artist_source") or ("lastfm" if config.LASTFM_API_KEY else "open")


def _deezer(key, name, max_age=14):
    hit = db.cache_get("dz:" + key, max_age)
    if hit is None:
        try:
            hit = _dz.find_artist(name)
            if not hit.get("missing"):
                hit["albums"] = _dz.albums(hit["id"])
        except Exception as e:
            log.warning("Deezer lookup failed for %s: %s", name, e)
            return {}
        db.cache_put("dz:" + key, hit)
        db.commit()
    return hit


@app.get("/api/album/{album_id}/tracks")
def album_tracks(album_id: int, name: str = "", album: str = ""):
    """A Deezer album's tracks, each marked if you own it in Plex or liked it on Spotify."""
    tracks = db.cache_get(f"dzalb:{album_id}", 30)
    if tracks is None:
        try:
            tracks = _dz.album_tracks(album_id)
        except Exception as e:
            raise HTTPException(502, f"Couldn't get the track list from Deezer: {friendly_error(e)}")
        db.cache_put(f"dzalb:{album_id}", tracks)
        db.commit()
    songs = db.get_meta("plex_songs")
    liked = {f"{ak}|{sk}" for t in db.get_meta("spotify_liked") or [] for sk in song_keys(t.get("title"))
             for credit in t.get("artists") or [] for ak in credit_keys(credit)}
    out = []
    for t, have in zip(tracks, _tracks_owned(tracks, name, album, songs)):
        keys = {f"{ak}|{sk}" for sk in song_keys(t.get("title")) for ak in credit_keys(name) | credit_keys(t.get("artist") or "")}
        out.append({**t, "owned": have, "liked": bool(keys & liked)})
    return {"tracks": out, "checked_plex": songs is not None, "all_owned": len(out) >= 2 and all(t["owned"] for t in out)}


@app.get("/api/art/{key}")
def art(key: str, name: str = ""):
    """Artist photo from Deezer, looked up once and cached."""
    hit = db.cache_get("dzart:" + key, 60)
    if hit is None:
        nm = name or (db.recs_map().get(key) or db.library_map().get(key) or {}).get("name") or key
        try:
            a = _dz.find_artist(nm)
        except Exception:
            raise HTTPException(404, "no picture")
        hit = {"thumb": a.get("thumb"), "picture": a.get("picture")}
        db.cache_put("dzart:" + key, hit)
        db.commit()
    url = hit.get("thumb")
    if not url or "/artist//" in url:
        raise HTTPException(404, "no picture")
    return RedirectResponse(url, status_code=302)


@app.get("/api/artist/{key}")
def artist(key: str, name: str = ""):
    rec = db.recs_map().get(key)
    lib = db.library_map().get(key)
    hist = db.history().get(key)
    nm = name or (rec or lib or hist or {}).get("name") or key
    ad, p = _artist_data()
    info = db.cache_get(p + key, config.CACHE_DAYS)
    if info is None:
        try:
            info = ad.artist(nm, (lib or {}).get("mbid"))
            db.cache_put(p + key, info)
            db.commit()
        except Exception as e:
            log.warning("Artist lookup failed: %s", e)
    info = info or {}
    if p == "ob:" and info.get("wikidata") and "bio_url" not in info:   # Wikipedia bio, fetched the first time it's opened
        try:
            b = ad.bio(info["wikidata"]) or {}
            info = {**info, "bio": b.get("bio") or "", "bio_url": b.get("bio_url") or ""}
            db.cache_put(p + key, info)
            db.commit()
        except Exception as e:
            log.warning("Wikipedia lookup failed: %s", e)
    dz = _deezer(key, info.get("name") or nm)
    tracks = []
    if dz.get("id"):
        tracks = db.cache_get(f"dztop:{key}", 14)
        if tracks is None:
            try:
                tracks = _dz.top_tracks(dz["id"])
                db.cache_put(f"dztop:{key}", tracks)
                db.commit()
            except Exception:
                tracks = []
    owned_albums = {album_key(t) for t in (lib or {}).get("albums", [])}
    plex_rk = {album_key(a.get("title")): a.get("rk") for a in db.get_meta("plex_albums") or [] if a.get("artist_key") == key} if lib else {}
    albums, seen = [], set()
    for a in dz.get("albums", []):
        ak = album_key(a["title"])
        if ak in seen or a.get("type") not in ("album", "ep"):
            continue
        seen.add(ak)
        owned = ak in owned_albums
        if not owned and lib:   # every track is in Plex, just filed under other album names
            owned = _owns_every_track(db.cache_get(f"dzalb:{a['id']}", 30), nm, a["title"], db.get_meta("plex_songs"))
        albums.append({**a, "owned": owned, "plex_rk": plex_rk.get(ak)})
    albums.sort(key=lambda a: a.get("date") or "", reverse=True)
    fb = db.feedback().get(key)
    out = {
        **(rec or {}), "key": key, "name": info.get("name") or nm, "mbid": info.get("mbid") or (lib or {}).get("mbid"),
        "bio": info.get("bio") or (rec or {}).get("bio"), "url": info.get("url"), "bio_url": info.get("bio_url"),
        "tags": (rec or {}).get("tags") or [t for t, _ in info.get("tags", []) if t not in engine.JUNK_TAGS][:8],
        "listeners": info.get("listeners") or (rec or {}).get("listeners"),
        "picture": dz.get("picture"), "deezer_id": dz.get("id"), "fans": dz.get("fans") or info.get("fans"),
        "tracks": tracks or [], "albums": albums,
        "similar": [{"key": artist_key(n), "name": n, "match": m} for n, m in info.get("similar", [])[:24]],
        "owned": bool(lib), "plex_key": (lib or {}).get("plex_key"), "plays": (lib or {}).get("plays"),
        "rating": (lib or {}).get("rating"), "owned_albums": len((lib or {}).get("albums", [])),
        "sources": (hist or {}).get("sources", {}), "in_lidarr": key in set(db.get_meta("lidarr_keys", [])),
        "feedback": fb["action"] if fb else None, "saved": key in db.saved(), "noseed": key in db.noseed(),
        "my_rating": (lib or {}).get("rating") or (fb or {}).get("rating"),
        "kind": "stream" if rec and not rec.get("because") and rec.get("sources") else ("new" if rec else None),
    }
    lm = db.library_map()
    for s in out["similar"]:
        s["owned"] = s["key"] in lm
    return out


# ======================================================================= search & more like this

@app.get("/api/search")
def search(q: str):
    if not q.strip():
        return {"items": []}
    ad, p = _artist_data()
    try:
        if p == "lf:":
            d = ad.call("artist.search", artist=q.strip(), limit=30) or {}
            arts = [{"name": a["name"], "listeners": int(a.get("listeners") or 0), "fans": None}
                    for a in ((d.get("results") or {}).get("artistmatches") or {}).get("artist", [])]
        else:
            arts = [{"name": a["name"], "listeners": None, "fans": a.get("fans")} for a in _dz.search_artists(q.strip(), 30)]
    except Exception as e:
        raise HTTPException(502, friendly_error(e))
    rm, seen, out = db.recs_map(), set(), []
    for a in arts:
        k = artist_key(a["name"])
        if k in seen:
            continue
        seen.add(k)
        r = rm.get(k) or {}
        out.append({"key": k, "name": a["name"], "listeners": a["listeners"], "fans": a["fans"], "score": r.get("score"),
                    "tags": r.get("tags", []), "because": r.get("because", []), "matches": r.get("matches", [])})
    return {"items": _flags(out)}


@app.get("/api/like/{key}")
def more_like(key: str, name: str = ""):
    """Artists like this one, ranked by how similar they are and how well they fit your taste."""
    base = artist(key, name)
    sims = base["similar"][:40]
    ad, p = _artist_data()
    have = {k[len(p):]: v for k, v in db.cache_get_many([p + s["key"] for s in sims], config.CACHE_DAYS).items()}
    mbids = (db.cache_get(p + key, config.CACHE_DAYS) or {}).get("similar_mbids") or {}

    def one(s):
        try:
            return s["key"], ad.artist(s["name"], mbids.get(s["key"]))
        except Exception:
            return s["key"], None
    with ThreadPoolExecutor(max_workers=max(1, config.API_WORKERS)) as ex:
        for k, info in ex.map(one, [s for s in sims if s["key"] not in have]):
            if info:
                have[k] = info
                db.cache_put(p + k, info)
                db.commit()   # straight away, so a sync running at the same time isn't locked out
    taste = {t["tag"]: t["w"] for t in db.get_meta("taste", [])}
    seeds = db.get_meta("seeds", {})
    idf = engine.build_idf(list(have.values()))
    out = []
    for i, s in enumerate(sims):
        info = have.get(s["key"]) or {}
        v = engine.tag_vec(info, idf)
        fit = sum(taste.get(t, 0) * x for t, x in v.items())
        r = {"key": s["key"], "name": info.get("name") or s["name"], "listeners": info.get("listeners"), "fans": info.get("fans"),
             "tags": [t for t, _ in info.get("tags", []) if t not in engine.JUNK_TAGS][:8],
             "score": round(0.6 * s["match"] + 0.4 * min(1.0, fit * 1.5), 4), "because": [], "matches": [],
             "known": s["key"] in seeds}
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return {"name": base["name"], "picture": base.get("picture"), "items": _flags(out)}


# ======================================================================= feedback, saved, don't-learn

# ======================================================================= Rate

def _known_info(key):
    """Whatever we already have on an artist (no lookups): saved artist data, or the pick itself."""
    return db.cache_get("lf:" + key) or db.cache_get("ob:" + key) or {}


@app.get("/api/rate/queue")
def rate_queue(mode: str = "library", limit: int = 40, skip: str = ""):
    """library: artists you play in Plex but haven't rated, most played first.
    picks: your top new picks, in case you already know some of them."""
    skip_keys = {x for x in skip.split("\n") if x}   # one per line: an artist key can contain a comma
    fb, noseed = db.feedback(), db.noseed()
    if mode == "picks":
        rows = [r for r in _visible("new") if r["key"] not in skip_keys]
        return _fast({"items": [{k: r.get(k) for k in ("key", "name", "tags", "because", "score", "listeners", "fans", "bio")}
                                for r in rows[:limit]], "remaining": len(rows)})
    todo = [r for r in db.library() if not r.get("rating") and r["key"] not in noseed and r["key"] not in fb
            and ((r.get("plays") or 0) > 0 or r.get("last_played"))]
    todo.sort(key=lambda r: (-(r.get("plays") or 0), -(r.get("last_played") or 0)))
    remaining = len(todo)
    todo = [r for r in todo if r["key"] not in skip_keys][:limit]
    covers, wanted = {}, {r["key"] for r in todo}
    for a in db.get_meta("plex_albums", []):
        if a.get("artist_key") in wanted and a.get("rk"):
            covers.setdefault(a["artist_key"], []).append(a)
    out = []
    for r in todo:
        info = _known_info(r["key"])
        albums = sorted(covers.get(r["key"], []), key=lambda a: -(a.get("plays") or 0))[:4]
        out.append({"key": r["key"], "name": r["name"], "plays": r.get("plays") or 0, "last_played": r.get("last_played"),
                    "owned_albums": len(r.get("albums") or []),
                    "covers": [{"cover": f"/api/cover/{a['rk']}", "title": a.get("title")} for a in albums],
                    "tags": [t for t, _ in info.get("tags", []) if t not in engine.JUNK_TAGS][:6]})
    return _fast({"items": out, "remaining": remaining})


class RateBody(BaseModel):
    key: str
    name: str = ""
    rating: float | None = None   # 1-10 (stars x2); None = "I know them", no rating


@app.post("/api/rate")
def rate(body: RateBody):
    """An artist in your library: the rating goes to Plex. Anyone else: remembered here, and they stop being suggested."""
    if body.rating is not None and not 1 <= body.rating <= 10:
        raise HTTPException(400, "Ratings go from half a star to five stars")
    lib = db.library_map().get(body.key)
    if lib and lib.get("plex_key"):
        if body.rating is None:
            raise HTTPException(400, "Pick a rating")
        try:
            Plex(config.PLEX_URL, config.PLEX_TOKEN).rate(lib["plex_key"], body.rating)
        except Exception as e:
            raise HTTPException(502, f"Couldn't save the rating to Plex: {friendly_error(e)}")
        db.set_library_rating(body.key, body.rating)
        saved_to = "plex"
    else:
        db.set_feedback(body.key, "known", body.name or (lib or {}).get("name") or body.key, body.rating)
        saved_to = "recordhound"
    schedule_rescore(90)   # rating sessions come in bursts
    return {"ok": True, "saved_to": saved_to}


class Act(BaseModel):
    key: str
    name: str = ""
    action: str = "dismissed"   # dismissed | liked | known
    on: bool = True


@app.post("/api/feedback")
def feedback(body: Act):
    if body.action not in ("dismissed", "liked", "known", "added"):
        raise HTTPException(400, "unknown action")
    db.set_feedback(body.key, body.action, body.name)
    schedule_rescore()
    return {"ok": True}


@app.delete("/api/feedback/{key}")
def feedback_undo(key: str):
    db.clear_feedback(key)
    schedule_rescore()
    return {"ok": True}


@app.get("/api/feedback")
def feedback_list():
    return {"items": [{"key": k, **v} for k, v in db.feedback().items()]}


@app.post("/api/saved")
def saved_set(body: Act):
    db.set_saved(body.key, body.name, body.on)
    return {"ok": True, "saved": body.on}


@app.get("/api/saved")
def saved_list():
    rm = db.recs_map()
    out = []
    for k, v in db.saved().items():
        out.append({**(rm.get(k) or {"key": k, "name": v["name"], "tags": [], "because": [], "matches": []}), "added": v["at"]})
    return {"items": _flags(out)}


@app.post("/api/noseed")
def noseed_set(body: Act):
    db.set_noseed(body.key, body.name, body.on)
    schedule_rescore()
    return {"ok": True}


@app.get("/api/noseed")
def noseed_list():
    return {"items": [{"key": k, "name": v["name"]} for k, v in db.noseed().items()]}


@app.get("/api/noseed/genres")
def noseed_genres():
    """Tags on the artists you own, most common first, for "Genres to leave out". Only what the last sync stored.
    Counts each artist's top tags: those are the ones that leave an artist out (Syncer.compute)."""
    from collections import Counter
    p = "lf:" if _source_name() == "lastfm" else "ob:"
    c, spell = Counter(), {}
    for info in db.cache_get_many([p + r["key"] for r in db.library()]).values():
        top = [t for t, _ in (info or {}).get("tags", []) if t not in engine.JUNK_TAGS][:engine.NOSEED_TAG_DEPTH]
        for k, t in {engine.tag_key(t): t for t in top}.items():
            c[k] += 1
            spell.setdefault(k, Counter())[t] += 1
    # spellings of one tag count together, shown the way most artists have it (like the Genres filter)
    return {"genres": [{"tag": spell[k].most_common(1)[0][0], "n": n} for k, n in c.most_common(150)]}


@app.get("/api/plex/sections")
def plex_sections():
    return {"sections": [{"key": k, **v} for k, v in db.get_meta("plex_sections", {}).items() if v.get("type") == "artist"]}


# ======================================================================= Lidarr

@app.get("/api/lidarr/options")
def lidarr_options():
    try:
        return Lidarr(config.LIDARR_URL, config.LIDARR_API_KEY).options()
    except Exception as e:
        raise HTTPException(502, friendly_error(e))


class LidarrAdd(BaseModel):
    key: str
    name: str
    mbid: str | None = None
    quality_id: int
    metadata_id: int
    root: str
    monitor: str = "all"
    search: bool = True


@app.post("/api/lidarr/add")
def lidarr_add(body: LidarrAdd):
    try:
        Lidarr(config.LIDARR_URL, config.LIDARR_API_KEY).add(body.name, body.mbid, body.quality_id, body.metadata_id,
                                                             body.root, body.monitor, body.search)
    except Exception as e:
        raise HTTPException(502, friendly_error(e))
    keys = set(db.get_meta("lidarr_keys", []))
    keys.add(body.key)
    db.set_meta("lidarr_keys", sorted(keys))
    db.set_feedback(body.key, "added", body.name)
    db.set_meta("lidarr_last", {"quality_id": body.quality_id, "metadata_id": body.metadata_id, "root": body.root, "monitor": body.monitor})
    return {"ok": True}


class LidarrAlbum(BaseModel):
    artist: str
    title: str
    quality_id: int
    metadata_id: int
    root: str


@app.post("/api/lidarr/album")
def lidarr_album(body: LidarrAlbum):
    """One album from an artist's page: only that album is monitored and searched."""
    try:
        title = Lidarr(config.LIDARR_URL, config.LIDARR_API_KEY).add_album(body.artist, body.title, body.quality_id, body.metadata_id, body.root)
    except Exception as e:
        raise HTTPException(502, friendly_error(e))
    db.set_meta("lidarr_last", {**db.get_meta("lidarr_last", {}), "quality_id": body.quality_id, "metadata_id": body.metadata_id, "root": body.root})
    return {"ok": True, "title": title}


@app.get("/api/lidarr/last")
def lidarr_last():
    return db.get_meta("lidarr_last", {})


# ======================================================================= Spotify

_sp = {"client": None, "at": 0, "token": None}


def _spotify_client():
    """A signed-in Spotify client, reused for 50 minutes (Spotify's access tokens last an hour)."""
    if _sp["client"] and _sp["token"] == config.SPOTIFY_REFRESH_TOKEN and time.time() - _sp["at"] < 3000:
        return _sp["client"]
    sp = Spotify(config.SPOTIFY_CLIENT_ID, config.SPOTIFY_REFRESH_TOKEN, lambda t: config.save({"SPOTIFY_REFRESH_TOKEN": t}))
    db.set_meta("spotify_can_play", sp.can_play())
    _sp.update(client=sp, at=time.time(), token=config.SPOTIFY_REFRESH_TOKEN)
    return sp


class SpotifyPlay(BaseModel):
    kind: str
    id: str
    on: str = "computer"   # what you're using Recordhound on: "computer" or "phone"


@app.post("/api/spotify/play")
def spotify_play(body: SpotifyPlay):
    """Start a song or album in your Spotify app. When that isn't possible, says why, and the page opens the app instead."""
    if body.kind not in ("track", "album") or not (len(body.id) == 22 and body.id.isalnum()):
        raise HTTPException(400, "That isn't a Spotify song or album")
    if not config.SPOTIFY_REFRESH_TOKEN:
        return {"ok": False, "reason": "not_connected"}
    try:
        sp = _spotify_client()
        if not sp.can_play():
            return {"ok": False, "reason": "reconnect"}
        return {"ok": True, "device": sp.play(body.kind, body.id, body.on)}
    except SpotifyNoDevice:
        return {"ok": False, "reason": "no_device"}
    except Exception as e:
        return {"ok": False, "reason": "error", "message": friendly_error(e)}


class SpotifyFind(BaseModel):
    kind: str = "track"
    title: str
    artist: str
    isrc: str | None = None
    on: str = "computer"


@app.post("/api/spotify/play-match")
def spotify_play_match(body: SpotifyFind):
    """Play a song or album found by name (from Deezer: artist pages and album panels) in your Spotify app."""
    if body.kind not in ("track", "album"):
        raise HTTPException(400, "That isn't a song or album")
    if not config.SPOTIFY_REFRESH_TOKEN:
        return {"ok": False, "reason": "not_connected"}
    key = f"spfind:{body.kind}:" + (body.isrc or f"{artist_key(body.artist)}|{song_key(body.title) if body.kind == 'track' else album_key(body.title)}")
    try:
        sp = _spotify_client()
        if not sp.can_play():
            return {"ok": False, "reason": "reconnect"}
        hit = db.cache_get(key, 60)
        if hit is None or (not hit.get("id") and db.cache_get(key, 7) is None):   # misses are tried again after a week
            hit = {"id": sp.find(body.kind, body.title, body.artist, body.isrc)}
            db.cache_put(key, hit)
            db.commit()
        if not hit["id"]:
            return {"ok": False, "reason": "not_found"}
        return {"ok": True, "device": sp.play(body.kind, hit["id"], body.on)}
    except SpotifyNoDevice:   # the page opens the app on it instead
        return {"ok": False, "reason": "no_device", "id": hit["id"]}
    except Exception as e:
        return {"ok": False, "reason": "error", "message": friendly_error(e)}


class SpotifyStart(BaseModel):
    back: str = ""   # this page's address, so the helper page knows where to send you back to


SPOTIFY_ATTEMPT_MINUTES = 30


@app.post("/api/spotify/start")
def spotify_start(body: SpotifyStart | None = None):
    if not config.SPOTIFY_CLIENT_ID:
        raise HTTPException(400, "Save your Spotify Client ID first")
    redirect = spotify_redirect(config.SPOTIFY_REDIRECT_URI)
    if urllib.parse.urlparse(redirect).scheme not in ("http", "https"):
        raise HTTPException(400, "The Spotify redirect address in Settings needs to start with https:// or http://")
    state = secrets.token_urlsafe(24)
    url, verifier = spotify_auth_url(config.SPOTIFY_CLIENT_ID, redirect, state)
    db.set_meta("spotify_auth", {"state": state, "verifier": verifier, "redirect": redirect, "at": int(time.time())})
    if redirect != SPOTIFY_HELPER:
        return {"url": url, "redirect": redirect, "helper": False}
    back = urllib.parse.urlparse((body.back if body else "") or "")
    if back.scheme not in ("http", "https") or not back.netloc or "@" in back.netloc:
        raise HTTPException(400, "Couldn't tell this page's address. Reload Settings and try again.")
    back_url = urllib.parse.urlunparse((back.scheme, back.netloc, back.path if back.path.endswith("/") else back.path + "/", "", "", ""))
    return {"url": SPOTIFY_HELPER + "#" + urllib.parse.urlencode({"go": url, "back": back_url}), "redirect": redirect, "helper": True}


def _spotify_attempt(state):
    """The sign-in this server started, if state matches it and it isn't too old. Used once."""
    a = db.get_meta("spotify_auth") or {}
    if not a.get("state") or not state or not secrets.compare_digest(str(state), a["state"]):
        return None
    if time.time() - a.get("at", 0) > SPOTIFY_ATTEMPT_MINUTES * 60:
        return None
    db.set_meta("spotify_auth", None)
    return a


def _spotify_read(token):
    """First read of a newly connected account. Connected either way: the next sync reads it if this fails."""
    try:
        rows = Spotify(config.SPOTIFY_CLIENT_ID, token, lambda t: config.save({"SPOTIFY_REFRESH_TOKEN": t})).history()
        db.replace_history("spotify", rows)
    except Exception as e:
        log.warning("Spotify connected, but reading it failed: %s", e)
        rows = []
    schedule_rescore(5)
    return rows


def _spotify_connect(code, attempt):
    token = spotify_exchange(config.SPOTIFY_CLIENT_ID, code, attempt["verifier"], attempt["redirect"])
    config.save({"SPOTIFY_REFRESH_TOKEN": token, "SPOTIFY_CONNECTED_AT": int(time.time())})
    db.set_meta("spotify_error", None)
    db.set_meta("spotify_can_play", None)   # known again the next time it signs in
    return token


@app.get("/callback")
def spotify_callback(code: str = "", state: str = "", error: str = ""):
    """Where Spotify (through the helper page, or directly) sends you after you approve. Goes back to Settings."""
    def back(result):   # relative, so it works behind a reverse proxy on a sub-path too
        return RedirectResponse(f"./?spotify={result}#/settings", status_code=303)
    attempt = _spotify_attempt(state)
    if attempt is None:
        return back("expired")
    if error:
        return back("denied")
    try:
        token = _spotify_connect(code, attempt)
    except Exception as e:
        db.set_meta("spotify_error", friendly_error(e))
        return back("failed")

    def read():   # a big account takes a while to page through; don't keep you waiting on a blank page
        try:
            _spotify_read(token)
        finally:
            db.close()
    threading.Thread(target=read, daemon=True).start()
    return back("connected")


class Pasted(BaseModel):
    url: str


@app.post("/api/spotify/finish")
def spotify_finish(body: Pasted):
    """The copy-and-paste way: the address the browser landed on after approving."""
    try:
        code, state = spotify_pasted(body.url)
        a = db.get_meta("spotify_auth") or {}
        attempt = _spotify_attempt(state or a.get("state"))
        if attempt is None:
            raise APIError("That sign-in is too old or came from another attempt. Click Connect Spotify again.")
        rows = _spotify_read(_spotify_connect(code, attempt))
    except Exception as e:
        raise HTTPException(400, friendly_error(e))
    return {"ok": True, "artists": len(rows)}


@app.post("/api/spotify/disconnect")
def spotify_disconnect():
    config.save({"SPOTIFY_REFRESH_TOKEN": "", "SPOTIFY_CONNECTED_AT": 0})
    db.replace_history("spotify", [])
    db.set_meta("spotify_liked", None)
    schedule_rescore(5)
    return {"ok": True}


@app.post("/api/import/spotify-export")
async def spotify_export(request: Request):
    data = await security.read_upload(request, 500 * 1024 * 1024)
    try:
        rows = parse_spotify_export(data)
    except APIError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Couldn't read that file. Upload the .zip Spotify emailed you.")
    db.replace_history("spotify_export", rows)
    db.set_meta("spotify_export_at", int(time.time()))
    db.set_meta("spotify_export_v", 2)   # 2: counted by listening time. Older uploads counted every play of 30 s or more.
    schedule_rescore(5)
    return {"ok": True, "artists": len(rows), "plays": round(sum(r["plays"] for r in rows))}


@app.post("/api/import/youtube-music")
async def youtube_music_import(request: Request):
    data = await security.read_upload(request, 500 * 1024 * 1024, "That file is too big. In Google Takeout, choose only YouTube history.")
    try:
        rows = parse_youtube_music(data)
    except APIError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Couldn't read that file. Upload the zip from Google Takeout, or watch-history.json or .html from it.")
    db.replace_history("youtube_music", rows)
    db.set_meta("youtube_music_at", int(time.time()))
    schedule_rescore(5)
    return {"ok": True, "artists": len(rows), "plays": round(sum(r["plays"] for r in rows))}


@app.delete("/api/import/youtube-music")
def youtube_music_remove():
    db.replace_history("youtube_music", [])
    db.set_meta("youtube_music_at", None)
    schedule_rescore(5)
    return {"ok": True}


# ======================================================================= settings

@app.get("/api/settings")
def get_settings():
    return {"values": config.public(), "sources": config.sources(), "configured": config.configured(),
            "stats": {**db.stats(), "last_sync": db.get_meta("last_sync"), "spotify_export_at": db.get_meta("spotify_export_at"),
                      "youtube_music_at": db.get_meta("youtube_music_at"),
                      "spotify_export_old": bool(db.get_meta("spotify_export_at") and not db.get_meta("spotify_export_v"))},
            "spotify_redirect": spotify_redirect(config.SPOTIFY_REDIRECT_URI), "spotify_helper": SPOTIFY_HELPER,
            "spotify_loopback": SPOTIFY_LOOPBACK, "spotify_error": db.get_meta("spotify_error"),
            "spotify_can_play": db.get_meta("spotify_can_play")}


def _resolve(values):
    return {n: config.coerce(n, values[n]) if n in values and not config.is_masked(values[n]) else getattr(config, n)
            for n in config.FIELDS}


@app.put("/api/settings")
def put_settings(values: dict):
    _guard_secrets(values)
    updates = {k: v for k, v in values.items() if k in config.FIELDS and not config.is_masked(v)
               and not k.startswith(("AUTH_", "SPOTIFY_REFRESH", "SPOTIFY_CONNECTED"))}
    try:
        changed = config.save(updates)
    except ValueError as e:
        raise HTTPException(400, f"Invalid value: {e}")
    if set(changed) & {"OWNED_WEIGHT", "RECENT_WEIGHT", "MIN_PLAYS", "MIN_LISTENERS", "MIN_FANS", "NOSEED_LIBRARIES", "NOSEED_TAGS", "SEED_ARTISTS", "CANDIDATE_POOL", "MAX_RECS", "COMP_TASTE"}:
        schedule_rescore(5)
    return {"changed": changed, **get_settings()}


@app.post("/api/settings/adopt-env")
def adopt_env():
    src = config.sources()
    config.save({k: getattr(config, k) for k, v in src.items() if v == "env" and not k.startswith("AUTH_")})
    return get_settings()


SECRET_PAIRS = (("PLEX_URL", "PLEX_TOKEN"), ("LIDARR_URL", "LIDARR_API_KEY"),)


def _guard_secrets(values):
    bad = security.secret_host_guard(values, {k: getattr(config, k) for k in config.FIELDS}, SECRET_PAIRS)
    if bad:
        what = bad[1].replace("_", " ").title().replace("Api", "API")
        raise HTTPException(400, f"You changed the address, so enter the {what} again. Saved keys are only sent to the address they were saved with.")


def _short_version(v):
    """Plex reports "1.41.0.8992-f63ef7c4a"; people know it as 1.41.0."""
    return ".".join(str(v).split("-")[0].split(".")[:3])


@app.post("/api/settings/test/{service}")
def test_service(service: str, values: dict):
    _guard_secrets(values)
    v = _resolve(values)
    try:
        if service == "plex":
            if not v["PLEX_URL"] or not v["PLEX_TOKEN"]:
                raise APIError("Enter the Plex URL and token")
            plex = Plex(v["PLEX_URL"], v["PLEX_TOKEN"])
            info = plex._get("/")
            libs = [{"title": d.get("title"), "type": d.get("type")} for d in plex.all_sections() if d.get("type") == "artist"]
            who = f"Connected to {info.get('friendlyName') or 'Plex'}{' v' + _short_version(info['version']) if info.get('version') else ''}"
            if not libs:
                return {"ok": False, "message": f"{who} · no music libraries"}
            return {"ok": True, "message": f"{who} · {len(libs)} music librar{'y' if len(libs) == 1 else 'ies'}", "libraries": libs}
        if service == "lastfm":
            if not v["LASTFM_API_KEY"]:
                if v["LASTFM_USERNAME"]:
                    raise APIError("Reading your scrobbles needs a Last.fm API key, and from December 1, 2026 the key's account needs Last.fm Pro")
                return {"ok": True, "message": "No key: artist data comes from MusicBrainz, ListenBrainz and Deezer"}
            lf = LastFM(v["LASTFM_API_KEY"])
            try:
                lf.call("artist.getInfo", artist="Radiohead")
            except APIError as e:
                if "API key" in str(e):
                    return {"ok": False, "message": LASTFM_PRO_NOTE + " Clear the key to use free sources instead."}
                raise
            msg = "Connected to Last.fm · key works"
            if v["LASTFM_USERNAME"]:
                u = lf.call("user.getInfo", user=v["LASTFM_USERNAME"])
                if not u:
                    return {"ok": False, "message": f"Key works, but there's no Last.fm user '{v['LASTFM_USERNAME']}'"}
                msg = f"Connected to Last.fm · {int(u['user'].get('playcount') or 0):,} scrobbles for {u['user']['name']}"
            return {"ok": True, "message": msg}
        if service == "listenbrainz":
            if not v["LISTENBRAINZ_USERNAME"]:
                raise APIError("Enter your ListenBrainz username")
            rows = listenbrainz_top_artists(v["LISTENBRAINZ_USERNAME"], 5)
            return {"ok": True, "message": f"Connected to ListenBrainz · found {v['LISTENBRAINZ_USERNAME']}" + ("" if rows else " (ListenBrainz hasn't calculated your stats yet)")}
        if service == "lidarr":
            if not v["LIDARR_URL"] or not v["LIDARR_API_KEY"]:
                raise APIError("Enter the Lidarr URL and API key")
            r = requests.get(v["LIDARR_URL"] + "/api/v1/system/status", headers={**UA, "X-Api-Key": v["LIDARR_API_KEY"]}, timeout=10)
            if r.status_code in (401, 403):
                raise APIError("Lidarr rejected the API key")
            r.raise_for_status()
            o = Lidarr(v["LIDARR_URL"], v["LIDARR_API_KEY"]).options()
            n = lambda k, w: f"{k} {w}{'' if k == 1 else 's'}"   # noqa: E731
            ver = r.json().get("version")
            return {"ok": True, "message": f"Connected to Lidarr{' v' + ver if ver else ''} · {n(len(o['profiles']), 'quality profile')}, {n(len(o['roots']), 'root folder')}"}
        if service == "spotify":
            Spotify(v["SPOTIFY_CLIENT_ID"], config.SPOTIFY_REFRESH_TOKEN,
                    lambda t: config.save({"SPOTIFY_REFRESH_TOKEN": t}))._get("/me")
            return {"ok": True, "message": "Connected to Spotify"}
        raise HTTPException(404, "unknown service")
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "message": friendly_error(e)}


@app.post("/api/maintenance/{action}")
def maintenance(action: str):
    c = db.conn()
    if action == "clear-feedback":
        c.execute("DELETE FROM feedback")
        c.commit()
        db.bump()
        schedule_rescore(5)
    elif action == "clear-cache":
        if syncer.state["running"]:
            raise HTTPException(409, "Wait for the current sync to finish")
        c.execute("DELETE FROM cache")
        c.commit()
        db.bump()
    else:
        raise HTTPException(404, "unknown action")
    return {"ok": True}


# ======================================================================= auth

@app.get("/login")
def login_page():
    return auth.login_page()


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    if not auth.enabled():
        return {"ok": True}
    ip = security.client_ip(request)
    if security.login_blocked(ip):
        raise HTTPException(429, "Too many wrong passwords. Wait 15 minutes and try again.")
    if body.username != config.AUTH_USERNAME or not auth.check_password(body.password, config.AUTH_PASSWORD_HASH):
        security.login_failed(ip)
        time.sleep(1)
        raise HTTPException(401, "Wrong username or password")
    security.login_ok(ip)
    auth.set_cookie(response, request, body.username)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


class AuthBody(BaseModel):
    username: str = ""
    password: str = ""
    enabled: bool = True


@app.post("/api/auth")
def set_auth(body: AuthBody, request: Request, response: Response):
    if not body.enabled:
        config.save({"AUTH_USERNAME": "", "AUTH_PASSWORD_HASH": ""})
        return {"ok": True, "auth": False}
    if not body.username.strip() or len(body.password) < 8:
        raise HTTPException(400, "Pick a username and a password of at least 8 characters")
    config.save({"AUTH_USERNAME": body.username.strip(), "AUTH_PASSWORD_HASH": auth.hash_password(body.password)})
    auth.set_cookie(response, request, body.username.strip())
    return {"ok": True, "auth": True}
