"""Liked, not owned: Spotify Liked Songs checked against every song in Plex."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import clients, config, db  # noqa: E402
from app.clients import APIError, Lidarr, credit_keys, song_key  # noqa: E402


def test_song_titles_match_across_versions():
    same = ["Karma Police", "Karma Police - Remastered 2017", "Karma Police (Live)", "Karma Police [2017 Remaster]",
            "Karma Police feat. Nobody", "KARMA POLICE!"]
    assert len({song_key(t) for t in same}) == 1
    assert song_key("Paranoid Android") != song_key("Karma Police")
    assert song_key("Rock & Roll") == song_key("Rock and Roll")
    assert song_key("夜に駆ける") and song_key("夜に駆ける") != song_key("群青")   # titles with no Latin letters stay distinct


def test_credits_split_into_each_artist():
    assert {"calvinharris", "dualipa"} <= credit_keys("Calvin Harris, Dua Lipa")
    assert {"simonandgarfunkel", "simon", "garfunkel"} <= credit_keys("Simon & Garfunkel")
    assert {"drake", "rihanna"} <= credit_keys("Drake feat. Rihanna")


def liked(title, artists, album, album_id, added, album_artist=None, kind="album"):
    return {"id": album_id + title, "title": title, "artists": artists, "album": album, "album_id": album_id, "album_type": kind,
            "album_artist": album_artist or artists[0], "year": "2026", "cover": "https://i.scdn.co/x", "added_at": added}


LIKED = [
    liked("A Man Written by a Woman", ["Whitney Whitney"], "A Man Written by a Woman", "al1", "2026-02-06T00:00:00Z", kind="single"),
    liked("Isabelle", ["Whitney Whitney"], "Isabelle", "al2", "2026-01-02T00:00:00Z", kind="single"),
    liked("Feels So Far Away", ["Bellvie"], "Feels So Far Away", "al3", "2026-01-23T00:00:00Z", kind="single"),
    liked("Idioteque", ["Radiohead"], "Kid A", "al4", "2025-12-01T00:00:00Z"),
    liked("Everything In Its Right Place", ["Radiohead"], "Kid A", "al4", "2025-11-01T00:00:00Z"),
    liked("Karma Police - Remastered", ["Radiohead"], "OK Computer OKNOTOK", "al5", "2025-10-01T00:00:00Z"),   # owned, other album
    liked("One Kiss", ["Calvin Harris", "Dua Lipa"], "One Kiss", "al6", "2025-09-01T00:00:00Z"),               # owned as Dua Lipa's
]
SONGS = {"radiohead|karmapolice": "501", "dualipa|onekiss": "502", "radiohead|airbag": "503"}


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    db.set_meta("spotify_liked", LIKED)
    db.set_meta("plex_songs", SONGS)
    for k in ("liked_hidden", "liked_lidarr", "plex_liked_playlist_rk"):
        db.set_meta(k, None)
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "r")
    return TestClient(m.app), m


def test_liked_page_groups_missing_songs_by_album_newest_first(client):
    c, _ = client
    d = c.get("/api/liked").json()
    assert d["ready"] and d["total"] == 7 and d["owned"] == 2
    assert [a["id"] for a in d["albums"]] == ["al1", "al3", "al2", "al4"]
    kid_a = d["albums"][-1]
    assert kid_a["title"] == "Kid A" and kid_a["songs"] == ["Idioteque", "Everything In Its Right Place"] and kid_a["artist_key"] == "radiohead"


def test_hiding_an_album_and_bringing_it_back(client):
    c, _ = client
    c.post("/api/liked/hide", json={"id": "al4"})
    d = c.get("/api/liked").json()
    assert "al4" not in [a["id"] for a in d["albums"]] and d["hidden"] == 2
    c.post("/api/liked/hide", json={"id": "al4", "on": False})
    assert "al4" in [a["id"] for a in c.get("/api/liked").json()["albums"]]
    c.post("/api/liked/hide", json={"id": "al1"})
    c.post("/api/liked/unhide-all")
    assert c.get("/api/liked").json()["hidden"] == 0


def test_empty_states(client, monkeypatch):
    c, _ = client
    db.set_meta("plex_songs", None)   # synced before this feature existed
    d = c.get("/api/liked").json()
    assert d["connected"] and not d["ready"]
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "")
    db.set_meta("spotify_liked", None)
    assert c.get("/api/liked").json() == {**d, "connected": False}


def test_playlist_of_liked_songs_you_own_replaces_only_its_own(client, monkeypatch):
    c, m = client
    made = []

    class FakePlex:
        def __init__(self, *a, **k):
            pass

        def machine_id(self):
            return "mach"

        def replace_playlist(self, title, rks, machine, old):
            made.append((title, rks, old))
            return "900"
    monkeypatch.setattr(m, "Plex", FakePlex)
    r = c.post("/api/plex/liked-playlist").json()
    assert r["songs"] == 2 and made[-1] == (m.LIKED_PLAYLIST, ["501", "502"], [])
    c.post("/api/plex/liked-playlist")
    assert made[-1][2] == ["900"]   # the second time it replaces the first one, and nothing else


def test_send_album_to_lidarr_remembers_it(client, monkeypatch):
    c, m = client
    calls = []

    class FakeLidarr:
        def __init__(self, *a):
            pass

        def add_album(self, artist, title, q, md, root):
            calls.append((artist, title, q, md, root))
            return "Kid A"
    monkeypatch.setattr(m, "Lidarr", FakeLidarr)
    r = c.post("/api/liked/lidarr", json={"id": "al4", "quality_id": 1, "metadata_id": 2, "root": "/music"})
    assert r.json() == {"ok": True, "title": "Kid A"} and calls == [("Radiohead", "Kid A", 1, 2, "/music")]
    assert next(a for a in c.get("/api/liked").json()["albums"] if a["id"] == "al4")["requested"]
    assert c.post("/api/liked/lidarr", json={"id": "nope", "quality_id": 1, "metadata_id": 2, "root": "/m"}).status_code == 404


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.content = data, status, b"x"
        self.text = str(data)

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, lookups):
        self.lookups, self.sent = lookups, []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        return FakeResp(self.lookups.get(params.get("term"), []))

    def request(self, method, url, json=None, timeout=None):
        self.sent.append((method, url.split("/api/v1")[1], json))
        return FakeResp({})


def lidarr_with(lookups):
    lid = Lidarr("http://lidarr", "key")
    lid.s = FakeSession(lookups)
    return lid


def test_lidarr_adds_only_the_album_for_a_new_artist():
    lid = lidarr_with({"Bellvie Feels So Far Away": [
        {"title": "Something Else", "foreignAlbumId": "x", "artist": {"artistName": "Bellvie", "foreignArtistId": "b"}},
        {"title": "Feels So Far Away", "foreignAlbumId": "rg1", "artist": {"artistName": "Bellvie", "foreignArtistId": "b"}}]})
    assert lid.add_album("Bellvie", "Feels So Far Away", 1, 2, "/music") == "Feels So Far Away"
    method, path, body = lid.s.sent[0]
    assert (method, path, body["foreignAlbumId"], body["monitored"]) == ("post", "/album", "rg1", True)
    assert body["addOptions"]["searchForNewAlbum"] and body["artist"]["addOptions"]["monitor"] == "none"
    assert body["artist"]["monitorNewItems"] == "none" and body["artist"]["rootFolderPath"] == "/music"


def test_lidarr_monitors_an_album_it_already_knows():
    lid = lidarr_with({"Radiohead Kid A": [{"id": 77, "title": "Kid A", "artist": {"id": 5, "artistName": "Radiohead"}}]})
    lid.add_album("Radiohead", "Kid A", 1, 2, "/music")
    assert lid.s.sent == [("put", "/album/monitor", {"albumIds": [77], "monitored": True}),
                          ("post", "/command", {"name": "AlbumSearch", "albumIds": [77]})]


def test_lidarr_says_so_when_musicbrainz_has_nothing():
    lid = lidarr_with({"Nobody Nothing": [{"title": "Nothing", "artist": {"artistName": "Somebody Else"}}]})
    with pytest.raises(APIError, match="couldn't find Nothing by Nobody"):
        lid.add_album("Nobody", "Nothing", 1, 2, "/music")


def test_spotify_liked_tracks_skip_local_files_and_pick_a_cover():
    sp = object.__new__(clients.Spotify)
    pages = {"/me/tracks": {"items": [
        {"added_at": "2026-02-06T00:00:00Z", "track": {"id": "t1", "name": "Isabelle", "artists": [{"name": "Whitney Whitney"}],
                                                      "album": {"id": "a1", "name": "Isabelle", "album_type": "single", "release_date": "2026-01-02",
                                                                "artists": [{"name": "Whitney Whitney"}],
                                                                "images": [{"url": "big", "width": 640}, {"url": "mid", "width": 300}, {"url": "small", "width": 64}]}}},
        {"added_at": "2026-01-01T00:00:00Z", "track": {"id": None, "name": "my mp3", "is_local": True}}], "next": None}}
    sp._get = lambda url, **p: pages[url]
    rows = sp.liked_tracks()
    assert len(rows) == 1 and rows[0]["cover"] == "mid" and rows[0]["album_artist"] == "Whitney Whitney" and rows[0]["year"] == "2026"


def test_big_playlists_are_added_in_batches():
    plex = object.__new__(clients.Plex)
    plex.url = "http://plex"
    sent = []

    class S:
        def post(self, url, params=None, timeout=None):
            sent.append(("post", params["uri"].count(",") + 1))
            return FakeResp({"MediaContainer": {"Metadata": [{"ratingKey": "9"}]}})

        def put(self, url, params=None, timeout=None):
            sent.append(("put", params["uri"].count(",") + 1))
            return FakeResp({})

        def delete(self, url, timeout=None):
            sent.append(("delete", url.rsplit("/", 1)[1]))
    plex.s = S()
    assert plex.replace_playlist("x", range(450), "m", ["3"]) == "9"
    assert sent == [("post", 200), ("put", 200), ("put", 50), ("delete", "3")]


def test_sync_keeps_plex_songs_and_liked_songs():
    import test_pipeline as tp
    from app.sync import Syncer

    class SongPlex(tp.FakePlex):
        songs = {"a|b": "1"}
    s = Syncer(plex_factory=SongPlex, deezer_factory=tp.FakeDeezer, artist_factory=lambda: (tp.FakeLastFM(), "lf:", None),
               sources={"spotify_liked": LIKED[:2]})
    s.sync_sources()
    assert db.get_meta("plex_songs") == {"a|b": "1"} and len(db.get_meta("spotify_liked")) == 2


def test_plex_read_lists_every_song_under_album_and_track_artists():
    plex = object.__new__(clients.Plex)
    plex.sections = lambda: [{"key": "1", "title": "Music"}]
    data = {8: [{"ratingKey": "a1", "title": "Radiohead"}, {"ratingKey": "a2", "title": "Various Artists"}],
            9: [{"ratingKey": "b1", "parentRatingKey": "a1", "title": "OK Computer"}, {"ratingKey": "b2", "parentRatingKey": "a2", "title": "Hits"}],
            10: [{"ratingKey": "t1", "grandparentRatingKey": "a1", "parentRatingKey": "b1", "title": "Karma Police"},
                 {"ratingKey": "t2", "grandparentRatingKey": "a2", "parentRatingKey": "b2", "title": "One Kiss (Radio Edit)",
                  "originalTitle": "Calvin Harris & Dua Lipa"}]}
    plex._paged = lambda sid, kind, **k: iter(data[kind])
    plex.artists()
    assert plex.songs["radiohead|karmapolice"] == "t1"
    assert plex.songs["dualipa|onekiss"] == plex.songs["calvinharris|onekiss"] == "t2"


def test_any_album_can_go_to_lidarr_and_remembers_the_choice(client, monkeypatch):
    c, m = client
    sent = []

    class FakeLidarr:
        def __init__(self, *a):
            pass

        def add_album(self, *a):
            sent.append(a)
            return "Kid A"
    monkeypatch.setattr(m, "Lidarr", FakeLidarr)
    r = c.post("/api/lidarr/album", json={"artist": "Radiohead", "title": "Kid A", "quality_id": 3, "metadata_id": 4, "root": "/m"})
    assert r.json() == {"ok": True, "title": "Kid A"} and sent == [("Radiohead", "Kid A", 3, 4, "/m")]
    assert c.get("/api/lidarr/last").json()["quality_id"] == 3

    class Broken(FakeLidarr):
        def add_album(self, *a):
            raise APIError("Lidarr couldn't find Kid A by Radiohead on MusicBrainz")
    monkeypatch.setattr(m, "Lidarr", Broken)
    r = c.post("/api/lidarr/album", json={"artist": "Radiohead", "title": "Kid A", "quality_id": 3, "metadata_id": 4, "root": "/m"})
    assert r.status_code == 502 and "couldn't find Kid A" in r.json()["detail"]


def test_album_tracks_show_what_you_own_and_liked(client, monkeypatch):
    c, m = client
    calls = []

    class FakeDeezer:
        def album_tracks(self, album_id):
            calls.append(album_id)
            return [{"n": 1, "disc": 1, "title": "Airbag", "duration": 284, "preview": "p1", "artist": "Radiohead"},
                    {"n": 2, "disc": 1, "title": "Karma Police (Remastered)", "duration": 264, "preview": "p2", "artist": "Radiohead"},
                    {"n": 3, "disc": 1, "title": "Idioteque", "duration": 309, "preview": None, "artist": "Radiohead"}]
    monkeypatch.setattr(m, "_dz", FakeDeezer())
    db.conn().execute("DELETE FROM cache WHERE k LIKE 'dzalb:%'")
    db.commit()
    d = c.get("/api/album/555/tracks?name=Radiohead").json()
    assert [(t["title"], t["owned"], t["liked"]) for t in d["tracks"]] == [
        ("Airbag", True, False), ("Karma Police (Remastered)", True, True), ("Idioteque", False, True)]
    assert d["checked_plex"] and d["tracks"][0]["duration"] == 284
    c.get("/api/album/555/tracks?name=Radiohead")
    assert calls == [555]   # cached after the first look

    class Down:
        def album_tracks(self, album_id):
            raise clients.requests.ConnectionError("down")
    monkeypatch.setattr(m, "_dz", Down())
    r = c.get("/api/album/556/tracks?name=Radiohead")
    assert r.status_code == 502 and db.cache_get("dzalb:556") is None   # a failure isn't remembered


def test_styled_names_and_two_spellings_of_one_artist():
    from app.clients import artist_key
    assert artist_key("CHVRCHΞS") == artist_key("CHVRCHES") == "chvrches"
    assert artist_key("Σωκράτης Μάλαμας") == "σωκράτης μάλαμας"   # a Greek name stays Greek
    plex = object.__new__(clients.Plex)
    plex.sections = lambda: [{"key": "1", "title": "Music"}]
    data = {8: [{"ratingKey": "a1", "title": "Chvrches", "Guid": [{"id": "mbid://m1"}]},
                {"ratingKey": "a2", "title": "Chvrches & Friends", "Guid": [{"id": "mbid://m1"}]}],
            9: [{"ratingKey": "b1", "parentRatingKey": "a1", "title": "Every Open Eye"},
                {"ratingKey": "b2", "parentRatingKey": "a2", "title": "The Bones Of What You Believe"}],
            10: [{"ratingKey": "t1", "grandparentRatingKey": "a1", "parentRatingKey": "b1", "title": "Clearest Blue", "viewCount": 5},
                 {"ratingKey": "t2", "grandparentRatingKey": "a1", "parentRatingKey": "b1", "title": "Leave a Trace"},
                 {"ratingKey": "t3", "grandparentRatingKey": "a2", "parentRatingKey": "b2", "title": "The Mother We Share", "viewCount": 2}]}
    plex._paged = lambda sid, kind, **k: iter(data[kind])
    rows = plex.artists()
    assert len(rows) == 1 and rows[0]["key"] == "chvrches" and rows[0]["plays"] == 7
    assert sorted(rows[0]["albums"]) == ["Every Open Eye", "The Bones Of What You Believe"]
    assert plex.songs["chvrches|themotherweshare"] == "t3"


def test_hiding_one_song_keeps_the_rest_of_its_album(client):
    c, _ = client
    c.post("/api/liked/hide", json={"id": "al4Idioteque"})   # a song's own id
    d = c.get("/api/liked").json()
    kid_a = next(a for a in d["albums"] if a["id"] == "al4")
    assert kid_a["songs"] == ["Everything In Its Right Place"] and d["hidden"] == 1
    assert [t["title"] for t in kid_a["tracks"]] == ["Everything In Its Right Place"] and kid_a["tracks"][0]["added_at"]
