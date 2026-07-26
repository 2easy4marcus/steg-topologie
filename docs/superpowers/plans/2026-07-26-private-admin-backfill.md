# Private Admin Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure browser-admin session and an accessible UI button that confirms, starts, and monitors the existing historical backfill without exposing application secrets.

**Architecture:** Put stateless session signing, expiry, origin validation, and bounded login throttling in a focused `app/admin_auth.py` module. Add small admin endpoints to FastAPI that translate an authenticated session into the existing background backfill call, while a new static admin page reads only sanitized progress and never stores a secret.

**Tech Stack:** Python 3, FastAPI, Pydantic, standard-library HMAC/SHA-256, vanilla HTML/CSS/JavaScript, pytest, BeautifulSoup, Playwright

---

### Task 1: Add stateless admin session signing

**Files:**
- Create: `app/admin_auth.py`
- Create: `tests/test_admin_auth.py`

- [ ] **Step 1: Write failing session tests**

```python
import pytest

from app import admin_auth


def test_session_round_trip_and_expiry():
    token = admin_auth.create_session_token(
        "admin-secret", now=1_000, ttl_seconds=3_600
    )

    assert admin_auth.verify_session_token(
        token, "admin-secret", now=4_599
    )
    assert not admin_auth.verify_session_token(
        token, "admin-secret", now=4_600
    )


def test_session_rejects_tampering_and_secret_rotation():
    token = admin_auth.create_session_token(
        "first-secret", now=1_000, ttl_seconds=3_600
    )

    assert not admin_auth.verify_session_token(
        token + "x", "first-secret", now=1_001
    )
    assert not admin_auth.verify_session_token(
        token, "replacement-secret", now=1_001
    )


@pytest.mark.parametrize("token", ["", "invalid", "1.2.invalid", "x.2.sig"])
def test_malformed_session_is_rejected(token):
    assert not admin_auth.verify_session_token(
        token, "admin-secret", now=1_000
    )
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest tests/test_admin_auth.py -v
```

Expected: collection fails because `app.admin_auth` does not exist.

- [ ] **Step 3: Implement the minimal signing module**

Create `app/admin_auth.py`:

```python
"""Authentication primitives for the private browser admin surface."""

import hashlib
import hmac


SESSION_TTL_SECONDS = 60 * 60


def _signature(payload: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def create_session_token(
    secret: str, *, now: int, ttl_seconds: int = SESSION_TTL_SECONDS
) -> str:
    payload = f"{now}.{now + ttl_seconds}"
    return f"{payload}.{_signature(payload, secret)}"


def verify_session_token(token: str, secret: str, *, now: int) -> bool:
    try:
        issued_text, expires_text, supplied_signature = token.split(".")
        issued_at = int(issued_text)
        expires_at = int(expires_text)
    except (AttributeError, TypeError, ValueError):
        return False
    if issued_at > now or expires_at <= now or expires_at <= issued_at:
        return False
    payload = f"{issued_at}.{expires_at}"
    expected_signature = _signature(payload, secret)
    return hmac.compare_digest(supplied_signature, expected_signature)
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
pytest tests/test_admin_auth.py -v
```

Expected: all session tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/admin_auth.py tests/test_admin_auth.py
git commit -m "feat: add signed admin sessions"
```

---

### Task 2: Add bounded login throttling and origin validation

**Files:**
- Modify: `app/admin_auth.py`
- Modify: `tests/test_admin_auth.py`

- [ ] **Step 1: Add failing tests for same-origin checks and throttling**

Append:

```python
from fastapi import HTTPException


def test_matching_origin_is_accepted():
    admin_auth.require_same_origin(
        origin="https://steg-topologie.onrender.com",
        host="steg-topologie.onrender.com",
    )


@pytest.mark.parametrize(
    "origin",
    [None, "https://attacker.example", "null", "file://local"],
)
def test_missing_or_cross_site_origin_is_rejected(origin):
    with pytest.raises(HTTPException) as exc:
        admin_auth.require_same_origin(
            origin=origin, host="steg-topologie.onrender.com"
        )
    assert exc.value.status_code == 403


def test_login_limiter_allows_five_attempts_then_blocks():
    limiter = admin_auth.LoginRateLimiter(
        max_attempts=5, window_seconds=60, max_clients=100
    )

    for second in range(5):
        assert limiter.allow("client-a", now=second)
    assert not limiter.allow("client-a", now=5)
    assert limiter.allow("client-a", now=61)


def test_login_limiter_bounds_client_state():
    limiter = admin_auth.LoginRateLimiter(
        max_attempts=5, window_seconds=60, max_clients=2
    )

    assert limiter.allow("client-a", now=0)
    assert limiter.allow("client-b", now=1)
    assert limiter.allow("client-c", now=2)
    assert len(limiter.attempts) <= 2
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
pytest tests/test_admin_auth.py -v
```

Expected: failures report missing `require_same_origin` and
`LoginRateLimiter`.

- [ ] **Step 3: Add the minimal validation and limiter implementation**

Add these imports and definitions to `app/admin_auth.py`:

```python
from collections import OrderedDict, deque
from urllib.parse import urlsplit

from fastapi import HTTPException


def require_same_origin(*, origin: str | None, host: str | None) -> None:
    if not origin or not host:
        raise HTTPException(status_code=403, detail="Invalid request origin")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != host:
        raise HTTPException(status_code=403, detail="Invalid request origin")


class LoginRateLimiter:
    def __init__(
        self, *, max_attempts: int, window_seconds: int, max_clients: int
    ):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self.attempts = OrderedDict()

    def allow(self, client: str, *, now: int) -> bool:
        values = self.attempts.pop(client, deque())
        cutoff = now - self.window_seconds
        while values and values[0] <= cutoff:
            values.popleft()
        allowed = len(values) < self.max_attempts
        values.append(now)
        self.attempts[client] = values
        while len(self.attempts) > self.max_clients:
            self.attempts.popitem(last=False)
        return allowed

    def clear(self) -> None:
        self.attempts.clear()
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
pytest tests/test_admin_auth.py -v
```

Expected: all signing, origin, and limiter tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/admin_auth.py tests/test_admin_auth.py
git commit -m "feat: protect admin login boundary"
```

---

### Task 3: Add admin login, session, and logout endpoints

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_admin_endpoints.py`

- [ ] **Step 1: Write failing endpoint tests**

Create `tests/test_admin_endpoints.py`:

```python
from fastapi.testclient import TestClient

from app import admin_auth, main


ORIGIN = "https://testserver"


def _client():
    return TestClient(main.app, base_url=ORIGIN)


def _headers():
    return {"Origin": ORIGIN, "Content-Type": "application/json"}


def test_login_is_unavailable_without_admin_secret(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SECRET", None)
    response = _client().post(
        "/api/admin/login",
        json={"secret": "anything"},
        headers=_headers(),
    )
    assert response.status_code == 503


def test_login_rejects_wrong_secret_generically(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SECRET", "correct-secret")
    response = _client().post(
        "/api/admin/login",
        json={"secret": "wrong-secret"},
        headers=_headers(),
    )
    assert response.status_code == 401
    assert "wrong-secret" not in response.text
    assert "correct-secret" not in response.text


def test_login_sets_secure_cookie_and_session_endpoint_accepts_it(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SECRET", "correct-secret")
    client = _client()

    login = client.post(
        "/api/admin/login",
        json={"secret": "correct-secret"},
        headers=_headers(),
    )
    session = client.get("/api/admin/session")

    assert login.status_code == 200
    cookie = login.headers["set-cookie"]
    assert "admin_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=3600" in cookie
    assert session.json() == {"authenticated": True}


def test_logout_clears_session(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SECRET", "correct-secret")
    client = _client()
    client.post(
        "/api/admin/login",
        json={"secret": "correct-secret"},
        headers=_headers(),
    )

    logout = client.post(
        "/api/admin/logout", json={}, headers=_headers()
    )

    assert logout.status_code == 200
    assert logout.json() == {"status": "logged_out"}
    assert client.get("/api/admin/session").status_code == 401


def test_admin_mutations_require_json_and_matching_origin(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SECRET", "correct-secret")
    client = _client()

    assert client.post(
        "/api/admin/login",
        json={"secret": "correct-secret"},
        headers={"Origin": "https://attacker.example"},
    ).status_code == 403
    assert client.post(
        "/api/admin/login",
        content="secret=correct-secret",
        headers={"Origin": ORIGIN, "Content-Type": "text/plain"},
    ).status_code == 415
```

- [ ] **Step 2: Run the endpoint tests and verify RED**

Run:

```bash
pytest tests/test_admin_endpoints.py -v
```

Expected: requests return 404 because the admin routes do not exist.

- [ ] **Step 3: Add models, dependencies, and endpoints**

In `app/main.py`, import `time`, `Cookie`, `Request`, and `admin_auth`. Add:

```python
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
ADMIN_COOKIE = "admin_session"
admin_login_limiter = admin_auth.LoginRateLimiter(
    max_attempts=5, window_seconds=60, max_clients=1_000
)


class AdminLoginIn(BaseModel):
    secret: str


def require_admin_json_origin(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0] != (
        "application/json"
    ):
        raise HTTPException(status_code=415, detail="JSON required")
    admin_auth.require_same_origin(
        origin=request.headers.get("origin"),
        host=request.headers.get("host"),
    )


def require_admin_session(
    admin_session: str | None = Cookie(None),
):
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=503, detail="Admin access unavailable"
        )
    if not admin_session or not admin_auth.verify_session_token(
        admin_session, ADMIN_SECRET, now=int(time.time())
    ):
        raise HTTPException(status_code=401, detail="Authentication required")


@app.post(
    "/api/admin/login",
    dependencies=[Depends(require_admin_json_origin)],
)
def admin_login(payload: AdminLoginIn, request: Request, response: Response):
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=503, detail="Admin access unavailable"
        )
    client = request.client.host if request.client else "unknown"
    if not admin_login_limiter.allow(client, now=int(time.time())):
        raise HTTPException(status_code=429, detail="Too many attempts")
    if not hmac.compare_digest(payload.secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = admin_auth.create_session_token(
        ADMIN_SECRET, now=int(time.time())
    )
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=admin_auth.SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return {"status": "authenticated"}


@app.get(
    "/api/admin/session",
    dependencies=[Depends(require_admin_session)],
)
def admin_session_status():
    return {"authenticated": True}


@app.post(
    "/api/admin/logout",
    dependencies=[
        Depends(require_admin_json_origin),
        Depends(require_admin_session),
    ],
)
def admin_logout(response: Response):
    response.delete_cookie(
        ADMIN_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
    )
    return {"status": "logged_out"}
```

Reset `admin_login_limiter` in an autouse fixture inside
`tests/test_admin_endpoints.py` so attempts do not leak between tests:

```python
import pytest


@pytest.fixture(autouse=True)
def reset_admin_limiter():
    main.admin_login_limiter.clear()
```

- [ ] **Step 4: Add and verify rate-limit and redaction tests**

Append:

```python
def test_login_rate_limit_returns_429(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SECRET", "correct-secret")
    client = _client()
    for _ in range(5):
        assert client.post(
            "/api/admin/login",
            json={"secret": "wrong"},
            headers=_headers(),
        ).status_code == 401
    assert client.post(
        "/api/admin/login",
        json={"secret": "wrong"},
        headers=_headers(),
    ).status_code == 429


def test_request_log_does_not_include_login_body_or_secrets(
    monkeypatch, capsys
):
    monkeypatch.setattr(main, "ADMIN_SECRET", "correct-secret")
    _client().post(
        "/api/admin/login",
        json={"secret": "submitted-secret"},
        headers=_headers(),
    )
    output = capsys.readouterr().out
    assert "submitted-secret" not in output
    assert "correct-secret" not in output
```

Run:

```bash
pytest tests/test_admin_auth.py tests/test_admin_endpoints.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_admin_endpoints.py
git commit -m "feat: add private admin authentication"
```

---

### Task 4: Add the authenticated backfill launch endpoint

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_admin_endpoints.py`

- [ ] **Step 1: Write failing launch tests**

Append:

```python
def _login(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SECRET", "correct-secret")
    response = client.post(
        "/api/admin/login",
        json={"secret": "correct-secret"},
        headers=_headers(),
    )
    assert response.status_code == 200


def test_admin_backfill_requires_session():
    response = _client().post(
        "/api/admin/backfill", json={}, headers=_headers()
    )
    assert response.status_code in {401, 503}


def test_admin_can_start_backfill(monkeypatch):
    client = _client()
    _login(client, monkeypatch)
    scheduled = []
    monkeypatch.setattr(
        main.backfill_official,
        "run_backfill_and_track_status",
        lambda: scheduled.append(True),
    )
    monkeypatch.setattr(
        main.backfill_official,
        "get_status",
        lambda: {"running": False},
    )
    monkeypatch.setattr(
        main.db, "latest_ingestion_run", lambda job_type: None
    )

    response = client.post(
        "/api/admin/backfill", json={}, headers=_headers()
    )

    assert response.status_code == 200
    assert response.json() == {"status": "started"}
    assert scheduled == [True]


def test_admin_backfill_reports_existing_run(monkeypatch):
    client = _client()
    _login(client, monkeypatch)
    monkeypatch.setattr(
        main.backfill_official,
        "get_status",
        lambda: {"running": True},
    )

    response = client.post(
        "/api/admin/backfill", json={}, headers=_headers()
    )

    assert response.json() == {"status": "already_running"}
```

- [ ] **Step 2: Run the launch tests and verify RED**

Run:

```bash
pytest tests/test_admin_endpoints.py -v
```

Expected: launch tests receive 404.

- [ ] **Step 3: Implement the minimal authenticated launch route**

Add to `app/main.py`:

```python
@app.post(
    "/api/admin/backfill",
    dependencies=[
        Depends(require_admin_json_origin),
        Depends(require_admin_session),
    ],
)
def admin_backfill(background_tasks: BackgroundTasks):
    in_process = backfill_official.get_status().get("running", False)
    latest = db.latest_ingestion_run("backfill")
    persisted_running = latest is not None and latest["status"] == "running"
    if in_process or persisted_running:
        return {"status": "already_running"}
    background_tasks.add_task(
        backfill_official.run_backfill_and_track_status
    )
    return {"status": "started"}
```

- [ ] **Step 4: Verify admin and legacy cron behavior**

Run:

```bash
pytest tests/test_admin_endpoints.py tests/test_app_internal_endpoints.py -v
```

Expected: all tests pass, including the existing cron-protected endpoint
tests.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_admin_endpoints.py
git commit -m "feat: launch backfill from admin session"
```

---

### Task 5: Build the accessible private admin page

**Files:**
- Create: `static/admin.html`
- Modify: `static/ops.html`
- Create: `tests/test_admin_page.py`

- [ ] **Step 1: Write failing static-page tests**

Create `tests/test_admin_page.py`:

```python
from pathlib import Path

from bs4 import BeautifulSoup


ADMIN_PAGE = Path("static/admin.html")


def test_admin_page_has_login_controls_and_authenticated_controls():
    soup = BeautifulSoup(ADMIN_PAGE.read_text(), "html.parser")
    assert soup.find("main")
    assert soup.find("form", id="login-form")
    assert soup.find("input", attrs={"type": "password"})
    assert soup.find("button", id="launch-backfill")
    assert soup.find("button", id="logout")
    assert soup.find(attrs={"aria-live": "polite"})


def test_admin_page_uses_only_admin_session_and_public_progress_apis():
    html = ADMIN_PAGE.read_text()
    assert "/api/admin/login" in html
    assert "/api/admin/session" in html
    assert "/api/admin/backfill" in html
    assert "/api/admin/logout" in html
    assert "/api/status/ingestion" in html
    assert "CRON_SECRET" not in html
    assert "OPS_SECRET" not in html
    assert "ADMIN_SECRET" not in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html


def test_admin_page_confirms_launch_and_adapts_polling():
    html = ADMIN_PAGE.read_text()
    assert "window.confirm" in html
    assert "5000" in html
    assert "60000" in html
    assert "launch.disabled" in html


def test_admin_page_has_accessibility_and_responsive_states():
    html = ADMIN_PAGE.read_text()
    for state in (
        "loading", "empty", "running", "completed", "failed",
        "expired", "network-error",
    ):
        assert state in html
    assert ":focus-visible" in html
    assert "@media (max-width:" in html
    assert "@media (prefers-reduced-motion: reduce)" in html
    assert 'dir="auto"' in html


def test_public_ops_page_links_to_admin_without_embedding_controls():
    soup = BeautifulSoup(
        Path("static/ops.html").read_text(), "html.parser"
    )
    assert soup.find("a", href="/admin.html")
    assert not soup.find(id="launch-backfill")
    assert not soup.find("input", attrs={"type": "password"})
```

- [ ] **Step 2: Run the page tests and verify RED**

Run:

```bash
pytest tests/test_admin_page.py -v
```

Expected: tests fail because `static/admin.html` does not exist.

- [ ] **Step 3: Create the page structure and styles**

Create `static/admin.html` using the color, spacing, navigation, card, mobile,
focus, and reduced-motion patterns already used by `static/ops.html`. Include:

```html
<main>
  <div id="announcer" aria-live="polite" data-state="loading">
    Vérification de la session…
  </div>

  <section id="login-view" hidden>
    <form id="login-form">
      <label for="admin-secret">Secret administrateur</label>
      <input id="admin-secret" type="password" required autocomplete="current-password">
      <button type="submit">Se connecter</button>
    </form>
  </section>

  <section id="control-view" hidden>
    <button id="launch-backfill" type="button">Lancer le backfill</button>
    <button id="logout" type="button">Se déconnecter</button>
    <article aria-labelledby="backfill-heading">
      <h2 id="backfill-heading">État du backfill</h2>
      <div id="backfill-status" data-state="empty" dir="auto"></div>
    </article>
  </section>
</main>
```

Add an “Administration” link to `static/ops.html`; do not add controls or a
password field to the public page.

- [ ] **Step 4: Implement login, confirmation, progress, expiry, and logout**

Add page-local JavaScript with these explicit behaviors:

```javascript
const ACTIVE_POLL_MS = 5000;
const IDLE_POLL_MS = 60000;
const loginView = document.getElementById("login-view");
const controlView = document.getElementById("control-view");
const launch = document.getElementById("launch-backfill");
const announcer = document.getElementById("announcer");
let pollTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
  });
  if (response.status === 401) {
    showLogin("expired");
    throw new Error("expired");
  }
  if (!response.ok) throw new Error("network-error");
  return response.json();
}

function showLogin(state = "empty") {
  loginView.hidden = false;
  controlView.hidden = true;
  announcer.dataset.state = state;
}

function showControls() {
  loginView.hidden = true;
  controlView.hidden = false;
}

document.getElementById("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("admin-secret");
  const secret = input.value;
  input.value = "";
  try {
    await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({secret}),
    });
    showControls();
    await refreshProgress();
  } catch (error) {
    showLogin(error.message === "expired" ? "expired" : "failed");
  }
});

launch.addEventListener("click", async () => {
  if (!window.confirm(
    "Le backfill peut durer plusieurs minutes et interroger STEG de façon répétée. Continuer ?"
  )) return;
  launch.disabled = true;
  try {
    await api("/api/admin/backfill", {
      method: "POST", body: "{}",
    });
    await refreshProgress();
  } catch (error) {
    launch.disabled = false;
    announcer.dataset.state = "network-error";
  }
});
```

Complete the script with text-only progress rendering, adaptive polling,
initial session checking, and logout:

```javascript
function addField(list, label, value) {
  const term = document.createElement("dt");
  const detail = document.createElement("dd");
  term.textContent = label;
  detail.textContent = value === null || value === undefined ? "—" : String(value);
  detail.dir = "auto";
  list.append(term, detail);
}

function renderProgress(backfill) {
  const target = document.getElementById("backfill-status");
  target.replaceChildren();
  const state = backfill?.status || "empty";
  target.dataset.state = state;
  const list = document.createElement("dl");
  [
    ["État", state],
    ["Pages", backfill?.pages_scanned],
    ["Liens", backfill?.links_discovered],
    ["Importés", backfill?.notices_imported],
    ["Inchangés", backfill?.notices_unchanged],
    ["Ignorés", backfill?.notices_skipped],
    ["Échecs", backfill?.notices_failed],
    ["Début", backfill?.started_at],
    ["Dernière progression", backfill?.last_progress_at],
    ["Fin", backfill?.finished_at],
    ["Code d’erreur", backfill?.public_error_code],
    ["Identifiant de requête", backfill?.request_id],
  ].forEach(([label, value]) => addField(list, label, value));
  target.append(list);
  return state;
}

async function refreshProgress() {
  try {
    const response = await fetch("/api/status/ingestion");
    if (!response.ok) throw new Error("network-error");
    const payload = await response.json();
    const state = renderProgress(payload.backfill);
    const running = state === "running";
    launch.disabled = running;
    announcer.dataset.state = running ? "running" : state;
    announcer.textContent = running
      ? "Le backfill est en cours."
      : "État du backfill actualisé.";
    clearTimeout(pollTimer);
    pollTimer = setTimeout(
      refreshProgress, running ? ACTIVE_POLL_MS : IDLE_POLL_MS
    );
  } catch (error) {
    announcer.dataset.state = "network-error";
    announcer.textContent = "Impossible de charger la progression.";
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refreshProgress, IDLE_POLL_MS);
  }
}

document.getElementById("logout").addEventListener("click", async () => {
  try {
    await api("/api/admin/logout", {method: "POST", body: "{}"});
  } finally {
    clearTimeout(pollTimer);
    showLogin("empty");
  }
});

(async function boot() {
  try {
    await api("/api/admin/session");
    showControls();
    await refreshProgress();
  } catch (error) {
    showLogin(error.message === "expired" ? "expired" : "network-error");
  }
})();
```

- [ ] **Step 5: Verify static behavior**

Run:

```bash
pytest tests/test_admin_page.py tests/test_ops_page.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Add rendered browser checks**

Extend `tests/test_accessibility.py` with a Playwright check that opens
`static/admin.html`, sets a 390-pixel viewport, and verifies:

```python
assert page.locator("main").count() == 1
assert page.locator("#login-form").count() == 1
assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
```

Run:

```bash
pytest tests/test_accessibility.py -v
```

Expected inside the restricted sandbox: static checks pass and the rendered
test may skip only when Chromium IPC is unavailable. Rerun this command with
browser permission before completion and require all tests to pass.

- [ ] **Step 7: Commit**

```bash
git add static/admin.html static/ops.html \
  tests/test_admin_page.py tests/test_accessibility.py
git commit -m "feat: add private backfill control page"
```

---

### Task 6: Document deployment and secret rotation

**Files:**
- Modify: `DEPLOYMENT.md`
- Modify: `docs/OPERATIONS.md`

- [ ] **Step 1: Add the deployment configuration**

Add `ADMIN_SECRET` to Render setup:

```text
ADMIN_SECRET=<independent-random-secret>
```

State that it must differ from `CRON_SECRET` and `OPS_SECRET`, exists only in
Render and the operator’s password manager, and is never added to GitHub
Actions or frontend code.

- [ ] **Step 2: Add the admin workflow**

Document:

1. Open `/admin.html`.
2. Sign in with `ADMIN_SECRET`.
3. Select “Launch backfill.”
4. Confirm the operation.
5. Monitor the sanitized progress card.
6. Log out.

Include response meanings for 401, 403, 429, 503, and
`already_running`. Explain that rotating `ADMIN_SECRET` immediately
invalidates existing signed sessions after the Render service restarts.

- [ ] **Step 3: Verify documentation contains no actual secret**

Run:

```bash
rg -n "ADMIN_SECRET|admin.html|429|already_running" \
  DEPLOYMENT.md docs/OPERATIONS.md
git diff --check
```

Expected: documented placeholders and workflow are present; no whitespace
errors.

- [ ] **Step 4: Commit**

```bash
git add DEPLOYMENT.md docs/OPERATIONS.md
git commit -m "docs: document private backfill control"
```

---

### Task 7: Run security and regression verification

**Files:**
- Verify only; no production changes expected

- [ ] **Step 1: Run focused security tests**

```bash
pytest tests/test_admin_auth.py tests/test_admin_endpoints.py \
  tests/test_admin_page.py tests/test_request_logging.py -v
```

Expected: all tests pass with no secret values in output.

- [ ] **Step 2: Confirm frontend secret boundaries**

```bash
! rg -n \
  "CRON_SECRET|OPS_SECRET|ADMIN_SECRET|X-Cron-Secret|X-Ops-Secret|localStorage|sessionStorage" \
  static
```

Expected: exit status 0 because no forbidden strings are found.

- [ ] **Step 3: Run the complete suite**

```bash
pytest -q
```

Expected: all tests pass; the browser-only accessibility test may skip in the
restricted sandbox.

- [ ] **Step 4: Run rendered accessibility verification with browser permission**

```bash
pytest tests/test_accessibility.py -v
```

Expected outside the restricted browser sandbox: all tests pass with no skip.

- [ ] **Step 5: Inspect final changes**

```bash
git diff --check
git status --short
git log --oneline -8
```

Expected: no uncommitted feature changes and the task commits appear in
order. Preserve unrelated user-owned files and staged changes.

- [ ] **Step 6: Finish the branch**

Use `superpowers:verification-before-completion`, then
`superpowers:finishing-a-development-branch`. Do not deploy, rotate secrets,
push, or merge without the user’s explicit choice.
