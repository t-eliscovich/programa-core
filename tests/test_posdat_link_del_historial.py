"""El link «Posdat #N» del historial tiene que llevar A ESE posdatado.

TMT 2026-08-07 (dueña, sobre un mov "Posdat: edit de importe" del 07/08):
*"podés ver por qué el link me manda a proveedores y no al posdatado que se
menciona?"* — y después: *"esos links deberían venir filtrados por lo que
quiero ver"*.

Eran DOS cosas distintas en la misma fila:

1. `link_origen` devolvía `"/proveedores"` para la tabla `posdat`, hardcodeado
   desde el primer commit. El test que recorre los links del historial no lo
   cazaba porque /proveedores EXISTE: resolvía contra el url_map, sólo que
   llevaba a la pantalla equivocada. **Un link puede estar roto sin dar 404.**

2. La etiqueta decía el **id interno** (`Posdat #134`) mientras que el CONCEPTO
   del mismo movimiento ya venía escrito con el **`num` visible**
   (`Edit importe posdat #134…`, posdat/queries.py). Dos números con la misma
   pinta que no son el mismo número — mismo problema que ya se arregló para
   facturas (`numf`) y compras (`numero`).

Y el destino tampoco podía ser `/posdat` a secas: la lista filtra por defecto
sólo deuda viva (`banc=0`), excluye YY/RT y excluye anuladas, así que el link a
un posdatado pagado o anulado caía en una lista vacía.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.historial import queries as hq  # noqa: E402
from modules.posdat import queries as pq  # noqa: E402

# ── 1) el link ───────────────────────────────────────────────────────────────


def test_el_link_ya_no_manda_a_proveedores():
    url, _ = hq.link_origen({"origen_table": "posdat", "origen_id": 134})
    assert url != "/proveedores"
    assert url == "/posdat?id=134"


def test_la_url_va_por_id_interno_y_la_etiqueta_por_el_num_visible():
    """Mismo criterio que compras: un `num` puede ser el id de OTRO posdatado."""
    url, etiqueta = hq.link_origen(
        {"origen_table": "posdat", "origen_id": 134},
        posdat_nums={134: "877"},
    )
    assert url == "/posdat?id=134"      # id interno en la URL
    assert etiqueta == "Posdat 877"     # número visible en la etiqueta


def test_sin_num_conocido_cae_al_id_con_numeral():
    _, etiqueta = hq.link_origen({"origen_table": "posdat", "origen_id": 134})
    assert etiqueta == "Posdat #134"


def test_el_lado_destino_usa_el_mismo_mapeo():
    """link_destino delega en link_origen: si no le pasa posdat_nums, la
    columna DESTINO mostraría el id interno y la ORIGEN el num."""
    url, etiqueta = hq.link_destino(
        {"destino_table": "posdat", "destino_id": 134},
        posdat_nums={134: "877"},
    )
    assert url == "/posdat?id=134" and etiqueta == "Posdat 877"


# ── 2) la lista pedida por id ────────────────────────────────────────────────

class _CapturaSQL:
    """Guarda TODAS las consultas. `buscar()`/`resumen()` hacen varias
    (existencia de columnas, nombres de proveedor…): quedarse con la última
    captura la equivocada."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def _rec(self, sql, params):
        self.queries.append((" ".join(sql.split()), params or {}))

    def fetch_all(self, sql, params=None):
        self._rec(sql, params)
        return []

    def fetch_one(self, sql, params=None):
        self._rec(sql, params)
        return {"total_abierto": 0, "partidas_abiertas": 0}

    def principal(self) -> tuple[str, dict]:
        """La consulta que lista/suma posdat — la que menciona id_posdat."""
        for sql, params in self.queries:
            if isinstance(params, dict) and "id_posdat" in params:
                return sql, params
        raise AssertionError(
            "ninguna consulta recibió el parámetro id_posdat; se hicieron: "
            + " | ".join(q[:70] for q, _ in self.queries))


@pytest.fixture
def cap(monkeypatch):
    c = _CapturaSQL()
    monkeypatch.setattr(pq, "db", c)
    return c


def test_buscar_por_id_filtra_por_esa_fila(cap):
    pq.buscar(id_posdat=134)
    sql, params = cap.principal()
    assert params["id_posdat"] == 134
    assert "%(id_posdat)s IS NULL OR pd.id_posdat = %(id_posdat)s" in sql


def test_pedir_por_id_apaga_el_filtro_de_deuda_viva_y_el_de_anuladas(cap):
    """Un posdatado PAGADO (banc<>0) o ANULADO también tiene que aparecer: el
    movimiento del historial que linkea a él existe justamente porque se pagó
    o se anuló."""
    pq.buscar(id_posdat=134, solo_abiertas=True)
    sql, _ = cap.principal()
    assert ("%(id_posdat)s IS NOT NULL OR NOT %(solo_abiertas)s "
            "OR COALESCE(pd.banc,0) = 0") in sql
    assert ("%(id_posdat)s IS NOT NULL OR pd.anulada IS NOT TRUE "
            "OR pd.anulada IS NULL") in sql


def test_el_filtro_de_TAB_no_se_apaga_ni_pidiendo_por_id(cap):
    """Si se apagara, un posdat YY entra en las DOS pestañas y el hero
    (Total = Posdatados + YY) lo suma dos veces. Verificado en vivo: con el
    bypass, ?id= de una provisión de 4.400 mostraba un total de 8.800."""
    pq.buscar(id_posdat=134)
    sql, _ = cap.principal()
    assert "%(id_posdat)s IS NOT NULL OR (%(tab)s = 'yy'" not in sql


def test_sin_id_la_lista_no_cambia(cap):
    """El caso normal sigue igual: id_posdat viaja como NULL y no filtra nada."""
    pq.buscar()
    _, params = cap.principal()
    assert params["id_posdat"] is None


def test_el_resumen_recibe_el_mismo_id_que_la_lista(cap):
    """Invariante del item 18 (2026-05-19): "X partidas" del hero tiene que
    coincidir con las filas visibles."""
    pq.resumen(id_posdat=134)
    sql, params = cap.principal()
    assert params["id_posdat"] == 134
    assert "%(id_posdat)s IS NULL OR pd.id_posdat = %(id_posdat)s" in sql


# ── 3) la pestaña la elige la vista, no el SQL ───────────────────────────────

@pytest.fixture
def app_logueada(app):
    """La app real con una sesión de Accionista (wildcard de permisos).

    Se registra DESPUÉS de create_app() a propósito: `app.py` importa
    `load_logged_in_user` en el import, así que pisarlo no sirve una vez que el
    módulo ya está cargado (pasa en la suite completa, no corriendo el archivo
    solo).
    """
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    return app


@pytest.mark.parametrize("prov,tab_esperado", [
    ("CO", "posdatados"),
    ("YY", "yy"),
    ("RT", "yy"),          # RT también vive en la pestaña de provisiones
])
def test_pedir_por_id_lleva_a_la_pestana_donde_vive_ese_posdatado(
        app_logueada, monkeypatch, prov, tab_esperado):
    """Sin esto, el link a una provisión caía en la pestaña Posdatados —
    que la excluye — y no mostraba nada."""
    from modules.posdat import views as pv

    vistos = {}

    def fake_buscar(**kw):
        vistos["tab"] = kw.get("tab")
        vistos["id_posdat"] = kw.get("id_posdat")
        return []

    monkeypatch.setattr(pv.queries, "por_id", lambda i: {"prov": prov})
    monkeypatch.setattr(pv.queries, "buscar", fake_buscar)
    monkeypatch.setattr(pv.queries, "resumen", lambda **kw: {})
    monkeypatch.setattr(pv.queries, "persistir_acumulacion_yy", lambda: None)

    r = app_logueada.test_client().get("/posdat?id=134")
    assert r.status_code == 200
    assert vistos["id_posdat"] == 134
    assert vistos["tab"] == tab_esperado


def test_la_pantalla_filtrada_no_agrega_ningun_cartel(app_logueada, monkeypatch):
    """DECISIÓN DE LA DUEÑA (2026-08-07): *"sin el cartel pero el resto
    perfecto"*.

    La primera versión ponía un aviso amarillo «Mostrando un solo posdatado ·
    ver todos». Este test está dado vuelta a propósito, no borrado: si mañana
    alguien vuelve a meter el cartel, que falle acá y lea esta decisión antes
    de re-discutirla. La pantalla filtrada se ve igual que siempre, con una
    fila."""
    from modules.posdat import views as pv

    monkeypatch.setattr(pv.queries, "por_id", lambda i: {"prov": "CO"})
    monkeypatch.setattr(pv.queries, "buscar", lambda **kw: [])
    monkeypatch.setattr(pv.queries, "resumen", lambda **kw: {})
    monkeypatch.setattr(pv.queries, "persistir_acumulacion_yy", lambda: None)

    html = app_logueada.test_client().get("/posdat?id=134").get_data(as_text=True)
    assert "Mostrando un solo posdatado" not in html
    assert html.count("<table") >= 1      # la grilla de siempre, sin adornos


def test_un_id_que_no_es_un_numero_se_ignora(app_logueada, monkeypatch):
    """?id=borrar-todo no puede tirar un 500 ni colarse en el SQL."""
    from modules.posdat import views as pv

    vistos = {}

    def fake_buscar(**kw):
        vistos["id"] = kw.get("id_posdat")
        return []

    monkeypatch.setattr(pv.queries, "buscar", fake_buscar)
    monkeypatch.setattr(pv.queries, "resumen", lambda **kw: {})
    monkeypatch.setattr(pv.queries, "persistir_acumulacion_yy", lambda: None)

    r = app_logueada.test_client().get("/posdat?id=borrar-todo")
    assert r.status_code == 200
    assert vistos.get("id") is None
