"""Response headers and a small in-process rate limiter.

Both are deliberately dependency-free. This is one machine serving an internal
dashboard to a handful of staff; a Redis-backed limiter and a report-only CSP
rollout would be more machinery than the thing being protected. Scaling past one
machine is already ruled out by the scheduler, so a shared store buys nothing.
"""

import ipaddress
import secrets
import time
from collections import OrderedDict

from starlette.middleware.base import BaseHTTPMiddleware

from partner_dashboard.config import get_settings

# One year, and the value browsers require before they will honour a preload
# submission. Your host should already force the HTTP->HTTPS redirect; this is what
# stops the first request of a session from going out in the clear at all.
HSTS = "max-age=31536000; includeSubDomains"

# 'nonce-...' rather than 'unsafe-inline': base.html carries four inline blocks
# (the hamburger toggle, the Copy MD fetch, the count-up animation, and the
# stylesheet), and allowing unsafe-inline to keep them working would leave the
# policy doing nothing at all. The nonce is minted per request and read in the
# template as {{ request.state.nonce }}, which needs no route changes because
# Starlette already injects `request` into every template context.
#
# style-src keeps 'unsafe-inline' on purpose: the charts set bar widths with
# style="width: N%" attributes, and a nonce cannot cover inline style
# attributes -- only <style> elements. Removing them would mean rewriting every
# chart to emit CSS custom properties, which is a rendering change, not a
# security fix. Inline styles are not a script-execution sink.
# The three third-party origins are the ones base.html already loads: htmx from
# unpkg (pinned and now SRI-checked) and the Google Fonts stylesheet/font pair.
# They are named explicitly so that adding a fourth is a deliberate act rather
# than something a copied <script> tag does silently.
CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data:; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self'; "
    # Nothing here embeds a plugin, and default-src already covers this by
    # fallback. Stated outright so the one directive an old Flash/PDF-object
    # trick would need is denied by name rather than by inheritance.
    "object-src 'none'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    # Every page here is per-request MRR/churn/merchant data behind sign-in, and
    # none of it benefits from being cached. no-store keeps it out of the disk
    # cache and, more to the point, off the back button on a shared laptop after
    # sign-out. setdefault means StaticFiles keeps its own caching for the three
    # illustrations if it sets any.
    "Cache-Control": "no-store",
    # Cross-origin isolation. frame-ancestors/XFO already stop this page being
    # framed; these stop a cross-origin opener from keeping a handle to our
    # window and stop another origin loading our responses as a subresource.
    # COOP only (not COEP): require-corp would break the Google Fonts and unpkg
    # subresources base.html loads, and buys nothing here.
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # Nothing here should ever be indexed. base.html carries the equivalent
    # <meta>, but only HTML has a <head>: this covers the .md twins, the
    # illustrations, and any JSON error body. robots.txt disallows everything
    # too, and is the weaker of the two -- a crawler that ignores it still
    # sees this on the response itself.
    "X-Robots-Tag": "noindex, nofollow",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set the standard headers on every response, and mint the CSP nonce.

    The nonce goes on `request.state` before the handler runs so templates can
    read it, and into the header afterwards, so the two can never disagree.

    It also stamps the start of the request, which the footer reads back to
    report how long the page took. Here rather than in a second middleware
    because this one already runs on every request, outermost.
    """

    async def dispatch(self, request, call_next):
        request.state.started = time.perf_counter()
        nonce = secrets.token_urlsafe(16)
        request.state.nonce = nonce
        response = await call_next(request)
        for key, value in STATIC_HEADERS.items():
            response.headers.setdefault(key, value)
        response.headers.setdefault("Content-Security-Policy",
                                    CSP_TEMPLATE.format(nonce=nonce))
        # Only meaningful over TLS, and setting it on a plain-HTTP local dev
        # response would pin localhost to HTTPS in the developer's browser.
        if request.url.scheme == "https" or \
                request.headers.get("x-forwarded-proto") == "https":
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        return response


class RateLimiter:
    """Fixed-window counter per key, held in memory.

    Deliberately not distributed: there is exactly one machine (two would mean
    two schedulers, so scaling out is already ruled out for other reasons). It
    resets on deploy, which for a brute-force guard costs an attacker a restart
    they do not control and cannot trigger.

    Only *failures* are counted on the auth paths, so a working session is never
    throttled no matter how many pages it loads.
    """

    # Bounded so an unauthenticated caller cannot grow this dict for free. Both
    # limiter paths call check() before authenticating, so every request would
    # otherwise insert one permanent entry keyed on something the caller
    # influences. An IPv6 /64 alone is more addresses than memory. Oldest key is
    # evicted first, which at worst forgives an attacker who has already
    # out-waited MAX_KEYS other sources within one window.
    MAX_KEYS = 10_000

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: OrderedDict[str, list[float]] = OrderedDict()

    def check(self, key: str) -> bool:
        """True if this key is still under its limit. Does not record anything."""
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, ()) if now - t < self.window]
        if hits:
            self._hits[key] = hits
            self._hits.move_to_end(key)
        else:
            # Nothing live for this key: drop it rather than storing an empty
            # list, so a read never grows the dict.
            self._hits.pop(key, None)
        return len(hits) < self.limit

    def record(self, key: str) -> None:
        self._hits.setdefault(key, []).append(time.monotonic())
        self._hits.move_to_end(key)
        while len(self._hits) > self.MAX_KEYS:
            self._hits.popitem(last=False)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


def client_key(request) -> str:
    """Best available client identity, for rate limiting only.

    Behind a TLS-terminating proxy, request.client.host is the proxy on every
    request, which would collapse every caller into one bucket: a limiter that
    limits nothing. TRUSTED_CLIENT_IP_HEADER names the header to read instead.

    **The rightmost entry, not the leftmost.** Proxies APPEND to
    X-Forwarded-For, so the leftmost entry is whatever the client sent and the
    rightmost is what your own proxy actually observed. Reading the leftmost
    lets a caller pick a fresh bucket per request by rotating a header value,
    which defeats the brute-force guard entirely. Single-value headers
    (Fly-Client-IP, CF-Connecting-IP, X-Real-IP) carry one entry and are
    unaffected either way; prefer one of those.

    With more than one proxy in front, the rightmost entry is the nearest proxy
    rather than the client, so everyone behind it shares a bucket. That is a
    weaker limiter, not a bypass, which is the right way round.

    The value must parse as an IP address. Without that check the bucket key is
    an arbitrary caller-supplied string, so the keyspace is unbounded and an
    8KB header becomes an 8KB dict key. Anything unparseable falls back to the
    socket peer, as does an absent header, which is correct with no proxy in
    front and in tests.
    """
    # Lower-cased because the operator writes "X-Forwarded-For" in .env and HTTP
    # header names are case-insensitive. Starlette's Headers already folds case,
    # but not every caller of this passes one.
    header = get_settings().trusted_client_ip_header.strip().lower()
    forwarded = request.headers.get(header) if header else None
    if forwarded:
        candidate = forwarded.split(",")[-1].strip()
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            pass          # spoofed or malformed: trust the socket instead
        else:
            return candidate
    return request.client.host if request.client else "unknown"
