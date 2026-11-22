"""Settings: built-in defaults, overridden by environment variables, overridden by
whatever was saved from the Settings page (DATA_DIR/settings.json).

Other modules read values as module attributes (config.PLEX_URL), so saving new
settings takes effect immediately without a restart.
"""
import json
import os
import sys
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")

# name: (type, default, secret)
FIELDS = {
    "PLEX_URL": (str, "", False),
    "PLEX_TOKEN": (str, "", True),
    "PLEX_LIBRARIES": (list, [], False),     # empty = every music library
    "LASTFM_API_KEY": (str, "", True),       # optional; needs Last.fm Pro from Dec 1 2026. Without one, artist data comes from MusicBrainz, ListenBrainz and Deezer
    "LASTFM_USERNAME": (str, "", False),     # optional: your scrobble history (needs the key)
    "LISTENBRAINZ_USERNAME": (str, "", False),
    "SPOTIFY_CLIENT_ID": (str, "", True),
    "SPOTIFY_REFRESH_TOKEN": (str, "", True),  # set by "Connect Spotify"
    "SPOTIFY_CONNECTED_AT": (int, 0, False),
    "SPOTIFY_REDIRECT_URI": (str, "", False),
    "SPOTIFY_PLAY": (bool, True, False),     # play buttons start the full song in Spotify when connected; off = 30-second previews  # empty = the one-click helper page; see clients.spotify_redirect()
    "LIDARR_URL": (str, "", False),
    "LIDARR_API_KEY": (str, "", True),
    "SYNC_HOUR": (int, 4, False),            # -1 disables the nightly sync
    "SEED_ARTISTS": (int, 150, False),
    "CANDIDATE_POOL": (int, 1500, False),    # how many candidate artists to look up and score
    "MAX_RECS": (int, 0, False),             # 0 = keep every scored artist
    "OWNED_WEIGHT": (float, 0.6, False),     # artists you own but rarely play
    "RECENT_WEIGHT": (float, 0.5, False),    # extra pull for artists you've played in the last six months
    "MIN_PLAYS": (int, 3, False),            # listening outside Plex below this (Spotify, ListenBrainz, Last.fm) doesn't shape your taste
    "MIN_LISTENERS": (int, 2000, False),     # skip artists with fewer Last.fm listeners (with a Last.fm key)
    "MIN_FANS": (int, 200, False),           # skip artists with fewer Deezer fans (without one)
    "NOSEED_LIBRARIES": (list, [], False),
    "NOSEED_TAGS": (list, [], False),        # e.g. children's music, christmas
    "AUTH_USERNAME": (str, "", False),
    "AUTH_PASSWORD_HASH": (str, "", True),
    "AUTH_SECRET": (str, "", True),
    "PLEX_PLAYLIST": (str, "Recordhound: Unplayed albums", False),
    "API_WORKERS": (int, 4, False),
    "CACHE_DAYS": (int, 30, False),
}
URL_FIELDS = {"PLEX_URL", "LIDARR_URL"}
_lock = threading.Lock()
_mod = sys.modules[__name__]


def _settings_path():
    return os.path.join(DATA_DIR, "settings.json")


def coerce(name, value):
    typ = FIELDS[name][0]
    if typ is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if typ is list:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [v.strip() for v in str(value).split(",") if v.strip()]
    if typ is int:
        return int(float(value))
    if typ is float:
        return float(value)
    v = str(value).strip()
    return v.rstrip("/") if name in URL_FIELDS else v


def _saved():
    try:
        with open(_settings_path()) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def sources():
    """Where each value currently comes from: default / env / saved."""
    saved = _saved()
    return {n: "saved" if n in saved else "env" if n in os.environ else "default" for n in FIELDS}


def load():
    saved = _saved()
    for name, (typ, default, _) in FIELDS.items():
        value = default
        if name in os.environ:
            try:
                value = coerce(name, os.environ[name])
            except ValueError:
                pass
        if name in saved:
            try:
                value = coerce(name, saved[name])
            except ValueError:
                pass
        setattr(_mod, name, value)


def save(updates):
    """Persist changed values. Returns the list of fields that changed."""
    with _lock:
        saved = _saved()
        changed = []
        for name, value in updates.items():
            if name not in FIELDS:
                continue
            v = coerce(name, value)
            if v != getattr(_mod, name):
                changed.append(name)
            saved[name] = v
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _settings_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(saved, f, indent=2)
        os.chmod(tmp, 0o600)   # holds API keys and tokens
        os.replace(tmp, _settings_path())
        load()
        return changed


def mask(value):
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 6 else "•" * 8


def public():
    """Settings for the UI, with secrets masked."""
    out = {}
    for name, (_, _, secret) in FIELDS.items():
        v = getattr(_mod, name)
        out[name] = mask(v) if secret else v
    return out


def is_masked(value):
    return isinstance(value, str) and value.startswith("•" * 8)


def configured():
    return bool(getattr(_mod, "PLEX_URL") and getattr(_mod, "PLEX_TOKEN"))


load()
