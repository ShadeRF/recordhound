"""Security behaviour: cross-site requests, headers, secret handling, sign-in, uploads."""
import io
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, config, db, security  # noqa: E402
import app.main as m  # noqa: E402

db.init()
c = TestClient(m.app, base_url="http://tower:9999")


def test_headers():
    r = c.get("/api/health")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_cross_site_blocked():
    # a page on another site trying to wipe your hidden list
    r = c.post("/api/maintenance/clear-feedback", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    r = c.post("/api/maintenance/clear-feedback", headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    # the app itself, and scripts without browser headers, still work
    assert c.post("/api/maintenance/clear-feedback", headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert c.post("/api/maintenance/clear-feedback", headers={"origin": "http://tower:9999"}).status_code == 200
    assert c.post("/api/maintenance/clear-feedback").status_code == 200
    assert c.get("/api/health", headers={"sec-fetch-site": "cross-site"}).status_code == 200   # reads aren't blocked


def test_saved_secret_never_sent_to_new_address():
    config.save({"PLEX_URL": "http://192.168.1.20:32400", "PLEX_TOKEN": "realtoken123456"})
    masked = config.public()["PLEX_TOKEN"]
    # attacker points Plex at their own server but leaves the token masked
    r = c.post("/api/settings/test/plex", json={"PLEX_URL": "http://evil.example:32400", "PLEX_TOKEN": masked})
    assert r.status_code == 400 and "enter the" in r.json()["detail"].lower()
    r = c.put("/api/settings", json={"PLEX_URL": "http://evil.example:32400"})
    assert r.status_code == 400 and config.PLEX_URL == "http://192.168.1.20:32400"
    # changing the address *with* a new token is fine
    r = c.put("/api/settings", json={"PLEX_URL": "http://192.168.1.50:32400", "PLEX_TOKEN": "newtoken654321"})
    assert r.status_code == 200 and config.PLEX_URL == "http://192.168.1.50:32400"
    # same host, different path: still allowed without re-entering
    r = c.put("/api/settings", json={"PLEX_URL": "http://192.168.1.50:32400/"})
    assert r.status_code == 200
    # addresses written so they look like the saved host but lead somewhere else
    for trick in (r"http://evil.example\@192.168.1.50:32400", "http://192.168.1.50:32400@evil.example",
                  "http://evil.example#@192.168.1.50:32400", "http://evil.example\t@192.168.1.50:32400"):
        r = c.post("/api/settings/test/plex", json={"PLEX_URL": trick, "PLEX_TOKEN": masked})
        assert r.status_code == 400, trick
        assert c.put("/api/settings", json={"PLEX_URL": trick}).status_code == 400, trick
    assert config.PLEX_URL.rstrip("/") == "http://192.168.1.50:32400"
    assert security._host("http://[::1]:32400/web") == ("::1", 32400) and security._host("tower:32400") == ("tower", 32400)


def test_error_messages_hide_keys():
    config.save({"PLEX_TOKEN": "supersecrettoken99"})

    @m.app.get("/api/_boom")
    def boom():
        raise m.HTTPException(502, "GET http://x/3/movie?api_key=abc123 failed for supersecrettoken99")
    detail = c.get("/api/_boom").json()["detail"]
    assert "abc123" not in detail and "supersecrettoken99" not in detail and "failed" in detail

    # a service failing where a route didn't expect it is a readable 502, not a bare server error
    import requests

    @m.app.get("/api/_down")
    def down(kind: str):
        if kind == "api":
            raise m.APIError("The service rejected the API key")
        raise requests.exceptions.ConnectionError("GET http://x/?api_key=abc123 refused for supersecrettoken99")
    r = c.get("/api/_down?kind=api")
    assert r.status_code == 502 and "rejected the API key" in r.json()["detail"]
    r = c.get("/api/_down?kind=net")
    assert r.status_code == 502 and "abc123" not in r.text and "supersecrettoken99" not in r.text


def test_broken_session_cookie_is_a_sign_in_not_a_crash():
    import base64
    config.save({"AUTH_USERNAME": "matt", "AUTH_PASSWORD_HASH": auth.hash_password("correct horse")})
    try:
        for raw in ("a|1|é", "a|notanumber|" + "0" * 64, "nonsense"):
            token = base64.urlsafe_b64encode(raw.encode()).decode()
            assert auth.valid(token) is False
            assert c.get("/api/status", headers={"cookie": f"{auth.COOKIE}={token}"}).status_code == 401
    finally:
        config.save({"AUTH_USERNAME": "", "AUTH_PASSWORD_HASH": ""})
        c.cookies.clear()


def test_redaction():
    config.save({"PLEX_TOKEN": "supersecrettoken99"})
    s = security.redact("GET http://x/library?X-Plex-Token=supersecrettoken99&api_key=abc123 failed: supersecrettoken99")
    assert "supersecrettoken99" not in s and "abc123" not in s


def test_login_throttle_and_password_change_signs_out():
    config.save({"AUTH_USERNAME": "matt", "AUTH_PASSWORD_HASH": auth.hash_password("correct horse")})
    try:
        for _ in range(security.MAX_FAILS):
            assert c.post("/api/login", json={"username": "matt", "password": "nope"}).status_code == 401
        assert c.post("/api/login", json={"username": "matt", "password": "correct horse"}).status_code == 429
        security.login_ok("testclient")
        r = c.post("/api/login", json={"username": "matt", "password": "correct horse"})
        assert r.status_code == 200
        token = r.cookies.get(auth.COOKIE)
        assert auth.valid(token)
        config.save({"AUTH_PASSWORD_HASH": auth.hash_password("new password!")})
        assert not auth.valid(token), "old sessions survive a password change"
        assert c.get("/api/status", cookies={auth.COOKIE: token}).status_code == 401
    finally:
        config.save({"AUTH_USERNAME": "", "AUTH_PASSWORD_HASH": ""})
        c.cookies.clear()


def test_zip_bomb_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.csv", b"0" * (5 * 1024 * 1024))
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        try:
            security.zip_read(z, "big.csv", [1024 * 1024])
            raise AssertionError("read past the budget")
        except ValueError:
            pass
        assert len(security.zip_read(z, "big.csv", [10 * 1024 * 1024])) == 5 * 1024 * 1024


def test_upload_size_limit_without_a_declared_length():
    import asyncio

    class Body:
        def __init__(self, chunks, headers=None):
            self.chunks, self.headers, self.read = chunks, headers or {}, 0

        async def stream(self):
            for ch in self.chunks:
                self.read += len(ch)
                yield ch
    assert asyncio.run(security.read_upload(Body([b"ab", b"cd"]), 10)) == b"abcd"
    for body in (Body([b"x" * 6] * 50), Body([], {"content-length": "999"})):
        try:
            asyncio.run(security.read_upload(body, 10))
            raise AssertionError("an oversized upload was accepted")
        except m.HTTPException as e:
            assert e.status_code == 413
        assert body.read <= 12, "kept reading after the limit was passed"


def test_login_failures_are_forgotten():
    security._fails.clear()
    old = security.time.time() - security.WINDOW - 5
    security._fails.update({f"10.0.{i // 250}.{i % 250}": [old] for i in range(1500)})
    security.login_failed("192.0.2.1")
    assert len(security._fails) == 1 and not security.login_blocked("192.0.2.1")
    security._fails.clear()


def test_tls_settings(monkeypatch, tmp_path):
    from app import serve
    monkeypatch.delenv("TLS_CERT", raising=False)
    monkeypatch.delenv("TLS_KEY", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")   # the standard CA-bundle variable must not switch HTTPS on
    assert serve.tls() == {}
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x"), key.write_text("y")
    monkeypatch.setenv("TLS_CERT", str(cert))
    monkeypatch.setenv("TLS_KEY", str(key))
    assert serve.tls() == {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    monkeypatch.setenv("TLS_KEY", str(tmp_path / "missing.pem"))
    try:
        serve.tls()
        raise AssertionError("started HTTPS with a missing key")
    except SystemExit as e:
        assert "missing" in str(e)
