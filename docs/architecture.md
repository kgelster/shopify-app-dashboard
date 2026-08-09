# Architecture

The map a new session needs before touching anything. The README is a running changelog of what was
built and when; this is the timeless version: how data moves, which table is the truth for which
number, the traps that have already cost a debugging session, and what this data set genuinely
cannot answer.

## The pipeline

```
Shopify Partner API
  │
  ├── appEvents(occurredAtMin…)      ─┐
  │     RELATIONSHIP_INSTALLED         │  ingest_raw.upsert_raw_events
  │     RELATIONSHIP_DEACTIVATED       ├──►  raw_app_events   (immutable, verbatim payloads)
  │     SUBSCRIPTION_CHARGE_*          │     charges          (upsert_charges, from inline objects)
  │                                   ─┘
  ├── transactions                    ────►  transactions     (money that actually moved)
  │
  └── (GA4 Data API, separate)        ────►  ga4_daily        (listing sessions and installs)

                                    derive.derive_installation
                                    replays one shop's raw events in occurred_at order
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
              app_events              subscriptions                  shops
        (one clean row per raw    (money: converted_at,      (current lifecycle state,
         event, immutable)         churned_at, monthly)       optionally CSV-enriched)
                    │                         │                         │
                    └─────────────────────────┼─────────────────────────┘
                                              ▼
                                          stats.py
                                  (all read-side aggregates)
                                              │
                            ┌─────────────────┴─────────────────┐
                            ▼                                   ▼
                    Jinja2 templates                    markdown_export.py
                    (8 HTML pages)                      (the .md twin of each)
```

`pipeline.py` runs the whole thing on a schedule (`scheduler.py`, every 30 minutes on one machine).
`sync_state` holds the cursor per source.

**Derivation is a full replay, not an incremental apply.** `derive_installation` re-reads every raw
event for a shop and recomputes its state from scratch on every run. That is why it is safe to run
repeatedly, and why any change to derivation logic changes history the next time a shop is touched.
Idempotence is load-bearing and is asserted in `tests/test_invariants.py`.

## Which table is the truth for which number

| Question | Source of truth | Never use |
|---|---|---|
| What are we owed per month (MRR) | `subscriptions.monthly_amount` where `churned_at is null` | `app_events.net_change` |
| What did we actually collect | `transactions.gross_amount` / `net_amount` | anything in `app_events` |
| Is a shop installed right now | `shops.install_state` | counting install/uninstall events |
| Was a shop installed at some past instant | replaying `app_events` (`stats.installed_at_time`) | `shops.install_state`, which only ever describes now |
| Why did a merchant leave | `app_events.uninstall_reason` joined to `raw_app_events` | `shops.uninstall_reason` alone |
| Did a merchant *choose* to leave | `raw_app_events.type = 'RELATIONSHIP_UNINSTALLED'` | `app_events.type = 'uninstalled'` |
| What plan is someone on | `charges.plan_interval` joined via `subscriptions.id` | inferring from the amount |

**The one rule that keeps getting broken: money comes from `subscriptions` joined to `charges`,
never from `app_events.net_change` or `app_events.plan_amount`.** Those columns are immutable by
design and were written before the annual-plan interval fix, so they still carry the old inflated
figures. `app_events` is an audit log of what happened, not a ledger.

**Per-number definitions live in `src/partner_dashboard/metrics.py`, not here.** That registry carries the
display name, the plain-English definition, the exact counting rule and the source table for every
headline figure, and it is what the tiles render behind the ⓘ and what the `.md` twins print. It is
deliberately the only copy: a definition restated in this document is a definition that drifts, and
the failure mode is never that one of them is wrong, it is that nobody knows which one shipped
first. Add a metric there and it appears on the page, in the twin, and in any agent the twin is
pasted into, with no second edit. The table above stays because it answers a different question:
which table to read, not what the number means.

## Traps, each with the symptom that reveals it

**Shopify mints a new subscription gid on a plan change.** It does not edit an `AppSubscription`; it
activates a new one and cancels the old. In the feed that reads `subscribed → upgraded →
unsubscribed`, usually within a day, and both are briefly live at once.
*Symptom:* the MRR chart reads below the MRR tile, or a plan change looks like a partial
cancellation. This is why `derive` tracks a `dict` of live subscriptions per id rather than one
running scalar.

**An uninstall does not guarantee a cancel event.** `derive` therefore churns whatever is still live
when a shop uninstalls.
*Symptom:* the Active MRR tile (which joins `shops` on `install_state`) disagrees with the MRR chart
(which does not join `shops` at all).

**`timestamptz` buckets in the connection's session timezone.** Ten aggregates in `stats.py` use
`date_trunc('month', …)`. `db.connect()` pins `TimeZone=UTC`; production was already UTC, a
developer machine was not.
*Symptom:* a test asserting a date passes locally and fails in CI, or a row lands in a different
month in dev than in prod.

**`app_events` folds `RELATIONSHIP_DEACTIVATED` into type `'uninstalled'`.** Those are stores Shopify
closed or froze, not merchants who chose to leave, and they were never shown the exit survey.
*Symptom:* churn looks worse than it is and reason coverage looks thinner than it is. Any
"merchant chose to leave" metric must join `raw_app_events` and filter on
`RELATIONSHIP_UNINSTALLED`; `stats.store_deaths` counts the other kind separately.

**The events cursor is opaque.** `poll_overlap_minutes` is inert on that source; it only re-windows
the feeds that take a timestamp.

**One Fly machine, deliberately.** Two machines means two schedulers, so duplicate polls, duplicate
Slack alerts, and a duplicate weekly digest. Never `fly scale count 2`. The in-process rate limiter
in `security.py` assumes this too.

**`SUBSCRIPTION_CHARGE_FROZEN` is treated as churn and there is no unfreeze handler.** Correct for
the data observed so far: frozen charges arrived the same day Shopify deactivated the store, and
those shops did not return. An unfreeze would silently understate MRR, so `tests/test_derive.py`
asserts the current behaviour rather than assuming it.

**`SUBSCRIPTION_CHARGE_DECLINED` is ignored entirely.** Also correct for the data observed so far: the
declines seen had no prior activation, so there was nothing to remove. An activation later declined
would leave MRR overstated.

## The invariants

`tests/test_invariants.py` asserts these on every `pytest` against a seeded world;
`scripts/check_invariants.py` runs the same questions read-only against a live database and exits
non-zero, so it can gate a deploy:

```
DATABASE_URL='postgres://…' uv run python scripts/check_invariants.py
```

1. Active MRR tile == last bucket of `mrr_trend` == `sum(plan_mix.mrr)`.
2. Each month's five movement buckets sum exactly to that month's step in the trend line.
3. The paying-shop count agrees across `overview_stats`, `unit_economics` and `funnel_stats`.
4. No shop has two simultaneously-live subscriptions.
5. No uninstalled shop has a live subscription.
6. No subscription churns before it converts.
7. A subscription with no `converted_at` is inert: no amount, already churned.
8. `shops.install_state` matches each shop's last lifecycle event.
9. No test charge contributes to any figure.
10. Every annual subscription counts at `price / 12`.
11. `collected_revenue`: `gross - taken == net`, and the monthly series sums to the totals.
12. Refunds are counted once and are already inside `gross` as negatives.
13. No orphaned `shop_gid` in `subscriptions` or `app_events`.
14. Every `app_event` traces back to a `raw_app_event`.

Run the script after anything that touches derivation. A failure here is a data bug
reaching the pages, not a flaky test.

## What this data cannot answer

- **Refunds have no event.** They exist only in `transactions` as `AppSaleAdjustment` /
  `AppSaleCredit`. Nothing derived from the events feed can see money coming back.
- **Some subscriptions have a cancel and no activation.** Their activation predates the Partner
  API's retention window, so they have no `converted_at` and no amount. They are inert by design,
  not a bug (invariant 7). How many you have depends on how old your app is.
- **A subscription cannot lapse and resume under one id.** `subscriptions` holds one
  `converted_at`/`churned_at` pair per id, so a gap is invisible and history reads as continuous.
  In practice no charge id activates twice, because Shopify mints a new subscription each time.
  Representing a gap would need a `subscription_periods` table; nothing justifies one yet.
- **Country and industry only exist if you imported them.** The Partner API exposes no merchant
  location or industry at all. `import_shops_csv` backfills them from a vendor export, and they go
  stale for every shop that installs after that import ran.
- **There is no product-usage data** beyond what the app itself posts to `POST /ingest/usage`.
  "Installed and silent" (`stats.trial_watch`) is a proxy for activation risk, not a measurement.
- **GA4 undercounts installs.** Consent banners and blockers suppress the browser-side event while
  the install still happens; `stats.install_reconciliation` states the gap rather than hiding it.
- **No merchant contact details exist.** `owner_name` and `email` were emptied by migration 008
  because their only source listed agency and app-team staff rather than the merchant.

## Auth and the request path

Google SSO first (signed `dashboard_session` cookie, re-checked against the domain allowlist on every
request), HTTP Basic second for curl and as the way in if Google is down. `HTTPBasic(auto_error=
False)` so a browser with no header falls through to `/auth/login` instead of getting a Basic popup.
The domain allowlist is enforced by us, never by Google: the OAuth client is External and will
authenticate any Google account.

`/auth/login` is a page that explains what the dashboard is; the redirect to Google lives behind
its button at `/auth/google`. Those are separate on purpose: when they were one route an
unauthenticated visitor was thrown at Google having read nothing, and a disallowed account's first
words from us were a 403.

401, 403 and 404 render `error.html` for browsers and keep their JSON body for everything else,
which is what the `.md` twins and curl see. Nothing here is indexable: `<meta name="robots">` in
`base.html`, an `X-Robots-Tag` header in `security.py` for the responses with no `<head>`, and
`/robots.txt`.

`POST /ingest/usage` is the one route that bypasses interactive auth. It takes a shared secret in
`X-Usage-Token`, caps its body, and is rate-limited. Do not rebuild it around SSO sessions; the
caller is your app's backend, not a browser.

`security.py` sets the response headers, including a per-request CSP nonce that `base.html` reads as
`{{ request.state.nonce }}`. Any new inline `<script>` needs `nonce="{{ request.state.nonce }}"` or
it will not run.

## Where things live

| Path | What |
|---|---|
| `src/partner_dashboard/ingest_raw.py` | Partner API → `raw_app_events`, `charges`, `transactions` |
| `src/partner_dashboard/derive.py` | replay → `app_events`, `subscriptions`, `shops` |
| `src/partner_dashboard/stats.py` | every read-side aggregate; no writes |
| `src/partner_dashboard/metrics.py` | one definition per number: name, rule, source table. Read by the tiles and the twins |
| `src/partner_dashboard/faq.py` | the why-don't-these-match answers, rendered at `/faq` and `/faq.md` |
| `src/partner_dashboard/annotations.py` | dated notes on the charts; the only place a person writes to this database |
| `src/partner_dashboard/ranges.py` | the allowlist behind every time-range control, shared by the pages and the twins |
| `src/partner_dashboard/markdown_export.py` | the `.md` twin of each page, and the one list of contact fields never exported |
| `src/partner_dashboard/export.py` | `/export.json`: every dataset at its widest window, as one downloadable file |
| `src/partner_dashboard/security.py` | response headers, CSP nonce, request clock, rate limiter |
| `src/partner_dashboard/static/` | the three error illustrations, and nothing else |
| `src/partner_dashboard/migrations/` | numbered, idempotent, run on deploy by `partner_dashboard.migrate` |
| `scripts/check_invariants.py` | the invariants against a live database |
| `docs/deploy.md` | deploy and secrets |
| `docs/usage-events-integration.md` | the `POST /ingest/usage` contract, to hand to your app developer |
