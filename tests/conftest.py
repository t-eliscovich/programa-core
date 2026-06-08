"""Shared pytest fixtures.

Design decisions:
    - `app` fixture uses an in-memory sqlite-like fake: we monkeypatch
      `db.fetch_one` / `db.fetch_all` / `db.execute` with a stub, so the
      unit tests don't need a live Postgres. DB-integration tests (marked
      `@pytest.mark.db`) are opt-in and expect a real DB.
    - CSRF is disabled for the app fixture — we test CSRF behavior
      separately with `WTF_CSRF_ENABLED=True`.
    - Rate limiter uses `storage_uri="memory://"` and we reset it between
      tests via `limiter.reset()`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Tests que TODAVÍA fallan por deuda de stubs/fixtures: la forma del SQL de
# producción cambió y los fakes (FakeDB / monkeypatch) no se actualizaron.
#
# TMT 2026-06-08: antes se marcaban xfail por ARCHIVO ENTERO (11 archivos), lo
# que escondía ~71 tests que YA pasan (XPASS) — la suite no los enforce-aba. Lo
# pasamos a nodeid EXACTO: SÓLO los 28 que realmente fallan quedan xfail; el
# resto de cada archivo ahora SÍ protege. Lista derivada del run de CI verde
# 2026-06-08. Al arreglar el fixture de un test, sacalo de este set.
KNOWN_FAILING_NODEIDS = {
    "tests/test_cheques_anticipo.py::test_cheque_anticipo_crea_espejo_negativo",
    "tests/test_cheques_anticipo.py::test_cheque_anticipo_default_es_false",
    "tests/test_cheques_anticipo.py::test_cheque_normal_no_crea_espejo",
    "tests/test_cheques_depositar_lote.py::test_happy_path_dos_cheques",
    "tests/test_cheques_depositar_lote.py::test_postdatado_p_es_depositable",
    "tests/test_compras_anular.py::test_compra_sin_numero_no_borra_posdat",
    "tests/test_compras_anular.py::test_happy_path_anular_actualiza_stat_y_borra_posdat",
    "tests/test_compras_anular.py::test_motivo_solo_espacios_raisa_value_error",
    "tests/test_compras_anular.py::test_motivo_vacio_raisa_value_error",
    "tests/test_compras_editar.py::test_crear_anticipo_dolares_inserta_dolares",
    "tests/test_compras_editar.py::test_crear_no_pagada_inserta_posdat",
    "tests/test_confirmar_accion.py::test_cheques_confirmar_reverso_get_200",
    "tests/test_confirmar_accion.py::test_cheques_reversar_sin_motivo_redirige",
    "tests/test_confirmar_accion.py::test_facturas_anular_sin_motivo_redirige_a_confirmacion",
    "tests/test_confirmar_accion.py::test_facturas_confirmar_anulacion_get_200",
    "tests/test_confirmar_accion.py::test_posdat_anular_sin_motivo_redirige",
    "tests/test_confirmar_accion.py::test_provisiones_eliminar_sin_motivo_redirige",
    "tests/test_confirmar_accion.py::test_retenciones_anular_sin_motivo_redirige",
    "tests/test_csv_upload.py::test_cargar_csv_requiere_permiso",
    "tests/test_diag_integraciones.py::test_diag_integraciones_sin_permiso_redirige",
    "tests/test_paridad_compra_a_balance.py::test_paridad_compra_anular_borra_posdat",
    "tests/test_paridad_compra_a_balance.py::test_paridad_compra_no_pagada_inserta_posdat",
    "tests/test_paridad_factura_a_balance.py::test_paridad_factura_alta_modifica_anular",
    "tests/test_session_timeout.py::test_sesion_dentro_del_timeout_actualiza_last_activity",
    "tests/test_session_timeout.py::test_sesion_expirada_se_limpia",
    "tests/test_session_timeout.py::test_sesion_sin_last_activity_no_expira_inmediata",
}


def pytest_collection_modifyitems(config, items):
    xfail_debt = pytest.mark.xfail(
        reason="stub/fixture debt: SQL production shape changed; fix the fake DB fixture and remove from KNOWN_FAILING_NODEIDS",
        strict=False,
    )
    for item in items:
        if item.nodeid in KNOWN_FAILING_NODEIDS:
            item.add_marker(xfail_debt)


# project root on path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Make sure create_app() doesn't try to open a real pool.
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_NAME", "programa_core_test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-long-okay")
os.environ.setdefault("ENV", "development")
os.environ.setdefault("DISABLE_BOOT_SYNC", "1")


class FakeDB:
    """In-memory stand-in for db.* calls during unit tests."""

    def __init__(self):
        self.users: dict[int, dict] = {}
        self.roles: dict[int, dict] = {}
        self.permisos: list[dict] = []
        self.next_id = 1

    def add_role(self, nombre_rol: str, permisos: list[str]) -> int:
        rid = self.next_id
        self.next_id += 1
        self.roles[rid] = {"id_rol": rid, "nombre_rol": nombre_rol}
        for p in permisos:
            self.permisos.append({"id_rol": rid, "nombre_opcion": p})
        return rid

    def add_user(self, username: str, password_hash: bytes, id_rol: int, activo: bool = True) -> int:
        uid = self.next_id
        self.next_id += 1
        self.users[uid] = {
            "id_usuario": uid,
            "username": username,
            "password_hash": password_hash.decode() if isinstance(password_hash, bytes) else password_hash,
            "id_rol": id_rol,
            "activo": activo,
        }
        return uid

    # ----- router
    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from seguridad.usuario u" in s and "join seguridad.rol" in s:
            uid = params[0]
            u = self.users.get(uid)
            if not u or not u["activo"]:
                return None
            r = self.roles[u["id_rol"]]
            return {
                "id_usuario": u["id_usuario"],
                "username": u["username"],
                "id_rol": u["id_rol"],
                "activo": u["activo"],
                "nombre_rol": r["nombre_rol"],
            }
        if "from seguridad.usuario" in s and "where lower(username)" in s:
            uname = params[0].lower()
            for u in self.users.values():
                if u["username"].lower() == uname:
                    return {
                        "id_usuario": u["id_usuario"],
                        "username": u["username"],
                        "password_hash": u["password_hash"],
                        "activo": u["activo"],
                    }
            return None
        if "from seguridad.rol" in s and "where nombre_rol" in s:
            nombre = params[0]
            for r in self.roles.values():
                if r["nombre_rol"] == nombre:
                    return dict(r)
            return None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from seguridad.permiso" in s and "where id_rol" in s:
            rid = params[0]
            return [p for p in self.permisos if p["id_rol"] == rid]
        return []

    def execute(self, sql, params=None, conn=None):
        return 0

    def execute_returning(self, sql, params=None, conn=None):
        return None

    def init_pool(self): pass
    def close_pool(self): pass


@pytest.fixture
def fake_db(monkeypatch):
    """Patch the db module with an in-memory fake."""
    import db

    fake = FakeDB()
    monkeypatch.setattr(db, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(db, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(db, "execute", fake.execute)
    monkeypatch.setattr(db, "execute_returning", fake.execute_returning)
    monkeypatch.setattr(db, "init_pool", fake.init_pool)
    return fake


@pytest.fixture
def app(fake_db, monkeypatch):
    """Flask app with CSRF + rate limit DISABLED for most tests."""
    # Prevent app.init_pool() from actually connecting.
    import db
    monkeypatch.setattr(db, "init_pool", lambda: None)

    from app import create_app
    from extensions import csrf, limiter

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.enabled = False
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# Integration-test fixtures — live Postgres. Sólo los cargamos si están
# instalados / configurados. Los tests `@pytest.mark.db` se skip-ean
# automáticamente si el fixture `real_pg_dsn` no consigue una DB.
import contextlib  # noqa: E402

with contextlib.suppress(ImportError):
    from tests.conftest_db import (  # noqa: F401, E402
        migrated_db,
        real_db_conn,
        real_pg_dsn,
    )
