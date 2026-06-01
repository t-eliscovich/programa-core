# Makefile — tareas frecuentes de Programa Core.
#
# Uso: `make <target>`. Compatible con macOS, Linux, y WSL.

.PHONY: help setup migrate seed run test test-unit test-db restore-test-db test-coverage ci lint fmt sync-dbf sync-dbf-dry-run sync-dbf-list docker-up docker-down docker-logs docker-test clean

PYTHON ?= python3
VENV   ?= .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python
COVERAGE_FAIL_UNDER ?= 100

help:
	@echo "Targets disponibles:"
	@echo "  setup          - crear venv y instalar requirements"
	@echo "  migrate        - aplicar migraciones pendientes"
	@echo "  seed           - crear primer admin interactivamente"
	@echo "  run            - correr el app localmente (launcher.sh)"
	@echo "  test           - correr unit coverage"
	@echo "  test-unit      - correr pytest sin @db con coverage"
	@echo "  test-db        - correr pytest @db contra Postgres con dump legacy"
	@echo "  restore-test-db - resetear DB test con dump legacy sanitizado"
	@echo "  test-coverage  - correr unit + db opcional y generar reporte combinado"
	@echo "  ci             - correr el gate local de coverage"
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
	$(PYTHON) -c "import sys; sys.exit('Python 3.10+ requerido; corré make setup PYTHON=python3.14 o PYTHON=python3.11') if sys.version_info < (3, 10) else None"
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

migrate:
	$(PY) scripts/migrate.py

seed:
	$(PY) scripts/seed_roles.py

run:
	./launcher.sh

test: test-unit

test-unit:
	$(PY) -m pytest -q -m "not db" --cov --cov-report=term-missing --cov-report=xml --cov-report=html --cov-fail-under=$(COVERAGE_FAIL_UNDER)

test-db:
	$(PY) -m pytest -q -m db

restore-test-db:
	$(PY) scripts/restaurar_test_legacy_dump.py --allow-reset

test-coverage:
	$(PY) -m coverage erase
	$(PY) -m pytest -q -m "not db" --cov --cov-report= --cov-append
	$(PY) -m pytest -q -m db --cov --cov-report= --cov-append
	$(PY) -m coverage report --fail-under=$(COVERAGE_FAIL_UNDER)
	$(PY) -m coverage xml
	$(PY) -m coverage html

ci: test-coverage

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
