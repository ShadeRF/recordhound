"""Artist recommendations.

1. Seeds: every artist you own or listen to gets a weight from what you've told us
   (star ratings), how much you play them (Plexamp, Last.fm, ListenBrainz, Spotify), and,
   for a curated library, simply owning them.
2. Collaborative: "similar artists" for each seed (Last.fm's with a Pro key; otherwise
   ListenBrainz's and Deezer's), weighted by the seed and by the match score. Many of your
   artists pointing at one name is the strongest signal.
3. Content: a TF-IDF profile of tags (genres, scenes, moods, eras; Last.fm's or MusicBrainz's)
   built from your seeds; candidates are scored by how close their tags sit to it.
4. Score = 0.55 collab + 0.35 content + 0.10 popularity (a small nudge toward artists
   enough people have heard of to have good data).
"""
import math
import random
import re
import time
from collections import defaultdict

SCORE_WEIGHTS = {"collab": 0.55, "content": 0.35, "pop": 0.10}
JUNK_TAGS = {"seen live", "favorites", "favourite", "favorite", "albums i own", "under 2000 listeners", "spotify",
             "my favorite", "love", "awesome", "beautiful", "cool", "good", "best", "amazing", "check out", "seen in concert",
             # where they're from, the language they sing in, or their record label: not a sound
             "uk", "usa", "us", "united states", "united kingdom", "england", "english", "british", "american", "canadian", "canada",
             "australian", "australia", "irish", "scottish", "welsh", "german", "swedish", "norwegian", "icelandic", "french",
             "japanese", "london", "4ad", "sub pop", "warp", "matador", "domino", "xl recordings", "rough trade", "merge records"}


NOSEED_TAG_DEPTH = 3   # "Genres to leave out" looks at each artist's top three tags


def tag_key(tag):
    """One key for spellings of the same tag: "singer/songwriter", "singer-songwriter" and "Singer Songwriter"."""
    return re.sub(r"[^a-z0-9]+", "", (tag or "").lower()) or (tag or "").lower()
SOURCES = ("plex", "lastfm", "listenbrainz", "spotify", "spotify_export", "youtube_music", "plex_comp")


# Spotify's account connection gives its own top-artists ranks, not play counts, so it isn't held to a minimum
MIN_PLAYS_EXEMPT = {"spotify"}


def listen_share(history_row, library_row, maxes, min_plays=0):
    """0..1: how much you play this artist, by whichever source shows it most.
    Listening outside Plex below min_plays (a song or two to check someone out) doesn't count."""
    best = 0.0
    if library_row and library_row.get("plays") and maxes.get("plex"):
        best = math.log1p(library_row["plays"]) / math.log1p(maxes["plex"])
    for src, v in ((history_row or {}).get("sources") or {}).items():
        if src not in MIN_PLAYS_EXEMPT and (v.get("plays") or 0) < min_plays:
            continue
        if v.get("plays") and maxes.get(src):
            best = max(best, math.log1p(v["plays"]) / math.log1p(maxes[src]))
    return min(1.0, best)


RECENT_DAYS = 180


def recent_share(library_row, recent_plays, recent_max, now=None, min_plays=0):
    """0..1: how much you've played this artist lately. Plex's last-played date fades over six months;
    ListenBrainz's last-six-months play count is scaled against your most-played artist in that time."""
    best = 0.0
    lp = (library_row or {}).get("last_played")
    if lp and (library_row or {}).get("plays"):
        age = ((now or time.time()) - lp) / 86400
        best = max(0.0, 1 - age / RECENT_DAYS)
    if recent_plays and recent_max and recent_plays >= min_plays:
        best = max(best, math.log1p(recent_plays) / math.log1p(recent_max))
    return min(1.0, best)


def build_seeds(library, history, feedback, owned_weight=0.6, noseed=(), recent=None, recent_weight=0.0, now=None, min_plays=0):
    """Returns {key: {"w": weight, "name", "kind", "plays", "rating", "listen", "recent"}}.
    recent: {key: plays in the last six months} (ListenBrainz); recent_weight adds pull for what you play lately."""
    recent = recent or {}
    recent_max = max(recent.values(), default=0)
    maxes = defaultdict(int)
    for r in library:
        maxes["plex"] = max(maxes["plex"], r.get("plays") or 0)
    for h in history.values():
        for src, v in h["sources"].items():
            maxes[src] = max(maxes[src], v.get("plays") or 0)
    lib = {r["key"]: r for r in library}
    # artists you know but don't own, rated on the Rate page or in an artist's panel
    rated = {k: f for k, f in feedback.items() if f.get("rating") and f["action"] == "known" and k not in lib}
    seeds = {}
    for k in set(lib) | set(history) | set(rated):
        if k in noseed:
            continue
        lr, hr = lib.get(k), history.get(k)
        listen = listen_share(hr, lr, maxes, min_plays)
        rating = (lr or {}).get("rating") or (rated.get(k) or {}).get("rating")
        saved = any(v.get("saved") for v in ((hr or {}).get("sources") or {}).values())
        if rating:
            w, kind = (rating - 5) / 2.5, "rated"
            if w > 0:
                w += 0.5 * listen
        elif listen > 0.02:
            w, kind = 0.4 + 1.6 * listen, "played"
        elif lr:
            albums = len(lr.get("albums") or [])
            w, kind = owned_weight * min(1.0, 0.5 + 0.1 * albums), "owned"
        elif saved:
            w, kind = 0.6, "saved"
        else:
            continue
        if saved and w > 0:
            w += 0.3
        rec = recent_share(lr, recent.get(k), recent_max, now, min_plays)
        if w > 0 and rec:
            w += recent_weight * rec
        seeds[k] = {"w": round(w, 4), "name": (lr or hr or rated[k])["name"], "kind": kind, "owned": bool(lr),
                    "plays": (lr or {}).get("plays", 0) + (hr or {}).get("plays", 0), "rating": rating, "listen": round(listen, 3),
                    "recent": round(rec, 3)}
    for k, f in feedback.items():
        if f["action"] == "liked" and k not in seeds:
            seeds[k] = {"w": 1.2, "name": f["name"], "kind": "liked", "owned": False, "plays": 0, "rating": None, "listen": 0}
        elif f["action"] == "dismissed" and k in seeds and not seeds[k]["owned"]:
            seeds[k]["w"] = min(seeds[k]["w"], -0.6)
    return seeds


def tag_vec(info, idf):
    tags = [(t, c) for t, c in (info or {}).get("tags", []) if t not in JUNK_TAGS]
    if not tags:
        return {}
    top = max(c for _, c in tags) or 1
    v = {t: (c / top) * idf.get(t, 1.0) for t, c in tags}
    n = math.sqrt(sum(x * x for x in v.values())) or 1
    return {t: x / n for t, x in v.items()}


def _cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in a.items())


def build_idf(infos):
    df = defaultdict(int)
    for i in infos:
        for t, _ in (i or {}).get("tags", []):
            df[t] += 1
    n = max(1, len(infos))
    return {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}


def build_profile(seeds, infos, idf):
    pos, neg = defaultdict(float), defaultdict(float)
    for k, s in seeds.items():
        v = tag_vec(infos.get(k), idf)
        tgt = pos if s["w"] > 0 else neg
        for t, x in v.items():
            tgt[t] += abs(s["w"]) * x
    for d in (pos, neg):
        n = math.sqrt(sum(x * x for x in d.values())) or 1
        for t in d:
            d[t] /= n
    return dict(pos), dict(neg)


def popularity(info):
    """Last.fm listeners when we have them, otherwise Deezer fans. None when unknown."""
    info = info or {}
    return info.get("listeners") if info.get("listeners") is not None else info.get("fans")


def too_obscure(info, min_listeners=0, min_fans=0):
    info = info or {}
    if info.get("listeners") is not None:
        return info["listeners"] < min_listeners
    if info.get("fans") is not None:
        return info["fans"] < min_fans
    return False   # unknown: don't hide them for it


def candidates(seeds, infos, exclude, max_seeds=150):
    """Walk the similar-artists lists out from your strongest seeds."""
    top = sorted((k for k, s in seeds.items() if s["w"] > 0 and infos.get(k) and not infos[k].get("missing")),
                 key=lambda k: -seeds[k]["w"])[:max_seeds]
    cands = {}
    from .clients import artist_key
    for sk in top:
        w = seeds[sk]["w"]
        for rank, (name, match) in enumerate(infos[sk].get("similar", [])):
            ck = artist_key(name)
            if ck in exclude or ck == sk:
                continue
            c = cands.setdefault(ck, {"name": name, "collab": 0.0, "because": defaultdict(float)})
            contrib = w * match * max(0.2, 1 - rank / 120)
            c["collab"] += contrib
            c["because"][sk] += contrib
    for k, s in seeds.items():   # artists you dislike drag down anything mainly similar to them
        if s["w"] < 0 and infos.get(k):
            for name, match in infos[k].get("similar", [])[:50]:
                ck = artist_key(name)
                if ck in cands:
                    cands[ck]["collab"] += s["w"] * match * 0.5
    return {k: v for k, v in cands.items() if v["collab"] > 0}


def score(cands, seeds, infos, min_listeners=0, label_tags=None, min_fans=0):
    idf = build_idf([infos.get(k) for k in list(seeds) + list(cands)])
    pos, neg = build_profile(seeds, infos, idf)
    if not cands:
        return [], pos, idf
    max_collab = math.log1p(max(c["collab"] for c in cands.values()))
    content_raw = {}
    for k in cands:
        v = tag_vec(infos.get(k), idf)
        content_raw[k] = max(0.0, _cos(pos, v) - 0.5 * _cos(neg, v)) if v else 0.0
    max_content = max(content_raw.values()) or 1
    max_pop = math.log1p(max(popularity(infos.get(k)) or 0 for k in cands) or 1)
    out = []
    for k, c in cands.items():
        info = infos.get(k) or {}
        if info.get("missing"):
            continue
        if too_obscure(info, min_listeners, min_fans):
            continue
        collab_n = math.log1p(c["collab"]) / max_collab if max_collab else 0
        content_n = content_raw[k] / max_content
        pop_n = math.log1p(popularity(info) or 0) / max_pop if max_pop else 0
        total = SCORE_WEIGHTS["collab"] * collab_n + SCORE_WEIGHTS["content"] * content_n + SCORE_WEIGHTS["pop"] * pop_n
        v = tag_vec(info, idf)
        shared = sorted(((pos.get(t, 0) * x, t) for t, x in v.items()), reverse=True)
        bsum = sum(x for x in c["because"].values() if x > 0) or 1
        because = [{"key": sk, "name": seeds[sk]["name"], "kind": seeds[sk]["kind"], "plays": seeds[sk]["plays"],
                    "rating": seeds[sk]["rating"], "owned": seeds[sk]["owned"], "share": round(x / bsum, 3)}
                   for sk, x in sorted(c["because"].items(), key=lambda kv: -kv[1])[:5] if x > 0]
        out.append({
            "key": k, "name": info.get("name") or c["name"], "mbid": info.get("mbid"),
            "score": round(total, 4), "components": {"collab": round(collab_n, 3), "content": round(content_n, 3), "pop": round(pop_n, 3)},
            "because": because, "seed_count": sum(1 for x in c["because"].values() if x > 0),
            "matches": [t for val, t in shared if val > 0][:3],
            "tags": [t for t, _ in info.get("tags", []) if t not in JUNK_TAGS][:8],
            "listeners": info.get("listeners"), "fans": info.get("fans"), "bio": (info.get("bio") or "")[:600], "url": info.get("url"),
        })
    out.sort(key=lambda r: -r["score"])
    return out, pos, idf


def unplayed_albums(albums, seeds, infos, pos, idf, noseed=(), limit=600):
    """Albums you own but have never played, best first.
    Pull comes from the artist (you play or rate them), from your other artists pointing at them
    (similar-artist lists), and from how well their tags fit your taste."""
    from .clients import artist_key
    point, because = defaultdict(float), defaultdict(lambda: defaultdict(float))
    for sk, s in seeds.items():
        if s["w"] <= 0 or not infos.get(sk):
            continue
        for name, match in (infos[sk].get("similar") or [])[:60]:
            ck = artist_key(name)
            point[ck] += s["w"] * match
            because[ck][sk] += s["w"] * match
    max_point = math.log1p(max(point.values(), default=0)) or 1
    out = []
    for a in albums:
        k = a.get("artist_key")
        if a.get("compilation"):   # a soundtrack or compilation: judged by the best-liked artist on it
            on = [x for x in a.get("artists") or [] if x not in noseed]
            if a.get("artists") and not on:   # everyone on it is someone you've asked not to learn from
                continue
            k = max(on, key=lambda x: (seeds.get(x) or {}).get("w", 0), default="")
        if a.get("plays") or not a.get("rk") or (k and k in noseed):
            continue
        s = seeds.get(k) or {}
        if s.get("w", 0) < 0:
            continue
        rating = s.get("rating") or 0
        artist_part = max(s.get("listen", 0), (rating - 5) / 5 if rating > 5 else 0)
        point_n = math.log1p(point.get(k, 0)) / max_point
        v = tag_vec(infos.get(k), idf)
        content = max(0.0, _cos(pos, v)) if v else 0.0
        score = 0.45 * artist_part + 0.35 * point_n + 0.20 * content
        who = s.get("name") if a.get("compilation") else a["artist"]
        if a.get("compilation") and who and (s.get("listen", 0) >= 0.2 or rating >= 8):
            why = f"Features {who}, who you play a lot" if s.get("listen", 0) >= 0.2 else f"Features {who}, who you rated highly"
        elif s.get("listen", 0) >= 0.2:
            why = f"You play {a['artist']} a lot"
        elif rating >= 8:
            why = f"You rated {a['artist']} highly"
        elif because.get(k):
            names = [seeds[x]["name"] for x, _ in sorted(because[k].items(), key=lambda kv: -kv[1])[:2]]
            why = "Because you play " + " · ".join(names)
        elif content > 0.2:
            why = "Fits your taste"
        else:
            why = "In your library"
        out.append({**{x: a.get(x) for x in ("rk", "title", "artist", "artist_key", "year", "added_at", "compilation")},
                    "key": f"al:{a['rk']}", "score": round(score, 4), "why": why})
    out.sort(key=lambda r: (-r["score"], -(r.get("added_at") or 0)))
    return out[:limit]


def varied(rows, n=24, per_seed=2, pool=3, salt=None):
    """Top picks with some variety: at most per_seed picks led by the same artist of yours, and a gentle
    reshuffle among the best n*pool that changes once a week (salt)."""
    rng = random.Random(salt if salt is not None else time.strftime("%G-%V"))
    head = rows[:n * pool]
    jittered = sorted(head, key=lambda r: -(r.get("score") or 0) * rng.uniform(0.85, 1.15))
    out, per = [], defaultdict(int)
    for r in jittered:
        lead = ((r.get("because") or [{}])[0]).get("key")
        if lead and per[lead] >= per_seed:
            continue
        per[lead] += 1
        out.append(r)
        if len(out) >= n:
            break
    if len(out) < n:   # not enough variety to fill the row: top up in score order
        have = {r["key"] for r in out}
        out += [r for r in rows if r["key"] not in have][:n - len(out)]
    return sorted(out, key=lambda r: -(r.get("score") or 0))   # the mix changes weekly; the row still reads best first


def taste_tags(pos, n=12):
    return [{"tag": t, "w": round(w, 3)} for t, w in sorted(pos.items(), key=lambda kv: -kv[1])[:n]]
