"""Medir cuánto usa la app cada vendedor, cada cliente y cada quien en
Intela (la oficina), y qué hace adentro.

TMT 2026-08-26 (dueña): *"¿podríamos medir cuánto usa cada vendedor la
aplicación? ¿y qué movimientos hace?"*. TMT 2026-09-14: *"hace que uso haya
tabs, vendedores, clientes, intela. hace un buen trabajo de medición"* — la
oficina se suma como tercer mundo.

Lo que protegen estos tests:

* que se registre lo que mira CUALQUIERA que entró logueado — vendedor u
  oficina —, y también el cliente del portal; pero no los 404, ni los
  estáticos;
* que los tres mundos (vendedor / oficina / portal) nunca se lean mezclados:
  `queries.resumen` exige `vend`, `queries.resumen_intela` exige que NO lo
  tenga, y `queries.pantallas(..., ambito=)` arma la condición según cuál sea;
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


def _app_portal():
    """El app del OTRO proceso, el del portal. Devuelve (app, deshacer)."""
    import os
    from unittest.mock import patch

    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        return build_app()


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
    """Si alguien renombra una vista, este test lo agarra.

    Los del portal viven en el OTRO proceso: se comprueban contra el app en
    modo portal, abajo."""
    reales = {r.endpoint for r in app.url_map.iter_rules()}
    faltan = sorted(e for e in registro.NOMBRES
                    if e not in reales and not e.startswith("portal."))
    assert not faltan, f"endpoints que ya no existen: {faltan}"


def test_los_endpoints_del_portal_con_nombre_existen():
    portal, deshacer = _app_portal()
    try:
        reales = {r.endpoint for r in portal.url_map.iter_rules()}
    finally:
        deshacer()
    faltan = sorted(e for e in registro.NOMBRES
                    if e.startswith("portal.") and e not in reales)
    assert not faltan, f"endpoints del portal que ya no existen: {faltan}"


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


def test_se_registra_lo_que_mira_alguien_de_intela(app, monkeypatch):
    """TMT 2026-09-14: *"hace un buen trabajo de medición"* — la oficina se
    mide igual que un vendedor, sólo que con `vend` vacío: es lo que separa
    los dos mundos en `queries.resumen` / `queries.resumen_intela`."""
    from flask import g

    import db

    escrito = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escrito.append((sql, params)))

    with app.test_request_context("/mi-cartera"):
        g.user = OFICINA
        registro.registrar_uso_after_request(_ok(app))

    assert len(escrito) == 1
    sql, params = escrito[0]
    assert "scintela.uso_pantalla" in sql
    usuario, vend, ruta, pantalla, codigo_cli, aparato, _ip = params
    assert (usuario, vend) == ("maribel", None)
    assert ruta == "/mi-cartera"


def test_el_preview_de_la_duena_se_anota_a_su_propio_nombre(app, monkeypatch):
    """`?vend=PPR` es la dueña (o cualquiera de oficina) mirando la cartera
    de PPR: cuenta como SU visita, nunca como una visita de PPR — si contara
    para PPR le ensuciaría los números (`resumen()` exige `vend` PROPIO)."""
    from flask import g

    import db

    escrito = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escrito.append(params))

    with app.test_request_context("/mi-cartera?vend=PPR"):
        g.user = OFICINA
        registro.registrar_uso_after_request(_ok(app))

    assert len(escrito) == 1
    usuario, vend, *_resto = escrito[0]
    assert (usuario, vend) == ("maribel", None)


def test_se_registra_lo_que_mira_un_cliente_en_el_portal():
    """TMT 04/09/2026: "así vemos qué hacen una vez que lancemos". Misma
    tabla, `usuario` con prefijo para que nunca se confunda con alguien de la
    casa, y el cliente en `codigo_cli`."""
    import os
    from unittest.mock import patch

    import db

    portal, deshacer = _app_portal()
    escrito = []
    # A mano y no con monkeypatch: `deshacer()` también restaura `db`, y los
    # dos restauradores se pisan entre sí (queda la FUGA del conftest).
    execute_previo = db.execute
    db.execute = lambda sql, params=None, conn=None: escrito.append((sql, params))
    try:
        with patch.dict(os.environ, {**os.environ, "MODO": "portal"}), \
             portal.test_request_context("/estado-de-cuenta",
                                         headers={"User-Agent": "Android Mobi"}):
            from flask import session
            session["portal_cliente"] = "ajt"
            registro.registrar_uso_after_request(_ok(portal))
    finally:
        db.execute = execute_previo
        deshacer()

    assert len(escrito) == 1
    sql, params = escrito[0]
    assert "scintela.uso_pantalla" in sql
    usuario, vend, ruta, pantalla, codigo_cli, aparato, _ip = params
    assert usuario == "portal:AJT"
    assert vend is None
    assert (ruta, pantalla, codigo_cli) == ("/estado-de-cuenta", "portal.estado_cuenta", "AJT")
    assert aparato == "celular"


def test_en_el_portal_sin_cliente_logueado_no_se_registra():
    """La pantalla de ingreso la abren los robots: no es uso de nadie."""
    import os
    from unittest.mock import patch

    import db

    portal, deshacer = _app_portal()
    escrito = []
    execute_previo = db.execute
    db.execute = lambda sql, params=None, conn=None: escrito.append(params)
    try:
        with patch.dict(os.environ, {**os.environ, "MODO": "portal"}), \
             portal.test_request_context("/ingresar"):
            registro.registrar_uso_after_request(_ok(portal))
    finally:
        db.execute = execute_previo
        deshacer()
    assert escrito == []


def test_en_la_oficina_la_llave_del_portal_no_cuenta(app, monkeypatch):
    """Una sesión de la oficina con la llave del portal puesta (no debería
    pasar, pero) se anota como SU visita, nunca como el cliente: el modo
    manda, no una llave de sesión que quedó pegada de otra pestaña."""
    import db

    escrito = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escrito.append(params))
    with app.test_request_context("/mi-cartera"):
        from flask import g, session
        session["portal_cliente"] = "AJT"
        g.user = OFICINA
        registro.registrar_uso_after_request(_ok(app))
    assert len(escrito) == 1
    usuario, vend, *_resto = escrito[0]
    assert usuario == "maribel"
    assert vend is None


@pytest.mark.parametrize(
    ("ruta", "metodo", "estado", "user"),
    [
        # Nadie logueado — ni vendedor, ni oficina, ni portal.
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
    """La pestaña por default es vendedores — no trae de arriba la data de
    clientes ni de Intela, cada pestaña pide sólo lo suyo."""
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "resumen", lambda d, h: [
        {"usuario": "ppr", "vend": "PPR", "rol": "Vendedor", "activo": True,
         "visitas": 40, "dias": 5, "entradas": 9, "clientes": 12, "cartera": 43,
         "papeles": 3, "celular": 38, "movimientos": 1, "ultima": None},
    ])
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    r = app.test_client().get("/uso")
    assert r.status_code == 200
    cuerpo = r.get_data(as_text=True)
    assert "PPR" in cuerpo
    assert "Veces que entró" in cuerpo
    # Los clientes se leen contra la cartera: 12 solos no dicen nada.
    assert "12" in cuerpo and "de 43" in cuerpo
    assert "Clientes en el portal" not in cuerpo


def test_la_pestana_de_clientes_muestra_quien_entro(app, monkeypatch):
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    monkeypatch.setattr(queries, "resumen_clientes", lambda d, h: [
        {"codigo_cli": "AJT", "nombre": "TEXTILES TOTOY", "vend": "EDG",
         "visitas": 9, "dias": 2, "entradas": 3, "papeles": 1, "celular": 9,
         "ultima": datetime(2026, 9, 4, 10, 30)},
    ])
    r = app.test_client().get("/uso?tab=clientes")
    assert r.status_code == 200
    cuerpo = r.get_data(as_text=True)
    # La grilla de los clientes del portal, con el cliente por CÓDIGO.
    assert "Clientes en el portal" in cuerpo
    assert "AJT" in cuerpo and "TEXTILES TOTOY" in cuerpo
    assert "04/09/2026 10:30" in cuerpo


def test_sin_clientes_en_el_portal_la_grilla_lo_dice(app, monkeypatch):
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    monkeypatch.setattr(queries, "resumen_clientes", lambda d, h: [])
    cuerpo = app.test_client().get("/uso?tab=clientes").get_data(as_text=True)
    assert "Ningún cliente entró al portal" in cuerpo


def test_la_pestana_de_intela_muestra_a_la_oficina(app, monkeypatch):
    """TMT 2026-09-14: *"hace que uso haya tabs, vendedores, clientes,
    intela"* — el tercer mundo, con su propia grilla."""
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    monkeypatch.setattr(queries, "resumen_intela", lambda d, h: [
        {"usuario": "maribel", "rol": "INT", "activo": True,
         "visitas": 20, "dias": 4, "entradas": 6, "fichas": 15,
         "movimientos": 3, "celular": 2, "ultima": datetime(2026, 9, 14, 9, 5)},
    ])
    r = app.test_client().get("/uso?tab=intela")
    assert r.status_code == 200
    cuerpo = r.get_data(as_text=True)
    assert "maribel" in cuerpo
    assert "Fichas de cliente" in cuerpo
    assert "14/09/2026 09:05" in cuerpo


def test_sin_nadie_de_intela_la_grilla_lo_dice(app, monkeypatch):
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    monkeypatch.setattr(queries, "resumen_intela", lambda d, h: [])
    cuerpo = app.test_client().get("/uso?tab=intela").get_data(as_text=True)
    assert "Todavía no hay nadie de Intela cargado." in cuerpo


def test_tab_invalida_cae_en_vendedores(app, monkeypatch):
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "resumen", lambda d, h: [])
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    r = app.test_client().get("/uso?tab=lo-que-sea")
    assert r.status_code == 200
    cuerpo = r.get_data(as_text=True)
    assert "Clientes en el portal" not in cuerpo
    assert "Todavía no hay vendedores cargados." in cuerpo


def test_el_csv_respeta_la_pestana(app, monkeypatch):
    """El CSV de la pestaña de clientes no es el de vendedores."""
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    monkeypatch.setattr(queries, "resumen_clientes", lambda d, h: [
        {"codigo_cli": "AJT", "nombre": "TEXTILES TOTOY", "vend": "EDG",
         "dias": 2, "entradas": 3, "visitas": 9, "papeles": 1, "celular": 9,
         "ultima": datetime(2026, 9, 4, 10, 30)},
    ])
    r = app.test_client().get("/uso?tab=clientes&export=csv")
    assert r.status_code == 200
    cuerpo = r.get_data(as_text=True)
    assert "AJT" in cuerpo and "TEXTILES TOTOY" in cuerpo
    assert "04/09/2026 10:30" in cuerpo


def test_las_pantallas_mas_abiertas_no_mezclan_al_portal(monkeypatch):
    """La tabla de abajo es de los vendedores: un cliente mirando su estado
    de cuenta no es «una pantalla que abrió un vendedor»."""
    import db

    visto = {}
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None, **k: visto.update(sql=sql, params=params) or [])
    queries.pantallas(date(2026, 9, 1), date(2026, 9, 4))
    assert "NOT LIKE %(prefijo)s" in visto["sql"]
    assert visto["params"]["prefijo"] == "portal:%"


@pytest.mark.parametrize(
    ("ambito", "fragmento"),
    [
        ("vendedor", "vend IS NOT NULL AND TRIM(vend) <> ''"),
        ("clientes", "usuario LIKE %(prefijo)s"),
        ("intela", "vend IS NULL OR TRIM(vend) = ''"),
    ],
)
def test_pantallas_separa_los_tres_mundos(monkeypatch, ambito, fragmento):
    """Vendedor, clientes e Intela arman cada uno su propia condición —
    nunca se leen mezclados entre sí (ver `queries._AMBITOS`)."""
    import db

    visto = {}
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None, **k: visto.update(sql=sql, params=params) or [])
    queries.pantallas(date(2026, 9, 1), date(2026, 9, 14), ambito=ambito)
    assert fragmento in visto["sql"]


def test_pantallas_con_usuario_puntual_no_filtra_por_ambito(monkeypatch):
    """Con `usuario` ya alcanza: esa persona es de un solo mundo. Si se le
    dejara puesto el filtro de vendedor por default, el detalle de alguien
    de Intela (`vend` vacío) saldría siempre vacío."""
    import db

    visto = {}
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None, **k: visto.update(sql=sql, params=params) or [])
    queries.pantallas(date(2026, 9, 1), date(2026, 9, 14), usuario="maribel")
    assert "TRUE" in visto["sql"]
    assert "vend IS NOT NULL" not in visto["sql"]
    assert visto["params"]["usuario"] == "maribel"


def test_resumen_intela_arma_la_consulta(monkeypatch):
    """No hay «cartera» en Intela: la condición es sólo `vend` vacío, y
    nunca entra un `usuario` del portal."""
    import db

    visto = {}
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None, **k: visto.update(sql=sql, params=params) or [])
    queries.resumen_intela(date(2026, 9, 1), date(2026, 9, 14))
    assert "vend IS NULL OR TRIM(u.vend) = ''" in visto["sql"]
    assert "usuario NOT LIKE %(prefijo)s" in visto["sql"]
    assert visto["params"]["prefijo"] == "portal:%"


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
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
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
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    monkeypatch.setattr(queries, "vend_de", lambda u: "PPR")
    monkeypatch.setattr(queries, "no_abiertos", lambda v, u, d, h: [])
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


def test_los_que_no_abrio_salen_con_lo_que_deben(app, monkeypatch):
    """La mitad accionable: no «abrió 12» sino «a estos no los miró»."""
    _login(app, OFICINA, {"bitacora.ver"})
    monkeypatch.setattr(queries, "por_dia", lambda u, d, h: [])
    monkeypatch.setattr(queries, "clientes", lambda u, d, h: [])
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    monkeypatch.setattr(queries, "movimientos", lambda u, d, h: [])
    monkeypatch.setattr(queries, "vend_de", lambda u: "PPR")
    monkeypatch.setattr(queries, "no_abiertos", lambda v, u, d, h: [
        {"codigo_cli": "TDV", "nombre": "Textiles del Valle",
         "saldo": 4820.5, "vencido": 1200.0},
    ])
    cuerpo = app.test_client().get("/uso/ppr").get_data(as_text=True)
    assert "Los que no abrió" in cuerpo
    assert "Textiles del Valle" in cuerpo
    # Formato EU: punto de miles, coma decimal (ver filters.money_es).
    assert "4.820,50" in cuerpo


def test_un_usuario_sin_vendedor_no_muestra_el_bloque(app, monkeypatch):
    """Sin código de vendedor no hay cartera contra la cual comparar."""
    _login(app, OFICINA, {"bitacora.ver"})
    for nombre in ("por_dia", "clientes", "movimientos"):
        monkeypatch.setattr(queries, nombre, lambda *a, **k: [])
    monkeypatch.setattr(queries, "pantallas", lambda d, h, usuario=None, ambito=None: [])
    monkeypatch.setattr(queries, "vend_de", lambda u: "")

    def no_deberia(*a, **k):  # pragma: no cover - el test falla si se llama
        raise AssertionError("no hay que buscar cartera de quien no es vendedor")

    monkeypatch.setattr(queries, "no_abiertos", no_deberia)
    cuerpo = app.test_client().get("/uso/maribel").get_data(as_text=True)
    assert "Los que no abrió" not in cuerpo
