"""Tests de `modules/asinfo/hora_emision.py` — la hora de emitida, sin segundos.

TMT 2026-09-02 (dueña, mirando /facturas/183296). Sin HTTP: se mockea
`metabase_client`. La 183296 se emitió a las 13:53:57 de Ecuador.
"""
from __future__ import annotations

from unittest.mock import patch

from modules.asinfo import hora_emision as he

N = "001-099-000183296"


def test_sin_numeros_con_forma_de_asinfo_no_pregunta_nada():
    """Lo único que se interpola en el SQL son los números: se validan
    ENTEROS, o el `IN (...)` sería una puerta abierta."""
    with patch("modules._lib.metabase_client.disponible") as m:
        assert he.horas([None, "", "183296", "001-099-18",
                         f"{N}'; DROP TABLE x--"]) == {}
    m.assert_not_called()


def test_sin_puente_devuelve_vacio():
    with patch("modules._lib.metabase_client.disponible", return_value=False), \
         patch("modules._lib.metabase_client.fetch_dataset_estado") as f:
        assert he.horas([N]) == {}
    f.assert_not_called()


def test_la_hora_sale_sin_segundos_y_por_numero():
    """108 + varchar(5) = 'HH:MM'. Y una sola consulta para todos."""
    filas = [{"numero": N, "hora": "13:53"},
             {"numero": "NTEN-10924", "hora": "09:07"}]
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=(filas, True)) as f:
        res = he.horas([N, "NTEN-10924", N])
    assert res == {N: "13:53", "NTEN-10924": "09:07"}
    assert f.call_count == 1
    sql = f.call_args[0][1]
    assert f"'{N}'" in sql and "'NTEN-10924'" in sql
    assert "CONVERT(varchar(5), fc.fecha_creacion, 108)" in sql
    assert "fc.estado <> 0" in sql


def test_metabase_que_no_contesta_no_es_hora_vacia():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], False)):
        assert he.horas([N]) == {}


def test_completar_guarda_solo_las_que_no_tenian_y_las_deja_en_la_fila():
    facts = [
        {"id_factura": 1, "numf_completo": N, "hora_emision": None},
        {"id_factura": 2, "numf_completo": "001-099-000183297", "hora_emision": "08:00"},
        {"id_factura": 3, "numf_completo": None, "hora_emision": None},
        {"id_factura": 4, "numf_completo": "001-099-000183298", "hora_emision": None},
    ]
    with patch.object(he, "horas", return_value={N: "13:53"}) as h, \
         patch("modules.facturas.queries.guardar_hora_emision") as g:
        assert he.completar(facts) == 1
    assert sorted(h.call_args[0][0]) == [N, "001-099-000183298"]
    g.assert_called_once_with(1, "13:53")
    assert facts[0]["hora_emision"] == "13:53"
    assert facts[3]["hora_emision"] is None


def test_completar_sin_pendientes_ni_pregunta():
    with patch.object(he, "horas") as h:
        assert he.completar([{"id_factura": 1, "numf_completo": N,
                              "hora_emision": "13:53"}]) == 0
    h.assert_not_called()


def test_la_ficha_muestra_la_hora_al_lado_de_la_fecha(app):
    """Render puro del template: la hora va pegada a la fecha, sin segundos."""
    from datetime import date

    fact = {"id_factura": 1, "numf": 183296, "numf_completo": N,
            "fecha": date(2026, 9, 2), "hora_emision": "13:53", "stat": "Z",
            "codigo_cli": "IRE", "importe": 329.37, "abono": 0, "saldo": 329.37}
    with app.test_request_context("/facturas/183296"):
        from flask import g, render_template
        g.user = {"usuario": "t", "rol": "Accionista"}
        g.permisos = {"*"}
        html = render_template("facturas/detalle.html", fact=fact, det=None,
                               aplicaciones=[], retenciones=[],
                               total_aplicado=0, total_retenido=0)
        fact["hora_emision"] = None
        sin = render_template("facturas/detalle.html", fact=fact, det=None,
                              aplicaciones=[], retenciones=[],
                              total_aplicado=0, total_retenido=0)
    assert "02/09/2026 13:53" in html
    assert "13:53" not in sin and "02/09/2026" in sin
