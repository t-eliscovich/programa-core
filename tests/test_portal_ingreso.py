"""Cómo entra el cliente al portal.

TMT 2026-08-26. Un solo camino:

    RUC  →  6 números al correo que ya teníamos  →  elige su clave  →  entra
    después:  RUC + su clave

⭐ **El usuario es el RUC** porque el cliente no se sabe su código de 3 letras
(que es una llave nuestra, no de él). El código igual sigue entrando por el
mismo campo: no se le sacó nada a nadie.

⭐ Y por eso la primera vez YA NO alcanza con el RUC: si el usuario es público
y el secreto es el mismo dato público, no hay secreto. Los 6 números van al
correo que YA teníamos cargado — el cliente no elige la dirección.

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
    vend       text,
    correo     text
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
    """⭐ Si dijera 'ese cliente no existe' contra 'la clave está mal',
    cualquiera podría averiguar qué RUC son clientes de Intela probando de a
    uno."""
    assert "RUC o la clave" in acceso.NO_ENTRO
    assert "no existe" not in acceso.NO_ENTRO.lower()


def test_lo_que_escribe_se_lee_como_RUC_o_como_codigo():
    """El campo es uno solo. Lo que decide es si trae números: un código de
    cliente son letras, y ningún RUC entra en 3 letras."""
    assert acceso.parece_ruc("1791234567001") is True
    assert acceso.parece_ruc("179-123.4567 001") is True
    assert acceso.parece_ruc("ATE") is False
    assert acceso.parece_ruc("") is False


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


def _alta(identificador="ATE"):
    """El primer intento del cliente: existe, y todavía no tiene clave."""
    return acceso.entrar(identificador, "")


def _con_clave(clave="miclave123"):
    _alta()
    acceso.guardar_clave("ATE", clave)


@sin_pg
def test_el_RUC_encuentra_al_cliente(base):
    """⭐ Lo que el cliente sí sabe. Está impreso en cada factura suya."""
    assert [c["codigo_cli"] for c in acceso.cuentas_de("1791234567001")] == ["ATE"]


@sin_pg
def test_la_cedula_pelada_encuentra_al_mismo(base):
    """La ficha a veces tiene la cédula y el cliente escribe el RUC entero, o
    al revés. Los 10 primeros dígitos hacen que las dos formas coincidan."""
    assert [c["codigo_cli"] for c in acceso.cuentas_de("1791234567")] == ["ATE"]


@sin_pg
def test_el_codigo_de_tres_letras_sigue_entrando(base):
    """No se le sacó nada al que ya lo sabe — la oficina y el vendedor lo usan."""
    assert [c["codigo_cli"] for c in acceso.cuentas_de(" ate ")] == ["ATE"]


@sin_pg
def test_un_RUC_que_no_es_de_nadie_no_abre_nada(base):
    assert acceso.cuentas_de("0999999999001") == []


@sin_pg
def test_la_primera_vez_NO_entra_todavia(base):
    """⭐ El corazón del cambio: sin clave elegida no se entra, se manda el
    código al correo. Si el RUC abriera solo, el usuario y el secreto serían el
    mismo dato público."""
    r = acceso.entrar("1791234567001", "")
    assert r["ok"] is False
    assert r["primera_vez"] is True
    assert r["codigo_cli"] == "ATE"
    assert _intentos(base)[-1][0] == "primera_vez"


@sin_pg
def test_la_primera_vez_no_cuenta_como_intento_fallido(base):
    """No es un error del cliente: es el camino normal. Si sumara intentos, el
    cliente nuevo se trabaría solo antes de recibir el correo."""
    for _ in range(acceso.TOPE_INTENTOS + 2):
        acceso.entrar("1791234567001", "")
    c = base.cursor()
    c.execute("SELECT intentos_fallidos, bloqueado_hasta FROM scintela.portal_acceso "
              " WHERE codigo_cli='ATE'")
    assert c.fetchone() == (0, None)


@sin_pg
def test_el_RUC_NUNCA_ABRE_por_si_solo(base):
    """⭐ Ni antes ni después de elegir la clave. El RUC dice QUIÉN es, no que
    sea él."""
    _con_clave()
    assert acceso.entrar("1791234567001", "miclave123")["ok"] is True
    assert acceso.entrar("1791234567001", "1791234567001")["ok"] is False


@sin_pg
def test_con_la_clave_puesta_ya_no_es_la_primera_vez(base):
    _con_clave()
    assert acceso.entrar("1791234567001", "miclave123")["primera_vez"] is False


@sin_pg
def test_un_cliente_que_no_existe_da_el_MISMO_mensaje(base):
    """Y queda anotado igual: muchos intentos contra RUC inexistentes es
    alguien probando de a uno, y eso se tiene que poder ver."""
    r = acceso.entrar("ZZZ", "loquesea")
    assert r["ok"] is False
    assert r["primera_vez"] is False
    assert r["mensaje"] == acceso.NO_ENTRO
    assert _intentos(base, "ZZZ")[-1][0] == "no_existe"


@sin_pg
def test_a_los_cinco_intentos_se_traba(base):
    _con_clave()
    for _ in range(acceso.TOPE_INTENTOS):
        assert acceso.entrar("1791234567001", "no-es")["ok"] is False
    r = acceso.entrar("1791234567001", "miclave123")   # ahora SÍ es la buena
    assert r["ok"] is False
    assert "Esperá" in r["mensaje"], "trabado tiene que decir cuánto falta"
    assert _intentos(base)[-1][0] == "trabado"


@sin_pg
def test_elegir_la_clave_destraba(base):
    """El cliente que se equivocó cinco veces y llama para que lo destraben no
    tiene que quedar trabado también después."""
    _con_clave()
    for _ in range(acceso.TOPE_INTENTOS):
        acceso.entrar("1791234567001", "no-es")
    acceso.guardar_clave("ATE", "otraclave123")
    assert acceso.entrar("1791234567001", "otraclave123")["ok"] is True


@sin_pg
def test_el_vendedor_le_corta_el_acceso(base):
    _con_clave()
    acceso.cortar("ATE", "edg")
    r = acceso.entrar("1791234567001", "miclave123")
    assert r["ok"] is False
    assert r["mensaje"] == acceso.CORTADO


# ---------------------------------------------------------------------------
# Un RUC en dos fichas — la misma empresa cargada dos veces
# ---------------------------------------------------------------------------


def _dos_fichas(cn):
    """AJO y AJ2: Almacenes José Puebla, el único caso real (medido 26/08)."""
    c = cn.cursor()
    c.execute("INSERT INTO scintela.cliente (codigo_cli, nombre, ruc) VALUES "
              "('AJ2', 'ALMACENES JOSE PUEBLA', '1793217341001'), "
              "('AJO', 'ALMACENES JOSE PUEBLA', '1793217341001')")


@sin_pg
def test_un_RUC_en_dos_fichas_devuelve_LAS_DOS(base):
    """⭐ Elegir por él sería mostrarle media deuda sin decírselo."""
    _dos_fichas(base)
    assert [c["codigo_cli"] for c in acceso.cuentas_de("1793217341001")] == ["AJ2", "AJO"]


@sin_pg
def test_la_clave_vive_donde_ya_la_eligieron(base):
    """Si entró alguna vez como AJO, esa es su clave: no se le pide otra por
    haber escrito el RUC."""
    _dos_fichas(base)
    acceso.entrar("AJO", "")            # crea la fila
    acceso.guardar_clave("AJO", "miclave123")
    cuentas = acceso.cuentas_de("1793217341001")
    assert acceso.cuenta_con_la_clave(cuentas) == "AJO"
    r = acceso.entrar("1793217341001", "miclave123")
    assert r["ok"] is True
    assert r["codigo_cli"] == "AJO"
    assert len(r["cuentas"]) == 2, "las dos viajan: adentro elige cuál mira"


@sin_pg
def test_sin_ninguna_clave_elegida_la_primera_es_la_de_acceso(base):
    _dos_fichas(base)
    assert acceso.cuenta_con_la_clave(acceso.cuentas_de("1793217341001")) == "AJ2"


@sin_pg
def test_cortar_no_borra_la_fila(base):
    """Reversar no es eliminar: queda el rastro de que existió y de quién lo
    cortó."""
    _alta()
    acceso.cortar("ATE", "edg")
    c = base.cursor()
    c.execute("SELECT activo, cortado_por FROM scintela.portal_acceso "
              " WHERE codigo_cli = 'ATE'")
    assert c.fetchone() == (False, "edg")


@sin_pg
def test_el_mail_del_portal_no_pisa_la_ficha_del_cliente(base):
    """⚠ Decisión de la dueña: se guarda para medir cuántos lo cambian, y
    pasarlo al maestro lo hace el vendedor desde la pantalla de siempre."""
    _alta()
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
    _alta()
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
    """⭐ Si dijera 'ese cliente no tiene correo' o 'ese RUC no existe', la
    pantalla de recuperación sería un buscador de clientes reales."""
    assert "Si ese RUC" in acceso.MANDADO
    assert "no existe" not in acceso.MANDADO.lower()


def test_al_que_no_le_llega_se_le_dice_que_llame():
    """Los 37 clientes sin correo cargado no entran solos, a propósito: los
    destraba la oficina. El mensaje tiene que decirles qué hacer."""
    assert "llámenos" in acceso.MANDADO.lower()


@sin_pg
def test_el_codigo_de_seis_se_guarda_cifrado(base):
    _alta()
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    seis, mail = acceso.pedir_codigo("ATE")
    assert len(seis) == 6 and mail == "compras@ate.com.ec"
    c = base.cursor()
    c.execute("SELECT codigo_hash FROM scintela.portal_codigo WHERE codigo_cli='ATE'")
    guardado = c.fetchone()[0]
    assert guardado != seis, "el código está en claro en la base"
    assert acceso.coincide(seis, guardado)


@sin_pg
def test_el_correo_de_la_ficha_sirve_para_el_primer_ingreso(base):
    """Es el camino para destrabar a los 37 sin correo en Asinfo, y para
    probar el portal con un correo nuestro sin tocar nada del cliente."""
    _alta()
    base.cursor().execute(
        "UPDATE scintela.cliente SET correo='oficina@intela.com.ec' "
        " WHERE codigo_cli='ATE'")
    seis, mail = acceso.pedir_codigo("ATE")
    assert len(seis) == 6 and mail == "oficina@intela.com.ec"


@sin_pg
def test_el_correo_que_eligio_en_el_portal_gana_al_de_la_ficha(base):
    _alta()
    base.cursor().execute(
        "UPDATE scintela.cliente SET correo='oficina@intela.com.ec' "
        " WHERE codigo_cli='ATE'")
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    assert acceso.pedir_codigo("ATE")[1] == "compras@ate.com.ec"


@sin_pg
def test_sin_correo_no_manda_nada(base):
    """Y el que llama contesta lo mismo igual: ver MANDADO."""
    _alta()
    seis, mail = acceso.pedir_codigo("ATE")
    assert (seis, mail) == ("", "")


@sin_pg
def test_a_un_cliente_cortado_no_se_le_manda_codigo(base):
    _alta()
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    acceso.cortar("ATE", "edg")
    assert acceso.pedir_codigo("ATE") == ("", "")


@sin_pg
def test_el_codigo_sirve_una_sola_vez(base):
    """🚨 Un código de un solo uso que se puede reusar no es de un solo uso."""
    _alta()
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    seis, _ = acceso.pedir_codigo("ATE")
    assert acceso.usar_codigo("ATE", seis) is True
    assert acceso.usar_codigo("ATE", seis) is False


@sin_pg
def test_el_codigo_vencido_no_sirve(base):
    _alta()
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
    _alta()
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    seis, _ = acceso.pedir_codigo("ATE")
    assert acceso.usar_codigo("BRC", seis) is False


@sin_pg
def test_un_codigo_inventado_no_entra(base):
    _alta()
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    acceso.pedir_codigo("ATE")
    assert acceso.usar_codigo("ATE", "000000") is False
    assert acceso.usar_codigo("ATE", "abc") is False
    assert acceso.usar_codigo("ATE", "") is False


@sin_pg
def test_se_recuerda_a_que_correo_le_mandamos(base):
    """Para llenarle la casilla en la pantalla de elegir la clave: acaba de
    recibir el código ahí, así que preguntárselo en blanco es hacerlo tipear de
    nuevo lo que ya sabemos — y ahí mismo lo corrige si es viejo."""
    _alta()
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    acceso.pedir_codigo("ATE")
    assert acceso.ultimo_mail_usado("ATE") == "compras@ate.com.ec"


@sin_pg
def test_sin_codigo_pedido_no_hay_correo_que_recordar(base):
    _alta()
    assert acceso.ultimo_mail_usado("ATE") == ""


@sin_pg
def test_el_codigo_del_correo_destraba(base):
    """El que llegó hasta acá probó que tiene el correo, no que se acuerda la
    clave: dejarlo trabado sería castigarlo por haberse olvidado."""
    _alta()
    acceso.guardar_mail("ATE", "compras@ate.com.ec")
    for _ in range(acceso.TOPE_INTENTOS):
        acceso.entrar("ATE", "0000000000")
    seis, _ = acceso.pedir_codigo("ATE")
    assert acceso.usar_codigo("ATE", seis) is True
    c = base.cursor()
    c.execute("SELECT intentos_fallidos, bloqueado_hasta FROM scintela.portal_acceso "
              " WHERE codigo_cli='ATE'")
    assert c.fetchone() == (0, None)
