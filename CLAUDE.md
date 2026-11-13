# CLAUDE.md — як працювати з цим репозиторієм

Uptime Monitor: Django 5 + PostgreSQL + Celery/Redis, SPA на React (`frontend/`). Мова інтерфейсу й повідомлень — українська.

## Запуск і тести

```bash
docker compose up -d --build            # web :8000, SPA :5173, db, redis, beat, workers (checks.eu/us/asia, notify, reports)
python manage.py seed_demo              # demo / demo12345 + 4 монітори

source .venv/bin/activate
python -m pytest -q                     # бекенд (потрібен локальний Postgres, БД test_uptime створюється сама)
ruff check . && ruff format --check .   # у CI це окремий job
python manage.py makemigrations --check --dry-run
python manage.py export_openapi --check # контракт API; змінили API -> export_openapi + (cd frontend && npm run gen:api)
(cd frontend && npm test -- --run && npm run lint && npm run typecheck && npm run build)
make perf                               # відтворює замірі з docs/perf.md
```

## Структура (де що живе)

- `monitors/models.py` — моделі. `CheckResult` **партиційований по днях** (PK `(id, checked_at)`, див. `partitions.py`).
- `monitors/check_types.py` — типи перевірок (strategy/registry). Нова перевірка = новий клас + реєстрація, **не** `if/elif` у `check_monitor`.
- `monitors/tasks.py` — Celery-задачі й логіка інцидентів. `monitors/async_checks.py` — асинхронний рушій пакетних перевірок (без ORM).
- `monitors/services.py` — читання/підрахунок чисел для сторінок (єдина реалізація для Django-сторінок і API).
- `monitors/rollups.py` — погодинні агрегати: усе довше за добу читається звідси. `maintenance.py`, `regions.py`, `webhooks.py`, `subscriptions.py`, `partitions.py` — по одній відповідальності.
- `monitors/views.py`, `public.py`, `subscribe_views.py` — Django-сторінки; **тонкі**: логіка в сервісах.
- `api/` — Django Ninja (`/api/v1/`). Валідація монітора = `MonitorForm` (`api/validation.py`), не друга копія правил.
- `accounts/` — вхід, 2FA (секрети шифруються Fernet), журнал входів. `reports/` — CSV-звіти.
- `config/observability.py` — JSON-логи, `request_id`, `client_ip`.

## Конвенції

- **Бізнес-логіка — у модулях/сервісах, не у view і не в сигналах.** Побічні ефекти (аудит, листи) викликаємо явно.
- Задачі Celery приймають **id**, не об'єкти; ідемпотентні; листи/webhook-и ставимо через `transaction.on_commit` або outbox.
- Назви задач: `monitors.tasks.<дієслово>_<об'єкт>`; черги: `checks.<region>`, `notifications`, `reports` (маршрути в `settings.CELERY_TASK_ROUTES`).
- Час усюди UTC; конвертація лише при відображенні. Години для rollup — `AT TIME ZONE 'UTC'`.
- Нова публічна поведінка → тест; нова міграція → перевірити `makemigrations --check` і зворотну міграцію, якщо вона не тривіальна.
- Коміти маленькі: одна логічна зміна. Без `Co-Authored-By` та згадок AI у комітах/PR (вимога власника репозиторію).

## Заборонено / пастки (виправлено на практиці)

- **Не ходити в мережу з тестів.** HTTP мокаємо (`responses` для `requests`, `httpx.MockTransport` для async-рушія); DNS у тестах фейковий (`conftest.py`, `monitors.security._resolve`).
- **Не робити запитів у циклі.** Для списків — `annotate`/`Subquery`/агрегація; є тести на сталу кількість запитів (`django_assert_max_num_queries`).
- `makemigrations` без `--noinput` **інтерактивний** і зависає, якщо додаєте non-null поле без default: або давайте default, або `null=True` → data-migration → `null=False`.
- Тести йдуть в одній транзакції: відкладені FK ще «в очікуванні» — перед `DROP` партиції робіть `SET CONSTRAINTS ALL IMMEDIATE`. Локальний Postgres має малий `max_connections`: тести з `transaction=True` і потоками закривають з'єднання в `finally`.
- Тест, що проходить на зламаному коді, не рахується: нову гонку/ідемпотентність перевіряйте **мутацією** (приберіть `select_for_update`, `on_commit`, умову фільтра — тест має впасти).
- Індекс без `EXPLAIN (ANALYZE, BUFFERS)` до й після не додаємо (див. `docs/perf.md`).
- На macOS `sed -i` вимагає `''`; для багаторядкових замін краще `python - <<EOF`.
- Секрети в репозиторій не кладемо: `.env*` у `.gitignore`, ключі — лише через env (`TOTP_ENCRYPTION_KEYS`, `SECRET_KEY`).

## Що додано в «Дні 2» (щоб не повторювати помилок)

- Типи перевірок (`check_types.py`), поріг-сповіщення SSL (14/7/1, кожен раз один), `CheckResult.details`.
- Підписки: `StatusSubscriber` + `SubscriberNotification` (unique `(incident, subscriber, event)`), розсилка батчами по 100, soft-відписка.
- `seed_volume`, `perf_report`, `docs/perf.md`: оптимізації лише з доказом `EXPLAIN`.
- Журнал помилок AI — `AI_LOG.md`: перед тим як довіряти згенерованому коду, перегляньте його.
