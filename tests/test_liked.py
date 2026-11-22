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


class PlayResp(FakeResp):
    pass


def spotify_with(devices, play_status=204, scopes="user-modify-playback-state user-read-playback-state"):
    sp = object.__new__(clients.Spotify)
    sp.scopes = set(scopes.split())
    sp._get = lambda url, **p: {"devices": devices}
    sp.sent = []

    class S:
        def put(self, url, params=None, json=None, timeout=None):
            sp.sent.append((params, json))
            return FakeResp({}, play_status)
    sp.s = S()
    return sp


def test_spotify_plays_on_the_active_device_or_an_open_app():
    sp = spotify_with([{"id": "phone", "name": "Phone", "type": "Smartphone"}, {"id": "pc", "name": "Desk PC", "type": "Computer"}])
    assert sp.play("track", "4uLU6hMCjMI75M1A2tKUQC") == "Desk PC"   # on a computer: the computer's app
    assert sp.sent[-1] == ({"device_id": "pc"}, {"uris": ["spotify:track:4uLU6hMCjMI75M1A2tKUQC"]})
    assert sp.play("track", "4uLU6hMCjMI75M1A2tKUQC", on="phone") == "Phone"
    # whatever is playing in the living room is left alone
    sp = spotify_with([{"id": "pc", "name": "Desk PC", "type": "Computer"}, {"id": "avr", "name": "Receiver", "type": "AVR", "is_active": True}])
    assert sp.play("album", "1DFixLWuPkv3KT3TnV35m3") == "Desk PC"
    assert sp.sent[-1] == ({"device_id": "pc"}, {"context_uri": "spotify:album:1DFixLWuPkv3KT3TnV35m3"})
    for others in ([], [{"id": "avr", "type": "AVR", "is_active": True}, {"id": "spk", "type": "Speaker"}, {"id": "tv", "type": "TV"}]):
        with pytest.raises(clients.SpotifyNoDevice):
            spotify_with(others).play("track", "4uLU6hMCjMI75M1A2tKUQC")
    with pytest.raises(APIError, match="Premium"):
        spotify_with([{"id": "pc", "type": "Computer"}], 403).play("track", "4uLU6hMCjMI75M1A2tKUQC")
    assert not spotify_with([], scopes="user-top-read").can_play()


def test_play_route_explains_what_to_do(client, monkeypatch):
    c, m = client
    tid = "4uLU6hMCjMI75M1A2tKUQC"
    monkeypatch.setattr(m, "_spotify_client", lambda: spotify_with([{"id": "pc", "name": "Desk PC", "type": "Computer"}]))
    assert c.post("/api/spotify/play", json={"kind": "track", "id": tid}).json() == {"ok": True, "device": "Desk PC"}
    monkeypatch.setattr(m, "_spotify_client", lambda: spotify_with([]))
    assert c.post("/api/spotify/play", json={"kind": "track", "id": tid}).json()["reason"] == "no_device"
    monkeypatch.setattr(m, "_spotify_client", lambda: spotify_with([], scopes="user-top-read"))
    assert c.post("/api/spotify/play", json={"kind": "track", "id": tid}).json()["reason"] == "reconnect"
    assert c.post("/api/spotify/play", json={"kind": "playlist", "id": tid}).status_code == 400
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "")
    assert c.post("/api/spotify/play", json={"kind": "track", "id": tid}).json()["reason"] == "not_connected"


def test_liked_singles_are_linked_to_the_album_they_ended_up_on(client, monkeypatch):
    c, m = client
    from app.sync import Syncer
    import test_pipeline as tp
    calls = []

    class FakeDeezer:
        def find_artist(self, name):
            calls.append(("artist", name))
            return {"id": 9, "name": name}

        def albums(self, artist_id):
            return [{"id": 1, "title": "Whitney Whitney (Live)", "type": "album", "date": "2027-01-01"},
                    {"id": 2, "title": "Debut", "type": "album", "date": "2026-09-01", "cover": "dzcover"},
                    {"id": 3, "title": "Isabelle", "type": "single", "date": "2026-01-02"}]

        def album_tracks(self, album_id):
            calls.append(("tracks", album_id))
            return [{"title": "Isabelle (Album Version)"}, {"title": "Another One"}] if album_id in (1, 2) else []
    for k in ("dz:whitneywhitney", "dz:bellvie", "dzalb:1", "dzalb:2"):
        db.conn().execute("DELETE FROM cache WHERE k=?", (k,))
    db.commit()
    db.set_meta("single_links", None)
    s = Syncer(plex_factory=tp.FakePlex, deezer_factory=FakeDeezer, artist_factory=lambda: (tp.FakeLastFM(), "lf:", None), sources={"spotify_liked": LIKED})
    s.link_singles()
    links = db.get_meta("single_links")
    assert links["al2Isabelle"]["album"]["title"] == "Debut"   # the studio album, not the live one
    assert links["al1A Man Written by a Woman"]["album"] is None and "al3Feels So Far Away" in links
    d = c.get("/api/liked").json()
    debut = next(a for a in d["albums"] if a["id"] == "dz:2")
    assert debut["title"] == "Debut" and debut["type"] == "album" and debut["linked"] and debut["cover"] == "dzcover"
    assert debut["tracks"][0]["single"] == "Isabelle" and not any(a["id"] == "al2" for a in d["albums"])
    sent = []

    class FakeLidarr:
        def __init__(self, *a):
            pass

        def add_album(self, artist, title, *a):
            sent.append((artist, title))
            return title
    monkeypatch.setattr(m, "Lidarr", FakeLidarr)
    c.post("/api/liked/lidarr", json={"id": "dz:2", "quality_id": 1, "metadata_id": 1, "root": "/m"})
    assert sent == [("Whitney Whitney", "Debut")]   # Lidarr gets the album, not the single
    n = len(calls)
    s.link_singles()   # found ones aren't looked up again; ones without an album wait two weeks
    assert len(calls) == n
    db.set_meta("single_links", None)


def test_spotify_finds_the_same_song_by_isrc_or_name():
    sp = object.__new__(clients.Spotify)
    asked = []

    def get(url, **p):
        asked.append(p["q"])
        if p["q"] == "isrc:GBAYE0601498":
            return {"tracks": {"items": [{"id": "byisrc"}]}}
        if p["type"] == "track":
            return {"tracks": {"items": [{"id": "wrong", "name": "Idioteque", "artists": [{"name": "Somebody Else"}]},
                                         {"id": "right", "name": "Idioteque - Remastered", "artists": [{"name": "Radiohead"}]}]}}
        return {"albums": {"items": [{"id": "kida", "name": "Kid A (Deluxe Edition)", "artists": [{"name": "Radiohead"}]}]}}
    sp._get = get
    assert sp.find("track", "Karma Police", "Radiohead", "GBAYE0601498") == "byisrc"
    assert sp.find("track", "Idioteque", "Radiohead") == "right"
    assert sp.find("album", "Kid A", "Radiohead") == "kida"
    assert sp.find("track", "Nothing Like It", "Radiohead") is None


def test_play_match_remembers_what_it_found(client, monkeypatch):
    c, m = client
    found = []
    sp = spotify_with([{"id": "pc", "name": "Desk PC", "type": "Computer"}])
    sp.find = lambda kind, title, artist, isrc=None: found.append(title) or ("x" * 22 if title != "Missing" else None)
    monkeypatch.setattr(m, "_spotify_client", lambda: sp)
    db.conn().execute("DELETE FROM cache WHERE k LIKE 'spfind:%'")
    db.commit()
    body = {"title": "Idioteque", "artist": "Radiohead", "isrc": "GBAYE0000351"}
    assert c.post("/api/spotify/play-match", json=body).json() == {"ok": True, "device": "Desk PC"}
    c.post("/api/spotify/play-match", json=body)
    assert found == ["Idioteque"] and sp.sent[-1][1] == {"uris": ["spotify:track:" + "x" * 22]}
    assert c.post("/api/spotify/play-match", json={"title": "Missing", "artist": "Radiohead"}).json()["reason"] == "not_found"
    sp_off = spotify_with([{"id": "avr", "name": "Receiver", "type": "AVR", "is_active": True}])
    sp_off.find = sp.find
    monkeypatch.setattr(m, "_spotify_client", lambda: sp_off)
    r = c.post("/api/spotify/play-match", json=body).json()   # no Spotify app open: the page gets the song to open in the app
    assert r == {"ok": False, "reason": "no_device", "id": "x" * 22} and not sp_off.sent
    monkeypatch.setattr(m, "_spotify_client", lambda: sp)
    assert c.post("/api/spotify/play-match", json={"kind": "album", "title": "Kid A", "artist": "Radiohead"}).json()["ok"]
    assert sp.sent[-1][1] == {"context_uri": "spotify:album:" + "x" * 22}


def test_an_album_counts_as_owned_when_you_have_every_track(client, monkeypatch):
    c, m = client
    db.set_meta("plex_songs", {"pensacolamist|thedyinglight": "1", "pensacolamist|eyesonyou": "2", "pensacolamist|lostinlove": "3"})
    tracks = [{"title": "The Dying Light", "artist": "Pensacola Mist"}, {"title": "Eyes On You", "artist": "Pensacola Mist"},
              {"title": "Lost In Love", "artist": "Pensacola Mist"}]
    for aid, title, want in ((70, "Lost In Love", True), (71, "Lost In Love (Instrumentals)", False)):
        db.cache_put(f"dzalb:{aid}", tracks if aid == 70 else [{**t, "title": t["title"] + " (Instrumental)"} for t in tracks])
        db.commit()
        d = c.get(f"/api/album/{aid}/tracks?name=Pensacola%20Mist&album={title}").json()
        assert d["all_owned"] is want and all(t["owned"] is want for t in d["tracks"]), title
    assert not m._owns_every_track(tracks[:2], "Pensacola Mist", "Lost In Love - Live", db.get_meta("plex_songs"))
    assert not m._owns_every_track([tracks[0], {"title": "Unknown"}], "Pensacola Mist", "Other", db.get_meta("plex_songs"))
    assert m._owns_every_track(tracks, "Pensacola Mist", "Lost In Love (Deluxe)", db.get_meta("plex_songs"))
    assert not m._tracks_owned([{"title": "Live Forever"}], "Oasis", "", {})[0]   # nothing known about Plex yet
    db.conn().execute("DELETE FROM cache WHERE k IN ('dzalb:70', 'dzalb:71')")
    db.commit()


def test_bracketed_titles_match_with_or_without_the_brackets():
    from app.clients import same_song
    assert same_song("About Nothing (For the Love of Destiny)", "About Nothing for the Love of Destiny")
    assert same_song("Norwegian Wood (This Bird Has Flown)", "Norwegian Wood")
    assert same_song("Karma Police - Remastered 2017", "Karma Police")
    assert same_song("Die For You (feat. The Midnight)", "Die For You")
    assert not same_song("About Nothing (For the Love of Destiny)", "About Something")
    plex = object.__new__(clients.Plex)
    plex.sections = lambda: [{"key": "1", "title": "Music"}]
    data = {8: [{"ratingKey": "a1", "title": "P.M. Dawn"}], 9: [{"ratingKey": "b1", "parentRatingKey": "a1", "title": "The Bliss Album...?"}],
            10: [{"ratingKey": "t7", "grandparentRatingKey": "a1", "parentRatingKey": "b1", "title": "About Nothing (For the Love of Destiny)"}]}
    plex._paged = lambda sid, kind, **k: iter(data[kind])
    plex.artists()
    import app.main as m
    assert m._tracks_owned([{"title": "About Nothing for the Love of Destiny"}], "P.M. Dawn", "The Bliss Album...?", plex.songs) == [True]


def test_albums_youre_missing_follows_the_eps_and_special_release_settings(client, monkeypatch):
    c, m = client
    from app.clients import special_release
    assert all(special_release(t) for t in ("Synthetic (Remixes)", "The Midnight Remixed", "Heroes (& Instrumentals)",
                                            "Siberia (Acoustic)", "Live at Wembley", "Album - Live"))
    assert not any(special_release(t) for t in ("Live Through This", "Alive", "The Listening (Deluxe Version)", "Endless Summer"))
    al = lambda t, kind="album", fans=1: {"title": t, "type": kind, "fans": fans, "special": special_release(t)}
    db.set_meta("gaps", {"at": 1, "groups": [
        {"key": "themidnight", "name": "The Midnight", "owned": 8, "albums": [al("Monsters", fans=9), al("Synthetic (Remixes)"), al("Horizons EP", "ep")]},
        {"key": "remixonly", "name": "Remix Only", "owned": 1, "albums": [al("Hits (Remixes)")]},
        {"key": "old", "name": "Older Sync", "owned": 2, "albums": [{"title": "Plain", "type": "album"}]}]})
    for eps, special, want in ((False, False, {"The Midnight": ["Monsters"], "Older Sync": ["Plain"]}),
                               (True, False, {"The Midnight": ["Monsters", "Horizons EP"], "Older Sync": ["Plain"]}),
                               (True, True, {"The Midnight": ["Monsters", "Synthetic (Remixes)", "Horizons EP"], "Remix Only": ["Hits (Remixes)"], "Older Sync": ["Plain"]})):
        c.put("/api/settings", json={"GAPS_EPS": eps, "GAPS_SPECIAL": special})
        d = c.get("/api/gaps").json()
        assert {g["name"]: [a["title"] for a in g["albums"]] for g in d["groups"]} == want
        assert next(g for g in d["groups"] if g["name"] == "The Midnight")["total"] == 8 + len(want["The Midnight"])
    c.put("/api/settings", json={"GAPS_EPS": False, "GAPS_SPECIAL": False})
    db.set_meta("gaps", None)


def test_soundtracks_and_compilations():
    from app.clients import is_compilation, owned_track
    assert all(is_compilation(n) for n in ("Various Artists", "VA", "Original Soundtrack", "Original Motion Picture Soundtrack",
                                           "Soundtrack", "OST", "Original Broadway Cast", "Original Cast Recording", "Original Score"))
    assert not any(is_compilation(n) for n in ("John Williams", "Hans Zimmer", "The Soundtrack of Our Lives", "Cast", "Original"))
    plex = object.__new__(clients.Plex)
    plex.sections = lambda: [{"key": "1", "title": "Music"}]
    data = {8: [{"ratingKey": "va", "title": "Various Artists"}, {"ratingKey": "jw", "title": "John Williams"}],
            9: [{"ratingKey": "drive", "parentRatingKey": "va", "title": "Drive (Original Motion Picture Soundtrack)"},
                {"ratingKey": "sw", "parentRatingKey": "jw", "title": "Star Wars"}],
            10: [{"ratingKey": "t1", "grandparentRatingKey": "va", "parentRatingKey": "drive", "title": "Nightcall",
                  "originalTitle": "Kavinsky; Lovefoxxx", "viewCount": 12},
                 {"ratingKey": "t2", "grandparentRatingKey": "va", "parentRatingKey": "drive", "title": "A Real Hero",
                  "originalTitle": "College & Electric Youth", "viewCount": 3},
                 {"ratingKey": "t3", "grandparentRatingKey": "va", "parentRatingKey": "drive", "title": "Tick of the Clock"},
                 {"ratingKey": "t4", "grandparentRatingKey": "jw", "parentRatingKey": "sw", "title": "Main Title", "viewCount": 5,
                  "originalTitle": "John Williams; London Symphony Orchestra"}]}
    plex._paged = lambda sid, kind, **k: iter(data[kind])
    rows = plex.artists()
    assert [r["name"] for r in rows] == ["John Williams"] and rows[0]["plays"] == 5   # a composer's score is his
    comp = plex.comp_albums
    assert len(comp) == 1 and comp[0]["title"].startswith("Drive") and comp[0]["compilation"] and comp[0]["artists"][0] == "kavinsky"
    assert plex.comp_plays["kavinsky"]["plays"] == 12 and plex.comp_plays["college"]["plays"] == 3
    # a liked song on a soundtrack is owned, even when the track doesn't name its artist
    assert owned_track({"title": "Nightcall", "artists": ["Kavinsky"], "album": "Drive"}, plex.songs) == "t1"
    assert owned_track({"title": "Tick of the Clock", "artists": ["The Chromatics"],
                        "album": "Drive (Original Motion Picture Soundtrack)"}, plex.songs) == "t3"
    # an unplayed soundtrack shows in Your Library, judged by the artists on it
    from app import engine
    seeds = {"kavinsky": {"w": 2.0, "name": "Kavinsky", "listen": 0.8}}
    comp = [{**comp[0], "plays": 0}]   # never played
    out = engine.unplayed_albums(comp, seeds, {}, {}, {}, set())
    assert out and out[0]["title"].startswith("Drive") and out[0]["why"] == "Features Kavinsky, who you play a lot"
    assert engine.unplayed_albums(comp, seeds, {}, {}, {}, {"kavinsky"})   # others on it still count
    assert not engine.unplayed_albums(comp, seeds, {}, {}, {}, {"kavinsky", "college"})   # everyone on it excluded


def test_soundtrack_settings(client, monkeypatch):
    c, m = client
    db.set_meta("unplayed", [{"rk": "1", "title": "Drive (Original Motion Picture Soundtrack)", "artist": "Various Artists",
                              "artist_key": "", "compilation": True},
                             {"rk": "2", "title": "Kid A", "artist": "Radiohead", "artist_key": "radiohead"}])
    assert c.get("/api/library").json()["total"] == 2
    monkeypatch.setattr(config, "COMP_LIBRARY", False)
    assert [a["title"] for a in c.get("/api/library").json()["items"]] == ["Kid A"]
    db.set_meta("unplayed", None)


def test_compilations_and_reissues_count_as_special_releases():
    from app.clients import special_release
    assert all(special_release(t) for t in ("Greatest Hits", "The Best of Blur", "B-Sides & Rarities", "Anthology", "The Singles",
                                            "The Essential Clash", "Rare Tracks", "The Collection", "TKOL RMX 1234567"))
    owned = ["OK Computer (Deluxe)", "Kid A"]
    assert special_release("OK Computer OKNOTOK 1997 2017", owned) and special_release("KID A MNESIA", owned)
    assert not special_release("Kid A", owned) and not special_release("Amnesiac", owned) and not special_release("OK Computer OKNOTOK")


def test_a_different_mix_is_not_the_album_you_own():
    from app.clients import owns_every_track, special_release
    songs = {f"marvingaye|{k}": "1" for k in ("whatsgoingon", "whatshappeningbrother", "mercymercyme")}
    original = [{"title": "What's Going On"}, {"title": "What's Happening Brother"}, {"title": "Mercy Mercy Me (The Ecology)"}]
    detroit = [{"title": t["title"] + " (Detroit Mix)"} for t in original]
    assert owns_every_track(original, "Marvin Gaye", "What's Going On", songs)
    assert not owns_every_track(detroit, "Marvin Gaye", "What's Going On: The Detroit Mix", songs)
    assert not owns_every_track(original, "Marvin Gaye", "What's Going On: The Detroit Mix", songs)   # the album says it's a mix
    assert owns_every_track(original, "Marvin Gaye", "What's Going On (Deluxe Edition)", songs)        # a deluxe edition still counts
    assert special_release("What's Going On: The Detroit Mix") and special_release("Songs - 2021 Mix")
    assert not special_release("Mixed Emotions") and not special_release("Mix Tape")


def test_albums_show_their_original_year_not_the_reissue():
    from app.clients import original_date
    groups = [{"title": "What's Going On", "date": "1971-05-21"}, {"title": "Let's Get It On", "date": "1973-08-28"},
              {"title": "Marvin Gaye Live!", "date": "1974-06"}]
    assert original_date({"title": "What's Going On", "date": "2020-01-01"}, groups) == "1971-05-21"
    assert original_date({"title": "Let's Get It On (Deluxe Edition)", "date": "2023-03-01"}, groups) == "1973-08-28"
    assert original_date({"title": "Marvin Gaye Live!", "date": "2025-01-01"}, groups) == "1974-06"
    assert original_date({"title": "The Very Best Of Marvin Gaye", "date": "2018-01-01"}, groups) == "2018-01-01"   # no original to go by
    assert original_date({"title": "Brand New", "date": "2026-03-01"}, groups + [{"title": "Brand New", "date": "2026-03-01"}]) == "2026-03-01"
    assert original_date({"title": "What's Going On", "date": "2020-01-01"}, None) == "2020-01-01"   # nothing known yet


def test_albums_you_have_every_track_of_leave_albums_youre_missing(client):
    c, m = client
    db.set_meta("plex_songs", {"pmdawn|intro": "1", "pmdawn|plastic": "2"})
    db.cache_put("dzalb:801", [{"title": "Intro"}, {"title": "Plastic"}])
    db.commit()
    db.set_meta("gaps", {"at": 1, "groups": [{"key": "pmdawn", "name": "P.M. Dawn", "owned": 1, "albums": [
        {"id": 801, "title": "The Bliss Album...?", "type": "album", "fans": 9}, {"id": 802, "title": "Jesus Wept", "type": "album", "fans": 5}]}]})
    g = c.get("/api/gaps").json()["groups"][0]
    assert [a["title"] for a in g["albums"]] == ["Jesus Wept"] and g["owned"] == 2 and g["total"] == 3
    db.set_meta("gaps", None)
    db.conn().execute("DELETE FROM cache WHERE k='dzalb:801'")
    db.commit()


def test_matching_report_points_at_near_misses(client):
    c, m = client
    keep = db.get_meta("plex_songs")
    lib = next(iter(db.library_map().values()), None)
    if lib is None:
        return
    k = lib["key"]
    db.set_meta("plex_songs", {f"{k}|intro": "1", f"{k}|breakthru": "2", f"{k}|outro": "3", f"{k}|lovesong": "4"})
    db.cache_put("dz:" + k, {"id": 1, "albums": [{"id": 901, "title": "Report Album", "type": "album"}]})
    db.cache_put("dzalb:901", [{"title": "Intro"}, {"title": "Break Through"}, {"title": "Outro"}, {"title": "Love Song"}])
    db.set_meta("spotify_liked", [{"title": "Love Is A Song", "artists": [lib["name"]], "album": "X"}])   # close, but not a typo
    db.commit()
    try:
        d = c.get("/api/matching-report").json()
        al = next(a for a in d["albums_nearly_complete"] if a["album"] == "Report Album")
        assert al["tracks"] == "3/4" and al["unmatched"][0]["title"] == "Break Through" and "breakthru" in al["unmatched"][0]["closest_in_plex"]
        assert d["liked_songs_with_a_close_title_in_plex"][0]["closest_in_plex"] == ["lovesong"]
    finally:
        db.set_meta("plex_songs", keep)
        db.conn().execute("DELETE FROM cache WHERE k IN (?, 'dzalb:901')", ("dz:" + k,))
        db.set_meta("spotify_liked", LIKED)
        db.commit()


def test_numbered_titles_match_however_they_are_written():
    from app.clients import same_song
    assert same_song("Shine On You Crazy Diamond (Pts. I-V)", "Shine On You Crazy Diamond")
    assert same_song("Another Brick in the Wall, Pt. 2", "Another Brick in the Wall (Part II)")
    assert same_song("Symphony No. 9", "Symphony Number 9")
    assert same_song("Vol. 3", "Volume III")
    assert not same_song("Another Brick in the Wall, Pt. 1", "Another Brick in the Wall, Pt. 2")
    assert not same_song("I Will", "Will")


def test_matching_rules_from_a_real_library():
    from app.clients import owned_track, song_keys, tracks_owned
    keys = lambda a, *titles: {f"{a}|{k}": "1" for t in titles for k in song_keys(t)}
    songs = {**keys("bilmuri", "Neon Medusa (Live)", "Lifeisgood"), **keys("michaeljackson", "Beat It (Demo)", "Beat It"),
             **keys("countingcrows", "Big Yellow Taxi"), **keys("ataris", "Bite My Tongue", "If You Really Wanna Hear About It"),
             **keys("bearmccreary", "Dreilide Thrace Sonata No. 1"), **keys("gunship", "Tech Noir II"),
             **keys("johnwilliams", "Star Wars & the Revenge of the Sith"), **keys("bingcrosby", "I'm Through With Love"),
             **keys("tearsforfears", "Everybody Wants to Rule the World"), **keys("brothertiger", "Wallow"),
             **keys("loversinjapan", "x"), **keys("coldplay", "Lovers in Japan")}
    have = lambda artist, *titles, album="Album": tracks_owned([{"title": t} for t in titles], artist, album, songs)
    assert have("Bilmuri", "Neon Medusa (Live)") == [True]                    # the live version you own
    assert have("Bilmuri", "Lifeisgood (Live)") == [False]                    # only the studio one in Plex
    assert have("Michael Jackson", "Beat It (Demo)") == [True]
    assert have("Counting Crows", "Big Yello Taxi") == [True]                 # a typo on Deezer's side
    assert have("The Ataris", "Bite My Tounge", "If You Really Want To Hear About It") == [True, True]
    assert have("Bear McCreary", "Dreilide Thrace Sonata #1") == [True]
    assert have("GUNSHIP", "Tech Noir 2") == [True]
    assert have("John Williams", "Star Wars and the Revenge of the Sith") == [True]
    assert have("Bing Crosby", "I'm Thru With Love") == [True]
    assert have("Tears for Fears", "Everybody Wants To Run The World") == [False]   # a different song
    assert have("Brothertiger", "Shallow") == [False]
    assert have("Coldplay", "Lovers in Japan (Osaka Sun Mix)") == [False]     # a different mix
    assert owned_track({"title": "Big Yello Taxi", "artists": ["Counting Crows"]}, songs)
    assert not owned_track({"title": "Shallow", "artists": ["Brothertiger"]}, songs)
    album = {"album:exitdemo|bedroom": "1", **{f"album:exitdemo|{k}": "1" for k in song_keys("Flight")}}
    assert tracks_owned([{"title": "Bedroom"}, {"title": "Flight"}], "Daphne Loves Derby", "Exit (Demo)", album) == [True, True]


def test_name_check_lists_titles_that_look_misspelled_in_plex(client, monkeypatch):
    c, m = client
    from app.clients import song_keys
    keep = db.get_meta("plex_songs")
    lib = next(iter(db.library_map().values()), None)
    if lib is None:
        return
    k, owned = lib["key"], (lib.get("albums") or ["Owned Album"])[0]
    db.set_meta("plex_songs", {**{f"{k}|{x}": "7" for t in ("Intro", "Bite My Tounge") for x in song_keys(t)}})
    db.cache_put("dz:" + k, {"id": 1, "albums": [{"id": 902, "title": owned, "type": "album"}]})
    db.cache_put("dzalb:902", [{"title": "Intro"}, {"title": "Bite My Tongue"}, {"title": "Totally Different Song"}])
    db.commit()
    monkeypatch.setattr(config, "PLEX_URL", "")   # don't ask Plex in a test
    try:
        rows = m._name_mismatches()
        assert [(r["plex_key"], r["elsewhere"]) for r in rows if r["album"] == owned] == [("bitemytounge", "Bite My Tongue")]
        page = c.get("/tools/names").text
        assert "Name check" in page and "Bite My Tongue" in page and "bitemytounge" in page
    finally:
        db.set_meta("plex_songs", keep)
        db.conn().execute("DELETE FROM cache WHERE k IN (?, 'dzalb:902')", ("dz:" + k,))
        db.commit()


def test_matching_rules_from_the_second_report():
    from app.clients import song_keys, tracks_owned
    keys = lambda a, *titles: {f"{a}|{k}": "1" for t in titles for k in song_keys(t)}
    songs = {**keys("corrs", "Toss the Feathers (Instrumental)"), **keys("marvingaye", "If I Could Build My World Around You",
             "Little Boy, Little Ole Girl"), **keys("bingcrosby", "Adeste Fidelis (O Come All Ye Faithful)"),
             **keys("johnwilliams", "City in the Clouds"), **keys("daphnelovesderby", "Bedroom (Demo)"),
             **keys("somebody", "Somebody Loves Me"), **keys("pinkfloyd", "Another Brick in the Wall, Part 1")}
    have = lambda artist, title, album="Album": tracks_owned([{"title": title}], artist, album, songs)[0]
    assert have("The Corrs", "Toss the Feathers (Instrumental Version)")             # "Version" changes nothing
    assert have("Marvin Gaye", "If I Could Build My Whole World Around You")         # a word dropped in the middle
    assert have("Marvin Gaye", "Little Ole Boy, Little Ole Girl")
    assert have("Bing Crosby", "Adeste Fideles (O Come All Ye Faithful)")             # one letter swapped
    assert have("John Williams", "The City in the Clouds")
    assert have("Daphne Loves Derby", "Bedroom", album="Exit (Demo)")                # plain names on a demo album
    assert not have("Daphne Loves Derby", "Breathe", album="Exit (Demo)")
    assert not have("Somebody", "Somebody Loves Me Too")                             # a word added at the end: another song
    assert not have("Pink Floyd", "Another Brick in the Wall, Part 3")               # numbers never fuzzy


def test_release_kinds_for_the_artist_page_tabs():
    from app.clients import album_key, release_kind
    studio = {album_key(t) for t in ("Abandoned Luncheonette", "Voices", "Daryl Hall & John Oates", "Whole Oats")}
    kind = lambda title, t="album", date="1990-01-01": release_kind(title, t, studio, date)
    assert kind("Voices") == "album" and kind("Whole Oats (Remastered)") == "album"
    assert kind("Daryl Hall & John Oates (The Silver Album)") == "album"
    assert kind("Live at the Apollo") == "live" and kind("Livetime") == "compilation"   # live on MusicBrainz, not a studio album
    assert kind("Greatest Hits Live") == "live" and kind("The Essential Collection") == "compilation"
    assert kind("Extended Mixes") == "other" and kind("Single Mixes") == "other"
    assert kind("Hall and Oates", date="2017-01-01") == "compilation"                # not a studio album on MusicBrainz
    assert kind("Brand New Record", date="2099-01-01") == "album"                    # too new for MusicBrainz: trust it
    assert release_kind("Some Record", "ep") == "ep" and release_kind("Some Record") == "album"   # no MusicBrainz list


def test_matching_rules_from_the_third_report():
    from app.clients import _exact_key, same_song, song_keys, tracks_owned
    assert same_song("(It's Hard To Live Up To) The Rock (Album Version)", "(It's Hard To Live Up To) The Rock")
    assert same_song("Straight For The Heart (2018 Remaster)", "Straight For The Heart")
    assert same_song("Act V, Scene V: And So It Ends Like It Begins", "And So It Ends Like It Begins")
    assert not same_song("Symphony No. 5: I. Allegro", "Symphony No. 7: I. Allegro")   # short tails don't count
    assert _exact_key("Neon Medusa (Live) (2021 Remaster)") == _exact_key("Neon Medusa (Live)")
    assert _exact_key("Runaway (Live Version)") == _exact_key("Runaway (Live)")      # but versions are kept
    songs = {f"shenandoah|{k}": "1" for k in song_keys("(It's Hard To Live Up To) The Rock")}
    assert tracks_owned([{"title": "(It's Hard To Live Up To) The Rock (Album Version)"}], "Shenandoah", "Super Hits", songs) == [True]


def test_soundtrack_titles_from_the_fourth_report():
    from app.clients import _exact_key, same_song
    assert same_song('Main Title / Looks Like a Suicide (From "Dance with Wolves" Soundtrack)', "Main Title - Looks Like a Suicide")
    assert same_song('Fire Dance (From "Dance with Wolves" Soundtrack', "Fire Dance")          # bracket never closed
    assert _exact_key('Ben (Live) (From "Michael")') == _exact_key("Ben (Live)")
    assert same_song("Song - Remastered 2011", "Song")
