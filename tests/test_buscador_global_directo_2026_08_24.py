"""El buscador de arriba (Ctrl+K) tiene que dejarte EN el dato, no en una lista.

TMT 2026-08-24 (dueña, sobre el overlay): *"acá me gustaría que vaya directo
al cliente, y factura me podría mostrar clientes con esa factura"*.

Dos cosas distintas que fallaban:

1. **Sin prefijo.** Tipear `iia` caía en la landing de Estados de cuenta con
   un solo renglón, que había que volver a clickear. Si lo que escribió no
   deja lugar a dudas —el código exacto, o un único cliente— se abre la ficha.

2. **`f:`** iba a `/facturas?q=N` pelado, y ahí la vista por defecto es la
   CARTERA: sólo las facturas VIVAS. Buscar una ya cobrada (stat T) o
   anulada contestaba *"0 facturas"*, que es lo peor que puede contestar un
   buscador: parece que el dato no existe. Con `vista=estado` se miran todos
   los estados y todas las fechas, y la lista trae la columna Cliente.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modules.informes import views as iv

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def logueado(app):
    @app.before_request
    def _login():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}
    return app.test_client()


def _clientes(monkeypatch, filas):
    monkeypatch.setattr(iv.queries, "buscar_clientes", lambda q, *a, **k: filas)
    monkeypatch.setattr(iv.queries, "cartera_por_cliente", lambda *a, **k: [])


# ---------------------------------------------------------------------------
# 1. Sin prefijo → la ficha del cliente
# ---------------------------------------------------------------------------

def test_el_codigo_exacto_abre_la_ficha(logueado, monkeypatch):
    _clientes(monkeypatch, [
        {"codigo_cli": "IIA", "nombre": "ANDRADE ZAMBRANO IDA ISABEL"},
        {"codigo_cli": "IIA2", "nombre": "OTRO PARECIDO"},
    ])
    r = logueado.get("/informes/estado-cuenta?q=iia")
    assert r.status_code == 302, "con el código exacto no hay nada que elegir"
    assert r.headers["Location"].endswith("/estado-cuenta/IIA")


def test_un_solo_match_abre_la_ficha(logueado, monkeypatch):
    _clientes(monkeypatch, [{"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA"}])
    r = logueado.get("/informes/estado-cuenta?q=chicaiza")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/estado-cuenta/ICH")


def test_con_varios_matches_sigue_mostrando_la_lista(logueado, monkeypatch):
    _clientes(monkeypatch, [
        {"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA"},
        {"codigo_cli": "MCH", "nombre": "MARIO CHICAIZA"},
    ])
    r = logueado.get("/informes/estado-cuenta?q=chicaiza")
    assert r.status_code == 200, "con dos candidatos hay que elegir, no adivinar"


def test_el_quisiste_decir_nunca_abre_solo(logueado, monkeypatch):
    """Una fila aproximada es una SUGERENCIA. Abrirla sola te deja mirando la
    cuenta de otro cliente creyendo que es el que buscaste."""
    _clientes(monkeypatch, [
        {"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA", "aprox": True},
    ])
    r = logueado.get("/informes/estado-cuenta?q=chicaisa")
    assert r.status_code == 200


def test_se_puede_pedir_la_lista_igual(logueado, monkeypatch):
    """`?lista=1` deja ver los candidatos aunque haya uno solo — para el día
    que la dueña quiera confirmar que no hay otro parecido."""
    _clientes(monkeypatch, [{"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA"}])
    r = logueado.get("/informes/estado-cuenta?q=chicaiza&lista=1")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 2. f: → la factura, en cualquier estado
# ---------------------------------------------------------------------------

def test_el_atajo_de_factura_no_se_queda_en_la_cartera():
    i = APP_JS.index("kind.toLowerCase() === 'f'")
    bloque = APP_JS[i:i + 600]
    url = bloque[bloque.index("url ="):bloque.index("\n", bloque.index("url ="))]
    assert "vista=estado" in url, (
        "sin vista=estado el atajo mira sólo la cartera viva: una factura "
        "cobrada o anulada contesta '0 facturas' y parece que no existe"
    )


def test_el_atajo_de_factura_no_fija_fechas():
    """Si le clavara un desde/hasta, una factura vieja volvería a no aparecer."""
    i = APP_JS.index("kind.toLowerCase() === 'f'")
    bloque = APP_JS[i:i + 600]
    url = bloque[bloque.index("url ="):bloque.index("\n", bloque.index("url ="))]
    assert "desde=" not in url and "hasta=" not in url


def test_la_ayuda_no_dice_numf():
    """`numf` es el nombre de la columna, no algo que se le diga a nadie."""
    tips = APP_JS[APP_JS.index("<b>Atajos:</b>"):APP_JS.index("</div>`")]
    assert "numf" not in tips
