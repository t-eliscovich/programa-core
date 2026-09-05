"""Los renglones de la traza que hablaban en clave (Tamara 05/09/2026, "todas").

  · la CAJA va en palabras: "Caja VI → retiro", "entró a caja", "Caja → compra";
  · el devengo de YY/RT se llama "provisiones del día", no "deudas corregidas";
  · el concepto de la deuda no arrastra el día que le pegaba el dBase
    ("22458 6" → "factura 22458");
  · N notas de débito contra deudas son "deudas pagadas por banco".
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.historial import queries as hq  # noqa: E402
from modules.informes import foto  # noqa: E402
from modules.informes import traza as t  # noqa: E402


def test_la_caja_habla_en_palabras():
    assert hq.corto("caja_s_to_retiro_socio", "VI") == "Caja VI → retiro"
    assert hq.corto("caja_s_to_compra_proveedor", "") == "Caja → compra"
    assert hq.corto("caja_e_simple", "") == "entró a caja"
    assert hq.corto("caja_s_directo", "BED") == "salió de caja BED"
    assert hq.corto("caja_s_to_xgast", "") == "Caja → gasto"
    assert hq.corto("cheque_emitido_caja", "") == "CH → caja"
    # Los códigos que definió la dueña siguen.
    assert hq.corto("cheque_aplicado_a_factura", "LES") == "CH LES → FA"


def test_el_devengo_de_provisiones_tiene_nombre():
    regla, fam = foto.regla("totp", "cambio", "p1", 33.3, etiqueta="Deuda YY 2026 · Décimos")
    assert (regla, fam) == ("Provisión del día", "utilidad")
    regla, _ = foto.regla("totp", "cambio", "p2", 1.0, etiqueta="Deuda RT 2026 · reserva")
    assert regla == "Provisión del día"
    # Una deuda común corregida sigue siendo eso.
    regla, _ = foto.regla("totp", "cambio", "p3", 1.0, etiqueta="Deuda SY 10020 · factura 22458")
    assert regla == "Deuda corregida"
    assert t.PLURALES["Provisión del día"] == "provisiones del día"


def test_el_concepto_de_la_deuda_pierde_el_dia_del_dbase():
    assert foto._concepto_deuda("22458          6") == "factura 22458"
    assert foto._concepto_deuda("22458 31") == "factura 22458"
    assert foto._concepto_deuda("TOSAVA 227") == "TOSAVA 227"
    assert foto._concepto_deuda("") == ""


def test_las_notas_de_debito_a_deudas_son_deudas_pagadas_por_banco():
    def _ev(g, doc):
        return {"tipo": "nota_debito", "grupo": g, "id_mov_doble": g, "destino_table": "posdat",
                "importe": 3000.0, "meta": {}, "docs": [doc],
                "label": "Nota de débito", "dia": "2026-09-05"}
    movs = [
        {"componente": "totp", "doc_id": "p1", "tipo": "baja",
         "etiqueta": "Deuda AP 100 · factura 1", "regla": "Deuda pagada o dada de baja",
         "aporte": 3000.0, "familia": "traspaso"},
        {"componente": "totp", "doc_id": "p2", "tipo": "baja",
         "etiqueta": "Deuda SY 200 · factura 2", "regla": "Deuda pagada o dada de baja",
         "aporte": 3000.0, "familia": "traspaso"},
    ]
    with patch.object(t.db, "fetch_all", return_value=[]):
        out = t.resumir(movs, 6000.0, {"p1": _ev("m1", "p1"), "p2": _ev("m2", "p2")})
    assert out[0]["texto"].startswith("2 deudas pagadas por banco")
    assert "AP" in out[0]["texto"] and "SY" in out[0]["texto"]
