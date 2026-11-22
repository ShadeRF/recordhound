"""Things that used to go wrong when a service misbehaved: Deezer outages, failed pictures,
playlist replacement, Spotify token renewal, and names that matched the wrong artist or album."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402
import requests  # noqa: E402

from app import clients, config, db  # noqa: E402
from app.clients import APIError, Deezer, Plex, album_key, artist_key  # noqa: E402

import test_pipeline as tp  # noqa: E402


class Answer:
    def __init__(self, status=200, body=None):
        self.status_code, self.ok, self._body = status, status < 400, body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error")


def test_names_that_used_to_collide():
    assert artist_key("MØ") == "mo" != artist_key("M")
    assert artist_key("Łona") != artist_key("Ona")
    assert artist_key("Sigur Rós") == "sigurros" and artist_key("Björk") == "bjork"
    story = "(What's the Story) Morning Glory?"
    assert album_key(story + " (Remastered)") == album_key(story) == "whatsthestorymorningglory"
    assert album_key("(I'm) Stranded [Expanded]") == "imstranded"
    assert album_key("Live (Bootleg) [Deluxe Edition]") == "livebootleg"
    assert album_key("Second LP (Deluxe Edition)") == album_key("Second LP")
    assert album_key("(Deluxe Edition)") != ""   # a title that is nothing but the bracket keeps something to match on


def test_deezer_outage_is_an_error_not_a_missing_artist(monkeypatch):
    monkeypatch.setattr(clients.time, "sleep", lambda s: None)
    dz = Deezer()
    for answer in (Answer(503), Answer(200, None), Answer(200, {"error": {"code": 4, "message": "Quota limit exceeded"}})):
        dz.s.get = lambda *a, _r=answer, **k: _r
        with pytest.raises(APIError):
            dz.find_artist("Slowdive")
    dz.s.get = lambda *a, **k: Answer(200, {"data": []})          # a real "nobody by that name"
    assert dz.find_artist("Slowdive") == {"missing": True}
    dz.s.get = lambda *a, **k: Answer(200, {"error": {"code": 800, "message": "no data"}})
    assert dz.albums(123) == []


def test_one_deezer_failure_does_not_stop_the_albums_step():
    db.init()
    s = tp.Syncer(lambda: tp.FakePlex(), lambda: tp.FakeLastFM(), tp.FakeDeezer,
                  artist_factory=lambda: (tp.FakeLastFM(), "lf:", None), sources={})
    s.run(full=True)
    assert s.state["error"] is None, s.state
    before = [g["key"] for g in db.get_meta("gaps")["groups"]]
    assert len(before) > 3
    gaze0, gaze1 = before[0], before[1]     # two artists whose Deezer lookup is about to fail
    db.conn().execute("UPDATE cache SET at = 0 WHERE k LIKE 'dz:%'")     # everything is due for a fresh look
    db.conn().execute("DELETE FROM cache WHERE k = ?", ("dz:" + gaze1,))
    db.commit()

    class Flaky(tp.FakeDeezer):
        def find_artist(self, name):
            if artist_key(name) in (gaze0, gaze1):
                raise requests.ConnectionError("timed out")
            return super().find_artist(name)
    s.deezer_factory = Flaky
    s.albums()
    groups = {g["key"] for g in db.get_meta("gaps")["groups"]}
    assert len(groups) > 2, "the other artists were still checked"
    assert gaze0 in groups, "an artist whose lookup failed keeps the albums we knew about"
    assert not db.cache_get_many(["dz:" + gaze1]), "a failed lookup was remembered"


def test_failed_pictures_are_not_kept_by_the_browser():
    from fastapi.testclient import TestClient
    import app.main as m
    c = TestClient(m.app)

    class Down:
        def find_artist(self, name):
            raise requests.ConnectionError("down")
    m._dz = Down()
    r = c.get("/api/art/some-new-artist?name=Some%20New%20Artist", follow_redirects=False)
    assert r.status_code == 404 and r.headers["cache-control"] == "no-store"
    assert db.cache_get("dzart:some-new-artist", 60) is None
    r = c.get("/api/cover/abc")
    assert r.status_code == 404 and r.headers["cache-control"] == "no-store"
    m._dz = tp.FakeDeezer()
    r = c.get("/api/art/some-new-artist?name=Some%20New%20Artist", follow_redirects=False)
    assert r.status_code == 302 and "max-age=604800" in r.headers["cache-control"]


class PlexSession:
    """Stands in for the HTTP session inside Plex: records calls, can fail the create."""
    def __init__(self, fail_create=False):
        self.calls, self.fail_create, self.headers = [], fail_create, {}

    def post(self, url, **kw):
        self.calls.append(("create", kw["params"]["title"]))
        return Answer(500, {}) if self.fail_create else Answer(200, {"MediaContainer": {"Metadata": [{"ratingKey": "77"}]}})

    def delete(self, url, **kw):
        self.calls.append(("delete", url.rsplit("/", 1)[1]))
        return Answer(200, {})


def test_playlist_is_made_before_the_old_one_goes():
    p = Plex("http://plex:32400", "tok")
    p.s = PlexSession()
    assert p.replace_playlist("Mine", [1, 2], "mach", ["55"]) == "77"
    assert p.s.calls == [("create", "Mine"), ("delete", "55")]
    p.s = PlexSession(fail_create=True)
    with pytest.raises(requests.HTTPError):
        p.replace_playlist("Mine", [1, 2], "mach", ["55"])
    assert p.s.calls == [("create", "Mine")], "the old playlist was removed although the new one failed"
    p.s = PlexSession()
    p.replace_playlist("Mine", [1, 2], "mach", ["77"])     # never deletes the one it just made
    assert p.s.calls == [("create", "Mine")]


def test_playlist_route_only_removes_its_own(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    c = TestClient(m.app)
    seen = {}

    class FakePlexAPI:
        def __init__(self, *a):
            pass

        def machine_id(self):
            return "mach"

        def playlists_titled(self, title):
            seen["looked_up"] = title
            return ["11"]

        def replace_playlist(self, title, rks, machine, old_keys=()):
            seen.update(title=title, old=list(old_keys))
            return "42"
    monkeypatch.setattr(m, "Plex", FakePlexAPI)
    monkeypatch.setattr(m, "_unplayed", lambda: [{"rk": 1}, {"rk": 2}])
    default = config.FIELDS["PLEX_PLAYLIST"][1]
    try:
        # a name of your own, nothing remembered yet: a playlist that merely shares the name is left alone
        db.set_meta("plex_playlist_rk", None)
        config.PLEX_PLAYLIST = "Favorites"
        assert c.post("/api/plex/playlist", json={"count": 2}).status_code == 200
        assert seen["old"] == [] and "looked_up" not in seen and db.get_meta("plex_playlist_rk") == "42"
        # next time, exactly the one made last time is replaced
        assert c.post("/api/plex/playlist", json={"count": 2}).status_code == 200
        assert seen["old"] == ["42"]
        # upgrading with the standard name: the playlist an older version made is found by name once
        db.set_meta("plex_playlist_rk", None)
        config.PLEX_PLAYLIST = default
        assert c.post("/api/plex/playlist", json={"count": 2}).status_code == 200
        assert seen["old"] == ["11"] and seen["looked_up"] == default
    finally:
        config.PLEX_PLAYLIST = default
        db.set_meta("plex_playlist_rk", None)


def test_spotify_test_button_keeps_a_renewed_token(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    c = TestClient(m.app)
    config.save({"SPOTIFY_CLIENT_ID": "client", "SPOTIFY_REFRESH_TOKEN": "old-refresh-token"})
    try:
        monkeypatch.setattr(clients.requests, "post",
                            lambda *a, **k: Answer(200, {"access_token": "acc", "refresh_token": "new-refresh-token"}))
        monkeypatch.setattr(clients.Spotify, "_get", lambda self, url, **p: {"id": "me"})
        r = c.post("/api/settings/test/spotify", json={}).json()
        assert r["ok"], r
        assert config.SPOTIFY_REFRESH_TOKEN == "new-refresh-token", "Spotify renewed the sign-in and the new one was thrown away"
    finally:
        config.save({"SPOTIFY_CLIENT_ID": "", "SPOTIFY_REFRESH_TOKEN": ""})


def test_disconnected_listening_source_stops_shaping_picks(monkeypatch):
    db.init()
    row = [{"key": "someband", "name": "Some Band", "plays": 40, "rank": 1}]
    db.replace_history("listenbrainz", row)
    db.replace_history("lastfm", row)
    db.replace_history("spotify_export", row)       # an uploaded history file isn't a connection: it stays
    db.set_meta("recent_listens", {"someband": 12})
    monkeypatch.setattr(config, "LISTENBRAINZ_USERNAME", "")
    monkeypatch.setattr(config, "LASTFM_USERNAME", "")
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "")
    monkeypatch.setattr(config, "LIDARR_URL", "")
    s = tp.Syncer(lambda: tp.FakePlex(), lambda: tp.FakeLastFM(), tp.FakeDeezer,
                  artist_factory=lambda: (tp.FakeLastFM(), "lf:", None))      # sources=None: the real path
    try:
        s.sync_sources()
        counts = db.history_counts()
        assert "listenbrainz" not in counts and "lastfm" not in counts, counts
        assert counts.get("spotify_export") == 1
        assert db.get_meta("recent_listens") == {}
    finally:
        db.replace_history("spotify_export", [])


def test_lookups_never_leave_a_write_open(monkeypatch):
    """While a slow lookup is on its way, nothing may be waiting to be saved: an open write blocks every other writer."""
    db.init()
    open_writes = []
    writer = db.conn()      # the sync below runs on this thread, so this is the connection that does its saving

    class Slow(tp.FakeLastFM):
        def artist(self, name, mbid=None):     # called from a worker thread, as a real lookup is
            for _ in range(100):               # give the saving thread a moment to finish the previous save
                if not writer.in_transaction:
                    break
                time.sleep(0.005)
            open_writes.append(writer.in_transaction)
            return super().artist(name, mbid)
    monkeypatch.setattr(config, "API_WORKERS", 1)
    s = tp.Syncer(lambda: tp.FakePlex(), lambda: Slow(), tp.FakeDeezer, artist_factory=lambda: (Slow(), "lf:", None), sources={})
    db.conn().execute("DELETE FROM cache WHERE k LIKE 'lf:%'")
    db.commit()
    s.run(full=True)
    assert s.state["error"] is None, s.state
    assert len(open_writes) > 20 and not any(open_writes)
