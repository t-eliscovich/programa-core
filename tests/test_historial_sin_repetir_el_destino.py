"""El destino que dice lo MISMO que el origen no se escribe.

TMT 2026-08-09, mirando dos "Posdat: edit de importe": *"hay algo, repetimos
mucho sueldos pero nunca veo de cuánto a cuánto cambió. tenemos que informar
todo"*.

El nombre del posdatado salía TRES veces en la misma fila —ORIGEN, DESTINO y
CONCEPTO— y con eso el dato que la fila existe para contar (de cuánto a cuánto
quedó el importe) no entraba en su columna: *"47.900,00 → 100,…"*.

No es un problema de los posdatados: un montón de movimientos son de UN solo
documento (la factura que se emite, el cheque que se crea) y el `mov_doble` los
guarda con el mismo origen y destino. Medido en producción el 09/08: ~11.000
filas activas escribían dos veces lo mismo, una al lado de la otra.

🚨 Se compara la ETIQUETA, no el id: `retiro_op` también es auto-referencia
pero se lee "OP · AC 17-35 → Retiro #17", que es exactamente lo que la dueña
pidió el 14/07.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.historial import queries  # noqa: E402


def _login(app, fake_db):
    rid = fake_db.add_role("Ver", ["historial.ver", "informes.ver"])
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _mov(**kw):
    base = {"id_mov": 1, "tipo": "factura_emitida", "fecha": None,
            "origen_table": "factura", "origen_id": 7,
            "destino_table": "factura", "destino_id": 7,
            "importe": 100.0, "concepto": "x", "usuario": "u",
            "estado": "activo", "metadata": None, "batch_id": None}
    base.update(kw)
    return base


def _filas(app, fake_db, movs):
    c = _login(app, fake_db)
    with patch.object(queries, "listar", return_value=list(movs)), \
         patch.object(queries, "conteos", return_value={"total_activos": 0, "n_activos": 0}), \
         patch.object(queries, "resumen_batches", return_value={}), \
         patch.object(queries, "op_cuenta_por_retiro", return_value={}):
        r = c.get("/historial")
    assert r.status_code == 200, r.data[:300]
    return r.data.decode()


def test_el_mismo_documento_de_los_dos_lados_se_escribe_una_vez(app, fake_db):
    """Cada celda escribe su etiqueta DOS veces —el `title` del tooltip y el
    texto—, así que la fila entera pasa de cuatro apariciones a dos."""
    repetido = _filas(app, fake_db, [_mov()])
    distinto = _filas(app, fake_db, [_mov(
        destino_table="cheque", destino_id=3)])
    assert repetido.count("Factura #7") == 2
    assert distinto.count("Factura #7") == 2   # el origen, intacto
    assert distinto.count("Cheque #3") == 2    # y el destino, escrito


def test_origen_y_destino_distintos_se_escriben_los_dos(app, fake_db):
    html = _filas(app, fake_db, [_mov(
        tipo="cheque_aplicado_a_factura",
        origen_table="cheque", origen_id=3,
        destino_table="factura", destino_id=7)])
    assert "Cheque #3" in html
    assert "Factura #7" in html


def test_retiro_op_sigue_mostrando_las_dos_patas(app, fake_db):
    """Self-ref, pero las etiquetas NO son iguales: el origen se fuerza a OP."""
    html = _filas(app, fake_db, [_mov(
        tipo="retiro_op", origen_table="retiros", origen_id=17,
        destino_table="retiros", destino_id=17)])
    assert "OP" in html
    assert "Retiro #17" in html
