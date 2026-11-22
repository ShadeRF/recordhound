// Recordhound: vanilla JS, hash router, no build step.
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };
let view = "home", items = [], total = 0, loading = false, current = null, lastStatus = {}, navList = [];
const REG = new Map();
const SAVED = new Set();

function toast(msg, action) {
  const t = $("toast"); t.innerHTML = `<span>${esc(msg)}</span>`;
  if (action) { const b = document.createElement("button"); b.textContent = action.label; b.onclick = () => { t.style.display = "none"; action.fn(); }; t.appendChild(b); }
  t.style.display = "flex"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", action ? 6000 : 2800);
}
const STALE = `${document.title} was updated, but the server is still running the old version. Restart the container (Compose Down, then Compose Up) to finish.`;
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  // A 404 for an endpoint the page knows about means new files were copied in but the server
  // process wasn't restarted, so the page is newer than the code that's running.
  if (r.status === 404 && body.detail === "Not Found" && path.startsWith("/api/")) throw new Error(STALE);
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
const fav = d => `https://www.google.com/s2/favicons?domain=${d}&sz=64`;
const fmtN = n => n == null ? "" : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n);
const fmtDate = d => { try { return new Date(d + "T12:00").toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }); } catch { return d || ""; } };
const initials = n => esc((n || "?").replace(/^the\s+/i, "").split(/\s+/).slice(0, 2).map(w => w[0]).join("").toUpperCase());
const enc = s => encodeURIComponent(s ?? "").replace(/['()*!]/g, c => "%" + c.charCodeAt(0).toString(16).toUpperCase());
const artURL = r => `/api/art/${enc(r.key)}?name=${enc(r.name)}`;
const SRC = { lastfm: ["last.fm", "Last.fm"], listenbrainz: ["listenbrainz.org", "ListenBrainz"], spotify: ["spotify.com", "Spotify"], spotify_export: ["spotify.com", "Spotify history"], plex_comp: ["plex.tv", "Soundtracks & compilations in Plex"], youtube_music: ["music.youtube.com", "YouTube Music"] };

const ICON = {
  hide: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg>`,
  heart: `<svg viewBox="0 0 24 24"><path d="M12 20s-7.5-4.6-9.2-9.3C1.6 7.3 3.9 4 7.3 4c2 0 3.5 1.1 4.7 2.7C13.2 5.1 14.7 4 16.7 4c3.4 0 5.7 3.3 4.5 6.7C19.5 15.4 12 20 12 20z"/></svg>`,
  play: `<svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z"/></svg>`,
  star: `<svg viewBox="0 0 24 24" class="fill"><path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.8z"/></svg>`,
  pause: `<svg viewBox="0 0 24 24"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>`,
  plus: `<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>`,
  check: `<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`,
  similar: `<svg viewBox="0 0 24 24"><circle cx="8" cy="12" r="5"/><circle cx="16" cy="12" r="5"/></svg>`,
  thumb: `<svg viewBox="0 0 24 24"><path d="M7 11v9H4v-9zM7 11l4-7c1.5 0 2.5 1 2.2 2.6L12.6 10H19a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 17.8 20H7"/></svg>`,
  ear: `<svg viewBox="0 0 24 24"><path d="M7 9a5 5 0 0 1 10 0c0 3-3 4-3 7a3 3 0 0 1-6 0"/></svg>`,
  left: `<svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>`,
  right: `<svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>`,
};

// ------------------------------------------------------------------ stars (same as Filmhound)
function starsWidget(cur, attr) {
  const v = Math.round(cur || 0);
  return `<span class="stars" ${attr}>${[1, 2, 3, 4, 5].map(i => `<button data-star="${i}" class="${v >= i * 2 ? "on" : v === i * 2 - 1 ? "half" : ""}" aria-label="${i} star${i === 1 ? "" : "s"}">${ICON.star}<span class="sf">${ICON.star}</span></button>`).join("")}</span>`;
}
// 1-10 from where the pointer is on a star: left half = half star. Enter or Space (no pointer) gives a whole star.
const starValue = (b, e) => +b.dataset.star * 2 - (e.detail !== 0 && e.clientX - b.getBoundingClientRect().left < b.offsetWidth / 2 ? 1 : 0);
function starHover(st) {
  const show = v => st.querySelectorAll("button").forEach(x => { const i = +x.dataset.star;
    x.classList.toggle("hov", v >= i * 2); x.classList.toggle("hovhalf", v === i * 2 - 1); });
  st.onmousemove = e => { const b = e.target.closest("[data-star]"); if (b) { show(starValue(b, e)); st.classList.add("hovering"); } };
  st.onmouseleave = () => { show(0); st.classList.remove("hovering"); };
}
const starText = r10 => { const v = Math.round(r10 || 0); return "★".repeat(Math.floor(v / 2)) + (v % 2 ? "½" : ""); };

// ------------------------------------------------------------------ cards
function whyHTML(r) {
  const b = r.because || [];
  if (b.length) {
    const played = b.filter(x => x.kind === "played" || x.kind === "rated");
    const lbl = played.length ? (played.some(x => x.kind === "rated") && !played.some(x => x.kind === "played") ? "Because you love" : "Because you play") : "Because you have";
    return `<div class="lbl">${lbl}</div><div class="ttl">${(played.length ? played : b).slice(0, 2).map(x => esc(x.name)).join(" · ")}</div>`;
  }
  if (r.sources && Object.keys(r.sources).length) {
    const [s, v] = Object.entries(r.sources).sort((a, b) => (b[1].plays || 0) - (a[1].plays || 0))[0];
    const label = SRC[s] ? SRC[s][1] : s;
    return `<div class="lbl">You play it on</div><div class="ttl">${esc(label)}${v.plays && s !== "spotify" ? ` · ${fmtN(v.plays)} plays` : v.rank ? ` · #${v.rank} top artist` : v.saved ? " · followed" : ""}</div>`;
  }
  if ((r.matches || []).length) return `<div class="lbl">Matches your taste</div><div class="ttl">${r.matches.slice(0, 3).map(esc).join(" · ")}</div>`;
  return "";
}
function card(r, opt = {}) {
  REG.set(r.key, r);
  if (r.saved) SAVED.add(r.key);
  const badge = opt.badge ?? (r.owned ? `<span class="match own">${ICON.check} Owned</span>` : r.score != null && !r.sources ? `<span class="match">${Math.round(r.score * 100)}%</span>` : "");
  const tags = (r.tags || []).slice(0, 2);
  const why = opt.why === false ? "" : opt.role ? `<div class="lbl">${esc(opt.role)}</div>` : whyHTML(r);
  return `<div class="card sq${opt.dim ? " dim" : ""}" data-key="${esc(r.key)}" tabindex="0">
    <div class="poster"><span class="ini">${initials(r.name)}</span><img loading="lazy" src="${artURL(r)}" alt="" onerror="this.remove()" style="position:absolute;inset:0"></div>
    ${badge}
    ${opt.noHide || r.owned ? "" : `<button class="quick" title="Not interested" data-hide="${esc(r.key)}">${ICON.hide}</button>`}
    ${r.owned ? "" : `<button class="quick sv${SAVED.has(r.key) ? " on" : ""}" title="Save for later" data-sv="${esc(r.key)}">${ICON.heart}</button>`}
    <div class="t" title="${esc(r.name)}">${esc(r.name)}</div>
    <div class="s">${r.listeners ? fmtN(r.listeners) + " listeners" : r.fans ? fmtN(r.fans) + " fans" : ""}</div>
    ${tags.length ? `<div class="tagline">${tags.map(t => `<span>${esc(t)}</span>`).join("")}</div>` : ""}
    ${why ? `<div class="why">${why}</div>` : ""}</div>`;
}
function albumCard(a, opt = {}) {
  const k = a.artist_key || "";
  return `<div class="card al" data-key="${esc(k)}" data-name="${esc(a.artist || "")}" tabindex="0">
    <div class="poster">${a.cover ? `<img loading="lazy" src="${esc(a.cover)}" alt="${esc(a.title)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:this.alt}))">` : `<span>${esc(a.title)}</span>`}</div>
    ${upcoming(a.date) ? `<span class="match taste">${esc(fmtDate(a.date))}</span>` : ""}
    <div class="t" title="${esc(a.title)}">${esc(a.title)}</div>
    <div class="s"><b>${esc(a.artist || "")}</b>${a.date && !opt.noYear ? " · " + esc(a.date.slice(0, 4)) : ""}${a.type === "ep" ? " · EP" : ""}</div>
    ${opt.role ? `<div class="why"><div class="lbl">${esc(opt.role)}</div></div>` : ""}</div>`;
}
const upcoming = d => !!d && d > new Date().toISOString().slice(0, 10);
const skeleton = (n = 14) => Array.from({ length: n }, () => `<div class="card sq sk"><div class="poster"></div><div class="t">&nbsp;</div><div class="s">&nbsp;</div></div>`).join("");
function emptyState() {
  const s = lastStatus;
  if (!s.configured) return `<div class="empty" style="grid-column:1/-1">Connect Plex to get started. <a href="#/settings">Open Settings</a></div>`;
  if (s.running) return `<div class="empty" style="grid-column:1/-1"><b>${esc(s.step || "Working")}</b>${s.progress ? " · " + esc(s.progress) : ""}<br><span class="muted">The first sync looks up every artist, which can take a while. Later syncs are quick.</span></div>`;
  if (view === "stream" && !hasHistory()) return `<div class="empty" style="grid-column:1/-1">You Stream lists artists you play outside Plex, and no listening history is connected yet.<br><br>
    <button class="btn primary" data-open-setting="set-history">Open Settings › Listening history</button></div>`;
  return `<div class="empty" style="grid-column:1/-1">Nothing here with these filters.</div>`;
}
// any listening outside Plex: ListenBrainz, Spotify, YouTube Music or Last.fm (Plex's own soundtracks row doesn't count)
const hasHistory = () => Object.entries(lastStatus.history || {}).some(([s, n]) => n && s !== "plex_comp");
document.addEventListener("click", e => { const b = e.target.closest && e.target.closest("[data-open-setting]"); if (b) { e.preventDefault(); openSetting(b.dataset.openSetting); } });

// ------------------------------------------------------------------ filters (All Picks)
const blankF = () => ({ q: "", tags: {}, mode: "any", pop: "", sort: "score", seed: "", seedLabel: "", fresh: 0 });
let F = blankF(), tagFacets = [], openPop = null;
function loadF() { try { F = { ...blankF(), ...JSON.parse(store.get("filters_" + view) || "{}"), seed: "", seedLabel: "", fresh: 0 }; } catch { F = blankF(); } }
function saveF() { const { seed, seedLabel, fresh, ...keep } = F; store.set("filters_" + view, JSON.stringify(keep)); }
const inc = () => Object.keys(F.tags).filter(t => F.tags[t] === 1), exc = () => Object.keys(F.tags).filter(t => F.tags[t] === -1);
function params(kind) {
  const p = new URLSearchParams({ kind, sort: F.sort, pop: F.pop, q: F.q, tags: inc().join(","), exclude_tags: exc().join(","), tag_mode: F.mode, seed: F.seed, fresh: F.fresh ? 1 : 0 });
  return p.toString();
}
function syncControls() {
  $("q").value = F.q; $("sort").value = F.sort;
  document.querySelectorAll("#popSeg button").forEach(b => b.classList.toggle("on", b.dataset.popV === F.pop));
  const tb = document.querySelector('[data-pop="tags"]'), n = inc().length + exc().length;
  tb.classList.toggle("active", n > 0); tb.innerHTML = `Genres &amp; tags ${n ? `<span class="n">${n}</span>` : `<span class="car">▼</span>`}`;
  const chips = [];
  if (F.seedLabel) chips.push(`<span class="achip">${esc(F.seedLabel)} <button data-clear="seed">×</button></span>`);
  if (F.fresh) chips.push(`<span class="achip">New this week <button data-clear="fresh">×</button></span>`);
  inc().forEach(t => chips.push(`<span class="achip">${esc(t)} <button data-clear-tag="${esc(t)}">×</button></span>`));
  exc().forEach(t => chips.push(`<span class="achip">not ${esc(t)} <button data-clear-tag="${esc(t)}">×</button></span>`));
  if (chips.length) chips.push(`<button class="link" data-clear="all">Clear all</button>`);
  $("activeRow").innerHTML = chips.join("");
  filterFade();
}
let debounce;
function changed(now) { saveF(); syncControls(); clearTimeout(debounce); debounce = setTimeout(() => load(true), now ? 0 : 200); }
$("q").oninput = e => { F.q = e.target.value; changed(); };
$("sort").onchange = e => { F.sort = e.target.value; changed(true); };
$("popSeg").onclick = e => { const b = e.target.closest("[data-pop-v]"); if (b) { F.pop = b.dataset.popV; changed(true); } };
$("activeRow").onclick = e => {
  const c = e.target.closest("[data-clear]"), t = e.target.closest("[data-clear-tag]");
  if (t) { delete F.tags[t.dataset.clearTag]; changed(true); }
  if (c) { if (c.dataset.clear === "seed") { F.seed = ""; F.seedLabel = ""; } else if (c.dataset.clear === "fresh") F.fresh = 0; else { const s = F.sort; F = blankF(); F.sort = s; } changed(true); }
};
document.querySelector('[data-pop="tags"]').onclick = e => { e.stopPropagation(); openPop === "tags" ? closePop() : renderPop(); };
function closePop() { openPop = null; $("pop").classList.remove("on"); }
function renderPop() {
  openPop = "tags";
  const taste = new Set((tagFacets.taste || []).map(t => t.tag));
  $("pop").innerHTML = `<h5>Genres &amp; tags</h5><p class="hint">Click once to include, twice to exclude, again to clear.</p>
    <div class="seg" style="margin-bottom:10px"><button data-mode="any" class="${F.mode === "any" ? "on" : ""}">Any of these</button><button data-mode="all" class="${F.mode === "all" ? "on" : ""}">All of these</button></div>
    <div class="gchips">${(tagFacets.tags || []).map(t => `<button class="gchip ${F.tags[t.tag] === 1 ? "inc" : F.tags[t.tag] === -1 ? "exc" : ""}" data-tag="${esc(t.tag)}">${taste.has(t.tag) ? "★ " : ""}${esc(t.tag)}<small>${t.n}</small></button>`).join("")}</div>
    <div class="foot"><span class="muted" style="font-size:12px">★ = in your taste</span><button class="link" data-tags-clear>Clear</button></div>`;
  $("pop").style.left = "200px"; $("pop").classList.add("on");
}
$("pop").onclick = e => {
  e.stopPropagation();
  const g = e.target.closest("[data-tag]"), m = e.target.closest("[data-mode]");
  if (g) { const t = g.dataset.tag, v = F.tags[t]; if (!v) F.tags[t] = 1; else if (v === 1) F.tags[t] = -1; else delete F.tags[t]; renderPop(); changed(); }
  if (m) { F.mode = m.dataset.mode; renderPop(); changed(); }
  if (e.target.closest("[data-tags-clear]")) { F.tags = {}; renderPop(); changed(); }
};
document.addEventListener("click", e => { if (openPop && !e.composedPath().includes($("pop"))) closePop(); });

// ------------------------------------------------------------------ grid (All Picks / You Stream)
const gridKind = () => view === "stream" ? "stream" : "new";
let seq = 0;
async function load(reset) {
  if (loading && !reset) return;
  if (!reset && items.length >= total) return;
  const my = ++seq; loading = true;
  if (reset) { items = []; total = 0; $("grid").innerHTML = skeleton(); }
  try {
    const d = await api(`/api/recs?${params(gridKind())}&offset=${items.length}&limit=60`);
    if (my !== seq) return;
    total = d.total; items = items.concat(d.items);
    if (reset) $("grid").innerHTML = items.length ? items.map(r => card(r)).join("") : emptyState();
    else $("grid").insertAdjacentHTML("beforeend", d.items.map(r => card(r)).join(""));
    $("count").textContent = total ? `${total.toLocaleString()} ${view === "stream" ? "artists you play but don't own" : "artists"}` : "";
  } catch (e) {
    if (my !== seq) return;   // an older request failing mustn't touch the newer page
    if (reset) $("grid").innerHTML = `<div class="empty" style="grid-column:1/-1">${esc(e.message)}</div>`;
    else toast(e.message);    // loading more failed: keep what's already on screen
  }
  finally { if (my === seq) loading = false; }
}
window.addEventListener("scroll", () => {
  if ((view === "picks" || view === "stream") && items.length < total && $("sentinel").getBoundingClientRect().top < innerHeight + 1200) load(false);
}, { passive: true });

// ------------------------------------------------------------------ clicks on cards
document.addEventListener("click", e => {
  const sv = e.target.closest(".quick.sv");
  if (sv) { e.stopPropagation(); toggleSaved(sv.dataset.sv); return; }
  const h = e.target.closest(".quick[data-hide]");
  if (h) { e.stopPropagation(); quickHide(h.dataset.hide); return; }
  const c = e.target.closest(".card[data-key]");
  if (c && c.dataset.key && !e.target.closest("a,button")) {
    const scope = c.closest("[data-list]") || c.parentElement;
    navList = [...new Set([...scope.querySelectorAll(".card[data-key]")].map(x => x.dataset.key).filter(Boolean))];   // album rows repeat their artist
    openArtist(c.dataset.key, c.dataset.name || (REG.get(c.dataset.key) || {}).name);
  }
});
document.addEventListener("keydown", e => { if (e.key === "Enter" && (e.target.classList?.contains("card") || e.target.classList?.contains("lk-row") || e.target.classList?.contains("lk-ahead"))) e.target.click(); });
async function rateArtist(rating) {
  const r = current; if (!r) return;
  try { await api("/api/rate", { method: "POST", body: JSON.stringify({ key: r.key, name: r.name, rating }) }); } catch (e) { toast(e.message); return; }
  if (r.owned) {
    current = { ...r, my_rating: rating }; REG.set(r.key, current); renderDrawer(current, false);
    toast(`Rated ${r.name} ${starText(rating)} and saved to Plex`);
    return;
  }
  closeDrawer(); removeCards(r.key);   // you know them now, so they stop being suggested
  toast(`Rated ${r.name} ${starText(rating)}`, { label: "Undo", fn: async () => { try { await api("/api/feedback/" + encodeURIComponent(r.key), { method: "DELETE" }); } catch (e) { toast(e.message); return; } refreshView(); } });
}
function removeCards(key) {
  document.querySelectorAll(`.card.sq[data-key="${CSS.escape(key)}"]`).forEach(x => { x.classList.add("leaving"); setTimeout(() => x.remove(), 350); });
  const i = items.findIndex(r => r.key === key); if (i >= 0) { items.splice(i, 1); total--; }
}
async function act(key, action) {
  const r = REG.get(key) || current || { key, name: key };
  try { await api("/api/feedback", { method: "POST", body: JSON.stringify({ key, name: r.name, action }) }); } catch (e) { toast(e.message); return false; }
  return true;
}
async function quickHide(key) {
  const r = REG.get(key); if (!(await act(key, "dismissed"))) return;
  removeCards(key);
  toast(r ? `Not interested in ${r.name}` : "Not interested", { label: "Undo", fn: async () => { await api("/api/feedback/" + encodeURIComponent(key), { method: "DELETE" }); refreshView(); } });
}
async function toggleSaved(key) {
  const r = REG.get(key) || current; if (!r) return;
  const on = !SAVED.has(key);
  try { await api("/api/saved", { method: "POST", body: JSON.stringify({ key, name: r.name, on }) }); } catch (e) { toast(e.message); return; }
  on ? SAVED.add(key) : SAVED.delete(key); r.saved = on;
  document.querySelectorAll(`.quick.sv[data-sv="${CSS.escape(key)}"]`).forEach(b => b.classList.toggle("on", on));
  const d = $("dSave"); if (d && current && current.key === key) { d.classList.toggle("on", on); d.innerHTML = ICON.heart + (on ? "Saved" : "Save"); }
  if (!on && view === "saved") removeCards(key);
  toast(on ? `Saved ${r.name}` : `Removed ${r.name}`);
}

// ------------------------------------------------------------------ previews
const audio = $("audio"); let playing = null;
// Full songs in your Spotify app when connected; the 30-second preview otherwise, or when Spotify can't.
const WHY_PREVIEW = { not_found: "Not on Spotify", no_device: "Spotify isn't open", reconnect: "Reconnect Spotify to play full songs" };
const playOn = () => matchMedia("(pointer: coarse)").matches ? "phone" : "computer";   // only ever your own kind of device
async function viaSpotify(kind, title, artist, isrc) {
  if (!lastStatus.spotify_play) return false;
  let r = null;
  try { r = await api("/api/spotify/play-match", { method: "POST", body: JSON.stringify({ kind, title, artist, isrc: isrc || null, on: playOn() }) }); } catch {}
  if (r && r.ok) {
    if (!audio.paused) audio.pause();
    toast(`Playing ${title} on ${r.device}`);
    return true;
  }
  if (r && r.reason === "no_device" && r.id) {   // no Spotify app open here: open it on this song (or the web player)
    if (!audio.paused) audio.pause();
    toast("Opening Spotify. Press play there; while it's open, play buttons start songs by themselves.");
    openSpotifyApp(`spotify:${kind}:${r.id}`, `https://open.spotify.com/${kind}/${r.id}`);
    return true;
  }
  const why = (r && (WHY_PREVIEW[r.reason] || r.message)) || "Spotify didn't answer";
  if (r && r.reason === "reconnect") toast(`${why}. Playing the preview.`, { label: "Reconnect", fn: () => openSetting("spotify") });
  else toast(kind === "album" ? `${why}.` : `${why}. Playing the preview.`);
  return false;
}
async function playTrack(i) {
  const t = (current?.tracks || [])[i]; if (!t) return;
  if (await viaSpotify("track", t.title, current.name, t.isrc)) return;
  const id = current.key + ":" + i;
  if (playing === id && !audio.paused) { audio.pause(); return; }
  audio.src = t.preview; audio.play().catch(() => toast("Couldn't play that preview"));
  playing = id; nowPlaying(t, current.name);
}
let albumTracks = [];   // the open album's tracks
async function playAlbumTrack(i) {
  const t = albumTracks.find(x => x.i === i); if (!t) return;
  if (await viaSpotify("track", t.title, t.by, t.isrc)) return;
  if (!t.preview) return;
  const id = `alb:${t.album}:${i}`;
  if (playing === id && !audio.paused) { audio.pause(); return; }
  audio.src = t.preview; audio.play().catch(() => toast("Couldn't play that preview"));
  playing = id; nowPlaying({ title: t.title, cover: t.cover }, t.by);
}
function nowPlaying(t, artist) {
  $("np").innerHTML = `${t.cover ? `<img src="${esc(t.cover)}" alt="">` : ""}<div><b>${esc(t.title)}</b><small>${esc(artist)} · 30-second preview</small></div>
    <button id="npToggle" title="Play/pause">${audio.paused ? "▶" : "❚❚"}</button><button id="npClose" title="Stop">×</button>`;
  $("np").classList.add("on");
}
audio.addEventListener("play", paintTracks); audio.addEventListener("pause", paintTracks);
audio.addEventListener("ended", () => {
  if ((playing || "").startsWith("alb:")) { const i = +playing.split(":")[2]; if (albumTracks.find(t => t.i > i && t.preview)) playAlbumTrack(albumTracks.find(t => t.i > i && t.preview).i); else paintTracks(); return; }
  const [k, i] = (playing || "").split(":"); if (current && current.key === k && current.tracks[+i + 1]) playTrack(+i + 1); else paintTracks();
});
audio.addEventListener("timeupdate", () => { const b = document.querySelector(".trk.on .tprog i, .atrk.on .tprog i"); if (b && audio.duration) b.style.width = (audio.currentTime / audio.duration * 100) + "%"; });
$("np").onclick = e => {
  if (e.target.id === "npClose") { audio.pause(); audio.removeAttribute("src"); playing = null; $("np").classList.remove("on"); paintTracks(); }
  if (e.target.id === "npToggle") audio.paused ? audio.play() : audio.pause();
};
function paintTracks() {
  document.querySelectorAll(".trk").forEach(el => {
    const on = current && playing === current.key + ":" + el.dataset.i;
    el.classList.toggle("on", !!on);
    el.querySelector(".pb").innerHTML = on && !audio.paused ? ICON.pause : ICON.play;
  });
  document.querySelectorAll(".atrk").forEach(el => {
    const on = playing === `alb:${el.dataset.alb}:${el.dataset.ai}`;
    el.classList.toggle("on", on);
    const pb = el.querySelector(".pb"); if (pb) pb.innerHTML = on && !audio.paused ? ICON.pause : ICON.play;
  });
  const tg = $("npToggle"); if (tg) tg.textContent = audio.paused ? "▶" : "❚❚";
}

// ------------------------------------------------------------------ drawer
async function openArtist(key, name) {
  if (!key) return;
  const r = REG.get(key) || { key, name };
  current = { ...r, tracks: [] };
  renderDrawer(current, true);
  $("drawer").classList.add("on"); $("scrim").classList.add("on");
  try {
    const d = await api(`/api/artist/${encodeURIComponent(key)}?name=${encodeURIComponent(name || r.name || "")}`);
    if (!current || current.key !== key) return;
    current = { ...r, ...d }; REG.set(key, { ...r, ...d });
    if (d.saved) SAVED.add(key);
    renderDrawer(current, false);
  } catch (e) { if (current && current.key === key) toast(e.message); }
}
function closeDrawer() { $("drawer").classList.remove("on"); $("scrim").classList.remove("on"); current = null; }
$("scrim").onclick = closeDrawer;
document.addEventListener("keydown", e => {
  if (e.target.matches("input,select,textarea") || !current) return;
  if (e.key === "Escape") { closeDrawer(); return; }
  const i = navList.indexOf(current.key);
  if (e.key === "ArrowRight" && i >= 0 && i < navList.length - 1) openArtist(navList[i + 1]);
  if (e.key === "ArrowLeft" && i > 0) openArtist(navList[i - 1]);
});
function seedLine(b) {
  const bits = [];
  if (b.kind === "rated" && b.rating) bits.push(`You rated them ${"★".repeat(Math.round(b.rating / 2))}`);
  else if (b.kind === "played") bits.push(`${b.plays ? fmtN(b.plays) + " plays" : "You play them"}`);
  else if (b.kind === "owned") bits.push("In your library");
  else if (b.kind === "liked") bits.push("You liked them here");
  else if (b.kind === "saved") bits.push("You follow them");
  if (b.owned && b.kind !== "owned") bits.push("in your library");
  return bits.join(" · ");
}
function renderDrawer(r, loading) {
  const i = navList.indexOf(r.key), prev = i > 0 ? navList[i - 1] : null, next = i >= 0 && i < navList.length - 1 ? navList[i + 1] : null;
  const q = encodeURIComponent(r.name || "");
  const links = [
    [r.url || `https://www.last.fm/music/${q}`, "last.fm", "Last.fm"],
    [r.mbid ? `https://musicbrainz.org/artist/${r.mbid}` : `https://musicbrainz.org/search?type=artist&query=${q}`, "musicbrainz.org", "MusicBrainz"],
    [`https://open.spotify.com/search/${q}`, "spotify.com", "Spotify"],
    [`https://music.apple.com/us/search?term=${q}`, "music.apple.com", "Apple Music"],
    [`https://bandcamp.com/search?q=${q}&item_type=b`, "bandcamp.com", "Bandcamp"],
    [`https://www.youtube.com/results?search_query=${q}`, "youtube.com", "YouTube"],
  ];
  const lidarrOn = lastStatus.lidarr && !r.owned;
  const b = r.because || [];
  const srcs = Object.entries(r.sources || {});
  const plex = r.owned && r.plex_key && lastStatus.plex_machine ? `https://app.plex.tv/desktop/#!/server/${lastStatus.plex_machine}/details?key=${encodeURIComponent("/library/metadata/" + r.plex_key)}` : "";
  $("drawer").innerHTML = `
    <div class="d-hero blur" ${r.picture ? `style="background-image:url(${esc(r.picture)})"` : ""}>
      <div class="d-top">
        <button class="ibtn" ${prev != null ? `data-nav="${esc(prev)}"` : "disabled"} title="Previous (←)">${ICON.left}</button>
        <button class="ibtn" ${next != null ? `data-nav="${esc(next)}"` : "disabled"} title="Next (→)">${ICON.right}</button>
        <span style="flex:1"></span><button class="ibtn" data-close title="Close (Esc)">×</button>
      </div>
      <div class="d-head">
        ${r.picture ? `<img class="d-art" src="${esc(r.picture)}" alt="">` : `<div class="d-art" style="display:grid;place-items:center;font:700 44px Georgia,serif;color:#6b625a">${initials(r.name)}</div>`}
        <div class="d-titles">
          <h2>${esc(r.name)}</h2>
          <div class="meta">${[r.listeners ? fmtN(r.listeners) + " Last.fm listeners" : "", r.fans ? fmtN(r.fans) + " Deezer fans" : ""].filter(Boolean).join(" · ")}</div>
          <div class="d-ratings">${r.score != null && b.length ? `<span class="pill mp">${Math.round(r.score * 100)}% match</span>` : ""}
            ${r.owned ? `<span class="pill">${ICON.check} ${r.owned_albums} album${r.owned_albums === 1 ? "" : "s"} in Plex${r.plays ? ` · ${fmtN(r.plays)} plays` : ""}</span>` : ""}
            ${r.in_lidarr ? `<span class="pill">In Lidarr</span>` : ""}</div>
          <div class="d-links">${links.map(([u, d, n]) => `<a href="${esc(u)}" target="_blank" rel="noopener" title="${n}"><img src="${fav(d)}" alt="${n}"></a>`).join("")}</div>
        </div>
      </div>
    </div>
    <div class="d-actions">
      ${plex ? `<a class="btn primary" href="${plex}" target="_blank" rel="noopener"><img class="svc" src="${fav("plex.tv")}" alt="">Open in Plex</a>` : ""}
      ${lidarrOn && !r.in_lidarr ? `<button class="btn primary" data-lidarr>${ICON.plus}Add to Lidarr</button>` : ""}
      ${r.owned ? "" : `<button class="btn ${SAVED.has(r.key) ? "on" : ""}" id="dSave" data-save>${ICON.heart}${SAVED.has(r.key) ? "Saved" : "Save"}</button>`}
      <a class="btn" href="#/like/${encodeURIComponent(r.key)}/${q}" data-close-nav>${ICON.similar}More like this</a>
      ${r.my_rating ? "" : `<button class="btn" data-rate-open>${ICON.star}Rate</button>`}
      ${r.owned ? "" : `<button class="btn" data-act="known" title="You already know them; stop suggesting">${ICON.ear}Already know them</button>
        <button class="btn" data-act="dismissed" title="Hide and show fewer like them">${ICON.hide}Not interested</button>`}
    </div>
    <div class="seenbox ${r.my_rating ? "on" : ""}" id="seenBox">
      <span>${r.owned ? "Your rating (saved to Plex)" : "Know them? Your rating"}</span>
      ${starsWidget(r.my_rating, 'id="seenStars"')}
    </div>
    <div class="arr" id="lidarrBox"></div>
    ${(r.tags || []).length ? `<div class="sec"><div class="chips">${r.tags.map(t => `<a href="#/picks" data-tagdrill="${esc(t)}">${esc(t)}</a>`).join("")}</div></div>` : ""}
    ${loading ? `<div class="sec"><div class="shimmer" style="height:160px"></div></div>` : ""}
    ${(r.tracks || []).length ? `<div class="sec"><h4>Listen</h4><div class="tracks">${r.tracks.map((t, i) => `
      <div class="trk" data-i="${i}"><button class="pb" aria-label="Play">${ICON.play}</button>${t.cover ? `<img src="${esc(t.cover)}" alt="">` : ""}
        <div class="nm"><b>${esc(t.title)}</b><small>${esc(t.album || "")}</small></div><div class="tprog"><i></i></div></div>`).join("")}</div>
      <div class="help" style="color:var(--dim);font-size:12px;margin-top:6px">${lastStatus.spotify_play ? "Plays the full song in your Spotify app." : "30-second previews from Deezer."}</div></div>` : ""}
    ${b.length ? `<div class="sec"><h4>Why this pick</h4><div class="seeds">${b.map(x => `
      <div class="seed" data-open="${esc(x.key)}" style="cursor:pointer">
        <div class="th" style="border-radius:50%;background-image:url('${artURL(x)}')"></div>
        <div class="info"><div class="nm">${esc(x.name)}</div><div class="st">${esc(seedLine(x))}</div>
          <div class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(x.share * 100)}" aria-label="How much this pushed the pick" title="How much this pushed the pick"><i style="width:${Math.round(x.share * 100)}%"></i></div></div></div>`).join("")}</div>
      ${r.seed_count > b.length ? `<div class="more-seeds">+ ${r.seed_count - b.length} more of your artists point here too</div>` : ""}</div>` : ""}
    ${srcs.length ? `<div class="sec"><h4>Your listening</h4><div class="srcs">${srcs.map(([s, v]) => `<span><img src="${fav((SRC[s] || [s])[0])}" alt="">${esc((SRC[s] || [, s])[1])}${v.plays && s !== "spotify" ? ` · ${fmtN(v.plays)} plays` : ""}${v.rank ? ` · #${v.rank}` : ""}${v.saved ? " · followed" : ""}</span>`).join("")}</div></div>` : ""}
    ${(r.matches || []).length ? `<div class="sec"><h4>Matches your taste</h4><div class="chips">${r.matches.map(t => `<a href="#/picks" data-tagdrill="${esc(t)}">${esc(t)}</a>`).join("")}</div></div>` : ""}
    ${r.bio ? `<div class="sec"><h4>About</h4><p class="bio">${esc(r.bio)}${r.bio_url ? ` <a href="${esc(r.bio_url)}" target="_blank" rel="noopener">Read more on Wikipedia</a>` : r.url ? ` <a href="${esc(r.url)}" target="_blank" rel="noopener">Read more on Last.fm</a>` : ""}</p></div>` : ""}
    ${(r.albums || []).length ? `<div class="sec"><h4>Albums &amp; EPs</h4><div class="albums">${r.albums.slice(0, 24).map((a, i) => `
      <button type="button" class="alb${a.owned ? " own" : ""}" data-alb="${i}" aria-expanded="false">${a.cover ? `<img loading="lazy" src="${esc(a.cover)}" alt="">` : `<div class="ph"></div>`}${a.owned ? `<span class="own-b">OWNED</span>` : ""}
        <b>${esc(a.title)}</b><small>${esc((a.date || "").slice(0, 4))}${a.type === "ep" ? " · EP" : ""}</small></button>`).join("")}</div>
      <div class="albbox" id="albBox" hidden></div></div>` : ""}
    ${(r.similar || []).length ? `<div class="sec"><h4>Similar artists</h4><div class="simrow">${r.similar.map(s => `<a data-open="${esc(s.key)}" data-name="${esc(s.name)}" class="${s.owned ? "own" : ""}" title="${s.owned ? "In your library" : ""}"><img loading="lazy" src="${artURL(s)}" alt="" onerror="this.style.visibility='hidden'">${esc(s.name)}</a>`).join("")}</div></div>` : ""}
    ${r.owned ? `<div class="sec noseed-sec"><label class="inline"><input type="checkbox" data-noseed ${r.noseed ? "checked" : ""}> Don't learn from this artist</label>
      <div class="help" style="color:var(--dim);font-size:12px;margin:4px 0 0 24px">Keep them in your library, but stop them from shaping your picks. Good for kids' music or someone else's albums.</div></div>` : ""}
    <div style="height:30px"></div>`;
  if (loading) $("drawer").scrollTop = 0;   // a newly opened artist starts at the top
  const st = $("seenStars");
  if (st) { st.onclick = e => { const b = e.target.closest("[data-star]"); if (b) rateArtist(starValue(b, e)); }; starHover(st); }
  paintTracks();
}
function showAlbum(i) {
  const r = current, a = r.albums[i], box = $("albBox");
  const again = !box.hidden && box.dataset.i === String(i);
  document.querySelectorAll("#drawer button[data-alb]").forEach(b => { b.setAttribute("aria-expanded", "false"); b.classList.remove("sel"); });
  if (again) { box.hidden = true; return; }   // a second click on the same album closes it
  const btn = document.querySelector(`#drawer button[data-alb="${i}"]`); btn.classList.add("sel"); btn.setAttribute("aria-expanded", "true");
  const q = encodeURIComponent(`${r.name} ${a.title}`);
  const plex = a.plex_rk && lastStatus.plex_machine ? `https://app.plex.tv/desktop/#!/server/${lastStatus.plex_machine}/details?key=${encodeURIComponent("/library/metadata/" + a.plex_rk)}` : "";
  box.dataset.i = i; box.hidden = false;
  box.innerHTML = `${a.cover ? `<img src="${esc(a.cover)}" alt="">` : `<div class="ph"></div>`}
    <div class="albinfo"><b>${esc(a.title)}</b><small>${esc([(a.date || "").slice(0, 4), a.type === "ep" ? "EP" : "Album", a.owned ? "in your library" : ""].filter(Boolean).join(" · "))}</small>
      <div class="albacts">
        ${plex ? `<a class="btn primary" href="${plex}" target="_blank" rel="noopener"><img class="svc" src="${fav("plex.tv")}" alt="">Open in Plex</a>` : ""}
        ${lastStatus.lidarr && !a.owned ? `<button class="btn primary" data-alb-lidarr>${ICON.plus}Add album to Lidarr</button>` : ""}
        ${lastStatus.spotify_play ? `<button class="btn lk-sp" data-alb-spotify>${ICON.play}Play album on Spotify</button>`
          : `<a class="btn" href="https://open.spotify.com/search/${q}" target="_blank" rel="noopener" data-alb-open-spotify><img class="svc" src="${fav("spotify.com")}" alt="">Open in Spotify</a>`}
        ${a.id ? `<a class="btn" href="https://www.deezer.com/album/${encodeURIComponent(a.id)}" target="_blank" rel="noopener"><img class="svc" src="${fav("deezer.com")}" alt="">Deezer</a>` : ""}</div>
      <div class="albl" hidden></div></div>
    <div class="atracks" id="albTracks">${a.id ? `<div class="shimmer" style="height:120px"></div>` : ""}</div>`;
  box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  if (a.id) loadAlbumTracks(r, a, i);
}
const mmss = s => s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "";
async function loadAlbumTracks(r, a, i) {
  let d; try { d = await api(`/api/album/${encodeURIComponent(a.id)}/tracks?name=${encodeURIComponent(r.name)}&album=${encodeURIComponent(a.title)}`); }
  catch (e) { if ($("albTracks")) $("albTracks").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const box = $("albBox"); if (!box || box.hidden || box.dataset.i !== String(i) || current !== r) return;   // you've opened something else since
  albumTracks = d.tracks.map((t, n) => ({ ...t, i: n, album: a.id, cover: a.cover, by: t.artist || r.name }));
  if (d.all_owned && !a.owned) {   // you have every track, filed under other album names: it counts as owned
    a.owned = true;
    const tile = document.querySelector(`#drawer button[data-alb="${i}"]`);
    if (tile && !tile.querySelector(".own-b")) { tile.classList.add("own"); tile.insertAdjacentHTML("afterbegin", `<span class="own-b">OWNED</span>`); }
    box.querySelector("[data-alb-lidarr]")?.remove();
    const sm = box.querySelector(".albinfo small"); if (sm && !sm.textContent.includes("library")) sm.textContent += " · in your library";
  }
  const discs = new Set(d.tracks.map(t => t.disc)).size > 1, owned = d.tracks.filter(t => t.owned).length;
  let disc = null;
  $("albTracks").innerHTML = (d.checked_plex && owned ? `<p class="muted atr-note">${d.all_owned ? `You have all ${d.tracks.length} tracks in Plex, so this counts as owned.` : `You own ${owned} of these ${d.tracks.length} tracks.`}</p>` : "") +
    albumTracks.map(t => {
      const head = discs && t.disc !== disc ? `<div class="adisc">Disc ${disc = t.disc}</div>` : "";
      return `${head}<div class="atrk" data-alb="${esc(a.id)}" data-ai="${t.i}">
        ${t.preview || lastStatus.spotify_play ? `<button class="pb" aria-label="Play ${esc(t.title)}">${ICON.play}</button>` : `<span class="pb off" title="No preview"></span>`}
        <span class="an">${t.n || ""}</span><span class="nm">${esc(t.title)}${t.artist && t.artist !== r.name ? ` <small>${esc(t.artist)}</small>` : ""}</span>
        ${t.liked ? `<span class="lk-heart" title="Liked on Spotify">♥</span>` : ""}${t.owned ? `<span class="own-t" title="In your Plex library">✓</span>` : ""}
        <span class="tprog"><i></i></span><span class="dur">${mmss(t.duration)}</span></div>`;
    }).join("");
  paintTracks();
}
// Quality, metadata and folder for sending one album to Lidarr; filled in from the last add.
async function albumLidarrForm(box, note) {
  box.hidden = false; box.innerHTML = `<div class="shimmer" style="height:40px"></div>`;
  try {
    if (!lidarrOpts) lidarrOpts = await api("/api/lidarr/options");
    const last = await api("/api/lidarr/last"), o = lidarrOpts;
    const sel = (arr, cls, v, lbl) => `<select class="${cls}">${arr.map(x => `<option value="${esc(x.id ?? x.path)}" ${String(x.id ?? x.path) === String(v) ? "selected" : ""}>${esc(lbl(x))}</option>`).join("")}</select>`;
    box.innerHTML = `<div class="row">
        <label>Quality profile${sel(o.profiles, "lkQ", last.quality_id, x => x.name)}</label>
        <label>Metadata profile${sel(o.metadata, "lkM", last.metadata_id, x => x.name)}</label>
        <label>Root folder${sel(o.roots, "lkR", last.root, x => x.path)}</label>
        <button class="btn primary" data-lk-add>Add album</button></div>
      <p class="muted">${note}</p>`;
  } catch (err) { box.innerHTML = `<div class="muted">${esc(err.message)}</div>`; }
}
const albumLidarrChoice = box => ({ quality_id: +box.querySelector(".lkQ").value, metadata_id: +box.querySelector(".lkM").value, root: box.querySelector(".lkR").value });
const albumNote = (artist, inLidarr) => `Lidarr monitors and searches for this album only.${inLidarr ? "" : ` ${esc(artist)} isn't in Lidarr yet, so they're added with nothing else monitored.`}`;
$("drawer").addEventListener("click", async e => {
  const t = e.target.closest(".trk"); if (t) { playTrack(+t.dataset.i); return; }
  const at = e.target.closest(".atrk"); if (at) { playAlbumTrack(+at.dataset.ai); return; }
  const ab = e.target.closest("button[data-alb]"); if (ab) { showAlbum(+ab.dataset.alb); return; }
  if (e.target.closest("[data-alb-spotify]")) { const a = current.albums[+$("albBox").dataset.i]; viaSpotify("album", a.title, current.name); return; }
  if (e.target.closest("[data-alb-lidarr]")) {
    const box = document.querySelector("#albBox .albl");
    if (!box.hidden) { box.hidden = true; return; }
    albumLidarrForm(box, albumNote(current.name, current.in_lidarr)); return;
  }
  const add = e.target.closest("#albBox [data-lk-add]");
  if (add) {
    const a = current.albums[+$("albBox").dataset.i]; add.disabled = true;
    try {
      const r = await api("/api/lidarr/album", { method: "POST", body: JSON.stringify({ artist: current.name, title: a.title, ...albumLidarrChoice($("albBox")) }) });
      document.querySelector("#albBox .albl").innerHTML = `<p class="muted">✓ Sent to Lidarr. It's looking for ${esc(r.title || a.title)}.</p>`;
      toast(`Lidarr is looking for ${r.title || a.title}`);
    } catch (err) { toast(err.message); add.disabled = false; }
    return;
  }
  const nav = e.target.closest("[data-nav]"); if (nav) { openArtist(nav.dataset.nav); return; }
  if (e.target.closest("[data-close]")) { closeDrawer(); return; }
  if (e.target.closest("[data-close-nav]")) { closeDrawer(); return; }
  const o = e.target.closest("[data-open]"); if (o) { e.preventDefault(); openArtist(o.dataset.open, o.dataset.name); return; }
  const td = e.target.closest("[data-tagdrill]"); if (td) { e.preventDefault(); drill({ tags: td.dataset.tagdrill }); return; }
  if (e.target.closest("[data-save]")) { toggleSaved(current.key); return; }
  if (e.target.closest("[data-rate-open]")) { $("seenBox").classList.toggle("on"); return; }
  if (e.target.closest("[data-lidarr]")) { toggleLidarr(); return; }
  const a = e.target.closest("[data-act]");
  if (a) {
    const r = current, action = a.dataset.act;
    if (!(await act(r.key, action))) return;
    closeDrawer(); removeCards(r.key);
    toast(action === "known" ? `Got it, you already know ${r.name}` : `Not interested in ${r.name}`, { label: "Undo", fn: async () => { await api("/api/feedback/" + encodeURIComponent(r.key), { method: "DELETE" }); refreshView(); } });
  }
});
$("drawer").addEventListener("change", async e => {
  if (e.target.matches("[data-noseed]")) {
    try { await api("/api/noseed", { method: "POST", body: JSON.stringify({ key: current.key, name: current.name, on: e.target.checked }) }); } catch (err) { toast(err.message); return; }
    toast(e.target.checked ? "Got it. Your picks will update in a minute without them." : "They'll shape your picks again.");
  }
});

// Lidarr
let lidarrOpts = null;
async function toggleLidarr() {
  const box = $("lidarrBox");
  if (box.classList.contains("on")) { box.classList.remove("on"); return; }
  box.innerHTML = `<div class="shimmer" style="height:60px"></div>`; box.classList.add("on");
  try {
    if (!lidarrOpts) lidarrOpts = await api("/api/lidarr/options");
    const last = await api("/api/lidarr/last");
    const o = lidarrOpts, sel = (arr, id, v, lbl) => `<select id="${id}">${arr.map(x => `<option value="${esc(x.id ?? x.path)}" ${String(x.id ?? x.path) === String(v) ? "selected" : ""}>${esc(lbl(x))}</option>`).join("")}</select>`;
    box.innerHTML = `<div class="row">
        <label>Quality profile${sel(o.profiles, "lQ", last.quality_id, x => x.name)}</label>
        <label>Metadata profile${sel(o.metadata, "lM", last.metadata_id, x => x.name)}</label>
        <label>Root folder${sel(o.roots, "lR", last.root, x => x.path + (x.free ? ` (${(x.free / 1e12).toFixed(1)} TB free)` : ""))}</label>
        <label>Monitor<select id="lMon">${[["all", "All albums"], ["future", "Future albums"], ["missing", "Missing albums"], ["latest", "Latest album"], ["first", "First album"], ["none", "None"]]
          .map(([v, l]) => `<option value="${v}" ${v === (last.monitor || "all") ? "selected" : ""}>${l}</option>`).join("")}</select></label></div>
      <div class="row"><label class="inline"><input type="checkbox" id="lSearch" checked> Start searching right away</label>
        <button class="btn primary" style="margin-left:auto" id="lAdd">Add</button></div>`;
    $("lAdd").onclick = addLidarr;
  } catch (e) { box.innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}
async function addLidarr() {
  const r = current;
  try {
    $("lAdd").disabled = true;
    await api("/api/lidarr/add", { method: "POST", body: JSON.stringify({ key: r.key, name: r.name, mbid: r.mbid || null,
      quality_id: +$("lQ").value, metadata_id: +$("lM").value, root: $("lR").value, monitor: $("lMon").value, search: $("lSearch").checked }) });
    closeDrawer(); removeCards(r.key); toast(`Added ${r.name} to Lidarr`);
  } catch (e) { toast(e.message); const b = $("lAdd"); if (b) b.disabled = false; }
}

// ------------------------------------------------------------------ home
async function loadHome() {
  $("homeRows").innerHTML = `<div class="hrow"><div class="hrow-head"><h3 class="shimmer" style="width:220px;height:22px"></h3></div><div class="hscroll">${skeleton(8)}</div></div>`.repeat(3);
  let d; try { d = await api("/api/home"); } catch (e) { $("homeRows").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (!d.rows.length) { $("homeRows").innerHTML = `<div class="grid">${emptyState()}</div>`; return; }
  const g = d.digest || {}, plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const bits = [g.new_picks ? `<a href="#" data-see='{"fresh":1}'>${plural(g.new_picks, "new pick")}</a>` : "",
    g.releases ? `<a href="#/releases">${plural(g.releases, "new release")}</a>` : "",
    g.upcoming ? `<a href="#/releases">${g.upcoming} coming soon</a>` : ""].filter(Boolean);
  $("homeRows").innerHTML = (bits.length ? `<div class="digest"><b>This week</b> ${bits.join(" · ")}</div>` : "") + d.rows.map(row => `
    <div class="hrow">
      <div class="hrow-head"><h3>${esc(row.title)}</h3>${row.subtitle ? `<span class="muted">${esc(row.subtitle)}</span>` : ""}
        <a class="link see" href="#" data-see='${esc(JSON.stringify(row.see_all || {}))}'>See all ›</a></div>
      <div class="hwrap"><button class="scroll-btn l" aria-label="Scroll left">${ICON.left}</button>
        <div class="hscroll" data-list>${row.items.map(x => row.type === "albums" ? albumCard(x, { role: x.gap }) : card(x)).join("")}</div>
        <button class="scroll-btn r" aria-label="Scroll right">${ICON.right}</button></div>
    </div>`).join("");
}
$("homeRows").addEventListener("click", e => {
  const b = e.target.closest(".scroll-btn");
  if (b) { const sc = b.parentElement.querySelector(".hscroll"); sc.scrollBy({ left: (b.classList.contains("l") ? -1 : 1) * sc.clientWidth * 0.85, behavior: "smooth" }); return; }
  const a = e.target.closest("[data-see]");
  if (a) { e.preventDefault(); const s = JSON.parse(a.dataset.see); s.view ? go(s.view) : drill(s); }
});
function drill(s) {
  closeDrawer(); view = "picks"; loadF();
  const sort = F.sort; F = blankF(); F.sort = sort;
  if (s.tags) F.tags = { [s.tags]: 1 };
  if (s.seed) { F.seed = s.seed; F.seedLabel = s.seed_label || ""; }
  if (s.pop) F.pop = s.pop;
  if (s.fresh) F.fresh = 1;
  skipRoute = location.hash !== "#/picks"; location.hash = "#/picks"; showView("picks", true);
}

// ------------------------------------------------------------------ list views
let listSeq = 0;   // newest request for the shared list page; older ones are dropped when they come back
function listShell(title, sub, inner, tools = "") {
  $("listBody").innerHTML = `<div class="list-head"><div><h2>${title}</h2>${sub ? `<p class="muted">${sub}</p>` : ""}</div>${tools}</div>${inner}`;
}
const gridOf = (arr, opt = () => ({})) => `<div class="grid" data-list>${arr.map(r => card(r, opt(r))).join("")}</div>`;
async function loadSaved() {
  listShell("Saved", "", `<div class="grid">${skeleton(10)}</div>`);
  const my = ++listSeq; let d; try { d = await api("/api/saved"); } catch (e) { if (my === listSeq) listShell("Saved", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  d.items.forEach(r => SAVED.add(r.key));
  const want = d.items.filter(r => !r.owned), got = d.items.filter(r => r.owned);
  listShell("Saved for later", want.length ? `${want.length} artist${want.length === 1 ? "" : "s"}` : "",
    want.length ? gridOf(want) : `<div class="empty">Nothing saved yet. Tap ♡ on any artist to save it.</div>`);
  if (got.length) $("listBody").insertAdjacentHTML("beforeend", `<h3 class="list-sub" style="font:600 18px Georgia,serif;margin:30px 0 12px">Now in your library</h3>${gridOf(got)}`);
}
async function runSearch(q) {
  $("gq").value = q;
  if (!q) { listShell("Search", "Find any artist.", ""); return; }
  listShell(`Results for “${esc(q)}”`, "", `<div class="grid">${skeleton(10)}</div>`);
  const my = ++listSeq; let d; try { d = await api("/api/search?q=" + encodeURIComponent(q)); } catch (e) { if (my === listSeq) listShell("Search", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  listShell(`Results for “${esc(q)}”`, `${d.items.length} artists`, d.items.length ? gridOf(d.items, r => ({ noHide: true })) : `<div class="empty">No artists match.</div>`);
}
$("gsearch").onsubmit = e => { e.preventDefault(); const q = $("gq").value.trim(); if (q) location.hash = "#/search/" + encodeURIComponent(q); };
async function loadLike(key, name) {
  listShell("More like this", "Looking up similar artists…", `<div class="grid">${skeleton(14)}</div>`);
  const my = ++listSeq; let d; try { d = await api(`/api/like/${encodeURIComponent(key)}?name=${encodeURIComponent(name || "")}`); } catch (e) { if (my === listSeq) listShell("More like this", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  const hide = store.get("likeHide") !== "0";
  const list = d.items.filter(r => !hide || !(r.owned || r.known));
  listShell(`More like <a href="#" data-open-artist="${esc(key)}" style="color:inherit">${esc(d.name)}</a>`, "Ranked by how close they are, then by how well they fit your taste.",
    list.length ? gridOf(list, r => ({ badge: r.owned ? `<span class="match own">${ICON.check} Owned</span>` : `<span class="match">${Math.round(r.score * 100)}%</span>`, dim: r.known })) : `<div class="empty">Nothing new here.</div>`,
    `<label class="inline"><input type="checkbox" id="likeHide" ${hide ? "checked" : ""}> Hide artists I own or play</label>`);
  $("likeHide").onchange = e => { store.set("likeHide", e.target.checked ? "1" : "0"); loadLike(key, name); };
}
$("listBody").addEventListener("click", e => { const a = e.target.closest("[data-open-artist]"); if (a) { e.preventDefault(); navList = []; openArtist(a.dataset.openArtist); } });
async function loadGaps() {
  listShell("Albums you're missing", "", `<div class="grid">${skeleton(8)}</div>`);
  const my = ++listSeq; let d; try { d = await api("/api/gaps"); } catch (e) { if (my === listSeq) listShell("Albums you're missing", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  const what = ["albums", d.eps ? "EPs" : "", d.special ? "remixes, live albums, compilations and reissues" : ""].filter(Boolean);
  const opts = `<div class="lib-tools gap-opts"><label class="inline"><input type="checkbox" data-gap-opt="GAPS_EPS" ${d.eps ? "checked" : ""}> EPs</label>
    <label class="inline"><input type="checkbox" data-gap-opt="GAPS_SPECIAL" ${d.special ? "checked" : ""}> Remixes, live albums, compilations and reissues</label></div>`;
  listShell("Albums you're missing", `${what.length > 1 ? what.slice(0, -1).join(", ") + " and " + what.at(-1) : "Albums"} from artists you already collect, most popular first.`.replace(/^./, c => c.toUpperCase()),
    d.groups.length ? d.groups.map(g => `<div class="gap"><div class="gap-head" data-open-artist="${esc(g.key)}">
        <span class="gap-ph" style="background-image:url('${esc(g.picture || "")}')"></span>
        <div><h3>${esc(g.name)}</h3><div class="muted">You have ${g.owned} of ${g.total}</div><div class="meter"><i style="width:${Math.round(g.owned / g.total * 100)}%"></i></div></div></div>
        <div class="hscroll" data-list>${g.albums.map(a => albumCard(a)).join("")}</div></div>`).join("")
      : `<div class="empty">${d.at ? "Nothing missing. Your collection is complete." : "This fills in after your next full sync."}</div>`, opts);
  document.querySelectorAll("[data-gap-opt]").forEach(cb => cb.onchange = async () => {
    try { await api("/api/settings", { method: "PUT", body: JSON.stringify({ [cb.dataset.gapOpt]: cb.checked }) }); } catch (e) { toast(e.message); cb.checked = !cb.checked; return; }
    loadGaps();
  });
}
async function loadReleases() {
  listShell("New from artists you love", "", `<div class="grid">${skeleton(8)}</div>`);
  const my = ++listSeq; let d; try { d = await api("/api/releases"); } catch (e) { if (my === listSeq) listShell("New from artists you love", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  listShell("New from artists you love", "Albums and EPs from the last four months, plus anything announced.",
    d.items.length ? `<div class="grid" data-list>${d.items.map(a => albumCard(a, { role: upcoming(a.date) ? "" : fmtDate(a.date), noYear: true })).join("")}</div>` : `<div class="empty">${d.at ? "Nothing new yet." : "This fills in after your next full sync."}</div>`);
}

let libItems = [], libTotal = 0, libQ = "", libTimer = null;
// 25, 50, 100, 250, but never more than there are: 60 albums gives 25, 50 and "All 60"
const sizeChoices = total => [...[25, 50, 100, 250].filter(n => n < total), ...(total <= 250 ? [total] : [])].filter(n => n > 0);
async function loadLibrary(more, typing) {
  if (!more) { libItems = []; if (!typing) listShell("Your Library", "", `<div class="grid">${skeleton(10)}</div>`); }
  const my = ++listSeq; let d; try { d = await api(`/api/library?offset=${libItems.length}&limit=120&q=${encodeURIComponent(libQ)}`); } catch (e) { if (my === listSeq) listShell("Your Library", esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  libItems = libItems.concat(d.items); libTotal = d.total;
  const box = $("libQ"), typed = box ? box.value : libQ, focused = box && document.activeElement === box, size = $("libN") && $("libN").value;
  const all = d.all ?? libTotal, sizes = sizeChoices(all);
  const tools = `<div class="lib-tools"><input type="search" id="libQ" placeholder="Filter by album or artist" value="${esc(typed)}" aria-label="Filter albums">
    <label class="lk-sort">Playlist size <select id="libN" ${all ? "" : "disabled"}>${sizes.map(n => `<option value="${n}" ${n === Math.min(50, all) ? "selected" : ""}>${n === all ? `All ${n}` : n}</option>`).join("")}</select></label>
    <button class="btn primary" id="libPl" ${all ? "" : "disabled"} title="Takes the top albums and replaces the playlist made last time">Make Plex playlist</button></div>`;
  listShell("Your Library", all ? `${all.toLocaleString()} album${all === 1 ? "" : "s"} you own but have never played, best for you first.` : "",
    (libQ && all ? `<p class="count">${libTotal.toLocaleString()} match${libTotal === 1 ? "es" : ""} these filters</p>` : "") +
    (libItems.length ? `<div class="grid" data-list>${libItems.map(a => albumCard(a, { role: a.why })).join("")}</div>
      ${libItems.length < libTotal ? `<div style="text-align:center;margin:24px"><button class="btn" id="libMore">Show more</button></div>` : ""}`
      : `<div class="empty">${libQ ? "No unplayed albums match." : lastStatus.last_sync ? "You've played every album you own. Impressive." : "This fills in after your next full sync."}</div>`), tools);
  if ($("libMore")) $("libMore").onclick = () => loadLibrary(true);
  if (size && sizes.includes(+size)) $("libN").value = size;
  if (focused) { const q = $("libQ"); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  $("libQ").oninput = e => { clearTimeout(libTimer); libTimer = setTimeout(() => { libQ = e.target.value.trim(); loadLibrary(false, true); }, 250); };
  $("libPl").onclick = async () => {
    const n = +$("libN").value; $("libPl").disabled = true;
    try { const r = await api("/api/plex/playlist", { method: "POST", body: JSON.stringify({ count: n }) }); toast(`Made “${r.title}” in Plex with ${r.albums} albums`); }
    catch (e) { toast(e.message); }
    $("libPl").disabled = false;
  };
}

// ------------------------------------------------------------------ liked, not owned (Spotify Liked Songs that aren't in Plex)
let liked = null, likedShown = 0, likedObs = null, likedQ = "", likedTimer = null;
// Sort and group choices, remembered in this browser
let LK = { sort: "new", group: "none" };
try { LK = { ...LK, ...JSON.parse(store.get("likedView2") || "{}") }; } catch {}
const bare = s => (s || "").toLowerCase().replace(/^the\s+/, "");
const byNew = (a, b) => (b.added_at || "").localeCompare(a.added_at || "");
const LK_SORTS = {
  new: ["Newest like", byNew],
  old: ["Oldest like", (a, b) => (a.added_at || "").localeCompare(b.added_at || "")],
  artist: ["Artist A–Z", (a, b) => bare(a.artist).localeCompare(bare(b.artist)) || byNew(a, b)],
  album: ["Album A–Z", (a, b) => bare(a.title).localeCompare(bare(b.title)) || byNew(a, b)],
  songs: ["Most liked songs", (a, b) => (b.count ?? b.songs.length) - (a.count ?? a.songs.length) || byNew(a, b)],
  year: ["Newest release", (a, b) => (b.year || "").localeCompare(a.year || "") || byNew(a, b)],
};
function likedItems() {
  const q = likedQ.toLowerCase(), cmp = (LK_SORTS[LK.sort] || LK_SORTS.new)[1];
  const songsOf = list => list.flatMap(a => (a.tracks || a.songs.map(t => ({ title: t, added_at: a.added_at }))).map(t => ({
    ...t, a, album: a.title, title: t.title, artist: a.artist, year: a.year, count: a.songs.length, single: t.single })));
  if (LK.group === "none") {   // one row per song
    const songs = songsOf(liked.albums);
    const hit = songs.filter(s => !q || [s.title, s.album, s.artist].some(x => (x || "").toLowerCase().includes(q)));
    const scmp = LK.sort === "album" ? (x, y) => bare(x.album).localeCompare(bare(y.album)) || byNew(x, y) : cmp;
    return { count: hit.length, of: songs.length, unit: "song", items: hit.sort(scmp).map(s => ({ s })) };
  }
  const albums = liked.albums.filter(a => !q || [a.title, a.artist, ...a.songs].some(s => (s || "").toLowerCase().includes(q)));
  if (LK.group !== "artist") return { count: albums.length, items: albums.sort(cmp).map(a => ({ a })) };
  const groups = new Map();
  for (const a of albums) {
    if (!groups.has(a.artist_key)) groups.set(a.artist_key, { artist: a.artist, artist_key: a.artist_key, title: a.artist, albums: [], songs: [], added_at: "", year: "" });
    const g = groups.get(a.artist_key);
    g.albums.push(a); g.songs.push(...a.songs);
    if ((a.added_at || "") > g.added_at) g.added_at = a.added_at;
    if ((a.year || "") > g.year) g.year = a.year;
  }
  const outer = LK.sort === "album" ? LK_SORTS.artist[1] : cmp, inner = ["album", "year", "old"].includes(LK.sort) ? cmp : byNew;
  const songCmp = LK.sort === "album" ? (x, y) => bare(x.album).localeCompare(bare(y.album)) || byNew(x, y) : inner;
  const items = [...groups.values()].sort(outer).flatMap(g => [{ head: g }, ...songsOf(g.albums).sort(songCmp).map(s => ({ s }))]);
  return { count: items.filter(x => x.s).length, of: songsOf(liked.albums).length, unit: "song", items };
}
const likedHead = g => `<div class="lk-ahead" data-lk-head="${esc(g.artist_key)}" data-name="${esc(g.artist)}" tabindex="0" title="Open ${esc(g.artist)}">
  <b>${esc(g.artist)}</b><small>${g.songs.length} song${g.songs.length === 1 ? "" : "s"}${g.albums.length > 1 ? ` · ${g.albums.length} releases` : ""}</small></div>`;
const likedItem = x => x.head ? likedHead(x.head) : x.s ? likedSongRow(x.s, LK.group === "artist") : likedRow(x.a);
// what Lidarr gets asked for, the same on song and album rows
const lidarrWhat = a => ({ single: "single", ep: "EP" }[a.type] || "album");
const lidarrBtn = a => !lastStatus.lidarr ? "" : a.requested ? `<span class="pill on">✓ Sent to Lidarr</span>` : `<button class="btn primary" data-lk-lidarr>${ICON.plus}Add ${lidarrWhat(a)} to Lidarr</button>`;
const likedDay = iso => { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : ""; };
// Opens that exact song or album in the Spotify app. The link itself is the web player's address, so Ctrl-click,
// middle-click and "copy link" still work, and it's offered as a fallback when no app answers.
const spotifyLink = (kind, id) => /^[A-Za-z0-9]{22}$/.test(id || "") ? `<a class="btn lk-sp" href="https://open.spotify.com/${kind}/${id}" data-app="spotify:${kind}:${id}" target="_blank" rel="noopener"
  title="${kind === "track" ? "Plays this song in your Spotify app" : "Plays this release in your Spotify app"}">${kind === "track" ? `${ICON.play}Play on Spotify` : `${ICON.play}Play on Spotify`}</a>` : "";
document.addEventListener("click", async e => {
  const a = e.target.closest && e.target.closest("a.lk-sp[data-app]");
  if (!a || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  const [, kind, id] = a.dataset.app.split(":");
  if (lastStatus.spotify) {   // connected: start it playing in your Spotify app
    let r = null;
    try { r = await api("/api/spotify/play", { method: "POST", body: JSON.stringify({ kind, id, on: playOn() }) }); } catch {}
    if (r && r.ok) { toast(`Playing on ${r.device}`); return; }
    if (r && r.reason === "reconnect") toast("Reconnect Spotify once so this button can start playback. Opening the app for now.", { label: "Reconnect", fn: () => openSetting("spotify") });
    else if (r && r.reason === "no_device") toast("Opening Spotify. Press play there; while Spotify is open, this button starts songs by itself.");
    else if (r && r.message) toast(`${r.message}. Opening the app instead.`);
  }
  openSpotifyApp(a.dataset.app, a.href);
});
function openSpotifyApp(app, web) {
  let left = false;
  const gone = () => { left = true; };
  window.addEventListener("blur", gone, { once: true });
  document.addEventListener("visibilitychange", gone, { once: true });
  location.href = app;   // the installed app takes over (the browser may ask first)
  setTimeout(() => {
    window.removeEventListener("blur", gone); document.removeEventListener("visibilitychange", gone);
    if (!left) toast("Didn't open? The Spotify app may not be installed here.", { label: "Open in web player", fn: () => window.open(web, "_blank", "noopener") });
  }, 2500);
}
function likedSongRow(s, underArtist) {   // underArtist: the row sits under its artist's heading, so the name isn't repeated
  const a = s.a, same = (s.album || "").toLowerCase() === (s.title || "").toLowerCase();
  return `<div class="lk-row" data-lk="${esc(a.id)}" ${s.id ? `data-song="${esc(s.id)}"` : ""} tabindex="0" title="Open ${esc(a.artist || "")}">
    ${a.cover ? `<img class="lk-cover" loading="lazy" src="${esc(a.cover)}" alt="">` : `<div class="lk-cover"></div>`}
    <div class="lk-main"><div class="lk-t">${esc(s.title || "")}</div>
      <div class="lk-s">${underArtist ? "" : `<span class="lk-a">${esc(a.artist || "")}</span> · `}${a.type === "single" && same ? "Single" : `from <i class="lk-from">${esc(s.album || "")}</i>${a.type === "single" ? " (single)" : a.type === "ep" ? " (EP)" : ""}`}${a.year ? " · " + esc(a.year) : ""}${s.single ? ` <span class="lk-was" title="You liked the single “${esc(s.single)}”">· first out as a single</span>` : ""}</div>
      ${s.added_at ? `<div class="lk-when"><span class="lk-heart" aria-label="Liked">♥</span> ${esc(likedDay(s.added_at))}</div>` : ""}</div>
    <div class="lk-act">${spotifyLink("track", s.id)}${lidarrBtn(a)}
      <button class="btn" data-lk-hide>${s.id ? "Hide song" : "Hide album"}</button></div>
    <div class="lk-box" hidden></div></div>`;
}
const LIKED_PAGE = 60;
const streamSwitch = on => `<div class="seg stream-seg" role="tablist">${[["stream", "Artists"], ["liked", "Liked songs"]].map(([v, l]) =>
  `<button role="tab" data-go="${v}" aria-selected="${v === on}" class="${v === on ? "on" : ""}">${l}</button>`).join("")}</div>`;
document.addEventListener("click", e => { const b = e.target.closest("[data-go]"); if (b && !b.classList.contains("on")) go(b.dataset.go); });
const LIKED_TITLE = "Liked, not owned";
async function loadLiked() {
  listShell(LIKED_TITLE, "", `<div class="shimmer" style="height:300px;border-radius:12px"></div>`);
  const my = ++listSeq; let d; try { d = await api("/api/liked"); } catch (e) { if (my === listSeq) listShell(LIKED_TITLE, esc(e.message), ""); return; }
  if (my !== listSeq) return;   // you've moved on to another page since
  liked = d; renderLiked();
}
function likedRow(a) {
  return `<div class="lk-row" data-lk="${esc(a.id)}" tabindex="0" title="Open ${esc(a.artist || "")}">
    ${a.cover ? `<img class="lk-cover" loading="lazy" src="${esc(a.cover)}" alt="">` : `<div class="lk-cover"></div>`}
    <div class="lk-main"><div class="lk-kind">${{ single: "Single", ep: "EP", compilation: "Compilation" }[a.type] || "Album"}</div><div class="lk-t">${esc(a.title || "")}</div>
      <div class="lk-s"><span class="lk-a">${esc(a.artist || "")}</span>${a.year ? " · " + esc(a.year) : ""}${a.linked ? ` <span class="lk-was">· you liked singles from it</span>` : ""}</div>
      <div class="lk-songs"><span class="lk-heart" aria-label="Liked">♥</span> <span class="lk-lbl">You liked:</span> ${a.songs.map(esc).join(" · ")}</div></div>
    <div class="lk-act">${spotifyLink("album", a.id)}${lidarrBtn(a)}
      <button class="btn" data-lk-hide>Hide album</button></div>
    <div class="lk-box" hidden></div></div>`;
}
function renderLiked() {
  const d = liked;
  if (!d.ready) {
    listShell(LIKED_TITLE, "", (d.connected ? `<div class="empty">This fills in after your next sync, which reads your Liked Songs from Spotify and every song in Plex.</div>`
      : `<div class="empty">Connect your Spotify account to see the songs you've liked there that aren't in your Plex library.<br><br><button class="btn primary" id="lkConnect">Connect Spotify in Settings</button></div>`));
    if ($("lkConnect")) $("lkConnect").onclick = () => openSetting("spotify");
    return;
  }
  const missing = d.albums.reduce((n, a) => n + a.songs.length, 0), plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? "" : "s"}`;
  const sub = `${plural(missing, "song")} of the ${d.total.toLocaleString()} you've liked on Spotify ${missing === 1 ? "isn't" : "aren't"} in Plex.` +
    (d.hidden ? ` ${plural(d.hidden, "song")} hidden. <button class="link accent" id="lkUnhide">Show them again</button>` : "");
  const tools = `<div class="lib-tools"><button class="btn" id="lkPl" ${d.owned ? "" : "disabled"} title="Makes “${esc(d.playlist)}” in Plex, replacing the one made last time">Plex playlist of the ${d.owned.toLocaleString()} you own</button></div>`;
  const bar = `<div class="lk-bar">
      <input type="search" id="lkQ" placeholder="Filter by artist, album or song" value="${esc(likedQ)}" aria-label="Filter liked songs">
      <label class="lk-sort">Sort <select id="lkSort">${Object.entries(LK_SORTS).map(([k, [l]]) => `<option value="${k}" ${k === LK.sort ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <span class="lk-grp"><span class="lk-sort">Group</span><span class="seg" id="lkGroup" role="group" aria-label="Group by">${[["none", "None"], ["album", "Album"], ["artist", "Artist"]].map(([k, l]) =>
        `<button data-lk-group="${k}" class="${LK.group === k ? "on" : ""}" aria-pressed="${LK.group === k}">${l}</button>`).join("")}</span></span>
      <span class="muted" id="lkCount"></span></div>`;
  listShell(LIKED_TITLE, sub, d.albums.length ? `${bar}<div class="lk-list" id="lkList"></div><div id="lkEnd"></div>`
    : `<div class="empty">${d.total ? "You own every song you've liked on Spotify." : "No liked songs on Spotify yet."}</div>`, tools);
  if (d.albums.length) {
    renderLikedList();
    $("lkQ").oninput = e => { clearTimeout(likedTimer); likedTimer = setTimeout(() => { likedQ = e.target.value.trim(); renderLikedList(); }, 200); };
    $("lkSort").onchange = e => { LK.sort = e.target.value; store.set("likedView2", JSON.stringify(LK)); renderLikedList(); };
    $("lkGroup").onclick = e => {
      const b = e.target.closest("[data-lk-group]"); if (!b) return;
      LK.group = b.dataset.lkGroup; store.set("likedView2", JSON.stringify(LK));
      $("lkGroup").querySelectorAll("button").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
      renderLikedList();
    };
  }
  if ($("lkUnhide")) $("lkUnhide").onclick = async () => { try { await api("/api/liked/unhide-all", { method: "POST" }); } catch (e) { toast(e.message); return; } loadLiked(); };
  $("lkPl").onclick = async () => {
    $("lkPl").disabled = true;
    try { const r = await api("/api/plex/liked-playlist", { method: "POST" }); toast(`Made “${r.title}” in Plex with ${plural(r.songs, "song")}`); }
    catch (e) { toast(e.message); }
    $("lkPl").disabled = false;
  };
}
function renderLikedList() {
  const { count, of, unit, items } = likedItems(), list = $("lkList"); if (!list) return;
  likedShown = Math.min(items.length, LIKED_PAGE);
  list.innerHTML = items.length ? items.slice(0, likedShown).map(likedItem).join("") : `<div class="empty">Nothing matches “${esc(likedQ)}”.</div>`;
  $("lkCount").textContent = likedQ ? `${count.toLocaleString()} of ${(of ?? liked.albums.length).toLocaleString()} ${unit || "release"}s` : "";
  if (likedObs) likedObs.disconnect();
  if (items.length > likedShown) {   // more rows as you near the bottom
    likedObs = new IntersectionObserver(es => {
      if (!es[0].isIntersecting || view !== "liked" || !$("lkList")) return;
      $("lkList").insertAdjacentHTML("beforeend", items.slice(likedShown, likedShown + LIKED_PAGE).map(likedItem).join(""));
      likedShown += LIKED_PAGE;
      if (likedShown >= items.length) { likedObs.disconnect(); return; }
      // Look again: if the end is still in reach after adding rows, the browser won't report it a second time.
      likedObs.unobserve($("lkEnd")); likedObs.observe($("lkEnd"));
    }, { rootMargin: "1200px" });
    likedObs.observe($("lkEnd"));
  }
}
const likedHide = (id, on) => api("/api/liked/hide", { method: "POST", body: JSON.stringify({ id, on }) });
$("listBody").addEventListener("click", async e => {
  if (view !== "liked" || !liked) return;
  const head = e.target.closest("[data-lk-head]"); if (head) { openArtist(head.dataset.lkHead, head.dataset.name); return; }
  const row = e.target.closest("[data-lk]"); if (!row) return;
  const id = row.dataset.lk, a = liked.albums.find(x => x.id === id); if (!a) return;
  if (!e.target.closest("button,a,select,label,input,.lk-box")) { openArtist(a.artist_key, a.artist); return; }   // anywhere on the row but its buttons
  if (e.target.closest("[data-lk-hide]")) {
    const song = row.dataset.song, t = song && (a.tracks || []).find(x => x.id === song);   // a song row hides just that song
    try { await likedHide(t ? song : id, true); } catch (err) { toast(err.message); return; }
    if (t) {
      a.tracks = a.tracks.filter(x => x !== t); a.songs.splice(a.songs.indexOf(t.title), 1); liked.hidden += 1;
      if (!a.tracks.length) liked.albums = liked.albums.filter(x => x !== a);
    } else { liked.albums = liked.albums.filter(x => x !== a); liked.hidden += a.songs.length; }
    renderLiked();
    toast(t ? `Hid the song “${t.title}”` : `Hid the album “${a.title}”`, { label: "Undo", fn: async () => { try { await likedHide(t ? song : id, false); } catch (err) { toast(err.message); return; } loadLiked(); } });
    return;
  }
  if (e.target.closest("[data-lk-lidarr]")) {
    const box = row.querySelector(".lk-box");
    if (!box.hidden) { box.hidden = true; return; }
    albumLidarrForm(box, albumNote(a.artist, a.artist_in_lidarr));
    return;
  }
  const add = e.target.closest("[data-lk-add]");
  if (add) {
    add.disabled = true;
    try {
      const r = await api("/api/liked/lidarr", { method: "POST", body: JSON.stringify({ id, ...albumLidarrChoice(row) }) });
      a.requested = true; row.querySelector(".lk-box").hidden = true;
      row.querySelector("[data-lk-lidarr]")?.insertAdjacentHTML("beforebegin", lidarrBtn(a)); row.querySelector("[data-lk-lidarr]")?.remove(); toast(`Lidarr is looking for ${r.title || a.title}`);
    } catch (err) { toast(err.message); add.disabled = false; }
  }
});

// ------------------------------------------------------------------ rate
let rateQ = [], rateMode = "library", rateDone = 0, rateRemaining = 0, rateBusy = false;
const rateSkipKey = () => `rateSkip_${rateMode}`;
function rateSkips() { try { return JSON.parse(store.get(rateSkipKey()) || "[]"); } catch { return []; } }
const rateURL = extra => `/api/rate/queue?mode=${rateMode}&skip=${encodeURIComponent([...rateSkips().slice(-300), ...extra].join("\n"))}`;
async function loadRate(reset = true) {
  if (reset) { rateQ = []; rateDone = 0; }
  $("rateBody").innerHTML = rateShell(`<div class="shimmer" style="height:420px;border-radius:12px"></div>`);
  const mode = rateMode;
  let d; try { d = await api(rateURL([])); } catch (e) { $("rateBody").innerHTML = rateShell(`<div class="empty">${esc(e.message)}</div>`); return; }
  if (view !== "rate" || mode !== rateMode) return;   // you've moved on since
  rateQ = d.items; rateRemaining = d.remaining ?? rateQ.length;
  renderRate();
}
function rateShell(inner) {
  return `<div class="rate">
    <div class="rate-top">
      <div class="seg" id="rateSeg"><button data-mode="library" class="${rateMode === "library" ? "on" : ""}">Artists you play, not rated</button>
        <button data-mode="picks" class="${rateMode === "picks" ? "on" : ""}">Know any of your picks?</button></div>
      <span class="muted">${rateDone ? `${rateDone} rated this session` : ""}</span>
    </div>
    <p class="muted rate-help">${rateMode === "library"
      ? `Ratings are the strongest signal Recordhound has, and they're saved to the artist in Plex too. <span class="mouse">Click</span><span class="touch">Tap</span> the left half of a star for a half star. <span class="keys">Keys: <kbd>1</kbd>–<kbd>5</kbd> to rate (hold <kbd>Shift</kbd> for a half star: <kbd>Shift</kbd>+<kbd>4</kbd> is 3½), <kbd>S</kbd> to skip.</span>`
      : `Already know some of these? Rate them and they stop showing up as picks, and the rest get better. <span class="mouse">Click</span><span class="touch">Tap</span> the left half of a star for a half star. <span class="keys">Keys: <kbd>1</kbd>–<kbd>5</kbd> to rate (<kbd>Shift</kbd> for a half star), <kbd>N</kbd> never heard of them, <kbd>K</kbd> already know them, no rating, <kbd>X</kbd> not interested.</span>`}</p>
    ${inner}</div>`;
}
function rateMeta(r) {
  if (rateMode === "library") {
    const last = r.last_played ? `, last ${new Date(r.last_played * 1000).toLocaleDateString([], { month: "short", year: "numeric" })}` : "";
    return `${r.plays ? `Played ${r.plays.toLocaleString()} time${r.plays === 1 ? "" : "s"} in Plex${last}` : "In your Plex library"} · ${r.owned_albums} album${r.owned_albums === 1 ? "" : "s"}`;
  }
  const b = (r.because || []).slice(0, 3).map(x => x.name);
  return [b.length ? "Because you like " + b.join(", ") : "", r.listeners ? fmtN(r.listeners) + " listeners" : r.fans ? fmtN(r.fans) + " fans" : ""].filter(Boolean).join(" · ");
}
function renderRate() {
  const r = rateQ[0];
  if (!r) {
    $("rateBody").innerHTML = rateShell(`<div class="empty"><h3>${rateMode === "library" ? "All caught up" : "That's the lot for now"}</h3>
      ${rateDone ? `You rated ${rateDone} this session. Your picks update automatically in a minute.` : ""}</div>`);
    return;
  }
  const next = rateQ.slice(1, 5).map(x => `<div class="rq sq" style="background-image:url('${artURL(x)}')"></div>`).join("");
  $("rateBody").innerHTML = rateShell(`
    <div class="rate-card">
      <div class="rate-poster sq"><span>${initials(r.name)}</span><img src="${artURL(r)}" alt="" onerror="this.remove()"></div>
      <div class="rate-info">
        <h2>${esc(r.name)}</h2>
        <div class="meta">${esc(rateMeta(r))}</div>
        ${(r.tags || []).length ? `<div class="meta">${esc(r.tags.slice(0, 6).join(", "))}</div>` : ""}
        ${(r.covers || []).length ? `<div class="rate-covers">${r.covers.map(c => `<img src="${esc(c.cover)}" alt="" title="${esc(c.title || "")}" loading="lazy" onerror="this.remove()">`).join("")}</div>` : ""}
        <div class="rate-stars">${starsWidget(0, 'id="rateStars"')}</div>
        <div class="rate-actions">
          ${rateMode === "library" ? `<button class="btn" data-ra="skip">Skip <kbd>S</kbd></button>`
            : `<button class="btn" data-ra="skip">Never heard of them <kbd>N</kbd></button><button class="btn" data-ra="known" title="Already know them, no rating">Already know them <kbd>K</kbd></button><button class="btn" data-ra="hide">${ICON.hide}Not interested <kbd>X</kbd></button>`}
          <button class="btn" data-ra-open>More about them</button>
        </div>
        ${rateRemaining > 1 ? `<div class="rate-next"><span class="muted">Up next</span>${next}${rateMode === "library" ? `<span class="muted">${(rateRemaining - 1).toLocaleString()} more</span>` : ""}</div>` : ""}
      </div>
    </div>`);
  starHover($("rateStars"));
}
async function rateAct(act, value) {   // value: 1-10
  const r = rateQ[0]; if (!r || rateBusy) return;   // one at a time: a double press must not skip the next artist unrated
  rateBusy = true;
  try { await rateStep(r, act, value); } finally { rateBusy = false; }
}
async function rateStep(r, act, value) {
  try {
    if (act === "rate" || act === "known") await api("/api/rate", { method: "POST", body: JSON.stringify({ key: r.key, name: r.name, rating: act === "rate" ? value : null }) });
    else if (act === "hide") await api("/api/feedback", { method: "POST", body: JSON.stringify({ key: r.key, name: r.name, action: "dismissed" }) });
    else { const s = rateSkips(); s.push(r.key); store.set(rateSkipKey(), JSON.stringify(s.slice(-1000))); }
  } catch (e) { toast(e.message); return; }
  if (act === "rate" || act === "known") rateDone++;
  rateQ.shift(); rateRemaining = Math.max(0, rateRemaining - 1);
  if (rateQ.length < 3 && rateRemaining > rateQ.length) {
    try {
      const d = await api(rateURL(rateQ.map(x => x.key)));
      const have = new Set(rateQ.map(x => x.key)); rateQ.push(...d.items.filter(x => !have.has(x.key)));
    } catch (e) { toast(e.message); }
  }
  if (view === "rate") renderRate();
}
$("rateBody").addEventListener("click", e => {
  const sg = e.target.closest("#rateSeg [data-mode]"); if (sg) { rateMode = sg.dataset.mode; loadRate(); return; }
  const s = e.target.closest("#rateStars [data-star]"); if (s) { rateAct("rate", starValue(s, e)); return; }
  if (e.target.closest("[data-ra-open]") && rateQ[0]) { navList = []; openArtist(rateQ[0].key, rateQ[0].name); return; }
  const a = e.target.closest("[data-ra]"); if (a) rateAct(a.dataset.ra);
});
document.addEventListener("keydown", e => {
  if (view !== "rate" || e.target.matches?.("input,select,textarea") || $("drawer").classList.contains("on")) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;   // Ctrl+S, Cmd+N and friends are the browser's, not ours
  const digit = /^Digit([1-5])$/.exec(e.code);   // e.code, because Shift+4 types "$"
  const k = (e.key || "").toLowerCase();
  if (digit) rateAct("rate", +digit[1] * 2 - (e.shiftKey ? 1 : 0));
  else if (k === "s" || k === "n") rateAct("skip");
  else if (k === "k" && rateMode === "picks") rateAct("known");
  else if (k === "x" && rateMode === "picks") rateAct("hide");
});

// ------------------------------------------------------------------ history (hidden / known / liked / added)
const HIST = {
  dismissed: ["Not interested", "Hidden from your picks, and similar artists get nudged down. Undo brings one back."],
  known: ["Already know them", "They stop showing up as picks. The ones you rated here also shape your taste."],
  liked: ["Liked", "Artists you liked here. They shape your picks."],
  added: ["Sent to Lidarr", "Artists you added to Lidarr from Recordhound."],
};
let histTab = "dismissed", histItems = [];
async function loadHistory() {
  try { histItems = (await api("/api/feedback")).items; } catch (e) { $("hiddenGrid").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  renderHistory();
}
function renderHistory() {
  const counts = {}; histItems.forEach(x => counts[x.action] = (counts[x.action] || 0) + 1);
  $("pills").innerHTML = Object.entries(HIST).map(([k, [l]]) => `<button class="${histTab === k ? "on" : ""}" data-ht="${k}">${l} <small>${counts[k] || 0}</small></button>`).join("");
  $("hiddenNote").textContent = HIST[histTab][1];
  const q = $("histQ").value.trim().toLowerCase();
  const list = histItems.filter(x => x.action === histTab && (!q || (x.name || "").toLowerCase().includes(q)));
  $("hiddenGrid").innerHTML = list.length ? list.map(x => `<div class="hcard" data-key="${esc(x.key)}">
      <div class="poster" style="aspect-ratio:1;border-radius:50%;overflow:hidden;padding:0"><img loading="lazy" src="${artURL(x)}" alt="" onerror="this.remove()" style="width:100%;height:100%;object-fit:cover"></div>
      <div class="t">${esc(x.name)}</div><div class="s">${new Date(x.at * 1000).toLocaleDateString()}${x.rating ? ` · <span class="gold">${starText(x.rating)}</span>` : ""}</div>
      <button class="btn sm" data-undo="${esc(x.key)}">${histTab === "added" ? "Forget" : "Undo"}</button></div>`).join("") : `<div class="empty">Nothing here yet.</div>`;
}
$("pills").onclick = e => { const b = e.target.closest("[data-ht]"); if (b) { histTab = b.dataset.ht; renderHistory(); } };
$("histQ").oninput = () => renderHistory();
$("hiddenGrid").onclick = async e => {
  const u = e.target.closest("[data-undo]");
  if (u) {
    try { await api("/api/feedback/" + encodeURIComponent(u.dataset.undo), { method: "DELETE" }); toast("Restored. Your picks will update in a minute."); loadHistory(); } catch (err) { toast(err.message); }
    return;
  }
  const c = e.target.closest(".hcard"); if (c) { navList = []; openArtist(c.dataset.key, (histItems.find(x => x.key === c.dataset.key) || {}).name); }
};

// ---- settings layout helpers (same in Filmhound and Recordhound)
const openCards = new Set();   // which collapsible cards are open, so a re-render keeps them open
const pill = (on, onText = "Set up", offText = "Not set up") => `<span class="pill ${on ? "on" : ""}">${on ? "✓ " + onText : offText}</span>`;
const setSection = (id, title, body, sub = "") =>
  `<section class="setsec" id="set-${id}"><h2 class="sh">${title}</h2>${sub ? `<p class="secsub">${sub}</p>` : ""}${body}</section>`;
// A card that can be collapsed: shows its title, status and a one-line summary when closed.
const foldCard = (id, head, status, sub, body) => `<details class="scard fold" id="svc-${id}" data-fold="${id}" ${openCards.has(id) ? "open" : ""}>
    <summary><h3>${head}</h3>${status}<span class="sumsub">${sub}</span><span class="chev">›</span></summary>
    <p class="sub">${sub}</p>${body}</details>`;
const advanced = (id, body) => `<details class="adv" data-fold="${id}" ${openCards.has(id) ? "open" : ""}><summary>Advanced<span class="chev">›</span></summary>${body}</details>`;
// a redirect address that is this Recordhound's own /callback: Spotify comes straight back, nothing to paste
const spDirect = u => { try { const x = new URL(u); return x.origin === location.origin && x.pathname === new URL("./callback", location.href).pathname; } catch { return false; } };
const openFor = id => { openCards.add(id); const d = document.querySelector(`#settingsBody [data-fold="${id}"]`); if (d) d.open = true; };
let setSpy = null;
// the full header, even while it's tucked away on a phone (it comes back when you scroll up)
const headerOffset = () => (parseFloat(document.body.style.getPropertyValue("--hdr")) || document.querySelector("header")?.offsetHeight || 60) + 16;
function setLayout(sections) {   // sections: [[id, menu label, html], …]
  const nav = `<nav class="setnav" id="setnav">${sections.map(([id, label]) => `<button type="button" data-jump="${id}">${label}</button>`).join("")}
    <p class="setnav-note">Changes to fields are saved with the bar at the bottom. Buttons act right away.</p></nav>`;
  return `<div class="setwrap">${nav}<div class="setmain">${sections.map(s => s[2]).join("")}</div></div>`;
}
function watchSections() {   // highlight the menu item for the section in view
  const nav = $("setnav"); if (!nav) return;
  const mark = () => {
    if (!nav.isConnected || view !== "settings") return;
    const secs = [...document.querySelectorAll("#settingsBody .setsec")];
    let cur = secs[0];
    for (const s of secs) if (s.getBoundingClientRect().top <= headerOffset() + 40) cur = s;
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) cur = secs[secs.length - 1];   // at the bottom
    nav.querySelectorAll("[data-jump]").forEach(b => b.classList.toggle("on", cur && b.dataset.jump === cur.id.slice(4)));
  };
  if (!setSpy) { setSpy = () => requestAnimationFrame(() => setSpy.mark && setSpy.mark()); window.addEventListener("scroll", setSpy, { passive: true }); }
  setSpy.mark = mark;
  mark();
}
$("settingsBody").addEventListener("toggle", e => {
  const id = e.target.dataset && e.target.dataset.fold; if (!id) return;
  e.target.open ? openCards.add(id) : openCards.delete(id);
}, true);
$("settingsBody").addEventListener("click", e => {
  const j = e.target.closest("[data-jump]"); if (!j) return;
  const el = document.getElementById("set-" + j.dataset.jump); if (!el) return;
  window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - headerOffset(), behavior: "smooth" });
});

// ------------------------------------------------------------------ settings
let S = null, draft = {}, dirty = false, plexLibs = null;
const SVC = {
  plex: { title: "Plex", sub: "Your music library, play counts and star ratings (Plexamp counts too).", url: "PLEX_URL", key: "PLEX_TOKEN", keyLabel: "Token",
    urlHelp: "Usually <code>http://YOUR-SERVER-IP:32400</code>", keyHelp: "Plex Web → any track → ⋯ → Get Info → View XML. The token is the <code>X-Plex-Token=</code> value in the URL." },
  lastfm: { optional: true, title: "Last.fm", sub: "Optional. Without a key, similar artists and genre tags come from MusicBrainz, ListenBrainz and Deezer. A key switches artist data to Last.fm and lets Recordhound read your scrobbles, but from December 1, 2026 a key only works while its account has <b>Last.fm Pro</b>. Keys are made at last.fm/api/account/create.",
    key: "LASTFM_API_KEY", keyLabel: "API key", user: "LASTFM_USERNAME", userHelp: "Optional, and needs a key. Your scrobbles train your taste, and artists you scrobble a lot but don't own show up under <b>You Stream</b>. If you scrobble to ListenBrainz too, use that below instead: it's free." },
  listenbrainz: { optional: true, title: "ListenBrainz", sub: "Optional. Your listening history from ListenBrainz (free, open-source scrobbling). Your profile must be public. Listen on Apple Music, YouTube Music, Deezer or Tidal? ListenBrainz can collect those plays as you go: the <b>Web Scrobbler</b> browser extension sends what you play in a web player, and <b>multi-scrobbler</b> runs on your server.", user: "LISTENBRAINZ_USERNAME" },
  lidarr: { optional: true, title: "Lidarr", sub: "Optional. Add artists straight to Lidarr with your quality and metadata profiles.", url: "LIDARR_URL", key: "LIDARR_API_KEY", keyLabel: "API key",
    urlHelp: "e.g. <code>http://192.168.1.10:8686</code>", keyHelp: "Lidarr → Settings → General → API Key" },
};
const val = k => k in draft ? draft[k] : S.values[k];
const srcTag = k => S.sources[k] === "env" && !(k in draft) ? `<span class="src" title="Set in docker-compose. Saving here overrides it.">from compose</span>` : "";
// Saved keys never reach the browser: the server sends eight dots plus the key's last four characters. That
// placeholder is shown as plain text, so you can tell which key is saved. The eye button only reveals what you type.
const isMasked = v => typeof v === "string" && v.startsWith("••••");
const isSecret = (k, v) => /TOKEN|KEY|CLIENT_ID/.test(k) || isMasked(v);
const EYE = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>`;
function secretInput(attr, k, v, ph) {
  const m = isMasked(v);
  return `<span class="secret"><input type="${m ? "text" : "password"}" ${attr}="${k}" data-secret value="${esc(v)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">` +
    `<button type="button" class="eye" data-eye aria-label="Show what you typed" title="Show what you typed" aria-pressed="false" ${m || !v ? "hidden" : ""}>${EYE}</button></span>` +
    `<div class="help saved-note" ${m ? "" : "hidden"}>Saved. For safety the full key is never shown here; paste a new one to replace it.</div>`;
}
document.addEventListener("focusin", e => {   // typing into a saved key replaces it
  if (e.target.matches && e.target.matches("[data-secret]") && isMasked(e.target.value)) e.target.select();
});
document.addEventListener("input", e => {
  const el = e.target;
  if (!el.matches || !el.matches("[data-secret]")) return;
  const eye = el.nextElementSibling, note = el.parentElement.nextElementSibling, m = isMasked(el.value);
  if (!m && eye.getAttribute("aria-pressed") !== "true") el.type = "password";   // what you type stays hidden until you ask
  if (m) el.type = "text";
  eye.hidden = m || !el.value;
  if (note && note.classList.contains("saved-note")) note.hidden = !m;
});
document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest("[data-eye]");
  if (!b) return;
  const el = b.previousElementSibling, show = el.type === "password";
  el.type = show ? "text" : "password";
  b.setAttribute("aria-pressed", String(show));
  b.title = show ? "Hide" : "Show what you typed";
  b.setAttribute("aria-label", b.title);
  el.focus();
});
function input(k, type = "text", ph = "") {
  const v = val(k);
  if (isSecret(k, v)) return secretInput("data-k", k, v, ph);
  return `<input type="${type}" data-k="${k}" value="${esc(v)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">`;
}
function ytCard() {
  const n = S.stats.history.youtube_music, at = S.stats.youtube_music_at;
  return foldCard("youtube", `<img class="svc" src="${fav("music.youtube.com")}" alt=""> YouTube Music`, n ? pill(true, "History uploaded") : pill(false),
    "Upload your listening history from Google Takeout.", `
    <p class="sub">Go to <a href="https://takeout.google.com" target="_blank" rel="noopener">takeout.google.com</a>, click <b>Deselect all</b>, then tick only
      <b>YouTube and YouTube Music</b>. Under <b>All YouTube data included</b>, keep just <b>history</b>. Google emails you a zip, usually within a few hours.</p>
    <div class="filerow"><label class="btn">Upload Takeout zip<input type="file" id="ytFile" accept=".zip,.json,.html" hidden></label>
      ${n ? `<button class="btn warn" id="ytRemove">Remove</button>` : ""}
      <span class="muted">${n ? `${n.toLocaleString()} artist${n === 1 ? "" : "s"} imported${at ? " " + new Date(at * 1000).toLocaleDateString() : ""}` : ""}</span></div>
    <p class="help">Only plays in YouTube Music count, not regular YouTube videos. Takeout doesn't say how long you listened, so every play counts as one.
      A newer upload replaces the last one. To keep your plays counting as you go, use ListenBrainz above.</p>`);
}
// What "Don't learn from" leaves out right now, in one line
function nsSummary() {
  const out = [...(val("NOSEED_LIBRARIES") || []).map(l => `the ${l} library`), ...(val("COMP_TASTE") ? [] : ["soundtracks & compilations"]), ...(val("NOSEED_TAGS") || [])];
  return out.length ? `<b>Left out:</b> ${out.map(esc).join(", ")}` : "Nothing is left out yet.";
}
function svcCard(id, status) {
  const c = SVC[id];
  const set = !!((!c.url || val(c.url)) && (c.key ? val(c.key) : val(c.user)));
  const head = `<img class="svc" src="${fav({ plex: "plex.tv", lastfm: "last.fm", listenbrainz: "listenbrainz.org", lidarr: "lidarr.audio" }[id])}" alt=""> ${c.title}`;
  const body = `${c.url ? `<div class="field"><label>URL ${srcTag(c.url)}</label>${input(c.url, "text", "http://")}${c.urlHelp ? `<div class="help">${c.urlHelp}</div>` : ""}</div>` : ""}
    ${c.key ? `<div class="field"><label>${c.keyLabel} ${srcTag(c.key)}</label>${input(c.key)}${c.keyHelp ? `<div class="help">${c.keyHelp}</div>` : ""}</div>` : ""}
    ${c.user ? `<div class="field"><label>Username ${srcTag(c.user)}</label>${input(c.user, "text", "your username")}${c.userHelp ? `<div class="help">${c.userHelp}</div>` : ""}</div>` : ""}
    <div class="testrow"><button class="btn" data-test="${id}">Test connection</button><span class="tres" id="tres-${id}"></span></div>
    ${id === "plex" ? `<div id="plexLibs"></div>` : ""}`;
  // Every connection folds the same way. A card opens by itself when it needs attention:
  // a required one that isn't set up, or any whose last test failed.
  if (!c.optional && !set) openCards.add(id);
  return foldCard(id, head, status || (c.optional ? pill(set) : pill(set, "Set up", "Needed")), c.sub, body);
}
function rangeField(k, label, min, max, step, help, fmt = v => v) {
  return `<div class="field"><label>${label} ${srcTag(k)}</label>
    <div class="rangeval"><input type="range" data-k="${k}" data-fmt="${fmt === pct ? "pct" : fmt === fmtN ? "n" : ""}" min="${min}" max="${max}" step="${step}" value="${val(k)}"><output>${fmt(val(k))}</output></div>
    ${help ? `<div class="help">${help}</div>` : ""}</div>`;
}
const pct = v => Math.round(v * 100) + "%";
const NOSEED_TAG_LIST = ["children's music", "christmas", "soundtrack", "classical", "comedy", "musicals", "lullaby", "disney", "kids", "holiday"];
async function loadSettings() {
  try { S = await api("/api/settings"); } catch (e) { $("settingsBody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  draft = {}; setDirty(false); renderSettings();
  if (S.values.PLEX_URL && S.values.PLEX_TOKEN && !plexLibs) testSvc("plex", true);
}
function setDirty(d) { dirty = d; $("savebar").classList.toggle("on", d); }
function renderSettings() {
  const st = S.stats, when = t => t ? new Date(t * 1000).toLocaleString() : "never";
  const hours = [`<option value="-1">Off</option>`].concat([...Array(24).keys()].map(h => `<option value="${h}" ${+val("SYNC_HOUR") === h ? "selected" : ""}>${new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: "numeric" })}</option>`)).join("");
  const spConnected = !!S.values.SPOTIFY_REFRESH_TOKEN;
  const spHelper = S.spotify_redirect === S.spotify_helper, spPaste = !spHelper && !spDirect(S.spotify_redirect);
  const spPasteBox = `<div class="filerow" id="spPasteBox" style="margin-top:6px" ${spPaste ? "" : "hidden"}><input type="text" id="spPaste" aria-label="The address Spotify sent you to" placeholder="${esc(S.spotify_redirect)}?code=…" style="flex:1"><button class="btn primary" id="spFinish">Finish</button></div>`;
  const spOld = S.spotify_redirect === S.spotify_loopback ? `<p class="help">Connecting can now be one click. First add the Redirect URI <code>${esc(S.spotify_helper)}</code> in your Spotify app, then switch. Until then it works the copy-and-paste way.</p>
    <div class="filerow"><button class="btn" id="spSwitch">Switch to one-click sign-in</button></div>` : "";
  const spAge = S.values.SPOTIFY_CONNECTED_AT ? Math.floor((Date.now() / 1000 - S.values.SPOTIFY_CONNECTED_AT) / 86400) : null;
  const lfKeyDead = !!val("LASTFM_API_KEY") && lastStatus.artist_source === "open" && S.values.LASTFM_API_KEY === val("LASTFM_API_KEY");
  const spStatus = spConnected ? pill(true, "Connected") : S.stats.spotify_export_old ? `<span class="pill warn">Upload again</span>`
    : S.stats.history.spotify_export ? pill(true, "History uploaded") : pill(false);
  const sections = [
    ["connections", "Connections", setSection("connections", "Connections",
      `${svcCard("plex")}${svcCard("lastfm", lfKeyDead ? `<span class="pill warn">Key not working</span>` : "")}${svcCard("lidarr")}`)],
    ["history", "Listening history", setSection("history", "Listening history", `
      ${svcCard("listenbrainz")}
      ${foldCard("spotify", `<img class="svc" src="${fav("spotify.com")}" alt=""> Spotify`, spStatus, "Two ways in: upload your history file, or connect your account. Use either or both.", `
      <h4>1 · Upload your listening history</h4>
      <p class="sub">The easiest option, and it needs no developer account. Request your data at <a href="https://www.spotify.com/account/privacy/" target="_blank" rel="noopener">spotify.com/account/privacy</a>
        (the <b>Extended streaming history</b> covers your whole account; the basic one covers the last year). Spotify emails a zip in a few days.</p>
      <div class="filerow"><label class="btn">Upload Spotify zip<input type="file" id="spFile" accept=".zip" hidden></label>
        <span class="muted">${S.stats.history.spotify_export ? `${S.stats.history.spotify_export.toLocaleString()} artists imported ${S.stats.spotify_export_at ? new Date(S.stats.spotify_export_at * 1000).toLocaleDateString() : ""}` : ""}</span></div>
      ${S.stats.spotify_export_old ? `<p class="help">This file was imported by an older version that counted every play of 30 seconds or more. Upload it again and Recordhound counts how long you actually listened, so quick skips count for less.</p>` : ""}
      <h4>2 · Connect your account</h4>
      <p class="sub">Keeps your top artists, followed artists and saved albums up to date on every sync. Spotify only allows this through a personal developer app, which needs Spotify Premium and works for up to 5 people.</p>
      ${spConnected ? `<p>✓ Connected${spAge != null ? ` ${spAge} day${spAge === 1 ? "" : "s"} ago` : ""}. ${S.stats.history.spotify ? `${S.stats.history.spotify} artists.` : ""}
          ${S.spotify_can_play === false ? `<b>Reconnect once so play buttons can start songs in your Spotify app.</b>` : ""}</p>
        <div class="opt"><label class="inline"><input type="checkbox" data-k="SPOTIFY_PLAY" ${val("SPOTIFY_PLAY") ? "checked" : ""}> Play buttons play the full song in Spotify ${srcTag("SPOTIFY_PLAY")}</label>
          <p class="help">On the Spotify app you're using, or your computer's. Songs that aren't on Spotify, or times no Spotify app is open, get the 30-second preview. Turn it off to always hear previews.</p></div><p>
          ${spAge != null && spAge > 160 ? `<b>Spotify ends connections after six months, so reconnect soon.</b>` : ""}</p>
        <div class="filerow"><button class="btn" data-test="spotify">Test connection</button><button class="btn" id="spReconnect">Reconnect</button><button class="btn warn" id="spDisconnect">Disconnect</button><span class="tres" id="tres-spotify"></span></div>
        ${spPaste ? "" : `<p class="help"><button class="link accent" id="spPasteShow">Didn't come back after reconnecting? Paste the address instead</button></p>${spPasteBox}`}${spOld}`
      : `<ol class="spotify-steps">
          <li>Go to <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">developer.spotify.com/dashboard</a> and create an app (any name). Tick <b>Web API</b>.</li>
          <li>Add this Redirect URI exactly, and save: <code>${esc(S.spotify_redirect)}</code></li>
          <li>Paste its Client ID here and click <b>Save changes</b>: <div class="field" style="margin:6px 0;display:block">${input("SPOTIFY_CLIENT_ID", "text", "Client ID")}</div></li>
          <li>Click <button class="btn" id="spConnect">Connect Spotify</button> and approve.
            ${spPaste ? `Spotify then sends you to a page that won't load. <b>That's expected.</b> Copy the whole address from your browser's address bar and paste it here:`
              : `You'll come straight back here. <button class="link accent" id="spPasteShow">Didn't come back? Paste the address instead</button>`}
            ${spPasteBox}</li>
        </ol>
        ${S.spotify_error ? `<p class="help bad">Last try: ${esc(S.spotify_error)}</p>` : ""}${spOld}
        ${spHelper ? `<p class="help">That Redirect URI is a small page that sends you from Spotify back to this Recordhound. It runs in your browser and never sees your Spotify data. <a href="https://github.com/ShadeRF/recordhound-auth" target="_blank" rel="noopener">How it works</a></p>` : ""}`}
      ${advanced("spotify-adv", `<div class="field"><label>Redirect address</label>${input("SPOTIFY_REDIRECT_URI", "text", S.spotify_helper)}
        <div class="help">Leave empty for one-click sign-in through the helper page. If Recordhound is on HTTPS, its own address ending in <code>/callback</code> works too, with no helper page. <code>${esc(S.spotify_loopback)}</code> is the old copy-and-paste way. Whatever is here must match your Spotify app's Redirect URIs exactly.</div></div>`)}
`)}
      ${ytCard()}`,
      "Everything here is optional. Listening outside Plex trains your taste, and artists you play a lot but don't own show up under <b>You Stream</b>.")],
    ["taste", "Your taste", setSection("taste", "Your taste", `<div class="scard">
      ${rangeField("OWNED_WEIGHT", "Artists you own but rarely play", 0, 1, 0.05, "How much your collection shapes your taste even when you haven't played it. Keep it high if your library is curated.", pct)}
      ${rangeField("RECENT_WEIGHT", "What you've played lately", 0, 1.5, 0.1, "Extra pull for artists you've played in the last six months (Plex, plus ListenBrainz if connected). Raise it to follow what you're into now; 0 treats all listening the same.", pct)}
      ${rangeField("MIN_PLAYS", "Plays before outside listening counts", 1, 20, 1, "Artists you've played fewer times than this in your Spotify history file, YouTube Music, ListenBrainz or Last.fm don't shape your picks or show up under You Stream. Plays in Plex and your connected Spotify account always count.")}
      <h4>Don't learn from</h4>
      <p class="sub">Tick anything that shouldn't shape your picks. It stays in your library.</p>
      <p class="ns-sum" id="nsSum">${nsSummary()}</p>
      <div class="field"><label>Libraries to leave out</label><div id="noseedLibs" class="chips pick"><span class="muted">Sync once to list your libraries.</span></div></div>
      <div class="field"><label>Albums to leave out</label><div><div class="chips pick"><button type="button" data-nocomp class="${val("COMP_TASTE") ? "" : "on"}">Soundtracks &amp; compilations</button></div>
        <div class="help">Albums credited to Various Artists, Original Soundtrack and the like. Normally a song you play from one counts for the artist who sang it.</div></div></div>
      <div class="field"><label>Genres to leave out</label><div><div class="chips pick">${NOSEED_TAG_LIST.map(t => `<button type="button" data-nst="${esc(t)}" class="${(val("NOSEED_TAGS") || []).includes(t) ? "on" : ""}">${esc(t)}</button>`).join("")}</div>
        <div class="help">Artists tagged with these, wherever their albums are. "soundtrack" covers score composers like John Williams and Hans Zimmer.</div></div></div>
      <div class="field"><label>Artists left out</label><div id="noseedArtists"><span class="muted">None. Open any artist you own and tick <b>Don't learn from this artist</b>.</span></div></div>
    </div>`, "Star ratings count most, then how much you play an artist anywhere, then what you own. These settings adjust the rest.")],
    ["picks", "Picks", setSection("picks", "Picks", `<div class="scard">
      ${lastStatus.artist_source === "lastfm"
        ? rangeField("MIN_LISTENERS", "Minimum Last.fm listeners", 0, 50000, 500, "Skip artists with fewer listeners than this. Lower it for more obscure picks.", fmtN)
        : rangeField("MIN_FANS", "Minimum Deezer fans", 0, 10000, 50, "Skip artists with fewer Deezer fans than this. Lower it for more obscure picks. Artists Deezer doesn't know are never skipped for this.", fmtN)}
      ${advanced("picks-adv", `
        ${rangeField("SEED_ARTISTS", "Your artists to branch out from", 25, 500, 25, "Your strongest artists by plays and ratings. More = broader picks, slower syncs.")}
        ${rangeField("CANDIDATE_POOL", "Candidates to score", 200, 5000, 100, "Each new candidate costs a few lookups the first time (then it's cached for a month).")}`)}
    </div>`)],
    ["plex", "Plex", setSection("plex", "Plex", `<div class="scard">
      <h4>Your Library</h4>
      <div class="opt"><label class="inline"><input type="checkbox" data-k="COMP_LIBRARY" ${val("COMP_LIBRARY") ? "checked" : ""}> Include soundtracks and compilations ${srcTag("COMP_LIBRARY")}</label>
        <p class="help">Your Library lists albums you own but have never played. Untick this to leave out Various Artists albums and soundtracks.</p></div>
      <div class="field"><label>Playlist name ${srcTag("PLEX_PLAYLIST")}</label>${input("PLEX_PLAYLIST")}
        <div class="help">Used by <b>Make Plex playlist</b> on the Your Library page. Recordhound replaces this playlist each time.</div></div>
    </div>`, "Your Plex server connection and libraries are under Connections.")],
    ["sync", "Sync &amp; data", setSection("sync", "Sync &amp; data", `<div class="scard">
      <div class="field"><label>Nightly sync ${srcTag("SYNC_HOUR")}</label><select data-k="SYNC_HOUR">${hours}</select>
        <div class="help">Re-reads Plex and your listening history and refreshes your picks once a day.</div></div>
      <div class="stats">
        <div class="stat"><b>${st.artists.toLocaleString()}</b><span>artists in Plex</span></div>
        ${Object.entries(st.history).map(([s, n]) => `<div class="stat"><b>${n.toLocaleString()}</b><span>from ${esc((SRC[s] || [, s])[1])}</span></div>`).join("")}
        <div class="stat"><b>${st.cached.toLocaleString()}</b><span>cached lookups</span></div>
        <div class="stat"><b>${(st.feedback || 0).toLocaleString()}</b><span>artists with feedback</span></div>
        <div class="stat"><b>${(st.db_bytes / 1e6).toFixed(0)} MB</b><span>database</span></div>
      </div>
      <p class="sub">Last full sync: ${when(st.last_sync)}</p>
      <div class="danger"><button class="btn" id="setSync">Sync now</button><button class="btn warn" data-maint="clear-cache">Clear lookup cache</button>
        <button class="btn warn" data-maint="clear-feedback">Reset all feedback</button><span class="muted" id="fbCount" style="align-self:center">${(st.feedback || 0).toLocaleString()} artist${st.feedback === 1 ? "" : "s"}</span></div>
    </div>`)],
    ["signin", "Sign-in", setSection("signin", "Sign-in", `<div class="scard">
      ${lastStatus.auth ? `<p class="sub">Sign-in is on for <b>${esc(lastStatus.user)}</b>.</p>
        <div class="filerow"><button class="btn" id="authChange">Change password</button><button class="btn warn" id="authOff">Turn off sign-in</button><button class="btn" id="logout">Sign out</button></div>`
        : `<p class="sub">Off. Anyone who can reach this page can use it, including Settings. Turn it on if Recordhound is reachable beyond your own devices.</p>`}
      <div id="authForm" class="authform" ${lastStatus.auth ? "hidden" : ""}>
        <div class="field"><label>Username</label><input type="text" id="authUser" value="${esc(lastStatus.user || "")}" autocomplete="username"></div>
        <div class="field"><label>Password</label><input type="password" id="authPass" autocomplete="new-password" placeholder="at least 8 characters"></div>
        <div class="field"><label></label><div><button class="btn primary" id="authSave">${lastStatus.auth ? "Save password" : "Turn on sign-in"}</button></div></div>
      </div>
    </div>`)],
  ];
  $("settingsBody").innerHTML = `
    ${S.configured ? "" : `<div class="setup-banner"><h3>Welcome to Recordhound</h3>Connect Plex to get started. Everything else is optional. Test each one, then save. Prefer a step-by-step? <a class="link accent" href="#/setup">Use the setup walkthrough</a>.</div>`}
    ${Object.values(S.sources).includes("env") ? `<div class="setup-banner env"><b>Some settings come from your docker-compose file.</b> Move them into Recordhound so you can manage everything here.
      <div style="margin-top:10px"><button class="btn primary" id="adoptEnv">Move them into Recordhound</button></div></div>` : ""}
    ${setLayout(sections)}
    <p class="muted" style="text-align:center;font-size:12px;margin-top:24px">Recordhound ${esc(lastStatus.version || "")} · Artist data from ${lastStatus.artist_source === "lastfm" ? "Last.fm" : "MusicBrainz, ListenBrainz and Deezer"} · Art and previews from Deezer · Lidarr for downloads</p>`;
  watchSections();
  focusPendingCard();
  loadNoseed();
  if (plexLibs) renderLibs();
}
function renderLibs() {
  const chosen = val("PLEX_LIBRARIES") || [], all = !chosen.length;
  $("plexLibs").innerHTML = `<div class="libs">${plexLibs.map(l => `<label><input type="checkbox" data-lib="${esc(l.title)}" ${all || chosen.includes(l.title) ? "checked" : ""}> ${esc(l.title)}</label>`).join("")}</div>`;
}
async function loadNoseed() {
  try {
    const [secs, ns] = await Promise.all([api("/api/plex/sections"), api("/api/noseed")]);
    const chosen = val("NOSEED_LIBRARIES") || [];
    if (secs.sections.length) $("noseedLibs").innerHTML = secs.sections.map(x => `<button type="button" data-nsl="${esc(x.title)}" class="${chosen.includes(x.title) ? "on" : ""}">${esc(x.title)}</button>`).join("");
    if (ns.items.length) $("noseedArtists").innerHTML = `<div class="chips">${ns.items.map(x => `<span class="rm">${esc(x.name)}<button type="button" data-unseed="${esc(x.key)}" title="Learn from them again">×</button></span>`).join("")}</div>`;
  } catch {}
}
$("settingsBody").addEventListener("input", e => {
  const el = e.target, k = el.dataset.k;
  if (k) {
    draft[k] = el.type === "checkbox" ? el.checked : el.type === "range" ? +el.value : el.value;
    if (el.type === "range") el.nextElementSibling.textContent = el.dataset.fmt === "pct" ? pct(el.value) : el.dataset.fmt === "n" ? fmtN(+el.value) : el.value;
    setDirty(true);
  }
  if (el.dataset.lib !== undefined) {
    const boxes = [...$("plexLibs").querySelectorAll("[data-lib]")], on = boxes.filter(b => b.checked).map(b => b.dataset.lib);
    draft.PLEX_LIBRARIES = on.length === boxes.length ? [] : on; setDirty(true);
  }
});
$("settingsBody").addEventListener("change", async e => {
  if (e.target.id === "spFile" && e.target.files[0]) {
    toast("Reading your Spotify history…");
    try {
      const r = await fetch("/api/import/spotify-export", { method: "POST", body: e.target.files[0] });
      const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || r.statusText || "The upload failed");
      toast(`Imported ${d.plays.toLocaleString()} plays across ${d.artists.toLocaleString()} artists. Picks update in a moment.`); loadSettings();
    } catch (err) { toast(err.message); }
    e.target.value = "";   // so picking the same file again after a failure works
  }
  if (e.target.id === "ytFile" && e.target.files[0]) {
    toast("Reading your YouTube Music history…");
    try {
      const r = await fetch("/api/import/youtube-music", { method: "POST", body: e.target.files[0] });
      const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || r.statusText || "The upload failed");
      openCards.add("youtube");
      toast(`Imported ${d.plays.toLocaleString()} plays across ${d.artists.toLocaleString()} artists. Picks update in a moment.`); loadSettings();
    } catch (err) { toast(err.message); }
    e.target.value = "";
  }
});
$("settingsBody").addEventListener("click", async e => {
  const id = e.target.id;
  const t = e.target.closest("[data-test]"); if (t) { testSvc(t.dataset.test); return; }
  const nc = e.target.closest("[data-nocomp]");
  if (nc) { draft.COMP_TASTE = !!nc.classList.contains("on"); nc.classList.toggle("on", !draft.COMP_TASTE); setDirty(true); $("nsSum").innerHTML = nsSummary(); return; }
  const ns = e.target.closest("[data-nst],[data-nsl]");
  if (ns) {
    const k = ns.dataset.nst ? "NOSEED_TAGS" : "NOSEED_LIBRARIES", v = ns.dataset.nst || ns.dataset.nsl;
    const cur = new Set(val(k) || []); cur.has(v) ? cur.delete(v) : cur.add(v); draft[k] = [...cur]; ns.classList.toggle("on", cur.has(v)); setDirty(true); $("nsSum").innerHTML = nsSummary(); return;
  }
  const un = e.target.closest("[data-unseed]");
  if (un) {
    try { await api("/api/noseed", { method: "POST", body: JSON.stringify({ key: un.dataset.unseed, on: false }) }); loadNoseed(); toast("They'll shape your picks again."); } catch (err) { toast(err.message); }
    return;
  }
  if (id === "adoptEnv") { try { S = await api("/api/settings/adopt-env", { method: "POST" }); renderSettings(); toast("Moved into Recordhound"); } catch (err) { toast(err.message); } return; }
  if (id === "spConnect" || id === "spReconnect") {
    if (dirty) { toast("Save your changes first"); return; }
    try {
      const d = await api("/api/spotify/start", { method: "POST", body: JSON.stringify({ back: new URL("./", location.href).href }) });
      if (d.helper || spDirect(d.redirect)) { location.href = d.url; return; }   // Spotify brings you straight back
      window.open(d.url, "_blank", "noopener");
      if (id === "spReconnect") { S.values.SPOTIFY_REFRESH_TOKEN = ""; renderSettings(); }
    } catch (err) { toast(err.message); }
    return;
  }
  if (id === "spSwitch") {   // older setups: drop the copy-and-paste address so Connect uses the helper page
    if (dirty) { toast("Save your changes first"); return; }
    try { S = await api("/api/settings", { method: "PUT", body: JSON.stringify({ SPOTIFY_REDIRECT_URI: "" }) }); renderSettings(); toast("Switched. Reconnect to sign in the new way."); }
    catch (err) { toast(err.message); }
    return;
  }
  if (id === "spPasteShow") { $("spPasteBox").hidden = false; $("spPaste").focus(); return; }
  if (id === "spFinish") {
    try { const d = await api("/api/spotify/finish", { method: "POST", body: JSON.stringify({ url: $("spPaste").value }) });
      toast(`Spotify connected · ${d.artists} artists`); loadSettings(); } catch (err) { toast(err.message); }
    return;
  }
  if (id === "ytRemove") { try { await api("/api/import/youtube-music", { method: "DELETE" }); loadSettings(); toast("YouTube Music history removed"); } catch (err) { toast(err.message); } return; }
  if (id === "spDisconnect") { try { await api("/api/spotify/disconnect", { method: "POST" }); loadSettings(); toast("Spotify disconnected"); } catch (err) { toast(err.message); } return; }
  if (id === "setSync") { try { await api("/api/sync", { method: "POST" }); wasRunning = userSync = true; poll(); toast("Sync started"); } catch (err) { toast(err.message); } return; }
  if (id === "authChange") { $("authForm").hidden = false; $("authPass").focus(); return; }
  if (id === "authSave") { try { await api("/api/auth", { method: "POST", body: JSON.stringify({ username: $("authUser").value, password: $("authPass").value }) }); lastStatus.auth = true; lastStatus.user = $("authUser").value.trim(); renderSettings(); toast("Sign-in is on"); } catch (err) { toast(err.message); } return; }
  if (id === "authOff") {
    if (!e.target.classList.contains("armed")) { e.target.classList.add("armed"); e.target.textContent = "Click again to confirm"; return; }
    try { await api("/api/auth", { method: "POST", body: JSON.stringify({ enabled: false }) }); lastStatus.auth = false; renderSettings(); toast("Sign-in is off"); } catch (err) { toast(err.message); }
    return;
  }
  if (id === "logout") { try { await api("/api/logout", { method: "POST" }); } catch (err) { toast(err.message); return; } location.href = "/login"; return; }
  const m = e.target.closest("[data-maint]");
  if (m) {
    if (m.dataset.maint === "clear-feedback") { if (!window.confirm(RESET_FEEDBACK)) return; }
    else if (!m.classList.contains("armed")) { m.classList.add("armed"); m.dataset.label = m.textContent; m.textContent = "Click again to confirm"; setTimeout(() => { m.classList.remove("armed"); m.textContent = m.dataset.label; }, 4000); return; }
    try { await api("/api/maintenance/" + m.dataset.maint, { method: "POST" }); toast("Done"); loadSettings(); } catch (err) { toast(err.message); }
  }
});
// what /api/maintenance/clear-feedback clears: every row of the feedback table, nothing else
const RESET_FEEDBACK = `Reset all feedback?

This clears everything on the History page:
• Artists you marked Not interested
• Artists you already know, and the ratings you gave them here
• Artists you liked here
• The Sent to Lidarr list

Star ratings in Plex, Saved artists, Don't learn from and hidden Liked songs stay as they are. Artists in Lidarr still stay out of your picks.`;
async function testSvc(id, quiet) {
  const out = $("tres-" + id); if (!out) return;
  out.className = "tres"; out.textContent = "Testing…";
  const body = {}; Object.keys(S.values).forEach(k => body[k] = val(k));
  try {
    const r = await api("/api/settings/test/" + id, { method: "POST", body: JSON.stringify(body) });
    out.className = "tres " + (r.ok ? "ok" : "bad"); if (!r.ok) openFor(id); out.textContent = (r.ok ? "✓ " : "✕ ") + r.message;
    if (id === "plex" && r.libraries) { plexLibs = r.libraries; renderLibs(); }
  } catch (err) { openFor(id); out.className = "tres bad"; out.textContent = quiet ? "" : "✕ " + err.message; }
}
$("discard").onclick = () => { draft = {}; setDirty(false); renderSettings(); };
$("saveBtn").onclick = async () => {
  try {
    const was = S.configured;
    const r = await api("/api/settings", { method: "PUT", body: JSON.stringify(draft) });
    S = r; draft = {}; setDirty(false); renderSettings();
    lastStatus.lidarr = !!(S.values.LIDARR_URL && S.values.LIDARR_API_KEY); lidarrOpts = null;
    if (!was && r.configured) { await api("/api/sync", { method: "POST" }); wasRunning = true; poll(); toast("Saved · finding your music now"); go("home"); }
    else if (r.changed.some(k => /PLEX|LASTFM|LISTENBRAINZ/.test(k)) && r.configured) toast("Saved", { label: "Sync now to apply", fn: async () => { await api("/api/sync", { method: "POST" }); userSync = wasRunning = true; poll(); } });
    else toast(r.changed.length ? "Saved" : "Nothing changed");
  } catch (err) { toast(err.message); }
};
window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

// ------------------------------------------------------------------ router
const VIEWS = ["home", "setup", "picks", "stream", "liked", "library", "saved", "rate", "history", "settings", "search", "like", "gaps", "releases"];
function showView(v, keepFilters) {
  view = v;
  if (v !== "search") $("gq").value = "";
  document.querySelectorAll(".view").forEach(x => x.classList.remove("on"));
  const sec = { home: "v-home", picks: "v-grid", stream: "v-grid", rate: "v-rate", history: "v-history", settings: "v-settings", setup: "v-setup" }[v] || "v-list";
  $(sec).classList.add("on");
  document.querySelectorAll("#nav button, #gear").forEach(b => b.classList.toggle("on", b.dataset.view === v || (v === "liked" && b.dataset.view === "stream")));
  const sn = v === "stream" || v === "liked";   // the switch stays put; only the page under it changes
  $("subnav").hidden = !sn; if (sn) $("subnav").innerHTML = streamSwitch(v);
  const grid = v === "picks" || v === "stream";
  $("filters").style.display = $("activeRow").style.display = grid ? "" : "none";
  if (v !== "settings") $("savebar").classList.remove("on");
  closePop(); closeDrawer(); window.scrollTo(0, 0);
  if (grid) {
    if (!keepFilters) loadF();
    syncControls();
    $("gridHead").innerHTML = v === "stream" ? `<div class="lib-banner"><div><h3>You stream these, but don't own them</h3><p>Artists you play on ListenBrainz, Spotify, YouTube Music or Last.fm that aren't in Plex${lastStatus.lidarr ? " or Lidarr" : ""}, most-played first.${lastStatus.lidarr ? " Add them to Lidarr from here." : ""}</p></div></div>` : "";
    api("/api/tags?kind=" + gridKind()).then(d => tagFacets = d).catch(() => {});
    load(true);
  }
  if (v === "home") loadHome();
  if (v === "saved") loadSaved();
  if (v === "rate") loadRate();
  if (v === "history") loadHistory();
  if (v === "settings") loadSettings();
  if (v === "setup") loadSetup();
  if (v === "gaps") loadGaps();
  if (v === "releases") loadReleases();
  if (v === "library") loadLibrary();
  if (v === "liked") loadLiked();
}
let skipRoute = false;
function route() {
  if (skipRoute) { skipRoute = false; return; }
  const parts = (location.hash || "#/home").split("/"), v = parts[1];
  if (dirty && v !== "settings" && !window.confirm("Discard unsaved settings changes?")) { history.replaceState(null, "", "#/settings"); return; }
  if (dirty && v !== "settings") { draft = {}; setDirty(false); }
  if (v === "search") { showView("search"); runSearch(safeDecode(parts[2] || "")); return; }
  if (v === "like") { showView("like"); loadLike(safeDecode(parts[2] || ""), safeDecode(parts[3] || "")); return; }
  showView(VIEWS.includes(v) ? v : "home");
}
const safeDecode = s => { try { return decodeURIComponent(s); } catch { return s; } };   // a hand-typed address like #/search/100%
function go(v) { if (location.hash === "#/" + v) showView(v); else location.hash = "#/" + v; }
window.addEventListener("hashchange", route);
document.querySelectorAll("#nav button, #gear").forEach(b => b.onclick = () => go(b.dataset.view));
function refreshView() { if (view === "home") loadHome(); else if (view === "picks" || view === "stream") load(true); else if (view === "saved") loadSaved(); else if (view === "library") loadLibrary(); else if (view === "history") loadHistory(); }


// setup steps that are already done (shared by the extras step and the "While you wait" tips)
const DONE = {
  listenbrainz: () => !!(lastStatus.history || {}).listenbrainz,
  spotify: () => { const h = lastStatus.history || {}; return !!(h.spotify_export || h.spotify || lastStatus.spotify); },
};
const APP_SETUP = {
  name: "Recordhound",
  intro: "Music recommendations built from your own Plex library and listening history, with a reason for every pick.",
  requiredText: "connect Plex",
  required: ["plex"],
  why: { plex: "Recordhound reads your music libraries, play counts (Plexamp included) and star ratings. Nothing in Plex changes unless you ask." },
  libType: null,
  libHelp: "Untick any library you want ignored. You can change this later in Settings.",
  extras: [
    { title: "Connect ListenBrainz", why: "Free scrobbling that covers your listening outside Plex. Your profile needs to be public.", card: "listenbrainz", done: () => DONE.listenbrainz() },
    { title: "Add your Spotify history", why: "Upload the file Spotify emails you (no developer account needed), or connect your account.", card: "spotify", done: () => DONE.spotify() },
    { title: "Connect Lidarr", why: "Add artists you like in one click.", card: "lidarr", done: () => !!lastStatus.lidarr },
  ],
  syncText: "Recordhound is reading your library and looking up every artist. The first time can take 20 minutes or more, because MusicBrainz allows one lookup a second. Later syncs are quick.",
  waitTips: [
    { title: "Rate the artists you play", why: "Ratings are the strongest signal there is. The Rate page goes one artist at a time.", label: "Open Rate", view: "rate" },
    { title: "Request your Spotify history", why: "Spotify takes a few days to email it, so it's worth asking now.", label: "Set up", card: "spotify", done: () => DONE.spotify() },
    { title: "Connect ListenBrainz", why: "If you scrobble there, your listening counts from the next sync.", label: "Set up", card: "listenbrainz", done: () => DONE.listenbrainz() },
  ],
  readyText: "Every pick shows which of your artists led to it. Star ratings in Plex or Plexamp sharpen them most.",
  doneLabel: "See your music",
  checklist: () => { const h = lastStatus.history || {}; return [
    { title: "Connect Plex", done: !!lastStatus.configured, card: "plex" },
    { title: "Finish the first sync", done: !!lastStatus.last_sync },
    { title: "Add listening history (ListenBrainz, Spotify or YouTube Music)", done: !!(h.listenbrainz || h.spotify || h.spotify_export || h.youtube_music || h.lastfm), card: "listenbrainz" },
    { title: "Connect Lidarr", done: !!lastStatus.lidarr, card: "lidarr" },
  ]; },
};
// ------------------------------------------------------------------ first-run setup walkthrough
// Shown instead of Settings on a fresh install. Uses the same settings and connection tests as
// Settings; only the presentation differs. APP_SETUP (above) says what's required and what to suggest.
let wz = { step: 0, vals: {}, ok: {}, msg: {}, libs: null, busy: false }, wzTimer = null, pendingCard = null;
const wzSteps = () => ["welcome", ...APP_SETUP.required, "extras", "sync"];
const wzVal = k => wz.vals[k] ?? "";
async function loadSetup() {
  store.set("onboarding", "1");
  try { S = await api("/api/settings"); } catch {}
  if (lastStatus.configured && wz.step < wzSteps().indexOf("extras")) wz.step = wzSteps().indexOf("extras");
  if (S && !wz.vals.PLEX_URL) wz.vals.PLEX_URL = S.values.PLEX_URL || "";
  renderSetup();
}
function openSetting(card) {   // jump to one card in Settings, opened
  pendingCard = card; openCards.add(card);
  go("settings");
}
function focusPendingCard() {
  if (!pendingCard) return;
  const el = $("svc-" + pendingCard) || $(pendingCard); pendingCard = null;
  if (!el) return;
  if (el.tagName === "DETAILS") el.open = true;
  setTimeout(() => window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - headerOffset(), behavior: "smooth" }), 50);
}
function wzField(k, label, help, ph = "") {
  const v = wzVal(k);
  return `<div class="field"><label>${label}</label>${isSecret(k, v) ? secretInput("data-wz-k", k, v, ph) : `<input type="text" data-wz-k="${k}" value="${esc(v)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">`}
    ${help ? `<div class="help">${help}</div>` : ""}</div>`;
}
function renderSetup() {
  clearInterval(wzTimer);
  const steps = wzSteps(), id = steps[wz.step], n = steps.length;
  const dots = `<div class="wz-dots">${steps.map((s, i) => `<span class="${i < wz.step ? "done" : i === wz.step ? "on" : ""}"></span>`).join("")}</div>`;
  let body = "";
  if (id === "welcome") {
    body = `<h1>Welcome to ${APP_SETUP.name}</h1><p class="lead">${APP_SETUP.intro}</p>
      <p class="muted">Setup takes a couple of minutes: ${APP_SETUP.requiredText}, then a few optional extras that make your picks better.</p>
      <div class="wz-actions"><span></span><button class="btn primary" data-wz="next">Get started</button></div>
      <p class="muted wz-skip">Rather do it yourself? <a href="#/settings">Go to Settings</a></p>`;
  } else if (APP_SETUP.required.includes(id)) {
    const c = SVC[id], tested = wz.ok[id];
    body = `<p class="wz-step">Step ${wz.step} of ${n - 1}</p><h2>Connect ${c.title}</h2><p class="lead">${APP_SETUP.why[id]}</p>
      <div class="scard">
        ${c.url ? wzField(c.url, "URL", c.urlHelp, "http://") : ""}
        ${wzField(c.key, c.keyLabel, c.keyHelp)}
        <div class="testrow"><button class="btn" data-wz="test">Test connection</button>
          <span class="tres ${tested ? "ok" : wz.msg[id] ? "bad" : ""}">${wz.msg[id] ? (tested ? "✓ " : "✕ ") + esc(wz.msg[id]) : ""}</span></div>
        ${id === "plex" && wz.libs ? `<div class="field"><label>Libraries</label><div><div class="libs wz-libs">${wz.libs.map(l =>
          `<label><input type="checkbox" data-wz-lib="${esc(l.title)}" ${!(wz.vals.PLEX_LIBRARIES && wz.vals.PLEX_LIBRARIES.length) || wz.vals.PLEX_LIBRARIES.includes(l.title) ? "checked" : ""}> ${esc(l.title)}${l.type && APP_SETUP.libType ? ` <small>${APP_SETUP.libType(l.type)}</small>` : ""}</label>`).join("")}</div>
          <div class="help">${APP_SETUP.libHelp}</div></div></div>` : ""}
      </div>
      <div class="wz-actions"><button class="btn" data-wz="back">Back</button>
        <button class="btn primary" data-wz="next" ${tested && !wz.busy ? "" : "disabled"}>${wz.busy ? "Saving…" : "Continue"}</button></div>
      ${tested ? "" : `<p class="muted wz-skip">Test the connection to continue.</p>`}`;
  } else if (id === "extras") {
    const items = APP_SETUP.extras.filter(x => !x.hide || !x.hide());
    body = `<p class="wz-step">Step ${wz.step} of ${n - 1}</p><h2>Make your picks better</h2>
      <p class="lead">All optional, and all of it can wait. ${lastStatus.running ? "Your first sync is already running in the background." : ""}</p>
      <div class="wz-list">${items.map(x => `<div class="wz-item"><div><b>${x.title}</b>${x.done() ? pill(true, "Done") : ""}<p>${x.why}</p></div>
        <button class="btn" data-wz-card="${x.card}">${x.done() ? "Change" : "Set up"}</button></div>`).join("")}</div>
      <p class="muted">Each <b>Set up</b> opens that part of Settings. You can also do these any time later from Settings.</p>
      <div class="wz-actions"><span></span><button class="btn primary" data-wz="next">Continue</button></div>`;
  } else if (id === "sync") {
    const s = lastStatus, ready = s.last_sync && !s.running;
    body = `<h2>${ready ? "Your picks are ready" : s.error && !s.running ? "The first sync hit a problem" : "Building your picks"}</h2>
      ${ready ? `<p class="lead">${APP_SETUP.readyText}</p><div class="wz-actions"><span></span><button class="btn primary" data-wz="done">${APP_SETUP.doneLabel}</button></div>`
        : s.error && !s.running ? `<p class="lead tres bad">${esc(s.error)}</p><p class="muted">Fix the setting it mentions, then start the sync again.</p>
          <div class="wz-actions"><a class="btn" href="#/settings">Open Settings</a><button class="btn primary" data-wz="resync">Try again</button></div>`
        : `<p class="lead">${APP_SETUP.syncText}</p>
          <div class="wz-progress"><div class="bar-track"><div class="bar-fill wz-indef"></div></div><p class="muted" id="wzStep">${esc(s.step && s.step !== "idle" ? s.step + (s.progress ? " · " + s.progress : "") + "…" : "Starting…")}</p></div>
          <h4 class="wz-h4">While you wait</h4>
          <div class="wz-list">${APP_SETUP.waitTips.map(t => { const done = !!(t.done && t.done()); return `<div class="wz-item"><div><b>${t.title}</b>${done ? pill(true, "Done") : ""}<p>${t.why}</p></div>
            <button class="btn" ${t.card ? `data-wz-card="${t.card}"` : `data-wz-go="${t.view}"`}>${done ? "Change" : t.label}</button></div>`; }).join("")}</div>
          <p class="muted">You can leave this page; the sync keeps going.</p>`}`;
    if (!ready) wzTimer = setInterval(async () => {
      if (view !== "setup") return clearInterval(wzTimer);
      try { lastStatus = await api("/api/status"); } catch { return; }
      if ((lastStatus.last_sync && !lastStatus.running) || (lastStatus.error && !lastStatus.running)) renderSetup();
      else if ($("wzStep")) $("wzStep").textContent = lastStatus.step && lastStatus.step !== "idle" ? `${lastStatus.step}${lastStatus.progress ? " · " + lastStatus.progress : ""}…` : "Starting…";
    }, 2000);
  }
  $("setupBody").innerHTML = `<div class="wz">${dots}${body}</div>`;
}
async function wzTest() {
  const id = wzSteps()[wz.step], c = SVC[id];
  const vals = { ...(S ? S.values : {}), [c.key]: wzVal(c.key) };
  if (c.url) vals[c.url] = wzVal(c.url);
  wz.msg[id] = "Testing…"; wz.ok[id] = false; renderSetup();
  try {
    const r = await api("/api/settings/test/" + id, { method: "POST", body: JSON.stringify(vals) });
    wz.ok[id] = !!r.ok; wz.msg[id] = r.message;
    if (id === "plex" && r.libraries) wz.libs = r.libraries;
  } catch (e) { wz.ok[id] = false; wz.msg[id] = e.message; }
  renderSetup();
}
async function wzNext() {
  const steps = wzSteps(), id = steps[wz.step];
  if (APP_SETUP.required.includes(id) && id === APP_SETUP.required[APP_SETUP.required.length - 1]) {
    // last required step: save everything required, then start the first sync so it runs during the extras
    const body = {};
    APP_SETUP.required.forEach(r => { const c = SVC[r]; if (c.url) body[c.url] = wzVal(c.url); body[c.key] = wzVal(c.key); });
    if (wz.vals.PLEX_LIBRARIES) body.PLEX_LIBRARIES = wz.vals.PLEX_LIBRARIES;
    wz.busy = true; renderSetup();
    try {
      S = await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      lastStatus.configured = S.configured;
      if (S.configured && !lastStatus.running) { await api("/api/sync", { method: "POST" }); lastStatus.running = true; wasRunning = true; poll(); }
    } catch (e) { wz.busy = false; wz.msg[id] = e.message; wz.ok[id] = false; renderSetup(); return; }
    wz.busy = false;
  }
  wz.step = Math.min(steps.length - 1, wz.step + 1); renderSetup(); window.scrollTo(0, 0);
}
$("setupBody").addEventListener("input", e => {
  const k = e.target.dataset.wzK;
  if (k) { wz.vals[k] = e.target.value; const id = wzSteps()[wz.step]; if (wz.ok[id]) { wz.ok[id] = false; wz.msg[id] = ""; renderSetup(); const el = document.querySelector(`[data-wz-k="${k}"]`); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } } }
  if (e.target.dataset.wzLib !== undefined) {
    const boxes = [...document.querySelectorAll("[data-wz-lib]")], on = boxes.filter(b => b.checked).map(b => b.dataset.wzLib);
    wz.vals.PLEX_LIBRARIES = on.length === boxes.length ? [] : on;
  }
});
$("setupBody").addEventListener("click", async e => {
  const a = e.target.closest("[data-wz]"), card = e.target.closest("[data-wz-card]"), gv = e.target.closest("[data-wz-go]");
  if (card) { openSetting(card.dataset.wzCard); return; }
  if (gv) { go(gv.dataset.wzGo); return; }
  if (!a) return;
  const act = a.dataset.wz;
  if (act === "next") wzNext();
  else if (act === "back") { wz.step = Math.max(0, wz.step - 1); renderSetup(); }
  else if (act === "test") wzTest();
  else if (act === "done") { store.set("onboarding", "1"); go("home"); }
  else if (act === "resync") { try { await api("/api/sync", { method: "POST" }); lastStatus.running = true; lastStatus.error = null; wasRunning = true; poll(); renderSetup(); } catch (err) { toast(err.message); } }
});

// ---- Getting started checklist on For You (only for installs set up with the walkthrough)
function checklistHTML() {
  if (store.get("onboarding") !== "1" || store.get("checklistDone") === "1") return "";
  const items = APP_SETUP.checklist();
  if (items.every(x => x.done)) { store.set("checklistDone", "1"); return ""; }
  const left = items.filter(x => !x.done).length;
  return `<div class="gs-card"><div class="gs-head"><h3>Getting started</h3><span class="muted">${left} left</span>
      <button class="link" data-gs="dismiss">Hide</button></div>
    <div class="gs-items">${items.map(x => `<div class="gs-item ${x.done ? "done" : ""}"><span class="gs-check">${x.done ? "✓" : ""}</span>
      <span>${x.title}</span>${x.done ? "" : x.card ? `<button class="link accent" data-gs-card="${x.card}">Set up</button>` : x.view ? `<button class="link accent" data-gs-go="${x.view}">Open</button>` : ""}</div>`).join("")}</div></div>`;
}
const _loadHomeBase = loadHome;
loadHome = async (...args) => {
  await _loadHomeBase(...args);
  const html = checklistHTML();
  if (html && $("homeRows") && !$("homeRows").querySelector(".gs-card")) $("homeRows").insertAdjacentHTML("afterbegin", html);
};
$("homeRows").addEventListener("click", e => {
  const d = e.target.closest("[data-gs]"), c = e.target.closest("[data-gs-card]"), g = e.target.closest("[data-gs-go]");
  if (d) { store.set("checklistDone", "1"); e.target.closest(".gs-card")?.remove(); toast("Hidden. Everything's still in Settings."); }
  else if (c) openSetting(c.dataset.gsCard);
  else if (g) go(g.dataset.gsGo);
});

// ---- phones: the filter bar fades at its right edge while more controls are off-screen, and the nav row and
// filters tuck away while you scroll down and come back when you scroll up. The top bar (logo, search, sync,
// Settings) stays. The header is fixed on phones with the page padded by its full height, so tucking never moves the page.
const phone = matchMedia("(max-width: 560px)"), hdr = document.querySelector("header");
function filterFade() { const f = $("filters"); f.classList.toggle("fade-r", f.scrollWidth - f.scrollLeft - f.clientWidth > 4); }
$("filters").addEventListener("scroll", filterFade, { passive: true });
new ResizeObserver(filterFade).observe($("filters"));   // narrower when a scrollbar appears
function tuck(on) { if (hdr.classList.contains("tucked") === on) return; if (on) closePop(); hdr.classList.toggle("tucked", on); }
function fitHeader() {
  if (!phone.matches) { tuck(false); document.body.style.removeProperty("--hdr"); return; }
  if (!hdr.classList.contains("tucked")) document.body.style.setProperty("--hdr", hdr.offsetHeight + "px");
  filterFade();
}
new ResizeObserver(fitHeader).observe(hdr);
phone.addEventListener("change", fitHeader);
let hdrY = 0;
window.addEventListener("scroll", () => {
  if (!phone.matches) return;
  const y = window.scrollY;
  if (y < 80) tuck(false);
  else if (y > hdrY + 10) tuck(true);
  else if (y >= hdrY - 10) return;   // small moves either way change nothing
  else tuck(false);
  hdrY = y;
}, { passive: true });

// ------------------------------------------------------------------ status
let wasRunning = false, userSync = false;
async function poll() {
  try {
    const s = await api("/api/status"); lastStatus = s;
    const ago = t => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
    let txt, title = "";
    if (s.running) txt = `${s.step}${s.progress ? " · " + s.progress : ""}…`;
    else if (s.error) { txt = "Sync failed"; title = s.error; }
    else { txt = s.last_sync ? `Synced ${ago(s.last_sync)}` : "Not synced yet"; title = `${s.artists.toLocaleString()} artists in Plex${(s.warnings || []).length ? "\n" + s.warnings.join("\n") : ""}`; }
    $("status").textContent = txt; $("status").title = title;
    $("status").classList.toggle("bad", !!(s.error && !s.running)); $("status").classList.toggle("busy", !!s.running);
    $("sync").disabled = s.running; $("sync").classList.toggle("spin", !!s.running);
    if (s.running && !items.length && (view === "picks" || view === "stream")) $("grid").innerHTML = emptyState();
    if (wasRunning && !s.running) {
      if (userSync || !items.length) refreshView();
      else if (["home", "picks", "stream"].includes(view)) toast("Your picks were updated", { label: "Refresh", fn: refreshView });
      if (s.error) toast("Sync failed: " + s.error);
      userSync = false;
    }
    wasRunning = s.running;
  } catch (e) { if (/sign in/i.test(e.message)) location.href = "/login"; }
  clearTimeout(poll._t); poll._t = setTimeout(poll, wasRunning ? 2000 : 15000);
}
$("sync").onclick = async () => { try { await api("/api/sync", { method: "POST" }); userSync = wasRunning = true; poll(); } catch (e) { toast(e.message); } };

(async () => {
  const sp = new URLSearchParams(location.search).get("spotify");   // back from connecting Spotify
  if (sp) {
    history.replaceState(null, "", location.pathname + location.hash);
    const said = { connected: "Spotify connected. Your picks update in a minute.", denied: "Spotify sign-in was cancelled.",
      expired: "That Spotify sign-in had expired. Click Connect Spotify to try again.", failed: "Spotify didn't connect. The reason is under Settings → Spotify." }[sp];
    if (said) { toast(said); openFor("spotify"); }
  }
  try { lastStatus = await api("/api/status"); } catch {}
  if (lastStatus.configured === false) location.hash = "#/setup";
  route(); poll();
})();
