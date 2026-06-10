"""Contract tests para `usuario_crea='asinfo-backfill'`.

Origen del bug que esto previene (TMT 2026-06-10):
    Tamara cargó 320 facturas vía /facturas/cargar-desde-asinfo-bulk. Cada
    factura quedó con `usuario_crea='tamara'` (el current user) en vez del
    marker canónico `'asinfo-backfill'`. Como los filtros NO_BACKFILL_WHERE
    de los reports live (TOTF, vent_mes, etc.) sólo excluyen 'asinfo-
    backfill', esas 320 facturas se sumaron a cartera/ventas/utilidad → la
    utilidad infló +$420k.

Capa 1 de protección (TMT 2026-06-10):
    Estos tests aseguran que los endpoints de carga Asinfo PASEN
    explícitamente `usuario='asinfo-backfill'` a `queries.crear()`,
    independientemente del current user. Si alguien revierte el fix (o
    agrega un endpoint nuevo sin el marker), un test falla.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest


@pytest.fixture
def app_client():
    """Cliente Flask de testing con login fake como 'tamara'."""
    import app as app_module
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as client:
        # Inyectar sesión de 'tamara' (NO 'asinfo-backfill') — el bug original
        # era que el current user se propagaba como usuario_crea. El fix
        # debe ignorar al current user y forzar el marker.
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "tamara"
            sess["permisos"] = {"facturas.crear", "facturas.ver"}
        yield client


# ---------------------------------------------------------------------------
# /facturas/cargar-desde-asinfo (single)
# ---------------------------------------------------------------------------


def test_cargar_desde_asinfo_single_marca_usuario_crea_como_backfill(app_client):
    """El endpoint single debe llamar queries.crear con usuario='asinfo-backfill',
    NO con el current user de la sesión."""
    captured = {}

    def fake_crear(**kwargs):
        captured.update(kwargs)
        return {"numf": 999999, "id_factura": 999999}

    # _resolver_cliente_asinfo retorna (codigo_pc, cliente_creado)
    def fake_resolver(*args, **kwargs):
        return ("CLI", False)

    with patch("modules.facturas.views.queries.crear", side_effect=fake_crear), \
         patch("modules.facturas.views._resolver_cliente_asinfo",
               side_effect=fake_resolver):
        resp = app_client.post("/facturas/cargar-desde-asinfo", data={
            "fecha": "10/06/2026",
            "codigo_cli": "CLI",
            "kg": "100",
            "usd": "500",
            "numero": "001-099-000999999",
            "tipo": "FACTURA",
        }, follow_redirects=False)

    assert resp.status_code in (302, 303), (
        f"Esperaba redirect, vino {resp.status_code} body={resp.data[:200]!r}"
    )
    assert captured, "queries.crear no fue llamado"
    assert captured.get("usuario") == "asinfo-backfill", (
        f"REGRESIÓN del bug 2026-06-10: el endpoint /facturas/cargar-desde-asinfo "
        f"debe pasar usuario='asinfo-backfill' pero pasó usuario={captured.get('usuario')!r}. "
        f"Sin este marker, los filtros NO_BACKFILL_WHERE no excluyen estas filas y la "
        f"utilidad LIVE infla por el monto de las facturas cargadas."
    )


# ---------------------------------------------------------------------------
# /facturas/cargar-desde-asinfo-bulk (batch)
# ---------------------------------------------------------------------------


def test_cargar_desde_asinfo_bulk_marca_usuario_crea_como_backfill(app_client):
    """El endpoint bulk debe llamar queries.crear con usuario='asinfo-backfill'
    en CADA iteración del loop, NO con el current user."""
    import json
    captured_calls = []

    def fake_crear(**kwargs):
        captured_calls.append(kwargs)
        return {"numf": 999999, "id_factura": 999999}

    def fake_resolver(*args, **kwargs):
        return ("CLI", False)

    rows = [
        {"fecha": "10/06/2026", "codigo_cli": "AQN", "kg": "50",
         "usd": "200", "numero": "001-099-000777777", "tipo": "FACTURA"},
        {"fecha": "10/06/2026", "codigo_cli": "FGJ", "kg": "30",
         "usd": "150", "numero": "001-099-000777778", "tipo": "FACTURA"},
    ]

    with patch("modules.facturas.views.queries.crear", side_effect=fake_crear), \
         patch("modules.facturas.views._resolver_cliente_asinfo",
               side_effect=fake_resolver):
        resp = app_client.post("/facturas/cargar-desde-asinfo-bulk", data={
            "rows_json": json.dumps(rows),
        }, follow_redirects=False)

    assert resp.status_code in (302, 303)
    assert len(captured_calls) == 2, (
        f"Esperaba 2 llamadas a crear, hubo {len(captured_calls)}"
    )
    for i, call in enumerate(captured_calls):
        assert call.get("usuario") == "asinfo-backfill", (
            f"REGRESIÓN bulk[{i}]: usuario={call.get('usuario')!r} en vez de "
            f"'asinfo-backfill'. Bug 2026-06-10 reabierto."
        )


# ---------------------------------------------------------------------------
# Smoke: los filtros NO_BACKFILL_WHERE excluyen 'asinfo-backfill'
# ---------------------------------------------------------------------------


def test_no_backfill_where_constant_excluye_asinfo_backfill():
    """La constante NO_BACKFILL_WHERE debe excluir 'asinfo-backfill'.

    Si alguien la modifica/borra, todos los reports live se rompen.
    """
    from modules.informes.queries import NO_BACKFILL_WHERE
    sql = NO_BACKFILL_WHERE.lower()
    assert "asinfo-backfill" in sql, (
        f"NO_BACKFILL_WHERE perdió el marker 'asinfo-backfill'. "
        f"Sin esto las facturas backfill cuentan en utilidad LIVE. "
        f"Valor actual: {NO_BACKFILL_WHERE!r}"
    )
    assert "usuario_crea" in sql, (
        f"NO_BACKFILL_WHERE perdió la columna usuario_crea. "
        f"Valor actual: {NO_BACKFILL_WHERE!r}"
    )


def test_totf_excluye_asinfo_backfill_en_sql():
    """totf() debe excluir 'asinfo-backfill' en su SQL.

    Si vuelve a no excluirlo, la cartera live infla por las facturas
    cargadas vía Asinfo manual.
    """
    import inspect

    from modules.informes import queries
    src = inspect.getsource(queries.totf)
    assert "asinfo-backfill" in src, (
        "totf() perdió el filtro 'asinfo-backfill'. Bug 2026-06-10 reabierto. "
        "La función debe excluir filas con usuario_crea='asinfo-backfill'."
    )


def test_totc_excluye_asinfo_backfill_en_sql():
    """totc() debe excluir 'asinfo-backfill' por consistencia con TOTF."""
    import inspect

    from modules.informes import queries
    src = inspect.getsource(queries.totc)
    assert "asinfo-backfill" in src, (
        "totc() perdió el filtro 'asinfo-backfill'. Defensivo, debe estar."
    )


def test_anticipos_excluye_asinfo_backfill_en_sql():
    """anticipos() debe excluir 'asinfo-backfill'."""
    import inspect

    from modules.informes import queries
    src = inspect.getsource(queries.anticipos)
    assert "asinfo-backfill" in src, (
        "anticipos() perdió el filtro 'asinfo-backfill'."
    )


def test_ventas_mes_corriente_kg_fisico_NO_filtra_backfill():
    """h_terminado_kg/h_tejido_kg DEBEN restar las kg físicas vendidas,
    incluyendo las marcadas como asinfo-backfill (= ventas reales).

    Si esta función filtrara backfill, el stock_terminado inflaría por las
    kg vendidas que no se descuentan. Bug TMT 2026-06-10 que ya cazamos.
    """
    import inspect

    from modules.informes import queries
    src = inspect.getsource(queries.ventas_mes_corriente_kg_fisico)
    assert "asinfo-backfill" not in src, (
        "ventas_mes_corriente_kg_fisico() introdujo filtro de asinfo-backfill. "
        "ESO ES EL BUG. Esta función DEBE incluir todas las kg vendidas, "
        "incluso las backfill, porque son ventas físicas reales. Si la filtramos, "
        "stock_terminado_kg infla por las kg que no se descuentan."
    )


# ---------------------------------------------------------------------------
# Sync dBase: usuario_crea='dbf-import'
# ---------------------------------------------------------------------------


def test_dbf_import_usa_marker_canonical():
    """El sync dBase debe usar usuario_crea='dbf-import'.

    Constante / patrón canónico — si cambia, todos los reports que excluyen
    dbf-import (junto con asinfo-backfill) rompen su semántica.
    """
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    sync_files = list(repo_root.glob("scripts/**/*.py"))
    sync_files += list((repo_root / "modules" / "admin_dbase").glob("*.py"))
    found = False
    for f in sync_files:
        try:
            content = f.read_text(encoding="utf-8")
            if "'dbf-import'" in content or '"dbf-import"' in content:
                found = True
                break
        except Exception:
            continue
    assert found, (
        "No encontré ninguna referencia a 'dbf-import' en scripts/ ni en "
        "modules/admin_dbase/. ¿Se renombró el marker del sync? Si sí, "
        "actualizar también los filtros NO_BACKFILL_WHERE que excluyen "
        "'dbf-import' y este test."
    )
