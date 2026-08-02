.PHONY: install run run-no-migrate test migrate migrate-down db-shell lint venv verify-copy parity

venv:
	test -d .venv || uv venv --python 3.10

install: venv
	uv pip install -r requirements.txt

# HOST, PORT and WORKERS come from .env (app/core/settings.py), NOT from flags
# here -- `python -m app.main` is the one runner, so the two can never drift.
# Override per invocation with real environment variables, which outrank .env:
#
#   PORT=9000 make run
#   API_PREFIX=/saarathi-service make run
#
# Single worker is MANDATORY when MITRA_ENABLED=1: MitraSessionManager pools
# live WebSockets in process memory, so a second worker would open a second
# Mitra channel for the same interview (assert_single_worker enforces it).
# Scale with THREADPOOL_SIZE instead.
#
# DEPENDS ON `migrate` so the app can never start against an un-migrated
# database. Booting one fails inside config sync with
# `relation "agents" does not exist`, which names a missing TABLE when the real
# cause is a missing STEP -- and that is now easier to hit than it used to be,
# because the sidebar's capabilities live in the schema too.
#
# `alembic upgrade head` is idempotent: already at head it is one SELECT
# against alembic_version and prints nothing, so a normal start is unchanged.
run: migrate
	uv run python -m app.main

# Start WITHOUT touching the schema.
#
# The escape hatch for when applying migrations is not this process's job:
# several instances starting together would race on the same upgrade, and a
# deployment usually wants schema changes as a separate, reviewable step.
run-no-migrate:
	uv run python -m app.main

test:
	uv run pytest

migrate:
	uv run alembic upgrade head

migrate-down:
	uv run alembic downgrade -1

# The project now uses a local Postgres rather than docker compose; this keeps
# working for either. The URL is resolved through Settings, so it honours the
# same .env precedence as the app -- no fallback literal that could quietly
# connect you to a different database than the one you are running.
db-shell:
	@url=$$(uv run python -c 'from app.core.settings import settings; print(settings.database_url or "")'); \
	test -n "$$url" || { echo "DATABASE_URL is not set (see backend/.env)"; exit 1; }; \
	psql "$$url"

lint:
	uv run lint-imports

# Gate A: proves the framework-agnostic layers were copied, not rewritten.
verify-copy:
	uv run python scripts/verify_verbatim_copy.py

# Gate C: drives both apps through identical requests and diffs the responses.
parity:
	uv run python scripts/parity_check.py
