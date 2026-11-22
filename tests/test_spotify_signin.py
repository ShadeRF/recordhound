"""Connecting Spotify: one click through the helper page, straight back on HTTPS, or by pasting the address."""
import os
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402

from app import clients, config, db  # noqa: E402
from app.clients import APIError, SPOTIFY_HELPER, SPOTIFY_LOOPBACK  # noqa: E402


class Answer:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


@pytest.fixture
def app(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    config.save({"SPOTIFY_CLIENT_ID": "client-id", "SPOTIFY_REDIRECT_URI": "", "SPOTIFY_REFRESH_TOKEN": "", "SPOTIFY_CONNECTED_AT": 0})
    db.set_meta("spotify_auth", None)
    db.set_meta("spotify_error", None)
    swaps = []

    def token_endpoint(url, data=None, **kw):
        swaps.append(data)
        if data.get("code") == "bad-code":
            return Answer(400, {"error": "invalid_grant", "error_description": "Invalid authorization code"})
        if data.get("code") == "garbled":
            return Answer(502, None)
        return Answer(200, {"refresh_token": "refresh-" + data["code"], "access_token": "a"})
    monkeypatch.setattr(clients.requests, "post", token_endpoint)
    read = []
    monkeypatch.setattr(m, "_spotify_read", lambda token: read.append(token) or [])
    monkeypatch.setattr(m.threading, "Thread", lambda target, daemon=None: type("T", (), {"start": lambda self: target()})())
    c = TestClient(m.app, base_url="http://tower:8789", follow_redirects=False)
    yield c, swaps, read
    config.save({"SPOTIFY_CLIENT_ID": "", "SPOTIFY_REDIRECT_URI": "", "SPOTIFY_REFRESH_TOKEN": "", "SPOTIFY_CONNECTED_AT": 0})


def _start(c, back="http://tower:8789/"):
    d = c.post("/api/spotify/start", json={"back": back}).json()
    return d, db.get_meta("spotify_auth")


def test_one_click_through_the_helper_page(app):
    c, swaps, read = app
    d, attempt = _start(c)
    assert d["helper"] and d["url"].startswith(SPOTIFY_HELPER + "#")
    hand = urllib.parse.parse_qs(d["url"].split("#", 1)[1])
    go = urllib.parse.urlparse(hand["go"][0])
    q = urllib.parse.parse_qs(go.query)
    assert go.netloc == "accounts.spotify.com" and q["redirect_uri"] == [SPOTIFY_HELPER] and q["state"] == [attempt["state"]]
    assert q["code_challenge_method"] == ["S256"] and "client_secret" not in q and hand["back"] == ["http://tower:8789/"]
    # the helper page sends the browser here
    r = c.get(f"/callback?code=good&state={attempt['state']}")
    assert r.status_code == 303 and r.headers["location"] == "./?spotify=connected#/settings"
    assert config.SPOTIFY_REFRESH_TOKEN == "refresh-good" and config.SPOTIFY_CONNECTED_AT > time.time() - 60
    assert swaps[-1]["redirect_uri"] == SPOTIFY_HELPER and swaps[-1]["code_verifier"] == attempt["verifier"]
    assert read == ["refresh-good"]
    # a sign-in can only finish once
    r = c.get(f"/callback?code=good&state={attempt['state']}")
    assert r.headers["location"] == "./?spotify=expired#/settings" and len(swaps) == 1


def test_callback_refuses_what_this_server_did_not_start(app):
    c, swaps, _ = app
    _, attempt = _start(c)
    for q in ("code=x&state=someone-elses", "code=x", f"code=x&state={attempt['state']}x"):
        assert c.get("/callback?" + q).headers["location"] == "./?spotify=expired#/settings"
    assert swaps == [] and config.SPOTIFY_REFRESH_TOKEN == ""
    # too old
    db.set_meta("spotify_auth", {**attempt, "at": int(time.time()) - 3600})
    assert c.get(f"/callback?code=x&state={attempt['state']}").headers["location"] == "./?spotify=expired#/settings"
    assert swaps == []


def test_cancelled_and_failed_sign_ins_say_why(app):
    c, _, _ = app
    _, attempt = _start(c)
    assert c.get(f"/callback?error=access_denied&state={attempt['state']}").headers["location"] == "./?spotify=denied#/settings"
    _, attempt = _start(c)
    assert c.get(f"/callback?code=bad-code&state={attempt['state']}").headers["location"] == "./?spotify=failed#/settings"
    s = c.get("/api/settings").json()
    assert "Invalid authorization code" in s["spotify_error"] and config.SPOTIFY_REFRESH_TOKEN == ""
    _, attempt = _start(c)
    c.get(f"/callback?code=garbled&state={attempt['state']}")
    assert "502" in c.get("/api/settings").json()["spotify_error"]
    _, attempt = _start(c)
    c.get(f"/callback?code=fine&state={attempt['state']}")
    assert c.get("/api/settings").json()["spotify_error"] is None, "an old failure shouldn't linger after it works"


def test_paste_the_address_still_works(app):
    c, swaps, read = app
    _, attempt = _start(c)
    pasted = f"https://shaderf.github.io/recordhound-auth/?code=pasted&state={attempt['state']}"
    r = c.post("/api/spotify/finish", json={"url": pasted})
    assert r.status_code == 200 and config.SPOTIFY_REFRESH_TOKEN == "refresh-pasted" and read == ["refresh-pasted"]
    # the old way: loopback address, no state in it
    config.save({"SPOTIFY_REDIRECT_URI": SPOTIFY_LOOPBACK})
    d, attempt = _start(c)
    assert not d["helper"] and urllib.parse.parse_qs(urllib.parse.urlparse(d["url"]).query)["redirect_uri"] == [SPOTIFY_LOOPBACK]
    assert c.post("/api/spotify/finish", json={"url": "http://127.0.0.1:8789/callback?code=old"}).status_code == 200
    assert swaps[-1]["redirect_uri"] == SPOTIFY_LOOPBACK and config.SPOTIFY_REFRESH_TOKEN == "refresh-old"
    # Spotify said no: say so, even though there's no code in the address
    _start(c)
    r = c.post("/api/spotify/finish", json={"url": "http://127.0.0.1:8789/callback?error=access_denied"})
    assert r.status_code == 400 and "cancelled" in r.json()["detail"]
    # a code from a different attempt
    _start(c)
    r = c.post("/api/spotify/finish", json={"url": "http://127.0.0.1:8789/callback?code=x&state=not-this-one"})
    assert r.status_code == 400 and "Connect Spotify again" in r.json()["detail"]


def test_https_install_comes_straight_back(app):
    c, swaps, _ = app
    config.save({"SPOTIFY_REDIRECT_URI": "https://music.example.com/callback"})
    d, attempt = _start(c, back="https://music.example.com/")
    assert not d["helper"] and d["url"].startswith("https://accounts.spotify.com/authorize?")
    assert c.get(f"/callback?code=direct&state={attempt['state']}").headers["location"] == "./?spotify=connected#/settings"
    assert swaps[-1]["redirect_uri"] == "https://music.example.com/callback"


def test_start_checks_its_inputs(app):
    c, _, _ = app
    assert c.post("/api/spotify/start", json={"back": "javascript:alert(1)"}).status_code == 400
    assert c.post("/api/spotify/start", json={"back": "http://user:pw@evil.example/"}).status_code == 400
    assert c.post("/api/spotify/start", json={}).status_code == 400
    d = c.post("/api/spotify/start", json={"back": "https://example.com/recordhound"}).json()
    assert urllib.parse.parse_qs(d["url"].split("#", 1)[1])["back"] == ["https://example.com/recordhound/"]
    config.save({"SPOTIFY_REDIRECT_URI": "ftp://nope"})
    assert c.post("/api/spotify/start", json={"back": "http://tower:8789/"}).status_code == 400
    config.save({"SPOTIFY_CLIENT_ID": "", "SPOTIFY_REDIRECT_URI": ""})
    assert c.post("/api/spotify/start", json={"back": "http://tower:8789/"}).status_code == 400


def _forget_saved(name):
    """As if this setting had never been saved (an install from before it existed)."""
    import json
    path = os.path.join(config.DATA_DIR, "settings.json")
    with open(path) as f:
        saved = json.load(f)
    saved.pop(name, None)
    with open(path, "w") as f:
        json.dump(saved, f)
    config.load()


def test_existing_spotify_setups_keep_their_address(app):
    import app.main as m
    # set up before one-click sign-in: Spotify only knows the old address, so keep using it
    _forget_saved("SPOTIFY_REDIRECT_URI")
    assert config.sources()["SPOTIFY_REDIRECT_URI"] == "default" and config.SPOTIFY_CLIENT_ID == "client-id"
    m._keep_old_spotify_redirect()
    assert config.SPOTIFY_REDIRECT_URI == SPOTIFY_LOOPBACK
    assert m.get_settings()["spotify_redirect"] == SPOTIFY_LOOPBACK
    # someone setting up Spotify for the first time gets one-click sign-in
    config.save({"SPOTIFY_CLIENT_ID": ""})
    _forget_saved("SPOTIFY_REDIRECT_URI")
    m._keep_old_spotify_redirect()
    assert config.sources()["SPOTIFY_REDIRECT_URI"] == "default" and m.get_settings()["spotify_redirect"] == SPOTIFY_HELPER


def test_callback_needs_sign_in_when_it_is_on(app):
    from app import auth
    c, swaps, _ = app
    _, attempt = _start(c)
    config.save({"AUTH_USERNAME": "matt", "AUTH_PASSWORD_HASH": auth.hash_password("correct horse")})
    try:
        r = c.get(f"/callback?code=x&state={attempt['state']}")
        assert r.status_code in (302, 307) and r.headers["location"].endswith("/login") and swaps == []
    finally:
        config.save({"AUTH_USERNAME": "", "AUTH_PASSWORD_HASH": ""})


def test_pasted_address_parsing():
    assert clients.spotify_pasted("https://x/?code=abc&state=s1") == ("abc", "s1")
    assert clients.spotify_pasted("  abc  ") == ("abc", None)
    assert clients.spotify_pasted("code=abc&state=s2") == ("abc", "s2")
    with pytest.raises(APIError):
        clients.spotify_pasted("http://127.0.0.1:8789/callback?error=access_denied&state=s")
