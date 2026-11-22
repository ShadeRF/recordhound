"""Recent listening, Your Library, New this week, MusicBrainz releases, home-page variety, batch lookups."""
import os
import sys
import tempfile
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import config, db, engine  # noqa: E402
from app.clients import OpenArtists, artist_key  # noqa: E402

import test_pipeline as tp  # noqa: E402


def test_recent_share_and_boost():
    now = 1_800_000_000
    lib = [{"key": "a", "name": "A", "plays": 50, "rating": None, "albums": ["x"], "last_played": now - 10 * 86400},
           {"key": "b", "name": "B", "plays": 50, "rating": None, "albums": ["x"], "last_played": now - 400 * 86400}]
    flat = engine.build_seeds(lib, {}, {}, now=now)
    boosted = engine.build_seeds(lib, {}, {}, recent={"b": 0}, recent_weight=1.0, now=now)
    assert flat["a"]["w"] == flat["b"]["w"]
    assert boosted["a"]["w"] > boosted["b"]["w"] == flat["b"]["w"]
    assert boosted["a"]["recent"] > 0.9 and boosted["b"]["recent"] == 0
    # ListenBrainz's last six months count even for artists Plex never played
    lb = engine.build_seeds(lib, {}, {}, recent={"b": 300, "a": 1}, recent_weight=1.0, now=now - 10**8)
    assert lb["b"]["recent"] == 1.0 and lb["b"]["w"] > flat["b"]["w"]


def test_varied():
    rows = [{"key": f"k{i}", "score": 1 - i / 100, "because": [{"key": "seedA" if i < 30 else f"s{i}"}]} for i in range(80)]
    a = engine.varied(rows, 24, salt="2026-47")
    assert len(a) == 24 and len({r["key"] for r in a}) == 24
    assert sum(1 for r in a if r["because"][0]["key"] == "seedA") <= 2
    assert a == engine.varied(rows, 24, salt="2026-47")            # same week, same row
    assert [r["key"] for r in a] != [r["key"] for r in engine.varied(rows, 24, salt="2026-48")]
    few = engine.varied(rows[:5], 24, salt="x")                      # not enough to fill: no crash, no repeats
    assert len(few) == 5


def test_unplayed_ranking():
    seeds = {"loved": {"w": 2.0, "name": "Loved", "listen": 0.9, "rating": None, "kind": "played"},
             "meh": {"w": 0.4, "name": "Meh", "listen": 0.0, "rating": None, "kind": "owned"},
             "hated": {"w": -1.0, "name": "Hated", "listen": 0.0, "rating": 2.0, "kind": "rated"}}
    infos = {"loved": {"similar": [("Neighbor", 0.9)], "tags": [("shoegaze", 100)]}, "neighbor": {"tags": [("shoegaze", 100)]}}
    albums = [{"rk": 1, "title": "Played", "artist": "Loved", "artist_key": "loved", "plays": 3},
              {"rk": 2, "title": "Deep one", "artist": "Loved", "artist_key": "loved", "plays": 0},
              {"rk": 3, "title": "Neighbor LP", "artist": "Neighbor", "artist_key": "neighbor", "plays": 0},
              {"rk": 4, "title": "Meh LP", "artist": "Meh", "artist_key": "meh", "plays": 0},
              {"rk": 5, "title": "Bad LP", "artist": "Hated", "artist_key": "hated", "plays": 0},
              {"rk": 6, "title": "Kids LP", "artist": "Kid", "artist_key": "kid", "plays": 0}]
    idf = engine.build_idf(list(infos.values()))
    pos, _ = engine.build_profile(seeds, infos, idf)
    out = engine.unplayed_albums(albums, seeds, infos, pos, idf, noseed={"kid"})
    titles = [a["title"] for a in out]
    assert titles[0] == "Deep one" and "Played" not in titles and "Bad LP" not in titles and "Kids LP" not in titles
    assert titles.index("Neighbor LP") < titles.index("Meh LP")
    assert out[0]["why"] == "You play Loved a lot" and out[0]["key"] == "al:2"
    assert next(a for a in out if a["rk"] == 3)["why"] == "Because you play Loved"


def test_prefetch_and_release_groups():
    calls = []
    oa = OpenArtists(None)

    def fake(url, **p):
        calls.append((url, p))
        if "metadata/artist" in url:
            return [{"artist_mbid": m, "name": m.upper(), "tag": {"artist": [{"tag": "rock", "count": 3}]}} for m in p["artist_mbids"].split(",")]
        if url.endswith("/release-group"):
            return {"release-groups": [{"id": "g1", "title": "New One", "first-release-date": "2026-12-05", "primary-type": "Album"},
                                       {"id": "g2", "title": "Live!", "first-release-date": "2026-11-01", "primary-type": "Album",
                                        "secondary-types": ["Live"]}]}
        raise AssertionError(url)
    oa._get = fake
    OpenArtists._mb_last[0] = 0
    oa.prefetch([f"m{i}" for i in range(120)] + [None, "m1"])
    assert len([c for c in calls if "metadata" in c[0]]) == 3       # 120 ids, 50 per request
    n = len(calls)
    assert oa.meta("m7")["name"] == "M7" and len(calls) == n          # served from the batch
    assert oa.release_groups("x") == [{"id": "g1", "title": "New One", "date": "2026-12-05", "type": "album"}]


class AlbumPlex(tp.FakePlex):
    def artists(self, step=lambda *a: None):
        rows = super().artists(step)
        for i, r in enumerate(rows):
            if r["name"] == tp.GAZE[0]:
                r["mbid"] = "mb-gaze0"   # Plex knows this one's MusicBrainz id
            r["album_rows"] = [{"rk": str(100 + i * 2), "title": "First LP", "year": 2001, "thumb": "/t", "added_at": i, "rating": None, "plays": 5},
                               {"rk": str(101 + i * 2), "title": "Second LP", "year": 2003, "thumb": "/t", "added_at": i, "rating": None, "plays": 0}]
        return rows


def test_pipeline_features(monkeypatch):
    db.init()
    for k in ("first_seen", "unplayed", "plex_albums"):
        db.set_meta(k, None)
    config.MIN_LISTENERS = 0
    soon = (date.today() + timedelta(days=20)).isoformat()
    gaze0 = artist_key(tp.GAZE[0])
    db.cache_put("mbrg:" + gaze0, [{"id": "rg1", "title": "Announced Record", "date": soon, "type": "album"},
                                   {"id": "rg2", "title": "Old Record", "date": "1999-01-01", "type": "album"}])
    db.commit()
    s = tp.Syncer(lambda: AlbumPlex(), lambda: tp.FakeLastFM(), tp.FakeDeezer, artist_factory=lambda: (tp.FakeLastFM(), "lf:", None), sources={})
    s.run(full=True)
    assert s.state["error"] is None, s.state
    # first sync: nothing counts as new this week
    assert all(v == 0 for v in db.get_meta("first_seen").values())
    # Your Library: only never-played albums, from artists you haven't disliked
    un = db.get_meta("unplayed")
    assert un and all(a["title"] == "Second LP" for a in un)
    assert artist_key(tp.COUNTRY[0]) not in {a["artist_key"] for a in un}
    # MusicBrainz adds announced releases, not old ones
    rel = db.get_meta("releases")["items"]
    assert any(a["title"] == "Announced Record" and a["cover"].endswith("rg1/front-250") for a in rel)
    assert not any(a["title"] == "Old Record" for a in rel)

    # a pick that appears later counts as new this week
    first = db.get_meta("first_seen")
    some = next(iter(first))
    first.pop(some)
    db.set_meta("first_seen", first)
    s.run(full=False)
    assert db.get_meta("first_seen")[some] > time.time() - 60

    from fastapi.testclient import TestClient
    import app.main as m
    m._dz = tp.FakeDeezer()
    m.artist_source = lambda key, dz=None: (tp.FakeLastFM(), "lf:", None)
    c = TestClient(m.app)
    h = c.get("/api/home").json()
    assert h["digest"]["new_picks"] == 1 and h["digest"]["upcoming"] >= 1
    ids = [r["id"] for r in h["rows"]]
    assert "unplayed" in ids
    top = h["rows"][0]["items"]
    assert [r["key"] for r in top] == [r["key"] for r in engine.varied(m._visible("new"), 24)]
    assert c.get("/api/recs?fresh=1").json()["total"] == 1
    lib = c.get("/api/library").json()
    assert lib["total"] == len(un) and lib["items"][0]["cover"].startswith("/api/cover/")
    assert c.get("/api/library?q=nothing-matches").json()["total"] == 0
    assert c.get("/api/cover/abc").status_code == 404

    made = {}

    class FakePlexAPI:
        def __init__(self, *a):
            pass

        def machine_id(self):
            return "mach"

        def playlists_titled(self, title):
            return []

        def replace_playlist(self, title, rks, machine, old_keys=()):
            made.update(title=title, rks=rks, machine=machine)
            return "9"
    monkeypatch.setattr(m, "Plex", FakePlexAPI)
    r = c.post("/api/plex/playlist", json={"count": 3}).json()
    assert r["albums"] == 3 and made["rks"] == [a["rk"] for a in un[:3]] and made["title"] == config.PLEX_PLAYLIST


def test_min_plays_outside_plex():
    lib = [{"key": "own", "name": "Own", "plays": 1, "rating": None, "albums": ["x"], "last_played": None}]
    hist = {"once": {"name": "Once", "plays": 1, "sources": {"spotify_export": {"plays": 1}}},
            "often": {"name": "Often", "plays": 40, "sources": {"spotify_export": {"plays": 40}}},
            "top": {"name": "Top", "plays": 2, "sources": {"spotify": {"plays": 2, "rank": 98}}},
            "saved": {"name": "Saved", "plays": 1, "sources": {"listenbrainz": {"plays": 1, "saved": True}}}}
    loose = engine.build_seeds(lib, hist, {}, min_plays=0)
    strict = engine.build_seeds(lib, hist, {}, min_plays=3)
    assert loose["once"]["kind"] == "played" and "once" not in strict          # one listen to check them out: ignored
    assert strict["often"]["kind"] == "played"
    assert strict["top"]["listen"] > 0                                           # Spotify's own top-artist ranks are exempt
    assert strict["saved"]["kind"] == "saved"                                    # saving is deliberate, so it still counts
    assert strict["own"]["listen"] > 0                                           # Plex plays always count
    recent = engine.build_seeds(lib, hist, {}, recent={"once": 1, "often": 40}, recent_weight=1.0, min_plays=3)
    assert recent["often"]["recent"] > 0 and "once" not in recent


def test_spotify_export_counts_listening_time():
    import io
    import json
    import zipfile
    from app.clients import parse_spotify_export
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Streaming_History_Audio_2025.json", json.dumps(
            [{"master_metadata_album_artist_name": "Skimmed", "ms_played": 31000}] * 10 +
            [{"master_metadata_album_artist_name": "Heard", "ms_played": 240000}] * 3))
    rows = {r["key"]: r for r in parse_spotify_export(buf.getvalue())}
    assert rows["heard"]["plays"] == 3.0 and rows["skimmed"]["plays"] < 2
    seeds = engine.build_seeds([], {k: {"name": r["name"], "plays": r["plays"], "sources": {"spotify_export": {"plays": r["plays"]}}}
                                    for k, r in rows.items()}, {}, min_plays=3)
    assert "heard" in seeds and "skimmed" not in seeds


def test_old_spotify_upload_gets_a_hint():
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    db.set_meta("spotify_export_v", None)
    db.set_meta("spotify_export_at", 1_700_000_000)   # imported before listening time was counted
    c = TestClient(m.app)
    assert c.get("/api/settings").json()["stats"]["spotify_export_old"] is True
    db.set_meta("spotify_export_v", 2)
    assert c.get("/api/settings").json()["stats"]["spotify_export_old"] is False
