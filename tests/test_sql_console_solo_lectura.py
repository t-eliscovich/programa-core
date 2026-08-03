"""La consola SQL no puede escribir. TMT 2026-08-03.

POR QUÉ EXISTE. El día del bug de la cadena de saldos, cada pregunta de datos
costaba un endpoint nuevo + push + 3 minutos de CI/deploy. La consola contesta
en el momento. El precio es que hay un cuadro de texto que le pega a la base
de producción, así que el guardarraíl tiene que ser real.

⚠ EL INVARIANTE. La garantía de que no escribe **no es** el filtro de texto de
`_validar()` — un blacklist se burla con comentarios, `/**/`, CTEs raras o
mayúsculas. La garantía es `SET TRANSACTION READ ONLY`, que la hace Postgres:
dentro de esa transacción, INSERT/UPDATE/DELETE/DDL fallan con 25006 y no hay
SQL que lo esquive. Si alguien saca esa línea de `correr()`, los tests de acá
abajo tienen que ponerse rojos.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.admin_dbase import sql_console_view as sc  # noqa: E402

# --- capa 1: la que importa de verdad -------------------------------------


def test_la_transaccion_es_READ_ONLY_y_termina_en_ROLLBACK():
    """El invariante de fondo. Sin esto lo demás es decoración."""
    src = inspect.getsource(sc.correr)
    assert "SET TRANSACTION READ ONLY" in src, (
        "se cayó el único guardarraíl que Postgres garantiza: sin "
        "`SET TRANSACTION READ ONLY` la consola puede escribir en producción "
        "en cuanto alguien burle el filtro de texto"
    )
    assert "conn.rollback()" in src
    assert "conn.commit()" not in src, "la consola NUNCA puede confirmar nada"
    # El SET tiene que ir ANTES de la consulta del usuario: Postgres sólo lo
    # acepta como primera sentencia de la transacción.
    assert src.index("SET TRANSACTION READ ONLY") < src.index("cur.execute(sql)")


def test_el_rollback_corre_aunque_la_consulta_explote():
    """Va en un `finally`, no después del happy path."""
    src = inspect.getsource(sc.correr)
    i_fin = src.index("finally:")
    assert "conn.rollback()" in src[i_fin:]


def test_hay_statement_timeout_antes_de_la_consulta():
    src = inspect.getsource(sc.correr)
    assert "statement_timeout" in src
    assert src.index("statement_timeout") < src.index("cur.execute(sql)")


# --- capa 2: el validador --------------------------------------------------


@pytest.mark.parametrize("sql", [
    "DELETE FROM scintela.transacciones_bancarias",
    "UPDATE scintela.historia SET utilidad = 0",
    "INSERT INTO scintela.usuarios (usuario) VALUES ('x')",
    "DROP TABLE scintela.historia",
    "TRUNCATE scintela.banco_matches",
    "ALTER TABLE scintela.historia ADD COLUMN x INT",
    "CREATE TABLE t (a INT)",
    "GRANT ALL ON SCHEMA scintela TO PUBLIC",
    "COPY scintela.historia FROM '/tmp/x.csv'",
    "  \n\t  delete from scintela.historia  ",
    "/* comentario */ DELETE FROM scintela.historia",
    "-- select\nDELETE FROM scintela.historia",
])
def test_rechaza_todo_lo_que_no_sea_lectura(sql):
    assert sc._validar(sql) is not None, f"dejó pasar: {sql!r}"


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from scintela.historia limit 10",
    "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
    "EXPLAIN SELECT * FROM scintela.historia",
    "TABLE scintela.bancos",
    "VALUES (1), (2)",
    "SELECT * FROM scintela.historia FETCH FIRST 10 ROWS ONLY",
    "SELECT 'DELETE FROM scintela.historia' AS texto",
    "SELECT concepto FROM scintela.transacciones_bancarias WHERE concepto LIKE '%;%'",
    "SELECT 1 -- delete from scintela.historia",
    "SELECT 1; ",
])
def test_deja_pasar_la_lectura_legitima(sql):
    assert sc._validar(sql) is None, f"frenó una consulta buena: {sql!r}"


def test_el_punto_y_coma_en_un_literal_no_cuenta_como_dos_sentencias():
    """Si el validador no entiende comillas, frena consultas buenas.

    Un filtro que se lleva puesto un dato bueno es peor que el bug que
    arregla — la misma lección del filtro de rótulos del Excel.
    """
    assert sc._validar("SELECT * FROM t WHERE nota = 'a; b'") is None


def test_frena_dos_sentencias_de_verdad():
    m = sc._validar("SELECT 1; DELETE FROM scintela.historia")
    assert m and "Una sentencia por vez" in m


@pytest.mark.parametrize("sql", [
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT lo_export(1, '/tmp/x')",
    "SELECT pg_terminate_backend(1)",
    "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "SELECT pg_sleep(3600)",
    "SELECT PG_READ_FILE ( '/etc/passwd' )",
])
def test_frena_lo_que_sale_de_la_base(sql):
    """Postgres las deja pasar en read-only; nosotros no.

    Leen el disco del servidor, matan conexiones o abren sockets afuera.
    """
    assert sc._validar(sql) is not None, f"dejó pasar: {sql!r}"


def test_explain_analyze_avisa_que_ejecuta():
    m = sc._validar("EXPLAIN ANALYZE SELECT * FROM scintela.historia")
    assert m and "corre la consulta de verdad" in m


def test_una_columna_que_se_llama_como_una_prohibida_no_molesta():
    """`pg_read_file` prohibido es la FUNCIÓN, no la palabra suelta."""
    assert sc._validar("SELECT pg_read_file AS x FROM t") is None


# --- capa 3: no filtrar credenciales --------------------------------------


def test_tapa_las_columnas_que_parecen_credenciales():
    cols = ["usuario", "password_hash", "clave", "totp_secret", "importe"]
    filas = [["tamara", "$2b$12$abc", "1234", "JBSWY3DP", 100.5]]
    out = sc._redactar(cols, filas)
    assert out[0][0] == "tamara"
    assert out[0][4] == 100.5
    for i in (1, 2, 3):
        assert out[0][i] == sc._OCULTO, f"se filtró {cols[i]}"


def test_no_tapa_columnas_normales():
    cols = ["fecha", "concepto", "importe", "saldo"]
    filas = [["2026-08-03", "DEPOSITO", 590.27, 1000.0]]
    assert sc._redactar(cols, filas) == filas


def test_null_en_columna_sensible_queda_null():
    """Tapar un NULL diría «hay algo» donde no hay nada."""
    assert sc._redactar(["clave"], [[None]])[0][0] is None


# --- capa 4: la bitácora ---------------------------------------------------


def test_toda_consulta_queda_registrada():
    src = inspect.getsource(sc.consola)
    assert "_bitacora(sql, res)" in src, (
        "sin bitácora no hay forma de auditar quién corrió qué contra "
        "producción"
    )


def test_la_bitacora_no_puede_romper_la_consulta():
    """Fail-soft: si el INSERT del log falla, la consulta igual contesta."""
    src = inspect.getsource(sc._bitacora)
    assert "except Exception" in src and "raise" not in src


# --- el tope de filas ------------------------------------------------------


def test_lee_una_fila_de_mas_para_saber_si_quedo_cortada():
    src = inspect.getsource(sc.correr)
    assert "fetchmany(limite + 1)" in src
    assert "truncado" in src


def test_el_limite_se_clampea():
    """Ni 0 ni 10 millones."""
    assert sc.LIMITE_MAX <= 20000
    src = inspect.getsource(sc.correr)
    assert "min(int(limite or LIMITE_DEFAULT), LIMITE_MAX)" in src


# --- las recetas -----------------------------------------------------------


def test_las_recetas_pasan_su_propio_validador():
    """Una receta guardada que el validador frena es una vergüenza."""
    for r in sc.RECETAS:
        assert sc._validar(r["sql"]) is None, f"receta «{r['nombre']}»: {sc._validar(r['sql'])}"


def test_las_recetas_tienen_lo_que_la_pantalla_muestra():
    ids = set()
    for r in sc.RECETAS:
        for campo in ("id", "nombre", "que_contesta", "sql"):
            assert r.get(campo), f"receta sin `{campo}`: {r}"
        assert r["id"] not in ids, f"id repetido: {r['id']}"
        ids.add(r["id"])


def test_esta_la_receta_que_encuentra_el_bug_del_3_de_agosto():
    """Backdateadas: fila con id posterior y fecha anterior.

    Es la consulta que habría cazado el bug de la cadena en 5 segundos en vez
    de en media jornada. Si se borra, se pierde la lección.
    """
    ids = {r["id"] for r in sc.RECETAS}
    assert {"backdateadas", "cadena-rota", "saldo-vs-suma"} <= ids
    receta = next(r for r in sc.RECETAS if r["id"] == "backdateadas")
    assert "max_fecha_previa" in receta["sql"]
    assert "ORDER BY id_transaccion" in receta["sql"]


# --- permisos --------------------------------------------------------------


def test_la_consola_pide_permiso_de_admin():
    src = inspect.getsource(sc)
    assert '@requiere_permiso("usuarios.admin")' in src
    assert "@requiere_login" in src


# --- el desliteralizador (es el corazón del validador) ---------------------


@pytest.mark.parametrize("entrada,esperado_sin", [
    ("SELECT 'a;b'", ";"),
    ('SELECT "col;raro"', ";"),
    ("SELECT 1 -- ; ojo", ";"),
    ("SELECT 1 /* ; */ + 2", ";"),
    ("SELECT $$ ; $$", ";"),
    ("SELECT $tag$ ; $tag$", ";"),
])
def test_desliteraliza_comillas_comentarios_y_dollar_quotes(entrada, esperado_sin):
    assert esperado_sin not in sc._sin_literales(entrada)


def test_desliteralizar_no_cambia_el_largo():
    """Se reemplaza por espacios, no se borra: las posiciones tienen que
    seguir alineadas con el texto original."""
    for s in ("SELECT 'a;b' FROM t", "SELECT 1 /* x */ + 2", "SELECT $$q$$"):
        assert len(sc._sin_literales(s)) == len(s), s


def test_comilla_escapada_adentro_del_literal():
    """`'no es'' el fin'` es UN literal, no dos."""
    assert ";" not in sc._sin_literales("SELECT 'no es'' el fin ;'")


# --- la ruta existe y está protegida --------------------------------------


def test_la_ruta_esta_registrada_y_el_link_del_menu_resuelve(app):
    """`base.html` linkea a `sql_console.consola`; si no existe, 500 en TODA
    la app (el menú está en el layout base)."""
    from flask import url_for

    reglas = {str(r) for r in app.url_map.iter_rules()
              if str(r).startswith("/admin/sql")}
    assert reglas, "la consola no quedó registrada en app.py"
    with app.test_request_context():
        assert url_for("sql_console.consola").startswith("/admin/sql")


def test_sin_permiso_de_admin_la_consola_no_existe(client, monkeypatch):
    """Mismo criterio que el resto de /admin: 404, no 403 (TMT 2026-05-22)."""
    r = client.get("/admin/sql")
    assert r.status_code in (302, 404), (
        f"la consola SQL contestó {r.status_code} a alguien sin sesión"
    )
