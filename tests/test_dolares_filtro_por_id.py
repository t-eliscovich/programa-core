"""El link del historial a un anticipo tiene que caer EN ESA FILA.

TMT 2026-08-07 (dueña, sobre los links del historial): *"si al clickear hay
que buscar la fila a ojo, el link no está terminado"*.

El historial linkeaba a `/dolares?cta=` con `cta` **vacío**: un no-op. El link
no daba 404 (la ruta existe) pero soltaba a la dueña en la pantalla entera de
anticipos a buscar la fila a ojo — un link roto sin dar 404.

Y el caso de /dolares es el más traicionero de todos, porque la pantalla no
sólo NO filtraba: filtraba EN CONTRA. Trae `solo_vivos` prendido por default
(nadie lo pide, viene "1" del querystring ausente) y el SQL descarta todo lo
que tenga `st` no vacío. Medido contra producción: **132 de las 149 filas
linkeadas desde el historial (89%) estaban invisibles**, y no por casualidad —
el anticipo que ya se convirtió/aplicó es JUSTAMENTE el que generó el
movimiento que se está mirando. O sea: el link llevaba a una lista donde la
fila buscada estaba deliberadamente escondida.

Los tests de acá abajo cubren, en orden: el WHERE (lo fácil), el apagado de
`solo_vivos` (lo que importa), qué agregados reciben el id y cuáles NO a
propósito, y que la pantalla filtrada no estrena ningún cartel.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.dolares import queries as dq  # noqa: E402

# ── 1) el SQL ────────────────────────────────────────────────────────────────


class _CapturaSQL:
    """Guarda TODAS las consultas: `lista()` hace varias (la lista, los tipos
    de proveedor…) y quedarse con la última captura la equivocada."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def fetch_all(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return []

    def fetch_one(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return {}

    def principal(self) -> tuple[str, dict]:
        """La consulta que lista scintela.dolares — la del parámetro id_dolares."""
        for sql, params in self.queries:
            if isinstance(params, dict) and "id_dolares" in params:
                return sql, params
        raise AssertionError(
            "ninguna consulta recibió el parámetro id_dolares; se hicieron: "
            + " | ".join(q[:70] for q, _ in self.queries))


@pytest.fixture
def cap(monkeypatch):
    c = _CapturaSQL()
    monkeypatch.setattr(dq, "db", c)
    return c


def test_lista_por_id_devuelve_esa_sola_fila(cap):
    dq.lista(id_dolares=8821)
    sql, params = cap.principal()
    assert params["id_dolares"] == 8821
    assert "%(id_dolares)s IS NULL OR d.id_dolares = %(id_dolares)s" in sql


def test_pedir_por_id_APAGA_solo_vivos(cap):
    """EL TEST QUE IMPORTA.

    `solo_vivos` es el único filtro que está prendido sin que nadie lo pida, y
    esconde el 89% de las filas que el historial linkea. Si esta línea del
    WHERE se rompe, el link vuelve a llevar a una lista vacía y parece que el
    movimiento apunta a la nada.
    """
    dq.lista(id_dolares=8821, solo_vivos=True)
    sql, _ = cap.principal()
    assert ("%(id_dolares)s IS NOT NULL OR NOT %(solo_vivos)s "
            "OR d.st IS NULL OR d.st IN ('', ' ')") in sql


def test_sin_id_la_pantalla_de_siempre_no_cambia(cap):
    """El caso normal: `id_dolares` viaja NULL y no filtra nada."""
    dq.lista()
    _, params = cap.principal()
    assert params["id_dolares"] is None


def test_el_id_viaja_como_parametro_y_no_concatenado(cap):
    """?id= no se puede colar en el SQL: entra siempre por psycopg."""
    dq.lista(id_dolares=8821)
    sql, _ = cap.principal()
    assert "8821" not in sql


# ── 2) la vista ──────────────────────────────────────────────────────────────

@pytest.fixture
def app_logueada(app):
    """La app real con sesión de Accionista (wildcard de permisos).

    Se registra DESPUÉS de create_app() a propósito: `app.py` importa
    `load_logged_in_user` en el import, así que pisarlo no sirve una vez que
    el módulo ya está cargado.
    """
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    return app


UNA_FILA = [{
    "id_dolares": 8821, "fecha": None, "cta": "AC", "concepto": "58/26",
    # `st='B'` = anticipo YA convertido a compra: el caso del 89%. Si algo
    # volviera a filtrar por "solo vivos", esta fila desaparece.
    "importe": 2000.0, "st": "B", "clave": "", "usuario_crea": "web",
    "saldo_acumulado": 2000.0,
}]

UNIVERSO = {
    "total_vivos": 45000.0, "n_vivos": 320,
    "total_aplicados": 900000.0, "n_aplicados": 1200, "n_ctas_vivos": 40,
}


@pytest.fixture
def espia(monkeypatch):
    """Intercepta las 3 consultas de la pantalla y anota con qué las llamaron."""
    from modules.dolares import views as dv

    visto: dict = {"lista": None, "resumen": 0, "por_cuenta": 0}

    def fake_lista(**kw):
        visto["lista"] = kw
        return [dict(f) for f in UNA_FILA]

    def fake_resumen(*a, **kw):
        visto["resumen"] += 1
        return dict(UNIVERSO)

    def fake_por_cuenta(**kw):
        visto["por_cuenta"] += 1
        return []

    monkeypatch.setattr(dv.queries, "lista", fake_lista)
    monkeypatch.setattr(dv.queries, "resumen", fake_resumen)
    monkeypatch.setattr(dv.queries, "por_cuenta", fake_por_cuenta)
    monkeypatch.setattr(dv.queries, "tipos_por_cuenta", lambda c: {})
    return visto


def test_la_vista_le_pasa_el_id_a_la_lista(app_logueada, espia):
    r = app_logueada.test_client().get("/dolares?id=8821")
    assert r.status_code == 200
    assert espia["lista"]["id_dolares"] == 8821


def test_la_vista_tambien_apaga_solo_vivos(app_logueada, espia):
    """Doble candado: el SQL lo saltea, y la vista además lo apaga para que el
    checkbox «Solo vivos» de la barra de filtros no mienta sobre lo que se está
    viendo."""
    app_logueada.test_client().get("/dolares?id=8821")
    assert espia["lista"]["solo_vivos"] is False


def test_los_demas_filtros_se_hacen_a_un_lado(app_logueada, espia):
    """Pedir un id es pedir ESA fila: no hay nada que combinar. Ninguno de
    estos está prendido por default (el único que lo está es `solo_vivos`),
    pero si llegan por la URL igual esconderían la fila."""
    app_logueada.test_client().get(
        "/dolares?id=8821&cta=ZZ&q=nada&desde=2030-01-01&hasta=2030-12-31"
        "&codigo=ZZ99&anio=1999&recibido_mes=2030-01")
    kw = espia["lista"]
    assert kw["cta"] is None and kw["q"] is None
    assert kw["desde"] is None and kw["hasta"] is None
    # `codigo` es el atajo que ESCRIBE cta y q ("AC 15" → cta=AC, q=15): si se
    # parseara, volvería a meter un cta por la ventana.
    assert kw["cta"] is None


def test_los_postfiltros_en_python_no_se_comen_la_fila(app_logueada, espia):
    """`anio` y `recibido_mes` se aplican sobre `filas` en Python, así que el
    bypass del WHERE no los cubre: se apagan en la vista."""
    html = app_logueada.test_client().get(
        "/dolares?id=8821&anio=1999&recibido_mes=2030-01").get_data(as_text=True)
    assert "1 movimiento" in html


def test_un_id_que_no_es_entero_se_ignora(app_logueada, espia):
    """?id=borrar-todo no puede tirar un 500 ni colarse en el SQL."""
    r = app_logueada.test_client().get("/dolares?id=borrar-todo")
    assert r.status_code == 200
    assert espia["lista"]["id_dolares"] is None


def test_sin_id_la_pantalla_sigue_trayendo_solo_vivos(app_logueada, espia):
    """El default de siempre no se toca para el resto de la casa."""
    app_logueada.test_client().get("/dolares")
    assert espia["lista"]["id_dolares"] is None
    assert espia["lista"]["solo_vivos"] is True


# ── 3) los agregados ─────────────────────────────────────────────────────────

def test_el_hero_no_canta_el_total_del_universo_arriba_de_una_fila(
        app_logueada, espia):
    """El bug que en /posdat se detectó recién rindiendo la pantalla: el hero
    mostraba el total de TODA la lista arriba de una sola fila.

    Acá el hero ya sabía mostrar el subtotal cuando hay filtro activo — sólo
    que `id_dolares` no contaba como filtro. Con la fila de 2.000 el hero dice
    2.000 y aclara «1 filtrados de 320», no 45.000.
    """
    html = app_logueada.test_client().get(
        "/dolares?id=8821").get_data(as_text=True)
    assert "US$ 2.000" in html
    assert "1 filtrados de 320" in html


def test_los_kpis_del_balance_NO_se_filtran_por_id(app_logueada, espia):
    """DECISIÓN EXPLÍCITA (no es un olvido).

    La tira de 4 KPIs es la conciliación contra el balance: dice, con esas
    palabras, «éste es el ANTICIPOS del balance». Filtrarla por id haría que
    esa card mostrara el importe de UN anticipo suelto afirmando que es el
    ANTICIPOS del balance — el mismo descalce del 31/07 (*"que nunca más
    vuelva a pasar que esto nos va a mover la utilidad por 20k"*). Es plata
    real, no un subtotal de lo visible: se queda con el universo siempre.

    Ídem `por_cuenta()`: saldo vivo POR CUENTA, ya hoy inmune a todos los
    filtros (con ?cta=ABC igual se dibujan las 12 cards).
    """
    html = app_logueada.test_client().get(
        "/dolares?id=8821").get_data(as_text=True)
    assert "45.000" in html          # Total vivo = el universo, intacto
    assert espia["resumen"] >= 1
    assert espia["por_cuenta"] >= 1


def test_la_pantalla_filtrada_no_estrena_ningun_cartel(app_logueada, espia):
    """DECISIÓN DE LA DUEÑA (2026-08-07): *"sin el cartel"*.

    Este test está dado vuelta a propósito, no borrado: si mañana alguien mete
    un banner «mostrando un solo anticipo», que falle acá y lea esta decisión
    antes de re-discutirla. La pantalla filtrada se ve igual que siempre, con
    una fila."""
    html = app_logueada.test_client().get(
        "/dolares?id=8821").get_data(as_text=True)
    for cartel in ("Mostrando un solo", "mostrando una sola fila",
                   "ver todos los anticipos"):
        assert cartel not in html
    assert html.count("<table") >= 1     # la grilla de siempre, sin adornos
