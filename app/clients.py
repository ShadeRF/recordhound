"""Talking to Plex, Last.fm, MusicBrainz, ListenBrainz, Wikipedia, Spotify, Deezer and Lidarr."""
import base64
import functools
import hashlib
import html
import io
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
import urllib.parse
import zipfile

import requests

from . import __version__

log = logging.getLogger("recordhound")
UA = {"User-Agent": f"Recordhound/{__version__} (self-hosted music recommendations; https://github.com/ShadeRF/recordhound)"}


class APIError(Exception):
    pass


# letters that don't break down into a plain letter plus an accent, so they'd otherwise vanish ("MØ" → "m")
_PLAIN = str.maketrans({"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "ß": "ss", "ẞ": "SS",
                        "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "þ": "th", "Þ": "TH", "ı": "i", "ħ": "h", "Ħ": "H"})


# Greek capitals used as styled Latin letters: "CHVRCHΞS" is Chvrches. Only applied to names that are otherwise plain Latin,
# so a Greek artist's name is never half-converted.
_LOOKALIKE = str.maketrans({"Ξ": "E", "Σ": "E", "Λ": "A", "Δ": "A"})


def _restyle(name):
    if any(c in name for c in "ΞΣΛΔ") and any(c.isascii() and c.isalpha() for c in name):
        rest = [c for c in name if ord(c) > 127 and c not in "ΞΣΛΔ"]
        if not any(unicodedata.category(c).startswith("L") and not unicodedata.name(c, "").startswith("LATIN") for c in rest):
            return name.translate(_LOOKALIKE)
    return name


@functools.lru_cache(maxsize=50_000)   # called tens of thousands of times per sync, mostly with names it has seen before
def artist_key(name):
    """One key per artist however it's spelled: 'The Beatles', 'Beatles', 'beatles' → 'beatles'."""
    s = unicodedata.normalize("NFKD", _restyle(name or "").translate(_PLAIN)).encode("ascii", "ignore").decode().lower().strip()
    s = s.replace("&", " and ").replace("+", " and ")
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r",\s*the$", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or (name or "").strip().lower()


def album_key(title):
    s = unicodedata.normalize("NFKD", (title or "").translate(_PLAIN)).encode("ascii", "ignore").decode().lower()
    # drop "(Deluxe Edition)", "[2011 Remaster]" and the like; each match stays inside one pair of brackets
    cut = re.sub(r"[\(\[][^\)\]]*(deluxe|remaster|edition|expanded|anniversary|bonus|version|mono|stereo)[^\)\]]*[\)\]]", "", s)
    return re.sub(r"[^a-z0-9]+", "", cut) or re.sub(r"[^a-z0-9]+", "", s)


@functools.lru_cache(maxsize=200_000)
def song_key(title):
    """One key per song however it's labelled: "Song - Remastered 2011", "Song (Radio Edit)" and "Song feat. X"
    are all "song". Forgiving on purpose: owning any version of a song counts."""
    s = unicodedata.normalize("NFKD", (title or "").translate(_PLAIN)).encode("ascii", "ignore").decode().lower()
    cut = re.sub(r"\s+-\s+.*$", "", s)                       # Spotify's " - Remastered 2011", " - Radio Edit"
    cut = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", cut)            # "(Live)", "[feat. X]"
    cut = re.sub(r"\s(?:feat|ft|featuring)\.?\s.*$", "", cut)
    cut = cut.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", cut) or re.sub(r"[^a-z0-9]+", "", s) or (title or "").strip().lower()


_CREDIT_SPLIT = re.compile(r"\s*(?:,|;|/|\s&\s|\sx\s|\s(?:feat|ft)\.?\s|\sfeaturing\s|\swith\s|\sand\s)\s*", re.I)


@functools.lru_cache(maxsize=50_000)
def credit_keys(credit):
    """Artist keys for a credit like "Calvin Harris, Dua Lipa" or "Simon & Garfunkel": the whole credit and each name."""
    keys = {artist_key(credit)}
    keys.update(artist_key(p) for p in _CREDIT_SPLIT.split(credit or "") if p.strip())
    keys.discard("")
    return frozenset(keys)


def owned_track(t, songs):
    """The Plex track for a liked song, if you own it under any of its credited artists."""
    sk = song_key(t.get("title"))
    for credit in (t.get("artists") or []) + [t.get("album_artist") or ""]:
        for ak in credit_keys(credit):
            rk = songs.get(f"{ak}|{sk}")
            if rk:
                return rk
    return None


# ======================================================================= Plex

class Plex:
    def __init__(self, url, token, libraries=None):
        self.url, self.libraries = url.rstrip("/"), libraries or []
        self.s = requests.Session()
        self.s.headers.update({**UA, "Accept": "application/json", "X-Plex-Token": token,
                               "X-Plex-Client-Identifier": "recordhound", "X-Plex-Product": "Recordhound"})

    def _get(self, path, **params):
        r = self.s.get(self.url + path, params=params, timeout=90)
        if r.status_code == 401:
            raise APIError("Plex rejected the token (401)")
        r.raise_for_status()
        return r.json().get("MediaContainer", {})

    def all_sections(self):
        return self._get("/library/sections").get("Directory", [])

    def rate(self, rating_key, rating):
        """Set your star rating (0-10, stars x2) on a Plex item: here, an artist."""
        r = self.s.put(self.url + "/:/rate", params={"key": rating_key, "identifier": "com.plexapp.plugins.library",
                                                     "rating": rating}, timeout=30)
        if r.status_code == 401:
            raise APIError("Plex rejected the token (401)")
        r.raise_for_status()

    def sections(self):
        secs = [d for d in self.all_sections() if d.get("type") == "artist"]
        if self.libraries:
            wanted = {n.lower() for n in self.libraries}
            secs = [d for d in secs if d.get("title", "").lower() in wanted]
        return secs

    def _paged(self, section, plex_type, **extra):
        start, size = 0, 2000
        while True:
            mc = self.s.get(f"{self.url}/library/sections/{section}/all", timeout=180,
                            params={"type": plex_type, "X-Plex-Container-Start": start, "X-Plex-Container-Size": size, **extra})
            mc.raise_for_status()
            mc = mc.json().get("MediaContainer", {})
            items = mc.get("Metadata", [])
            yield from items
            start += len(items)
            if not items or start >= mc.get("totalSize", 0):
                break

    def artists(self, step=lambda *a: None):
        """One row per artist: plays (Plexamp counts), rating, owned albums. Also fills self.songs:
        {"artist key|song key": track rating key} for every track, to tell which liked songs you own."""
        out = {}
        self.songs = {}
        for sec in self.sections():
            sid = sec["key"]
            step(f"Reading Plex: {sec.get('title')}")
            by_rk = {}
            for a in self._paged(sid, 8, includeGuids=1):
                mbid = next((g["id"][7:] for g in a.get("Guid", []) if g.get("id", "").startswith("mbid://")), None)
                row = {"name": a.get("title"), "mbid": mbid, "plex_key": a.get("ratingKey"), "thumb": a.get("thumb"),
                       "section": sid, "plays": 0, "tracks": 0, "albums": [], "album_rows": [], "ratings": [],
                       "rating": a.get("userRating"), "last_played": a.get("lastViewedAt"), "added_at": a.get("addedAt")}
                by_rk[a.get("ratingKey")] = row
            albums = {}
            for al in self._paged(sid, 9):
                row = by_rk.get(al.get("parentRatingKey"))
                if row:
                    row["albums"].append(al.get("title"))
                    if al.get("userRating"):
                        row["ratings"].append(al["userRating"])
                    a = {"rk": al.get("ratingKey"), "title": al.get("title"), "year": al.get("year"), "thumb": al.get("thumb"),
                         "added_at": al.get("addedAt"), "rating": al.get("userRating"), "plays": 0}
                    albums[a["rk"]] = a
                    row["album_rows"].append(a)
            n = 0
            for t in self._paged(sid, 10):
                row = by_rk.get(t.get("grandparentRatingKey"))
                if not row:
                    continue
                n += 1
                row["tracks"] += 1
                row["plays"] += t.get("viewCount") or 0
                al = albums.get(t.get("parentRatingKey"))
                if al:
                    al["plays"] += t.get("viewCount") or 0
                if t.get("userRating"):
                    row["ratings"].append(t["userRating"])
                sk = song_key(t.get("title"))
                for ak in credit_keys(row["name"]) | (credit_keys(t["originalTitle"]) if t.get("originalTitle") else frozenset()):
                    self.songs.setdefault(f"{ak}|{sk}", t.get("ratingKey"))
                lv = t.get("lastViewedAt")
                if lv and (not row["last_played"] or lv > row["last_played"]):
                    row["last_played"] = lv
                if n % 5000 == 0:
                    step(f"Reading Plex: {sec.get('title')}", f"{n:,} tracks")
            for row in by_rk.values():
                k = artist_key(row["name"])
                if not k or k in ("variousartists", "various"):
                    continue
                rs = row.pop("ratings")
                if not row["rating"] and rs:
                    row["rating"] = round(sum(rs) / len(rs), 1)
                prev = out.get(k)
                if prev:   # same artist in two libraries
                    prev["plays"] += row["plays"]
                    prev["tracks"] += row["tracks"]
                    prev["albums"] += row["albums"]
                    prev["album_rows"] += row["album_rows"]
                    prev["rating"] = prev["rating"] or row["rating"]
                    prev["mbid"] = prev["mbid"] or row["mbid"]
                else:
                    out[k] = {**row, "key": k}
        # The same MusicBrainz artist under two spellings ("Chvrches" on one album, "CHVRCHES ft." tags on another) is one artist.
        by_mbid = {}
        for r in sorted(out.values(), key=lambda r: -r["tracks"]):
            if not r.get("mbid"):
                continue
            main = by_mbid.setdefault(r["mbid"], r)
            if main is r:
                continue
            for f in ("plays", "tracks"):
                main[f] += r[f]
            main["albums"] += r["albums"]
            main["album_rows"] += r["album_rows"]
            main["rating"] = main["rating"] or r["rating"]
            if r["last_played"] and (not main["last_played"] or r["last_played"] > main["last_played"]):
                main["last_played"] = r["last_played"]
            for sk in [s for s in self.songs if s.startswith(r["key"] + "|")]:   # its songs count under the main spelling too
                self.songs.setdefault(main["key"] + sk[len(r["key"]):], self.songs[sk])
            del out[r["key"]]
        return list(out.values())

    def machine_id(self):
        return self._get("/identity").get("machineIdentifier")

    def thumb(self, path, size=300):
        """Album art through Plex's transcoder, so the browser never needs the token."""
        r = self.s.get(f"{self.url}/photo/:/transcode", params={"width": size, "height": size, "minSize": 1, "url": path},
                       headers={"Accept": "image/*"}, timeout=30)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "image/jpeg")

    def playlists_titled(self, title):
        """Keys of ordinary (not smart) audio playlists with exactly this name."""
        return [str(p["ratingKey"]) for p in self._get("/playlists", playlistType="audio").get("Metadata", [])
                if p.get("title") == title and not p.get("smart") and p.get("ratingKey")]

    def replace_playlist(self, title, rating_keys, machine, old_keys=()):
        """Create an audio playlist holding these albums or tracks, then remove the ones it replaces.
        The new one is made first, so a failure never leaves you with nothing. Returns the new playlist's key."""
        keys = [str(k) for k in rating_keys]
        uri = lambda part: f"server://{machine}/com.plexapp.plugins.library/library/metadata/{','.join(part)}"
        r = self.s.post(f"{self.url}/playlists", params={"type": "audio", "title": title, "smart": 0, "uri": uri(keys[:200])}, timeout=120)
        r.raise_for_status()
        new = (r.json().get("MediaContainer", {}).get("Metadata") or [{}])[0].get("ratingKey")
        for i in range(200, len(keys), 200):   # long lists go in batches, so the address stays a sensible length
            self.s.put(f"{self.url}/playlists/{new}/items", params={"uri": uri(keys[i:i + 200])}, timeout=120).raise_for_status()
        for old in old_keys:
            if new and str(old) != str(new):
                try:
                    self.s.delete(f"{self.url}/playlists/{old}", timeout=60)
                except requests.RequestException as e:   # already gone, or Plex hiccuped: the new playlist exists either way
                    log.warning("Couldn't remove the previous playlist: %s", e)
        return str(new) if new else None


# ======================================================================= Last.fm

class LastFM:
    BASE = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, key):
        if not key:
            raise APIError("Add a Last.fm API key in Settings")
        self.key = key
        self.s = requests.Session()
        self.s.headers.update(UA)

    def call(self, method, **params):
        for attempt in range(5):
            r = self.s.get(self.BASE, params={"method": method, "api_key": self.key, "format": "json", **params}, timeout=30)
            try:
                d = r.json()
            except ValueError:
                d = {}
            err = d.get("error")
            if err == 29 or r.status_code == 429:          # rate limited
                time.sleep(2 + attempt * 2)
                continue
            if err in (10, 26):
                raise APIError("Last.fm rejected the API key (since December 1, 2026 a key needs Last.fm Pro)")
            if err == 6:                                   # artist/user not found
                return None
            if r.status_code >= 500 or err in (8, 11, 16):
                time.sleep(1 + attempt)
                continue
            if err:
                raise APIError(f"Last.fm: {d.get('message')}")
            return d
        raise APIError("Last.fm kept timing out")

    def artist(self, name, mbid=None):
        """Everything we need about one artist, in one cacheable dict."""
        q = {"mbid": mbid} if mbid else {"artist": name, "autocorrect": 1}
        info = self.call("artist.getInfo", **q) or (self.call("artist.getInfo", artist=name, autocorrect=1) if mbid else None)
        if not info:
            return {"missing": True, "name": name}
        a = info.get("artist", {})
        tags = self.call("artist.getTopTags", artist=a.get("name") or name, autocorrect=1) or {}
        sim = self.call("artist.getSimilar", artist=a.get("name") or name, autocorrect=1, limit=100) or {}
        bio = re.sub(r"<a href=.*?</a>\.?", "", (a.get("bio") or {}).get("summary") or "").strip()
        return {
            "name": a.get("name") or name, "mbid": a.get("mbid") or mbid,
            "listeners": int((a.get("stats") or {}).get("listeners") or 0),
            "playcount": int((a.get("stats") or {}).get("playcount") or 0),
            "bio": bio, "url": a.get("url"),
            "tags": [(t["name"].lower(), int(t.get("count") or 0)) for t in (tags.get("toptags") or {}).get("tag", [])[:20] if t.get("count")],
            "similar": [(s["name"], float(s.get("match") or 0)) for s in (sim.get("similarartists") or {}).get("artist", [])],
            "v": 1,
        }

    def user_top_artists(self, user, limit=1000):
        out, page = [], 1
        while len(out) < limit:
            d = self.call("user.getTopArtists", user=user, period="overall", limit=500, page=page)
            if not d:
                raise APIError(f"Last.fm user '{user}' not found")
            arts = (d.get("topartists") or {}).get("artist", [])
            out += [{"key": artist_key(a["name"]), "name": a["name"], "plays": int(a.get("playcount") or 0), "rank": len(out) + i + 1}
                    for i, a in enumerate(arts)]
            if len(arts) < 500:
                break
            page += 1
        return out[:limit]


# ======================================================================= ListenBrainz

def listenbrainz_top_artists(user, limit=1000, rng="all_time"):
    """Top artists for a time range (all_time, half_yearly, quarter, month…). Rows carry MusicBrainz ids when known."""
    r = requests.get(f"https://api.listenbrainz.org/1/stats/user/{urllib.parse.quote(user)}/artists",
                     params={"range": rng, "count": min(limit, 1000)}, headers=UA, timeout=30)
    if r.status_code == 404:
        raise APIError(f"ListenBrainz user '{user}' not found")
    if r.status_code == 204:
        return []   # stats not calculated yet
    r.raise_for_status()
    arts = (r.json().get("payload") or {}).get("artists", [])
    return [{"key": artist_key(a["artist_name"]), "name": a["artist_name"], "plays": a.get("listen_count", 0), "rank": i + 1,
             "mbid": a.get("artist_mbid") or next(iter(a.get("artist_mbids") or []), None)}
            for i, a in enumerate(arts)]


# ======================================================================= Spotify

# Spotify only sends people back to https:// or 127.0.0.1 addresses, which a home server usually isn't. This public,
# static page (source: spotify-helper/ in this repo) forwards the browser back to the Recordhound that started the
# sign-in, so connecting is one click. It never sees anything useful: the code is worthless without the PKCE verifier,
# which only this server holds.
SPOTIFY_HELPER = "https://shaderf.github.io/recordhound-auth/"
SPOTIFY_LOOPBACK = "http://127.0.0.1:8789/callback"   # the copy-and-paste way, from before the helper page
SPOTIFY_SCOPES = "user-top-read user-library-read user-follow-read user-modify-playback-state user-read-playback-state"
PLAY_SCOPES = {"user-modify-playback-state", "user-read-playback-state"}   # Play on Spotify; added after the first release


class SpotifyNoDevice(APIError):
    """No Spotify app is open anywhere to play on."""


def spotify_redirect(setting=""):
    """The redirect address Spotify sends people back to: your own setting, or the helper page."""
    return (setting or "").strip() or SPOTIFY_HELPER


def spotify_auth_url(client_id, redirect=SPOTIFY_HELPER, state=None):
    """PKCE: no client secret needed. Returns (url, verifier)."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    q = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SPOTIFY_SCOPES,
         "code_challenge_method": "S256", "code_challenge": challenge, **({"state": state} if state else {})}
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(q), verifier


def spotify_pasted(pasted):
    """(code, state) from the address the browser landed on, or a bare code. Raises if Spotify said no."""
    text = (pasted or "").strip()
    if "?" in text or "=" in text:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(text).query if "?" in text else text)
        if qs.get("error"):
            raise APIError("Spotify sign-in was cancelled" if qs["error"][0] == "access_denied" else f"Spotify said: {qs['error'][0]}")
        return qs.get("code", [""])[0], qs.get("state", [None])[0]
    return text, None


def spotify_exchange(client_id, code, verifier, redirect):
    """Swap the one-time code for a refresh token."""
    if not code:
        raise APIError("That link doesn't have a code in it. Copy the whole address from the page Spotify sent you to.")
    r = requests.post("https://accounts.spotify.com/api/token", timeout=30, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier})
    if r.status_code != 200:
        try:
            why = r.json().get("error_description")
        except ValueError:
            why = None
        raise APIError(f"Spotify didn't accept that code ({why or r.status_code}). Try connecting again.")
    return r.json()["refresh_token"]


class Spotify:
    def __init__(self, client_id, refresh_token, on_new_refresh=None):
        if not (client_id and refresh_token):
            raise APIError("Spotify isn't connected")
        r = requests.post("https://accounts.spotify.com/api/token", timeout=30, data={
            "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id})
        if r.status_code != 200:
            raise APIError("Spotify sign-in expired. Reconnect it in Settings (Spotify ends connections after six months).")
        d = r.json()
        if d.get("refresh_token") and d["refresh_token"] != refresh_token and on_new_refresh:
            on_new_refresh(d["refresh_token"])
        self.scopes = set((d.get("scope") or "").split())
        self.s = requests.Session()
        self.s.headers.update({**UA, "Authorization": f"Bearer {d['access_token']}"})

    def _get(self, url, **params):
        for attempt in range(4):
            r = self.s.get(url if url.startswith("http") else "https://api.spotify.com/v1" + url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(min(30, int(r.headers.get("Retry-After", 2))))
                continue
            if r.status_code == 403:
                raise APIError("Spotify refused (403). Development-mode apps only work for the app owner and up to 5 users added in the Spotify dashboard, and the owner needs Premium.")
            r.raise_for_status()
            return r.json()
        raise APIError("Spotify kept rate-limiting")

    def history(self):
        """Top artists (all three time ranges), followed artists, and artists of saved albums."""
        out = {}

        def add(name, plays=0, rank=None, saved=False):
            k = artist_key(name)
            row = out.setdefault(k, {"key": k, "name": name, "plays": 0, "rank": None, "saved": False})
            row["plays"] += plays
            if rank and (row["rank"] is None or rank < row["rank"]):
                row["rank"] = rank
            row["saved"] = row["saved"] or saved
        for rng, w in (("long_term", 3), ("medium_term", 2), ("short_term", 1)):
            for off in (0, 49):
                d = self._get("/me/top/artists", time_range=rng, limit=50, offset=off)
                for i, a in enumerate(d.get("items", [])):
                    rank = off + i + 1
                    add(a["name"], plays=max(1, (100 - rank)) * w, rank=rank)   # Spotify gives ranks, not counts
                if len(d.get("items", [])) < 50:
                    break
        url = "/me/following"
        params = {"type": "artist", "limit": 50}
        while url:
            d = self._get(url, **params)["artists"]
            for a in d.get("items", []):
                add(a["name"], saved=True)
            url, params = d.get("next"), {}
        url, params = "/me/albums", {"limit": 50}
        while url:
            d = self._get(url, **params)
            for it in d.get("items", []):
                for a in (it.get("album") or {}).get("artists", [])[:1]:
                    add(a["name"], plays=5, saved=True)
            url, params = d.get("next"), {}
        return list(out.values())

    def can_play(self):
        return PLAY_SCOPES <= self.scopes

    def play(self, kind, spotify_id):
        """Start a song (or an album from the top) on the device you're using, or on an open Spotify app.
        Returns the device's name. Spotify only allows this for Premium accounts."""
        devices = self._get("/me/player/devices").get("devices", [])
        dev = (next((d for d in devices if d.get("is_active")), None) or next((d for d in devices if d.get("type") == "Computer"), None)
               or (devices[0] if devices else None))
        if not dev:
            raise SpotifyNoDevice("No Spotify app is open")
        body = {"uris": [f"spotify:track:{spotify_id}"]} if kind == "track" else {"context_uri": f"spotify:album:{spotify_id}"}
        r = self.s.put("https://api.spotify.com/v1/me/player/play", params={"device_id": dev["id"]}, json=body, timeout=30)
        if r.status_code == 404:
            raise SpotifyNoDevice("That Spotify app stopped answering")
        if r.status_code == 403:
            raise APIError("Spotify only lets Premium accounts start playback")
        if r.status_code >= 400:
            raise APIError(f"Spotify couldn't start playback ({r.status_code})")
        return dev.get("name") or "Spotify"

    def liked_tracks(self):
        """Your Liked Songs, newest first: title, every credited artist, and the album it's on."""
        out, url, params = [], "/me/tracks", {"limit": 50}
        while url:
            d = self._get(url, **params)
            for it in d.get("items", []):
                t = it.get("track") or {}
                if not t.get("id") or t.get("is_local"):   # files on your own computer: nothing to look up
                    continue
                al = t.get("album") or {}
                imgs = sorted(al.get("images") or [], key=lambda i: i.get("width") or 0)
                cover = next((i["url"] for i in imgs if (i.get("width") or 0) >= 250), imgs[-1]["url"] if imgs else None)
                out.append({"id": t["id"], "title": t.get("name"), "artists": [a["name"] for a in t.get("artists", []) if a.get("name")],
                            "album": al.get("name"), "album_id": al.get("id"), "album_type": al.get("album_type"),
                            "album_artist": ((al.get("artists") or [{}])[0]).get("name"), "year": (al.get("release_date") or "")[:4],
                            "cover": cover, "added_at": it.get("added_at")})
            url, params = d.get("next"), {}
        return out


def parse_spotify_export(data):
    """Spotify's privacy-page download (either the 'account data' or 'extended streaming history' zip).
    Counts listening time, not taps: each play of 30 seconds or more adds its length in full-song
    equivalents (3 minutes = 1, capped at 1 per play), so skimming ten songs counts far less than hearing them."""
    plays = {}
    z = zipfile.ZipFile(io.BytesIO(data))
    files = [n for n in z.namelist() if n.lower().endswith(".json") and ("streaminghistory" in n.lower().replace("_", "") or "streaming_history_audio" in n.lower())]
    if not files:
        raise APIError("No streaming history in that zip. Use the file from spotify.com/account/privacy.")
    from .security import MAX_UNZIPPED, zip_read
    budget = [MAX_UNZIPPED * 3]   # extended history is large: ~1 KB per play
    for n in files:
        for e in json.loads(zip_read(z, n, budget).decode("utf-8")):
            name = e.get("master_metadata_album_artist_name") or e.get("artistName")
            ms = e.get("ms_played") or e.get("msPlayed") or 0
            if name and ms >= 30000:
                k = artist_key(name)
                p = plays.setdefault(k, {"key": k, "name": name, "plays": 0.0})
                p["plays"] += min(ms / 180000, 1.0)
    return _ranked(plays)


def _ranked(plays):
    rows = sorted(plays.values(), key=lambda r: -r["plays"])
    for i, r in enumerate(rows):
        r["plays"] = round(r["plays"], 1)
        r["rank"] = i + 1
    return rows


_YT_CHANNEL_JUNK = re.compile(r"\s*(?:-\s*Topic|VEVO|\s-\s*Official)\s*$", re.I)
_YT_CELL = re.compile(r'class="outer-cell', re.I)
_YT_HEADER = re.compile(r'mdl-typography--title">\s*(.*?)\s*<br', re.I | re.S)
_YT_LINK = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.I | re.S)


def _yt_artist(channel):
    """YouTube Music plays are credited to the artist's auto-made channel ("Radiohead - Topic") or their own
    ("RadioheadVEVO", "Radiohead")."""
    name = _YT_CHANNEL_JUNK.sub("", html.unescape(channel or "")).strip()
    return name or None


def _yt_plays_json(data):
    """Takeout's watch-history.json: a list of entries, one per play."""
    try:
        entries = json.loads(data.decode("utf-8-sig"))
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    out = []
    for e in entries:
        if not isinstance(e, dict) or e.get("details"):   # details mark ads ("From Google Ads")
            continue
        music = e.get("header") == "YouTube Music" or "music.youtube.com" in (e.get("titleUrl") or "")
        subs = e.get("subtitles") or []
        if music and subs and isinstance(subs[0], dict):
            out.append(subs[0].get("name"))
    return out


def _yt_plays_html(data):
    """Takeout's watch-history.html (the default format): one "outer-cell" block per play, headed by the
    product, then links to the video and to the channel."""
    text = data.decode("utf-8", "replace")
    out = []
    for cell in _YT_CELL.split(text)[1:]:
        head = _YT_HEADER.search(cell)
        body = cell.split("content-cell", 2)
        links = _YT_LINK.findall(body[1] if len(body) > 1 else "")   # not the footer's "control these settings here" link
        if "Google Ads" in cell or len(links) < 2:   # removed videos have no channel link
            continue
        music = (head and html.unescape(head.group(1)).strip() == "YouTube Music") or "music.youtube.com" in links[0][0]
        if music:
            out.append(re.sub(r"<[^>]+>", "", links[1][1]))
    return out


def parse_youtube_music(data):
    """Google Takeout's YouTube history: the whole zip, or watch-history.json / .html on its own. Only plays
    in YouTube Music count; regular YouTube videos are left out. Takeout has no listening time, so every play
    counts as one."""
    from .security import MAX_UNZIPPED, zip_read
    files = []
    if data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        budget = [MAX_UNZIPPED]
        # watch history only; folder and file names are translated, so skip what's clearly something else
        for n in z.namelist():
            low = n.lower()
            if low.endswith((".json", ".html")) and "youtube" in low and "search" not in low and "comment" not in low:
                files.append(zip_read(z, n, budget))
    else:
        files.append(data)
    names, found = [], False
    for f in files:
        got = _yt_plays_json(f) if f.lstrip()[:1] in (b"[", b"{", b"\xef") else _yt_plays_html(f)
        if got is not None:
            found = found or bool(got)
            names += got
    if not found:
        raise APIError("No YouTube Music plays in that file. Use the watch history from Google Takeout "
                       "(YouTube and YouTube Music → history).")
    plays = {}
    for raw in names:
        name = _yt_artist(raw)
        if name:
            k = artist_key(name)
            p = plays.setdefault(k, {"key": k, "name": name, "plays": 0.0})
            p["plays"] += 1
    return _ranked(plays)


# ======================================================================= Deezer (art, albums, previews; no key needed)

class Deezer:
    BASE = "https://api.deezer.com"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)

    def _get(self, path, **params):
        """Raises when Deezer can't be reached or is over its limit, so callers never mistake
        an outage for "no such artist" and remember that."""
        for attempt in range(4):
            r = self.s.get(self.BASE + path, params=params, timeout=20)
            if not r.ok:
                raise APIError(f"Deezer answered {r.status_code}")
            try:
                d = r.json()
            except ValueError:
                raise APIError("Deezer sent an answer that couldn't be read")
            if not isinstance(d, dict):
                return {}
            if (d.get("error") or {}).get("code") == 4:     # quota: 50 requests / 5 s
                if attempt < 3:
                    time.sleep(1.5 + attempt)
                continue
            return d
        raise APIError("Deezer is busy right now")

    def find_artist(self, name):
        want = artist_key(name)
        res = self._get("/search/artist", q=name, limit=10).get("data", [])
        exact = [a for a in res if artist_key(a.get("name")) == want]
        a = max(exact, key=lambda x: x.get("nb_fan") or 0) if exact else None
        if not a:
            return {"missing": True}
        return {"id": a["id"], "name": a["name"], "picture": a.get("picture_xl") or a.get("picture_big"),
                "thumb": a.get("picture_medium"), "fans": a.get("nb_fan")}

    def related(self, artist_id, limit=50):
        """Deezer's related artists, most related first: [(name, fans)]."""
        return [(a["name"], a.get("nb_fan")) for a in self._get(f"/artist/{artist_id}/related", limit=limit).get("data", []) if a.get("name")]

    def search_artists(self, q, limit=30):
        return [{"name": a["name"], "fans": a.get("nb_fan"), "id": a.get("id")}
                for a in self._get("/search/artist", q=q, limit=limit).get("data", []) if a.get("name")]

    def top_tracks(self, artist_id, n=5):
        return [{"title": t.get("title"), "preview": t.get("preview"), "album": (t.get("album") or {}).get("title"),
                 "cover": (t.get("album") or {}).get("cover_medium"), "duration": t.get("duration")}
                for t in self._get(f"/artist/{artist_id}/top", limit=n).get("data", []) if t.get("preview")]

    def album_tracks(self, album_id):
        """An album's track list, in order, with 30-second previews."""
        return [{"n": t.get("track_position"), "disc": t.get("disk_number") or 1, "title": t.get("title"), "duration": t.get("duration"),
                 "preview": t.get("preview") or None, "artist": (t.get("artist") or {}).get("name")}
                for t in self._get(f"/album/{album_id}/tracks", limit=200).get("data", [])]

    def albums(self, artist_id):
        out = []
        for a in self._get(f"/artist/{artist_id}/albums", limit=200).get("data", []):
            out.append({"id": a["id"], "title": a.get("title"), "cover": a.get("cover_medium"), "date": a.get("release_date"),
                        "type": a.get("record_type"), "fans": a.get("fans")})
        return out


# ======================================================================= Free artist data (no Last.fm)

def _lucene(s):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s or "")


def parse_similar(d):
    """ListenBrainz Labs answers either a flat list of artists or a list of sections with "data"."""
    rows = []
    for item in d if isinstance(d, list) else []:
        if not isinstance(item, dict):
            continue
        if "data" in item and isinstance(item["data"], list):
            rows += parse_similar(item["data"])
        elif item.get("artist_mbid") and item.get("name"):
            rows.append({"mbid": item["artist_mbid"], "name": item["name"], "score": float(item.get("score") or 0)})
    return rows


class OpenArtists:
    """Artist data from free sources, for when there's no working Last.fm key (Last.fm keys need Pro from
    December 1, 2026). MusicBrainz finds the artist, ListenBrainz gives similar artists (from what its users
    listen to) and MusicBrainz tags, Deezer adds its own related artists and fan counts, and Wikipedia the bio."""
    MB = "https://musicbrainz.org/ws/2"
    LB = "https://api.listenbrainz.org/1"
    LABS = "https://labs.api.listenbrainz.org"
    ALGO = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
    _mb_lock = threading.Lock()   # MusicBrainz allows one request a second, across every thread
    _mb_last = [0.0]

    def __init__(self, deezer=None):
        self.s = requests.Session()
        self.s.headers.update({**UA, "Accept": "application/json"})
        self.dz = deezer or Deezer()
        self._meta = {}   # filled in batches by prefetch()

    def _get(self, url, **params):
        for attempt in range(5):
            r = self.s.get(url, params=params, timeout=30)
            if r.status_code in (429, 503):
                wait = r.headers.get("X-RateLimit-Reset-In") or r.headers.get("Retry-After")
                try:
                    wait = float(wait)
                except (TypeError, ValueError):
                    wait = 2 + attempt * 2
                time.sleep(min(30.0, max(1.0, wait)))
                continue
            if r.status_code in (204, 400, 404):
                return None
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                return None
        raise APIError("MusicBrainz or ListenBrainz kept asking us to slow down. Try again later.")

    @staticmethod
    def _mb_wait():
        with OpenArtists._mb_lock:
            wait = OpenArtists._mb_last[0] + 1.05 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            OpenArtists._mb_last[0] = time.monotonic()

    def mbid(self, name):
        """MusicBrainz id for a name, only when one artist's name matches exactly."""
        self._mb_wait()
        d = self._get(f"{self.MB}/artist", query=f'artist:"{_lucene(name)}"', fmt="json", limit=10) or {}
        want = artist_key(name)
        hits = []
        for a in d.get("artists", []):
            names = [a.get("name"), a.get("sort-name")] + [x.get("name") for x in a.get("aliases") or []]
            if any(artist_key(n) == want for n in names if n):
                hits.append(a)
        return max(hits, key=lambda a: int(a.get("score") or 0))["id"] if hits else None

    def similar(self, mbid):
        return parse_similar(self._get(f"{self.LABS}/similar-artists/json", artist_mbids=mbid, algorithm=self.ALGO))

    def meta(self, mbid):
        if mbid in self._meta:
            return self._meta[mbid]
        d = self._get(f"{self.LB}/metadata/artist/", artist_mbids=mbid, inc="tag")
        return d[0] if isinstance(d, list) and d else {}

    def prefetch(self, mbids, chunk=50):
        """Tags and links for many artists, fifty per request instead of one each."""
        todo = [m for m in dict.fromkeys(m for m in mbids if m) if m not in self._meta]
        for i in range(0, len(todo), chunk):
            part = todo[i:i + chunk]
            try:
                d = self._get(f"{self.LB}/metadata/artist/", artist_mbids=",".join(part), inc="tag") or []
            except Exception as e:
                log.warning("ListenBrainz batch lookup failed: %s", e)
                continue
            got = {(x.get("artist_mbid") or x.get("mbid")): x for x in d if isinstance(x, dict)}
            for m in part:
                self._meta[m] = got.get(m, {})

    def release_groups(self, mbid):
        """Albums and EPs on MusicBrainz, including announced ones: [{id, title, date, type}]."""
        self._mb_wait()
        d = self._get(f"{self.MB}/release-group", artist=mbid, type="album|ep", fmt="json", limit=100) or {}
        out = []
        for g in d.get("release-groups", []):
            if g.get("secondary-types"):   # live, compilation, remix…
                continue
            out.append({"id": g["id"], "title": g.get("title"), "date": g.get("first-release-date") or "",
                        "type": (g.get("primary-type") or "").lower()})
        return out

    def artist(self, name, mbid=None):
        """Same shape as LastFM.artist, plus fans (Deezer), similar_mbids and wikidata."""
        mbid = mbid or self.mbid(name)
        try:
            dz = self.dz.find_artist(name)
        except Exception:
            if not mbid:
                raise   # nothing to go on: fail this lookup rather than remember the artist as unknown
            dz = {"missing": True}
        if not mbid and dz.get("missing"):
            return {"missing": True, "name": name}
        meta = self.meta(mbid) if mbid else {}

        tags = {}
        for t in ((meta.get("tag") or {}).get("artist") or []):
            tg = (t.get("tag") or "").strip().lower()
            if tg and (t.get("count") or 0) > 0:
                tags[tg] = max(tags.get(tg, 0), int(t["count"]))

        # similar artists: ListenBrainz (listening sessions) and Deezer (related), merged by name
        sim, mbids = {}, {}
        rows = self.similar(mbid) if mbid else []
        rows = [r for r in rows if r["mbid"] != mbid]
        top = max((r["score"] for r in rows), default=0) or 1
        for i, r in enumerate(rows):
            k = artist_key(r["name"])
            m = 0.5 * (r["score"] / top) + 0.5 * (1 - i / max(len(rows), 1))
            if m > sim.get(k, (None, 0))[1]:
                sim[k] = (r["name"], m)
            mbids.setdefault(k, r["mbid"])
        if not dz.get("missing"):
            try:
                rel = self.dz.related(dz["id"])
            except Exception:
                rel = []
            for i, (n, _fans) in enumerate(rel):
                k = artist_key(n)
                m = 0.9 - 0.7 * i / max(len(rel), 1)
                if k in sim:   # both services agree: a stronger signal
                    sim[k] = (sim[k][0], min(1.0, max(sim[k][1], m) + 0.1))
                else:
                    sim[k] = (n, m)
        own = artist_key(name)
        similar = sorted(((n, round(m, 4)) for k, (n, m) in sim.items() if k != own), key=lambda x: -x[1])[:100]
        wikidata = ((meta.get("rels") or {}).get("wikidata") or "").rstrip("/").rsplit("/", 1)[-1] or None
        similar_keys = {artist_key(n) for n, _ in similar}
        return {
            "name": meta.get("name") or (None if dz.get("missing") else dz.get("name")) or name, "mbid": mbid,
            "listeners": None, "playcount": None, "fans": None if dz.get("missing") else dz.get("fans"),
            "bio": "", "url": None, "wikidata": wikidata,
            "tags": sorted(tags.items(), key=lambda kv: -kv[1])[:20],
            "similar": similar, "similar_mbids": {k: v for k, v in mbids.items() if k in similar_keys},
            "src": "open", "v": 2,
        }

    def bio(self, wikidata):
        """First paragraphs of the English Wikipedia article, found through Wikidata."""
        if not wikidata or not re.fullmatch(r"Q\d+", wikidata):
            return None
        d = self._get("https://www.wikidata.org/w/api.php", action="wbgetentities", ids=wikidata, props="sitelinks",
                      sitefilter="enwiki", format="json") or {}
        ent = next(iter((d.get("entities") or {}).values()), {})
        title = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
        if not title:
            return None
        s = self._get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"), safe=""))
        if not s or s.get("type") == "disambiguation" or not s.get("extract"):
            return None
        return {"bio": s["extract"], "bio_url": ((s.get("content_urls") or {}).get("desktop") or {}).get("page")}


LASTFM_PRO_NOTE = "Last.fm rejected the API key. Since December 1, 2026 a key only works while its account has Last.fm Pro."
_probe = {}


def lastfm_works(key, max_age=3600):
    """True unless Last.fm says the key itself is no good. Checked at most once an hour."""
    hit = _probe.get(key)
    if hit and time.time() - hit[1] < max_age:
        return hit[0]
    try:
        LastFM(key).call("artist.getInfo", artist="Radiohead")
        ok = True
    except APIError as e:
        ok = "API key" not in str(e)
    except requests.RequestException:
        ok = True   # Last.fm being down isn't a reason to switch sources
    _probe[key] = (ok, time.time())
    return ok


def forget_lastfm_check():
    _probe.clear()


def artist_source(lastfm_key, deezer=None):
    """(client, cache prefix, warning). Last.fm while a working key is set, free sources otherwise.
    Each source has its own cache so tags from the two never mix."""
    if lastfm_key:
        if lastfm_works(lastfm_key):
            return LastFM(lastfm_key), "lf:", None
        return OpenArtists(deezer), "ob:", LASTFM_PRO_NOTE + " Using MusicBrainz, ListenBrainz and Deezer instead."
    return OpenArtists(deezer), "ob:", None


# ======================================================================= Lidarr

class Lidarr:
    def __init__(self, url, key):
        if not (url and key):
            raise APIError("Lidarr isn't set up")
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({**UA, "X-Api-Key": key})

    def _get(self, path, **params):
        r = self.s.get(f"{self.url}/api/v1{path}", params=params, timeout=60)
        if r.status_code == 401:
            raise APIError("Lidarr rejected the API key")
        r.raise_for_status()
        return r.json()

    def options(self):
        return {"profiles": [{"id": p["id"], "name": p["name"]} for p in self._get("/qualityprofile")],
                "metadata": [{"id": p["id"], "name": p["name"]} for p in self._get("/metadataprofile")],
                "roots": [{"path": r["path"], "free": r.get("freeSpace")} for r in self._get("/rootfolder")]}

    def artist_keys(self):
        return set(self.artist_mbids())

    def artist_mbids(self):
        """{artist key: MusicBrainz id} for every artist in Lidarr."""
        return {artist_key(a.get("artistName")): a.get("foreignArtistId") for a in self._get("/artist")}

    def add(self, name, mbid, quality_id, metadata_id, root, monitor="all", search=True):
        found = self._get("/artist/lookup", term=f"lidarr:{mbid}") if mbid else []
        if not found:
            found = [a for a in self._get("/artist/lookup", term=name) if artist_key(a.get("artistName")) == artist_key(name)]
        if not found:
            raise APIError(f"Lidarr couldn't find {name} on MusicBrainz")
        a = found[0]
        if a.get("id"):
            raise APIError(f"{a.get('artistName')} is already in Lidarr")
        body = {**a, "qualityProfileId": quality_id, "metadataProfileId": metadata_id, "rootFolderPath": root,
                "monitored": True, "monitorNewItems": "all" if monitor in ("all", "future") else "none",
                "addOptions": {"monitor": monitor, "searchForMissingAlbums": search}}
        return self._send("post", "/artist", body)

    def _send(self, method, path, body):
        r = self.s.request(method, f"{self.url}/api/v1{path}", json=body, timeout=60)
        if r.status_code == 401:
            raise APIError("Lidarr rejected the API key")
        if r.status_code >= 400:
            try:
                d = r.json()
                msg = d[0].get("errorMessage") if isinstance(d, list) else d.get("message")
            except Exception:
                msg = r.text[:200]
            raise APIError(f"Lidarr: {msg}")
        return r.json() if r.content else {}

    def add_album(self, artist, title, quality_id, metadata_id, root):
        """Get one album: monitor it and start a search. An artist Lidarr doesn't have yet is added with only this
        album monitored, so one liked song doesn't pull in a whole discography. Returns the album's title in Lidarr."""
        want_a, want_t = credit_keys(artist), song_key(title)
        by_artist = lambda found: [al for al in found if artist_key((al.get("artist") or {}).get("artistName")) in want_a]
        cands = by_artist(self._get("/album/lookup", term=f"{artist} {title}")) or by_artist(self._get("/album/lookup", term=title))
        al = (next((x for x in cands if album_key(x.get("title")) == album_key(title)), None)
              or next((x for x in cands if song_key(x.get("title")) == want_t), None))
        if not al:
            raise APIError(f"Lidarr couldn't find {title} by {artist} on MusicBrainz")
        if al.get("id"):   # Lidarr already knows this album
            self._send("put", "/album/monitor", {"albumIds": [al["id"]], "monitored": True})
            self._send("post", "/command", {"name": "AlbumSearch", "albumIds": [al["id"]]})
            return al.get("title")
        a = al.get("artist") or {}
        if not a.get("id"):
            a = {**a, "qualityProfileId": quality_id, "metadataProfileId": metadata_id, "rootFolderPath": root, "monitored": True,
                 "monitorNewItems": "none", "addOptions": {"monitor": "none", "searchForMissingAlbums": False}}
        body = {**al, "artist": a, "monitored": True, "anyReleaseOk": True, "addOptions": {"searchForNewAlbum": True}}
        if a.get("id"):
            body["artistId"] = a["id"]
        self._send("post", "/album", body)
        return al.get("title")
