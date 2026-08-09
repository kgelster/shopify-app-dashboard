# Deploy runbook

One container: the FastAPI dashboard plus an APScheduler poll loop, with Postgres for storage.
There is a `Dockerfile` and a `fly.toml`, but nothing in the application is Fly-specific.

Two constraints are real wherever you run it:

- **Exactly one instance.** Two means two APScheduler instances, so duplicate Partner API polls and
  duplicate Slack alerts. `fly.toml` pins `min_machines_running` and `max_machines_running` to 1.
- **Migrations on release.** `python -m partner_dashboard.migrate` is idempotent and tracked in
  `schema_migrations`. It is wired as the `release_command`, and the container's `CMD` runs it again
  on boot as a second guard.

## Secrets

Every required setting has no default, so a missing one is a startup `ValidationError` rather than a
subtly wrong dashboard. Set them all before the first deploy:

```bash
fly secrets set \
  PARTNER_API_TOKEN=... \
  PARTNER_ORG_ID=... \
  PARTNER_APP_ID=... \
  DASHBOARD_USERS="admin:$(python -c 'import secrets;print(secrets.token_urlsafe(24))')" \
  PUBLIC_BASE_URL=https://your-dashboard.example.com \
  GOOGLE_ALLOWED_DOMAINS=yourcompany.com \
  SESSION_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(32))') \
  APP_NAME="Your App" \
  ANNUAL_PLAN_AMOUNTS=<your annual prices, comma separated> \
  TRUSTED_CLIENT_IP_HEADER=Fly-Client-IP
```

`DATABASE_URL` is set for you by `fly postgres attach`. `.env.example` is the complete list,
including the optional Google SSO, GA4, Slack and usage-ingest settings.

Three of these decide whether the numbers are right rather than whether the app starts:

- **`ANNUAL_PLAN_AMOUNTS`** must list every annual price you charge, with cents. `AppSubscription`
  carries no billing-interval field, so an unlisted annual price is counted as monthly, at twelve
  times its true MRR. Empty means every plan is monthly, and the app logs a warning at startup.
  Setting it *after* charges are stored does not correct them: a corrected price only reaches a
  charge on re-ingest, so follow the change with a cursor reset and replay (see below).
- **`TRUSTED_CLIENT_IP_HEADER`** must name the header your proxy sets. Wrong or unset behind a
  proxy, and rate limiting collapses every caller into one bucket.
- **`GOOGLE_ALLOWED_DOMAINS`** is the entire SSO access gate. The OAuth client is External, so
  Google authenticates any account and this decides who gets in.

## One-time setup

```bash
fly launch --no-deploy        # picks up fly.toml; do not let it overwrite the app name
fly postgres create           # a separate Postgres app
fly postgres attach <postgres-app-name>
fly scale count 1             # confirm; see above
```

If you kept the repository's app name, deploy with `fly deploy -a <your-app>`, which overrides it.

## Deploy

```bash
fly deploy
```

Builds on remote builders, so no local Docker daemon is needed.

## Verify

Do not consider it deployed until all three pass:

1. `fly logs` shows at least one completed `run_sync` line. The first sync replays your app's full
   history and takes a few minutes.
2. `curl https://<your-host>/healthz` returns `{"status": "ok"}`.
3. `/customers` renders after signing in.

Then run the invariants against the live database. This is the check that catches a bad deploy the
health check cannot see:

```bash
DATABASE_URL='postgres://...' uv run python scripts/check_invariants.py
```

If Postgres is not directly reachable, tunnel to it first (`fly proxy 15432:5432 -a <db-app>`) and
point `DATABASE_URL` at the local end.

## After a change to derivation or a widened query

`sync_state.cursor` persists, so a normal poll only fetches events newer than the cursor. Any change
that widens the GraphQL query or corrects a stored value needs history replayed, or the deploy will
appear to do nothing:

```sql
update sync_state set cursor = null where source = 'partner_api';
```

Restart the app afterwards; the scheduler syncs at boot. Safe and repeatable: `raw_app_events`
dedupes on its unique key, derivation is idempotent, and Slack does not re-alert because replayed
events keep their existing `app_events.id`.

## Backfilling country and industry

The Partner API exposes neither. If you have a CSV export from a vendor that did:

```bash
python -m partner_dashboard.import_shops_csv shops path/to/export.csv
```

Retitle `COLUMN_MAP` in `src/partner_dashboard/import_shops_csv.py` to match your export's header first. It is
update-only and never touches install state, so it is safe to re-run.
