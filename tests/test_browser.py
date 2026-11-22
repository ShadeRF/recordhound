"""The real pages in a real browser (Chrome), against a test library, with nothing reaching the internet.

Needs Playwright and Google Chrome: pip install playwright. Skipped when either is missing, except in the build,
which sets REQUIRE_BROWSER=1 so these can never be skipped quietly there.
"""
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402

REQUIRED = os.environ.get("REQUIRE_BROWSER") == "1"
try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright  # noqa: E402
except ImportError:
    if REQUIRED:
        raise
    pytest.skip("Playwright isn't installed (pip install playwright)", allow_module_level=True)

HELPER_DIR = os.path.join(os.path.dirname(__file__), "..", "spotify-helper")
PLEX_SECTIONS = [{"key": "1", "title": "Music", "type": "artist"}, {"key": "2", "title": "Live recordings", "type": "artist"},
                 {"key": "3", "title": "Audiobooks", "type": "artist"}]


class FakePlexAPI:
    """Stands in for Plex in the running app: answers Test, records ratings."""
    rated = []

    def __init__(self, *a, **k):
        pass

    def _get(self, path, **params):
        return {"friendlyName": "Test Plex", "version": "1.40.0"} if path == "/" else {"Directory": PLEX_SECTIONS}

    def all_sections(self):
        return PLEX_SECTIONS

    def rate(self, rating_key, rating):
        FakePlexAPI.rated.append((rating_key, rating))

    def thumb(self, path, size=300):
        raise OSError("no covers in tests")

    def machine_id(self):
        return "test-machine"


class NoDeezer:
    def find_artist(self, name):
        return {"missing": True}

    def search_artists(self, q, limit=30):
        return []


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class HelperPage(http.server.SimpleHTTPRequestHandler):
    """Serves spotify-helper/ at /recordhound-auth/, as GitHub Pages does."""
    def translate_path(self, path):
        p = urllib.parse.urlparse(path).path
        rel = p[len("/recordhound-auth/"):] if p.startswith("/recordhound-auth/") else "missing"
        return os.path.join(HELPER_DIR, rel or "index.html")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    import requests
    import uvicorn
    from app import clients, config, db
    import app.main as m
    import test_features as tf
    import test_pipeline as tp

    db.init()
    s = tp.Syncer(lambda: tf.AlbumPlex(), lambda: tp.FakeLastFM(), tp.FakeDeezer,
                  artist_factory=lambda: (tp.FakeLastFM(), "lf:", None), sources={})
    s.run(full=True)
    assert s.state["error"] is None, s.state

    test_settings = {"PLEX_URL": "http://plex.test:32400", "PLEX_TOKEN": "test-token", "SYNC_HOUR": -1, "LASTFM_API_KEY": "k"}
    keep = {k: getattr(config, k) for k in test_settings}
    config.save(test_settings)   # saved, not just set: saving anything else reloads every setting from disk
    real = {"request": requests.Session.request, "Plex": m.Plex, "rescore": m.schedule_rescore, "dz": m._dz,
            "source": m.artist_source, "helper_m": m.SPOTIFY_HELPER, "helper_c": clients.SPOTIFY_HELPER}

    def offline(self, method, url, *a, **k):
        raise requests.ConnectionError(f"offline test: {url}")
    requests.Session.request = offline          # the app can't reach Deezer, MusicBrainz, Plex or anything else
    m.Plex, m.schedule_rescore, m._dz = FakePlexAPI, (lambda *a, **k: None), NoDeezer()
    m.artist_source = lambda key, dz=None: (tp.FakeLastFM(), "lf:", None)
    helper = _serve(HelperPage)
    helper_url = f"http://127.0.0.1:{helper.server_address[1]}/recordhound-auth/"
    m.SPOTIFY_HELPER = clients.SPOTIFY_HELPER = helper_url   # a local copy of the GitHub Pages helper

    server = uvicorn.Server(uvicorn.Config(m.app, host="127.0.0.1", port=_free_port(), log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the app didn't start"
    yield {"base": f"http://127.0.0.1:{server.config.port}/", "helper": helper_url, "m": m, "config": config, "db": db,
           "clients": clients}
    server.should_exit = True
    thread.join(10)
    helper.shutdown()
    requests.Session.request = real["request"]
    m.Plex, m.schedule_rescore, m._dz, m.artist_source = real["Plex"], real["rescore"], real["dz"], real["source"]
    m.SPOTIFY_HELPER, clients.SPOTIFY_HELPER = real["helper_m"], real["helper_c"]
    config.save(keep)


@pytest.fixture(scope="module")
def browser():
    pw = sync_playwright().start()
    b = None
    for opts in ({"channel": "chrome"}, {}):   # the installed Chrome, else Playwright's own (playwright install chromium)
        try:
            b = pw.chromium.launch(**opts)
            break
        except PlaywrightError:
            continue
    if b is None:
        pw.stop()
        if REQUIRED:
            raise RuntimeError("No browser for the browser tests: install Google Chrome, or run: playwright install chromium")
        pytest.skip("No Chrome found for the browser tests")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser, site):
    """A fresh browser tab on the app. Fails the test on any script error in the page."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    base = site["base"]

    def only_local(route):   # artist photos and favicons come from the internet: not in tests
        url = route.request.url
        local = url.startswith((base, site["helper"], "data:", "blob:"))
        route.continue_() if local else route.abort()
    ctx.route("**/*", only_local)
    p = ctx.new_page()
    p.errors = []
    p.on("pageerror", lambda e: p.errors.append(f"script error: {e}"))
    p.on("console", lambda msg: p.errors.append(f"console: {msg.text}")
         if msg.type == "error" and "Failed to load resource" not in msg.text else None)
    p.base, p.ctx = base, ctx
    yield p
    errors = list(p.errors)
    ctx.close()
    assert not errors, errors


def go(page, where):
    page.goto(page.base + "#/" + where)
    settle(page)


def settle(page):
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(150)


def view(page):
    return page.locator(".view.on").first


def test_every_page_loads(page):
    for where, sec in (("home", "v-home"), ("picks", "v-grid"), ("stream", "v-grid"), ("library", "v-list"),
                       ("saved", "v-list"), ("rate", "v-rate"), ("history", "v-history"), ("settings", "v-settings"),
                       ("gaps", "v-list"), ("releases", "v-list"), ("search/gaze", "v-list"), ("search/100%25", "v-list"),
                       ("like/gazeband1/Gaze%20Band%201", "v-list"), ("nonsense", "v-home")):
        go(page, where)
        assert view(page).get_attribute("id") == sec, where
        text = view(page).inner_text()
        assert text.strip() and "undefined" not in text and "[object Object]" not in text, (where, text[:200])
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"


def test_hiding_shows_everywhere_and_undo_brings_it_back(page):
    go(page, "picks")
    card = page.locator("#grid .card[data-key]").first
    key = card.get_attribute("data-key")
    card.hover()
    card.locator(".quick[data-hide]").click()
    settle(page)
    go(page, "home")
    assert page.locator(f'#homeRows .card[data-key="{key}"]').count() == 0, "a hidden artist still shows on For You"
    go(page, "picks")
    assert page.locator(f'#grid .card[data-key="{key}"]').count() == 0
    go(page, "history")
    hidden = page.locator(f'#hiddenGrid .hcard[data-key="{key}"]')
    assert hidden.count() == 1
    hidden.locator("[data-undo]").click()
    settle(page)
    go(page, "picks")
    assert page.locator(f'#grid .card[data-key="{key}"]').count() == 1, "undo didn't bring it back"


def test_fast_page_switching_ends_on_the_last_page(page):
    go(page, "home")
    for v in ("saved", "gaps", "releases", "library", "history", "rate", "gaps", "saved", "library", "gaps"):
        page.evaluate("v => location.hash = '#/' + v", v)
    settle(page)
    page.wait_for_timeout(500)
    assert page.locator("#listBody h2").first.inner_text() == "Albums you're missing"


def test_rate_page(page, site):
    db = site["db"]
    FakePlexAPI.rated.clear()
    go(page, "rate")
    name = lambda: page.locator(".rate-info h2").first.inner_text()   # noqa: E731
    first = name()
    page.keyboard.press("Control+s")            # the browser's shortcut, not "skip"
    page.wait_for_timeout(200)
    assert name() == first
    page.keyboard.press("4")
    page.keyboard.press("4")                    # a fast double press rates one artist, not two
    settle(page)
    assert len(FakePlexAPI.rated) == 1 and FakePlexAPI.rated[0][1] == 8, FakePlexAPI.rated
    rated_key = next(k for k, r in db.library_map().items() if r["plex_key"] == FakePlexAPI.rated[0][0])
    assert name() != first
    page.locator('[data-ra="skip"]').click()
    settle(page)
    # picks you might already know
    page.locator('#rateSeg [data-mode="picks"]').click()
    settle(page)
    loved = name()
    page.keyboard.press("Shift+Digit5")         # 4½ stars
    settle(page)
    known = name()
    page.keyboard.press("k")                    # know them, no rating
    settle(page)
    page.keyboard.press("x")                    # not for me
    settle(page)
    fb = {v["name"]: v for v in db.feedback().values()}
    try:
        assert fb[loved]["action"] == "known" and fb[loved]["rating"] == 9
        assert fb[known]["action"] == "known" and fb[known]["rating"] is None
        assert len([v for v in fb.values() if v["action"] == "dismissed"]) >= 1
        go(page, "history")
        page.locator('#pills [data-ht="known"]').click()
        settle(page)
        card = page.locator("#hiddenGrid .hcard", has_text=loved)
        assert "★★★★½" in card.inner_text()
        go(page, "picks")
        assert page.locator("#grid .card", has_text=loved).count() == 0, "an artist you know is still suggested"
    finally:
        for k, v in list(db.feedback().items()):
            if v["name"] in (loved, known) or v["action"] == "dismissed":
                db.clear_feedback(k)
        db.set_library_rating(rated_key, None)


def test_rate_from_the_artist_panel(page, site):
    db = site["db"]
    go(page, "picks")
    card = page.locator("#grid .card[data-key]").first
    key, name = card.get_attribute("data-key"), card.get_attribute("data-name")
    card.click()
    settle(page)
    page.locator("[data-rate-open]").click()
    stars = page.locator("#seenStars [data-star]")
    assert stars.count() == 5
    stars.nth(3).focus()
    page.keyboard.press("Enter")                # keyboard: a whole star, 4 of them
    settle(page)
    try:
        assert db.feedback()[key]["rating"] == 8 and db.feedback()[key]["action"] == "known"
        assert not page.locator("#drawer").evaluate("d => d.classList.contains('on')")
        assert "Rated" in page.locator("#toast").inner_text()
        assert page.locator(f'#grid .card[data-key="{key}"]').count() == 0 or name is None
    finally:
        db.clear_feedback(key)


def test_album_without_a_cover_keeps_its_title_clear_of_the_date(page):
    go(page, "releases")
    card = page.locator(".card.al", has=page.locator(".match")).filter(has=page.locator(".poster > span")).first
    assert card.count() == 1, "the test library has an announced album with no cover"
    # a long title fills the card, which is when it used to run under the badge
    card.locator(".poster > span").evaluate("e => e.textContent = 'An Orchestral Evening with the Choir (Live at the Concert Hall, Recorded Over Two Nights in Reykjavik) '.repeat(3)")
    badge, title = card.locator(".match").bounding_box(), card.locator(".poster > span").bounding_box()
    pad = card.locator(".poster > span").evaluate("e => parseFloat(getComputedStyle(e).paddingTop)")
    text_top = title["y"] + pad   # where the words start inside the title box
    assert text_top >= badge["y"] + badge["height"], (badge, title)


def test_library_filter_keeps_focus_while_typing(page):
    go(page, "library")
    page.locator("#libN").select_option("100")
    box = page.locator("#libQ")
    box.click()
    box.press_sequentially("ep", delay=120)
    settle(page)
    page.wait_for_timeout(400)
    assert page.evaluate("document.activeElement && document.activeElement.id") == "libQ"
    assert page.locator("#libQ").input_value() == "ep" and page.locator("#libN").input_value() == "100"


def test_setup_walkthrough_keeps_every_library_ticked(page, site):
    config = site["config"]
    keep = config.PLEX_URL, config.PLEX_TOKEN
    config.PLEX_URL, config.PLEX_TOKEN = "", ""     # a new install
    try:
        page.goto(page.base)
        settle(page)
        assert page.evaluate("location.hash") == "#/setup"
        page.locator('[data-wz="next"]').click()
        page.locator('[data-wz-k="PLEX_URL"]').fill("http://plex.test:32400")
        page.locator('[data-wz-k="PLEX_TOKEN"]').fill("test-token")
        page.locator('[data-wz="test"]').click()
        settle(page)
        boxes = page.locator("[data-wz-lib]")
        assert boxes.count() == 3 and all(boxes.nth(i).is_checked() for i in range(3))
        boxes.nth(1).uncheck()
        boxes.nth(1).check()                        # all ticked again means "every library"
        page.locator('[data-wz="test"]').click()    # anything that redraws the step
        settle(page)
        boxes = page.locator("[data-wz-lib]")
        assert all(boxes.nth(i).is_checked() for i in range(3)), "boxes show unticked though every library will be read"
    finally:
        config.PLEX_URL, config.PLEX_TOKEN = keep


def test_spotify_connects_in_one_click(page, site, monkeypatch):
    """Settings → Connect → helper page → Spotify (faked) → helper page → back in Settings, connected."""
    config, clients = site["config"], site["clients"]

    class Answer:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body
    swaps = []
    def token_endpoint(url, data=None, **k):   # swaps the code for a refresh token; renewals don't rotate it
        swaps.append(data)
        return Answer({"access_token": "a"} if data["grant_type"] == "refresh_token" else {"refresh_token": "refresh-" + data["code"], "access_token": "a"})
    monkeypatch.setattr(clients.requests, "post", token_endpoint)
    config.save({"SPOTIFY_CLIENT_ID": "test-client", "SPOTIFY_REDIRECT_URI": "", "SPOTIFY_REFRESH_TOKEN": ""})
    seen = []

    def spotify(route):   # Spotify's sign-in page: approve straight away and send the browser back
        q = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
        seen.append(q)
        back = q["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "fresh-code", "state": q["state"][0]})
        route.fulfill(status=200, content_type="text/html", body=f"<script>location.replace({json.dumps(back)})</script>")
    page.ctx.route("https://accounts.spotify.com/**", spotify)
    try:
        go(page, "settings")
        page.locator('[data-fold="spotify"] > summary').click()
        assert site["helper"] in page.locator("#svc-spotify").inner_text(), "the card should show the address to register"
        assert not page.locator("#spPasteBox").is_visible(), "one-click mode shouldn't ask to paste anything"
        page.locator("#spPasteShow").click()
        assert page.locator("#spPasteBox").is_visible()
        page.locator("#spConnect").click()
        # the address is #/settings before and after, so wait for the result itself, not the address
        page.wait_for_function("() => (document.getElementById('toast') || {}).innerText?.includes('Spotify connected')", timeout=20000)
        settle(page)
        assert page.url == page.base + "#/settings"
        assert seen and seen[0]["redirect_uri"] == [site["helper"]] and seen[0]["code_challenge_method"] == ["S256"]
        assert swaps[0]["code"] == "fresh-code" and swaps[0]["redirect_uri"] == site["helper"]
        assert config.SPOTIFY_REFRESH_TOKEN == "refresh-fresh-code"
        assert "Spotify connected" in page.locator("#toast").inner_text()
        assert page.evaluate("location.search") == "", "the result shouldn't stay in the address"
        assert "Connected" in page.locator("#svc-spotify").inner_text()
    finally:
        config.save({"SPOTIFY_CLIENT_ID": "", "SPOTIFY_REFRESH_TOKEN": "", "SPOTIFY_CONNECTED_AT": 0, "SPOTIFY_REDIRECT_URI": ""})


def test_older_spotify_setup_switches_in_one_click(page, site):
    """Set up before one-click sign-in: one button moves it over, then Reconnect goes through the helper page."""
    config = site["config"]
    loopback = site["clients"].SPOTIFY_LOOPBACK
    config.save({"SPOTIFY_CLIENT_ID": "test-client", "SPOTIFY_REDIRECT_URI": loopback, "SPOTIFY_REFRESH_TOKEN": "old", "SPOTIFY_CONNECTED_AT": 1})
    asked = []

    def spotify(route):
        asked.append(urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)["redirect_uri"][0])
        route.fulfill(status=200, content_type="text/html", body="Spotify")
    page.ctx.route("https://accounts.spotify.com/**", spotify)
    try:
        go(page, "settings")
        page.locator('[data-fold="spotify"] > summary').click()
        page.locator("#spSwitch").click()
        settle(page)
        assert config.SPOTIFY_REDIRECT_URI == "" and page.locator("#spSwitch").count() == 0
        assert "Switched" in page.locator("#toast").inner_text()
        page.locator("#spReconnect").click()
        page.wait_for_function("() => location.hostname === 'accounts.spotify.com'", timeout=15000)
        assert asked == [site["helper"]], asked
    finally:
        config.save({"SPOTIFY_CLIENT_ID": "", "SPOTIFY_REDIRECT_URI": "", "SPOTIFY_REFRESH_TOKEN": "", "SPOTIFY_CONNECTED_AT": 0})


def test_spotify_helper_page_on_its_own(page, site):
    helper = site["helper"]
    # opened in another browser: no note of where to go back, so it offers the address to paste
    page.goto(helper + "?code=abc&state=nobody")
    page.wait_for_load_state("networkidle")
    assert page.locator("#paste").is_visible() and "code=abc" in page.locator("#addr").input_value()
    # a link that would send you somewhere other than Spotify is refused
    page.goto(helper + "#" + urllib.parse.urlencode({"go": "https://evil.example/authorize?state=s&redirect_uri=" + helper,
                                                     "back": "http://127.0.0.1:1/"}))
    page.wait_for_timeout(300)
    assert page.url.startswith(helper) and "wasn't made by Recordhound" in page.locator("#msg").inner_text()
    page.goto(helper)
    assert "Settings → Spotify" in page.locator("#msg").inner_text()


def test_phone_layout(browser, site):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site["base"]) else r.abort())
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    try:
        for where in ("home", "picks", "rate", "settings"):
            p.goto(site["base"] + "#/" + where)
            p.wait_for_load_state("networkidle")
            assert p.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{where} scrolls sideways on a phone"
            box = p.locator("#gsearch").bounding_box()
            assert box and box["width"] > 60 and box["x"] + box["width"] <= 390, "search should fit in the phone header"
            gear = p.locator("#gear").bounding_box()
            assert abs(gear["y"] - box["y"]) < 12 and gear["x"] + gear["width"] <= 390, "the header should stay on one line on a phone"
        assert not errors, errors
    finally:
        ctx.close()
