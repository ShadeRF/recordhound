"""Artist data without Last.fm (MusicBrainz, ListenBrainz, Deezer, Wikipedia), with every service faked."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import clients, config, db, engine  # noqa: E402
from app.clients import OpenArtists, artist_key, artist_source, parse_similar  # noqa: E402

RH = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
PJ = "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11"


class FakeDZ:
    def find_artist(self, name):
        if name == "Nobody Knows Them":
            return {"missing": True}
        return {"id": 399, "name": name, "fans": 4_000_000, "thumb": "t", "picture": "p"}

    def related(self, _id, limit=50):
        return [("Portishead", 1), ("Thom Yorke", 1), ("Muse", 1)]


def fake_get(url, **params):
    if url.endswith("/ws/2/artist"):
        assert params["query"] == 'artist:"Radiohead"'
        return {"artists": [{"id": "wrong", "name": "Radiohead Tribute", "score": 100},
                            {"id": RH, "name": "Radiohead", "score": 98}]}
    if "similar-artists" in url:
        assert params["artist_mbids"] == RH
        return [{"artist_mbid": PJ, "name": "Portishead", "score": 900},
                {"artist_mbid": "c1", "name": "The Smile", "score": 450},
                {"artist_mbid": RH, "name": "Radiohead", "score": 9999}]
    if "/metadata/artist/" in url:
        return [{"name": "Radiohead", "mbid": RH, "rels": {"wikidata": "https://www.wikidata.org/wiki/Q44190"},
                 "tag": {"artist": [{"tag": "Experimental Rock", "count": 14}, {"tag": "britpop", "count": 2},
                                    {"tag": "zero", "count": 0}]}}]
    if "wikidata.org" in url:
        return {"entities": {"Q44190": {"sitelinks": {"enwiki": {"title": "Radiohead"}}}}}
    if "wikipedia.org" in url:
        return {"type": "standard", "extract": "Radiohead are an English rock band.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Radiohead"}}}
    raise AssertionError(url)


def test_parse_similar_both_shapes():
    flat = [{"artist_mbid": "a", "name": "A", "score": 3}]
    nested = [{"type": "markup", "data": "<p>hi</p>"}, {"type": "dataset", "data": flat}]
    assert parse_similar(flat) == parse_similar(nested) == [{"mbid": "a", "name": "A", "score": 3.0}]
    assert parse_similar(None) == [] and parse_similar({"error": "x"}) == []


def test_open_artist():
    oa = OpenArtists(FakeDZ())
    oa._get = fake_get
    clients.OpenArtists._mb_last[0] = 0
    info = oa.artist("Radiohead")
    assert info["mbid"] == RH and info["fans"] == 4_000_000 and info["listeners"] is None
    assert info["tags"][0] == ("experimental rock", 14) and ("zero", 0) not in info["tags"]
    names = [n for n, _ in info["similar"]]
    assert "Radiohead" not in names
    assert names[0] == "Portishead"            # both services name it
    assert {"The Smile", "Thom Yorke", "Muse"} <= set(names)
    assert info["similar_mbids"][artist_key("Portishead")] == PJ and artist_key("Muse") not in info["similar_mbids"]
    assert all(0 < m <= 1 for _, m in info["similar"])
    assert info["wikidata"] == "Q44190"
    assert oa.bio("Q44190")["bio_url"].endswith("/Radiohead")
    assert oa.bio("not-an-id") is None
    # nothing matches anywhere: missing, not an error
    oa._get = lambda url, **p: {"artists": []} if url.endswith("/ws/2/artist") else None
    assert oa.artist("Nobody Knows Them")["missing"]


def test_source_choice(monkeypatch):
    assert artist_source("", FakeDZ())[1] == "ob:"
    monkeypatch.setattr(clients, "lastfm_works", lambda key: False)
    client, prefix, note = artist_source("dead-key", FakeDZ())
    assert prefix == "ob:" and isinstance(client, OpenArtists) and "Pro" in note
    monkeypatch.setattr(clients, "lastfm_works", lambda key: True)
    assert artist_source("pro-key", FakeDZ())[1] == "lf:"


def test_fans_threshold():
    assert engine.too_obscure({"fans": 50}, min_listeners=0, min_fans=200)
    assert not engine.too_obscure({"fans": 500}, min_listeners=99999, min_fans=200)
    assert engine.too_obscure({"listeners": 100, "fans": 10**6}, min_listeners=2000, min_fans=0)
    assert not engine.too_obscure({"fans": None}, 2000, 200)   # unknown isn't hidden


def test_pipeline_without_lastfm():
    """Same scenes as test_pipeline, through the free-sources path: Deezer fans instead of listeners, its own cache."""
    import test_pipeline as tp
    from app.sync import Syncer

    class FakeOpen:
        calls = []

        def artist(self, name, mbid=None):
            FakeOpen.calls.append((name, mbid))
            i = tp.info(name) if name in tp.SCENE else {"missing": True, "name": name}
            if not i.get("missing"):
                i = {**i, "listeners": None, "fans": 10 if name == tp.GAZE[39] else 5000, "src": "open", "v": 2,
                     "mbid": "mb-" + name, "similar_mbids": {artist_key(n): "mb-" + n for n, _ in i["similar"]}}
            return i
    db.init()
    config.MIN_FANS = 200
    s = Syncer(lambda: tp.FakePlex(), None, tp.FakeDeezer, sources={},
               artist_factory=lambda: (FakeOpen(), "ob:", "Last.fm rejected the API key. Using free sources."))
    s.run(full=True)
    assert s.state["error"] is None, s.state
    assert any("rejected" in w for w in s.state["warnings"])
    assert db.get_meta("artist_source") == "open"
    recs = db.recs("new")
    assert recs and all(tp.SCENE[r["name"]] == "shoegaze" for r in recs[:10])
    assert all(r["fans"] for r in recs) and artist_key(tp.GAZE[39]) not in {r["key"] for r in recs}
    # candidates arrive with the MusicBrainz ids ListenBrainz gave, so no name search is needed
    looked_up = dict(FakeOpen.calls)
    cand = next(r["name"] for r in recs)
    assert looked_up[cand] == "mb-" + cand
    assert db.cache_get("ob:" + recs[0]["key"], 30) is not None
