# Usage events: your app → this dashboard

Hand this document to whoever writes your Shopify app. It is the contract for `POST /ingest/usage`,
the one route that accepts writes from outside.

**What the app has to do:** a POST, from its server, whenever a merchant does something worth
counting. A few event names, one endpoint, one header.

**Why:** the Partner API tells you a shop installed and that it pays. It has no idea whether anyone
ever set the app up. So a merchant who installs, never configures anything, and cancels in week
three is indistinguishable from one who used the app heavily and cancelled anyway. Those are
opposite problems with opposite fixes, and lifecycle data alone cannot tell them apart.

---

## The request

```
POST https://<your-dashboard-host>/ingest/usage
Content-Type: application/json
X-Usage-Token: <the shared secret>
```

```json
{
  "events": [
    {
      "event_id": "01J8ZQ7X4M9K2N",
      "shop_gid": "gid://shopify/Shop/12345678",
      "event_type": "offer_created",
      "occurred_at": "2026-08-12T14:03:11Z",
      "properties": { "offer_type": "bogo", "trigger": "cart_value" }
    }
  ]
}
```

Response is `200` with a count of what happened:

```json
{ "received": 1, "stored": 1, "duplicates": 0, "rate_limited": 0 }
```

## The events

The accepted event names are configured on the dashboard side via `USAGE_EVENT_TYPES`, and two of
them are singled out: `USAGE_ACTIVATION_EVENT` is what "activated" means, and `USAGE_LIVE_EVENT` is
what proves the app is actually running for shoppers. The defaults suit an app that lets merchants
build something a shopper then sees:

| `event_type` | Send it when |
| --- | --- |
| `settings_completed` | The merchant finishes onboarding, or saves app settings for the first time |
| `offer_created` | The merchant creates their first configured thing. **This is the activation event.** |
| `offer_impression` | That thing is shown to a shopper. **This is the live event.** |
| `offer_conversion` | A shopper acts on it |

Rename them to whatever your app actually does, set `USAGE_EVENT_TYPES` to match, and keep the
activation and live roles pointed at the right two. Anything outside the configured list is rejected
with `422` and nothing is stored, so the dashboard has to learn a new event name before the app
starts sending it.

The live event is the high-volume one. Batch it. If storefront-side counting is impractical, sending
one aggregated event per shop per hour with a count in `properties` is fine and still answers every
question the activation reports ask. Note which shape you chose so the reports are read correctly.

## Field rules

| Field | Required | Rules |
| --- | --- | --- |
| `event_id` | yes | Your idempotency key. Any string up to 200 chars, unique per shop. A UUID/ULID is ideal. |
| `shop_gid` | yes | The Shopify shop GID, e.g. `gid://shopify/Shop/12345678`. Not the myshopify domain. |
| `event_type` | yes | One of the configured names, exactly. |
| `occurred_at` | yes | ISO 8601 **with an offset** (`Z` or `+00:00`). When it happened, not when you sent it. Rejected if more than 5 minutes in the future. |
| `properties` | no | Flat JSON object. Max 25 keys, values must be strings/numbers/booleans (no nesting, no arrays), strings max 500 chars, whole object max 4 KB. |

`shop_gid` matters more than anything else here: it is the join key to every lifecycle and billing
fact the dashboard already holds. If the app has the myshopify domain and not the GID, say so early,
because mapping domains to GIDs is a per-shop lookup and sending the GID is much cheaper.

## Batching, retries, failures

- Up to **500 events** per request, **1 MB** total body.
- **Retries are free.** Ingestion is idempotent on `(shop_gid, event_id)`: resending an event you
  already sent stores nothing and reports it as a duplicate. A stored event is never overwritten,
  so a retry cannot corrupt anything either. If you time out and do not know whether it landed, just
  send it again.
- A batch is **all or nothing on validation**. One malformed event rejects the whole batch with a
  `4xx` and a message naming the index (`events[3].occurred_at is in the future`). Fix and resend.
- **Queue and retry on `5xx` or a network failure.** These are analytics events, so buffering for
  minutes or hours is fine, but do not drop them silently on the first failure.
- **Do not retry a `4xx`** (except `429` if you ever see one). A `4xx` means the payload is wrong
  and resending it unchanged will fail identically.

Status codes:

| Code | Meaning |
| --- | --- |
| `200` | Stored (see the counts in the body) |
| `401` | Missing or wrong token |
| `413` | Body over 1 MB, or batch over 500 events |
| `422` | Validation failure; body says which field |
| `5xx` | Dashboard-side problem. Queue and retry. |

There is also a per-shop flood ceiling of 20,000 events per rolling day. Past it, that shop's events
are dropped and reported back in `rate_limited` rather than failing the request. Normal traffic will
never come near it; a non-zero `rate_limited` means something is looping.

## The token

The dashboard operator sends the value for `X-Usage-Token` out of band. It is a shared secret, not a
Shopify credential and not tied to any user:

- Store it wherever the app keeps its other secrets (env var, hosting platform secret store).
  Never commit it, never put it in client-side code.
- **Server-side only.** These events must be posted from the app's backend. A token shipped to the
  storefront is a token anyone can read, and then anyone can write to the analytics.
- If it leaks, tell the operator and rotate it. Rotation is one secret change on the dashboard and
  one config change in the app; there is no migration.

## What this makes possible

Once events flow, these light up with no further app work:

- **Activation rate per install cohort:** what share of each month's installs fired the activation
  event within 48 hours and within 7 days. This is the number that says whether onboarding is the
  bottleneck.
- **Median time to activation**, and a count of merchants who installed and never activated.
- **"Paying, but gone quiet":** active subscribers whose live event has not fired in 14 days.
  Without usage data the closest available signal is "installed recently and has not subscribed",
  which is a guess.

Until events arrive, activation reports read **unknown, not 0%**. A shop that installed before
tracking started has no activation event to find, and reporting that as zero would be a lie.

## Checklist

- [ ] Token stored server-side
- [ ] Onboarding-complete event fires on first settings save
- [ ] Activation event fires on first creation
- [ ] Live event fires (per-event or hourly aggregate: say which)
- [ ] Conversion event fires
- [ ] `shop_gid` is the Shopify GID, not the domain
- [ ] `occurred_at` carries a timezone offset
- [ ] Failed batches are queued and retried on `5xx`, not on `4xx`
- [ ] One test batch sent and the `stored` count confirmed with the operator

Send one real batch when it is wired, and confirm it landed before rolling out to all shops.
