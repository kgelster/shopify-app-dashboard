# Changelog

Development history before the open-source release, kept because several entries explain *why* a
piece of the system is shaped the way it is. Figures from the deployment this was built for have
been removed.

## Unreleased

- First public release. Everything deployment-specific moved to configuration: `APP_NAME`,
  `ANNUAL_PLAN_AMOUNTS`, `USAGE_EVENT_TYPES`, `TRUSTED_CLIENT_IP_HEADER`, the digest schedule, and
  the GA4 / annotation / uninstall-reason boundary dates. `PUBLIC_BASE_URL` and
  `GOOGLE_ALLOWED_DOMAINS` became required with no default.
- The rate limiter read a hardcoded `Fly-Client-IP`. Behind any other proxy that meant every caller
  shared one bucket, so it limited nothing. It now reads only the header named by
  `TRUSTED_CLIENT_IP_HEADER`, takes the leftmost entry of a forwarded chain, and falls back to the
  socket peer when unset.
- **Upgrading an existing deployment:** the package is now `partner_dashboard` rather than `ppa`, so
  the ASGI target is `partner_dashboard.web:app` and the release command is
  `python -m partner_dashboard.migrate`. `PPA_NO_SCHEDULER` is now `NO_SCHEDULER`. The session cookie
  was renamed, so everyone is signed out once on deploy.

## 2026-08-08

**Transactions feed.** Until this shipped there was exactly one Partner API query, `app(id:).events`,
which describes *subscription state*. Every money figure was a forward-looking projection, and the
system was blind to refunds: a refund, downgrade adjustment or credit arrives only as a transaction
and never as an app event, so MRR read identically whether or not money came back out.

Four things verified live against the 2026-07 API, all easy to get wrong:

- `shopifyFee` is Shopify's revenue share, 0% on the first $1M of lifetime revenue since 2025-01-01.
  It reads `$0.00` on nearly every row. The processing fee lives only in the gap between
  `grossAmount` and `netAmount`, so `gross - shopifyFee = net` is false.
- That deduction is not a flat rate. Identically priced charges settle at 2.895%, 4.895% and 5.895%
  depending on the merchant. Read it per transaction.
- `Transaction` is a GraphQL interface, not a union, so `id` and `createdAt` are selectable on the
  node itself. Only `AppSubscriptionSale` adds `billingInterval`, which is the one place the Partner
  API states the billing interval outright.
- No stored cursor needed: `transactions` accepts `createdAtMin`, so the sync derives its window
  from the newest row it holds, rewound by `poll_overlap_minutes`. The events feed cannot do this;
  its cursor is an opaque token with no time semantics.

Paging is throttled to 0.3s per call. The Partner API 429s readily.

**Subscriptions tracked per id, not as a running total.** Shopify mints a *new* `AppSubscription` on
a plan change and cancels the old one (`subscribed → upgraded → unsubscribed`, both briefly live),
so a scalar running total was wrong in both directions. Derivation now keys on subscription id, and
churns whatever is still live when a shop uninstalls, because Shopify does not guarantee a cancel
event alongside one.

**Per-merchant detail page.** The header is computed from the same timeline rows the page draws,
never from a second query. Computing them independently is how a page ends up reading "churned"
above a timeline whose last event is an install. `current_state` collapses to installed/uninstalled,
because "reinstalled" is an event, not a state.

**Data invariants.** 14 of them, running in the test suite and as a read-only script against a live
database.

## 2026-08-07

**MRR correction.** `AppSubscription` carries no billing-interval field, so the interval has to be
inferred from the price. An annual plan was being counted as monthly, which overstated MRR by twelve
times for every annual subscriber. This is now `ANNUAL_PLAN_AMOUNTS`, and the FAQ says so on the
page rather than only in a comment.

**Uninstall reasons.** Native to the Partner API as `RelationshipUninstalled.reason` and
`.description`, not a vendor exclusive. They arrive localised to the merchant's admin language, so
grouping on the raw string produces a long tail of one-off bars that says nothing.

Shopify made the question mandatory partway through 2026. Coverage either side is wildly different,
so any figure spanning the boundary averages two different questions and describes neither. Reports
split on `REASON_MANDATORY_FROM`.

`RELATIONSHIP_DEACTIVATED` (store closed or frozen by Shopify) folds into type `uninstalled`, and
those merchants are never shown the survey, so coverage figures must exclude them.

**Growth build-out.** MRR movements waterfall, churn autopsy, call sheets, install-cohort retention,
ops health strip with a stale-sync Slack alert, weekly digest, customers pagination, and a `.md`
mirror of every page with a Copy button.

**Usage event ingestion.** `POST /ingest/usage`, the only route without interactive auth. Three
things in that path look simplifiable and are not: the dedupe key is scoped `(shop_gid, event_id)`
rather than global, it is `ON CONFLICT DO NOTHING` rather than `DO UPDATE`, and event names are
whitelisted. Activation reports read *unknown, not 0%* until events arrive, because a shop that
installed before tracking started has no activation event to find.

**Google SSO**, with Basic auth kept alongside as the curl and fallback path.

**GA4 traffic.** The Partner API exposes no App Store listing traffic at all, so this is the only
source for listing views, sources, and listing-to-install conversion.

## 2026-07-01

Initial build: Partner API ingestion, derivation, and the Customers page.
