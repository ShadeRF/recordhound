"""The Rate page: which artists it asks about, where each rating is saved, and how ratings shape picks."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import db, engine  # noqa: E402

import test_pipeline as tp  # noqa: E402


def _synced():
    db.init()
    s = tp.Syncer(lambda: tp.FakePlex(), lambda: tp.FakeLastFM(), tp.FakeDeezer,
                  artist_factory=lambda: (tp.FakeLastFM(), "lf:", None), sources={})
    s.run(full=True)
    assert s.state["error"] is None, s.state
    return s


def _client():
    from fastapi.testclient import TestClient
    import app.main as m
    return TestClient(m.app), m


def test_library_queue_asks_about_played_unrated_artists_most_played_first():
    _synced()
    c, _ = _client()
    lib = db.library()
    want = sorted([r for r in lib if not r.get("rating") and ((r.get("plays") or 0) > 0 or r.get("last_played"))],
                  key=lambda r: (-(r.get("plays") or 0), -(r.get("last_played") or 0)))
    assert len(want) >= 2, "the fake library should have played, unrated artists"
    d = c.get("/api/rate/queue?mode=library").json()
    assert [x["key"] for x in d["items"]] == [r["key"] for r in want][:40] and d["remaining"] == len(want)
    # skipped artists (one per line, since a key can hold a comma) and don't-learn artists aren't asked about
    first, second = want[0]["key"], want[1]["key"]
    db.set_noseed(second, "x")
    try:
        d = c.get("/api/rate/queue", params={"mode": "library", "skip": first + "\nsome,odd,key"}).json()
        keys = [x["key"] for x in d["items"]]
        assert first not in keys and second not in keys
    finally:
        db.set_noseed(second, None, on=False)


def test_rating_a_library_artist_goes_to_plex(monkeypatch):
    _synced()
    c, m = _client()
    sent = []

    class FakePlexAPI:
        def __init__(self, *a):
            pass

        def rate(self, rk, rating):
            sent.append((rk, rating))
    monkeypatch.setattr(m, "Plex", FakePlexAPI)
    monkeypatch.setattr(m, "schedule_rescore", lambda *a: None)
    target = c.get("/api/rate/queue?mode=library").json()["items"][0]
    lib = db.library_map()[target["key"]]
    r = c.post("/api/rate", json={"key": target["key"], "name": target["name"], "rating": 9})
    assert r.json() == {"ok": True, "saved_to": "plex"} and sent == [(lib["plex_key"], 9)]
    assert db.library_map()[target["key"]]["rating"] == 9, "the rating should count before the next sync reads it back"
    assert target["key"] not in [x["key"] for x in c.get("/api/rate/queue?mode=library").json()["items"]]
    assert c.post("/api/rate", json={"key": target["key"], "rating": 11}).status_code == 400

    class Down(FakePlexAPI):
        def rate(self, rk, rating):
            raise OSError("Plex is down")
    monkeypatch.setattr(m, "Plex", Down)
    other = c.get("/api/rate/queue?mode=library").json()["items"][0]
    r = c.post("/api/rate", json={"key": other["key"], "rating": 6})
    assert r.status_code == 502 and "Plex" in r.json()["detail"]
    assert not db.library_map()[other["key"]].get("rating"), "a failed save must not look saved"


def test_rating_a_pick_you_know_hides_it_and_shapes_taste(monkeypatch):
    s = _synced()
    c, m = _client()
    monkeypatch.setattr(m, "schedule_rescore", lambda *a: None)
    picks = c.get("/api/rate/queue?mode=picks").json()
    assert picks["remaining"] >= 3
    loved, meh, unknown = picks["items"][0], picks["items"][1], picks["items"][2]
    assert loved["because"]
    try:
        assert c.post("/api/rate", json={"key": loved["key"], "name": loved["name"], "rating": 10}).json()["saved_to"] == "recordhound"
        assert c.post("/api/rate", json={"key": meh["key"], "name": meh["name"], "rating": None}).status_code == 200
        keys = [x["key"] for x in c.get("/api/recs?kind=new").json()["items"]]
        assert loved["key"] not in keys and meh["key"] not in keys, "artists you know shouldn't be suggested"
        assert unknown["key"] in [x["key"] for x in c.get("/api/rate/queue?mode=picks").json()["items"]]
        hist = {x["key"]: x for x in c.get("/api/feedback").json()["items"]}
        assert hist[loved["key"]]["action"] == "known" and hist[loved["key"]]["rating"] == 10 and hist[meh["key"]]["rating"] is None
        s.run(full=False)
        seeds = db.get_meta("seeds")
        assert seeds[loved["key"]]["kind"] == "rated" and seeds[loved["key"]]["w"] == 2.0 and not seeds[loved["key"]]["owned"]
        assert meh["key"] not in seeds, "knowing an artist without rating them says nothing about taste"
    finally:
        db.clear_feedback(loved["key"])
        db.clear_feedback(meh["key"])


def test_low_rating_pushes_away_and_old_databases_gain_the_column():
    seeds = engine.build_seeds([], {}, {"a": {"action": "known", "name": "A", "rating": 2},
                                        "b": {"action": "dismissed", "name": "B", "rating": None},
                                        "c": {"action": "known", "name": "C", "rating": None}})
    assert seeds["a"]["w"] < 0 and seeds["a"]["kind"] == "rated" and "b" not in seeds and "c" not in seeds
    # an install from before the Rate page: the feedback table has no rating column yet
    db.init()
    c = db.conn()
    c.execute("DROP TABLE feedback")
    c.execute("CREATE TABLE feedback (key TEXT PRIMARY KEY, action TEXT NOT NULL, name TEXT, at INTEGER NOT NULL)")
    c.execute("INSERT INTO feedback VALUES ('oldkey', 'dismissed', 'Old', 1)")
    c.commit()
    db.init()
    db.bump()
    assert db.feedback()["oldkey"]["rating"] is None
    db.set_feedback("newkey", "known", "New", 8)
    assert db.feedback()["newkey"]["rating"] == 8
    db.clear_feedback("oldkey")
    db.clear_feedback("newkey")
