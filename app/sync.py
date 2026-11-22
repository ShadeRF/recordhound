"""Reads your sources, then scores artists. Runs nightly, on demand, and (score only) after feedback."""
import logging
import zlib
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests

from . import config, db, engine
from .clients import (APIError, Deezer, LastFM, Lidarr, OpenArtists, Plex, Spotify, album_key, artist_key, artist_source, forget_lastfm_check, owned_track, same_song, special_release,
                      listenbrainz_top_artists)

log = logging.getLogger("recordhound")


def friendly_error(e):
    from .security import redact
    return redact(_friendly_error(e))


def _friendly_error(e):
    if isinstance(e, APIError):
        return str(e)
    if isinstance(e, requests.exceptions.ConnectionError):
        host = getattr(getattr(e, "request", None), "url", "") or ""
        return f"Couldn't connect to {host.split('/')[2] if host.count('/') >= 2 else 'a service'}. Check the address in Settings."
    if isinstance(e, requests.exceptions.Timeout):
        return "A service took too long to answer. Try again in a minute."
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        return f"{e.response.url.split('?')[0]} answered {e.response.status_code}"
    return str(e)


def cache_days(key):
    """How long a saved lookup stays fresh: CACHE_DAYS plus up to a third more, fixed per artist. Everything looked up
    in the first sync would otherwise go out of date on the same night and repeat that slow sync a month later."""
    days = config.CACHE_DAYS
    return days + (zlib.crc32(key.encode()) % 1000) / 1000 * days / 3 if days > 0 else days


def _days(d):
    try:
        return date.fromisoformat((d or "")[:10]).toordinal()
    except ValueError:
        return None


def _same_album(ak, when, seen):
    """A title that's only a longer form of one already listed, out around the same time: "Hidden in Pieces: Live"
    and "Hidden in Pieces: Live at the Royal Albert Hall". Short titles never match ("Blue" vs "Blue Lines"), and
    releases months apart stay separate ("Led Zeppelin" and "Led Zeppelin II")."""
    for s, other in seen.items():
        if s != ak and min(len(s), len(ak)) >= 8 and (ak.startswith(s) or s.startswith(ak)):
            a, b = _days(when), _days(other)
            if a is None or b is None or abs(a - b) <= 120:
                return True
    return False


class Syncer:
    def __init__(self, plex_factory=None, lastfm_factory=None, deezer_factory=None, sources=None, artist_factory=None):
        self.plex_factory = plex_factory or (lambda: Plex(config.PLEX_URL, config.PLEX_TOKEN, config.PLEX_LIBRARIES))
        self.lastfm_factory = lastfm_factory or (lambda: LastFM(config.LASTFM_API_KEY))   # scrobble history
        self.deezer_factory = deezer_factory or Deezer
        # artist data: () -> (client, cache prefix, warning). Last.fm with a working key, free sources otherwise.
        self.artist_factory = artist_factory or (lambda: artist_source(config.LASTFM_API_KEY, self.deezer_factory()))
        self.ad, self.prefix = None, "lf:"
        self.sources = sources   # tests pass {"lastfm": rows, ...} instead of calling out
        self.lock = threading.Lock()
        self.state = {"running": False, "step": "idle", "progress": None, "error": None, "warnings": []}

    def _step(self, step, progress=None):
        self.state.update(step=step, progress=progress)
        log.info("%s %s", step, progress or "")

    def _warn(self, msg):
        log.warning(msg)
        self.state["warnings"].append(msg)

    def run(self, full=True):
        if not self.lock.acquire(blocking=False):
            return
        self.state.update(running=True, error=None, warnings=[])
        try:
            if full:
                self.sync_sources()
            self.compute()
            if full:
                try:
                    self.albums()
                except Exception as e:
                    self._warn(f"Albums: {friendly_error(e)}")
                try:
                    self.link_singles()
                except Exception as e:
                    self._warn(f"Singles: {friendly_error(e)}")
            db.set_meta("last_sync" if full else "last_compute", int(time.time()))
            self._step("done")
        except Exception as e:
            msg = friendly_error(e)
            log.error("sync failed: %s\n%s", msg, traceback.format_exc())
            self.state["error"] = msg
        finally:
            self.state["running"] = False
            self.lock.release()
            if threading.current_thread() is not threading.main_thread():
                db.close()

    # ------------------------------------------------------------------ sources
    def sync_sources(self):
        self._step("Reading Plex")
        plex = self.plex_factory()
        rows = plex.artists(self._step)
        albums = [{**a, "artist_key": r["key"], "artist": r["name"]} for r in rows for a in r.pop("album_rows", None) or []]
        albums += getattr(plex, "comp_albums", None) or []   # soundtracks and Various Artists albums
        if getattr(plex, "comp_plays", None) is not None:   # plays of songs on them count for the artists who sang them
            db.replace_history("plex_comp", [r for r in plex.comp_plays.values() if r["plays"]])
        db.replace_library(rows)
        db.set_meta("plex_albums", albums)
        if getattr(plex, "songs", None) is not None:   # every track, to tell which liked songs you own
            db.set_meta("plex_songs", plex.songs)
        self._step("Plex read", f"{len(rows):,} artists")
        try:
            db.set_meta("plex_sections", {d["key"]: {"title": d.get("title"), "type": d.get("type")} for d in plex.all_sections()})
            db.set_meta("plex_machine", plex.machine_id())
        except Exception as e:
            log.warning("Plex extras: %s", e)

        fetchers = {
            "lastfm": (config.LASTFM_USERNAME, lambda: self.lastfm_factory().user_top_artists(config.LASTFM_USERNAME)),
            "listenbrainz": (config.LISTENBRAINZ_USERNAME, self._listenbrainz),
            "spotify": (config.SPOTIFY_REFRESH_TOKEN, self._spotify),
        }
        for src, (enabled, fn) in fetchers.items():
            if self.sources is not None:
                if src in self.sources:
                    db.replace_history(src, self.sources[src])
                if src == "spotify" and "spotify_liked" in self.sources:
                    db.set_meta("spotify_liked", self.sources["spotify_liked"])
                continue
            if not enabled:
                if db.history_counts().get(src):   # disconnected since last time: its plays stop shaping picks
                    db.replace_history(src, [])
                    if src == "listenbrainz":
                        db.set_meta("recent_listens", {})
                if src == "spotify" and db.get_meta("spotify_liked") is not None:
                    db.set_meta("spotify_liked", None)
                continue
            self._step(f"Reading {src.title() if src != 'lastfm' else 'Last.fm'}")
            try:
                db.replace_history(src, fn())
            except Exception as e:
                self._warn(f"{src}: {friendly_error(e)}")
        if config.LIDARR_URL and config.LIDARR_API_KEY and self.sources is None:
            try:
                mb = Lidarr(config.LIDARR_URL, config.LIDARR_API_KEY).artist_mbids()
                db.set_meta("lidarr_keys", sorted(mb))
                db.set_meta("lidarr_mbids", {k: v for k, v in mb.items() if v})
            except Exception as e:
                self._warn(f"Lidarr: {friendly_error(e)}")

    SINGLE_RECHECK_DAYS = 14   # a single with no album yet is looked at again after this long
    SINGLE_ARTISTS_PER_SYNC = 300   # keeps one sync from running long; the rest carry over to the next

    def link_singles(self):
        """Liked songs that came out as singles often end up on an album later. Find that album on Deezer (the artist's
        albums and EPs, then their track lists), so Liked, not owned can offer the album instead of the single."""
        liked, songs = db.get_meta("spotify_liked"), db.get_meta("plex_songs") or {}
        if not liked or (self.sources is not None and "spotify_liked" not in self.sources):
            return
        links, now = dict(db.get_meta("single_links") or {}), time.time()
        due = {}
        for t in liked:
            if t.get("album_type") != "single" or not t.get("id") or owned_track(t, songs):
                continue
            old = links.get(t["id"])
            if old and (old.get("album") or now - old.get("at", 0) < self.SINGLE_RECHECK_DAYS * 86400):
                continue
            artist = t.get("album_artist") or (t.get("artists") or [""])[0]
            due.setdefault(artist_key(artist), (artist, []))[1].append(t)
        if not due:
            return
        dz, found = self.deezer_factory(), 0
        for n, (key, (artist, tracks)) in enumerate(list(due.items())[:self.SINGLE_ARTISTS_PER_SYNC]):
            if n % 10 == 0:
                self._step("Finding albums for liked singles", f"{n:,} of {min(len(due), self.SINGLE_ARTISTS_PER_SYNC):,} artists")
            try:
                albums = self._dz_albums(dz, key, artist)
                for t in tracks:
                    hit = self._album_with(dz, albums, t.get("title"))
                    links[t["id"]] = {"at": int(now), "album": hit}
                    found += bool(hit)
            except Exception as e:   # Deezer hiccup: try these again next sync
                log.warning("Linking singles for %s: %s", artist, e)
        db.set_meta("single_links", links)
        if found:
            self._step("Singles linked", f"{found:,} now on albums")

    def _dz_albums(self, dz, key, name):
        """The artist's albums and EPs on Deezer, oldest first, studio albums before live ones. Shares the artist page's cache."""
        hit = db.cache_get("dz:" + key, 14)
        if hit is None:
            hit = dz.find_artist(name)
            if not hit.get("missing"):
                hit["albums"] = dz.albums(hit["id"])
            db.cache_put("dz:" + key, hit)
            db.commit()
        albums = [a for a in hit.get("albums") or [] if a.get("type") in ("album", "ep")]
        live = lambda a: "live" in (a.get("title") or "").lower()
        return sorted(albums, key=lambda a: (live(a), a.get("type") != "album", a.get("date") or "9999"))

    def _album_with(self, dz, albums, title):
        for a in albums:
            tracks = db.cache_get(f"dzalb:{a['id']}", 30)
            if tracks is None:
                tracks = dz.album_tracks(a["id"])
                db.cache_put(f"dzalb:{a['id']}", tracks)
                db.commit()
            if any(same_song(t.get("title"), title) for t in tracks):
                return {"id": a["id"], "title": a.get("title"), "year": (a.get("date") or "")[:4], "cover": a.get("cover"),
                        "type": "ep" if a.get("type") == "ep" else "album"}
        return None

    def _spotify(self):
        """Top, followed and saved artists for your taste, plus your Liked Songs for Liked, not owned."""
        sp = Spotify(config.SPOTIFY_CLIENT_ID, config.SPOTIFY_REFRESH_TOKEN, lambda t: config.save({"SPOTIFY_REFRESH_TOKEN": t}))
        db.set_meta("spotify_can_play", sp.can_play())
        rows = sp.history()
        try:
            db.set_meta("spotify_liked", sp.liked_tracks())
        except Exception as e:
            self._warn(f"Spotify liked songs: {friendly_error(e)}")
        return rows

    def _listenbrainz(self):
        """All-time top artists for your taste, plus the last six months for the recent-listening boost."""
        user = config.LISTENBRAINZ_USERNAME
        rows = listenbrainz_top_artists(user)
        mbids = dict(db.get_meta("history_mbids", {}))
        mbids.update({r["key"]: r["mbid"] for r in rows if r.get("mbid")})
        try:
            recent = listenbrainz_top_artists(user, rng="half_yearly")
            db.set_meta("recent_listens", {r["key"]: r["plays"] for r in recent})
            mbids.update({r["key"]: r["mbid"] for r in recent if r.get("mbid")})
        except Exception as e:
            log.warning("ListenBrainz recent stats: %s", e)
        db.set_meta("history_mbids", mbids)
        return rows

    # ------------------------------------------------------------------ artist lookups (cached)
    def _infos(self, wanted, label):
        """{key: info} for (key, name, mbid) tuples; fetches what isn't cached."""
        p = self.prefix
        now, have, stale = time.time(), {}, {}
        for k, (v, at) in db.cache_rows([p + k for k, _, _ in wanted]).items():
            (have if now - at <= cache_days(k) * 86400 else stale)[k[len(p):]] = v
        # An out-of-date entry still knows the artist's MusicBrainz id, which saves the slow one-a-second name search.
        todo = [(k, n, m or (stale.get(k) or {}).get("mbid")) for k, n, m in wanted if k not in have]
        if todo:
            lf = self.ad
            if hasattr(lf, "prefetch"):   # free sources: fetch tags for everyone we have an id for in a few batches
                self._step(label, "batch lookups")
                lf.prefetch([m for _, _, m in todo if m])
            done = 0

            def one(t):
                k, n, m = t
                try:
                    return k, lf.artist(n, m)
                except APIError as e:
                    if "API key" in str(e):
                        forget_lastfm_check()   # the next sync re-checks the key and can switch to free sources
                        raise
                    return k, None
                except Exception:
                    return k, None
            with ThreadPoolExecutor(max_workers=max(1, config.API_WORKERS)) as ex:
                for k, info in ex.map(one, todo):
                    done += 1
                    if info is not None:
                        have[k] = info
                        db.cache_put(p + k, info)
                        db.commit()   # straight away: an open write would block hides and saves until the next lookup returns
                    elif k in stale:
                        have[k] = stale[k]   # the refresh failed: what we knew last month beats dropping the artist
                    if done % 25 == 0:
                        self._step(label, f"{done:,}/{len(todo):,}")
            db.commit()
        return have

    # ------------------------------------------------------------------ scoring
    def compute(self):
        self._step("Building your taste")
        library = db.library()
        sections = db.get_meta("plex_sections", {})
        skip_libs = {s.lower() for s in config.NOSEED_LIBRARIES}
        noseed = set(db.noseed()) | {r["key"] for r in library
                                      if (sections.get(str(r.get("section")), {}).get("title") or "").lower() in skip_libs}
        history, feedback = db.history(), db.feedback()
        if not config.COMP_TASTE:   # soundtrack and compilation plays left out of your taste
            history = {k: {**h, "sources": {s: v for s, v in h["sources"].items() if s != "plex_comp"}} for k, h in history.items()}
            history = {k: h for k, h in history.items() if h["sources"]}
        seeds = engine.build_seeds(library, history, feedback, config.OWNED_WEIGHT, noseed,
                                   recent=db.get_meta("recent_listens", {}), recent_weight=config.RECENT_WEIGHT,
                                   min_plays=config.MIN_PLAYS)
        if not seeds:
            raise APIError("Nothing to learn from yet. Check that Plex has a music library, or connect Last.fm, ListenBrainz or Spotify.")
        self.ad, self.prefix, note = self.artist_factory()
        if note:
            self._warn(note)
        db.set_meta("artist_source", "lastfm" if self.prefix == "lf:" else "open")
        # MusicBrainz ids we already know save a name search each: Plex's, then ListenBrainz's, then Lidarr's
        lib_mbid = {**db.get_meta("lidarr_mbids", {}), **db.get_meta("history_mbids", {}),
                    **{r["key"]: r["mbid"] for r in library if r.get("mbid")}}
        top = sorted(seeds, key=lambda k: -abs(seeds[k]["w"]))[:max(config.SEED_ARTISTS, 1)]
        infos = self._infos([(k, seeds[k]["name"], lib_mbid.get(k)) for k in top], "Looking up your artists")
        if config.NOSEED_TAGS:   # spellings of one tag count as one ("hip hop", "Hip-Hop")
            bad, depth = {engine.tag_key(t) for t in config.NOSEED_TAGS}, engine.NOSEED_TAG_DEPTH
            def tops(info):
                return {engine.tag_key(t) for t in [t for t, _ in (info or {}).get("tags", []) if t not in engine.JUNK_TAGS][:depth]}
            for k in [k for k in seeds if tops(infos.get(k)) & bad]:
                seeds.pop(k)

        lib_keys = {r["key"] for r in library}
        lidarr = set(db.get_meta("lidarr_keys", []))
        gone = {k for k, f in feedback.items() if f["action"] in ("dismissed", "added", "known")}
        known = {k for k, s in seeds.items() if not s["owned"] and s["listen"] > 0.05}
        exclude = lib_keys | lidarr | gone | known

        self._step("Finding similar artists")
        cands = engine.candidates(seeds, infos, exclude, config.SEED_ARTISTS)
        pool = sorted(cands, key=lambda k: -cands[k]["collab"])[:config.CANDIDATE_POOL]
        known_mbids = {}   # ListenBrainz names similar artists by MusicBrainz id, which saves a search per candidate
        for i in infos.values():
            for k, m in ((i or {}).get("similar_mbids") or {}).items():
                known_mbids.setdefault(k, m)
        infos.update(self._infos([(k, cands[k]["name"], known_mbids.get(k)) for k in pool], "Looking up candidates"))
        recs, pos, idf = engine.score({k: cands[k] for k in pool}, seeds, infos, config.MIN_LISTENERS, min_fans=config.MIN_FANS)
        if config.MAX_RECS:
            recs = recs[:config.MAX_RECS]
        db.replace_recs("new", recs)

        # when each pick first appeared, for "New this week". The very first sync counts as the start, not as new.
        first = db.get_meta("first_seen")
        now = int(time.time())
        start = first is None
        rec_keys = {r["key"] for r in recs}
        first = {k: v for k, v in (first or {}).items() if k in rec_keys}
        for r in recs:
            first.setdefault(r["key"], 0 if start else now)
        db.set_meta("first_seen", first)

        # albums you own but have never played
        db.set_meta("unplayed", engine.unplayed_albums(db.get_meta("plex_albums", []), seeds, infos, pos, idf, noseed))
        db.set_meta("seed_mbids", {k: (infos.get(k) or {}).get("mbid") or lib_mbid.get(k) for k in top
                                   if (infos.get(k) or {}).get("mbid") or lib_mbid.get(k)})

        # you play them elsewhere but don't own them (and haven't sent them to Lidarr)
        stream_keys = [k for k in known if k not in lidarr and k not in gone]
        infos.update(self._infos([(k, seeds[k]["name"], lib_mbid.get(k)) for k in stream_keys[:400]], "Looking up artists you stream"))
        stream = []
        for k in stream_keys:
            s, info, h = seeds[k], infos.get(k) or {}, history.get(k) or {}
            stream.append({"key": k, "name": info.get("name") or s["name"], "mbid": info.get("mbid"),
                           "score": round(s["listen"] + (0.1 if any(v.get("saved") for v in h.get("sources", {}).values()) else 0), 4),
                           "sources": {src: v for src, v in h.get("sources", {}).items()}, "plays": s["plays"],
                           "tags": [t for t, _ in info.get("tags", []) if t not in engine.JUNK_TAGS][:8],
                           "listeners": info.get("listeners"), "fans": info.get("fans"), "bio": (info.get("bio") or "")[:600], "url": info.get("url"),
                           "because": [], "matches": []})
        stream.sort(key=lambda r: -r["score"])
        db.replace_recs("stream", stream)
        db.set_meta("taste", engine.taste_tags(pos, 16))
        db.set_meta("seeds", {k: s for k, s in sorted(seeds.items(), key=lambda kv: -kv[1]["w"])[:400]})
        self._step("Scored", f"{len(recs):,} new artists, {len(stream):,} you stream")

    # ------------------------------------------------------------------ albums: gaps & new releases
    def albums(self):
        seeds = db.get_meta("seeds", {})
        lib = db.library_map()
        dz = self.deezer_factory()
        top = [k for k, s in seeds.items() if s["w"] > 0.5][:120]
        self._step("Checking albums from your artists", f"{len(top)} artists")

        found = {k[3:]: v for k, v in db.cache_get_many(["dz:" + k for k in top], 7).items()}

        def deezer(k):   # network only: database access stays on this thread
            try:
                a = dz.find_artist(seeds[k]["name"])
                if not a.get("missing"):
                    a["albums"] = dz.albums(a["id"])
            except Exception as e:   # one artist failing must not stop the rest; it's tried again next sync
                log.warning("Deezer lookup failed for %s: %s", seeds[k]["name"], e)
                return k, None
            return k, a
        failed = set()
        with ThreadPoolExecutor(max_workers=3) as ex:   # Deezer allows ~10 requests a second
            for k, a in ex.map(deezer, [k for k in top if k not in found]):
                if a is None:
                    failed.add(k)
                    continue
                found[k] = a
                db.cache_put("dz:" + k, a)
                db.commit()
        if failed:   # fall back to what we knew last time, however old, so their gaps and releases don't vanish for a night
            found.update({k[3:]: v for k, v in db.cache_get_many(["dz:" + k for k in failed]).items()})
        mbrg = self._mb_releases(top)
        today = date.today()
        recent = (today - timedelta(days=120)).isoformat()
        gaps, releases = [], []
        for k in top:
            a = found.get(k) or {}
            if a.get("missing") and not mbrg.get(k):
                continue
            owned = {album_key(t) for t in (lib.get(k) or {}).get("albums", [])}
            seen = {}   # album key -> release date
            albums = []
            for al in a.get("albums", []):
                ak = album_key(al["title"])
                if ak in seen or al.get("type") not in ("album", "ep") or _same_album(ak, al.get("date"), seen):
                    continue
                seen[ak] = al.get("date")
                albums.append({**al, "owned": ak in owned, "artist": seeds[k]["name"], "artist_key": k})
            for g in mbrg.get(k, []):   # new releases Deezer doesn't have yet, including announced ones (not used for gaps)
                ak = album_key(g["title"])
                if ak in seen or ak in owned or len(g["date"]) < 7 or g["date"] < recent or _same_album(ak, g["date"], seen):
                    continue
                seen[ak] = g["date"]
                albums.append({"id": g["id"], "title": g["title"], "date": g["date"] if len(g["date"]) == 10 else g["date"] + "-01",
                               "type": g["type"], "cover": f"https://coverartarchive.org/release-group/{g['id']}/front-250",
                               "fans": 0, "owned": False, "artist": seeds[k]["name"], "artist_key": k, "source": "musicbrainz",
                               "mb_only": True})
            if (al_new := [x for x in albums if (x.get("date") or "") >= recent and not x["owned"]]):
                releases += al_new
            if k in lib and not a.get("missing"):
                missing = [{**x, "special": special_release(x["title"], (lib.get(k) or {}).get("albums", []))} for x in albums
                           if not x.get("mb_only") and not x["owned"] and (x.get("date") or "9") <= today.isoformat()]
                n_own = len([x for x in albums if x["owned"]])
                if missing and n_own:   # EPs and special releases are kept, marked; the page filters by your settings
                    missing.sort(key=lambda x: -(x.get("fans") or 0))
                    gaps.append({"key": k, "name": seeds[k]["name"], "picture": a.get("thumb"), "owned": n_own, "albums": missing[:30]})
        releases.sort(key=lambda x: x.get("date") or "", reverse=True)
        if self.sources is None:
            self._cover_check([x for x in releases if x.get("mb_only")])
        gaps.sort(key=lambda g: -seeds[g["key"]]["w"])
        db.set_meta("gaps", {"at": time.time(), "groups": gaps})
        db.set_meta("releases", {"at": time.time(), "items": releases})


def _cover_check(self, items):
    """Announced albums often have no artwork yet. Ask the Cover Art Archive once a week, so they show a clean title
    card instead of a broken image, and For You can lead with the albums that do have covers."""
    ids = [x["id"] for x in items]
    known = {k[4:]: v for k, v in db.cache_get_many(["caa:" + i for i in ids], 7).items()}

    def has_art(i):   # network only: database access stays on this thread
        try:
            r = requests.head(f"https://coverartarchive.org/release-group/{i}/front-250", timeout=10, allow_redirects=True,
                              headers={"User-Agent": "Recordhound"})
            return i, (r.status_code == 200 if r.status_code in (200, 404) else None)
        except requests.RequestException:
            return i, None
    with ThreadPoolExecutor(max_workers=3) as ex:
        for i, art in ex.map(has_art, [i for i in ids if i not in known]):
            if art is not None:   # unsure (archive down): leave the cover and ask again next time
                known[i] = art
                db.cache_put("caa:" + i, art)
                db.commit()
    for x in items:
        if known.get(x["id"]) is False:
            x["cover"] = None


Syncer._cover_check = _cover_check


def _mb_releases(self, keys):
    """MusicBrainz release groups for your top artists with a known id, cached for a week."""
    ids = db.get_meta("seed_mbids", {})
    keys = [k for k in keys if ids.get(k)]
    found = {k[5:]: v for k, v in db.cache_get_many(["mbrg:" + k for k in keys], 7).items()}
    todo = [k for k in keys if k not in found]
    if todo and self.sources is None:
        oa = OpenArtists(self.deezer_factory())
        for i, k in enumerate(todo):
            try:
                found[k] = oa.release_groups(ids[k])
                db.cache_put("mbrg:" + k, found[k])
                db.commit()
            except Exception as e:
                log.warning("MusicBrainz releases for %s: %s", k, e)
            if i % 20 == 19:
                self._step("Checking new releases on MusicBrainz", f"{i + 1}/{len(todo)}")
        db.commit()
    return found


Syncer._mb_releases = _mb_releases


def _start(self, full=True):
    if self.state["running"]:
        return False
    threading.Thread(target=self.run, kwargs={"full": full}, daemon=True).start()
    return True


Syncer.start = _start
