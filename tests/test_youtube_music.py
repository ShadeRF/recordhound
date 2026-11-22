"""YouTube Music history from Google Takeout. The samples follow the layout Takeout uses for watch history,
in both of its formats (JSON and the default HTML)."""
import io
import json
import os
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import db  # noqa: E402
from app.clients import APIError, parse_youtube_music  # noqa: E402


def entry(title, channel=None, music=True, ad=False):
    e = {"header": "YouTube Music" if music else "YouTube", "title": f"Watched {title}",
         "titleUrl": f"https://{'music' if music else 'www'}.youtube.com/watch?v=x{abs(hash(title)) % 10**6}",
         "time": "2026-11-02T21:14:03.120Z", "products": ["YouTube"], "activityControls": ["YouTube watch history"]}
    if channel:
        e["subtitles"] = [{"name": channel, "url": "https://www.youtube.com/channel/UCx"}]
    if ad:
        e["details"] = [{"name": "From Google Ads"}]
    return e


JSON_PLAYS = ([entry("Idioteque", "Radiohead - Topic")] * 3 + [entry("Karma Police", "RadioheadVEVO")] +
              [entry("Hyperballad", "Björk - Topic")] * 2 + [entry("The Boxer", "Simon & Garfunkel - Topic")] +
              [entry("How to fix a bike", "Bike Channel", music=False)] * 5 +   # regular YouTube: not music
              [entry("Buy now", "Some Brand", ad=True)] +                        # an ad
              [entry("https://www.youtube.com/watch?v=gone")])                  # a removed video: no channel


def cell(product, title, url, channel=None):
    chan = f'<a href="https://www.youtube.com/channel/UCx">{channel}</a><br>' if channel else ""
    return ('<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
            f'<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">{product}<br></p></div>'
            f'<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Watched&nbsp;<a href="{url}">{title}</a><br>{chan}'
            'Nov 2, 2026, 9:14:03 PM EST<br></div>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
            '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"><b>Products:</b><br>&emsp;YouTube<br>'
            '<b>Why is this here?</b><br>&emsp;This activity was saved to your Google Account because the following settings were on:'
            '&nbsp;YouTube watch history.&nbsp;You can control these settings &nbsp;<a href="https://myaccount.google.com/activitycontrols">here</a>.'
            '</div></div></div>')


HTML_PLAYS = ('<html><head><title>History</title></head><body><div class="mdl-grid">' +
              cell("YouTube Music", "Idioteque", "https://music.youtube.com/watch?v=a", "Radiohead - Topic") * 2 +
              cell("YouTube Music", "The Boxer", "https://music.youtube.com/watch?v=b", "Simon &amp; Garfunkel - Topic") +
              cell("YouTube", "How to fix a bike", "https://www.youtube.com/watch?v=c", "Bike Channel") +
              cell("YouTube Music", "https://music.youtube.com/watch?v=gone", "https://music.youtube.com/watch?v=gone") +
              '</div></body></html>')


def takeout(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def plays(rows):
    return {r["name"]: r["plays"] for r in rows}


def test_json_counts_only_youtube_music_plays_by_artist():
    rows = parse_youtube_music(json.dumps(JSON_PLAYS).encode())
    assert plays(rows) == {"Radiohead": 4, "Björk": 2, "Simon & Garfunkel": 1}
    assert [r["rank"] for r in rows] == [1, 2, 3]


def test_html_export_works_too_and_ignores_the_footer_link():
    assert plays(parse_youtube_music(HTML_PLAYS.encode())) == {"Radiohead": 2, "Simon & Garfunkel": 1}


def test_whole_takeout_zip_reads_watch_history_and_skips_searches():
    searches = json.dumps([entry("Searched for radiohead", "Not An Artist")])
    data = takeout({"Takeout/YouTube and YouTube Music/history/watch-history.json": json.dumps(JSON_PLAYS),
                    "Takeout/YouTube and YouTube Music/history/search-history.json": searches,
                    "Takeout/YouTube and YouTube Music/subscriptions/subscriptions.csv": "Channel Id,Channel Url,Channel Title\n",
                    "Takeout/archive_browser.html": "<html>index</html>"})
    assert plays(parse_youtube_music(data))["Radiohead"] == 4


def test_translated_folder_names_and_html_in_a_zip():
    data = takeout({"Takeout/YouTube und YouTube Music/Verlauf/Wiedergabeverlauf.html": HTML_PLAYS})
    assert plays(parse_youtube_music(data))["Radiohead"] == 2


def test_files_without_music_plays_explain_what_to_upload():
    with pytest.raises(APIError, match="No YouTube Music plays"):
        parse_youtube_music(json.dumps([entry("How to fix a bike", "Bike Channel", music=False)]).encode())
    with pytest.raises(APIError, match="No YouTube Music plays"):
        parse_youtube_music(takeout({"Takeout/Spotify/whatever.txt": "x"}))


def test_upload_and_remove_through_settings(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    monkeypatch.setattr(m, "schedule_rescore", lambda *a, **k: None)
    c = TestClient(m.app)
    r = c.post("/api/import/youtube-music", content=json.dumps(JSON_PLAYS).encode())
    assert r.status_code == 200 and r.json() == {"ok": True, "artists": 3, "plays": 7}
    s = c.get("/api/settings").json()["stats"]
    assert s["history"]["youtube_music"] == 3 and s["youtube_music_at"]
    assert db.history()[next(k for k, h in db.history().items() if h["name"] == "Radiohead")]["sources"]["youtube_music"]["plays"] == 4
    bad = c.post("/api/import/youtube-music", content=b"not a takeout file")
    assert bad.status_code == 400 and "YouTube Music" in bad.json()["detail"]
    assert db.history_counts().get("youtube_music") == 3   # a failed upload keeps the last one
    assert c.delete("/api/import/youtube-music").json() == {"ok": True}
    assert not db.history_counts().get("youtube_music")
