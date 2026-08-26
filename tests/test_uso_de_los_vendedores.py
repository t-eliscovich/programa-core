"""Medir cuánto usa cada vendedor la app, y qué hace adentro.

TMT 2026-08-26 (dueña): *"¿podríamos medir cuánto usa cada vendedor la
aplicación? ¿y qué movimientos hace?"*.

Lo que protegen estos tests:

* que se registre lo que un VENDEDOR mira, y **sólo** eso — ni la oficina, ni
  los 404, ni los estáticos, ni el preview de la dueña;
* que medir no pueda tumbar una pantalla (si el INSERT falla, el request sigue);
* que la pantalla de uso no la vea quien no puede ver la bitácora, y que un
  vendedor no la vea nunca;
* que los nombres lindos de las pantallas apunten a endpoints que EXISTEN. Es
  la misma lección de los links hardcodeados del historial: un endpoint que se
  renombra no se ve desde el código, y acá dejaría media tabla sin nombre.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.uso import queries, registro  # noqa: E402

VENDEDOR = {"id_usuario": 7, "username": "ppr", "nombre_rol": "Vendedor",
            "activo": True, "vend": "PPR"}
OFICINA = {"id_usuario": 3, "username": "maribel", "nombre_rol": "INT",
           "activo": True, "vend": None}


def _ok(app):
    return app.response_class(status=200)


# --------------------------------------------------------------------------
# Los nombres de las pantallas
# --------------------------------------------------------------------------


def test_nombre_de_pantalla():
    assert registro.nombre_de("mi_cartera.cliente") == "Ficha de un cliente"
    # Una pantalla que todavía no tiene nombre lindo no rompe nada: se muestra
    # el endpoint, que ya dice bastante.
    assert registro.nombre_de("otro.endpoint") == "otro.endpoint"
    assert registro.nombre_de(None) == "—"


def test_los_endpoints_con_nombre_existen(app):
    """Si alguien renombra una vista, este test lo agarra."""
    reales = {r.endpoint for r in app.url_map.iter_rules()}
    faltan = sorted(e for e in registro.NOMBRES if e not in reales)
    assert not faltan, f"endpoints que ya no existen: {faltan}"


def test_los_papeles_tienen_nombre():
    faltan = sorted(p for p in registro.PAPELES if p not in registro.NOMBRES)
    assert not faltan
    assert registro.es_papel("mi_cartera.pdf")
    assert not registro.es_papel("mi_cartera.inicio")
    assert not registro.es_papel(None)


@pytest.mark.parametrize(
    ("agente", "esperado"),
    [
        ("Mozilla/5.0 (iPhone) Mobile/15E148 Safari/604.1 Mobi", "celular"),
        ("Mozilla/5.0 (Linux; Android 13) Chrome/120", "celular"),
        ("Mozilla/5.0 (Macintosh) Chrome/120 Safari/537", "computadora"),
        (None, "computadora"),
    ],
)
def test_de_que_aparato_entro(agente, esperado):
    assert registro.dispositivo_de(agente) == esperado


# --------------------------------------------------------------------------
# Qué se registra y qué no
# --------------------------------------------------------------------------


def test_se_registra_lo_que_mira_un_vendedor(app, monkeypatch):
    from flask import g

    import db

    escrito = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escrito.append((sql, params)))

    with app.test_request_context("/mi-cartera/cliente/tdv",
                                  headers={"User-Agent": "Android Mobi"}):
        g.user = VENDEDOR
        registro.registrar_uso_after_request(_ok(app))

    assert len(escrito) == 1
    sql, params = escrito[0]
    assert "scintela.uso_pantalla" in sql
    usuario, vend, ruta, pantalla, codigo_cli, aparato, _ip = params
    assert (usuario, vend) == ("ppr", "PPR")
    assert ruta == "/mi-cartera/cliente/tdv"
    assert pantalla == "mi_cartera.cliente"
    # El código va normalizado, como lo JOINea todo el sistema (mig 0155).
    assert codigo_cli == "TDV"
    assert aparato == "celular"


@pytest.mark.parametrize(
    ("ruta", "metodo", "estado", "user"),
    [
        # La oficina no se mide: se preguntó por los vendedores.
        ("/mi-cartera", "GET", 200, OFICINA),
        # El preview de la dueña (?vend=PPR) tampoco: quien lo abre no tiene
        # `vend` propio, así que no le ensucia los números al vendedor.
        ("/mi-cartera?vend=PPR", "GET", 200, OFICINA),
        # Nadie logueado.
        ("/mi-cartera", "GET", 200, None),
        # Una escritura ya la guarda la bitácora — no se cuenta dos veces.
        ("/mi-cartera/cliente/tdv/portal", "POST", 200, VENDEDOR),
        # Un 404 no es una pantalla que alguien haya usado.
        ("/pantalla-que-no-existe", "GET", 404, VENDEDOR),
        # Y los estáticos no son pantallas.
        ("/static/tailwind.css", "GET", 200, VENDEDOR),
    ],
)
def test_lo_que_no_se_registra(app, monkeypatch, ruta, metodo, estado, user):
    from flask import g

    import db

    escrito = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escrito.append(params))

    with app.test_request_context(ruta, method=metodo):
        g.user = user
        registro.registrar_uso_after_request(app.response_class(status=estado))

    assert escrito == []


def test_medir_nunca_rompe_la_pantalla(app, monkeypatch):
    """Si el INSERT falla, el vendedor sigue trabajando."""
    from flask import g

    import db

    def explota(*a, **k):
        raise RuntimeError("se cayó la base")

    monkeypatch.setattr(db, "execute", explota)
    with app.test_request_context("/mi-cartera"):
        g.user = VENDEDOR
        respuesta = _ok(app)
        assert registro.registrar_uso_after_request(respuesta) is respuesta


# --------------------------------------------------------------------------
# El rango de fechas
# --------------------------------------------------------------------------


def test_la_ventana_es_de_dias_de_ecuador():
    """Un día de Ecuador va de sus 00:00 a sus 24:00, o sea +5 en UTC."""
    v = queries.ventana(date(2026, 8, 1), date(2026, 8, 31))
    assert v["desde"].isoformat() == "2026-08-01T05:00:00"
    assert v["hasta"].isoformat() == "2026-09-01T05:00:00"


# --------------------------------------------------------------------------
# La pantalla
# --------------------------------------------------------------------------


def _login(app, user, permisos):
    @app.before_request
    def _entrar():  # pragma: no cover - infra de test
        from flask import g, session

        session["user_id"] = user["id_usuario"]
        g.user = user
        g.permisos = set(permisos)


def test_la_pantalla_de_uso_pide_el_permiso_de_la_bitacora(app, monkeypatch):
    _login(app, OFICINA, {"clientes.ver"})
    assert app.test_client().get("/uso").status_code == 404


def test_la_pantalla_de_uso_abre_con_el_permiso(app, monkeypatch):
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "resumen", lambda d, h: [
        {"usuario": "ppr", "vend": "PPR", "rol": "Vendedor", "activo": True,
         "visitas": 40, "dias": 5, "entradas": 9, "clientes": 12, "papeles": 3,
         "celular": 38, "movimientos": 1, "ultima": None},
    ])
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None: [])
    r = app.test_client().get("/uso")
    assert r.status_code == 200
    cuerpo = r.get_data(as_text=True)
    assert "PPR" in cuerpo
    assert "Veces que entró" in cuerpo


def test_un_vendedor_no_ve_el_uso_de_nadie(app):
    """El scope de vendedores le cierra todo lo que no sea /mi-cartera.

    Se prueba contra el hook y no con el test client porque el login falso de
    los tests se registra DESPUÉS de `enforce_scope_vendedor` (que ya está
    puesto desde `create_app`) y el hook no llegaría a ver el usuario. En
    producción el orden es el bueno: `load_logged_in_user` corre primero.
    Mismo patrón que tests/test_scope_vendedor.py.
    """
    from flask import g

    from scope_vendedor import enforce_scope_vendedor

    for ruta in ("/uso", "/uso/ppr"):
        with app.test_request_context(ruta):
            g.user = VENDEDOR
            g.permisos = {"bitacora.ver"}
            respuesta = enforce_scope_vendedor()
        assert respuesta is not None, f"{ruta} le quedó abierta a un vendedor"
        assert respuesta[1] == 404


def test_el_csv_del_resumen_lleva_la_hora(app, monkeypatch):
    """El formateador por defecto deja sólo la fecha, y acá la hora importa."""
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "resumen", lambda d, h: [
        {"usuario": "ppr", "vend": "PPR", "rol": "Vendedor", "activo": True,
         "visitas": 40, "dias": 5, "entradas": 9, "clientes": 12, "papeles": 3,
         "celular": 38, "movimientos": 1,
         "ultima": datetime(2026, 8, 26, 10, 53)},
    ])
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None: [])
    r = app.test_client().get("/uso?export=csv")
    assert r.status_code == 200
    cuerpo = r.get_data(as_text=True)
    assert "Veces que entró" in cuerpo
    assert "26/08/2026 10:53" in cuerpo


def test_el_detalle_junta_lo_que_miro_con_lo_que_cambio(app, monkeypatch):
    """Las dos fuentes en una sola lista: las visitas y la bitácora."""
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "por_dia", lambda u, d, h: [])
    monkeypatch.setattr(queries, "clientes", lambda u, d, h: [])
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None: [])
    monkeypatch.setattr(queries, "movimientos", lambda u, d, h: [
        {"cuando": datetime(2026, 8, 26, 9, 10), "tipo": "miro",
         "pantalla": "mi_cartera.pdf", "ruta": "/mi-cartera/cliente/TDV/pdf",
         "codigo_cli": "TDV", "detalle": None},
        {"cuando": datetime(2026, 8, 24, 17, 51), "tipo": "hizo",
         "pantalla": "portal_acceso", "ruta": "/mi-cartera/cliente/TDV/portal",
         "codigo_cli": None, "detalle": "Le dio acceso al portal a TDV"},
    ])
    cuerpo = app.test_client().get("/uso/ppr").get_data(as_text=True)
    assert "Estado de cuenta en PDF" in cuerpo
    assert "Le dio acceso al portal a TDV" in cuerpo
    assert "Cambió:" in cuerpo

    csv = app.test_client().get("/uso/ppr?export=csv").get_data(as_text=True)
    assert "miró" in csv and "cambió" in csv
