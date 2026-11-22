"""Calls the real free services Recordhound depends on and checks their answers still look right.

The tests fake every service, so they can't notice when MusicBrainz, ListenBrainz, Deezer or Wikipedia
change. This can. Run it with `python scripts/live_check.py`; the weekly workflow runs it too and opens
an issue when something breaks. Exits non-zero on any failure.
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", "/tmp/recordhound-live-check")

from app.clients import Deezer, OpenArtists, artist_key, listenbrainz_top_artists  # noqa: E402

RADIOHEAD = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
results = []


def check(name):
    def wrap(fn):
        try:
            detail = fn()
            results.append((name, True, detail or ""))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))
        return fn
    return wrap


oa = OpenArtists(Deezer())


@check("MusicBrainz finds an artist by name")
def _():
    m = oa.mbid("Radiohead")
    assert m == RADIOHEAD, m


@check("ListenBrainz Labs similar artists")
def _():
    rows = oa.similar(RADIOHEAD)
    assert len(rows) >= 10, f"only {len(rows)} similar artists"
    assert all(r["mbid"] and r["name"] for r in rows)
    return f"{len(rows)} artists, e.g. {', '.join(r['name'] for r in rows[:5])}"


@check("ListenBrainz artist metadata (tags, Wikidata link)")
def _():
    m = oa.meta(RADIOHEAD)
    tags = (m.get("tag") or {}).get("artist") or []
    assert m.get("name") == "Radiohead", m.get("name")
    assert len(tags) >= 3, f"{len(tags)} tags"
    assert "wikidata" in (m.get("rels") or {}), "no wikidata link"


@check("ListenBrainz batch metadata")
def _():
    oa2 = OpenArtists(Deezer())
    oa2.prefetch([RADIOHEAD, "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11"])
    assert oa2._meta[RADIOHEAD].get("name") == "Radiohead"
    assert oa2._meta["8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11"].get("name") == "Portishead"


@check("Deezer search, related artists, albums, previews")
def _():
    dz = Deezer()
    a = dz.find_artist("Radiohead")
    assert not a.get("missing") and a.get("fans", 0) > 100_000, a
    rel = dz.related(a["id"])
    assert len(rel) >= 5, f"{len(rel)} related"
    assert len(dz.albums(a["id"])) >= 5
    assert dz.top_tracks(a["id"]), "no previews"
    return f"{len(rel)} related, e.g. {', '.join(n for n, _ in rel[:5])}"


@check("Full artist lookup without Last.fm")
def _():
    info = oa.artist("Radiohead")
    assert info["mbid"] == RADIOHEAD and info["tags"] and info["fans"]
    assert len(info["similar"]) >= 10 and info["similar_mbids"]
    assert artist_key("Radiohead") not in {artist_key(n) for n, _ in info["similar"]}


@check("Wikipedia bio through Wikidata")
def _():
    b = oa.bio("Q44190")
    assert b and "Radiohead" in b["bio"] and b["bio_url"].startswith("https://en.wikipedia.org/")


@check("MusicBrainz release groups")
def _():
    groups = oa.release_groups(RADIOHEAD)
    titles = {g["title"] for g in groups}
    assert "OK Computer" in titles, sorted(titles)[:10]


@check("ListenBrainz user stats")
def _():
    rows = listenbrainz_top_artists("rob", 10)   # the ListenBrainz founder's public profile
    assert isinstance(rows, list)
    return f"{len(rows)} artists"


if __name__ == "__main__":
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail.splitlines()[0]})" if detail else ""))
    if failed:
        print("\nDetails:")
        for name, _, detail in failed:
            print(f"\n## {name}\n{detail}")
    sys.exit(1 if failed else 0)
