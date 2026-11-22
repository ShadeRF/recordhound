"""What stays loaded after a hide or save, and how saved artist lookups are reused when they go out of date."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import config, db  # noqa: E402
from app import sync as sync_mod  # noqa: E402
from app.clients import artist_key  # noqa: E402

import test_pipeline as tp  # noqa: E402


def test_hide_and_save_keep_picks_loaded_but_show_at_once():
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    s = tp.Syncer(lambda: tp.FakePlex(), lambda: tp.FakeLastFM(), tp.FakeDeezer,
                  artist_factory=lambda: (tp.FakeLastFM(), "lf:", None), sources={})
    s.run(full=True)
    assert s.state["error"] is None, s.state
    c = TestClient(m.app)
    first = c.get("/api/recs?kind=new").json()
    victim, other = first["items"][0]["key"], first["items"][1]["key"]
    db.set_meta("plex_albums", [{"rk": 1, "thumb": "/t"}])
    loaded = db.recs("new"), db.library_map(), db.history(), db.get_meta("plex_albums")
    try:
        assert c.post("/api/saved", json={"key": other, "name": "x", "on": True}).status_code == 200
        db.set_feedback(victim, "dismissed", "x")
        assert (db.recs("new") is loaded[0] and db.library_map() is loaded[1] and db.history() is loaded[2]
                and db.get_meta("plex_albums") is loaded[3]), "a hide or save reloaded the picks, library or album list"
        after = c.get("/api/recs?kind=new").json()
        assert after["total"] == first["total"] - 1 and victim not in [i["key"] for i in after["items"]]
        assert next(i for i in after["items"] if i["key"] == other)["saved"] is True
        assert victim not in {i["key"] for row in c.get("/api/home").json()["rows"] for i in row["items"] if "key" in i}
        assert other in [i["key"] for i in c.get("/api/saved").json()["items"]]
        # undoing shows at once as well
        db.clear_feedback(victim)
        assert c.get("/api/recs?kind=new").json()["total"] == first["total"]
        # one stored value changing leaves the others loaded, and is itself fresh
        db.set_meta("taste", [{"tag": "shoegaze", "w": 1.0}])
        assert db.get_meta("plex_albums") is loaded[3] and db.recs("new") is loaded[0]
        assert db.get_meta("taste") == [{"tag": "shoegaze", "w": 1.0}]
        db.set_meta("plex_albums", [{"rk": 2, "thumb": "/u"}])
        assert db.get_meta("plex_albums") == [{"rk": 2, "thumb": "/u"}]
        # what a sync writes still reloads everything
        db.replace_recs("new", db.recs("new"))
        assert db.recs("new") is not loaded[0]
    finally:
        db.clear_feedback(victim)
        db.set_saved(other, None, on=False)


def test_lookup_freshness_is_spread_out():
    days = [sync_mod.cache_days(f"ob:artist{i}") for i in range(500)]
    assert min(days) >= config.CACHE_DAYS and max(days) <= config.CACHE_DAYS * 4 / 3
    assert max(days) - min(days) > config.CACHE_DAYS / 4, "everything would still go out of date on the same night"
    assert sync_mod.cache_days("ob:artist1") == sync_mod.cache_days("ob:artist1")


def test_out_of_date_lookup_keeps_its_musicbrainz_id(monkeypatch):
    db.init()
    asked = {}

    class Source:
        fail = set()

        def artist(self, name, mbid=None):
            asked[name] = mbid
            if name in self.fail:
                raise OSError("down")
            return {"name": name, "mbid": mbid or "looked-up-by-name", "tags": [], "similar": [], "v": 2}
    old = int(time.time()) - 400 * 86400
    for name, mbid in (("Old Known", "mbid-old"), ("Old Broken", "mbid-broken")):
        db.cache_put("ob:" + artist_key(name), {"name": name, "mbid": mbid, "tags": [["dream pop", 5]], "similar": []})
    db.cache_put("ob:" + artist_key("Still Fresh"), {"name": "Still Fresh", "mbid": "mbid-fresh", "tags": [], "similar": []})
    db.conn().execute("UPDATE cache SET at = ? WHERE k IN (?, ?)", (old, "ob:" + artist_key("Old Known"), "ob:" + artist_key("Old Broken")))
    db.commit()
    s = tp.Syncer(lambda: tp.FakePlex(), lambda: tp.FakeLastFM(), tp.FakeDeezer)
    s.prefix, s.ad = "ob:", Source()
    Source.fail = {"Old Broken"}
    wanted = [(artist_key(n), n, None) for n in ("Old Known", "Old Broken", "Still Fresh", "Brand New")]
    got = s._infos(wanted, "test")
    assert asked == {"Old Known": "mbid-old", "Old Broken": "mbid-broken", "Brand New": None}, asked   # fresh one not asked again
    assert got[artist_key("Old Known")]["mbid"] == "mbid-old"
    assert got[artist_key("Old Broken")]["tags"] == [["dream pop", 5]], "a failed refresh dropped an artist we already knew"
    assert got[artist_key("Still Fresh")]["mbid"] == "mbid-fresh" and got[artist_key("Brand New")]["mbid"] == "looked-up-by-name"
    # the refreshed one is fresh again; the failed one is tried again next time
    asked.clear()
    Source.fail = set()
    s._infos(wanted, "test")
    assert asked == {"Old Broken": "mbid-broken"}, asked
