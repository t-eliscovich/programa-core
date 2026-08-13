"""Una factura ANULADA no aparece en el estado de cuenta. En ningún renglón.

TMT 2026-08-13, dueña, sobre la #176817 de CTE (001-099-000176817, $77,05,
anulada en Asinfo y marcada `stat='X'` por la migración 0098 el 11/06):

    *"las facturas anuladas en ASINFO, reflejan aun en los estados de cuenta.
    No deberiamos reflejar en ningun lado anuladas"*

## Qué se rompió y por qué costó verlo

`estado_cuenta_cliente()` filtraba con una **lista negra de dos ítems** —
`stat <> 'T'` y `usuario_crea <> 'asinfo-backfill'` — mientras el resto de la
app (cartera, /clientes, el portal, `estado_cuenta_clientes_saldos`) usa la
**lista blanca** canónica `stat IN ('Z','A','',' ')`, que deja afuera la X sin
tener que nombrarla. La anulada se colaba SÓLO acá. No era un filtro roto: era
un filtro que nunca existió.

Lo caro es que la misma función alimenta cinco números y cuatro pantallas:

  · el renglón, el ACUM, el pie "Totales (N facturas)"
  · el tile **Saldo facturas**, el tile **Total (facturas + cheques)** y el
    **% usado del cupo** que cuelga de él
  · `/informes/estado-cuenta/<cli>`, su **PDF** (el que se le manda al cliente
    por WhatsApp), la impresión en lote por vendedor y la ficha del portal
    `/mi-cartera/cliente/<cli>`

Medido contra producción el 13/08: **17 facturas anuladas visibles en 11
clientes, $12.153,78 de saldo fantasma**. TJC mostraba 9.153,97 contra los
3.895,95 que decía /cartera para el mismo cliente el mismo día.

## Por qué estos tests y no los de antes

`tests/test_estado_cuenta_totales.py` ya cubría esta función y estaba VERDE:
buscaba el literal `<> 'T'`, o sea la forma del bug de julio. Un test que
enumera stats muertos sólo protege los que alguien ya se acordó de escribir.
Acá se fija el invariante por el lado de la salida — *lo que la pantalla
devuelve* — así que un stat nuevo que se cuele lo caza igual.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.informes import queries as IQ  # noqa: E402

#: El caso real, con sus números. `CTE` tenía además 5.119,21 + 188,13 vivas.
CLI = "ZZT"

ESQUEMA = """
DROP SCHEMA IF EXISTS scintela CASCADE;
CREATE SCHEMA scintela;
CREATE TABLE scintela.cliente (
  id_cliente bigserial PRIMARY KEY, codigo_cli varchar(10), nombre text,
  telefono text, ruc text, cupo numeric(14,2), stop varchar(2), pago varchar(2),
  pase varchar(2), descuento numeric(9,2), direccion1 text, direccion2 text,
  provincia text, canton text, correo text, observacion text, vend varchar(10));
CREATE TABLE scintela.cliente_mail_asinfo (
  ruc10 varchar(10) PRIMARY KEY, email text);
CREATE TABLE scintela.factura (
  id_factura bigserial PRIMARY KEY, numf bigint, numf_completo text,
  fecha date, vencimiento date, codigo_cli varchar(10), kg numeric(14,2),
  importe numeric(14,2), abono numeric(14,2), retencion numeric(14,2),
  saldo numeric(14,2), stat varchar(2), condic varchar(2), tipo varchar(4),
  usuario_crea text);
CREATE TABLE scintela.banco (
  no_banco integer PRIMARY KEY, nombre text);
CREATE TABLE scintela.cheque (
  id_cheque bigserial PRIMARY KEY, codigo_cli varchar(10), no_cheque text,
  fecha date, fechad date, fechaing date, fechaout date, fecha_recibido date,
  fecha_crea timestamptz, fechad_original date, fecha_postergacion date,
  importe numeric(14,2), stat varchar(2), banco text, no_banco integer,
  id_cheque_padre bigint, usuario_crea text);
"""

#: (numf, fecha, importe, abono, saldo, stat, usuario_crea)
FACTURAS = [
    (900001, date(2026, 6, 1), 5119.21,    0.0, 5119.21, "Z", "asinfo-carga"),
    # ⭐ LA DEL CASO: anulada en Asinfo, marcada X por la migración 0098.
    (900002, date(2026, 6, 4),   77.05,    0.0,   77.05, "X", "asinfo-carga"),
    (900003, date(2026, 6, 22),  188.13,   0.0,  188.13, "Z", "asinfo-carga"),
    (900004, date(2026, 7, 3),   844.90, 100.0,  744.90, "A", "asinfo-carga"),
    # Totalizada: ya estaba fuera desde el 11/06/2026 (no debe volver).
    (900005, date(2026, 5, 10), 1000.00, 1000.0,   0.00, "T", "asinfo-carga"),
    # Backfill histórico: fuera desde el fix NJL del 06/07/2026.
    (900006, date(2026, 4, 2),   500.00,    0.0,  500.00, "Z", "asinfo-backfill"),
]

#: Lo que la pantalla TIENE que mostrar: 5119,21 + 188,13 + 744,90.
SALDO_ESPERADO = 6052.24
#: Lo que mostraba con el bug (SALDO_ESPERADO + los 77,05 de la anulada).
SALDO_CON_EL_BUG = 6129.29


@pytest.fixture
def pg(monkeypatch):
    """Postgres de verdad + `IQ.db` apuntado ahí.

    La base va por `PGDUP_DSN` y tiene que ser de TEST: este fixture hace
    `DROP SCHEMA scintela CASCADE`, así que apuntarlo a otra cosa borra datos.
    """
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
        cur.execute(
            "INSERT INTO scintela.cliente (codigo_cli, nombre, ruc, cupo, stop,"
            " vend) VALUES (%s, %s, %s, %s, %s, %s)",
            (CLI, "CONSORCIO DE PRUEBA", "1790000000001", 20000, "N", "FL1"),
        )
        for numf, fecha, imp, ab, sal, stat, uc in FACTURAS:
            cur.execute(
                "INSERT INTO scintela.factura (numf, numf_completo, fecha,"
                " vencimiento, codigo_cli, kg, importe, abono, retencion,"
                " saldo, stat, tipo, usuario_crea)"
                " VALUES (%s,%s,%s,%s,%s,10,%s,%s,0,%s,%s,'FA',%s)",
                (numf, f"001-099-000{numf}", fecha, fecha, CLI,
                 imp, ab, sal, stat, uc),
            )
    conn.commit()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    class _DB:
        def fetch_all(self, sql, params=None, conn=None):
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

        def fetch_one(self, sql, params=None, conn=None):
            cur.execute(sql, params)
            r = cur.fetchone()
            return dict(r) if r else None

        def execute(self, sql, params=None, conn=None):
            cur.execute(sql, params)

    monkeypatch.setattr(IQ, "db", _DB())
    # `estado_cuenta_cliente` bootstrapea la tabla espejo de mails en caliente
    # (el deploy no corre migraciones). Acá ya está creada por el ESQUEMA.
    from modules.clientes import mail_asinfo as _ma
    monkeypatch.setattr(_ma, "asegurar_tabla", lambda *a, **k: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _numeros(data) -> set[int]:
    return {int(f["numf"]) for f in data["facturas"]}


@pytest.mark.db
def test_la_anulada_no_esta_en_la_lista(pg):
    """El renglón de la #176817 no se dibuja. Es lo que ella vio en pantalla."""
    data = IQ.estado_cuenta_cliente(CLI)
    assert 900002 not in _numeros(data), (
        "la factura ANULADA (stat X) sigue apareciendo en el estado de cuenta"
    )


@pytest.mark.db
def test_lo_que_queda_es_exactamente_lo_vivo(pg):
    """Ni de más ni de menos: sin X, sin T y sin backfill — nada más."""
    assert _numeros(IQ.estado_cuenta_cliente(CLI)) == {900001, 900003, 900004}


@pytest.mark.db
def test_la_anulada_no_suma_en_el_saldo(pg):
    """`totales.saldo` es el tile "Saldo facturas" y el último ACUM.

    Con el bug daba 6.129,29 — los 77,05 de la anulada arriba del saldo real.
    """
    totales = IQ.estado_cuenta_cliente(CLI)["totales"]
    assert round(float(totales["saldo"]), 2) == SALDO_ESPERADO
    assert round(float(totales["saldo"]), 2) != SALDO_CON_EL_BUG


@pytest.mark.db
def test_el_pie_cierra_con_las_filas_visibles(pg):
    """El invariante que el bug de julio (caso EDU) ya había roto una vez.

    Si el pie y la lista miran universos distintos, "Totales (N facturas)"
    miente y el último ACUM deja de ser el saldo. Se verifica sobre las TRES
    columnas de plata, no sólo el saldo.
    """
    data = IQ.estado_cuenta_cliente(CLI)
    totales = data["totales"]
    for col in ("importe", "abono", "saldo", "kg"):
        suma_filas = round(sum(float(f[col] or 0) for f in data["facturas"]), 2)
        assert round(float(totales[col]), 2) == suma_filas, (
            f"el pie de '{col}' no coincide con la suma de las filas visibles"
        )


@pytest.mark.db
def test_saldo_vivo_y_saldo_del_pie_ya_no_divergen(pg):
    """Los dos números que la pantalla mostraba juntos, ahora son el mismo.

    `saldo_vivo` SIEMPRE usó la lista blanca; `saldo` usaba la lista negra.
    Esa grieta es la que dejaba pasar la anulada, y se veía como un tile que
    no cerraba con el pie de su propia tabla.
    """
    totales = IQ.estado_cuenta_cliente(CLI)["totales"]
    assert round(float(totales["saldo"]), 2) == round(float(totales["saldo_vivo"]), 2)


@pytest.mark.db
def test_no_hay_stat_muerto_en_la_salida(pg):
    """Fija el invariante, no la lista de stats que hoy se me ocurren.

    Si mañana alguien inventa un stat nuevo y la pantalla lo empieza a
    mostrar, este test se cae aunque nadie se acuerde de venir a editarlo.
    """
    stats = {(f["stat"] or "").strip() for f in IQ.estado_cuenta_cliente(CLI)["facturas"]}
    assert stats <= {"Z", "A", ""}, f"stats no vivos en el estado de cuenta: {stats}"
