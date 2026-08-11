"""El bloque "Órdenes de tintura" del detalle de factura ya no existe.

TMT 2026-08-11 (dueña, viéndolo con el cartel "DATOS DE DEMO" y la tabla
vacía): *"datos demo y órdenes de tintura creo que no se usa, si es así,
borrar"*.

No se usaba, y no podía usarse: `modules/costos_ot` pedía "costos por cliente"
a formulas_app, y formulas_app NO TIENE CLIENTES — no hay tabla, ni columna, ni
nada en la orden que diga para quién es. Eso lo sabe el ERP, no la app de
tintorería. Por eso el `PostgresAdapter` estaba deshabilitado a propósito y el
ambiente quedó en el adapter `fake`: el bloque sólo podía mostrar datos de
mentira o una tabla vacía.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def test_el_modulo_no_esta():
    assert not Path("modules/costos_ot").exists()


def test_nadie_lo_nombra():
    """Un módulo borrado que sigue nombrado en un template es un 500 esperando:
    `url_for` de un endpoint inexistente revienta la página entera."""
    vivos = []
    for p in list(Path("modules").rglob("*.py")) + list(Path("modules").rglob("*.html")):
        if "costos_ot" in p.read_text():
            vivos.append(str(p))
    for f in ("app.py", ".coveragerc"):
        if "costos_ot" in Path(f).read_text():
            vivos.append(f)
    assert vivos == [], vivos


def test_el_detalle_de_factura_sigue_de_pie(app, fake_db):
    """Lo que se sacó era un bloque al pie; el resto de la ficha no se toca."""
    rid = fake_db.add_role("T", ["facturas.ver"])
    uid = fake_db.add_user("t", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    r = c.get("/facturas/1")
    assert r.status_code in (200, 302, 404)      # existe o no, pero no 500
