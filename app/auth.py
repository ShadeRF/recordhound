"""Optional single-user login. Off until a password is set on the Settings page."""
import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config

COOKIE = "recordhound_session"
DAYS = 30
OPEN_PATHS = ("/login", "/api/login", "/static/", "/favicon.ico", "/api/health")


def enabled():
    return bool(config.AUTH_USERNAME and config.AUTH_PASSWORD_HASH)


def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240_000).hex()
    return f"pbkdf2${salt}${h}"


def check_password(pw, stored):
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240_000).hex(), h)


def _secret():
    if not config.AUTH_SECRET:
        config.save({"AUTH_SECRET": secrets.token_hex(32)})
    return config.AUTH_SECRET.encode()


def _key():
    # The password hash is part of the signing key, so changing the password signs everyone out.
    return _secret() + config.AUTH_PASSWORD_HASH.encode()


def make_token(user):
    exp = int(time.time()) + DAYS * 86400
    body = f"{user}|{exp}"
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()


def valid(token):
    try:
        user, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 2)
        good = hmac.new(_key(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.encode(), good.encode()) and int(exp) > time.time() and user == config.AUTH_USERNAME
    except Exception:   # a cookie that isn't ours, however it's malformed
        return False


def set_cookie(response, request, user):
    response.set_cookie(COOKIE, make_token(user), max_age=DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")


async def middleware(request: Request, call_next):
    if not enabled() or request.url.path.startswith(OPEN_PATHS) or valid(request.cookies.get(COOKIE, "")):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Please sign in"}, status_code=401)
    return RedirectResponse("/login")


LOGIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in · Recordhound</title>
<link rel="icon" href="/static/icon.svg" type="image/svg+xml"><meta name="theme-color" content="#0e0d0c">
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e0d0c;color:#ece6dc;font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
form{width:min(340px,88vw);background:#171513;border:1px solid #2a2622;border-radius:14px;padding:30px 28px;text-align:center}
img{width:72px;height:72px;border-radius:16px}
h1{font:700 24px Georgia,serif;letter-spacing:.14em;color:#e8b04b;text-transform:uppercase;margin:14px 0 22px}
input{display:block;width:100%;box-sizing:border-box;background:#0e0d0c;color:#ece6dc;border:1px solid #2a2622;border-radius:8px;padding:10px 12px;font:inherit;margin-bottom:10px}
input:focus{outline:none;border-color:#e8b04b}
button{width:100%;background:#e8b04b;color:#1a1408;border:0;border-radius:8px;padding:11px;font:inherit;font-weight:700;cursor:pointer;margin-top:6px}
.err{color:#d9675b;font-size:13px;min-height:18px;margin-bottom:6px}
</style></head><body>
<form id="f"><img src="/static/icon.svg" alt=""><h1><img src="/static/wordmark.svg" alt="Recordhound" style="height:40px;width:auto;border-radius:0"></h1>
<input name="username" placeholder="Username" autocomplete="username" autofocus required>
<input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
<div class="err" id="e"></div><button>Sign in</button></form>
<script>
document.getElementById("f").onsubmit=async e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.target));
const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d)});
if(r.ok)location.href="/";else document.getElementById("e").textContent="Wrong username or password";};
</script></body></html>"""


def login_page():
    if not enabled():
        return RedirectResponse("/")
    return HTMLResponse(LOGIN_PAGE)
