"""Las sucursales de Asinfo se detectan SOLAS por el RUC (TMT 2026-07-30).

Dueña: *"como hoy cargan manualmente en dbase, en asinfo es automatico, como
sabemos si es AJO o AJ2... tenes que encontrar en asinfo como diferenciarlas"*.

El dato existe: cada sucursal es una `empresa` propia cuyo `identificacion` es
el RUC de la matriz MÁS un dígito, con su propio `nombre_comercial`:

    22656  1793217341001    AJO   ALMACENES JOSE PUEBLA   ← matriz
    22657  17932173410011   AJ2   ALMACENES JOSE PUEBLA   ← sucursal

y la factura de la sucursal se emite a la MATRIZ con la DIRECCIÓN DE ENTREGA
apuntando a la empresa-sucursal. La card 199 exporta el código del facturado,
así que AJ2 no llegaba nunca a PC.

Lo que estos tests fijan:
  1. La SQL exige que el RUC de la dirección EMPIECE con el del facturado y sea
     más largo. Sin ese guard, las facturas donde el vendedor eligió la
     dirección de otro cliente (DIS→AJ2, AJO→RRV, CJE→FEB) se irían al cliente
     equivocado.
  2. `facturas_periodo` devuelve el código de la sucursal, y deja el del
     facturado a la vista.
  3. Fail-soft: si Metabase no contesta, queda el código del facturado (el
     comportamiento de antes) — nunca un código vacío ni una excepción.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.asinfo import service as svc  # noqa: E402


def _reset():
    svc.reset_sucursal_cache()
    svc.reset_facturas_cache()


def test_sql_exige_el_ruc_de_la_matriz():
    """El guard del RUC está en la query. No sacarlo."""
    sql = svc._SQL_SUCURSAL_POR_NUMERO
    assert "e2.identificacion LIKE e1.identificacion + '%'" in sql, (
        "sin esto, la dirección de entrega de OTRO cliente reclasifica la "
        "factura al cliente equivocado"
    )
    assert "LEN(e2.identificacion) > LEN(e1.identificacion)" in sql
    assert "de.id_empresa <> fc.id_empresa" in sql
    assert "fc.estado <> 0" in sql, "las anuladas no cuentan"


def test_sucursal_por_numero_arma_el_mapa(monkeypatch):
    _reset()
    llamadas = []

    def fake_dataset(db_id, sql, params=None, max_results=10000):
        llamadas.append((db_id, sql))
        return [
            {"numero": "001-099-000180340", "sucursal": "AJ2"},
            {"numero": "001-099-000180614", "sucursal": "cl3"},  # se normaliza
            {"numero": "", "sucursal": "AJ2"},                   # se descarta
            {"numero": "001-099-000180999", "sucursal": None},    # se descarta
        ]

    monkeypatch.setattr(svc.metabase_client, "fetch_dataset", fake_dataset)
    out = svc.sucursal_por_numero("2026-07-01", "2026-07-30")
    assert out == {
        "001-099-000180340": "AJ2",
        "001-099-000180614": "CL3",
    }
    assert llamadas and llamadas[0][0] == 2  # database 2 = Asinfo


def test_fechas_no_iso_no_llegan_a_la_sql(monkeypatch):
    """La SQL se interpola: sólo fechas ISO, nada de texto libre."""
    _reset()
    llamadas = []
    monkeypatch.setattr(svc.metabase_client, "fetch_dataset",
                        lambda *a, **k: llamadas.append(a) or [])
    assert svc.sucursal_por_numero("2026-07-01'; DROP", "2026-07-30") == {}
    assert llamadas == [], "no debería haber tocado Metabase"


def test_facturas_periodo_reclasifica_a_la_sucursal(monkeypatch):
    _reset()
    monkeypatch.setattr(
        svc, "fetch_card_from_env",
        lambda *a, **k: [
            {"numero": "001-099-000180340", "cliente_codigo": "AJO", "usd": 100},
            {"numero": "001-099-000180341", "cliente_codigo": "AJO", "usd": 200},
        ],
    )
    monkeypatch.setattr(
        svc.metabase_client, "fetch_dataset",
        lambda *a, **k: [{"numero": "001-099-000180340", "sucursal": "AJ2"}],
    )
    rows = svc.facturas_periodo("2026-07-01", "2026-07-30")
    suc = [r for r in rows if r["numero"].endswith("340")][0]
    matriz = [r for r in rows if r["numero"].endswith("341")][0]
    assert suc["cliente_codigo"] == "AJ2"
    assert suc["cliente_codigo_facturado"] == "AJO"
    assert suc["es_sucursal"] is True
    # La que se entrega en la matriz NO se toca.
    assert matriz["cliente_codigo"] == "AJO"
    assert "es_sucursal" not in matriz


def test_si_metabase_no_contesta_queda_el_facturado(monkeypatch):
    """Fail-soft: el comportamiento de antes, nunca un código vacío."""
    _reset()
    monkeypatch.setattr(
        svc, "fetch_card_from_env",
        lambda *a, **k: [
            {"numero": "001-099-000180340", "cliente_codigo": "AJO", "usd": 100},
        ],
    )

    def explota(*a, **k):
        raise RuntimeError("Metabase caído")

    monkeypatch.setattr(svc.metabase_client, "fetch_dataset", explota)
    rows = svc.facturas_periodo("2026-07-01", "2026-07-30")
    assert rows[0]["cliente_codigo"] == "AJO"
    assert "es_sucursal" not in rows[0]


def test_aplicar_sucursales_es_idempotente(monkeypatch):
    _reset()
    monkeypatch.setattr(
        svc.metabase_client, "fetch_dataset",
        lambda *a, **k: [{"numero": "001-099-000180340", "sucursal": "AJ2"}],
    )
    rows = [{"numero": "001-099-000180340", "cliente_codigo": "AJO"}]
    svc._aplicar_sucursales(rows, "2026-07-01", "2026-07-30")
    svc._aplicar_sucursales(rows, "2026-07-01", "2026-07-30")
    assert rows[0]["cliente_codigo"] == "AJ2"
    # el facturado NO se sobreescribe con "AJ2" en la segunda pasada
    assert rows[0]["cliente_codigo_facturado"] == "AJO"
