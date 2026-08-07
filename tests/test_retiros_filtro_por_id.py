"""`/retiros?id=<id_retiro>` tiene que mostrar EXACTAMENTE esa fila.

TMT 2026-08-07 (dueña, sobre los links del historial): *"esos links deberían
venir filtrados por lo que quiero ver"*, y más fuerte: *"si al clickear hay que
buscar la fila a ojo, el link no está terminado"*.

El movimiento del historial linkeaba a `/retiros` a secas — la pantalla entera,
hasta 500 filas — y había que cazar el retiro mencionado a ojo.

Lo que muerde acá NO es el `WHERE` (que es trivial), son los DEFAULTS que
esconden la fila o mienten arriba de ella:

* `buscar()` no tiene ventana de fechas por default, pero SÍ arrastra el
  `q`/`de`/`desde`/`hasta` de la pantalla anterior: cualquiera de esos deja la
  grilla vacía aunque el id exista.
* `resumen()` (el hero: Total retirado / N retiros / Ticket / Periodo) traía por
  default los ÚLTIMOS 90 DÍAS y `totales_por_persona()` (los chips «Por código»)
  el ÚLTIMO AÑO. Un retiro más viejo que eso mostraba "Total retirado" de un
  período que no lo incluye, arriba de la fila pedida. Ese es exactamente el
  contador incongruente que ya se pagó en /posdat (item 18, 2026-05-19) y que
  volvió a aparecer ahí el 2026-08-07 al agregar el `?id=`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.retiros import queries as rq  # noqa: E402

# ── la capa SQL ──────────────────────────────────────────────────────────────

class _CapturaSQL:
    """Guarda TODAS las consultas: quedarse con la última captura la
    equivocada cuando una función hace más de una."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def _rec(self, sql, params=None, **kw):
        self.queries.append((" ".join(sql.split()), params or {}))

    def fetch_all(self, sql, params=None, **kw):
        self._rec(sql, params)
        return []

    def fetch_one(self, sql, params=None, **kw):
        self._rec(sql, params)
        return {}

    def principal(self) -> tuple[str, dict]:
        """La consulta que lista/suma retiros — la que menciona id_retiro."""
        for sql, params in self.queries:
            if isinstance(params, dict) and "id_retiro" in params:
                return sql, params
        raise AssertionError(
            "ninguna consulta recibió el parámetro id_retiro; se hicieron: "
            + " | ".join(q[:70] for q, _ in self.queries))


@pytest.fixture
def cap(monkeypatch):
    c = _CapturaSQL()
    monkeypatch.setattr(rq, "db", c)
    return c


def test_buscar_por_id_filtra_por_esa_fila(cap):
    rq.buscar(id_retiro=77)
    sql, params = cap.principal()
    assert params["id_retiro"] == 77
    assert "%(id_retiro)s IS NULL OR r.id_retiro = %(id_retiro)s" in sql


def test_pedir_por_id_apaga_los_filtros_que_venian_de_la_pantalla(cap):
    """El `q`, el código `de` y el rango de fechas describen lo que se estaba
    mirando ANTES del click, no la fila pedida: cualquiera la esconde y el link
    parece apuntar a la nada."""
    rq.buscar(q="sueldos", desde="2026-08-01", hasta="2026-08-07",
              de="RR", id_retiro=77)
    sql, params = cap.principal()
    assert params["id_retiro"] == 77
    assert "%(id_retiro)s IS NOT NULL OR %(q)s IS NULL" in sql
    assert "%(id_retiro)s IS NOT NULL OR %(de)s IS NULL" in sql
    assert "%(id_retiro)s IS NOT NULL OR %(desde)s::date IS NULL" in sql
    assert "%(id_retiro)s IS NOT NULL OR %(hasta)s::date IS NULL" in sql


def test_sin_id_la_lista_no_cambia(cap):
    """El caso normal sigue igual: id_retiro viaja como NULL y no filtra nada."""
    rq.buscar()
    _, params = cap.principal()
    assert params["id_retiro"] is None


def test_el_hero_recibe_el_mismo_id_que_la_lista(cap):
    """Invariante: "N retiros" del hero tiene que coincidir con las filas
    visibles. Sin esto muestra el total de toda la lista sobre una fila."""
    rq.resumen(id_retiro=77)
    sql, params = cap.principal()
    assert params["id_retiro"] == 77
    assert "%(id_retiro)s IS NULL OR id_retiro = %(id_retiro)s" in sql


def test_el_hero_apaga_la_ventana_de_90_dias(cap):
    """EL filtro por default de esta pantalla. Un retiro de hace 4 meses caía
    fuera de la ventana: la grilla mostraba la fila y el hero decía 0."""
    rq.resumen(id_retiro=77)
    _, params = cap.principal()
    assert params["desde"] is None and params["hasta"] is None


def test_sin_id_el_hero_conserva_su_ventana_de_90_dias(cap):
    """La ventana no se apaga "en general": sólo cuando se pide una fila."""
    rq.resumen()
    _, params = cap.principal()
    assert params["desde"] is not None and params["hasta"] is not None


def test_el_periodo_del_hero_pasa_a_ser_el_dia_de_esa_fila(monkeypatch):
    """Apagada la ventana, el KPI "Periodo" no puede seguir diciendo los 90
    días viejos (sería mentira arriba de la fila). Sale del MIN/MAX de lo que
    quedó filtrado, o sea la fecha del propio retiro."""
    class _Uno:
        def fetch_one(self, sql, params=None, **kw):
            return {"total": 1200, "n": 1, "n_personas": 1,
                    "f_min": "2026-03-11", "f_max": "2026-03-11"}

        def fetch_all(self, sql, params=None, **kw):
            return []

    monkeypatch.setattr(rq, "db", _Uno())
    r = rq.resumen(id_retiro=77)
    assert r["desde"] == "2026-03-11" and r["hasta"] == "2026-03-11"
    assert r["n"] == 1 and r["total"] == 1200


def test_los_chips_por_codigo_reciben_el_id_y_apagan_el_ano(cap):
    """`totales_por_persona` pinta los chips «Por código» ARRIBA de la grilla,
    con su propia ventana de 365 días. Es un agregado más de la pantalla: si no
    recibe el id, suma todo el año sobre una sola fila."""
    rq.totales_por_persona(id_retiro=77)
    sql, params = cap.principal()
    assert params["id_retiro"] == 77
    assert params["desde"] is None and params["hasta"] is None
    assert "%(id_retiro)s IS NULL OR id_retiro = %(id_retiro)s" in sql


def test_sin_id_los_chips_conservan_su_ventana(cap):
    rq.totales_por_persona()
    _, params = cap.principal()
    assert params["desde"] is not None and params["hasta"] is not None


# ── la vista ─────────────────────────────────────────────────────────────────

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


@pytest.fixture
def vistos(monkeypatch):
    """Espía sobre las TRES funciones que alimentan la pantalla."""
    capt: dict[str, dict] = {}
    from modules.retiros import views as rv

    def _buscar(q="", desde=None, hasta=None, de=None, limite=500, id_retiro=None):
        capt["buscar"] = {"id_retiro": id_retiro}
        return []

    def _resumen(desde=None, hasta=None, id_retiro=None):
        capt["resumen"] = {"id_retiro": id_retiro}
        return {}

    def _por_persona(desde=None, hasta=None, id_retiro=None):
        capt["por_persona"] = {"id_retiro": id_retiro}
        return []

    monkeypatch.setattr(rv.queries, "buscar", _buscar)
    monkeypatch.setattr(rv.queries, "resumen", _resumen)
    monkeypatch.setattr(rv.queries, "totales_por_persona", _por_persona)
    return capt


def test_la_url_del_link_lleva_el_id_a_los_tres_agregados(app_logueada, vistos):
    """/retiros?id=77 — la grilla, el hero y los chips, los tres."""
    r = app_logueada.test_client().get("/retiros?id=77")
    assert r.status_code == 200
    assert vistos["buscar"]["id_retiro"] == 77
    assert vistos["resumen"]["id_retiro"] == 77
    assert vistos["por_persona"]["id_retiro"] == 77


def test_sin_id_la_pantalla_sigue_siendo_la_de_siempre(app_logueada, vistos):
    app_logueada.test_client().get("/retiros")
    assert vistos["buscar"]["id_retiro"] is None
    assert vistos["resumen"]["id_retiro"] is None
    assert vistos["por_persona"]["id_retiro"] is None


@pytest.mark.parametrize("crudo", ["borrar-todo", "1 OR 1=1", "", "0", "1;--"])
def test_un_id_que_no_es_un_numero_se_ignora(app_logueada, vistos, crudo):
    """?id=borrar-todo no puede tirar un 500 ni colarse en el SQL."""
    r = app_logueada.test_client().get(f"/retiros?id={crudo}")
    assert r.status_code == 200
    assert vistos["buscar"]["id_retiro"] is None


def test_la_pantalla_filtrada_no_agrega_ningun_cartel(app_logueada, vistos):
    """DECISIÓN DE LA DUEÑA (2026-08-07): *"sin el cartel pero el resto
    perfecto"*.

    En /posdat la primera versión puso un aviso amarillo «Mostrando un solo
    posdatado · ver todos» y se sacó. Este test está dado vuelta a propósito, no
    borrado: si mañana alguien vuelve a meter el cartel acá, que falle y lea
    esta decisión antes de re-discutirla. La pantalla filtrada se ve igual que
    siempre, con una fila.
    """
    html = app_logueada.test_client().get("/retiros?id=77").get_data(as_text=True)
    for cartel in ("Mostrando un solo", "ver todos", "un solo retiro"):
        assert cartel not in html
    assert html.count("<table") >= 1      # la grilla de siempre, sin adornos
