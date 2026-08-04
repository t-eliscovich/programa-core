"""Cambiar el CÓDIGO de una ficha de cliente.

TMT 2026-08-04. `scintela.cliente` tiene 20 códigos DUPLICADOS: dos empresas
distintas con el mismo código de 3 letras. La causa es estructural — el
código son las INICIALES del nombre, así que dos clientes con las mismas
iniciales colisionan solos: `LEC` es "Luis Ernesto Cañamar" **y** "Lola
Emperatriz Cisneros"; `LUL` es "Luis Llugla" **y** "Luis Lopez".

De esos 20, 17 son DOS CLIENTES REALES: borrar uno (lo único que existía,
`eliminar_por_id`) perdería una ficha legítima. Lo que hace falta es que uno
de los dos pase a tener código propio, y no había ninguna pantalla para eso.

Contrato que fijan estos tests:
  1. Se rechaza si el código destino ya existe (crearía una colisión nueva).
  2. Se rechaza si el código destino tiene movimientos huérfanos colgando.
  3. DECISIÓN: si el código de ORIGEN tiene movimientos, se RECHAZA — no se
     arrastran. Los movimientos apuntan al código, no al `id_cliente`, y en
     un código duplicado están MEZCLADOS entre las dos empresas: no hay forma
     automática de saber cuáles son de cuál.
  4. Todo (validación + UPDATE) pasa por UNA sola transacción.
  5. Se opera por PK: con dos fichas `LEC` el código no señala una ficha.
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

# Las dos fichas reales que comparten el código LEC.
LEC_CANAMAR = {"id_cliente": 100, "codigo_cli": "LEC",
               "nombre": "LUIS ERNESTO CANAMAR", "ruc": "1700000001"}
LEC_CISNEROS = {"id_cliente": 200, "codigo_cli": "LEC",
                "nombre": "LOLA EMPERATRIZ CISNEROS", "ruc": "1700000002"}


class _FakeTx:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self.fake.tx_abiertas += 1
        return f"conn-{self.fake.tx_abiertas}"

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.fake.rollbacks += 1
        else:
            self.fake.commits += 1
        return False


class _FakeDB:
    """Stub de db: contesta por la forma del SQL."""

    def __init__(self, *, ficha=None, sin_ficha=False, movs_origen=0,
                 movs_destino=0, destino_ocupado=None, hermanas=None,
                 rowcount=1):
        self.ficha = None if sin_ficha else (ficha or dict(LEC_CISNEROS))
        self.movs_origen = movs_origen
        self.movs_destino = movs_destino
        self.destino_ocupado = destino_ocupado
        self.hermanas = hermanas or []
        self.rowcount = rowcount
        self.updates: list[tuple[str, tuple, object]] = []
        self.tx_abiertas = 0
        self.commits = 0
        self.rollbacks = 0
        self.conns_vistas: list = []

    # -- helpers -----------------------------------------------------------
    def _es_origen(self, cod: str) -> bool:
        return cod == (self.ficha.get("codigo_cli") or "").upper().strip()

    def tx(self):
        return _FakeTx(self)

    # -- router ------------------------------------------------------------
    def fetch_one(self, sql, params=None, conn=None):
        self.conns_vistas.append(conn)
        s = " ".join(sql.split())
        if "WHERE id_cliente = %s" in s:
            return dict(self.ficha) if self.ficha else None
        if "UPPER(TRIM(codigo_cli)) = %s LIMIT 1" in s:
            cod = params[0]
            if self.destino_ocupado and cod == self.destino_ocupado["codigo_cli"]:
                return dict(self.destino_ocupado)
            return None
        raise AssertionError(f"fetch_one inesperado: {s}")

    def fetch_all(self, sql, params=None, conn=None):
        self.conns_vistas.append(conn)
        s = " ".join(sql.split())
        if "UNION ALL" in s and "COUNT(*) AS n" in s:
            cod = (params or {}).get("cod")
            n = self.movs_origen if self._es_origen(cod) else self.movs_destino
            # Todo el conteo se le carga a la 1ra tabla (facturas).
            return [
                {"tabla": t, "n": n if i == 0 else 0}
                for i, (t, _e) in enumerate(q._TABLAS_CON_CODIGO_CLI)
            ]
        if "SELECT id_cliente, codigo_cli, nombre, ruc" in s:
            return [dict(h) for h in self.hermanas]
        if "= ANY(%s)" in s:
            return []
        raise AssertionError(f"fetch_all inesperado: {s}")

    def execute(self, sql, params=None, conn=None):
        self.conns_vistas.append(conn)
        self.updates.append((" ".join(sql.split()), tuple(params or ()), conn))
        return self.rowcount


@pytest.fixture()
def fake(monkeypatch):
    def _mk(**kw):
        f = _FakeDB(**kw)
        monkeypatch.setattr(q, "db", f)
        return f

    return _mk


# --------------------------------------------------------------------------
# Requisito 1 — rechazar si el destino ya existe
# --------------------------------------------------------------------------

def test_rechaza_si_el_codigo_destino_ya_existe(fake):
    f = fake(destino_ocupado={"codigo_cli": "LEE", "nombre": "LEONOR ESPIN"})
    with pytest.raises(ValueError, match="Ya existe un cliente con código LEE"):
        q.cambiar_codigo(200, "LEE")
    assert f.updates == [], "no puede haber escrito nada"


def test_rechaza_el_destino_ocupado_tambien_en_el_plan(fake):
    """La pantalla de confirmación usa `plan_cambio_codigo`: mismo veredicto."""
    fake(destino_ocupado={"codigo_cli": "LEE", "nombre": "LEONOR ESPIN"})
    with pytest.raises(ValueError, match="colisión NUEVA"):
        q.plan_cambio_codigo(200, "LEE")


def test_rechaza_si_el_destino_tiene_movimientos_huerfanos(fake):
    """Sin ficha pero con facturas: mudarse ahí regala plata ajena."""
    f = fake(movs_destino=12)
    with pytest.raises(ValueError, match="no tiene ficha, pero tiene movimientos"):
        q.cambiar_codigo(200, "LEE")
    assert f.updates == []


def test_rechaza_el_mismo_codigo_que_ya_tiene(fake):
    fake()
    with pytest.raises(ValueError, match="ya tiene el código LEC"):
        q.cambiar_codigo(200, "lec")


def test_rechaza_codigos_invalidos(fake):
    fake()
    for malo in ("", "   ", "LEEE", "L E", "L-E", "ÑÑ!"):
        with pytest.raises(ValueError, match="1 a 3 letras"):
            q.cambiar_codigo(200, malo)


def test_rechaza_ficha_inexistente(fake):
    fake(sin_ficha=True)
    with pytest.raises(ValueError, match="No existe la ficha"):
        q.cambiar_codigo(999, "LEE")


# --------------------------------------------------------------------------
# Requisito 2 — LA DECISIÓN sobre los movimientos: rechazar, no arrastrar
# --------------------------------------------------------------------------

def test_rechaza_si_el_codigo_origen_tiene_movimientos(fake):
    """Los movimientos apuntan al código y están mezclados entre las dos
    empresas: ni arrastrarlos ni dejarlos es correcto. Se rechaza."""
    f = fake(movs_origen=55, hermanas=[dict(LEC_CANAMAR)])
    with pytest.raises(ValueError, match="No puedo cambiar el código LEC"):
        q.cambiar_codigo(200, "LEE")
    assert f.updates == [], "no puede renombrar la ficha con movimientos vivos"


def test_el_rechazo_cuenta_los_movimientos_y_avisa_de_la_mezcla(fake):
    fake(movs_origen=55, hermanas=[dict(LEC_CANAMAR)])
    with pytest.raises(ValueError) as ei:
        q.cambiar_codigo(200, "LEE")
    msg = str(ei.value)
    assert "55 facturas" in msg
    assert "2 fichas" in msg, "tiene que decir que el código está compartido"


def test_NUNCA_toca_las_tablas_de_movimientos(fake):
    """La decisión es no arrastrar: sólo se escribe scintela.cliente."""
    f = fake(movs_origen=0)
    q.cambiar_codigo(200, "LEE")
    assert len(f.updates) == 1
    sql = f.updates[0][0]
    assert "UPDATE scintela.cliente" in sql
    for tabla, _etiqueta in q._TABLAS_CON_CODIGO_CLI:
        assert f"UPDATE {tabla}" not in sql


# --------------------------------------------------------------------------
# El camino feliz
# --------------------------------------------------------------------------

def test_renombra_la_ficha_sin_movimientos(fake):
    f = fake(movs_origen=0, hermanas=[dict(LEC_CANAMAR)])
    plan = q.cambiar_codigo(200, "lee", usuario="tamara", motivo="duplicado con Cañamar")
    assert plan["codigo_actual"] == "LEC"
    assert plan["codigo_nuevo"] == "LEE"
    assert plan["filas"] == 1
    sql, params, _conn = f.updates[0]
    assert "SET codigo_cli = %s" in sql
    assert params[0] == "LEE"
    assert params[-1] == 200, "tiene que ir por id_cliente (PK), no por código"


def test_va_por_PK_porque_el_codigo_esta_duplicado(fake):
    """Con dos fichas LEC, `WHERE codigo_cli` renombraría las dos."""
    f = fake(movs_origen=0, hermanas=[dict(LEC_CANAMAR)])
    q.cambiar_codigo(200, "LEE")
    sql = f.updates[0][0]
    assert "WHERE id_cliente = %s" in sql
    assert "WHERE codigo_cli" not in sql


def test_deja_rastro_del_codigo_viejo_en_la_observacion(fake):
    f = fake(movs_origen=0)
    q.cambiar_codigo(200, "LEE", usuario="tamara", motivo="duplicado")
    sql, params, _conn = f.updates[0]
    assert "observacion" in sql
    assert any(isinstance(p, str) and "[CÓDIGO LEC→LEE]" in p for p in params), params
    assert "tamara" in params


def test_el_plan_lista_todas_las_tablas_aunque_den_cero(fake):
    """La confirmación muestra el cero explícito: es la prueba de que no se
    mueve plata a espaldas de nadie."""
    fake(movs_origen=0)
    plan = q.plan_cambio_codigo(200, "LEE")
    assert [m["tabla"] for m in plan["movimientos"]] == [
        t for t, _e in q._TABLAS_CON_CODIGO_CLI
    ]
    assert plan["total_movimientos"] == 0


# --------------------------------------------------------------------------
# Requisito 5 — una sola transacción
# --------------------------------------------------------------------------

def test_todo_pasa_por_una_sola_transaccion(fake):
    f = fake(movs_origen=0)
    q.cambiar_codigo(200, "LEE")
    assert f.tx_abiertas == 1
    assert f.commits == 1
    assert f.rollbacks == 0


def test_la_validacion_corre_dentro_de_la_misma_transaccion(fake):
    """Si validara fuera del tx, otra sesión podría crear el destino en el
    medio. Todas las consultas tienen que ver la conn de la transacción."""
    f = fake(movs_origen=0)
    q.cambiar_codigo(200, "LEE")
    assert f.conns_vistas, "no hubo consultas"
    assert all(c == "conn-1" for c in f.conns_vistas), f.conns_vistas


def test_un_update_que_pega_en_mas_de_una_fila_hace_rollback(fake):
    f = fake(movs_origen=0, rowcount=2)
    with pytest.raises(ValueError, match="afectó 2 filas"):
        q.cambiar_codigo(200, "LEE")
    assert f.rollbacks == 1
    assert f.commits == 0


def test_un_update_que_no_pega_en_nada_hace_rollback(fake):
    f = fake(movs_origen=0, rowcount=0)
    with pytest.raises(ValueError, match="afectó 0 filas"):
        q.cambiar_codigo(200, "LEE")
    assert f.rollbacks == 1
    assert f.commits == 0


def test_el_rechazo_tambien_hace_rollback_no_commit(fake):
    f = fake(movs_origen=55)
    with pytest.raises(ValueError):
        q.cambiar_codigo(200, "LEE")
    assert f.commits == 0


# --------------------------------------------------------------------------
# Sugerencias de código libre
# --------------------------------------------------------------------------

def test_sugiere_codigos_libres_parecidos_al_nombre(monkeypatch):
    class _DB:
        def fetch_all(self, sql, params=None, conn=None):
            # LEC y LEE ya están ocupados.
            return [{"c": c} for c in params[0] if c in ("LEC", "LEE")]

    monkeypatch.setattr(q, "db", _DB())
    libres = q.sugerir_codigos("LOLA EMPERATRIZ CISNEROS", limite=3)
    assert "LEC" not in libres
    assert "LEE" not in libres
    assert len(libres) == 3
    assert all(len(c) == 3 and c.isupper() for c in libres)


def test_sugerir_codigos_sin_nombre_no_rompe(monkeypatch):
    monkeypatch.setattr(q, "db", object())
    assert q.sugerir_codigos("") == []
    assert q.sugerir_codigos("   ") == []


# --------------------------------------------------------------------------
# Las pantallas (requisitos 3 y 4)
# --------------------------------------------------------------------------

@pytest.fixture()
def app_logueada():
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


def _stub_vistas(monkeypatch, *, ficha=None, movs=0, hermanas=()):
    from modules.clientes import views
    monkeypatch.setattr(views, "_ficha_por_id",
                        lambda i: dict(ficha) if ficha else None)
    monkeypatch.setattr(
        views.queries, "movimientos_por_codigo",
        lambda cod, conn=None: [
            {"tabla": t, "etiqueta": e, "n": movs if i == 0 else 0}
            for i, (t, e) in enumerate(q._TABLAS_CON_CODIGO_CLI)
        ],
    )
    monkeypatch.setattr(views.queries, "fichas_con_el_codigo",
                        lambda cod, conn=None: [dict(h) for h in hermanas])
    monkeypatch.setattr(views.queries, "sugerir_codigos",
                        lambda nombre, limite=8, conn=None: ["LEE", "LEO"])
    return views


def test_las_tres_rutas_existen_en_el_url_map(app_logueada):
    """Los links entre pantallas son strings: la ruta tiene que existir."""
    reglas = {r.rule: r.methods for r in app_logueada.url_map.iter_rules()}
    assert "GET" in reglas["/clientes/<int:id_cliente>/cambiar-codigo"]
    assert "POST" in reglas["/clientes/<int:id_cliente>/confirmar-cambio-codigo"]
    assert "POST" in reglas["/clientes/<int:id_cliente>/aplicar-cambio-codigo"]


def test_el_formulario_renderiza_y_muestra_las_fichas_hermanas(app_logueada, monkeypatch):
    _stub_vistas(monkeypatch, ficha=LEC_CISNEROS, movs=0,
                 hermanas=[LEC_CANAMAR, LEC_CISNEROS])
    rv = app_logueada.test_client().get("/clientes/200/cambiar-codigo")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "LUIS ERNESTO CANAMAR" in body
    assert "LEE" in body  # sugerencia


def test_el_formulario_da_404_si_la_ficha_no_existe(app_logueada, monkeypatch):
    _stub_vistas(monkeypatch, ficha=None)
    assert app_logueada.test_client().get("/clientes/9999/cambiar-codigo").status_code == 404


def test_hay_pantalla_de_confirmacion_antes_de_aplicar(app_logueada, monkeypatch):
    """Requisito: confirmar mostrando qué se mueve y cuánto, sin escribir."""
    views = _stub_vistas(monkeypatch, ficha=LEC_CISNEROS)
    plan = {
        "cliente": dict(LEC_CISNEROS), "codigo_actual": "LEC",
        "codigo_nuevo": "LEE", "total_movimientos": 0,
        "movimientos": [{"tabla": t, "etiqueta": e, "n": 0}
                        for t, e in q._TABLAS_CON_CODIGO_CLI],
        "fichas_hermanas": [dict(LEC_CANAMAR)],
    }
    monkeypatch.setattr(views.queries, "plan_cambio_codigo",
                        lambda i, nuevo, conn=None: plan)
    aplicado = []
    monkeypatch.setattr(views.queries, "cambiar_codigo",
                        lambda *a, **kw: aplicado.append(a))

    rv = app_logueada.test_client().post(
        "/clientes/200/confirmar-cambio-codigo", data={"nuevo_codigo": "LEE"})
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "LEC" in body and "LEE" in body
    assert "0 facturas" in body, "tiene que mostrar QUÉ se mueve y cuánto"
    assert "/clientes/200/aplicar-cambio-codigo" in body
    assert aplicado == [], "confirmar NO puede aplicar nada"


def test_confirmar_con_destino_ocupado_no_muestra_el_boton(app_logueada, monkeypatch):
    views = _stub_vistas(monkeypatch, ficha=LEC_CISNEROS)

    def explota(i, nuevo, conn=None):
        raise ValueError("Ya existe un cliente con código LEE (LEONOR ESPIN).")

    monkeypatch.setattr(views.queries, "plan_cambio_codigo", explota)
    rv = app_logueada.test_client().post(
        "/clientes/200/confirmar-cambio-codigo", data={"nuevo_codigo": "LEE"})
    assert rv.status_code == 302
    assert "/clientes/200/cambiar-codigo" in rv.headers["Location"]


def test_aplicar_esta_gateado_por_el_permiso_de_editar_cliente(app_logueada, monkeypatch):
    """Mismo permiso que editar cliente: `clientes.editar`."""
    _stub_vistas(monkeypatch, ficha=LEC_CISNEROS)

    @app_logueada.before_request
    def _sin_permisos():  # pragma: no cover - infra de test
        from flask import g
        g.permisos = {"clientes.ver"}

    cli = app_logueada.test_client()
    assert cli.get("/clientes/200/cambiar-codigo").status_code == 404
    assert cli.post("/clientes/200/confirmar-cambio-codigo",
                    data={"nuevo_codigo": "LEE"}).status_code == 404
    assert cli.post("/clientes/200/aplicar-cambio-codigo",
                    data={"nuevo_codigo": "LEE"}).status_code == 404


def test_aplicar_deja_rastro_en_la_bitacora(app_logueada, monkeypatch):
    views = _stub_vistas(monkeypatch, ficha=LEC_CISNEROS)
    monkeypatch.setattr(views.queries, "cambiar_codigo", lambda i, nuevo, **kw: {
        "codigo_actual": "LEC", "codigo_nuevo": "LEE", "filas": 1,
        "cliente": dict(LEC_CISNEROS),
    })
    anotado = {}
    monkeypatch.setattr(views, "registrar_bitacora",
                        lambda **kw: anotado.update(kw))

    rv = app_logueada.test_client().post(
        "/clientes/200/aplicar-cambio-codigo",
        data={"nuevo_codigo": "LEE", "motivo": "duplicado con Cañamar"})
    assert rv.status_code == 302
    assert anotado["accion"] == "cambiar_codigo"
    assert anotado["payload"]["codigo_anterior"] == "LEC"
    assert anotado["payload"]["codigo_nuevo"] == "LEE"
    assert anotado["id_entidad"] == 200
