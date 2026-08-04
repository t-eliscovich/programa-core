"""Cuánta cobranza no le suma a ningún vendedor.

TMT 2026-08-04. La comisión se apoya en `cliente.vend`, un campo que nadie
vigila. Medido sobre julio 2026: **$1.117.837,12 de 112 clientes sin
vendedor**, contra $1.346.321,48 de 351 con vendedor — casi la mitad del mes
no le suma a nadie, y no había ninguna pantalla donde eso se viera.

⭐ Lo central que fijan estos tests es que **sin vendedor NO es un error**
(la dueña: *"si hay algunos que compran directo a la fábrica sin vendedor"*),
y que sí lo son las dos formas de perderlo sin querer: un código que no es de
ningún vendedor, y un cliente que tenía uno y se quedó sin.

Los que tocan SQL corren contra un Postgres de verdad; el resto siempre.

    PGDUP_DSN=postgresql://testpc:testpc@127.0.0.1:5432/pctest \\
        pytest tests/test_comisiones_sin_vendedor.py -q
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.comisiones import queries as Q  # noqa: E402
from modules.comisiones import sin_vendedor as SV  # noqa: E402

JULIO = (date(2026, 7, 1), date(2026, 7, 31))

ESQUEMA = """
DROP SCHEMA IF EXISTS scintela CASCADE;
CREATE SCHEMA scintela;
CREATE TABLE scintela.cliente (
  id_cliente bigserial PRIMARY KEY, codigo_cli varchar(10), nombre text,
  vend varchar(10));
CREATE TABLE scintela.cheque (
  id_cheque bigserial PRIMARY KEY, codigo_cli varchar(10), importe numeric(14,2),
  fechad date, stat varchar(2));
CREATE TABLE scintela.bitacora_acciones (
  id_bitacora bigserial PRIMARY KEY, ts timestamptz, usuario text, accion text,
  status_http integer, payload jsonb);
"""


def _cliente(cur, codigo, nombre, vend):
    cur.execute("INSERT INTO scintela.cliente (codigo_cli, nombre, vend) "
                "VALUES (%s,%s,%s)", (codigo, nombre, vend))


def _cheque(cur, codigo, importe, *, fechad=date(2026, 7, 10), stat="B"):
    cur.execute("INSERT INTO scintela.cheque (codigo_cli, importe, fechad, stat) "
                "VALUES (%s,%s,%s,%s)", (codigo, importe, fechad, stat))


def _edicion(cur, codigo, vend, ts, *, usuario="alex", status=200,
             accion="clientes.editar"):
    cur.execute(
        "INSERT INTO scintela.bitacora_acciones (ts, usuario, accion, "
        "status_http, payload) VALUES (%s,%s,%s,%s,%s)",
        (ts, usuario, accion, status,
         json.dumps({"codigo_cli": codigo, "vend": vend})),
    )


def _sembrar(cur):
    _cliente(cur, "AAA", "CON VENDEDOR", "PPR")
    _cliente(cur, "BBB", "VENTA DIRECTA", "")
    _cliente(cur, "CCC", "CODIGO VIEJO", "BED")   # BED no es de los seis
    _cliente(cur, "DDD", "SIN COBRANZA", "DJA")   # código raro pero no cobró
    _cheque(cur, "AAA", 1000)
    _cheque(cur, "BBB", 500)
    _cheque(cur, "CCC", 250)


@pytest.fixture
def pg():
    dsn = os.environ.get("PGDUP_DSN")
    if not dsn:
        pytest.skip("Sin PGDUP_DSN: los tests de integración no corren.")
    if "test" not in dsn.rsplit("/", 1)[-1].lower():
        pytest.fail("PGDUP_DSN tiene que apuntar a una base de TEST.")
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(ESQUEMA)
        _sembrar(cur)
    conn.commit()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    class _DB:
        def fetch_all(self, sql, params=None, conn=None):
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    original = SV.db
    SV.db = _DB()
    try:
        yield conn
    finally:
        SV.db = original
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# La cifra
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_parte_la_cobranza_en_tres(pg):
    d = SV.cifra(*JULIO)
    assert d[SV.CON_VENDEDOR]["total"] == 1000
    assert d[SV.SIN_VENDEDOR]["total"] == 500
    assert d[SV.CODIGO_DESCONOCIDO]["total"] == 250


@pytest.mark.db
def test_un_codigo_que_no_es_de_nadie_no_cuenta_como_con_vendedor(pg):
    """⭐ BED, DJA, EDU, X… son restos del backfill.

    Un cliente con uno de esos NO está sin vendedor: está asignado a un
    vendedor que no existe, así que se cae de todos los informes por vendedor
    *y* nadie ve el hueco. Meterlo en "con vendedor" lo esconde justo donde
    duele.
    """
    d = SV.cifra(*JULIO)
    assert d[SV.CON_VENDEDOR]["n_clientes"] == 1
    assert d[SV.CODIGO_DESCONOCIDO]["n_clientes"] == 1


@pytest.mark.db
def test_solo_cuenta_lo_ya_cobrado(pg):
    with pg.cursor() as cur:
        _cheque(cur, "BBB", 9999, stat="Z")   # en cartera: no es plata todavía
    pg.commit()
    assert SV.cifra(*JULIO)[SV.SIN_VENDEDOR]["total"] == 500


@pytest.mark.db
def test_respeta_el_periodo(pg):
    with pg.cursor() as cur:
        _cheque(cur, "BBB", 7777, fechad=date(2026, 6, 30))
    pg.commit()
    assert SV.cifra(*JULIO)[SV.SIN_VENDEDOR]["total"] == 500


@pytest.mark.db
def test_un_cliente_duplicado_no_cuenta_la_cobranza_dos_veces(pg):
    """Regresión del 03/08: `scintela.cliente` tuvo 25 códigos repetidos y un
    JOIN crudo devolvía dos filas por cheque — le infló $4.341,86 a PPR."""
    with pg.cursor() as cur:
        _cliente(cur, "BBB", "VENTA DIRECTA (FICHA REPETIDA)", "")
    pg.commit()
    assert SV.cifra(*JULIO)[SV.SIN_VENDEDOR]["total"] == 500


@pytest.mark.db
def test_lista_los_codigos_raros_que_movieron_plata(pg):
    filas = SV.codigos_desconocidos(*JULIO)
    assert [f["codigo_cli"] for f in filas] == ["CCC"], (
        "DDD tiene código raro pero no cobró: listarlo convierte el reporte "
        "en un directorio de 1.500 fichas viejas"
    )
    assert filas[0]["vend"] == "BED"


# ---------------------------------------------------------------------------
# La alarma: perdieron el vendedor
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_detecta_al_que_tenia_vendedor_y_quedo_sin(pg):
    """Pasó y en silencio: cuatro fichas mudas (ADO/OVB/UDA/LCP) rechazaban el
    guardado con "Nombre requerido" y no guardaban nada — la asignación se
    perdía sin un solo mensaje."""
    with pg.cursor() as cur:
        _edicion(cur, "BBB", "FL1", datetime(2026, 7, 1))
        _edicion(cur, "BBB", "", datetime(2026, 7, 5))
    pg.commit()
    filas = SV.perdieron_vendedor(date(2026, 1, 1))
    assert [f["codigo_cli"] for f in filas] == ["BBB"]
    assert filas[0]["vendedor_anterior"] == "FL1"


@pytest.mark.db
def test_si_ya_se_lo_devolvieron_deja_de_avisar(pg):
    """El chequeo corre todos los días. Sin esto seguiría gritando por algo
    que alguien ya arregló, y eso es exactamente cómo se apaga una alarma."""
    with pg.cursor() as cur:
        _edicion(cur, "BBB", "FL1", datetime(2026, 7, 1))
        _edicion(cur, "BBB", "", datetime(2026, 7, 5))
        cur.execute("UPDATE scintela.cliente SET vend='FL1' WHERE codigo_cli='BBB'")
    pg.commit()
    assert SV.perdieron_vendedor(date(2026, 1, 1)) == []


@pytest.mark.db
def test_el_que_nunca_tuvo_vendedor_no_es_una_perdida(pg):
    """⭐ Venta directa. Es un estado válido, no un error.

    Sin el `LAG`, cualquier edición con `vend` vacío se leería como pérdida y
    la alarma sonaría para los 112 clientes que compran directo a fábrica.
    """
    with pg.cursor() as cur:
        _edicion(cur, "BBB", "", datetime(2026, 7, 1))
        _edicion(cur, "BBB", "", datetime(2026, 7, 5))
    pg.commit()
    assert SV.perdieron_vendedor(date(2026, 1, 1)) == []


@pytest.mark.db
def test_una_edicion_que_fallo_no_prueba_nada(pg):
    """Un POST que volvió 400 no guardó nada — justamente el bug de las fichas
    mudas. Tomarlo como "acá perdió el vendedor" acusa al revés."""
    with pg.cursor() as cur:
        _edicion(cur, "BBB", "FL1", datetime(2026, 7, 1))
        _edicion(cur, "BBB", "", datetime(2026, 7, 5), status=400)
    pg.commit()
    assert SV.perdieron_vendedor(date(2026, 1, 1)) == []


@pytest.mark.db
def test_cada_cliente_aparece_una_sola_vez(pg):
    """Aunque lo hayan editado y desasignado varias veces."""
    with pg.cursor() as cur:
        _edicion(cur, "BBB", "FL1", datetime(2026, 7, 1))
        _edicion(cur, "BBB", "", datetime(2026, 7, 5))
        _edicion(cur, "BBB", "EDG", datetime(2026, 7, 6))
        _edicion(cur, "BBB", "", datetime(2026, 7, 9))
    pg.commit()
    filas = SV.perdieron_vendedor(date(2026, 1, 1))
    assert len(filas) == 1
    assert filas[0]["vendedor_anterior"] == "EDG", "tiene que ser la última"


# ---------------------------------------------------------------------------
# Puros
# ---------------------------------------------------------------------------

def _d(con, sin, desc):
    return {
        SV.CON_VENDEDOR: {"total": con, "n_clientes": 1, "n_cheques": 1},
        SV.SIN_VENDEDOR: {"total": sin, "n_clientes": 1, "n_cheques": 1},
        SV.CODIGO_DESCONOCIDO: {"total": desc, "n_clientes": 1, "n_cheques": 1},
    }


def test_el_porcentaje_junta_el_vacio_con_el_codigo_raro():
    """Para el informe por vendedor los dos son lo mismo: plata que no aparece."""
    assert SV.porcentaje_sin_vendedor(_d(500, 300, 200)) == 50.0


def test_sin_cobranza_el_porcentaje_es_cero_y_no_una_division_por_cero():
    assert SV.porcentaje_sin_vendedor(_d(0, 0, 0)) == 0.0


def test_los_vendedores_oficiales_son_seis():
    assert len(Q.VENDEDORES_OFICIALES) == 6
    assert set(Q.VENDEDORES_OFICIALES) == {"PPR", "EDG", "SEP", "JQU", "FL1", "RMY"}


def test_la_lista_de_vendedores_esta_en_un_solo_lugar():
    """⭐ Estaba escrita adentro de un WHERE. Ahora la usan la comisión Y el
    chequeo de salud: dos copias que se desincronizan hacen que el chequeo
    avise de un cliente que la comisión sí está contando (o al revés)."""
    ruta = os.path.join(_REPO_ROOT, "modules", "comisiones", "queries.py")
    with open(ruta, encoding="utf-8") as fh:
        fuente = fh.read()
    cuerpo = fuente[fuente.index("VENDEDORES_OFICIALES = ("):]
    cuerpo = cuerpo[cuerpo.index("\n"):]
    assert "'PPR','EDG'" not in cuerpo.replace(" ", ""), (
        "volvió una copia literal de la lista de vendedores"
    )
