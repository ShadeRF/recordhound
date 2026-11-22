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
        for where in ("home", "picks", "rate", "settings", "liked"):
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


def test_saved_keys_show_only_their_last_characters_and_typed_keys_can_be_revealed(page):
    go(page, "settings")
    page.evaluate("document.getElementById('svc-plex').open = true")
    field = page.locator('#settingsBody [data-k="PLEX_TOKEN"]')
    eye = field.locator("xpath=following-sibling::button[@data-eye]")
    note = page.locator("#svc-plex .saved-note")
    # the saved token never reaches the page; its last four characters show so you can tell which one it is
    assert "test-token" not in page.content()
    assert field.get_attribute("type") == "text" and field.input_value() == "••••••••oken"
    assert eye.is_hidden() and note.is_visible()
    # a new key stays hidden while you type, and the eye shows it only on request
    field.fill("brand-new-token")
    assert field.get_attribute("type") == "password" and eye.is_visible() and note.is_hidden()
    eye.click()
    assert field.get_attribute("type") == "text" and eye.get_attribute("aria-pressed") == "true"
    eye.click()
    assert field.get_attribute("type") == "password"
    field.fill("")
    assert eye.is_hidden()


def test_youtube_music_history_upload_and_remove(page, tmp_path):
    import json
    f = tmp_path / "watch-history.json"
    f.write_text(json.dumps([{"header": "YouTube Music", "title": "Watched Idioteque", "titleUrl": "https://music.youtube.com/watch?v=a",
                              "subtitles": [{"name": "Radiohead - Topic", "url": "https://www.youtube.com/channel/UCx"}],
                              "time": "2026-11-02T21:14:03.120Z"}] * 4))
    go(page, "settings")
    page.evaluate("document.getElementById('svc-youtube').open = true")
    card = page.locator("#svc-youtube")
    assert "Not set up" in card.locator("summary").inner_text()
    page.set_input_files("#ytFile", str(f))
    page.wait_for_function("document.querySelector('#svc-youtube summary').innerText.includes('History uploaded')")
    assert "1 artist imported" in card.inner_text()
    card.locator("#ytRemove").click()
    page.wait_for_function("document.querySelector('#svc-youtube summary').innerText.includes('Not set up')")


def test_liked_not_owned_lists_missing_songs_and_hides_albums(page, monkeypatch):
    from app import config, db
    song = lambda t, a, al, aid, when: {"id": aid + t, "title": t, "artists": [a], "album": al, "album_id": aid, "album_type": "single",
                                        "album_artist": a, "year": "2026", "cover": None, "added_at": when}
    db.set_meta("spotify_liked", [song("Isabelle", "Whitney Whitney", "Isabelle", "al1", "2026-02-01"),
                                  song("Feels So Far Away", "Bellvie", "Feels So Far Away", "al2", "2026-01-01"),
                                  song("Owned One", "Mine", "Mine", "al3", "2025-01-01")])
    db.set_meta("plex_songs", {"mine|ownedone": "42"})
    db.set_meta("liked_hidden", None)
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "r")
    try:
        go(page, "stream")
        at = page.locator("#subnav").bounding_box()
        page.click("#subnav [data-go=liked]")
        settle(page)
        assert page.locator("#nav button.on").inner_text() == "You Stream"
        rows = page.locator(".lk-row")
        assert rows.count() == 2 and "Isabelle" in rows.first.inner_text()
        assert "2 songs of the 3 you've liked" in view(page).inner_text()
        rows.first.locator(".lk-main").click()   # anywhere on the row opens the artist
        page.wait_for_selector("#drawer h2")
        assert page.locator("#drawer h2").inner_text() == "Whitney Whitney"
        page.keyboard.press("Escape")
        assert "of the 1 you own" in page.locator("#lkPl").inner_text()
        rows.first.locator("[data-lk-hide]").click()
        page.wait_for_function("document.querySelectorAll('.lk-row').length === 1")
        assert "1 song hidden" in view(page).inner_text() and "1 song of the 3" in view(page).inner_text()
        page.reload(); settle(page)
        assert page.locator(".lk-row").count() == 1 and "1 song hidden" in view(page).inner_text()   # remembered
        assert page.locator("#subnav").bounding_box() == at   # the switch doesn't move between the two pages
        page.click("#subnav [data-go=stream]")   # and back to the artists
        settle(page)
        assert view(page).get_attribute("id") == "v-grid" and page.locator("#subnav [data-go=stream].on").count() == 1
    finally:
        for k in ("spotify_liked", "plex_songs", "liked_hidden"):
            db.set_meta(k, None)


def test_liked_not_owned_keeps_loading_as_you_scroll(page, monkeypatch):
    from app import config, db
    db.set_meta("spotify_liked", [{"id": f"t{i}", "title": f"Song {i}", "artists": [f"Artist {i}"], "album": f"Album {i}", "album_id": f"al{i}",
                                   "album_type": "album", "album_artist": f"Artist {i}", "year": "2020", "cover": None,
                                   "added_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"} for i in range(130)])
    db.set_meta("plex_songs", {})
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "r")
    try:
        go(page, "liked")
        assert page.locator(".lk-row").count() == 60 and not page.locator("#lkMore").count()
        for want in (120, 130):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_function(f"document.querySelectorAll('.lk-row').length === {want}")
    finally:
        db.set_meta("spotify_liked", None); db.set_meta("plex_songs", None)


def test_albums_on_an_artist_page_open_with_links_and_lidarr(page, site, monkeypatch):
    from app import config, db
    lib = next(r for r in db.library_map().values() if r.get("albums"))
    owned = lib["albums"][0]
    before = db.conn().execute("SELECT json, at FROM cache WHERE k=?", ("dz:" + lib["key"],)).fetchone()
    db.cache_put("dz:" + lib["key"], {"id": 1, "name": lib["name"], "albums": [
        {"id": 11, "title": owned, "type": "album", "date": "2001-01-01", "cover": None},
        {"id": 22, "title": "Not Yet Mine", "type": "album", "date": "2024-05-01", "cover": None}]})
    db.cache_put("dzalb:22", [{"n": 1, "disc": 1, "title": "First Song", "duration": 200, "preview": "https://cdn.test/1.mp3", "artist": lib["name"]},
                               {"n": 2, "disc": 1, "title": "Second Song", "duration": 65, "preview": None, "artist": lib["name"]}])
    db.set_meta("plex_songs", {lib["key"] + "|firstsong": "9"})
    db.commit()
    monkeypatch.setattr(config, "LIDARR_URL", "http://lidarr.test"); monkeypatch.setattr(config, "LIDARR_API_KEY", "k")
    sent = []

    class FakeLidarr:
        def __init__(self, *a):
            pass

        def options(self):
            return {"profiles": [{"id": 1, "name": "Lossless"}], "metadata": [{"id": 2, "name": "Standard"}], "roots": [{"path": "/music", "free": None}]}

        def add_album(self, artist, title, q, md, root):
            sent.append((artist, title, q, md, root))
            return title
    monkeypatch.setattr(site["m"], "Lidarr", FakeLidarr)
    try:
        go(page, "home")
        page.evaluate("([k, n]) => openArtist(k, n)", [lib["key"], lib["name"]])
        page.wait_for_selector("#drawer [data-alb]")
        page.click("#drawer [data-alb='0']")   # newest first: the one you don't own
        box = page.locator("#albBox")
        assert box.is_visible() and "Not Yet Mine" in box.inner_text() and "deezer.com/album/22" in box.inner_html()
        page.wait_for_selector("#albTracks .atrk")
        tracks = page.locator("#albTracks .atrk")
        assert tracks.count() == 2 and "3:20" in tracks.first.inner_text() and "1:05" in tracks.nth(1).inner_text()
        assert tracks.first.locator(".own-t").count() == 1 and tracks.nth(1).locator(".pb.off").count() == 1
        assert "You own 1 of these 2 tracks" in box.inner_text()
        box.locator("[data-alb-lidarr]").click()
        box.locator("[data-lk-add]").click()
        page.wait_for_function("document.querySelector('#albBox .albl').innerText.includes('Sent to Lidarr')")
        assert sent == [(lib["name"], "Not Yet Mine", 1, 2, "/music")]
        page.click("#drawer [data-alb='1']")   # the one you own: no Lidarr button
        assert owned in box.inner_text() and "in your library" in box.inner_text() and not box.locator("[data-alb-lidarr]").count()
        page.click("#drawer [data-alb='1']")   # a second click closes it
        assert box.is_hidden()
    finally:   # leave the cache as it was, for the tests that run after this one
        db.conn().execute("DELETE FROM cache WHERE k=?", ("dz:" + lib["key"],))
        db.conn().execute("DELETE FROM cache WHERE k='dzalb:22'")
        db.set_meta("plex_songs", None)
        if before:
            db.conn().execute("INSERT INTO cache(k, json, at) VALUES (?, ?, ?)", ("dz:" + lib["key"], before[0], before[1]))
        db.commit()


def test_liked_not_owned_sorts_groups_and_filters(page, monkeypatch):
    from app import config, db
    song = lambda t, a, al, aid, when, year: {"id": aid + t, "title": t, "artists": [a], "album": al, "album_id": aid, "album_type": "album",
                                              "album_artist": a, "year": year, "cover": None, "added_at": when}
    db.set_meta("spotify_liked", [song("Isabelle", "Whitney Whitney", "Isabelle", "al1", "2026-02-01", "2026"),
                                  song("Feels So Far Away", "Bellvie", "Feels So Far Away", "al2", "2026-01-01", "2025"),
                                  song("A Man Written by a Woman", "Whitney Whitney", "A Man Written", "al3", "2025-06-01", "2024"),
                                  song("Lead Me Home", "The California Honeydrops", "A River's Invitation", "al4", "2025-01-01", "2015")])
    db.set_meta("plex_songs", {})
    db.set_meta("liked_hidden", None)
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "r")
    titles = lambda: page.locator(".lk-row .lk-t").all_inner_texts()
    try:
        go(page, "liked")
        page.evaluate("localStorage.removeItem('likedView2')")
        page.reload(); settle(page)
        # one row per song by default, newest like first
        assert titles() == ["Isabelle", "Feels So Far Away", "A Man Written by a Woman", "Lead Me Home"]
        assert page.locator("[data-lk-group=none].on").count() == 1 and "♥" in page.locator(".lk-row").first.inner_text()
        page.evaluate("liked.albums[0].tracks[0].id = '4uLU6hMCjMI75M1A2tKUQC'; renderLikedList()")   # a real-looking Spotify id
        assert page.locator(".lk-row").first.locator(".lk-sp").get_attribute("href") == "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        assert page.locator(".lk-row").first.locator(".lk-sp").inner_text().strip() == "Play on Spotify"
        assert page.locator(".lk-row").first.locator(".lk-sp").get_attribute("data-app") == "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
        page.select_option("#lkSort", "artist")
        assert titles() == ["Feels So Far Away", "Lead Me Home", "Isabelle", "A Man Written by a Woman"]
        page.select_option("#lkSort", "new")
        page.click("[data-lk-group=album]")   # one row per release
        assert titles() == ["Isabelle", "Feels So Far Away", "A Man Written", "A River's Invitation"]
        page.select_option("#lkSort", "artist")
        assert titles() == ["Feels So Far Away", "A River's Invitation", "Isabelle", "A Man Written"]   # "The" is ignored
        page.select_option("#lkSort", "year")
        assert titles()[0] == "Isabelle" and titles()[-1] == "A River's Invitation"
        page.click("[data-lk-group=artist]")
        heads = page.locator(".lk-ahead b").all_inner_texts()
        assert heads == ["Whitney Whitney", "Bellvie", "The California Honeydrops"]
        assert "2 songs · 2 releases" in page.locator(".lk-ahead").first.inner_text()
        page.fill("#lkQ", "isab")
        page.wait_for_function("document.querySelectorAll('.lk-row').length === 1")
        assert page.locator("#lkCount").inner_text() == "1 of 4 releases"
        page.locator(".lk-ahead").first.click()   # an artist heading opens the artist
        page.wait_for_selector("#drawer h2")
        assert page.locator("#drawer h2").inner_text() == "Whitney Whitney"
        page.keyboard.press("Escape")
        page.reload(); settle(page)   # the choices are remembered
        assert page.locator("#lkSort").input_value() == "year" and page.locator("[data-lk-group=artist].on").count() == 1
    finally:
        page.evaluate("localStorage.removeItem('likedView2')")
        for k in ("spotify_liked", "plex_songs"):
            db.set_meta(k, None)
