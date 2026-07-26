# Private Admin Backfill Design

## Goal

Provide an authenticated browser interface where an operator can launch and
monitor the historical STEG backfill without exposing `CRON_SECRET`,
`OPS_SECRET`, or `ADMIN_SECRET` to frontend code, storage, logs, or public
status responses.

## Scope

This feature adds:

- a private admin login page;
- a short-lived authenticated admin session;
- a confirmation-protected backfill button;
- live, sanitized backfill progress;
- logout;
- authentication, CSRF, rate-limit, accessibility, and secret-redaction
  tests.

It does not add user accounts, roles, password recovery, diagnostic event
viewing, scrape/recluster controls, or secret management through the browser.

## Configuration and secret boundaries

Render receives a new `ADMIN_SECRET` environment variable. It must be a
separate, randomly generated value and must not equal `CRON_SECRET` or
`OPS_SECRET`.

The admin login sends the entered secret once to the backend over HTTPS. The
page must not persist it in local storage, session storage, IndexedDB,
cookies, URLs, DOM attributes, or JavaScript variables after the login request
finishes. Request bodies are never logged.

The backend compares the supplied value with `ADMIN_SECRET` using a
constant-time comparison. `CRON_SECRET` remains exclusive to automation and
never reaches the browser.

## Session design

Successful login returns a stateless, signed session cookie. The payload
contains only an issued-at timestamp and expiry timestamp. It is authenticated
with an HMAC derived from `ADMIN_SECRET`; it contains no secret or personally
identifying information.

The cookie:

- is named `admin_session`;
- is `HttpOnly`;
- is `Secure`;
- uses `SameSite=Strict`;
- has path `/`;
- expires after one hour.

Rotating `ADMIN_SECRET` invalidates existing sessions. Logout clears the
cookie. The server returns 503 when `ADMIN_SECRET` is missing, 401 for invalid
credentials, and 401 for missing, invalid, or expired sessions. Authentication
errors use generic messages.

Login attempts are rate-limited to five attempts per minute per effective
client address. Rate-limit state is bounded and held in process memory; it is
best-effort across Render restarts and multiple instances. A rate-limited
request returns 429 without revealing whether the supplied secret was close or
valid.

## CSRF and request validation

Every state-changing admin request requires:

- a valid `admin_session` cookie;
- `Content-Type: application/json`;
- an `Origin` header matching the request origin.

`SameSite=Strict` is an additional defense, not the sole CSRF control. Login,
backfill, and logout reject mismatched or absent origins in production. Tests
may use an explicitly configured trusted origin.

## API

### `POST /api/admin/login`

Request:

```json
{"secret":"operator-entered-value"}
```

On success, sets the session cookie and returns:

```json
{"status":"authenticated"}
```

The secret is excluded from logs and error responses.

### `GET /api/admin/session`

Returns `{"authenticated":true}` for a valid session. Otherwise it returns
401. The admin page uses this endpoint to decide whether to show the login or
control view.

### `POST /api/admin/backfill`

Requires a valid session and matching origin. It delegates to the existing
background backfill mechanism and returns:

```json
{"status":"started"}
```

If the persistent or in-process job state indicates a backfill is active, it
returns:

```json
{"status":"already_running"}
```

No cron secret is used by or returned to the frontend.

### `POST /api/admin/logout`

Clears the session cookie and returns:

```json
{"status":"logged_out"}
```

## Admin interface

`/admin.html` follows the existing visual system and links back to the tracker,
model, and public pipeline pages.

When unauthenticated, it shows a labeled password field and a “Sign in”
button. It uses an accessible generic error message for failed login.

When authenticated, it shows:

- a “Launch backfill” button;
- a logout button;
- the current or latest backfill state;
- pages scanned;
- links discovered;
- notices imported, unchanged, skipped, and failed;
- start, last-progress, and finish timestamps;
- safe public error code;
- request ID.

Selecting “Launch backfill” opens a native confirmation dialog explaining
that the operation may run for several minutes and contact STEG repeatedly.
Only confirmation sends the request. The launch button remains disabled while
the job is running.

The page polls the public `/api/status/ingestion` endpoint every five seconds
while a backfill runs and every 60 seconds while idle. It uses text content,
not raw HTML, for API-derived values. Loading, empty, running, completed,
failed, expired-session, and network-error states are announced through an
`aria-live` region. The layout supports keyboard use, mobile widths, visible
focus, and reduced motion.

The public `/ops.html` remains sanitized and receives no login form or admin
controls.

## Logging and privacy

Structured request logs may contain request ID, route template, status,
duration, and safe job correlation identifiers. They must never contain:

- login request bodies;
- cookie values;
- `ADMIN_SECRET`, `OPS_SECRET`, or `CRON_SECRET`;
- authorization headers;
- citizen comments or raw STEG HTML.

Admin authentication failures log only a stable event name, request ID, status,
and bounded client-rate-limit metadata.

## Testing

Backend tests cover:

- 503 when `ADMIN_SECRET` is not configured;
- constant-time credential verification behavior;
- successful login and required cookie flags;
- generic 401 responses;
- session signature validation and one-hour expiry;
- invalidation after secret rotation;
- logout;
- five-attempt-per-minute rate limiting and bounded state;
- origin and JSON content-type enforcement;
- authenticated launch and already-running behavior;
- assurance that cron authentication is not involved in the admin endpoint;
- secret and request-body redaction from logs.

Frontend tests cover:

- unauthenticated and authenticated views;
- confirmation cancellation and acceptance;
- disabled launch control while running;
- five-second active and 60-second idle polling;
- loading, empty, completed, failed, expired-session, and network-error states;
- safe text rendering;
- keyboard, screen-reader, mobile, focus, and reduced-motion behavior;
- absence of all secret values, protected diagnostic calls, and secret
  persistence APIs from frontend source.

Existing cron endpoints and the public operations page retain their current
behavior.

## Deployment

1. Generate a new random `ADMIN_SECRET`.
2. Add it to the Render service environment without quotes or whitespace.
3. Deploy and verify that `/api/admin/session` returns 401 before login.
4. Sign in through `/admin.html`.
5. Launch a backfill, confirm progress appears, and verify logout.
6. Confirm the public pipeline page contains no admin controls.

Operators rotate `ADMIN_SECRET` independently of the cron and operations
secrets.
