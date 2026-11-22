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
    total = page.evaluate("libTotal")
    sizes = [int(v) for v in page.locator("#libN option").evaluate_all("os => os.map(o => o.value)")]
    assert sizes and max(sizes) <= total, (sizes, total)   # never offers more albums than there are
    assert "Playlist size" in page.locator(".lib-tools").inner_text()
    pick = str(sizes[0])
    page.locator("#libN").select_option(pick)
    box = page.locator("#libQ")
    box.click()
    box.press_sequentially("ep", delay=120)
    settle(page)
    page.wait_for_timeout(400)
    assert page.evaluate("document.activeElement && document.activeElement.id") == "libQ"
    shown = page.evaluate("libTotal")
    assert f"{shown} match" in view(page).inner_text() and f"{total} album" in page.locator(".list-head").inner_text()   # the title keeps the full count
    assert page.locator("#libQ").input_value() == "ep" and page.locator("#libN").input_value() == pick


def lib_states(page, attr="data-lib"):
    """{library: chosen state} as the segmented controls show them."""
    return page.evaluate(f"""() => Object.fromEntries([...document.querySelectorAll('[{attr}].on')].map(b => [b.getAttribute('{attr}'), b.dataset.state]))""")


def test_setup_walkthrough_libraries_use_own_only_or_ignore(page, site):
    config = site["config"]
    keep = config.PLEX_URL, config.PLEX_TOKEN
    config.PLEX_URL, config.PLEX_TOKEN = "", ""     # a new install
    sent = []

    def put(route):   # see what Continue would save, without saving or starting a sync
        if route.request.method == "PUT":
            sent.append(json.loads(route.request.post_data))
            route.abort()
        else:
            route.continue_()
    try:
        page.goto(page.base)
        settle(page)
        assert page.evaluate("location.hash") == "#/setup"
        page.locator('[data-wz="next"]').click()
        page.locator('[data-wz-k="PLEX_URL"]').fill("http://plex.test:32400")
        page.locator('[data-wz-k="PLEX_TOKEN"]').fill("test-token")
        page.locator('[data-wz="test"]').click()
        settle(page)
        assert lib_states(page, "data-wz-lib") == {"Music": "use", "Live recordings": "use", "Audiobooks": "use"}
        rows, token = page.locator(".wz .librows").bounding_box(), page.locator('[data-wz-k="PLEX_TOKEN"]').bounding_box()
        assert abs(rows["x"] - token["x"]) < 4, "the library rows line up with the fields above, not indented again"
        assert "Connected to Test Plex v1.40.0 · 3 music libraries" in page.locator(".wz .tres").inner_text()
        assert "Own only" in page.locator(".wz .libfield .help").inner_text()
        page.locator('[data-wz-lib="Live recordings"][data-state="ignore"]').click()
        page.locator('[data-wz-lib="Audiobooks"][data-state="own"]').click()
        page.locator('[data-wz="test"]').click()    # anything that redraws the step
        settle(page)
        assert lib_states(page, "data-wz-lib") == {"Music": "use", "Live recordings": "ignore", "Audiobooks": "own"}
        page.route("**/api/settings", put)
        page.locator('[data-wz="next"]').click()
        settle(page)
        assert sent and sent[0]["PLEX_LIBRARIES"] == ["Music", "Audiobooks"] and sent[0]["NOSEED_LIBRARIES"] == ["Audiobooks"]
    finally:
        config.PLEX_URL, config.PLEX_TOKEN = keep


def test_plex_card_libraries_save_each_state(page, site):
    config = site["config"]
    keep = {k: getattr(config, k) for k in ("PLEX_LIBRARIES", "NOSEED_LIBRARIES")}

    def save():
        page.locator("#saveBtn").click()
        settle(page)
        return {k: getattr(config, k) for k in keep}
    try:
        config.save({"PLEX_LIBRARIES": ["Music", "audiobooks"], "NOSEED_LIBRARIES": ["Audiobooks"]})   # saved by an older version
        go(page, "settings")
        page.wait_for_function("document.querySelectorAll('[data-lib]').length === 9")
        page.locator("#svc-plex > summary").click()   # the Plex card opens like any other
        assert lib_states(page) == {"Music": "use", "Live recordings": "ignore", "Audiobooks": "own"}, "old settings carry over"
        assert "the Audiobooks library (own only)" in page.locator("#nsSum").inner_text()
        assert "Libraries to leave out" not in page.locator("#set-taste").inner_text()
        page.locator('[data-lib="Audiobooks"][data-state="ignore"]').click()   # Ignore wins over Own only
        assert page.locator("#savebar").is_visible()
        assert "own only" not in page.locator("#nsSum").inner_text()
        assert save() == {"PLEX_LIBRARIES": ["Music"], "NOSEED_LIBRARIES": []}
        assert "Sync now to apply" in page.locator("#toast").inner_text()
        page.locator('[data-lib="Live recordings"][data-state="own"]').click()
        assert save() == {"PLEX_LIBRARIES": ["Music", "Live recordings"], "NOSEED_LIBRARIES": ["Live recordings"]}
        page.locator('[data-lib="Live recordings"][data-state="use"]').click()
        page.locator('[data-lib="Audiobooks"][data-state="use"]').click()
        assert save() == {"PLEX_LIBRARIES": [], "NOSEED_LIBRARIES": []}, "nothing ignored is stored as every library"
        assert lib_states(page) == {"Music": "use", "Live recordings": "use", "Audiobooks": "use"}
        for t in ("Music", "Live recordings"):
            page.locator(f'[data-lib="{t}"][data-state="ignore"]').click()
        page.locator('[data-lib="Audiobooks"][data-state="ignore"]').click()   # the last one left can't be ignored
        assert "Keep at least one library to read" in page.locator("#toast").inner_text()
        assert lib_states(page)["Audiobooks"] == "use"
        page.locator("#discard").click()
    finally:
        config.save(keep)


def test_more_genres_lists_your_tags(page, site):
    config = site["config"]
    keep = config.NOSEED_TAGS
    try:
        config.save({"NOSEED_TAGS": []})
        go(page, "settings")
        more = page.locator("#nsMoreBtn")
        assert more.inner_text() == "More genres…" and page.locator("#nsMore").is_hidden()
        more.click()
        assert more.inner_text() == "Fewer genres"
        chips = page.locator("#nsAll [data-nst]")
        assert chips.first.get_attribute("data-nst") == "shoegaze" and chips.first.locator("small").inner_text() == "12"
        assert "seen live" not in page.locator("#nsAll").inner_text()
        assert page.locator("#nsFilter").get_attribute("placeholder") == "Filter tags"
        page.locator("#nsFilter").fill("POP")
        assert [c.get_attribute("data-nst") for c in chips.all()] == ["dream pop", "noise pop"]
        page.locator('#nsAll [data-nst="dream pop"]').click()
        assert page.locator('#nsAll [data-nst="dream pop"]').get_attribute("class") == "on"
        assert page.locator('#nsQuick [data-nst="dream pop"].on').count() == 1, "a choice from the full list shows among the quick picks"
        assert page.locator("#nsFilter").input_value() == "POP", "choosing keeps the filter"
        more.click()
        assert page.locator("#nsMore").is_hidden() and page.locator('#nsQuick [data-nst="dream pop"].on').is_visible()
        assert "dream pop" in page.locator("#nsSum").inner_text()
        page.locator("#saveBtn").click()
        settle(page)
        assert config.NOSEED_TAGS == ["dream pop"]
        assert page.locator('#nsQuick [data-nst="dream pop"].on').count() == 1, "still shown after saving"
        page.locator('#nsQuick [data-nst="dream pop"]').click()   # unchoosing it from the quick picks
        page.locator("#saveBtn").click()
        settle(page)
        assert config.NOSEED_TAGS == []
        assert page.locator('#nsQuick [data-nst="dream pop"]').count() == 0
    finally:
        config.save({"NOSEED_TAGS": keep})


def test_more_genres_before_the_first_sync(page):
    page.route("**/api/noseed/genres", lambda r: r.fulfill(json={"genres": []}))
    go(page, "settings")
    assert page.locator("#nsMoreBox").inner_text() == "Sync once to list your genres."
    assert page.locator("#nsMoreBtn").count() == 0


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
        assert page.locator("#nav button.on").inner_text() == "Missing"
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


def test_missing_tab_switches_between_its_four_pages(page):
    go(page, "home")
    tab = page.locator('#nav button[data-view="stream"]')
    assert tab.inner_text() == "Missing" and "You Stream" not in page.locator("#nav").inner_text()
    tab.click()
    settle(page)
    assert page.evaluate("location.hash") == "#/stream"
    switch = page.locator("#subnav [data-go]")
    assert switch.all_inner_texts() == ["Artists", "Liked songs", "Albums", "New releases"]
    at = page.locator("#subnav").bounding_box()
    for v, sec, title in (("liked", "v-list", "Liked, not owned"), ("gaps", "v-list", "Albums you're missing"),
                          ("releases", "v-list", "New from artists you love"), ("stream", "v-grid", "You stream these, but don't own them")):
        page.click(f"#subnav [data-go={v}]")
        settle(page)
        assert page.evaluate("location.hash") == "#/" + v
        assert view(page).get_attribute("id") == sec and title in view(page).inner_text(), v
        assert page.locator("#nav button.on").inner_text() == "Missing", v
        assert page.locator(f"#subnav [data-go={v}].on").count() == 1
        assert page.locator("#subnav").bounding_box() == at, f"the switch moved on {v}"
    for v in ("stream", "liked", "gaps", "releases"):   # old addresses still work, and keep the tab lit
        go(page, v)
        assert page.locator("#nav button.on").inner_text() == "Missing" and page.locator(f"#subnav [data-go={v}].on").count() == 1
    go(page, "missing")
    assert page.evaluate("location.hash") == "#/stream" and view(page).get_attribute("id") == "v-grid"
    go(page, "picks")
    assert page.locator("#subnav").is_hidden()


def test_missing_switch_scrolls_sideways_on_a_phone(browser, site):
    ctx = browser.new_context(viewport={"width": 320, "height": 700}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site["base"]) else r.abort())
    p = ctx.new_page()
    try:
        p.goto(site["base"] + "#/stream")
        settle(p)
        sn = p.locator("#subnav")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page itself never scrolls sideways"
        assert p.evaluate("(() => { const s = document.getElementById('subnav'); return s.scrollWidth > s.clientWidth; })()")
        assert "fade-r" in sn.get_attribute("class")
        p.goto(site["base"] + "#/releases")   # the chosen view scrolls into sight
        settle(p)
        box, on = sn.bounding_box(), p.locator("#subnav .on").bounding_box()
        assert on["x"] + on["width"] <= box["x"] + box["width"] + 1
        p.mouse.wheel(0, 600)
        p.wait_for_timeout(300)
        if p.evaluate("document.documentElement.scrollHeight > innerHeight + 100"):
            assert sn.is_hidden(), "the switch tucks away with the other rows"
    finally:
        ctx.close()


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
            page.locator("#lkEnd").scroll_into_view_if_needed()
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
        assert box.locator("[data-alb-spotify]").count() == 0 and box.locator("[data-alb-open-spotify]").inner_text().strip() == "Open in Spotify"
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
        assert page.locator(".lk-row .lk-a").count() == 4
        assert page.locator(".lk-row [data-lk-hide]").first.inner_text() == "Hide album"
        page.click("[data-lk-group=artist]")
        assert page.locator(".lk-row .lk-a").count() == 0, "rows under an artist's heading repeat the artist"
        assert page.locator(".lk-row [data-lk-hide]").first.inner_text() == "Hide song"
        heads = page.locator(".lk-ahead b").all_inner_texts()
        assert heads == ["Whitney Whitney", "Bellvie", "The California Honeydrops"]
        assert "2 songs · 2 releases" in page.locator(".lk-ahead").first.inner_text()
        page.fill("#lkQ", "isab")
        page.wait_for_function("document.querySelectorAll('.lk-row').length === 1")
        assert page.locator("#lkCount").inner_text() == "1 of 4 songs"
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


def test_play_buttons_use_spotify_when_connected_and_fall_back_to_previews(page, site, monkeypatch):
    from app import config, db
    lib = next(r for r in db.library_map().values() if r.get("albums"))
    before = db.conn().execute("SELECT json, at FROM cache WHERE k=?", ("dz:" + lib["key"],)).fetchone()
    db.cache_put("dz:" + lib["key"], {"id": 1, "name": lib["name"], "albums": [{"id": 33, "title": "Spotify Album", "type": "album", "date": "2024-01-01"}]})
    db.cache_put("dzalb:33", [{"n": 1, "disc": 1, "title": "On Spotify", "duration": 100, "preview": "https://cdn.test/a.mp3", "artist": lib["name"], "isrc": "X1"},
                              {"n": 2, "disc": 1, "title": "Not On Spotify", "duration": 100, "preview": "https://cdn.test/b.mp3", "artist": lib["name"]}])
    db.set_meta("spotify_can_play", True)
    db.commit()
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "r")
    played = []

    class FakeSpotify:
        def can_play(self):
            return True

        def find(self, kind, title, artist, isrc=None):
            return "a" * 22 if title in ("On Spotify", "Spotify Album") else None

        def play(self, kind, sid, on="computer"):
            played.append((kind, sid))
            assert on == "computer"
            return "Desk PC"
    monkeypatch.setattr(site["m"], "_spotify_client", lambda: FakeSpotify())
    try:
        db.conn().execute("DELETE FROM cache WHERE k LIKE 'spfind:%'"); db.commit()
        go(page, "home")
        page.evaluate("([k, n]) => openArtist(k, n)", [lib["key"], lib["name"]])
        page.click("#drawer button[data-alb='0']")
        page.wait_for_selector("#albTracks .atrk")
        page.evaluate("window.said = []; const t = toast; toast = (m, a) => { said.push(m); t(m, a); }")   # every message, in order
        page.locator("#albTracks .atrk").first.click()
        page.wait_for_function("said.includes('Playing On Spotify on Desk PC')")
        page.locator("#albTracks .atrk").nth(1).click()   # not on Spotify: the preview plays instead
        page.wait_for_function("said.some(m => String(m).startsWith('Not on Spotify'))")
        assert page.evaluate("document.getElementById('audio').src").endswith("/b.mp3")
        assert "Open in Spotify" not in page.locator("#albBox").inner_text()   # one Spotify button: the one that plays
        page.click("[data-alb-spotify]")
        page.wait_for_function("said.some(m => String(m).startsWith('Playing Spotify Album'))")
        assert played == [("track", "a" * 22), ("album", "a" * 22)]
    finally:
        page.evaluate("document.getElementById('audio').pause()")
        db.conn().execute("DELETE FROM cache WHERE k IN (?, 'dzalb:33') OR k LIKE 'spfind:%'", ("dz:" + lib["key"],))
        if before:   # leave the cache as it was, for the tests that run after this one
            db.conn().execute("INSERT INTO cache(k, json, at) VALUES (?, ?, ?)", ("dz:" + lib["key"], before[0], before[1]))
        db.set_meta("spotify_can_play", None)
        db.commit()


def test_history_has_a_note_and_a_search_box(page, site):
    db = site["db"]
    go(page, "picks")
    card = page.locator("#grid .card[data-key]").first
    key = card.get_attribute("data-key")
    name = page.evaluate("k => REG.get(k).name", key)
    card.hover()
    card.locator(".quick[data-hide]").click()
    settle(page)
    assert page.locator("#toast").inner_text().startswith(f"Not interested in {name}")
    try:
        go(page, "history")
        assert page.locator('#pills [data-ht="dismissed"]').inner_text().startswith("Not interested")
        assert page.locator('#pills [data-ht="known"]').inner_text().startswith("Already know them")
        assert "nudged down" in page.locator("#hiddenNote").inner_text()
        assert page.locator(f'#hiddenGrid .hcard[data-key="{key}"]').count() == 1
        page.fill("#histQ", "zzz no such artist")
        assert page.locator("#hiddenGrid").inner_text().strip() == "Nothing here yet."
        page.fill("#histQ", name[:3])
        assert page.locator(f'#hiddenGrid .hcard[data-key="{key}"]').count() == 1
        page.locator('#pills [data-ht="added"]').click()
        assert "Lidarr" in page.locator("#hiddenNote").inner_text()
    finally:
        db.clear_feedback(key)


def test_header_search_clears_when_you_leave_search(page):
    go(page, "search/gaze")
    assert page.locator("#gq").input_value() == "gaze"
    go(page, "home")
    assert page.locator("#gq").input_value() == ""


def test_why_this_pick_shows_bars_without_numbers(page):
    go(page, "picks")
    page.locator("#grid .card[data-key]").first.click()
    page.wait_for_selector("#drawer .seed .meter")
    meter = page.locator("#drawer .seed .meter").first
    assert meter.get_attribute("aria-label") == "How much this pushed the pick"
    assert page.locator("#drawer .seed .pct").count() == 0 and "%" not in page.locator("#drawer .seeds").inner_text()
    assert page.locator("#drawer [data-rate-open] svg").count() == 1   # the Rate button has the star


def test_rate_page_words_and_keys(page, site):
    go(page, "rate")
    page.locator('#rateSeg [data-mode="picks"]').click()
    settle(page)
    acts = page.locator(".rate-actions").inner_text()
    assert "Already know them" in acts and "Not interested" in acts and "Not for me" not in acts
    assert page.locator('[data-ra="known"] kbd').inner_text() == "K"
    assert page.locator(".rate-help .keys").is_visible() and not page.locator(".rate-help .touch").is_visible()


def test_rate_page_on_a_touch_screen_has_no_keyboard_help(browser, site):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site["base"]) else r.abort())
    p = ctx.new_page()
    try:
        p.goto(site["base"] + "#/rate")
        p.wait_for_load_state("networkidle")
        if not p.evaluate("matchMedia('(hover: none)').matches"):
            pytest.skip("this browser doesn't emulate a touch screen")
        assert not p.locator(".rate-help .keys").is_visible() and p.locator(".rate-help .touch").is_visible()
        assert "Tap the left half" in p.locator(".rate-help").inner_text()
        assert all(not k.is_visible() for k in p.locator(".rate kbd").all())
    finally:
        ctx.close()


def test_settings_wording(page, site):
    db = site["db"]
    db.set_feedback("someone", "dismissed", "Someone")
    try:
        go(page, "settings")
        assert page.locator('#svc-plex [data-test="plex"]').text_content() == "Test connection"
        page.wait_for_function("document.getElementById('tres-plex').textContent.includes('Connected')")
        assert page.locator("#tres-plex").text_content() == "✓ Connected to Test Plex v1.40.0 · 3 music libraries"
        labels = page.locator("#set-taste .field > label").all_inner_texts()
        for want in ("Albums to leave out", "Genres to leave out", "Artists left out"):
            assert any(want in x for x in labels), (want, labels)
        reset = page.locator('[data-maint="clear-feedback"]')
        n = db.stats()["feedback"]
        assert reset.inner_text() == "Reset all feedback" and page.locator("#fbCount").inner_text() == f"{n} artist{'' if n == 1 else 's'}"
        assert "artists with feedback" in page.locator("#set-sync .stats").inner_text()
        asked = []
        page.once("dialog", lambda d: (asked.append(d.message), d.dismiss()))
        reset.click()   # says what goes first; saying no keeps everything
        assert asked and asked[0].startswith("Reset all feedback?") and "• Artists you marked Not interested" in asked[0]
        settle(page)
        assert db.stats()["feedback"] == n
        assert [s.get_attribute("id") for s in page.locator(".setsec").all()] == \
            ["set-connections", "set-history", "set-taste", "set-picks", "set-plex", "set-sync", "set-signin"]
    finally:
        db.clear_feedback("someone")


def test_you_stream_says_when_no_listening_history_is_connected(page):
    go(page, "stream")
    html = page.evaluate("lastStatus.history = {plex_comp: 4}; emptyState()")
    assert "no listening history is connected" in html and "data-open-setting" in html
    page.evaluate("document.getElementById('grid').innerHTML = emptyState()")
    page.click("[data-open-setting]")
    settle(page)
    assert view(page).get_attribute("id") == "v-settings"
    page.wait_for_function("Math.abs(document.getElementById('set-history').getBoundingClientRect().top - headerOffset()) < 30")


def test_albums_youre_missing_tick_boxes_sit_by_their_labels(page):
    go(page, "gaps")
    for cb in page.locator("[data-gap-opt]").all():
        assert cb.bounding_box()["width"] < 30


def test_releases_show_the_date_once(page):
    go(page, "releases")
    soon = page.locator(".card.al", has=page.locator(".match")).first
    assert soon.count() == 1 and soon.locator(".why").count() == 0, "an announced album shows its date twice"
    past = page.locator(".card.al").filter(has_not=page.locator(".match")).first
    if past.count():
        assert past.locator(".why").count() == 1 and " · 20" not in past.locator(".s").inner_text()


def test_setup_while_you_wait_marks_whats_done(page):
    go(page, "setup")
    page.evaluate("""() => { clearInterval(wzTimer); wz.step = wzSteps().length - 1;
        lastStatus = { ...lastStatus, running: true, last_sync: null, error: null, history: { listenbrainz: 3 } }; renderSetup(); clearInterval(wzTimer); }""")
    items = page.locator(".wz-list .wz-item")
    assert items.first.inner_text().startswith("Rate the artists you play") and items.first.locator('[data-wz-go="rate"]').count() == 1
    lb = items.filter(has_text="Connect ListenBrainz")
    assert "Done" in lb.inner_text() and lb.locator("button").inner_text() == "Change"
    sp = items.filter(has_text="Request your Spotify history")
    assert "Done" not in sp.inner_text()


def test_phone_header_tucks_away_while_scrolling_down(browser, site):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site["base"]) else r.abort())
    p = ctx.new_page()
    try:
        p.goto(site["base"] + "#/picks")
        p.wait_for_load_state("networkidle")
        p.evaluate("document.getElementById('sentinel').style.height = '4000px'")
        f = p.locator("#filters")
        assert f.evaluate("f => f.scrollWidth > f.clientWidth"), "the test needs a filter bar wider than the phone"
        assert f.evaluate("f => f.classList.contains('fade-r')"), "no fade while more filters are off to the right"
        heights = f.evaluate("f => [...f.querySelectorAll('button')].map(b => b.offsetHeight)")
        assert max(heights) < 40, heights   # no button wraps onto two lines
        f.evaluate("f => { f.scrollLeft = f.scrollWidth; f.dispatchEvent(new Event('scroll')); }")
        assert not f.evaluate("f => f.classList.contains('fade-r')"), "the fade stays at the end of the bar"
        assert p.locator("#gridHead").bounding_box()["y"] >= p.locator("header").bounding_box()["height"] - 1, "the page starts under the header"
        for y in (300, 600, 900):
            p.evaluate("y => scrollTo(0, y)", y)
            p.wait_for_timeout(80)
        assert p.locator("#nav").is_hidden() and p.locator("#filters").is_hidden() and p.locator("#gsearch").is_visible()
        where = "document.getElementById('grid').getBoundingClientRect().top + scrollY"
        before = p.evaluate(where)
        p.evaluate("scrollTo(0, 700)")
        p.wait_for_timeout(80)
        assert p.locator("#nav").is_visible() and p.locator("#filters").is_visible(), "scrolling up brings them back"
        assert p.evaluate(where) == before, "the page moved when the header changed"
    finally:
        ctx.close()


def test_albums_you_have_every_track_of_show_owned_without_opening_them(page, site):
    from app import db
    from app.clients import artist_key, song_key
    lib = next(r for r in db.library_map().values() if r.get("albums"))
    before = db.conn().execute("SELECT json, at FROM cache WHERE k=?", ("dz:" + lib["key"],)).fetchone()
    db.cache_put("dz:" + lib["key"], {"id": 1, "name": lib["name"], "albums": [
        {"id": 44, "title": "Filed Elsewhere EP", "type": "ep", "date": "2026-01-01"},
        {"id": 45, "title": "Not Mine", "type": "album", "date": "2020-01-01"}]})
    db.cache_put("dzalb:44", [{"title": "Arrival", "artist": lib["name"]}, {"title": "Vacation", "artist": lib["name"]}])
    db.cache_put("dzalb:45", [{"title": "Arrival", "artist": lib["name"]}, {"title": "Something Else", "artist": lib["name"]}])
    keep = db.get_meta("plex_songs")
    db.set_meta("plex_songs", {f"{lib['key']}|{song_key(t)}": "1" for t in ("Arrival", "Vacation")})
    db.commit()
    try:
        go(page, "home")
        page.evaluate("([k, n]) => openArtist(k, n)", [lib["key"], lib["name"]])
        page.wait_for_selector("#drawer button[data-alb='0'] .own-b")   # every track is in Plex: owned, no click needed
        page.wait_for_timeout(500)
        assert not page.locator("#drawer button[data-alb='1'] .own-b").count()   # one track short: not owned
    finally:
        db.conn().execute("DELETE FROM cache WHERE k IN (?, 'dzalb:44', 'dzalb:45')", ("dz:" + lib["key"],))
        if before:
            db.conn().execute("INSERT INTO cache(k, json, at) VALUES (?, ?, ?)", ("dz:" + lib["key"], before[0], before[1]))
        db.set_meta("plex_songs", keep)
        db.commit()


def test_albums_by_an_artist_filed_under_someone_else_in_plex_still_show_owned(page, site):
    from app import db
    from app.clients import song_key
    pick = next(r for r in db.recs("new") if r["key"] not in db.library_map())   # not an artist in your library
    k, name = pick["key"], pick["name"]
    before = db.conn().execute("SELECT json, at FROM cache WHERE k=?", ("dz:" + k,)).fetchone()
    db.cache_put("dz:" + k, {"id": 1, "name": name, "albums": [{"id": 46, "title": "Grand Hotel", "type": "ep", "date": "2026-01-01"}]})
    db.cache_put("dzalb:46", [{"title": "Champagne Arrival", "artist": "Sun City"}, {"title": "Next to You", "artist": "Sun City"}])
    keep = db.get_meta("plex_songs")
    # Plex only knows these songs as Sun City's (this artist is featured); Deezer credits them to Sun City too
    db.set_meta("plex_songs", {f"suncity|{song_key(t)}": "1" for t in ("Champagne Arrival", "Next to You")})
    db.commit()
    try:
        go(page, "home")
        page.evaluate("([k, n]) => openArtist(k, n)", [k, name])
        page.wait_for_selector("#drawer button[data-alb='0'] .own-b")
        page.keyboard.press("Escape")
        page.evaluate("([k, n]) => openArtist(k, n)", [k, name])   # come back: still owned, straight from the server
        page.wait_for_selector("#drawer button[data-alb='0']")
        assert page.locator("#drawer button[data-alb='0'] .own-b").count() == 1
    finally:
        db.conn().execute("DELETE FROM cache WHERE k IN (?, 'dzalb:46')", ("dz:" + k,))
        if before:
            db.conn().execute("INSERT INTO cache(k, json, at) VALUES (?, ?, ?)", ("dz:" + k, before[0], before[1]))
        db.set_meta("plex_songs", keep)
        db.commit()
