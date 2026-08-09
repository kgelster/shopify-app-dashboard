# Contributing

This is the dashboard we run for our own Shopify app. It was built when Mantle shut down, and
published because it may save someone else the same build.

**No support is promised.** Issues and pull requests are welcome and may sit for a while. If you
need something to depend on, fork it. That is not a brush-off, it is the honest expectation.

## What is likely to be merged

- Bug fixes with a test that fails without them.
- Partner API changes: new fields, changed behaviour, a version bump that breaks a query.
- New uninstall-reason strings for `CANONICAL` in `src/partner_dashboard/uninstall_reasons.py`. Shopify serves
  that pick-list localised, so this table will never be complete. Include the language.
- Documentation that corrects something wrong.

## What is unlikely to be merged

Anything that turns this into a platform. It is deliberately one Partner org, one app, one Postgres,
one machine. Multi-tenancy, a plugin system, a second database backend, and a configurable metric
framework are all reasonable things to want and all reasons to fork rather than to add a flag here.

Scaling past one instance is not a feature request either: two instances means two APScheduler
instances, so duplicate polls and duplicate alerts.

## Running the tests

```bash
createdb partner_dashboard_test
uv sync
uv run pytest
```

Tests need a real Postgres to run migrations against. `tests/conftest.py` sets everything else,
including the required-settings block, so a fresh clone runs green. `DATABASE_URL` is the one value
it does not override.

**One suite per database.** The `db` fixture truncates every table before each test, so two pytest
runs against the same database will destroy each other's fixtures and fail in a different, confusing
place each time. If you want to run two at once, give each its own `DATABASE_URL`.

CI runs the suite, `pip-audit` against the lockfile, and `gitleaks` over full history on every push.

## Things worth knowing before you change anything

- **Read `docs/architecture.md`** before touching `derive.py` or `stats.py`. It holds the
  source-of-truth table and the traps.
- **Derivation is a full replay**, not an incremental apply. Any change to derive logic rewrites
  history the next time a shop is touched.
- **`app_events` is an audit log, not a ledger.** Money comes from `subscriptions` joined to
  `charges`. The `net_change` and `plan_amount` columns are immutable by design and may predate
  fixes.
- **Widening a query needs a cursor reset.** `sync_state.cursor` persists, so a normal poll only
  fetches events past it. Without `update sync_state set cursor = null`, your change will appear to
  do nothing.
- **Any new inline `<script>` needs `nonce="{{ request.state.nonce }}"`** or the CSP in
  `security.py` blocks it silently: blank page, console-only error.
- **`POST /ingest/usage` is the only route without interactive auth.** Three things in that path
  look simplifiable and are not: the dedupe key is scoped `(shop_gid, event_id)` rather than global,
  it is `ON CONFLICT DO NOTHING` rather than `DO UPDATE`, and event names are whitelisted. Changing
  any of the three opens a hole.
- **Per-number definitions live in `src/partner_dashboard/metrics.py`.** A tile that shows a number it cannot
  define is the thing that registry exists to make impossible.
- **`scripts/check_invariants.py`** runs the same 14 invariants as the test suite against a live
  database. Run it after anything that touches derivation.

## Reporting a security issue

See [SECURITY.md](SECURITY.md). Please do not open a public issue for one.
