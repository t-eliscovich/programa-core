"""/admin/clientes-asinfo — el código de cliente lo dice ASINFO, no las iniciales.

QUÉ PASÓ. `scintela.cliente` tiene 20 códigos de 3 letras REPETIDOS: dos
empresas distintas comparten código porque el código son las iniciales del
nombre (`LEC` = Luis Ernesto Cañamar y Lola Emperatriz Cisneros; `LUL` = Luis
Llugla y Luis Lopez). Como todo el sistema JOINea por `codigo_cli`, el código
repetido **duplica la plata**: infló la comisión de un vendedor en $4.341,86 y
muestra el saldo dos veces en el estado de cuenta.

El desempate no está en PC: está en **Asinfo**, el ERP que factura contra el
RUC. Caso testigo ya verificado: el RUC `1591718165001` (ASOTEXMANA) en Asinfo
es **NUF**; en PC estaba cargado como **GUF**, que es Guadalupe Fiallos.

Estos tests fijan el contrato de la pantalla:

1. La SQL que se le manda a Asinfo es **SQL Server** (TOP / ISNULL / LEN),
   no Postgres.
2. El cruce PC↔Asinfo va por los **10 primeros dígitos del RUC**, nunca por
   el RUC completo (PC guarda a veces la cédula pelada, Asinfo el RUC).
3. Con **Metabase caído** la vista devuelve 200 y muestra igual el lado PC.
4. La vista **no escribe nada**: ni INSERT, ni UPDATE, ni DELETE.
"""
from __future__ import annotations

import contextlib
import inspect
import os
import re
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.test_routes_smoke import build_app  # noqa: E402

URL = "/admin/clientes-asinfo/"

# Las dos fichas GUF reales (03/08/2026). La primera es ASOTEXMANA, que en
# Asinfo es NUF; la segunda es la Guadalupe Fiallos legítima.
FICHA_ASOTEXMANA = {
    "id_cliente": 31488, "codigo_cli": "GUF",
    "nombre": "ASOCIACION DE PRODUCCION TEXTIL MANOS UNIDAS DE NAPO ASOTEXMANA",
    "ruc": "1591718165001", "vend": "PPR", "activo": True,
    "n_facturas": 55, "n_cheques": 8,
}
FICHA_GUADALUPE = {
    "id_cliente": 20114, "codigo_cli": "GUF",
    "nombre": "GUADALUPE FIALLOS", "ruc": "1801234567001", "vend": "EDG",
    "activo": True, "n_facturas": 55, "n_cheques": 8,
}

# Lo que Asinfo contesta para esos dos RUC10.
ASINFO = {
    "1591718165": [{"codigo": "NUF", "nombre_fiscal": "ASOTEXMANA", "es_cliente": True}],
    "1801234567": [{"codigo": "GUF", "nombre_fiscal": "FIALLOS GUADALUPE", "es_cliente": True}],
}


# --------------------------------------------------------------------------
# Helpers. Se pisan `db.*` y `metabase_client.*` con context managers propios
# (no monkeypatch) porque `build_app()` YA pisa `db.*` y restaura al final del
# fixture: con monkeypatch el orden de teardown deja el stub puesto para toda
# la suite. Ver el comentario de `_apply_db_stubs` en test_routes_smoke.py.
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _pc_devuelve(fichas):
    """`db.fetch_all` devuelve estas fichas (el lado Programa Core)."""
    import db as real_db
    previo = real_db.fetch_all
    real_db.fetch_all = lambda *a, **kw: [dict(f) for f in fichas]
    try:
        yield
    finally:
        real_db.fetch_all = previo


@contextlib.contextmanager
def _db_execute_prohibido():
    """Cualquier escritura durante el request revienta el test."""
    import db as real_db
    previo = real_db.execute

    def _boom(*a, **kw):
        raise AssertionError(f"la pantalla de diagnóstico ESCRIBIÓ: {a[:1]}")

    real_db.execute = _boom
    try:
        yield
    finally:
        real_db.execute = previo


@contextlib.contextmanager
def _asinfo(*, disponible=True, filas=None, contesto=True, capturas=None):
    """Pisa el cliente Metabase. `capturas` recibe la SQL que se mandó."""
    from modules._lib import metabase_client as mc
    previo_disp, previo_fetch = mc.disponible, mc.fetch_dataset_estado

    def _fake(db_id, sql, *a, **kw):
        if capturas is not None:
            capturas.append((db_id, sql))
        return (list(filas or []), contesto)

    mc.disponible = lambda: disponible
    mc.fetch_dataset_estado = _fake
    try:
        yield
    finally:
        mc.disponible, mc.fetch_dataset_estado = previo_disp, previo_fetch


@pytest.fixture
def app():
    app, deshacer = build_app()

    # `app.py` importa `auth.load_logged_in_user` en el import, así que pisar
    # el loader sólo funciona si app.py no se importó todavía. Registramos
    # nuestro before_request DESPUÉS de create_app: corre siempre.
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "test", "id_rol": 1,
                  "nombre_rol": "Dueño", "activo": True}
        g.permisos = {"*"}

    try:
        yield app
    finally:
        deshacer()


# --------------------------------------------------------------------------
# 1. La SQL a Asinfo es SQL Server
# --------------------------------------------------------------------------


def test_sql_a_asinfo_usa_dialecto_sql_server_no_postgres():
    from modules.admin_dbase import clientes_asinfo_view as v

    sql = v.sql_asinfo(["1591718165", "1801234567"])
    assert sql, "con RUCs válidos tiene que salir SQL"

    # SQL Server pagina con TOP, no con LIMIT. Un LIMIT acá vuelve con
    # "Incorrect syntax near 'LIMIT'" y la pantalla se queda sin lado Asinfo.
    assert re.search(r"\bSELECT\s+TOP\s+\d+", sql, re.I), (
        "SQL Server necesita `SELECT TOP n`; con LIMIT la query no corre"
    )
    assert not re.search(r"\bLIMIT\b", sql, re.I), "LIMIT es Postgres, no SQL Server"
    assert not re.search(r"\bILIKE\b", sql, re.I), "ILIKE no existe en SQL Server"
    assert "::" not in sql, "el cast `::` es Postgres; SQL Server usa CAST()"

    # Los helpers de nulos/strings del dialecto (los mismos que ya usa
    # mail_asinfo._SQL_ASINFO contra esta misma tabla `empresa`).
    assert "ISNULL(" in sql, "SQL Server usa ISNULL, no IFNULL/NVL"
    assert re.search(r"\bLEN\s*\(", sql), "el largo en SQL Server es LEN(), no LENGTH()"
    assert "LTRIM(RTRIM(" in sql, "SQL Server no tiene TRIM() en las versiones viejas"
    assert re.search(r"\bFROM\s+empresa\b", sql, re.I), "el maestro de Asinfo es `empresa`"


def test_sql_a_asinfo_pide_las_columnas_que_la_pantalla_muestra():
    from modules.admin_dbase import clientes_asinfo_view as v

    sql = v.sql_asinfo(["1591718165"])
    for col in ("identificacion", "codigo", "nombre_fiscal", "indicador_cliente"):
        assert col in sql, f"falta la columna {col} en la consulta a Asinfo"


def test_sin_rucs_validos_no_se_consulta_asinfo():
    from modules.admin_dbase import clientes_asinfo_view as v

    assert v.sql_asinfo([]) == ""
    assert v.sql_asinfo(["", "abc", "123"]) == "", (
        "un RUC basura no puede terminar interpolado dentro del IN()"
    )


# --------------------------------------------------------------------------
# 2. El cruce va por los 10 primeros dígitos del RUC
# --------------------------------------------------------------------------


def test_la_vista_le_pide_a_asinfo_el_ruc10_no_el_ruc_completo(app):
    capturas: list = []
    with _pc_devuelve([FICHA_ASOTEXMANA, FICHA_GUADALUPE]), \
            _asinfo(filas=[], capturas=capturas):
        rv = app.test_client().get(URL)
    assert rv.status_code == 200
    assert capturas, "no se consultó a Asinfo"
    db_id, sql = capturas[0]
    assert db_id == 2, "Asinfo es la DB 2 de Metabase"
    assert "'1591718165'" in sql, (
        "el cruce tiene que ir por los 10 primeros dígitos del RUC "
        "(mail_asinfo.ruc10): PC guarda a veces la cédula pelada y Asinfo "
        "el RUC completo — comparar el RUC entero no matchea nunca"
    )
    assert "1591718165001" not in sql, "se mandó el RUC completo en vez del ruc10"


def test_cruzar_matchea_la_cedula_de_pc_contra_el_ruc_de_asinfo():
    from modules.admin_dbase import clientes_asinfo_view as v

    # El mapa de Asinfo viene indexado por ruc10. En PC el mismo titular puede
    # estar como cédula pelada (10) o como RUC completo (13): los dos tienen
    # que caer en la misma entrada. Comparar el RUC entero no matchea nunca.
    for ruc in ("1801234567", "1801234567001", "1801234567002", "1801234567 "):
        grupos = v.cruzar([dict(FICHA_GUADALUPE, ruc=ruc)], ASINFO, True)
        ficha = grupos[0]["fichas"][0]
        assert ficha["ruc10"] == "1801234567", f"ruc {ruc!r} no se normalizó"
        assert ficha["estado"] == v.COINCIDE, f"ruc {ruc!r} no cruzó con Asinfo"


def test_cruzar_marca_la_ficha_que_asinfo_desmiente():
    from modules.admin_dbase import clientes_asinfo_view as v

    grupos = v.cruzar([FICHA_ASOTEXMANA, FICHA_GUADALUPE], ASINFO, True)
    assert len(grupos) == 1 and grupos[0]["codigo_cli"] == "GUF"
    por_id = {f["id_cliente"]: f for f in grupos[0]["fichas"]}

    # ASOTEXMANA está mal codificada: Asinfo dice NUF, la ficha dice GUF.
    mala = por_id[31488]
    assert mala["estado"] == v.DIFIERE
    assert mala["asinfo_codigo"] == "NUF"
    assert mala["asinfo_nombre"] == "ASOTEXMANA"

    # Guadalupe Fiallos sí es GUF.
    assert por_id[20114]["estado"] == v.COINCIDE

    # Exactamente una coincide → Asinfo resuelve el duplicado solo.
    assert grupos[0]["hay_ganador"] is True
    assert grupos[0]["n_fichas"] == 2
    assert grupos[0]["n_facturas"] == 55 and grupos[0]["n_cheques"] == 8


def test_cruzar_no_inventa_ganador_cuando_asinfo_no_conoce_ninguno():
    from modules.admin_dbase import clientes_asinfo_view as v

    grupos = v.cruzar([FICHA_ASOTEXMANA, FICHA_GUADALUPE], {}, True)
    assert [f["estado"] for f in grupos[0]["fichas"]] == [v.SIN_ASINFO] * 2
    assert grupos[0]["hay_ganador"] is False


def test_ficha_sin_ruc_no_se_confunde_con_ruc_que_asinfo_no_tiene():
    from modules.admin_dbase import clientes_asinfo_view as v

    grupos = v.cruzar([dict(FICHA_ASOTEXMANA, ruc="")], ASINFO, True)
    assert grupos[0]["fichas"][0]["estado"] == v.SIN_RUC


# --------------------------------------------------------------------------
# 3. Fail-soft: con Metabase caído la pantalla muestra igual el lado PC
# --------------------------------------------------------------------------


def test_metabase_no_configurado_no_rompe_la_pantalla(app):
    with _pc_devuelve([FICHA_ASOTEXMANA, FICHA_GUADALUPE]), _asinfo(disponible=False):
        rv = app.test_client().get(URL)
    assert rv.status_code == 200, "Metabase sin configurar NO puede dar 500"
    body = rv.get_data(as_text=True)
    assert "GUF" in body and "ASOTEXMANA" in body, "se perdió el lado PC"
    assert "no está disponible" in body, "hay que avisar que falta el lado Asinfo"


def test_metabase_no_contesta_no_rompe_la_pantalla(app):
    # Configurado pero timeout/500: `fetch_dataset_estado` devuelve ([], False).
    with _pc_devuelve([FICHA_ASOTEXMANA, FICHA_GUADALUPE]), \
            _asinfo(filas=[], contesto=False):
        rv = app.test_client().get(URL)
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "GUF" in body and "55" in body, "el lado PC tiene que seguir estando"
    assert "no está disponible" in body


def test_metabase_que_revienta_no_rompe_la_pantalla(app):
    from modules._lib import metabase_client as mc
    previo_disp, previo_fetch = mc.disponible, mc.fetch_dataset_estado

    def _explota(*a, **kw):
        raise RuntimeError("connection reset by peer")

    mc.disponible, mc.fetch_dataset_estado = (lambda: True), _explota
    try:
        with _pc_devuelve([FICHA_ASOTEXMANA]):
            rv = app.test_client().get(URL)
    finally:
        mc.disponible, mc.fetch_dataset_estado = previo_disp, previo_fetch
    assert rv.status_code == 200
    assert "GUF" in rv.get_data(as_text=True)


def test_no_contesto_no_es_lo_mismo_que_no_tiene_el_ruc():
    from modules.admin_dbase import clientes_asinfo_view as v

    with _asinfo(filas=[], contesto=False):
        mapa, ok = v.codigos_de_asinfo(["1591718165"])
    assert (mapa, ok) == ({}, False), (
        "un timeout de Metabase se leería como 'Asinfo no tiene el RUC' y la "
        "pantalla diría que la ficha está bien cuando nadie preguntó nada"
    )

    with _asinfo(filas=[{"ruc10": "1591718165", "codigo": "NUF",
                         "nombre_fiscal": "ASOTEXMANA", "indicador_cliente": 1}]):
        mapa, ok = v.codigos_de_asinfo(["1591718165"])
    assert ok is True and mapa["1591718165"][0]["codigo"] == "NUF"


def test_lado_pc_caido_tampoco_rompe(app):
    import db as real_db
    previo = real_db.fetch_all

    def _explota(*a, **kw):
        raise RuntimeError("no se pudo conectar a Postgres")

    real_db.fetch_all = _explota
    try:
        rv = app.test_client().get(URL)
    finally:
        real_db.fetch_all = previo
    assert rv.status_code == 200


# --------------------------------------------------------------------------
# 4. Sólo lectura
# --------------------------------------------------------------------------


def test_la_pantalla_no_escribe_nada_en_postgres(app):
    with _pc_devuelve([FICHA_ASOTEXMANA, FICHA_GUADALUPE]), \
            _db_execute_prohibido(), _asinfo(filas=[]):
        rv = app.test_client().get(URL)
    assert rv.status_code == 200


def test_el_modulo_no_tiene_ninguna_sentencia_de_escritura():
    from modules.admin_dbase import clientes_asinfo_view as v

    src = inspect.getsource(v)
    prohibidas = re.findall(
        r"\b(INSERT\s+INTO|UPDATE\s+scintela|DELETE\s+FROM|DROP\s+TABLE|"
        r"TRUNCATE|CREATE\s+TABLE|ALTER\s+TABLE)\b",
        src, re.I,
    )
    assert not prohibidas, f"la pantalla de diagnóstico escribe: {prohibidas}"
    assert "db.execute" not in src and "_db.execute" not in src, (
        "sólo lectura: este módulo no puede llamar a db.execute"
    )


def test_la_ruta_es_solo_GET_y_esta_gateada(app):
    reglas = [r for r in app.url_map.iter_rules()
              if r.rule.startswith("/admin/clientes-asinfo")]
    assert reglas, "la ruta /admin/clientes-asinfo no está registrada"
    for r in reglas:
        assert "POST" not in (r.methods or set()), "no hay POST en una pantalla de lectura"

    from modules.admin_dbase import clientes_asinfo_view as v
    src = inspect.getsource(v)
    assert "@requiere_login" in src
    assert '@requiere_permiso("admin_dbase.ver")' in src, (
        "las otras pantallas de diagnóstico de admin_dbase gatean con "
        "admin_dbase.ver — esta también"
    )


def test_sin_permiso_no_se_ve():
    app, deshacer = build_app()

    @app.before_request
    def _sin_permisos():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 2
        g.user = {"id_usuario": 2, "username": "bodega", "id_rol": 9,
                  "nombre_rol": "Bodega", "activo": True}
        g.permisos = {"stock.ver"}

    try:
        rv = app.test_client().get(URL)
    finally:
        deshacer()
    assert rv.status_code in (302, 403, 404), (
        "requiere_permiso gatea la ruta entera; sin admin_dbase.ver no se entra"
    )


# --------------------------------------------------------------------------
# 5. El lado PC: qué considera "duplicado"
# --------------------------------------------------------------------------


def test_el_lado_pc_agrupa_por_codigo_y_solo_trae_los_repetidos():
    from modules.admin_dbase import clientes_asinfo_view as v

    sql = v.SQL_DUPLICADOS
    assert re.search(r"GROUP\s+BY\s+UPPER\(TRIM\(codigo_cli\)\)", sql, re.I)
    assert re.search(r"HAVING\s+COUNT\(\*\)\s*>\s*1", sql, re.I), (
        "duplicado = COUNT(*) > 1 agrupando por codigo_cli; sin el HAVING "
        "la pantalla lista los 3.973 clientes"
    )
    assert "scintela.factura" in sql and "scintela.cheque" in sql, (
        "hay que mostrar cuántas facturas y cheques cuelgan del código: es la "
        "plata que el duplicado cuenta dos veces"
    )
