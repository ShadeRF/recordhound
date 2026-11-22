<p align="center">
  <img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/icons/recordhound.png" width="96" alt="">
  <br>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/icons/recordhound-wordmark.svg">
    <img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/icons/recordhound-wordmark-light-bg.svg" height="64" alt="Recordhound">
  </picture>
</p>

Self-hosted music recommendations built from **your** Plex library and listening history.
Browse new artists, see why each one was picked, preview them, and send what you want to Lidarr.
Nothing is ever downloaded automatically.

Sibling app for movies and TV: [Filmhound](https://github.com/ShadeRF/filmhound).

<p align="center"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/recordhound-for-you.jpg" alt="For You: top picks, each with the artists that led to it, then new releases from artists you love"></p>

<table>
  <tr>
    <td width="50%"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/recordhound-details.jpg" alt="An artist page: previews, tags, and why it was picked"><br><sub>Every artist: previews, why it was picked, and one click to add it to Lidarr.</sub></td>
    <td width="50%"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/recordhound-library.jpg" alt="Your Library: albums you own but have never played, best for you first"><br><sub>Your Library: albums you own but never played, ready to send to a Plex playlist.</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/recordhound-rate.jpg" alt="The Rate page: rate the artists you play, one at a time"><br><sub>Rate the artists you play; ratings go to Plex and Plexamp too.</sub></td>
    <td width="50%" align="center"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/recordhound-phone.jpg" width="45%" alt="For You on a phone"><br><sub>Works on your phone.</sub></td>
  </tr>
</table>

<sub>Screenshots show a demo library.</sub>

## Why Recordhound?

I tried a lot of music recommendation apps, and none of them gave me suggestions that truly matched what was in my library or what I actually listen to. They leaned on what's popular, what a service wants to promote, or what everyone else is playing.

So I built my own. Recordhound starts from your Plex library and listening history and works outward from there:

- **Your library and listening come first.** Your star ratings, your plays in Plex and Plexamp, and listening from ListenBrainz, Spotify or Last.fm shape your taste.
- **Every pick explains itself:** the artists of yours that led to it, and the genres and scenes it shares with your taste.
- **You can tune it.** In Settings you choose how much artists you own but rarely play count, how strongly recent listening pulls, how many plays elsewhere it takes before an artist counts, and how well known picks should be. You can also stop libraries, genres or artists from shaping your picks.

## Install

**Unraid:** search for **Recordhound** in Community Apps. Setup is covered in [the Unraid guide](https://github.com/ShadeRF/unraid-templates/blob/main/docs/recordhound.md).

**Docker anywhere else:**

```yaml
services:
  recordhound:
    image: ghcr.io/shaderf/recordhound:latest
    container_name: recordhound
    restart: unless-stopped
    ports:
      - "8789:8789"
    volumes:
      - ./recordhound:/data
    environment:
      TZ: America/New_York   # used for the nightly sync time
```

Open `http://YOUR-SERVER:8789`. A short setup walkthrough opens on first start: connect Plex and the first sync begins. No API keys are required. A Getting started checklist on the home page then points to the optional extras.
The first sync looks up every artist and can take a while (MusicBrainz allows one lookup a second, so artists without a known MusicBrainz id are slow); lookups are then cached for a month and later syncs are quick.
The container runs as `99:100` by default (`PUID`, `PGID`, `UMASK` change that).

**Versions:** the end of the image name picks which builds you get.

| Tag | What you get |
|---|---|
| `:latest` | Stable releases. Recommended. |
| `:nightly` | Built each night from the newest code, when there's anything new. New features first, and occasionally a bug. |
| `:0.13.0` or `:0.13` | One exact release (or the newest fix in that line). Use it to stay put or to roll back if a release causes trouble. |
| `:edge` | Test builds, published whenever a change is being tried out, sometimes several times a day. Expect rough edges; only use it if you're helping test. |

Your settings and data carry over when you switch, so you can move between them any time.

## What you need

- A Plex server with a music library, and its token (Plex Web → any track → ⋯ → Get Info → View XML; the token is the `X-Plex-Token=` value in the address)
- Optional: ListenBrainz, Spotify, Lidarr, and a Last.fm API key (see [Last.fm](#lastfm))

## What's in it

- **For You:** a *This week* summary (new picks, new releases, what's coming soon), *Top picks* (reshuffled weekly, never dominated by one of your artists), *New for you this week*, *Because you play…*, *Because you love…*, genre rows, *Deep cuts*, *New from artists you love*, *Albums you're missing*, *In your library, never played* and your saved artists.
- **All Picks:** every pick with genre include/exclude, how well known an artist is (deep cuts, mid, well known), and sorting.
- **You Stream:** artists you play a lot on ListenBrainz, Spotify or Last.fm but don't own in Plex (or Lidarr), most-played first. The best list to send to Lidarr.
- **Your Library:** albums you own but have never played, ranked by how much you like the artist and how well they fit your taste, each with a reason. One click puts the top 25, 50 or 100 in a Plex playlist for Plexamp.
- **Rate:** rate the artists you play, one at a time (keys 1–5; hold Shift for a half star). Ratings go to the artist in Plex. You can also rate picks you already know, so they stop being suggested and shape the rest. Every artist page has a **Rate** button too.
- **Albums you're missing** from artists you already collect, and **new releases** from artists you love, including announced albums from MusicBrainz.
- **Artist pages:** 30-second previews, why it was picked, bio, albums with the ones you own marked, similar artists, *More like this*, and links to Spotify, Apple Music, Bandcamp, YouTube, MusicBrainz and Last.fm. Open artists you own straight in Plex.
- **Lidarr:** add artists with your quality and metadata profiles and choose what to monitor.
- **Save for later**, **Not interested**, **Already know them** (stops suggesting them without counting as a dislike), and **Don't learn from** for kids' music, someone else's albums, whole libraries or genres.
- **History:** everything you've hidden or marked, with undo.
- **Search** for any artist.
- **Settings page** for every connection, with a **Test** button for each, and optional sign-in.

## How it picks

1. **Your taste:** star ratings count most, then how much you play an artist anywhere (Plexamp, ListenBrainz, Spotify, Last.fm), then what you own (adjustable). Artists you've played in the last six months get extra pull, so picks follow what you're into now (adjustable).
2. **Candidates:** similar artists for each of your strongest artists. Artists that several of yours point to rank highest.
3. **Taste match:** a profile of the genres, scenes and eras you return to, from artist tags.
4. **Popularity:** a small nudge toward artists with enough listeners to have good data.

Score = 55% "your artists point here" + 35% taste match + 10% popularity. Every pick lists the artists that led to it.

## What it connects to

| Service | Needed? | Used for |
|---|---|---|
| Plex | Yes | Your music libraries, play counts (Plexamp included) and star ratings |
| MusicBrainz, ListenBrainz | Automatic, no key | Artist ids, similar artists (what ListenBrainz users play together), genre tags |
| Deezer | Automatic, no key | Related artists, fan counts, photos, album lists, 30-second previews |
| Wikipedia | Automatic, no key | Artist bios |
| ListenBrainz account | Optional | Your listening history (public profile) |
| Spotify history file | Optional | Upload the zip from spotify.com/account/privacy. No developer account needed |
| Spotify account | Optional | Top, followed and saved artists, kept current. Needs a personal Spotify developer app (Spotify requires Premium and allows 5 users per app) |
| Last.fm | Optional | See below |
| Lidarr | Optional | Adding artists |

Your data stays in `/data` (`recordhound.db`, `settings.json`). Recordhound only talks to the services above.

### Last.fm

With a Last.fm API key, Recordhound uses Last.fm's similar artists, tags and listener counts instead of the free sources, and can read your scrobbles.
From December 1, 2026, a Last.fm key only works while the account that owns it has **Last.fm Pro**. If your key stops working, Recordhound switches to the free sources by itself and shows a warning; clear the key in Settings to make that permanent.
If you scrobble to ListenBrainz as well, that's a free way to bring in your history.

### Connecting Spotify

Create an app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) (tick **Web API**) and add the Redirect URI `https://shaderf.github.io/recordhound-auth/`. Paste its Client ID in Settings, click **Connect Spotify** and approve. You come straight back to Recordhound.

Spotify only sends people back to `https://` or `127.0.0.1` addresses, which a home server usually isn't, so that address is a small page that forwards you back to your own Recordhound. It runs in your browser and never sees your Spotify data ([how it works](https://github.com/ShadeRF/recordhound-auth)). If Recordhound is on HTTPS, you can skip it: register `https://your-address/callback` in your Spotify app and enter the same address under **Redirect address** in Settings.

Connected Spotify before one-click sign-in? Your connection keeps working, and reconnecting still uses copy and paste. To switch, add the new Redirect URI in your Spotify app, then click **Switch to one-click sign-in** in Settings → Spotify and reconnect.

Spotify ends these connections after six months, and Settings reminds you.

## HTTPS

Run it behind a reverse proxy (Nginx Proxy Manager, SWAG, Caddy, Traefik or `tailscale serve`) at the root of a domain or subdomain. Proxies on your own network are trusted automatically; set `TRUSTED_PROXIES` to narrow that.
Or serve HTTPS directly: set `TLS_CERT` and `TLS_KEY` to a certificate and key inside the container, such as `/data/certs/cert.pem` and `/data/certs/key.pem`.
Turn on sign-in in Settings before reaching it from outside your home.

## Tips

- Star ratings are the biggest lever. The **Rate** page makes it quick, and the ratings go to Plex and Plexamp too.
- Picks feel stuck on an old phase? Raise *What you've played lately* in Settings.
- Listening outside Plex only counts once you've played an artist a few times (3 by default; *Plays before outside listening counts* in Settings), so checking someone out once doesn't steer your picks. Spotify history files count listening time, so skipping through songs adds up to less than one play each.
- The Plex playlist is named *Recordhound: Unplayed albums* and is replaced each time you make it. Set `PLEX_PLAYLIST` to change the name.
- Too many obscure picks? Raise *Minimum Deezer fans* (or *Minimum Last.fm listeners* with a Last.fm key). Too mainstream? Lower it and look at *Deep cuts*.
- Every setting can also come from an environment variable with the same name (`PLEX_URL`, `LIDARR_URL`…). Anything saved on the Settings page wins.
- Delete `recordhound.db` and `settings.json` in your data folder to start fresh.

## About the name

Filmhound and Recordhound are named for Daisy, my beagle and the hound of my life. She's gone now, but she always had a nose for good things, and I like to think these apps carry a little of that.

## Source and license

The source code is at [github.com/ShadeRF/recordhound](https://github.com/ShadeRF/recordhound). Bug reports and ideas are welcome as [issues](https://github.com/ShadeRF/recordhound/issues); see [CONTRIBUTING](https://github.com/ShadeRF/recordhound/blob/main/CONTRIBUTING.md) before sending code.

Recordhound is free software under the [GNU Affero General Public License v3.0](https://github.com/ShadeRF/recordhound/blob/main/LICENSE). You can use, study, change and share it. Anything built from it must stay open under the same license, including versions offered to other people over a network.

The name *Recordhound* and its logo and icons are not covered by that license. If you publish a changed version, give it a different name and its own logo, and don't present it as the original.

Copyright © 2026 ShadeRF.
