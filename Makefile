# Makefile — tareas frecuentes de Programa Core.
#
# Uso: `make <target>`. Compatible con macOS, Linux, y WSL.

.PHONY: help setup migrate seed run test lint fmt sync-dbf sync-dbf-dry-run sync-dbf-list docker-up docker-down docker-logs docker-test clean

PYTHON ?= python3
VENV   ?= .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

help:
	@echo "Targets disponibles:"
	@echo "  setup          - crear venv y instalar requirements"
	@echo "  migrate        - aplicar migraciones pendientes"
	@echo "  seed           - crear primer admin interactivamente"
	@echo "  run            - correr el app localmente (launcher.sh)"
	@echo "  test           - correr pytest + ruff"
	@echo "  lint           - sólo ruff"
	@echo "  fmt            - ruff --fix"
	@echo ""
	@echo "  sync-dbf-dry-run - mostrar qué pasaría al sincronizar DBFs (no toca Postgres)"
	@echo "  sync-dbf         - sincronizar DBFs legacy → Postgres (TRUNCATE+INSERT por tabla)"
	@echo "  sync-dbf-list    - listar las tablas que el sync conoce"
	@echo ""
	@echo "  docker-up      - docker compose up -d db app"
	@echo "  docker-down    - docker compose down"
	@echo "  docker-logs    - seguir logs del app"
	@echo "  docker-test    - correr la suite dentro del container"
	@echo "  clean          - borrar caches, __pycache__, .pyc"

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

migrate:
	$(PY) scripts/migrate.py

seed:
	$(PY) scripts/seed_roles.py

run:
	./launcher.sh

test: lint
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

fmt:
	$(PY) -m ruff check --fix .

# ---- Sync DBF → Postgres (proceso de transición mientras corre dBase en paralelo)
# Ver docs/RUNBOOK_sync_dbf.md para detalle.
sync-dbf-dry-run:
	$(PY) scripts/import_dbf.py --dry-run

sync-dbf:
	$(PY) scripts/import_dbf.py

sync-dbf-list:
	$(PY) scripts/import_dbf.py --list

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app

docker-test:
	docker compose run --rm test

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
