# Developing Recordhound

Notes for working on the code. The README is also what shows on the package page at ghcr.io, so keep it about using the app.

## Running from source on Unraid

Put this folder at `/mnt/user/appdata/recordhound/src`, then in Compose Manager Plus add a stack with **External folder** → that path → **Compose Up**.
`docker-compose.yml` mounts `./app` into the container, so code changes only need Compose Down/Up, not a rebuild.

## Tests

```
pip install -r requirements.txt pytest httpx
python -m pytest
```

Every service is faked, so the tests need no network.

**Browser tests** (`tests/test_browser.py`) start the app against a test library and click through the real pages in Chrome: every page loads without script errors, hiding and undo show up everywhere, the Rate page, the setup walkthrough, the phone layout and more. They need Playwright and Google Chrome:

```
pip install playwright
python -m pytest tests/test_browser.py
```

Without Playwright or Chrome they skip themselves. The build sets `REQUIRE_BROWSER=1`, so there they always run and a failure stops the release.

## Releasing

1. Bump `__version__` in `app/__init__.py` and commit.
2. `git push`, then `git tag v0.9.0` and `git push origin v0.9.0`.
   Push only the new tag. `git push --tags` also pushes any older tag GitHub doesn't have yet, which starts a release build for that version, and whichever build finishes last becomes `latest`.
3. The workflow publishes `ghcr.io/shaderf/recordhound:0.9.0`, `:0.9` and `:latest`; Unraid shows "update available".

Pushing to `main` alone publishes nothing.
Only the newest version tag moves `latest`: building an older tag publishes that version's own tags and leaves `latest` alone. A tag that isn't `vX.Y.Z` publishes nothing.
Every build runs the tests first (then the browser tests in Chrome) and publishes nothing if they fail. Pull requests (including Dependabot's) run them too.

**Nightly builds:** every night at about 3:41 AM Eastern, if `main` has commits the last nightly didn't, the workflow publishes `ghcr.io/shaderf/recordhound:nightly`. Nights with nothing new are skipped in a few seconds.
Nightly images show their build in the version, e.g. `1.2.3+nightly.20261121.ab12cd3`. Users can pick Latest or Nightly when installing from Community Apps.

**Edge builds (for testing):** run the workflow by hand from the Actions tab or with `gh workflow run docker.yml`. It publishes `ghcr.io/shaderf/recordhound:edge` from `main` right away (version like `1.2.3+edge.20261121.ab12cd3`). Use `:edge` on your own server for quick test cycles, so `:nightly` stays a true once-a-night build. Neither touches `:latest`.

## Community Apps

The template, icons and user guide live in the public [ShadeRF/unraid-templates](https://github.com/ShadeRF/unraid-templates) repo (`templates/recordhound.xml`, `docs/recordhound.md`).
When a release changes setup or requirements, update those after the release is published, not before.

## Dependabot

Weekly PRs for workflow actions, pip packages and the Docker base image. `actions/checkout` is held at v5.0.0: newer versions couldn't fetch the repository with the workflow token while it was private. That no longer applies, so the hold can be lifted.
Merging a Dependabot PR doesn't publish anything; it ships with the next tag or nightly.

## Artist data sources

`clients.artist_source()` picks where artist data comes from: Last.fm while a working key is set (checked at most once an hour), otherwise `OpenArtists` (MusicBrainz, ListenBrainz, Deezer, Wikipedia).
Each source has its own cache prefix (`lf:` and `ob:`) so their tags never mix. MusicBrainz allows one request a second across all threads; `OpenArtists` enforces that.

## Live check

The tests fake every service. `scripts/live_check.py` calls the real MusicBrainz, ListenBrainz, Deezer and Wikipedia APIs and checks their answers still have the shape Recordhound expects.
The *Live check* workflow runs it every Monday and opens (or comments on) an issue when something fails. Run it by hand from the Actions tab, or locally with `python scripts/live_check.py`.

