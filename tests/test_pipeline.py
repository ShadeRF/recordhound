"""End-to-end with fake Plex / Last.fm / Deezer, no network.

Universe: a shoegaze scene and a country scene. The library is mostly shoegaze, heavily
played and rated; one country artist is rated 2/10. Shoegaze picks should win, owned
artists never appear as picks, and artists you only stream land in their own list.
"""
import io
import json
import os
import random
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import config, db, engine  # noqa: E402
from app.clients import artist_key, parse_spotify_export, album_key  # noqa: E402
from app.sync import Syncer  # noqa: E402

config.DATA_DIR = os.environ["DATA_DIR"]
config.MIN_LISTENERS = 0
random.seed(3)

GAZE = [f"Gaze Band {i}" for i in range(40)]
COUNTRY = [f"Country Act {i}" for i in range(40)]
SCENE = {n: "shoegaze" for n in GAZE} | {n: "country" for n in COUNTRY}


def info(name):
    scene = SCENE[name]
    pool = GAZE if scene == "shoegaze" else COUNTRY
    others = random.Random(name).sample([x for x in pool if x != name], 20)
    tags = [("shoegaze", 100), ("dream pop", 60), ("noise pop", 30)] if scene == "shoegaze" else [("country", 100), ("americana", 70), ("seen live", 50)]
    return {"name": name, "mbid": None, "listeners": 50_000 + hash(name) % 400_000, "playcount": 1, "bio": "…", "url": "x",
            "tags": tags, "similar": [(o, 1 - i / 25) for i, o in enumerate(others)], "v": 1}


class FakeLastFM:
    calls = 0

    def artist(self, name, mbid=None):
        FakeLastFM.calls += 1
        return info(name) if name in SCENE else {"missing": True, "name": name}


OWNED = GAZE[:12] + [COUNTRY[0]]


class FakePlex:
    def artists(self, step=lambda *a: None):
        rows = []
        for n in OWNED:
            rows.append({"key": artist_key(n), "name": n, "mbid": None, "plex_key": "1", "thumb": None, "section": "1",
                         "plays": 0 if n == GAZE[11] else 200, "tracks": 30, "albums": ["First LP", "Second LP (Deluxe Edition)"],
                         "rating": 2.0 if n == COUNTRY[0] else (9.0 if n == GAZE[0] else None), "last_played": 1, "added_at": 1})
        return rows

    def all_sections(self):
        return [{"key": "1", "title": "Music", "type": "artist"}]

    def machine_id(self):
        return "m"


class FakeDeezer:
    def find_artist(self, name):
        return {"id": 1, "name": name, "thumb": "t", "picture": "p"}

    def albums(self, _id):
        return [{"id": 1, "title": "First LP", "type": "album", "date": "2001-01-01", "fans": 10},
                {"id": 2, "title": "Second LP", "type": "album", "date": "2003-01-01", "fans": 9},
                {"id": 3, "title": "Third LP", "type": "album", "date": "2005-01-01", "fans": 8},
                {"id": 4, "title": "Brand New", "type": "album", "date": "2099-01-01", "fans": 1}]


def test_keys():
    assert artist_key("The Beatles") == artist_key("Beatles") == artist_key("beatles")
    assert artist_key("Simon & Garfunkel") == artist_key("Simon and Garfunkel")
    assert artist_key("Björk") == "bjork"
    assert album_key("Second LP (Deluxe Edition)") == album_key("Second LP")


def test_spotify_export():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2024.json", json.dumps(
            [{"master_metadata_album_artist_name": "Slowdive", "ms_played": 200000}] * 3 +
            [{"master_metadata_album_artist_name": "Slowdive", "ms_played": 5000}]))
        z.writestr("MyData/StreamingHistory_music_0.json", json.dumps([{"artistName": "Ride", "msPlayed": 90000}]))
    rows = {r["key"]: r for r in parse_spotify_export(buf.getvalue())}
    assert rows["slowdive"]["plays"] == 3 and rows["ride"]["plays"] == 0.5   # 90 s is half a song


def test_pipeline():
    db.init()
    stream_only = GAZE[30]
    s = Syncer(lambda: FakePlex(), lambda: FakeLastFM(), FakeDeezer, artist_factory=lambda: (FakeLastFM(), "lf:", None), sources={
        "lastfm": [{"key": artist_key(stream_only), "name": stream_only, "plays": 900, "rank": 1}],
        "spotify": [{"key": artist_key(GAZE[31]), "name": GAZE[31], "plays": 50, "rank": 3, "saved": True}]})
    s.run(full=True)
    assert s.state["error"] is None, s.state
    recs = db.recs("new")
    keys = [r["key"] for r in recs]
    owned = {artist_key(n) for n in OWNED}
    assert recs and not set(keys) & owned, "owned artist recommended"
    assert all(SCENE[r["name"]] == "shoegaze" for r in recs[:10]), [r["name"] for r in recs[:10]]
    assert recs[0]["because"] and "shoegaze" in recs[0]["matches"]
    assert "seen live" not in recs[-1]["tags"]
    stream = {r["key"] for r in db.recs("stream")}
    assert artist_key(stream_only) in stream and artist_key(stream_only) not in keys
    assert artist_key(GAZE[31]) in stream
    seeds = db.get_meta("seeds")
    assert seeds[artist_key(COUNTRY[0])]["w"] < 0 and seeds[artist_key(GAZE[0])]["kind"] == "rated"
    assert seeds[artist_key(GAZE[11])]["kind"] == "owned"
    assert db.get_meta("taste")[0]["tag"] == "shoegaze"
    gaps = db.get_meta("gaps")["groups"]
    g = next(x for x in gaps if x["key"] == artist_key(GAZE[0]))
    assert [a["title"] for a in g["albums"]] == ["Third LP"] and g["owned"] == 2, g
    rel = db.get_meta("releases")["items"]
    assert any(a["title"] == "Brand New" for a in rel)

    # cached: a re-score doesn't look anything up again
    before = FakeLastFM.calls
    s.run(full=False)
    assert FakeLastFM.calls == before

    # hiding removes; don't-learn drops a seed; liking adds one
    db.set_feedback(keys[0], "dismissed", "x")
    db.set_noseed(artist_key(GAZE[0]), "x")
    s.run(full=False)
    assert keys[0] not in [r["key"] for r in db.recs("new")]
    assert artist_key(GAZE[0]) not in db.get_meta("seeds")
    print("OK", [r["name"] for r in recs[:5]], recs[0]["because"][0], recs[0]["matches"])


def test_api():
    from fastapi.testclient import TestClient
    import app.main as m
    m._dz = FakeDeezer()
    m.artist_source = lambda key, dz=None: (FakeLastFM(), "lf:", None)
    config.LASTFM_API_KEY = "k"
    c = TestClient(m.app)
    h = c.get("/api/home").json()
    titles = [r["title"] for r in h["rows"]]
    assert titles[0] == "Top picks for you" and "You stream these, but don't own them" in titles, titles
    r = c.get("/api/recs?tags=shoegaze").json()
    assert r["total"] > 5 and all("shoegaze" in x["tags"] for x in r["items"])
    k = r["items"][0]["key"]
    a = c.get(f"/api/artist/{k}").json()
    assert a["name"] and a["albums"] and a["similar"] and a["because"]
    assert c.post("/api/saved", json={"key": k, "name": a["name"]}).json()["saved"]
    assert c.get("/api/saved").json()["items"][0]["key"] == k
    like = c.get(f"/api/like/{k}").json()
    assert like["items"] and like["items"][0]["score"] >= like["items"][-1]["score"]


if __name__ == "__main__":
    test_keys()
    test_spotify_export()
    test_pipeline()
    test_api()
