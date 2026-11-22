"""Security helpers shared by the web app: headers, cross-site request blocking, login throttling,
secret redaction for logs and error messages, and size-limited zip reading."""
import logging
import re
import threading
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from urllib3.util import parse_url

from . import config

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",                     # nobody can embed the app to trick clicks
    "Referrer-Policy": "no-referrer",              # don't leak your server's address to sites you click through to
    "Cross-Origin-Opener-Policy": "same-origin",
}


# ------------------------------------------------------------------ cross-site requests & headers

def _cross_site(request: Request):
    """True when a browser sent this from another website (a page you visited trying to drive the app)."""
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        return urlparse(origin).netloc.lower() != host.lower()
    return False   # not a browser (curl, scripts): nothing to protect against here


async def middleware(request: Request, call_next):
    if request.method in UNSAFE and request.url.path.startswith("/api/") and _cross_site(request):
        return JSONResponse({"detail": "Blocked a request from another website"}, status_code=403)
    resp = await call_next(request)
    for k, v in HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


# ------------------------------------------------------------------ login throttling

_fails = {}
_fails_lock = threading.Lock()
MAX_FAILS, WINDOW = 8, 15 * 60


def client_ip(request: Request):
    return request.client.host if request.client else "?"


def login_blocked(ip):
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if time.time() - t < WINDOW]
        _fails[ip] = recent
        return len(recent) >= MAX_FAILS


def login_failed(ip):
    with _fails_lock:
        now = time.time()
        if len(_fails) > 1000:   # forget addresses whose failures are all old, so the list can't grow without end
            for old in [k for k, v in _fails.items() if not v or now - v[-1] >= WINDOW]:
                del _fails[old]
        _fails.setdefault(ip, []).append(now)


def login_ok(ip):
    with _fails_lock:
        _fails.pop(ip, None)


# ------------------------------------------------------------------ secrets in logs and errors

_QUERY_SECRET = re.compile(r"((?:api_key|apikey|token|X-Plex-Token|access_token|refresh_token|password)=)[^&\s'\"]+", re.I)


def redact(text):
    """Remove configured secrets (and anything that looks like ?api_key=…) from a string."""
    s = str(text)
    for name, (_, _, secret) in config.FIELDS.items():
        v = getattr(config, name, "")
        if secret and isinstance(v, str) and len(v) >= 6:
            s = s.replace(v, "•••")
    return _QUERY_SECRET.sub(r"\1•••", s)


class _RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def install_log_redaction():
    f = _RedactFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            h.addFilter(f)


# ------------------------------------------------------------------ settings: don't send a saved secret to a new address

def secret_host_guard(values, current, pairs):
    """If an address changed but its secret wasn't re-entered, refuse to reuse the saved secret.

    Without this, anyone who can reach the page could point Plex/Radarr/etc. at their own server,
    press Test, and receive your stored token. Returns the message for the first offending pair."""
    for url_field, secret_field in pairs:
        new, old = values.get(url_field), current.get(url_field)
        if new is None or not old:
            continue
        if _host(new) != _host(old) and (secret_field not in values or config.is_masked(values[secret_field])):
            return url_field, secret_field
    return None


def _host(url):
    """(host, port) an address really leads to. An address that could be read two ways never
    matches a normal one, so it always counts as changed and the secret has to be entered again."""
    s = str(url).strip()
    full = s if "://" in s else "http://" + s
    try:
        p = urlparse(full)
        host, port = (p.hostname or "").lower(), p.port
        sent = parse_url(full)   # the parser the HTTP library uses when it actually sends the request
        sent_host, sent_port = (sent.host or "").lower().strip("[]"), sent.port
    except Exception:
        return "?", s
    odd = "\\" in s or "@" in p.netloc or any(ch.isspace() or ord(ch) < 32 for ch in s)
    if odd or sent_host != host or sent_port != port:
        return "?", s
    return host, port


# ------------------------------------------------------------------ uploads

MAX_UNZIPPED = 400 * 1024 * 1024


async def read_upload(request: Request, limit, too_big="That file is too big"):
    """The request body, refused as soon as it passes limit bytes, whether or not the sender said how big it is."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        raise HTTPException(413, too_big)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, too_big)
        chunks.append(chunk)
    return b"".join(chunks)


def zip_read(z, name, budget):
    """Read one member of a zip with a running size budget (stops zip bombs). budget is a one-item list."""
    info = z.getinfo(name)
    if info.file_size > budget[0]:
        raise ValueError("That zip expands to more data than an export should")
    with z.open(info) as f:
        data = f.read(budget[0] + 1)
    if len(data) > budget[0]:
        raise ValueError("That zip expands to more data than an export should")
    budget[0] -= len(data)
    return data
