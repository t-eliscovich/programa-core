# Makefile — tareas frecuentes de Programa Core.
#
# Uso: `make <target>`. Compatible con macOS, Linux, y WSL.

.PHONY: help setup migrate seed run test test-unit test-db restore-test-db test-coverage ci lint fmt test-uv sync-dbf sync-dbf-dry-run sync-dbf-list docker-up docker-down docker-logs docker-test clean

PYTHON ?= python3
VENV   ?= .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python
COVERAGE_FAIL_UNDER ?= 100

# TMT 2026-08-13 (dueña): *"tarda mucho el deploy cada vez que hago cambios"*.
# El deploy en sí tarda 50s; lo que tardaba era el CI, y dentro del CI un
# único `pytest` serial de 4.303 tests: 3m57s de los 5m35s totales de
# push→producción. Los tests `not db` no tocan Postgres (usan el FakeDB
# monkeypatcheado del conftest), así que reparten sin compartir estado.
#   --dist loadfile = agrupa POR ARCHIVO, no por test suelto. Los tests de
#   este repo comparten fixtures a nivel módulo y hay dependencias de orden
#   conocidas (ver el monkeypatch permanente del conftest y el gotcha de
#   app.py/auth en el skill): mantener el archivo entero en un mismo worker
#   preserva ese orden.
# Los `-m db` siguen SERIALES a propósito: son 63, tardan 14s y sí tocan una
# base real compartida.
# Para volver a serial (debug de un flaky, o comparar tiempos): `make ci PYTEST_PAR=`
# `worksteal` en vez de `loadfile`: con loadfile la ganancia medida fue 1,69x
# sobre 4 cores (237s -> 140s) porque los 305 archivos son muy desparejos y
# el worker que agarra el mas gordo marca el ritmo. worksteal arranca igual
# (bloques por archivo) pero un worker que se queda sin trabajo le ROBA
# tests al que va atrasado, asi que la cola no la fija el archivo mas largo.
#
# --maxfail: un CI ROJO cortaba igual a los ~4 min. Con esto avisa apenas
# junta 3 fallas. NO acelera el caso verde (ahi hay que correr todo igual).
# `worksteal` (04/09/2026). Hasta hoy iba `loadfile` porque el 13/08 había 50
# tests que dependían del orden (docs/tests_dependientes_del_orden.md). El
# 26/08 se re-midió y la lista quedó en CERO, y el doc decía "volver a
# worksteal y medir": medido hoy, tres corridas con worksteal dan el mismo
# resultado, y con cobertura tarda 11,5 s contra 19,5 s de loadfile (local,
# 4 cores) — loadfile deja a un worker solo con el archivo más gordo al final.
PYTEST_PAR ?= -n auto --dist worksteal --maxfail=3

help:
	@echo "Targets disponibles:"
	@echo "  setup          - crear venv y instalar requirements"
	@echo "  migrate        - aplicar migraciones pendientes"
	@echo "  seed           - crear primer admin interactivamente"
	@echo "  run            - correr el app localmente (launcher.sh)"
	@echo "  test           - correr unit coverage"
	@echo "  test-uv        - bootstrap uv+py3.11 y correr pytest (sandbox/py<3.11)"
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
	$(PYTHON) -c "import sys; sys.exit('Python 3.11+ requerido (filters.py usa datetime.UTC); corré make setup PYTHON=python3.11') if sys.version_info < (3, 11) else None"
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

# TMT 2026-06-11 — entornos con Python <3.11 (ej. sandbox de Claude trae 3.10
# y filters.py usa datetime.UTC que es 3.11+): bootstrapea un venv 3.11 con uv
# y corre la suite ahí. Es el camino estándar para correr tests en sandbox.
test-uv:
	uv venv --python 3.11 /tmp/v311 || true
	uv pip install -q -p /tmp/v311/bin/python -r requirements.txt pytest
	/tmp/v311/bin/python -m pytest tests -q

test-unit:
	$(PY) -m pytest -q -m "not db" $(PYTEST_PAR) --cov --cov-report=term-missing --cov-report=xml --cov-report=html --cov-fail-under=$(COVERAGE_FAIL_UNDER)

test-db:
	$(PY) -m pytest -q -m db

restore-test-db:
	$(PY) scripts/restaurar_test_legacy_dump.py --allow-reset

# TMT 2026-08-13 — MEDIDO Y DESCARTADO: correr los dos pytest A LA VEZ (uno de
# fondo con `&` y `wait`) NO sirve. Probado en CI: el de `not db` pasó de 61 s a
# 70 s y el de `db` de 11 s a 28 s, porque `-n auto` ya tiene los 4 cores
# ocupados y el segundo proceso no encuentra CPU libre — le roba a los workers.
# Total 73 s contra 74 s: un segundo y medio, a cambio de dos COVERAGE_FILE
# separados y un `wait` con exit code a mano. No vale la pena; si a alguien se
# le ocurre de nuevo, ya está medido.
#
# `coverage html` SÍ salió: en CI no lo mira nadie y se generaba y subía en cada
# corrida. Para verlo local: `make test-coverage && python3 -m coverage html`.
# TMT 2026-08-14: DOS gates sobre la MISMA corrida (no cuesta CI extra).
#   1. Los archivos historicos siguen al 100% clavado.
#   2. La conciliacion tiene su propio PISO, que arranco en lo medido el
#      14/08 y solo puede subir. Sin esto el trabajo de cobertura se deshace
#      solo: alguien agrega 300 lineas sin tests y nadie se entera.
# El piso se sube A MANO cuando se gana terreno — es la unica forma de que
# el numero signifique algo.
COVERAGE_CORE ?= config/roles.py,csv_upload.py,error_messages.py,exports.py,extensions.py,ip_allowlist.py,modules/_lib/formulas_db.py,modules/conciliacion/matcher.py,modules/diag/views.py,modules/healthz/views.py,modules/recientes/queries.py,modules/tintura/service.py,modules/two_fa/core.py,reparto_mensual.py,scope_vendedor.py
COVERAGE_CONCILIACION_MIN ?= 29

# Cada paso imprime la hora (04/09/2026): en el log del CI se ve en qué se va el
# gate sin tener que adivinar (`grep '^⏱'`).
test-coverage:
	@date '+⏱ %H:%M:%S erase'
	$(PY) -m coverage erase
	@date '+⏱ %H:%M:%S pytest not-db'
	$(PY) -m pytest -q -m "not db" $(PYTEST_PAR) --cov --cov-report= --cov-append
	@date '+⏱ %H:%M:%S pytest db'
	$(PY) -m pytest -q -m db --cov --cov-report= --cov-append
	@date '+⏱ %H:%M:%S coverage report'
	$(PY) -m coverage report --include=$(COVERAGE_CORE) --fail-under=$(COVERAGE_FAIL_UNDER)
	$(PY) -m coverage report --include='modules/conciliacion/*' --fail-under=$(COVERAGE_CONCILIACION_MIN)
	$(PY) -m coverage xml
	@date '+⏱ %H:%M:%S fin'


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
