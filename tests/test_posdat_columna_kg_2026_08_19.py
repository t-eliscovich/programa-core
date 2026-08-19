"""La columna Kg de /posdat — TMT 2026-08-19.

Dueña, con la grilla de Pasivos a la vista: *"agregar después de concepto una
columna que diga KG y traiga los KGs"*.

Un posdatado NO tiene kilos: es una deuda en dólares. Los kilos viven en la
COMPRA que generó esa deuda, y el vínculo ya existía — el `mov_doble`
`compra_a_posdat` que escribe `compras.crear()`. Lo que protege este archivo:

· la columna sale ENTRE Concepto e Importe (no al final, que es donde termina
  cualquier columna agregada de apuro);
· una deuda sin compra detrás (las heredadas del dBase) o de una compra sin
  kilos (servicios, químicos) muestra un guión, no un 0 que se lee como dato;
· en la pestaña YY no aparece: las provisiones nunca tuvieron kilos y una
  columna siempre vacía es una columna que miente;
· el pie NO totaliza los kilos: la lista se corta en 500 filas y el total de
  Importe viene del universo completo — dos totales de universos distintos, uno
  al lado del otro, mienten.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def app_logueada(app):
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    return app


FILAS = [
    # De una compra de maquila: trae kilos.
    {"id_posdat": 937, "num": 1, "fecha": "2026-07-10", "fechad": "2026-08-10",
     "prov": "AP", "importe": 588.24, "banc": 0, "kg": 588.24,
     "concepto": "OFT-000038931 A PONCE", "proveedor": "PONCE"},
    # Heredada del dBase: no hay compra detrás, no hay kilos.
    {"id_posdat": 368, "num": 2, "fecha": "2025-08-14", "fechad": "2026-08-01",
     "prov": "QI", "importe": 1426.0, "banc": 0, "kg": None,
     "concepto": "133641  14", "proveedor": "QUIMICA"},
]


def _html(app_logueada, monkeypatch, filas, url="/posdat"):
    from modules.posdat import views as pv

    monkeypatch.setattr(pv.queries, "buscar", lambda **kw: list(filas))
    monkeypatch.setattr(pv.queries, "resumen", lambda **kw: {})
    monkeypatch.setattr(pv.queries, "persistir_acumulacion_yy", lambda: None)
    r = app_logueada.test_client().get(url)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _encabezados(html: str) -> list[str]:
    tabla = html[html.index('id="posdat-tabla"'):]
    thead = tabla[:tabla.index("</thead>")]
    return [re.sub(r"<[^>]+>", "", th).strip()
            for th in re.findall(r"<th[^>]*>(.*?)</th>", thead, re.S)]


def test_kg_va_justo_despues_de_concepto(app_logueada, monkeypatch):
    cols = _encabezados(_html(app_logueada, monkeypatch, FILAS))
    assert "Kg" in cols, cols
    assert cols.index("Kg") == cols.index("Concepto") + 1, cols
    assert cols.index("Kg") < cols.index("Importe"), cols


def test_muestra_los_kilos_y_un_guion_cuando_no_hay(app_logueada, monkeypatch):
    html = _html(app_logueada, monkeypatch, FILAS)
    assert "588,24" in html                      # formato EU/Ecuador
    # La fila sin compra detrás no inventa un 0: pone el guión de "no hay dato".
    tabla = html[html.index('id="posdat-tabla"'):]
    fila_qi = re.search(r'<tr[^>]*data-id-posdat="368".*?</tr>', tabla, re.S).group(0)
    # data-sort-value="0" es la celda de Kg vacía (la de Venc. ordena por fecha
    # ISO): sin esto el test podría estar mirando otra columna.
    celda_kg = re.search(r'<td[^>]*data-sort-value="0"[^>]*>(.*?)</td>',
                         fila_qi, re.S).group(1)
    assert "—" in celda_kg
    assert "0,00" not in celda_kg


def test_en_la_pestana_yy_la_columna_no_existe(app_logueada, monkeypatch):
    filas_yy = [{"id_posdat": 1, "num": 1, "fecha": "2026-08-01",
                 "fechad": "2026-08-31", "prov": "YY", "importe": 33300.0,
                 "banc": 0, "concepto": "SUELDOS", "proveedor": "",
                 "cuota_diaria": 1110.0, "cuota_mensual": 33300.0}]
    cols = _encabezados(_html(app_logueada, monkeypatch, filas_yy,
                              url="/posdat?tab=yy"))
    assert "Kg" not in cols, cols


def test_el_pie_no_totaliza_kilos(app_logueada, monkeypatch):
    html = _html(app_logueada, monkeypatch, FILAS)
    pie = html[html.index("<tfoot"):]
    # El único total del pie sigue siendo el de Importe (2.014,24).
    assert "588,24" not in pie[:pie.index("</tfoot>")]


def test_el_kg_sale_de_la_compra_por_mov_doble_no_del_concepto():
    """El SQL tiene que colgarse del vínculo compra→posdat, no del texto.

    Si mañana alguien lo cambia por un LIKE sobre el concepto, esto falla: el
    concepto es editable inline desde la propia pantalla."""
    import inspect

    from modules.posdat import queries as q
    src = inspect.getsource(q.buscar)
    assert "scintela.mov_doble" in src
    assert "destino_table = 'posdat'" in src
    assert "origen_table = 'compra'" in src
    assert "kgc.kg AS kg" in src
