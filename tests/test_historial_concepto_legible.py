"""El concepto del audit, escrito como lo lee una persona.

TMT 2026-08-09, después de arreglar caja/gastos: *"¿algo más? mostrame
ejemplos"*. Barrido sobre los 19.465 movimientos activos:

  · **15.539** conceptos escriben "#" delante de un número que casi siempre es
    el VISIBLE (`numf`, `no_cheque`): el "#" lo hace pasar por id interno, que
    es exactamente la confusión del día ("¿el número es del cheque real o del
    programa?");
  · **4.508** terminan con el importe entre paréntesis — el MISMO número que
    ya está en la columna Importe, y encima con punto decimal;
  · **5.402** arrancan con "[backfill]", el nombre del proceso que los cargó;
  · **480** dicen "#0": las facturas del dBase que llegaron sin número;
  · **177** dicen "Gasto #1", donde el 1 es la CATEGORÍA V1.

Se limpia al MOSTRAR: `mov_doble.concepto` es el audit y no se toca.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.historial import queries  # noqa: E402
from modules.historial.views import _concepto_legible as limpio  # noqa: E402


@pytest.mark.parametrize("crudo, legible", [
    # El numeral se va: esos números son los que ella lee en las pantallas.
    ("Cheque #100392 → Factura #176812 (155.10)", "Cheque 100392 → Factura 176812"),
    ("Dep. cheque #100007 EDR", "Dep. cheque 100007 EDR"),
    ("DEVOLUCION Factura #10973 DSN", "DEVOLUCION Factura 10973 DSN"),
    # El proceso que la cargó no es información.
    ("[backfill] Factura #0 BED", "Factura s/n · BED"),
    # El 1 es la categoría V1, no un correlativo.
    ("[backfill] Gasto #1 OTR — KK SU CAJA", "Gasto V1 · KK SU CAJA"),
    # Y acá el 368 SÍ era un id interno: se va con la frase entera.
    ("Clasificar caja #368 como gasto V7: CCSU CAJA",
     "Caja → Gasto V7 · CCSU CAJA"),
])
def test_conceptos_reales_del_historial(crudo, legible):
    assert limpio(crudo) == legible


def test_el_importe_repetido_se_va():
    """Es el mismo número de la columna Importe, con punto decimal."""
    assert limpio("Dep. Pich. → Factura #172730 (1283.19)") == \
        "Dep. Pich. → Factura 172730"


def test_no_se_come_un_parentesis_que_no_es_un_importe():
    assert limpio("Cheque 55 (parcial)") == "Cheque 55 (parcial)"


def test_un_concepto_limpio_no_cambia():
    assert limpio("SU ADM QUINTEROS") == "SU ADM QUINTEROS"
    assert limpio("") == ""
    assert limpio(None) == ""


def test_los_reversos_que_faltaban_tienen_castellano():
    assert queries.label("reverso_retiro_op") == "Reverso: retiro OP"
    assert queries.label("reverso_cheque_emitido") == "Reverso: cheque emitido"
    assert queries.label("reverso_retiro_dbase") == "Reverso: retiro"


# ── el concepto que repite las dos columnas ─────────────────────────────────
def test_el_concepto_que_solo_repite_origen_y_destino_se_va(app, fake_db):
    """Una vez limpio, "Dep. Pich. → Factura 172730" decía exactamente lo
    mismo que las columnas ORIGEN y DESTINO. Son 5.038 filas."""
    from unittest.mock import patch

    import db

    def _fake(sql, params=None, *a, **k):
        if "FROM scintela.cheque c" in (sql or ""):
            return [{"id_cheque": 102090, "no_cheque": "", "no_banco": 90,
                     "banco_nombre": "DEP.PICH."}]
        if "FROM scintela.factura" in (sql or ""):
            return [{"id_factura": 7, "numf": "172730"}]
        return []

    rid = fake_db.add_role("Ver", ["historial.ver", "informes.ver"])
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    mov = {"id_mov": 1, "tipo": "cheque_aplicado_a_factura", "fecha": None,
           "origen_table": "cheque", "origen_id": 102090,
           "destino_table": "factura", "destino_id": 7,
           "importe": 1283.19, "estado": "activo", "metadata": None,
           "batch_id": None, "usuario": "alex",
           "concepto": "Cheque #102090 → Factura #172730 (1283.19)"}
    with patch.object(queries, "listar", return_value=[mov]), \
         patch.object(queries, "conteos", return_value={"total_activos": 0}), \
         patch.object(queries, "resumen_batches", return_value={}), \
         patch.object(queries, "op_cuenta_por_retiro", return_value={}), \
         patch.object(db, "fetch_all", side_effect=_fake):
        html = c.get("/historial").data.decode()
    assert "Dep. Pich." in html
    assert "Factura 172730" in html
    # la frase del concepto ya no está: la dicen las columnas
    assert "Dep. Pich. → Factura 172730" not in html


def test_una_factura_sin_numero_no_se_llama_cero(app, fake_db):
    """`numf = 0` son las 538 importadas del dBase: "Factura 0" es un número
    que no existe."""
    from unittest.mock import patch

    import db

    def _fake(sql, params=None, *a, **k):
        if "FROM scintela.factura" in (sql or ""):
            return [{"id_factura": 274226, "numf": "0"}]
        return []

    rid = fake_db.add_role("Ver", ["historial.ver", "informes.ver"])
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    mov = {"id_mov": 1, "tipo": "factura_emitida", "fecha": None,
           "origen_table": "factura", "origen_id": 274226,
           "destino_table": "factura", "destino_id": 274226,
           "importe": 100.0, "estado": "activo", "metadata": None,
           "batch_id": None, "usuario": "u", "concepto": "x"}
    with patch.object(queries, "listar", return_value=[mov]), \
         patch.object(queries, "conteos", return_value={"total_activos": 0}), \
         patch.object(queries, "resumen_batches", return_value={}), \
         patch.object(queries, "op_cuenta_por_retiro", return_value={}), \
         patch.object(db, "fetch_all", side_effect=_fake):
        html = c.get("/historial").data.decode()
    assert "Factura s/n" in html
    assert "Factura 0" not in html
