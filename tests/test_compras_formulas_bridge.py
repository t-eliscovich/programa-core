"""Tests del puente formulas_app → compras (modules/compras/formulas_bridge).

Sin DB real: se mockean `formulas_db` (lado formulas) y `db.fetch_all`
(lado PC). `queries.crear` se mockea para verificar los argumentos con los
que el puente daría de alta (y que genera el pasivo vía ese camino único).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from modules.compras import formulas_bridge as fb

# ── Normalización ───────────────────────────────────────────────────────────

def test_normalizar_factura_quita_ceros():
    assert fb.normalizar_factura("AQ", "0085") == "85"
    assert fb.normalizar_factura("PO", "0272") == "272"


def test_normalizar_factura_sey_completa_millar():
    # formulas trunca el prefijo: '2444' es la 22444 real
    assert fb.normalizar_factura("SY", "2444") == "22444"
    # con 5 dígitos ya viene completa
    assert fb.normalizar_factura("SY", "22380") == "22380"
    # con 3 dígitos no se puede saber → queda como está
    assert fb.normalizar_factura("SY", "859") == "859"


def test_normalizar_factura_no_digito_queda():
    assert fb.normalizar_factura("AQ", "F26-27/125") == "F26-27/125"


def test_concepto_pc_formato_dbase():
    c = fb.concepto_pc("85", date(2026, 7, 1))
    assert len(c) == 15
    assert c.startswith("85 ")
    assert c.endswith(" 1")
    c2 = fb.concepto_pc("22521", date(2026, 7, 14))
    assert len(c2) == 15
    assert c2.startswith("22521")
    assert c2.endswith("14")


def test_concepto_pc_sin_fecha():
    assert fb.concepto_pc("85", None) == "85"


# ── estado_mes ──────────────────────────────────────────────────────────────

GRUPOS = [
    # cargada por número exacto (dBase la trae como '85            1')
    {"proveedor": "AVQ", "factura": "0085", "fecha": "2026-07-01",
     "kg": 990, "importe_siva": 3430.90},
    # cargada por sufijo + importe en rango (SY manual con IVA mixto)
    {"proveedor": "SEY", "factura": "1945", "fecha": "2026-07-01",
     "kg": 1300, "importe_siva": 910.0},
    # pendiente
    {"proveedor": "AVQ", "factura": "0127", "fecha": "2026-07-14",
     "kg": 1015, "importe_siva": 5391.45},
    # excluida (importación)
    {"proveedor": "COLO", "factura": "26-27/125", "fecha": "2026-07-09",
     "kg": 9000, "importe_siva": 228863.50},
    # sin mapear
    {"proveedor": "NUEVO", "factura": "1", "fecha": "2026-07-02",
     "kg": 10, "importe_siva": 100.0},
    # EMP → ES con IVA 0%
    {"proveedor": "EMP", "factura": "7197", "fecha": "2026-07-07",
     "kg": 25000, "importe_siva": 5000.0},
]

COMPRAS_PC = [
    {"id_compra": 1, "codigo_prov": "AQ", "importe": 3945.54,
     "concepto": "85            1"},
    # SY cargada a mano con el número completo y total real (entre s/IVA y c/IVA)
    {"id_compra": 2, "codigo_prov": "SY", "importe": 1014.50,
     "concepto": "21945         1"},
]


def _estado_mock():
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=GRUPOS), \
         patch.object(fb.db, "fetch_all", return_value=COMPRAS_PC):
        return fb.estado_mes(2026, 7)


def test_estado_mes_estados():
    est = _estado_mock()
    assert est["disponible"] is True
    por_factura = {(f.proveedor_formulas, f.factura_formulas): f
                   for f in est["filas"]}
    assert por_factura[("AVQ", "0085")].estado == "cargada"
    assert por_factura[("SEY", "1945")].estado == "cargada"
    assert por_factura[("AVQ", "0127")].estado == "pendiente"
    assert por_factura[("COLO", "26-27/125")].estado == "excluida"
    assert por_factura[("NUEVO", "1")].estado == "sin_mapear"
    assert por_factura[("EMP", "7197")].estado == "pendiente"


def test_estado_mes_iva_por_proveedor():
    est = _estado_mock()
    por = {(f.proveedor_formulas, f.factura_formulas): f for f in est["filas"]}
    # químicos: 15%
    assert por[("AVQ", "0127")].importe_con_iva == pytest.approx(6200.17, abs=0.01)
    # sal (ES): 0%
    fila_sal = por[("EMP", "7197")]
    assert fila_sal.iva_pct == 0.0
    assert fila_sal.importe_con_iva == pytest.approx(5000.0, abs=0.01)


def test_estado_mes_pendientes_y_total():
    est = _estado_mock()
    assert est["pendientes"] == 2  # AVQ 0127 + EMP 7197
    assert est["total_pendiente"] == pytest.approx(6200.17 + 5000.0, abs=0.02)


def test_estado_mes_sin_bridge():
    with patch.object(fb.formulas_db, "disponible", return_value=False):
        est = fb.estado_mes(2026, 7)
    assert est["disponible"] is False
    assert est["filas"] == []


# ── sincronizar_mes ─────────────────────────────────────────────────────────

def test_sincronizar_crea_solo_pendientes():
    llamadas = []

    def _crear(**kw):
        llamadas.append(kw)
        return {"id_compra": 99, "numero": 500 + len(llamadas)}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=GRUPOS), \
         patch.object(fb.db, "fetch_all", return_value=COMPRAS_PC), \
         patch("modules.compras.queries.crear", side_effect=_crear):
        rep = fb.sincronizar_mes(2026, 7, usuario="formulas-test")

    assert rep["disponible"] is True
    assert len(rep["creadas"]) == 2
    assert rep["errores"] == []
    assert rep["ya_cargadas"] == 2
    por_prov = {c["proveedor"]: c for c in rep["creadas"]}
    assert set(por_prov) == {"AQ", "ES"}
    # argumentos del alta: tipo Q, clave F, concepto formato dBase, importe c/IVA
    kw_aq = next(k for k in llamadas if k["codigo_prov"] == "AQ")
    assert kw_aq["tipo"] == "Q"
    assert kw_aq["clave"] == "F"
    assert kw_aq["pagada"] is False
    assert kw_aq["fecha"] == date(2026, 7, 14)
    assert kw_aq["importe"] == pytest.approx(6200.17, abs=0.01)
    assert kw_aq["concepto"].startswith("127")
    assert kw_aq["concepto"].endswith("14")
    assert kw_aq["usuario"] == "formulas-test"
    kw_es = next(k for k in llamadas if k["codigo_prov"] == "ES")
    assert kw_es["importe"] == pytest.approx(5000.0, abs=0.01)


def test_sincronizar_un_error_no_frena_al_resto():
    intentos = []

    def _crear(**kw):
        intentos.append(kw)
        if kw["codigo_prov"] == "AQ":
            raise ValueError("proveedor bloqueado")
        return {"id_compra": 1, "numero": 1}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=GRUPOS), \
         patch.object(fb.db, "fetch_all", return_value=COMPRAS_PC), \
         patch("modules.compras.queries.crear", side_effect=_crear):
        rep = fb.sincronizar_mes(2026, 7)

    assert len(intentos) == 2
    assert len(rep["creadas"]) == 1
    assert len(rep["errores"]) == 1
    assert rep["errores"][0]["proveedor"] == "AQ"


def test_sincronizar_sin_bridge_no_crea():
    with patch.object(fb.formulas_db, "disponible", return_value=False), \
         patch("modules.compras.queries.crear") as crear_mock:
        rep = fb.sincronizar_mes(2026, 7)
    assert rep["disponible"] is False
    crear_mock.assert_not_called()


# ── autosync / hook del cron ────────────────────────────────────────────────

def test_autosync_habilitado_default_on(monkeypatch):
    monkeypatch.delenv("FORMULAS_COMPRAS_AUTOSYNC", raising=False)
    assert fb.autosync_habilitado() is True


@pytest.mark.parametrize("valor", ["0", "false", "OFF", "no"])
def test_autosync_apagable(monkeypatch, valor):
    monkeypatch.setenv("FORMULAS_COMPRAS_AUTOSYNC", valor)
    assert fb.autosync_habilitado() is False


def test_sincronizar_mes_actual_apagado(monkeypatch):
    monkeypatch.setenv("FORMULAS_COMPRAS_AUTOSYNC", "0")
    rep = fb.sincronizar_mes_actual()
    assert rep.get("apagado") is True
    assert rep["creadas"] == []


def test_sincronizar_mes_actual_nunca_levanta(monkeypatch):
    monkeypatch.delenv("FORMULAS_COMPRAS_AUTOSYNC", raising=False)
    with patch.object(fb, "sincronizar_mes", side_effect=RuntimeError("boom")):
        rep = fb.sincronizar_mes_actual()
    assert rep["disponible"] is False
    assert rep["errores"]


def test_contar_pendientes_fail_soft():
    with patch.object(fb, "estado_mes", side_effect=RuntimeError("boom")):
        assert fb.contar_pendientes_mes_actual() == 0


def test_contar_pendientes_ok():
    with patch.object(fb, "estado_mes", return_value={"pendientes": 3}):
        assert fb.contar_pendientes_mes_actual(hoy=date(2026, 7, 17)) == 3
