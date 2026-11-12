# Hardcore H1 — webhooks with a transactional outbox

Spec: `uptime-monitor-hardcore.md` §H1. Branch `hardcore/h1-webhooks` (stacked on H2).

**Decisions**
- One `OutboxEvent` row per (incident transition × active endpoint), created inside the same transaction as the Incident (`update_incident` runs inside `check_monitor`'s atomic block). Extra columns beyond the brief: `endpoint` FK, `event_id` (UUID, goes out as `X-Event-Id`), `status` (`pending|delivered|dead`).
- Claiming = a short transaction: `SELECT … WHERE due FOR UPDATE SKIP LOCKED LIMIT n`, then set a **lease** (`next_attempt_at = now + 2 min`, `attempts += 1`) and commit. HTTP happens *outside* any transaction, one Celery task per event, so a slow/dead endpoint only occupies its own task and never a DB lock. A worker that dies mid-delivery loses nothing: the lease expires and the event is claimed again (at-least-once, hence `X-Event-Id`).
- Body is serialised **once** to bytes (`json.dumps(sort_keys, compact)`), HMAC-SHA256 is computed over those bytes and the same bytes object is sent. `verify_signature()` (test receiver) uses `hmac.compare_digest`.
- Backoff `60 s · 2^(attempt-1)`, ±25% jitter, 8 attempts, then `dead`; a janitor also kills pending rows that exhausted attempts through crashes.
- SSRF: URL checked when saved (after DNS resolution, `blocked_reason`) **and** at delivery (`guarded_session` = connection-level guard against DNS rebinding); redirects are not followed.
- Endpoint secrets are Fernet-encrypted at rest (reuses `accounts.fields.EncryptedTextField`).
- UI: Django page `/monitors/{id}/webhooks/` (endpoints, delivery log, "Повторити" for dead events), as the brief says.

**Tests pin:** outbox atomic with the incident; no events for inactive/foreign endpoints; signature over exact bytes + tamper/wrong-secret/format failures; backoff sequence and `dead` after 8; two concurrent claimers never share an event, and a locked row does not block the rest; dead endpoint does not delay a healthy one; SSRF at save and at delivery (rebinding); lease/crash recovery; retry only for dead + owner only.
