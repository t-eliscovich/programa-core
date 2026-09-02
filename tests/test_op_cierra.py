"""`/admin/health/op-cierra` — el crédito OP contado dos veces, o de menos.

Tamara 2026-09-02: *"¿el saldo OP cierra contra los aportes y retiros de cada
accionista?"*.

El crédito OP (aporte del accionista) vive en DOS lugares, y `compras.queries.
crear` los mantiene a los dos: la compra NEGATIVA (`scintela.compra`,
codigo_prov='OP') y su ESPEJO en `scintela.posdat` (prov='OP', banc=0), que es el
que entra al balance vía TOTP. Una línea cargada a mano existe SÓLO en posdat.

Las dos pantallas cuentan distinto:

  · `saldo_op()`  — el titular: suma SÓLO compra. No ve las cargadas a mano.
  · `lineas_op()` — el listado: UNION ALL de compra Y posdat, sin deduplicar.
                    Cuenta dos veces cada crédito que sí tiene compra detrás.

No pueden estar bien las dos. Este chequeo no arregla ninguna: las MIDE.
"""
from unittest.mock import patch

import pytest

from modules.admin_dbase import health_audit_view as hv


@pytest.fixture
def _app_ctx(app):
    with app.test_request_context("/"):
        yield


def _run(*, compra, posdat, retiros):
    """`compra`/`posdat` son los SUM(importe) crudos (negativos para OP)."""
    def _fake(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "scintela.compra" in s:
            return {"s": compra}
        if "scintela.posdat" in s:
            return {"s": posdat}
        if "scintela.retiros" in s:
            return {"s": retiros}
        raise AssertionError(f"query inesperada: {s}")

    with patch.object(hv.db, "fetch_one", side_effect=_fake):
        vista = hv.op_cierra
        while hasattr(vista, "__wrapped__"):
            vista = vista.__wrapped__
        return vista().get_json()


def test_cuando_cada_compra_tiene_su_espejo_no_alerta(_app_ctx):
    """El caso sano: mismo crédito por las dos fuentes."""
    d = _run(compra=-200_000.0, posdat=-200_000.0, retiros=150_000.0)
    assert d["ok"] is True
    assert d["stats"]["credito_compras"] == 200_000.0
    assert d["stats"]["credito_posdat"] == 200_000.0
    assert d["stats"]["disponible_segun_posdat"] == 50_000.0


def test_avisa_cuando_lineas_op_duplicaria_el_credito(_app_ctx):
    """`lineas_op()` sumaría las dos fuentes: 400.000 donde hay 200.000."""
    d = _run(compra=-200_000.0, posdat=-200_000.0, retiros=0.0)
    assert d["stats"]["credito_como_lo_suma_lineas_op"] == 400_000.0
    assert d["stats"]["credito_posdat"] == 200_000.0


def test_una_linea_op_cargada_a_mano_hace_saltar_la_alarma(_app_ctx):
    """El caso del 01/09: dos líneas OP con `num` de la serie de posdat
    (100335, 100334), sin compra detrás. El titular 'Saldo OP' no las ve."""
    d = _run(compra=-105_926.13, posdat=-223_813.12, retiros=0.0)
    assert d["ok"] is False
    assert d["alerts"][0]["category"] == "op_credito_no_cuadra"
    assert d["stats"]["delta_compras_vs_posdat"] == -117_886.99
    assert "cargadas a mano" in d["alerts"][0]["msg"]


def test_avisa_si_se_retiro_mas_credito_del_que_hay(_app_ctx):
    d = _run(compra=-100_000.0, posdat=-100_000.0, retiros=180_000.0)
    assert d["ok"] is False
    cats = {a["category"] for a in d["alerts"]}
    assert "op_retirado_de_mas" in cats
    assert d["stats"]["disponible_segun_posdat"] == -80_000.0


def test_sin_nada_cargado_no_revienta(_app_ctx):
    d = _run(compra=0.0, posdat=0.0, retiros=0.0)
    assert d["ok"] is True
    assert d["stats"]["credito_posdat"] == 0.0
