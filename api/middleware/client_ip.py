"""
Client IP extraction helper for code running behind Railway's reverse proxy.

Railway sits in front of every backend with TWO proxy layers — a public
edge proxy and an internal forwarding layer. Reading request.client.host
directly returns the internal forwarding IP (typically 100.64.x.x), which
makes per-IP rate limiting and per-IP quotas useless because every request
looks like it came from the same internal address.

This module is the single source of truth for client IP extraction. Use it
from any middleware or route that needs to identify the caller by IP.

WHY X-REAL-IP IS PREFERRED OVER X-FORWARDED-FOR FOR RAILWAY:

The common advice "use rightmost X-Forwarded-For to defeat spoofing" only
applies when there is exactly one trusted proxy in front of the app. On
Railway there are TWO, and the chain looks like this:

  real_client_ip → railway_edge → railway_internal → your_app

The headers your app sees are:

  X-Real-IP:        <real_client_ip>            (set by railway edge)
  X-Forwarded-For:  <real_client_ip>, <railway_edge_ip>
  request.client.host: <railway_internal_ip>    (100.64.x.x mesh)

Notice that the *rightmost* XFF entry is a Railway edge IP, not the user.
Two different users routed through the same edge would share a key, and a
single user whose request gets re-routed to a different edge would jump to
a fresh bucket. Both are wrong.

The *leftmost* XFF entry IS the real client IP, because Railway's edge
proxy strips and rewrites X-Forwarded-For from incoming requests before
forwarding upstream — so a client cannot spoof the leftmost entry by
sending a forged X-Forwarded-For header. The edge always overwrites it
with what it actually observed.

X-Real-IP is set by the same edge proxy with the same observed value,
and is the more explicit signal. We prefer it because:
  - It is unambiguous (one value, no parsing of comma chains)
  - It cannot be spoofed (edge overwrites)
  - It matches the leftmost XFF entry empirically — they are both set
    from the same observation by the edge proxy

If Railway ever changes its proxy topology (adds more hops, swaps
in a different edge product), revisit this. The verification recipe
is in api/routes/debug_ip.py — hit /api/_debug/ip from two different
networks and check that extracted_ip differs and looks like a real
public client IP, not a 10.x / 100.64.x / 172.16.x address.
"""
from fastapi import Request


def get_client_ip(request: Request) -> str:
    """
    Return the originating client IP for a request running behind Railway.

    Order of precedence:
      1. X-Real-IP — set by Railway's edge proxy from observation. Cannot
         be spoofed because the edge overwrites whatever the client sent.
      2. Leftmost X-Forwarded-For entry — the same value, parsed from the
         chain. Used as a fallback in case some platform variant only
         populates XFF.
      3. request.client.host as a last-resort fallback for local dev
         where no proxy is in front of the app.
      4. The literal string "unknown" if nothing is available.

    The returned value is treated as untrusted input — never log or store
    it directly. The anon quota module hashes it with a secret salt before
    persistence so a database leak does not expose IPs.
    """
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[0]

    if request.client and request.client.host:
        return request.client.host

    return "unknown"
