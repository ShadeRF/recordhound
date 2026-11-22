"""Docker health check that works over HTTP or HTTPS (self-signed certificates included)."""
import os
import ssl
import sys
import urllib.request

port = os.environ.get("PORT", "8789")
https = bool(os.environ.get("TLS_CERT", "").strip())
ctx = ssl._create_unverified_context() if https else None
try:
    urllib.request.urlopen(f"{'https' if https else 'http'}://127.0.0.1:{port}/api/health", timeout=4, context=ctx)
except Exception as e:
    sys.exit(f"unhealthy: {e}")
