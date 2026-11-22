"""Start the web server. HTTPS and reverse-proxy trust come from environment variables:

  TLS_CERT / TLS_KEY             serve HTTPS directly with this certificate (paths inside the container,
                                 e.g. /data/certs/cert.pem). Leave empty for plain HTTP.
  TRUSTED_PROXIES                which addresses may tell the app the original request was HTTPS
                                 (X-Forwarded-Proto) and who the visitor was (X-Forwarded-For).
                                 Defaults to private networks, which covers a reverse proxy or
                                 Tailscale on the same server. Use "*" to trust everyone, or a single
                                 address to lock it down.
"""
import os
import sys

import uvicorn

PORT = int(os.environ.get("PORT", "8789"))
PRIVATE = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7"


def tls():
    cert, key = os.environ.get("TLS_CERT", "").strip(), os.environ.get("TLS_KEY", "").strip()
    if not cert and not key:
        return {}
    missing = [p for p in (cert, key) if not p or not os.path.isfile(p)]
    if missing:
        sys.exit(f"HTTPS is set up but these files are missing: {', '.join(m or '(empty path)' for m in missing)}. "
                 "Check TLS_CERT and TLS_KEY, or clear both to use plain HTTP.")
    return {"ssl_certfile": cert, "ssl_keyfile": key}


def main():
    opts = tls()
    print(f"Serving on {'https' if opts else 'http'}://0.0.0.0:{PORT}", flush=True)
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("TRUSTED_PROXIES", "").strip() or PRIVATE, **opts)


if __name__ == "__main__":
    main()
