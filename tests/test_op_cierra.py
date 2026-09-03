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
from datetime import date
from unittest.mock import patch

import pytest

from modules.admin_dbase import health_audit_view as hv


@pytest.fixture
def _app_ctx(app):
    with app.test_request_context("/"):
        yield


def _run(*, compra, posdat, retiros, consumido=0.0, retiros_historicos=None,
         desde=date(2026, 1, 15)):
    """`compra`/`posdat` son los SUM(importe) crudos (negativos para OP).

    `retiros` es lo retirado DESDE `desde` (la primera compra OP viva) —
    el scope que usa el titular. `retiros_historicos` es el total de todos
    los tiempos; por defecto igual a `retiros`.
    """
    if retiros_historicos is None:
        retiros_historicos = retiros

    def _fake(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "scintela.compra" in s:
            return {"s": compra, "d": desde}
        if "scintela.posdat" in s:
            return {"s": posdat}
        if "scintela.retiros" in s:
            return {"s": retiros if "fecha >= %s" in s else retiros_historicos}
        if "scintela.op_retiro_linea" in s:
            return {"s": consumido}
        raise AssertionError(f"query inesperada: {s}")

    with patch.object(hv.db, "fetch_one", side_effect=_fake):
        vista = hv.op_cierra
        while hasattr(vista, "__wrapped__"):
            vista = vista.__wrapped__
        return vista().get_json()


def test_el_credito_consumido_no_es_un_descuadre(_app_ctx):
    """El caso sano.

    `crear_op` consume el crédito haciendo `importe += monto` sobre la fila
    posdat de la línea, así que el posdat ENCOGE con cada retiro y la compra
    no. Cargados 200.000, retirados (e imputados) 150.000: quedan 50.000 en
    posdat. Eso cierra, no descuadra.
    """
    d = _run(compra=-200_000.0, posdat=-50_000.0, retiros=150_000.0,
             consumido=150_000.0)
    assert d["ok"] is True
    assert d["stats"]["residuo_compras_vs_posdat"] == 0.0
    assert d["stats"]["consumido_bajo_posdat"] == 150_000.0
    assert d["stats"]["disponible_saldo_op"] == 50_000.0


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
    assert d["stats"]["residuo_compras_vs_posdat"] == -117_886.99
    assert "cargadas a mano" in d["alerts"][0]["msg"]


def test_avisa_si_el_titular_da_disponible_negativo(_app_ctx):
    d = _run(compra=-100_000.0, posdat=-100_000.0, retiros=180_000.0)
    assert d["ok"] is False
    cats = {a["category"] for a in d["alerts"]}
    assert "op_disponible_negativo" in cats
    assert d["stats"]["disponible_saldo_op"] == -80_000.0


def test_los_retiros_viejos_no_cuentan_como_agujero(_app_ctx):
    """La regresión del 03/09.

    `scintela.compra` espeja el DBF, que PURGA las compras viejas. Los retiros
    anteriores a la primera compra viva cancelaban créditos que ya no están de
    ningún lado. La primera versión de este chequeo los sumaba igual y denunció
    6,5M de retiro de más que eran retiros históricos legítimos.

    Acá: 6.784.647,66 retirados de todos los tiempos, de los cuales 500.000 son
    del período vivo contra 603.162,32 de crédito. No hay agujero.
    """
    d = _run(compra=-603_162.32, posdat=-603_162.32,
             retiros=500_000.0, retiros_historicos=6_784_647.66)
    cats = {a["category"] for a in d["alerts"]}
    assert "op_disponible_negativo" not in cats
    assert d["stats"]["retirado"] == 500_000.0
    assert d["stats"]["retirado_historico"] == 6_784_647.66
    assert d["stats"]["disponible_saldo_op"] == 103_162.32


def test_produccion_03_09_el_consumo_explica_casi_todo(_app_ctx):
    """Producción 03/09: compras 603.162,32 y posdat 223.813,12.

    La diferencia de 379.349,20 NO es un descuadre: es crédito consumido. Si
    las imputaciones suman esos mismos 379.349,20, cierra y no alerta.
    """
    d = _run(compra=-603_162.32, posdat=-223_813.12, retiros=500_000.0,
             consumido=379_349.2)
    cats = {a["category"] for a in d["alerts"]}
    assert "op_credito_no_cuadra" not in cats
    assert d["stats"]["residuo_compras_vs_posdat"] == 0.0


def test_sin_nada_cargado_no_revienta(_app_ctx):
    d = _run(compra=0.0, posdat=0.0, retiros=0.0)
    assert d["ok"] is True
    assert d["stats"]["credito_posdat"] == 0.0
