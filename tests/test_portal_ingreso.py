"""Cómo entra el cliente al portal.

TMT 2026-08-24, `PLAN_PORTAL_CLIENTE_2026_08_24.md`. Un solo camino:

    código de 3 letras + RUC  →  elige su clave  →  entra
    después:  código + su clave

⚠ **El riesgo que se acepta a propósito**: código y RUC son los dos públicos
—el RUC está en cada factura y en el SRI—, así que alguien con una factura
vieja puede entrar antes que el cliente. Lo que lo frena es que el vendedor lo
ve enseguida y le corta el acceso. Por eso lo que SÍ se testea acá es que quede
rastro de todo y que no se pueda averiguar nada probando.

Los de base necesitan un Postgres descartable (`PG_PORTAL_DSN`):

    PG_PORTAL_DSN=postgresql://pgtest@127.0.0.1:5439/postgres \\
        pytest tests/test_portal_ingreso.py -q
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from pathlib import Path

import pytest

if not hasattr(_dt, "UTC"):          # el sandbox a veces corre python 3.10
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.portal import acceso  # noqa: E402

_DSN = os.environ.get("PG_PORTAL_DSN")
sin_pg = pytest.mark.skipif(not _DSN, reason="sin PG_PORTAL_DSN")

MIG = (ROOT / "migrations" / "0211_portal_cliente.sql").read_text(encoding="utf-8")

#: Lo mínimo de `cliente` que el portal mira. La tabla de verdad tiene decenas
#: de columnas; acá sólo hacen falta estas.
CLIENTE_MINIMO = """
CREATE TABLE scintela.cliente (
    id_cliente serial PRIMARY KEY,
    codigo_cli text,
    nombre     text,
    ruc        text,
    vend       text
);
"""


# ---------------------------------------------------------------------------
# Sin base: las piezas sueltas
# ---------------------------------------------------------------------------


def test_el_codigo_se_normaliza_como_joinea_el_sistema():
    assert acceso.normalizar_codigo("  ate ") == "ATE"
    assert acceso.normalizar_codigo(None) == ""


def test_el_ruc_se_compara_por_los_diez_primeros():
    """En Ecuador el RUC de persona natural es la cédula (10) + '001', y la
    ficha a veces tiene la cédula pelada. Si comparáramos el string entero, el
    cliente que escribe su RUC completo no entraría."""
    assert acceso.ruc10("1791234567001") == acceso.ruc10("1791234567")
    assert acceso.ruc10("179-123.4567 001") == "1791234567"
    assert acceso.ruc10("") == ""


def test_la_clave_se_guarda_cifrada_y_se_puede_comprobar():
    h = acceso.cifrar("hola1234")
    assert h != "hola1234"
    assert acceso.coincide("hola1234", h)
    assert not acceso.coincide("otracosa", h)


def test_comprobar_nunca_levanta():
    """Un hash corrupto en la base no puede tirar la pantalla de ingreso."""
    assert acceso.coincide("x", None) is False
    assert acceso.coincide("", "loquesea") is False
    assert acceso.coincide("x", "no-es-un-hash") is False


def test_la_clave_pide_ocho_y_nada_mas():
    """Pedir mayúsculas, números y un símbolo no la hace más segura de verdad
    y sí hace que la gente la anote en un papel."""
    assert acceso.clave_aceptable("1234567")[0] is False
    assert acceso.clave_aceptable("12345678")[0] is True
    assert acceso.clave_aceptable("mi clave larga")[0] is True


def test_el_codigo_de_seis_es_de_seis_y_no_es_random():
    """🚨 `random` es predecible. Un código de recuperación adivinable es una
    puerta abierta."""
    import inspect
    assert len(acceso.codigo_de_seis()) == 6
    assert acceso.codigo_de_seis().isdigit()
    fuente = inspect.getsource(acceso)
    assert "secrets" in fuente
    assert "import random" not in fuente


def test_el_mensaje_de_error_es_siempre_el_mismo():
    """⭐ Si dijera 'ese código no existe' contra 'la clave está mal',
    cualquiera podría averiguar qué códigos de cliente son reales probando de a
    uno — y el código son 3 letras, o sea 17.576 pruebas."""
    assert "código o la clave" in acceso.NO_ENTRO
    assert "no existe" not in acceso.NO_ENTRO.lower()


# ---------------------------------------------------------------------------
# Con base
# ---------------------------------------------------------------------------


@pytest.fixture()
def base(monkeypatch):
    import psycopg2

    import db as _db

    cn = psycopg2.connect(_DSN)
    cn.autocommit = True
    cur = cn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS scintela CASCADE; CREATE SCHEMA scintela")
    cur.execute(CLIENTE_MINIMO)
    cur.execute(MIG)
    cur.execute("INSERT INTO scintela.cliente (codigo_cli, nombre, ruc, vend) "
                "VALUES ('ATE', 'ALMACENES TEXTILES', '1791234567001', 'EDG')")

    def fetch_one(sql, params=None, *a, **k):
        c = cn.cursor()
        c.execute(sql, params)
        if c.description is None:
            return None
        cols = [d[0] for d in c.description]
        fila = c.fetchone()
        return dict(zip(cols, fila, strict=False)) if fila else None

    def fetch_all(sql, params=None, *a, **k):
        c = cn.cursor()
        c.execute(sql, params)
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, f, strict=False)) for f in c.fetchall()]

    def execute(sql, params=None, *a, **k):
        c = cn.cursor()
        c.execute(sql, params)
        return c.rowcount

    monkeypatch.setattr(_db, "fetch_one", fetch_one)
    monkeypatch.setattr(_db, "fetch_all", fetch_all)
    monkeypatch.setattr(_db, "execute", execute)
    yield cn
    cn.close()


def _intentos(cn, codigo="ATE"):
    c = cn.cursor()
    c.execute("SELECT resultado, con_que FROM scintela.portal_ingreso "
              " WHERE codigo_cli = %s ORDER BY id_portal_ingreso", (codigo,))
    return c.fetchall()


@sin_pg
def test_la_primera_vez_entra_con_el_ruc(base):
    r = acceso.entrar("ate", "1791234567001")
    assert r["ok"] is True
    assert r["codigo_cli"] == "ATE"
    assert r["elegir_clave"] is True, "sin clave, la pantalla siguiente es elegirla"
    assert _intentos(base)[-1] == ("ok", "ruc")


@sin_pg
def test_la_cedula_pelada_tambien_entra(base):
    assert acceso.entrar("ATE", "1791234567")["ok"] is True


@sin_pg
def test_con_el_ruc_de_otro_no_entra(base):
    r = acceso.entrar("ATE", "0999999999001")
    assert r["ok"] is False
    assert r["mensaje"] == acceso.NO_ENTRO
    assert _intentos(base)[-1] == ("ruc_malo", "ruc")


@sin_pg
def test_desde_que_elige_la_clave_el_RUC_YA_NO_ABRE(base):
    """⭐ El punto de todo el diseño. El RUC sirve para el primer ingreso; si
    siguiera abriendo, la clave sería un adorno."""
    acceso.entrar("ATE", "1791234567001")
    ok, _ = acceso.guardar_clave("ATE", "miclave123")
    assert ok

    assert acceso.entrar("ATE", "miclave123")["ok"] is True
    assert acceso.entrar("ATE", "1791234567001")["ok"] is False


@sin_pg
def test_al_entrar_con_la_clave_ya_no_pide_elegirla(base):
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_clave("ATE", "miclave123")
    assert acceso.entrar("ATE", "miclave123")["elegir_clave"] is False


@sin_pg
def test_un_codigo_que_no_existe_da_el_MISMO_mensaje(base):
    """Y queda anotado igual: muchos intentos contra códigos inexistentes es
    alguien probando de a uno, y eso se tiene que poder ver."""
    r = acceso.entrar("ZZZ", "1791234567001")
    assert r["ok"] is False
    assert r["mensaje"] == acceso.NO_ENTRO
    assert _intentos(base, "ZZZ")[-1][0] == "no_existe"


@sin_pg
def test_a_los_cinco_intentos_se_traba(base):
    for _ in range(acceso.TOPE_INTENTOS):
        assert acceso.entrar("ATE", "0000000000")["ok"] is False
    r = acceso.entrar("ATE", "1791234567001")   # ahora SÍ es el RUC bueno
    assert r["ok"] is False
    assert "Esperá" in r["mensaje"], "trabado tiene que decir cuánto falta"
    assert _intentos(base)[-1][0] == "trabado"


@sin_pg
def test_elegir_la_clave_destraba(base):
    """El cliente que se equivocó cinco veces y llama para que lo destraben no
    tiene que quedar trabado también después."""
    for _ in range(acceso.TOPE_INTENTOS):
        acceso.entrar("ATE", "0000000000")
    acceso.guardar_clave("ATE", "miclave123")
    assert acceso.entrar("ATE", "miclave123")["ok"] is True


@sin_pg
def test_el_vendedor_le_corta_el_acceso(base):
    acceso.entrar("ATE", "1791234567001")
    acceso.cortar("ATE", "edg")
    r = acceso.entrar("ATE", "1791234567001")
    assert r["ok"] is False
    assert r["mensaje"] == acceso.CORTADO


@sin_pg
def test_cortar_no_borra_la_fila(base):
    """Reversar no es eliminar: queda el rastro de que existió y de quién lo
    cortó."""
    acceso.entrar("ATE", "1791234567001")
    acceso.cortar("ATE", "edg")
    c = base.cursor()
    c.execute("SELECT activo, cortado_por FROM scintela.portal_acceso "
              " WHERE codigo_cli = 'ATE'")
    assert c.fetchone() == (False, "edg")


@sin_pg
def test_el_mail_del_portal_no_pisa_la_ficha_del_cliente(base):
    """⚠ Decisión de la dueña: se guarda para medir cuántos lo cambian, y
    pasarlo al maestro lo hace el vendedor desde la pantalla de siempre."""
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "nuevo@ate.com.ec", "viejo@ate.com.ec")
    c = base.cursor()
    c.execute("SELECT mail, mail_cambiado FROM scintela.portal_acceso "
              " WHERE codigo_cli = 'ATE'")
    assert c.fetchone() == ("nuevo@ate.com.ec", True)
    c.execute("SELECT count(*) FROM information_schema.columns "
              " WHERE table_schema='scintela' AND table_name='cliente' "
              "   AND column_name='mail'")
    assert c.fetchone()[0] == 0, "el portal no le agregó ni una columna al maestro"


@sin_pg
def test_confirmar_el_mismo_mail_no_cuenta_como_cambio(base):
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "  Compras@ATE.com.ec ", "compras@ate.com.ec")
    c = base.cursor()
    c.execute("SELECT mail_cambiado FROM scintela.portal_acceso WHERE codigo_cli='ATE'")
    assert c.fetchone()[0] is False


@sin_pg
def test_queda_anotado_desde_donde_entro(base):
    acceso.entrar("ATE", "1791234567001", ip="190.152.1.5", navegador="Android")
    c = base.cursor()
    c.execute("SELECT ip, navegador FROM scintela.portal_ingreso "
              " WHERE codigo_cli='ATE' ORDER BY id_portal_ingreso DESC LIMIT 1")
    assert c.fetchone() == ("190.152.1.5", "Android")


# ---------------------------------------------------------------------------
# El correo se pide DOS veces
# ---------------------------------------------------------------------------


def test_el_correo_se_pide_dos_veces():
    """⭐ Es por donde le llega la clave si la olvida. Una letra mal tipeada lo
    deja afuera para siempre y sin forma de darse cuenta: el mail SALE, no
    rebota a la vista de nadie, y el cliente espera un código que nunca llega.
    Pedirlo dos veces es lo único que ataja el error de tipeo."""
    assert acceso.mail_aceptable("a@b.com", "a@b.com")[0] is True
    ok, msg = acceso.mail_aceptable("a@b.com", "a@b.con")
    assert ok is False
    assert "no son iguales" in msg


def test_el_correo_no_es_obligatorio():
    """Cargarlo no se le exige a nadie: 52 clientes no tienen ninguno."""
    assert acceso.mail_aceptable("", "")[0] is True


def test_el_correo_se_compara_sin_mayusculas_ni_espacios():
    assert acceso.mail_aceptable("  Compras@ATE.com.ec ", "compras@ate.com.ec")[0] is True


def test_no_se_valida_el_correo_mas_alla_de_lo_obvio():
    """Una expresión regular estricta rechaza correos raros pero válidos. Para
    el que se equivoca de dominio ya está el pedirlo dos veces."""
    assert acceso.mail_aceptable("juan+facturas@empresa.com.ec",
                                 "juan+facturas@empresa.com.ec")[0] is True
    assert acceso.mail_aceptable("sinarroba", "sinarroba")[0] is False


PLANTILLA_CLAVE = (ROOT / "modules" / "portal" / "templates" / "portal"
                   / "elegir_clave.html").read_text(encoding="utf-8")


def test_la_pantalla_tiene_los_dos_campos_de_correo():
    assert 'name="mail"' in PLANTILLA_CLAVE
    assert 'name="mail2"' in PLANTILLA_CLAVE


# ---------------------------------------------------------------------------
# Olvidé mi clave
# ---------------------------------------------------------------------------


def test_la_respuesta_es_la_misma_exista_o_no_el_cliente():
    """⭐ Si dijera 'ese cliente no tiene correo' o 'ese código no existe',
    la pantalla de recuperación sería un buscador de códigos de cliente
    reales — y el código son sólo 3 letras."""
    assert "Si ese código" in acceso.MANDADO
    assert "no existe" not in acceso.MANDADO.lower()


@sin_pg
def test_el_codigo_de_seis_se_guarda_cifrado(base):
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    seis, mail = acceso.pedir_codigo("ATE")
    assert len(seis) == 6 and mail == "compras@ate.com.ec"
    c = base.cursor()
    c.execute("SELECT codigo_hash FROM scintela.portal_codigo WHERE codigo_cli='ATE'")
    guardado = c.fetchone()[0]
    assert guardado != seis, "el código está en claro en la base"
    assert acceso.coincide(seis, guardado)


@sin_pg
def test_sin_correo_no_manda_nada(base):
    """Y el que llama contesta lo mismo igual: ver MANDADO."""
    acceso.entrar("ATE", "1791234567001")
    seis, mail = acceso.pedir_codigo("ATE")
    assert (seis, mail) == ("", "")


@sin_pg
def test_a_un_cliente_cortado_no_se_le_manda_codigo(base):
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    acceso.cortar("ATE", "edg")
    assert acceso.pedir_codigo("ATE") == ("", "")


@sin_pg
def test_el_codigo_sirve_una_sola_vez(base):
    """🚨 Un código de un solo uso que se puede reusar no es de un solo uso."""
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    seis, _ = acceso.pedir_codigo("ATE")
    assert acceso.usar_codigo("ATE", seis) is True
    assert acceso.usar_codigo("ATE", seis) is False


@sin_pg
def test_el_codigo_vencido_no_sirve(base):
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    seis, _ = acceso.pedir_codigo("ATE")
    base.cursor().execute(
        "UPDATE scintela.portal_codigo SET vence_en = now() - interval '1 minute'")
    assert acceso.usar_codigo("ATE", seis) is False


@sin_pg
def test_el_codigo_de_un_cliente_no_abre_el_de_otro(base):
    c = base.cursor()
    c.execute("INSERT INTO scintela.cliente (codigo_cli, nombre, ruc) "
              "VALUES ('BRC', 'OTRO', '0999999999001')")
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    seis, _ = acceso.pedir_codigo("ATE")
    assert acceso.usar_codigo("BRC", seis) is False


@sin_pg
def test_un_codigo_inventado_no_entra(base):
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    acceso.pedir_codigo("ATE")
    assert acceso.usar_codigo("ATE", "000000") is False
    assert acceso.usar_codigo("ATE", "abc") is False
    assert acceso.usar_codigo("ATE", "") is False


@sin_pg
def test_el_codigo_del_correo_destraba(base):
    """El que llegó hasta acá probó que tiene el correo, no que se acuerda la
    clave: dejarlo trabado sería castigarlo por haberse olvidado."""
    acceso.entrar("ATE", "1791234567001")
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    for _ in range(acceso.TOPE_INTENTOS):
        acceso.entrar("ATE", "0000000000")
    seis, _ = acceso.pedir_codigo("ATE")
    assert acceso.usar_codigo("ATE", seis) is True
    c = base.cursor()
    c.execute("SELECT intentos_fallidos, bloqueado_hasta FROM scintela.portal_acceso "
              " WHERE codigo_cli='ATE'")
    assert c.fetchone() == (0, None)
