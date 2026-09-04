"""`/admin/health/op-cierra` — el crédito OP vivo en posdatados y sus consumos.

Tamara 2026-09-02: *"¿el saldo OP cierra contra los aportes y retiros?"*.

TMT 2026-09-04: la primera versión comparaba compras OP contra posdat OP y
sonaba por construcción (compras del dBase ya consumidas sin rastro, líneas
nuevas cargadas a mano). Ahora el crédito OP es Σ posdat OP vivos, y el health
vigila sólo lo que sí tiene que cerrar: ninguna línea viva positiva, y cada
consumo `bajo_posdat` apunta a una línea que existe.
"""
from unittest.mock import patch

import pytest

from modules.admin_dbase import health_audit_view as hv
from modules.retiros import queries as rq


@pytest.fixture
def _app_ctx(app):
    with app.test_request_context("/"):
        yield


def _run(*, vivos, todas=None, lineas=None, retiros=0.0, lineas_revientan=False):
    """`vivos`: [(num, concepto, importe)] posdat OP no anulados.
    `todas`: ídem incluyendo anuladas (por defecto = vivos).
    `lineas`: [(line_key, monto, n)] consumos bajo_posdat agrupados."""
    todas = vivos if todas is None else todas
    lineas = lineas or []

    def _all(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "op_retiro_linea" in s:
            if lineas_revientan:
                raise RuntimeError("no existe")
            return [{"line_key": k, "s": m, "n": n} for k, m, n in lineas]
        if "anulada IS NOT TRUE" in s:
            return [{"num": a, "concepto": b, "importe": c} for a, b, c in vivos]
        if "scintela.posdat" in s:
            return [{"num": a, "concepto": b} for a, b, _ in todas]
        raise AssertionError(f"query inesperada: {s}")

    def _one(sql, params=None, conn=None):
        assert "scintela.retiros" in sql
        return {"s": retiros}

    with patch.object(hv.db, "fetch_all", side_effect=_all), \
         patch.object(hv.db, "fetch_one", side_effect=_one):
        vista = hv.op_cierra
        while hasattr(vista, "__wrapped__"):
            vista = vista.__wrapped__
        return vista().get_json()


def test_produccion_04_09_cierra(_app_ctx):
    """Lo que había el 04/09: 5 líneas vivas (223.813,12), consumos que
    apuntan a líneas ya anuladas en cero. No alerta."""
    vivos = [(100330, "AC 46-50-58-59-60", -41304.16),
             (100335, "AC 47/49/54/55/61/62/67", -96227.02)]
    todas = vivos + [(0, "AC 17-35", 0.0), (100331, "MH 62", 0.0)]
    lineas = [("P|0|AC 17-35", 115207.0, 3), ("P|100331|MH 62", 14618.73, 1),
              ("P|100330|AC 46-50-58-59-60", 29397.0, 1)]
    d = _run(vivos=vivos, todas=todas, lineas=lineas, retiros=6_784_647.66)
    assert d["ok"] is True
    assert d["stats"]["credito_posdat"] == 137_531.18
    assert d["stats"]["n_lineas_vivas"] == 2
    assert d["stats"]["consumido_bajo_posdat"] == 159_222.73
    assert d["stats"]["retirado_historico"] == 6_784_647.66


def test_una_linea_positiva_es_un_credito_dado_vuelta(_app_ctx):
    d = _run(vivos=[(100340, "MH 80", 1_250.0), (100341, "AC 90", -5_000.0)])
    assert d["ok"] is False
    a = d["alerts"][0]
    assert a["category"] == "op_linea_positiva"
    assert "100340 MH 80 +1,250.00" in a["msg"]
    assert d["stats"]["credito_posdat"] == 3_750.0
    assert d["stats"]["lineas_positivas"] == [
        {"num": 100340, "concepto": "MH 80", "importe": 1250.0}]


def test_un_consumo_sin_linea_avisa(_app_ctx):
    d = _run(vivos=[(100341, "AC 90", -5_000.0)],
             lineas=[("P|100341|AC 90", 1_000.0, 1),
                     ("P|100399|NO EXISTE", 700.0, 2)])
    assert d["ok"] is False
    cats = {a["category"] for a in d["alerts"]}
    assert cats == {"op_consumo_sin_linea"}
    assert d["stats"]["consumos_huerfanos"] == [
        {"line_key": "P|100399|NO EXISTE", "monto": 700.0, "n": 2}]
    assert "700.00" in d["alerts"][0]["msg"]


def test_centavos_positivos_no_alertan(_app_ctx):
    d = _run(vivos=[(100342, "AI 1", 0.5)])
    assert d["ok"] is True


def test_sin_tabla_de_consumos_no_revienta(_app_ctx):
    d = _run(vivos=[], lineas_revientan=True)
    assert d["ok"] is True
    assert d["stats"]["consumido_bajo_posdat"] == 0.0
    assert d["stats"]["n_lineas_vivas"] == 0


def test_saldo_op_lee_posdat(_app_ctx):
    """El titular: Σ posdat OP vivos, en positivo."""
    def _one(sql, params=None, conn=None):
        assert "scintela.posdat" in sql and "anulada IS NOT TRUE" in sql
        assert "scintela.compra" not in sql
        return {"s": -223_813.12, "n": 5}
    with patch.object(rq.db, "fetch_one", side_effect=_one):
        assert rq.saldo_op() == {"credito": 223_813.12, "n_lineas": 5}
    with patch.object(rq.db, "fetch_one", return_value=None):
        assert rq.saldo_op() == {"credito": 0.0, "n_lineas": 0}
