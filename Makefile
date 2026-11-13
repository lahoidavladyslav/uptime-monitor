# `make perf` reproduces every number in docs/perf.md (needs Docker). Seeding 5M checks takes a few minutes.
COMPOSE = docker compose -f docker-compose.yml -f docker-compose.perf.yml

.PHONY: perf perf-seed perf-report perf-clean test lint

perf: perf-seed perf-report

perf-seed:
	$(COMPOSE) up -d --build db redis web
	$(COMPOSE) exec -T web python manage.py migrate --noinput
	$(COMPOSE) exec -T db psql -U uptime -d uptime -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
	$(COMPOSE) exec -T web python manage.py seed_volume --clean
	$(COMPOSE) exec -T web python manage.py seed_volume

perf-report:
	$(COMPOSE) exec -T web python manage.py perf_report --runs 5 --explain

perf-clean:
	$(COMPOSE) exec -T web python manage.py seed_volume --clean

test:
	python -m pytest -q

lint:
	ruff check . && ruff format --check .
