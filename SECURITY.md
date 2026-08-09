# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/kgelster/shopify-partner-dashboard/security/advisories/new)
rather than a public issue. There is no bounty and no guaranteed response time; this is a
self-hosted tool maintained alongside other work.

Only the latest commit on `main` is supported. There are no released versions to backport to.

## What this application assumes

Read this before deploying it. Several of its security properties depend on how you run it, not on
the code.

- **It is not multi-tenant.** Everyone who can sign in sees every merchant's data. There is no
  per-user scoping and adding a user is granting full access.
- **`GOOGLE_ALLOWED_DOMAINS` is the entire access gate for SSO.** The OAuth client is External, so
  Google will happily authenticate any Google account; this app decides who gets in. It has no
  default. The allowlist is re-checked on every request rather than only at sign-in, so removing an
  entry invalidates cookies it already issued.
- **Basic auth sits alongside SSO and bypasses the domain allowlist.** It exists so curl, health
  checks and scripted access keep working, and as the fallback when Google is unreachable. Anyone
  holding a `DASHBOARD_USERS` pair is in, regardless of `GOOGLE_ALLOWED_DOMAINS`. Treat those
  credentials as equivalent to full access and generate them randomly.
- **`SESSION_SECRET` must be at least 32 characters.** It signs every session cookie, and the cookie
  salt is in this repository, so a guessable secret is a full pre-authentication bypass: anyone can
  mint a session for any allowed address. The app refuses to start on a non-localhost
  `PUBLIC_BASE_URL` with a shorter one. The check is on length, not on equality with a placeholder,
  because the likeliest accident is an unset variable rather than the placeholder. `PUBLIC_BASE_URL`
  is parsed and its hostname compared, so `https://localhost.evil.com` is not treated as local.
  Rotating the secret logs everyone out, which is also how you revoke every session at once.
- **Rate limiting is per-process and in memory.** It resets on restart, and it keys on
  `TRUSTED_CLIENT_IP_HEADER`. Leave that unset behind a proxy and every caller shares one bucket, so
  the limiter protects nothing. Prefer a single-value header your proxy overwrites
  (`Fly-Client-IP`, `CF-Connecting-IP`, `X-Real-IP`). `X-Forwarded-For` is a list that proxies
  append to, so the leftmost entry is attacker-supplied; only the rightmost entry is read, and only
  values that parse as an IP address are accepted. The key space is bounded, so an unauthenticated
  caller cannot grow it.
- **`POST /ingest/usage` is the only route without interactive auth**, gated on a shared secret in
  `X-Usage-Token`. Unset means it refuses everything, which is the right state until your app is
  wired. It is server-to-server only; a token shipped to a storefront is a token anyone can read.
  **One token authorises writes for every shop.** There is no per-shop credential and no notion of
  a caller "owning" a `shop_gid`, so anyone holding the token can write usage events attributed to
  any merchant. `shop_gid` is shape-checked against the Shopify GID form and `occurred_at` is
  bounded in both directions, which stops a forged backfill from rewriting activation history with
  impossible dates, but a leaked token still means fabricated activation data. Rotate it if the app
  that holds it is ever compromised.
- **`POST /annotations` is gated on the session cookie, not on `verify_creds`.** Basic auth is sent
  cross-site by browsers, so accepting it there would make a cross-site form post work. `SameSite=lax`
  is necessary but not sufficient on its own: "site" is the registrable domain, so any sibling host
  under your domain still gets the cookie attached. Both annotation routes therefore also check
  `Origin` against `PUBLIC_BASE_URL`. Any signed-in principal can delete any note; notes are not
  owned.
- **`/static` is mounted unauthenticated.** It holds decorative illustrations only. Do not put
  anything else there.
- **Merchant free text is untrusted input.** Shop names and uninstall verbatims are typed by
  merchants and reproduced verbatim in the `.md` twins and `export.json`, which are designed to be
  pasted into language models. They are escaped for HTML and flagged as untrusted in the export
  metadata, but a value may still contain text aimed at whatever model reads the file.
- **The dashboard is `noindex` everywhere** via header, meta tag, and robots.txt, and every response
  is `Cache-Control: no-store`.

## What it deliberately does not store

No merchant contact details. `shops.owner_name` and `shops.email` exist but are emptied by migration
008 and never rendered or exported. The reasoning is in that migration.
