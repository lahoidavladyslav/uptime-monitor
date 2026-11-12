# Frontend SPA — Stage 1: API foundation, auth, dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working `frontend/` SPA where a user can sign up, confirm email, log in (with 2FA), reset a password and see a live dashboard — backed by a typed `/api/v1/` whose OpenAPI contract cannot drift from the frontend.

**Architecture:** A new Django app `api` exposes Django Ninja routers over the *existing* logic (shared services, not copies). The committed `api/openapi.json` is the contract; `openapi-typescript` generates `frontend/src/api/schema.d.ts` from it, and CI fails if either is stale. The SPA (React + TS + Vite) talks to the API with session cookies + CSRF on one origin (Vite proxy in dev). The old Django UI keeps working untouched.

**Tech Stack:** Django 5.2 / Python 3.12 / django-ninja 1.x (existing stack), React 18 + TypeScript strict + Vite, React Router, TanStack Query, Tailwind + shadcn/ui + lucide-react, openapi-typescript + openapi-fetch, react-hook-form + zod, Vitest + Testing Library + MSW.

**Spec:** `docs/superpowers/specs/2026-11-12-frontend-spa-design.md` (stage 1 = §7.8 item 1). Stages 2–4 (monitor page, forms/status pages/reports/2FA settings, public pages + prod nginx + Playwright) are separate plans.

## Global Constraints

- Backend: Django 5.2, Python 3.12, Postgres, Celery/Redis — no architecture changes. Node 20 LTS (machine has v20.19.4), npm, versions pinned by `package-lock.json`.
- API prefix `/api/v1/`; OpenAPI at `/api/v1/openapi.json`; framework Django Ninja.
- Auth = **session cookies + CSRF** (`csrftoken` cookie → `X-CSRFToken` header), one origin. **No CORS, no tokens in `localStorage`.** Session cookie `SameSite=Lax`, `HttpOnly`; CSRF cookie is JS-readable.
- `GET /api/v1/auth/csrf` (uses `ensure_csrf_cookie`) is called by the SPA at start.
- One validation-error format for every endpoint: HTTP `422` with `{ "errors": { "<field>": ["..."] }, "message": "..." }`. Non-field errors use the key `"__all__"`.
- Pagination shape (used from stage 2): `?page=&page_size=` (≤100) → `{ items, total, page, pages }`.
- Another user's object id → `404`. Anonymous request to a protected endpoint → `401 {message}`.
- Sensitive flows (resend confirmation, password reset) answer **identically** for existing and non-existing accounts.
- 2FA rules stay exactly as today: ≤5 attempts, 5-minute window, TOTP code one-time (replay-protected), recovery codes one-time, `next` never redirects off-site.
- Frontend: React 18 (not 19), TypeScript `strict`, Tailwind + shadcn/ui (components copied into the repo), lucide icons, TanStack Query for data, react-hook-form + zod for forms with **error messages taken from the API response**.
- Types are generated (`npm run gen:api`), never hand-written; CI check: regenerate → `git diff --exit-code`.
- UI language Ukrainian, all strings in one module `src/lib/strings.ts`. Dates shown in the browser time zone; backend stays UTC.
- Design tokens: font Inter (system fallback), radius 10 px, 1 px borders, almost no shadows; accent indigo `#4F46E5` (light) / `#818CF8` (dark); statuses up=green, degraded=amber, down=red, maintenance=blue, unknown=grey; **colour is never the only carrier of meaning** (always icon + text); contrast WCAG AA; works from 360 px.
- Dashboard polling: every 10 s, only while the tab is visible.
- Dependabot covers npm; `npm audit --audit-level=high` runs in CI.
- Old Django templates/views/tests stay and must stay green (`pytest` currently: 187 passed).

## Review Focus

- **Session expires while the dashboard is polling** → the next poll gets 401: the app must go to the login page once, with no retry storm and no half-broken table. (Task 11)
- **CSRF token rotates on login** (Django rotates it) → the client must read the cookie at request time, not cache it; first POST after login must succeed. (Task 6)
- **Proxy answers with HTML/empty body** (502 from nginx, dev server down) instead of JSON → error normaliser yields a friendly message, never `Unexpected token <`. (Task 6)
- **Brand-new user / monitors with no checks yet** → `uptime_24h: null`, empty sparkline, UI shows «—» and an empty state, never `NaN%` or an empty chart that looks broken. (Tasks 4, 11)
- **Very long / emoji / right-to-left monitor names** in a dashboard row → truncated with ellipsis, row layout and badge intact. (Task 11)

## File Structure

Backend (new unless marked):
- `api/__init__.py`, `api/apps.py` — app config.
- `api/api.py` — `NinjaAPI` instance, exception handlers, mounted routers.
- `api/errors.py` — `ApiError`, `field_errors_from_django(form_errors) -> dict[str, list[str]]`.
- `api/deps.py` — `session_auth`, `enforce_csrf(request)`.
- `api/schemas.py` — shared Pydantic schemas (`ErrorOut`, `UserOut`, `StatusOut`).
- `api/routers/auth.py`, `api/routers/dashboard.py`.
- `api/management/commands/export_openapi.py` — writes `api/openapi.json`.
- `api/openapi.json` — committed contract snapshot.
- `api/tests/` — API tests.
- `accounts/services.py` — shared auth logic (extracted; `accounts/views.py` modified to use it).
- `monitors/services.py` (modify) — `dashboard_rows`, `sparklines`; `monitors/views.py` (modify) — `DashboardView` uses `dashboard_rows`.
- `config/settings.py`, `config/urls.py`, `requirements.txt` (modify).

Frontend (`frontend/`): `package.json`, `vite.config.ts`, `tsconfig*.json`, `index.html`, `src/main.tsx`, `src/app/{router.tsx,providers.tsx}`, `src/api/{client.ts,errors.ts,schema.d.ts(generated)}`, `src/lib/{csrf.ts,strings.ts,format.ts}`, `src/components/{ThemeProvider.tsx,AppShell.tsx,StatusBadge.tsx,Sparkline.tsx,States.tsx}`, `src/components/ui/*` (shadcn), `src/features/auth/*`, `src/features/dashboard/*`, `src/test/{setup.ts,server.ts}`.

---

### Task 1: API skeleton, error format, CSRF, `me`

**Files:**
- Create: `api/__init__.py`, `api/apps.py`, `api/api.py`, `api/errors.py`, `api/deps.py`, `api/schemas.py`, `api/routers/__init__.py`, `api/routers/auth.py`, `api/tests/__init__.py`, `api/tests/test_skeleton.py`
- Modify: `requirements.txt` (add `django-ninja>=1.3,<2`), `config/settings.py` (add `"api"` to `INSTALLED_APPS`; `FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")`), `config/urls.py` (`path("api/v1/", api.urls)`)

**Interfaces:**
- Produces: `api.api.api: NinjaAPI`; `api.errors.ApiError(status: int, message: str, errors: dict[str, list[str]] | None = None)`; `api.errors.field_errors_from_django(errors: ErrorDict) -> dict[str, list[str]]` (Django's `NON_FIELD_ERRORS` → `"__all__"`); `api.deps.session_auth` (Ninja auth: 401 `{message}` when anonymous, CSRF enforced on unsafe methods); `api.deps.enforce_csrf(request) -> None` (raises `ApiError(403, ...)` on failure — for *unauthenticated* POSTs such as login); schemas `ErrorOut{message: str, errors: dict[str, list[str]]}`, `UserOut{id: int, username: str, email: str, has_2fa: bool}`, `StatusOut{status: str}`; routes `GET /auth/csrf`, `GET /auth/me`.

- [ ] **Step 1: Write failing tests** in `api/tests/test_skeleton.py` (pytest-django `client`, `user` fixtures from root `conftest.py`):
  - `test_openapi_is_served`: `GET /api/v1/openapi.json` → 200, `paths` contains `/api/v1/auth/me`.
  - `test_me_anonymous_is_401_json`: 401, body `{"message": ...}` is JSON.
  - `test_me_returns_user`: after `client.force_login(user)` → `{"id": user.id, "username": "alice", "email": "alice@example.com", "has_2fa": False}`.
  - `test_csrf_endpoint_sets_cookie`: `GET /auth/csrf` → 200 and `"csrftoken" in response.cookies`.
  - `test_validation_error_uses_common_format`: register a throwaway route in the test via the API instance that raises `ApiError(422, "Невірні дані", {"name": ["Обов'язкове"]})` → body `{"message": "Невірні дані", "errors": {"name": ["Обов'язкове"]}}`; and a Ninja body-validation failure (missing field) is **also** `422` with `errors` keyed by field name and a `message`.
  - `test_unsafe_method_requires_csrf`: `Client(enforce_csrf_checks=True)`, logged in, `POST /auth/logout`-like protected route without header → 403; with `X-CSRFToken` from the cookie → not 403.
- [ ] **Step 2:** `pytest api/tests/test_skeleton.py -q` → FAIL (module/route missing).
- [ ] **Step 3: Implement** the files above. Register handlers for `ApiError` and Ninja `ValidationError` (map `errors[].loc` last element → field). `session_auth` = Ninja `SessionAuth` subclass returning the shared 401 body. `enforce_csrf` calls Django's `CsrfViewMiddleware.process_view` and raises `ApiError(403)` if it returns a response. Add a protected `POST /auth/logout` stub only if needed for the CSRF test; otherwise test against a route registered inside the test.
- [ ] **Step 4:** run the file → PASS; run `pytest -q` → all 187+ pass.
- [ ] **Step 5: Commit** `git add api config requirements.txt && git commit -m "feat(api): Ninja skeleton with unified errors and CSRF"`.

### Task 2: Extract shared auth logic into `accounts/services.py`

**Files:**
- Create: `accounts/services.py`, `accounts/tests/test_services.py`
- Modify: `accounts/views.py` (call the services; behaviour unchanged)

**Interfaces:**
- Produces (all in `accounts.services`):
  - `make_confirmation_token(user) -> str`; `confirm_token(token: str) -> User | None` (activates the user; `None` if bad/expired).
  - `send_confirmation_email(user, link: str) -> None`.
  - `resend_confirmation(email: str, link_for: Callable[[User], str]) -> None` (silent when no inactive user matches).
  - `has_two_factor(user) -> bool`.
  - `begin_two_factor(request, user, next_url: str) -> None` (stores the `pre_2fa` session state exactly as today).
  - `finish_two_factor(request, code: str) -> Literal["ok", "invalid", "expired", "locked"]` (enforces ≤5 tries / 5 min / replay / recovery code; on `"ok"` calls `login(...)` and clears state).
- Consumes: existing `TOTPDevice`, `_verify_totp` logic (move it, keep its tests green).

- [ ] **Step 1: Write failing tests** `accounts/tests/test_services.py`: token roundtrip activates user; tampered/expired token → `None`; `resend_confirmation` sends exactly one mail for an inactive user and none for unknown/active emails; `finish_two_factor` returns `"expired"` with no pending state, `"locked"` on the 6th attempt, `"invalid"` for a wrong code, `"ok"` for a valid code and `"invalid"` when the same code is replayed.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement** by *moving* the logic out of `accounts/views.py` (views become thin wrappers). Do not change any message or limit.
- [ ] **Step 4:** `pytest accounts -q` and `pytest -q` → all green (the 15+ existing accounts tests are the regression gate).
- [ ] **Step 5: Commit** `refactor(accounts): extract auth services shared by views and API`.

### Task 3: Auth API — signup, confirm, resend, login (+2FA), logout, password reset

**Files:**
- Modify: `api/routers/auth.py`, `api/api.py` (mount router), `accounts/services.py` (only if a missing helper is needed)
- Create: `api/tests/test_auth_api.py`

**Interfaces:**
- Consumes: Task 1 (`ApiError`, `field_errors_from_django`, `enforce_csrf`, `session_auth`, `StatusOut`), Task 2 services, existing `accounts.forms.SignupForm`, Django `PasswordResetForm`/`SetPasswordForm`/`default_token_generator`.
- Produces endpoints (all JSON, all unauthenticated POSTs call `enforce_csrf` first):
  - `POST /auth/signup` `{username, email, password1, password2}` → `201 {status: "confirmation_sent"}`; validation via `SignupForm` → `422` format. Email link = `f"{settings.FRONTEND_URL}/confirm/{token}"`.
  - `POST /auth/confirm` `{token}` → `200 {status: "confirmed"}`, bad/expired → `400 {message}`.
  - `POST /auth/resend` `{email}` → always `200 {status: "ok"}`.
  - `POST /auth/login` `{username, password, next?: str}` → `200 {status: "ok"}` (session created) or `200 {status: "2fa_required"}` (**no** session); wrong credentials or inactive → `422` with `errors: {"__all__": [msg]}` (use `accounts.forms.LoginForm` messages).
  - `POST /auth/login/2fa` `{code}` → `200 {status: "ok"}`; `invalid` → `422 {errors: {"code": ["Невірний код."]}}`; `expired`/`locked` → `401 {message}`.
  - `POST /auth/logout` (requires `session_auth`) → `200 {status: "ok"}`.
  - `POST /auth/password-reset` `{email}` → always `200 {status: "ok"}`; mail link `f"{FRONTEND_URL}/reset-password/{uid}/{token}"` (own email template `templates/registration/password_reset_spa_email.html`).
  - `POST /auth/password-reset/confirm` `{uid, token, new_password1, new_password2}` → `200 {status: "ok"}`; bad link `400 {message}`; weak password `422`.

- [ ] **Step 1: Write failing tests** (`Client(enforce_csrf_checks=True)` helper that fetches `/auth/csrf` first and sends `X-CSRFToken`), one per row below, asserting exactly:
  - signup: inactive user created, 1 mail whose body contains `"{FRONTEND_URL}/confirm/"`; duplicate email → `422` with `errors["email"]`; password mismatch → `422`.
  - confirm: token from the mail activates; garbage → 400.
  - resend: identical response body for existing-inactive vs unknown email; mail only for the former.
  - login: correct → `ok` and `/auth/me` works; wrong password → `422` `__all__`; unconfirmed account → `422`; user with 2FA → `2fa_required` and `/auth/me` is still 401; POST without CSRF → 403.
  - 2FA: valid code → `ok` and `/auth/me` works; wrong → `422` on `code`; six wrong tries → `401`; replay of a used code → `422`; recovery code works once.
  - **second login attempt while a 2FA challenge is pending replaces the pending state** (Review Focus): login as A (2FA) then as B (no 2FA) → B logged in, A's pending state gone; `/auth/login/2fa` now → `401`.
  - logout: ends the session; anonymous logout → `401`.
  - password reset: unknown and known email give identical bodies; known email gets one mail with `"{FRONTEND_URL}/reset-password/"`; confirm with a valid `uid/token` changes the password; reused token → 400; `next` containing `https://evil.example/` never appears in any response.
- [ ] **Step 2:** `pytest api/tests/test_auth_api.py -q` → FAIL.
- [ ] **Step 3: Implement** with Django forms for validation (no duplicated rules); map errors through `field_errors_from_django`. `login`: use `LoginForm(request, data=...)`; if `has_two_factor(user)` call `begin_two_factor` instead of `login()`; clear any previous `pre_2fa` before handling a new login.
- [ ] **Step 4:** run file → PASS; full `pytest -q` green.
- [ ] **Step 5: Commit** `feat(api): auth endpoints (signup, confirm, login with 2FA, reset)`.

### Task 4: Dashboard service + endpoint (no N+1)

**Files:**
- Modify: `monitors/services.py`, `monitors/views.py` (`DashboardView` → uses `dashboard_rows`)
- Create: `api/routers/dashboard.py`, `api/tests/test_dashboard_api.py`, `monitors/tests/test_dashboard_service.py`
- Modify: `api/api.py` (mount)

**Interfaces:**
- Produces in `monitors.services`:
  - `sparklines(monitor_ids: list[int], now: datetime | None = None) -> dict[int, list[int | None]]` — for each id exactly 24 values, oldest→newest, hourly average `response_time_ms` rounded, `None` for hours with no timing; **one query** for all ids.
  - `@dataclass DashboardRow(id, name, status, last_checked_at, uptime_24h, down_minutes, spark)` and `dashboard_rows(user, now: datetime | None = None) -> list[DashboardRow]`. `status ∈ {"up","down","unknown","maintenance","paused"}` (paused = `is_active=False`; maintenance wins over up/down; unknown = no checks yet). Reuses the existing annotated queryset logic currently in `DashboardView.get_queryset`.
- Produces endpoint `GET /api/v1/dashboard` (auth required) → `{ summary: {up, down, unknown, maintenance, paused, open_incidents: int}, monitors: [ {id, name, status, last_checked_at: datetime|null, uptime_24h: float|null, down_minutes: int|null, spark: (int|null)[24]} ] }`.

- [ ] **Step 1: Write failing tests.** Service: `test_sparklines_24_buckets_one_query` (3 monitors → `django_assert_num_queries(1)`, each list length 24, known hour gets the rounded average, others `None`); `test_status_precedence` (maintenance beats down; paused; unknown for no checks); `test_new_user_no_data` (Review Focus: `uptime_24h is None`, `spark == [None]*24`, summary all zero, no exception). API: anonymous → 401; only the caller's monitors; `test_query_count_does_not_grow` (`django_assert_max_num_queries(6)` with 2 and with 15 monitors); the old `DashboardView` page still renders the same table (existing dashboard tests stay green).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement** `sparklines` with `TruncHour` + `values("monitor_id","hour")` + `Avg`; refactor `DashboardView.get_context_data` to build from `dashboard_rows` *without changing the template output* (adapt template variable names if required and keep its tests green).
- [ ] **Step 4:** `pytest -q` green.
- [ ] **Step 5: Commit** `feat(api): dashboard endpoint with sparklines`.

### Task 4a: One validation path for monitors (prepares stage 3)

The spec (§4, §7.2) forbids two implementations of monitor validation. Stage 3 adds the create/edit endpoints; this task fixes the shape now so they cannot drift.

**Files:**
- Create: `api/validation.py`, `api/tests/test_validation_parity.py`

**Interfaces:**
- Consumes: `monitors.forms.MonitorForm`, `api.errors.field_errors_from_django`.
- Produces: `validate_monitor(user, data: Mapping[str, Any], instance: Monitor | None = None) -> tuple[dict[str, Any], dict[str, list[str]]]` — returns `(cleaned_data, errors)`; `errors` is empty on success. *My call:* it **wraps `MonitorForm`** (the existing source of truth for URL scheme, SSRF check, status ranges, headers, HEAD+text, `(user,url)` uniqueness) instead of re-implementing the rules, which gives the spec's guarantee with no copy to keep in sync.

- [ ] **Step 1: Write failing tests** `test_validation_parity.py`: parametrised table of bad inputs (`ftp://x.com`, `http://127.0.0.1/`, `expected_status="abc"`, `request_headers="Host: x"`, `http_method="HEAD"` + `expected_text`, duplicate `(user,url)`, `interval_seconds=7`) → `validate_monitor(...)[1]` equals `field_errors_from_django(MonitorForm(data, user=user).errors)` and is non-empty; a valid payload returns `{}` errors and `cleaned["request_headers"] == {"X-A": "1"}` for `"X-A: 1"`; editing an existing monitor with its own URL is valid.
- [ ] **Step 2:** `pytest api/tests/test_validation_parity.py -q` → FAIL.
- [ ] **Step 3:** implement the wrapper.
- [ ] **Step 4:** run → PASS; `pytest -q` green.
- [ ] **Step 5: Commit** `feat(api): single monitor validation path shared with the form`.

### Task 5: OpenAPI snapshot + contract tooling

**Files:**
- Create: `api/management/commands/export_openapi.py`, `api/openapi.json`, `api/tests/test_contract.py`
- Modify: `README.md` (short «API contract» section)

**Interfaces:**
- Produces: `python manage.py export_openapi` writes deterministic JSON (sorted keys, 2-space indent, trailing newline) to `api/openapi.json`; `python manage.py export_openapi --check` exits non-zero if the file differs.

- [ ] **Step 1: Write failing test** `test_committed_openapi_matches_code`: runs the command in check mode (via `call_command`) and expects success; `test_schema_has_all_stage1_paths`: asserts the 12 stage-1 paths exist.
- [ ] **Step 2:** run → FAIL (no snapshot).
- [ ] **Step 3:** implement the command; run `python manage.py export_openapi` to create the snapshot.
- [ ] **Step 4:** `pytest api -q` → PASS.
- [ ] **Step 5: Commit** `feat(api): committed OpenAPI contract snapshot with drift check`.

### Task 6: Frontend scaffold, typed client, CSRF and error handling

**Files:**
- Create (in `frontend/`): `package.json` (scripts `dev`, `build`, `lint`, `typecheck`, `test`, `gen:api`), `vite.config.ts` (dev server port 5173, proxy `/api` → `http://localhost:8000`, Vitest config), `tsconfig.json` (strict), `index.html`, `src/main.tsx`, `src/api/{client.ts,errors.ts}`, `src/api/schema.d.ts` (generated), `src/lib/{csrf.ts,strings.ts}`, `src/test/{setup.ts,server.ts}`, `src/api/client.test.ts`, `src/lib/csrf.test.ts`, `.gitignore`
- `gen:api` = `openapi-typescript ../api/openapi.json -o src/api/schema.d.ts`.

**Interfaces:**
- Produces:
  - `getCsrfToken(): string | null` — reads the `csrftoken` cookie **each call**; `ensureCsrf(): Promise<void>` — `GET /api/v1/auth/csrf` once when the cookie is missing.
  - `class ApiError extends Error { status: number; fieldErrors: Record<string, string[]>; }`
  - `client` (openapi-fetch `createClient<paths>`, `credentials: "same-origin"`) with middleware: adds `X-CSRFToken` on non-GET using `getCsrfToken()` at request time; and `unwrap<T>(result): T` which throws `ApiError` for any error result.
  - `strings.ts` exports `t` (uk strings object); at least `errors.network`, `errors.server`, `errors.session`.

- [ ] **Step 1: Write failing tests (Vitest + MSW):**
  - `csrf.test.ts`: cookie present → returned; changing `document.cookie` between two calls changes the result (**rotation**, Review Focus); `ensureCsrf` fetches only when the cookie is absent.
  - `client.test.ts`: POST sends `X-CSRFToken` equal to the *current* cookie; a `422 {message, errors}` becomes `ApiError` with `fieldErrors` from `errors`; **a `502` with an HTML body** and **an empty `500` body** become `ApiError` whose `message === t.errors.server` (no JSON parse crash); network failure → `message === t.errors.network`; `401` → `ApiError.status === 401`.
- [ ] **Step 2:** `cd frontend && npm test -- --run` → FAIL.
- [ ] **Step 3:** scaffold (`npm create vite@latest` react-ts template; if the newest Vite needs Node > 20.19, use the previous major), pin React 18, add dependencies, run `npm run gen:api`, implement the modules.
- [ ] **Step 4:** `npm test -- --run`, `npm run typecheck`, `npm run build` → pass.
- [ ] **Step 5: Commit** `feat(frontend): scaffold with typed client, CSRF and error normalisation`.

### Task 7: Design system — theme, tokens, shell, status components

**Files:**
- Create: `src/index.css` (Tailwind + tokens), `src/components/{ThemeProvider.tsx,AppShell.tsx,StatusBadge.tsx,Sparkline.tsx,States.tsx}`, `src/components/ui/*` (shadcn: button, input, label, badge, card, skeleton, sonner/toast as needed), tests next to each component
- Modify: `package.json` (tailwind, shadcn deps, lucide-react)

**Interfaces:**
- Produces: `ThemeProvider` + `useTheme(): { theme: "light"|"dark"|"system"; setTheme(t): void }` (persists in `localStorage` under `uptime-theme`; applies `class="dark"` on `<html>`; `"system"` follows `prefers-color-scheme`); `StatusBadge({ status: "up"|"down"|"unknown"|"maintenance"|"paused"|"degraded" })` (icon + Ukrainian text + colour class); `Sparkline({ values: (number|null)[]; className?: string })` (inline SVG; `null` → gap; all-null → renders a flat dashed «немає даних» line and `aria-label`); `AppShell({ children })` with sidebar (≥768 px: Монітори · Статус-сторінки · Звіти · Безпека, user menu with logout, theme switch) and bottom bar (<768 px); `EmptyState`, `ErrorState({ onRetry })`, `ListSkeleton` in `States.tsx`.

- [ ] **Step 1: Write failing tests:** theme persists and sets/removes the `dark` class; `"system"` honours a mocked `matchMedia`; `StatusBadge` renders text **and** an `svg` for each status (never colour only); `Sparkline` with `[1,null,3]` renders a path with a gap, with all `null` renders the «немає даних» element and no `NaN` in any attribute; `AppShell` renders both nav variants (desktop `aside` and mobile `nav`) with the four labels; `ErrorState` calls `onRetry`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement; tokens from Global Constraints (accent, radius 10 px, Inter). Components are controlled, no data fetching.
- [ ] **Step 4:** `npm test -- --run`, `npm run lint`, `npm run build` → pass.
- [ ] **Step 5: Commit** `feat(frontend): design tokens, theme, app shell and status components`.

### Task 8: Auth state, router and guards

**Files:**
- Create: `src/app/{providers.tsx,router.tsx}`, `src/features/auth/{useMe.ts,RequireAuth.tsx,PublicOnly.tsx}`, tests
- Modify: `src/main.tsx`

**Interfaces:**
- Consumes: Task 6 `client`/`unwrap`/`ensureCsrf`/`ApiError`; Task 7 `AppShell`.
- Produces: `useMe(): UseQueryResult<UserOut | null>` (`null` when the API answers 401 — not an error state; `staleTime` 60 s); `RequireAuth` (redirects to `/login?next=<path>` when `null`, renders `AppShell` + `<Outlet/>` otherwise); `PublicOnly` (redirects authed users to `/`); `queryClient` default `retry: (n, err) => !(err instanceof ApiError && [401,403,404,422].includes(err.status)) && n < 2`; global handler: a 401 from any query/mutation sets `me` to `null` once (single redirect). Routes: `/login`, `/signup`, `/signup/sent`, `/confirm/:token`, `/resend`, `/forgot-password`, `/reset-password/:uid/:token`, `/` (dashboard), `*` (not found page).

- [ ] **Step 1: Write failing tests (MSW):** anonymous visit to `/` lands on `/login?next=%2F`; authed visit to `/login` redirects to `/`; `useMe` maps 401 → `null` without throwing; `ensureCsrf` is called once on app start; unknown route renders the not-found page.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** tests/typecheck pass.
- [ ] **Step 5: Commit** `feat(frontend): router, auth state and route guards`.

### Task 9: Login (with 2FA step), logout, signup flow screens

**Files:**
- Create: `src/features/auth/{LoginPage.tsx,TwoFactorForm.tsx,SignupPage.tsx,SignupSentPage.tsx,ConfirmPage.tsx,ResendPage.tsx,AuthLayout.tsx,useAuthMutations.ts}`, tests

**Interfaces:**
- Consumes: Task 6/8. Forms: react-hook-form + zod for *shape only* (non-empty); all validation messages shown come from `ApiError.fieldErrors` (`__all__` rendered as a form-level alert).
- Produces: `useLogin()` → `{status: "ok"|"2fa_required"}`; on `ok` invalidates `me` and navigates to `next` (**only same-origin relative paths are accepted**, else `/`); on `2fa_required` the page swaps to `TwoFactorForm` (6-digit input, link «використати код відновлення»); `useVerify2fa()`; `useSignup()`; `useConfirm(token)`; `useResend()`.

- [ ] **Step 1: Write failing tests (MSW):** login success → navigates to `/`; wrong password → form alert with the API's `__all__` text, button re-enabled; `2fa_required` → 2FA form appears, correct code → dashboard, `422` shows the API message under the field, `401` (expired) → back to the login form with a «сесія входу завершилась» notice; `next=https://evil.example/` and `next=//evil.example` are ignored; submit button is disabled while pending (no double submit); signup `422` shows per-field API errors; signup success → `/signup/sent`; `/confirm/:token` success shows a login link, bad token shows the resend link.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement (Ukrainian copy in `strings.ts`). **Step 4:** pass.
- [ ] **Step 5: Commit** `feat(frontend): login with 2FA, signup and email confirmation screens`.

### Task 10: Password reset screens

**Files:**
- Create: `src/features/auth/{ForgotPasswordPage.tsx,ResetPasswordPage.tsx}`, tests

**Interfaces:**
- Consumes: Task 3 endpoints `POST /auth/password-reset`, `POST /auth/password-reset/confirm`.
- Produces: pages at `/forgot-password` (always shows the same «якщо акаунт існує…» confirmation) and `/reset-password/:uid/:token`.

- [ ] **Step 1: Write failing tests:** forgot-password shows the identical confirmation for a 200 regardless of email; reset success → redirected to `/login` with a success notice; weak password → API field errors shown under `new_password1`; invalid link (400) → message + link to request a new one.
- [ ] **Step 2–4:** run (FAIL) → implement → pass.
- [ ] **Step 5: Commit** `feat(frontend): password reset screens`.

### Task 11: Dashboard screen

**Files:**
- Create: `src/features/dashboard/{DashboardPage.tsx,SummaryBar.tsx,MonitorRow.tsx,useDashboard.ts}`, `src/lib/format.ts`, tests

**Interfaces:**
- Consumes: Task 4 `GET /api/v1/dashboard`; Task 7 `StatusBadge`, `Sparkline`, `States`.
- Produces: `useDashboard()` (TanStack Query, `refetchInterval: 10_000`, `refetchIntervalInBackground: false`); `formatRelative(date: string, now?: Date): string` («12 с тому», «5 хв тому», «—» for null); `formatUptime(v: number | null): string` («99.9%» or «—»).

- [ ] **Step 1: Write failing tests (MSW):** renders summary (up/down/incidents) and one row per monitor with badge, sparkline, uptime, relative time; search filters rows by name (case-insensitive, unicode-safe) and status filter works; `down_minutes: 42` shows «Down вже 42 хв»; **new user** (`monitors: []`) shows the empty state with an «Додати» hint; **`uptime_24h: null` shows «—», never `NaN`** (Review Focus); **a 200-character name and one with emoji/RTL text** render truncated (`truncate` class, title attribute holds the full name) and the badge is still visible; loading shows skeletons, a `500` shows `ErrorState` and «Повторити» refetches; **session expiry:** the first poll returns `401` → exactly one redirect to `/login?next=%2F`, no further requests after it (count handler calls); polling interval is 10 s (fake timers) and does not fire while `document.visibilityState === "hidden"`.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement; rows link to `/monitors/:id` (route added in stage 2; for now a placeholder page). **Step 4:** tests, typecheck, lint, build pass.
- [ ] **Step 5: Commit** `feat(frontend): dashboard with live polling and sparklines`.

### Task 12: Dev environment, CI and docs

**Files:**
- Create: `frontend/Dockerfile.dev`, `frontend/.dockerignore`
- Modify: `docker-compose.yml` (service `frontend`: Vite on 5173 proxying to `web:8000`, `FRONTEND_URL=http://localhost:5173` for `web`), `.github/workflows/ci.yml` (job `frontend`), `.github/dependabot.yml` (npm, `directory: /frontend`), `README.md`, `.gitignore`

**Interfaces:**
- Consumes: everything above. CI job `frontend` (Node 20, `npm ci`): `npm run lint`, `npm run typecheck`, `npm test -- --run`, `npm run build`, `npm audit --audit-level=high`, then **contract sync**: `npm run gen:api && git diff --exit-code src/api/schema.d.ts`; and in the existing `test` job: `python manage.py export_openapi --check`.

- [ ] **Step 1:** add the CI job and compose service; validate YAML (`python -c "import yaml; …"`) and `docker compose config -q`.
- [ ] **Step 2:** locally run the exact CI commands in a clean checkout of `frontend/` (`npm ci` etc.) and `docker compose up -d --build`; open `http://localhost:5173`, register → read the confirmation mail from `docker compose logs worker-notify`, confirm, log in, see the dashboard of the `demo` data after `seed_demo` (log in as `demo` / `demo12345`). Expected: dashboard shows the demo monitors with live statuses.
- [ ] **Step 3:** README: «Frontend» section (dev workflow, `gen:api`, contract rule, how the contract check fails).
- [ ] **Step 4:** push the branch/main and confirm CI shows all jobs green (`gh run watch`).
- [ ] **Step 5: Commit** `ci(frontend): dev compose service, frontend CI job, contract sync check`.

---

## Stage-1 acceptance

1. `pytest -q` green (187 existing + new API/service tests); `ruff` clean; `python manage.py export_openapi --check` passes.
2. `cd frontend && npm run lint && npm run typecheck && npm test -- --run && npm run build` green.
3. In the running Docker stack: signup → email confirmation → login (and login with 2FA for a 2FA user) → dashboard polls live; logout works; password reset works end-to-end.
4. Changing a response schema in the backend without regenerating types makes CI fail.
5. The old Django UI at `:8000` is unchanged and its tests pass.
