# Frontend Stage 2 — Monitor page Implementation Plan

> Executed inline (superpowers:executing-plans). Spec: `docs/superpowers/specs/2026-11-12-frontend-spa-design.md` §7.2, §7.5, §7.8 (stage 2).

**Goal:** replace the "У розробці" stub at `/monitors/:id` with the real page: overview (chart, uptime cards, SSL, maintenance), checks (paginated, failures filter), incidents, "Check now".

**Architecture:** new Ninja router `api/routers/monitors.py` reuses `monitors.services` (one implementation of every number) and `MaintenanceForm` (one validation). SPA reads it via generated types; TanStack Query per tab.

**Tech:** Django Ninja, React 18, TanStack Query, Recharts, MSW/Vitest.

## Global constraints
- Error shape `{message, errors{field:[..]}}` everywhere, incl. 404 (`Не знайдено.`); foreign monitor id → 404, never 403.
- Same owner isolation as the Django views; no N+1 (query-count test on lists).
- Contract: `api/openapi.json` + `frontend/src/api/schema.d.ts` regenerated in the same commit as the API change.
- Copy in `frontend/src/lib/strings.ts`; colour is never the only carrier of meaning.

## Interfaces (API, all `session_auth`)
| Route | Result |
|---|---|
| `GET /monitors/{id}` | `{id,name,url,status,is_active,last_checked_at,ssl_days_left,ssl_error,down_minutes,uptime:{h24,d7,d30,d90}}` |
| `GET /monitors/{id}/stats?period=24h\|7d` | `{total,up,down,uptime,rt_avg,rt_min,rt_max}` (bad period → 422) |
| `GET /monitors/{id}/series?period=` | `[{t,ms,down}]` (labels are UTC) |
| `GET /monitors/{id}/checks?page&page_size&only_failed` | `{items[{id,checked_at,status_code,response_time_ms,is_up,error}],total,page,pages}`, `page_size ≤ 100` |
| `GET /monitors/{id}/incidents` | `[{id,started_at,resolved_at,first_error,minutes}]` (≤50) |
| `POST /monitors/{id}/check` | `202 {status:"queued"}`; `429` cooldown; `409` in maintenance |
| `GET/POST /monitors/{id}/maintenance`, `DELETE …/{wid}` | windows `{id,starts_at,ends_at,reason}`; POST validated by `MaintenanceForm` (422 field errors) |

## Tasks
1. **API read endpoints** (detail, stats, series, checks, incidents) + 404 handler + isolation/query-count tests. Commit with regenerated contract.
2. **API actions** (check now, maintenance CRUD) + tests (cooldown, maintenance block, validation parity, foreign window → 404). Move `_cooldown_ok` to `services.cooldown_ok`.
3. **Frontend shell of the page**: route, header (name, status badge, last checked, "Перевірити зараз" with cooldown/maintenance messages), tabs in `?tab=`, 404/error states.
4. **Overview tab**: stats cards (24h/7d/30d/90d uptime, rt avg/min/max), period switch, Recharts response-time chart (gaps for failures, accessible label), SSL card, maintenance list + create form + delete.
5. **Checks & Incidents tabs**: paginated table with "only failures", incidents list ("триває"/duration).
6. **Wrap-up**: README stage note, full suites, lint/typecheck/build, live check in Docker, commit.

## Review focus (each gets a test in the owning task)
- Foreign monitor id on every route → 404 (task 1/2).
- `page` past the end / `page_size=1000` clamp instead of 500 (task 1).
- Monitor with zero checks: stats `null`s, empty series, no division by zero (task 1).
- Check-now twice quickly → second is 429 and queues once; during maintenance → 409 (task 2).
- Maintenance window end before start / in the past → 422 with field-level message (task 2).
