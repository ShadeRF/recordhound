"""Libraries (Use / Own only / Ignore on the Plex card) and the full list behind "More genres…"."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app.clients import Plex, artist_key  # noqa: E402

import test_pipeline as tp  # noqa: E402

SECTIONS = [{"key": "1", "title": "Music", "type": "artist"}, {"key": "2", "title": "Kids", "type": "artist"},
            {"key": "3", "title": "Old Rips", "type": "artist"}, {"key": "9", "title": "Films", "type": "movie"}]


def test_genres_list_tags_on_owned_artists_most_common_first(monkeypatch):
    import app.main as m
    lib = [{"key": f"a{i}"} for i in range(5)]
    infos = {"ob:a0": {"tags": [["Hip-Hop", 9], ["seen live", 8], ["jazz", 5], ["soul", 4], ["funk", 1]]},
             "ob:a1": {"tags": [["hip hop", 9], ["jazz", 2]]},
             "ob:a2": {"tags": [["hip hop", 9], ["uk", 3]]},
             "ob:a3": {"tags": []}}   # a4 was never looked up
    monkeypatch.setattr(m, "_source_name", lambda: "open")
    monkeypatch.setattr(m.db, "library", lambda: lib)
    monkeypatch.setattr(m.db, "cache_get_many", lambda keys, max_age_days=None: {k: v for k, v in infos.items() if k in keys})
    got = m.noseed_genres()["genres"]
    # spellings merged and shown the common way, junk ("seen live", "uk") skipped, only each artist's top three counted
    assert got == [{"tag": "hip hop", "n": 3}, {"tag": "jazz", "n": 2}, {"tag": "soul", "n": 1}]
    monkeypatch.setattr(m, "_source_name", lambda: "lastfm")   # Last.fm's tags live under their own prefix
    assert m.noseed_genres() == {"genres": []}


def test_genres_stop_at_150(monkeypatch):
    import app.main as m
    lib = [{"key": f"a{i}"} for i in range(200)]
    infos = {f"lf:a{i}": {"tags": [[f"tag {i}", 1], ["common", 1]]} for i in range(200)}
    monkeypatch.setattr(m, "_source_name", lambda: "lastfm")
    monkeypatch.setattr(m.db, "library", lambda: lib)
    monkeypatch.setattr(m.db, "cache_get_many", lambda keys, max_age_days=None: infos)
    got = m.noseed_genres()["genres"]
    assert len(got) == 150 and got[0] == {"tag": "common", "n": 200}


def test_ignored_libraries_are_not_read(monkeypatch):
    p = Plex("http://plex.test:32400", "t")
    monkeypatch.setattr(p, "all_sections", lambda: SECTIONS)
    assert [s["title"] for s in p.sections()] == ["Music", "Kids", "Old Rips"]   # [] = every music library
    p.libraries = ["Music", "kids"]   # what saving with Old Rips on Ignore stores
    assert [s["title"] for s in p.sections()] == ["Music", "Kids"]


def test_library_settings_round_trip_unchanged():
    """Nothing moves on upgrade: the two settings keep what older versions saved."""
    import app.main as m
    db.init()
    keep = {k: getattr(config, k) for k in ("PLEX_LIBRARIES", "NOSEED_LIBRARIES")}
    try:
        config.save({"PLEX_LIBRARIES": ["Music", "Kids"], "NOSEED_LIBRARIES": ["Kids"]})
        v = TestClient(m.app).get("/api/settings").json()["values"]
        assert v["PLEX_LIBRARIES"] == ["Music", "Kids"] and v["NOSEED_LIBRARIES"] == ["Kids"]
    finally:
        config.save(keep)


class TwoLibraryPlex(tp.FakePlex):
    def artists(self, step=lambda *a: None):
        rows = super().artists(step)
        for r in rows:
            if r["name"] in tp.GAZE[6:12]:
                r["section"] = "2"
        return rows

    def all_sections(self):
        return SECTIONS[:2]


def test_own_only_library_and_left_out_genres_dont_shape_picks(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "NOSEED_LIBRARIES", ["kids"])
    monkeypatch.setattr(config, "NOSEED_TAGS", ["Americana"])   # another spelling counts too
    s = tp.Syncer(lambda: TwoLibraryPlex(), lambda: tp.FakeLastFM(), tp.FakeDeezer,
                  artist_factory=lambda: (tp.FakeLastFM(), "lf:", None), sources={})
    s.run(full=True)
    assert s.state["error"] is None, s.state
    seeds = db.get_meta("seeds")
    kids = {artist_key(n) for n in tp.GAZE[6:12]}
    assert kids and all(k not in seeds for k in kids)                   # own only: no say in your picks
    assert artist_key(tp.GAZE[0]) in seeds                               # Use: shapes them
    assert artist_key(tp.COUNTRY[0]) not in seeds                        # tagged americana
    owned = {r["key"] for r in db.library()}
    assert kids <= owned                                                 # still yours, so never suggested
    assert not kids & {r["key"] for r in db.recs("new")}
