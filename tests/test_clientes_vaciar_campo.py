"""Desde la pantalla de clientes se tiene que poder VACIAR un campo.

TMT 2026-08-04 — caso KET. El cliente KET tenía vendedor `FL1` en Programa
Core y en el dBase **no tiene vendedor asignado**. Había que dejarlo sin
vendedor para que las dos bases coincidan (y para que no se le siga
liquidando comisión a FL1). No se podía:

  * `views.editar` mandaba `form["vend"] or None`, y
  * `queries.editar` salteaba los campos que llegaban en `None`
    (`if val is not None`).

Resultado: desde la pantalla se podía cambiar un valor por otro pero NUNCA
borrarlo, y encima el flash decía "Cliente KET actualizado" — mentía.

El contrato que fijan estos tests:
  * `None` sigue significando **"no toques esta columna"** (lo usan los
    callers parciales).
  * `""` (y el sentinela `VACIAR` para los numéricos) significa
    **"poné NULL"**.
  * `editar()` devuelve 0 cuando no cambió nada, y la pantalla lo dice.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.clientes import queries as q  # noqa: E402
from tests.test_routes_smoke import build_app  # noqa: E402


class _FakeDB:
    """Captura el UPDATE que arma `editar()`."""

    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount
        self.ejecutados: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None, conn=None):
        self.ejecutados.append((" ".join(sql.split()), tuple(params or ())))
        return self.rowcount

    @property
    def sql(self) -> str:
        assert self.ejecutados, "editar() no ejecutó ningún UPDATE"
        return self.ejecutados[-1][0]

    @property
    def params(self) -> tuple:
        assert self.ejecutados, "editar() no ejecutó ningún UPDATE"
        return self.ejecutados[-1][1]

    def valor_de(self, columna: str):
        """Valor que el UPDATE le asigna a `columna` en el SET."""
        sets = self.sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        cols = [c.strip().split(" = ")[0] for c in sets.split(", ")]
        assert columna in cols, f"{columna} no está en el SET: {cols}"
        return self.params[cols.index(columna)]


@pytest.fixture()
def fake(monkeypatch):
    def _mk(rowcount: int = 1):
        f = _FakeDB(rowcount)
        monkeypatch.setattr(q, "db", f)
        return f

    return _mk


@pytest.fixture()
def app_logueada():
    """App con DB stubbeada y usuario con todos los permisos.

    `app.py` importa `load_logged_in_user` al importarse, así que registramos
    NUESTRO before_request después de create_app: corre siempre, sin depender
    del orden de la suite (ver skill programa-core).
    """
    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    try:
        yield app
    finally:
        deshacer()


# --------------------------------------------------------------------------
# EL caso: vaciar
# --------------------------------------------------------------------------

def test_string_vacio_pone_la_columna_en_null(fake):
    """KET: vend='' tiene que llegar al UPDATE como vend = NULL."""
    f = fake()
    q.editar("KET", vend="")
    assert "vend = %s" in f.sql
    assert f.valor_de("vend") is None


def test_vaciar_de_verdad_no_es_un_no_op(fake):
    """El UPDATE se ejecuta y devuelve filas: la pantalla no puede mentir."""
    f = fake(rowcount=1)
    assert q.editar("KET", vend="") == 1
    assert len(f.ejecutados) == 1


def test_todos_los_campos_de_texto_se_pueden_vaciar(fake):
    campos = ("nombre", "ruc", "telefono", "correo", "direccion1", "direccion2",
              "provincia", "canton", "parroquia", "pago", "vend",
              "observacion", "stop")
    for campo in campos:
        f = fake()
        q.editar("KET", **{campo: ""})
        assert f.valor_de(campo) is None, f"{campo} no se pudo vaciar"


def test_cupo_se_vacia_con_el_sentinela_VACIAR(fake):
    """`cupo` es numérico: no hay string vacío, va el sentinela."""
    f = fake()
    q.editar("KET", cupo=q.VACIAR)
    assert f.valor_de("cupo") is None


# --------------------------------------------------------------------------
# Lo que NO se puede romper: None = "no toques este campo"
# --------------------------------------------------------------------------

def test_None_sigue_significando_no_tocar(fake):
    """El caller parcial que manda sólo `nombre` no debe pisar `vend`."""
    f = fake()
    q.editar("KET", nombre="KETY SA", vend=None, cupo=None)
    assert "nombre = %s" in f.sql
    assert "vend = %s" not in f.sql
    assert "cupo = %s" not in f.sql


def test_sin_ningun_campo_no_ejecuta_nada_y_devuelve_cero(fake):
    f = fake()
    assert q.editar("KET") == 0
    assert f.ejecutados == []


def test_cupo_cero_es_un_valor_no_un_no_tocar(fake):
    f = fake()
    q.editar("KET", cupo=0)
    assert f.valor_de("cupo") == 0


# --------------------------------------------------------------------------
# El flash tiene que decir la verdad
# --------------------------------------------------------------------------

def test_el_update_compara_con_IS_DISTINCT_FROM(fake):
    """Sin esto el rowcount es siempre 1 (usuario_modifica cambia solo) y la
    pantalla no puede distinguir "actualizado" de "no cambió nada"."""
    f = fake()
    q.editar("KET", vend="FL1", nombre="KETY SA")
    assert "vend IS DISTINCT FROM %s" in f.sql
    assert "nombre IS DISTINCT FROM %s" in f.sql
    # Los valores viajan dos veces: SET y comparación.
    assert f.params.count("FL1") == 2
    assert f.params.count("KETY SA") == 2


def test_los_params_del_where_van_despues_del_codigo(fake):
    """Orden exacto: valores del SET, usuario, código, valores otra vez."""
    f = fake()
    q.editar("KET", vend="", usuario="tamara")
    assert f.params == (None, "tamara", "KET", None)


def test_devuelve_cero_cuando_la_base_no_cambio_nada(fake):
    fake(rowcount=0)
    assert q.editar("KET", vend="FL1") == 0


def test_la_vista_manda_el_string_vacio_tal_cual(app_logueada, monkeypatch):
    """views.editar NO puede volver a poner `or None` — ahí murió KET."""
    from modules.clientes import views

    visto: dict = {}

    def falso_editar(codigo, **kw):
        visto.update(kw)
        return 1

    monkeypatch.setattr(views.queries, "por_codigo", lambda c: {
        "codigo_cli": "KET", "nombre": "KETY", "vend": "FL1", "cupo": 500,
    })
    monkeypatch.setattr(views.queries, "editar", falso_editar)
    monkeypatch.setattr(views, "tiene_permiso", lambda p: True)

    rv = app_logueada.test_client().post(
        "/clientes/KET/editar",
        data={"nombre": "KETY", "vend": "", "cupo": ""},
        follow_redirects=False,
    )
    assert rv.status_code in (200, 302), rv.get_data(as_text=True)[:300]
    assert visto["vend"] == "", f"la vista mandó {visto['vend']!r} en vez de ''"
    assert visto["cupo"] is q.VACIAR, "el cupo vacío tiene que llegar como VACIAR"


def test_el_flash_dice_la_verdad_cuando_no_cambio_nada(app_logueada, monkeypatch):
    """0 filas → NO puede decir "actualizado"."""
    from modules.clientes import views

    monkeypatch.setattr(views.queries, "por_codigo", lambda c: {
        "codigo_cli": "KET", "nombre": "KETY", "vend": "FL1", "cupo": 500,
    })
    monkeypatch.setattr(views.queries, "editar", lambda codigo, **kw: 0)
    monkeypatch.setattr(views, "tiene_permiso", lambda p: True)

    cli = app_logueada.test_client()
    cli.post("/clientes/KET/editar", data={"nombre": "KETY", "vend": ""})
    with cli.session_transaction() as sess:
        mensajes = [m for _cat, m in sess.get("_flashes", [])]
    assert mensajes, "no flasheó nada"
    assert "actualizado" not in mensajes[0].lower(), mensajes
    assert "nada que cambiar" in mensajes[0].lower(), mensajes
