"""Mover un cobro CONTRA POSTGRES DE VERDAD — TMT 2026-08-24.

🚨 Por qué existe este archivo además del de al lado
(`test_mover_cobro_a_otra_factura_2026_08_24.py`): ahí las dos mitades
—`desaplicar_factura` y `aplicar_a_factura`— están **espiadas**, así que esos
tests pasarían en verde aunque la integración estuviera rota. Prueban que
`mover_aplicacion` ORQUESTA bien; no prueban que la plata termine donde tiene
que terminar.

Acá corre todo de verdad y se mira la BASE después: el abono, el saldo y el
stat de las DOS facturas, las filas de `chequesxfact`, y que no haya aparecido
ni un movimiento bancario.

Un fake que no filtra tapa el bug (lección del caso CEM, 04/08).
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

pytestmark = pytest.mark.db

CLI = "ZQR"          # cliente propio de este archivo, no pisa a los de al lado
NUMF_ORIGEN = 990101
NUMF_DESTINO = 990102


class _DBReal:
    """La interfaz de `db` sobre UNA conexión que nunca commitea."""

    def __init__(self, conn):
        self._conn = conn

    def _cur(self):
        import psycopg2.extras
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def fetch_all(self, sql, params=None, conn=None):
        with self._cur() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def fetch_one(self, sql, params=None, conn=None):
        filas = self.fetch_all(sql, params)
        return filas[0] if filas else None

    def execute(self, sql, params=None, conn=None):
        with self._cur() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def execute_returning(self, sql, params=None, conn=None):
        with self._cur() as cur:
            cur.execute(sql, params)
            try:
                r = cur.fetchone()
            except Exception:  # noqa: BLE001 — sin RETURNING no hay fila
                return None
            return dict(r) if r else None

    @contextlib.contextmanager
    def tx(self):
        """Una sola conexión, con SAVEPOINT — no es un detalle del test.

        `db.tx()` de verdad commitea al salir bien y hace ROLLBACK ante
        cualquier excepción. Si acá sólo cediéramos la conexión, un fallo
        dentro del `with` abortaría la transacción ENTERA (incluida la data
        que sembró el fixture) y no habría forma de mirar si la mitad que ya
        había corrido volvió atrás — que es justo lo que hay que probar.
        El SAVEPOINT reproduce ese borde sin commitear nada.
        """
        with self._conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_tx")
        try:
            yield self._conn
        except Exception:
            with self._conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT sp_tx")
            raise
        else:
            with self._conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT sp_tx")


@pytest.fixture
def escenario(real_pg_dsn, monkeypatch):
    """El caso de RAR del 19/08, reconstruido.

    Un cobro DEPOSITADO de 500 aplicado a una factura de 264,99 (o sea que la
    dejó sobrepagada) y otra factura del mismo cliente con saldo de sobra.
    """
    import psycopg2

    from modules.cheques import queries as cq

    conn = psycopg2.connect(real_pg_dsn)
    fake = _DBReal(conn)
    monkeypatch.setattr(cq, "db", fake)
    # `aplicar_a_factura` y `desaplicar_factura` viven en el MISMO módulo, así
    # que con pisar `cq.db` alcanza. `mov_doble` y `facturas.queries` abren lo
    # suyo: los apuntamos a la misma conexión para que el rollback los lleve.
    import mov_doble
    monkeypatch.setattr(mov_doble, "db", fake, raising=False)
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **k: None)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scintela.banco (no_banco, nombre) VALUES (10,'PICHINCHA'), "
            "(90,'DEP.PICH.') ON CONFLICT DO NOTHING")
        cur.execute(
            "INSERT INTO scintela.cliente (codigo_cli, nombre) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING", (CLI, "CLIENTE DE PRUEBA MOVER"))
        cur.execute(
            """INSERT INTO scintela.factura
               (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, usuario_crea)
               VALUES (%s, %s, %s, 10, 264.99, 500.00, -235.01, 'A', 'test')
               RETURNING id_factura""",
            (NUMF_ORIGEN, date(2026, 6, 24), CLI))
        id_origen = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO scintela.factura
               (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, usuario_crea)
               VALUES (%s, %s, %s, 20, 2406.29, 806.28, 1600.01, 'A', 'test')
               RETURNING id_factura""",
            (NUMF_DESTINO, date(2026, 7, 10), CLI))
        id_destino = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO scintela.cheque
               (no_cheque, fecha, fechad, codigo_cli, importe, no_banco, banco,
                stat, fechaout, fecha_recibido, usuario_crea)
               VALUES ('', %s, %s, %s, 500, 90, 'DEP.PICH.', 'B', %s, %s, 'test')
               RETURNING id_cheque""",
            (date(2026, 7, 20), date(2026, 7, 20), CLI,
             date(2026, 7, 20), date(2026, 7, 20)))
        id_cheque = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO scintela.chequesxfact
               (id_cheque, id_fact, fechaing, codigo_cli, importe, no_banco,
                stat_f, abono_f, saldo_f, usuario_crea)
               VALUES (%s, %s, %s, %s, 500, 90, 'A', 500, -235.01, 'test')""",
            (id_cheque, id_origen, date(2026, 7, 20), CLI))

    try:
        yield {
            "conn": conn, "cq": cq,
            "id_cheque": id_cheque,
            "id_origen": id_origen,
            "id_destino": id_destino,
        }
    finally:
        conn.rollback()
        conn.close()


def _factura(conn, id_factura) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT importe, abono, saldo, TRIM(COALESCE(stat,'')) "
            "  FROM scintela.factura WHERE id_factura = %s", (id_factura,))
        importe, abono, saldo, stat = cur.fetchone()
    return {"importe": float(importe), "abono": float(abono),
            "saldo": float(saldo), "stat": stat}


def _aplicaciones(conn, id_cheque) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id_fact, importe FROM scintela.chequesxfact "
            " WHERE id_cheque = %s ORDER BY id_fact", (id_cheque,))
        return [(r[0], float(r[1])) for r in cur.fetchall()]


# ---------------------------------------------------------------- la plata

def test_la_plata_termina_en_la_otra_factura(escenario):
    e = escenario
    e["cq"].mover_aplicacion(
        id_cheque=e["id_cheque"],
        id_fact_origen=e["id_origen"],
        id_fact_destino=e["id_destino"],
        usuario="test",
    )
    origen = _factura(e["conn"], e["id_origen"])
    destino = _factura(e["conn"], e["id_destino"])

    # La de origen vuelve a quedar sin cobrar…
    assert origen["abono"] == pytest.approx(0.0, abs=0.005)
    assert origen["saldo"] == pytest.approx(264.99, abs=0.005)
    # …y deja de estar sobrepagada, que era el síntoma.
    assert origen["saldo"] > 0

    # La de destino recibe los 500 exactos.
    assert destino["abono"] == pytest.approx(1306.28, abs=0.005)
    assert destino["saldo"] == pytest.approx(1100.01, abs=0.005)


def test_la_aplicacion_se_mudo_de_fila(escenario):
    """No queda rastro en la vieja ni se duplica en la nueva."""
    e = escenario
    assert _aplicaciones(e["conn"], e["id_cheque"]) == [(e["id_origen"], 500.0)]
    e["cq"].mover_aplicacion(
        id_cheque=e["id_cheque"], id_fact_origen=e["id_origen"],
        id_fact_destino=e["id_destino"], usuario="test")
    assert _aplicaciones(e["conn"], e["id_cheque"]) == [(e["id_destino"], 500.0)]


def test_el_cobro_sigue_depositado_y_no_aparece_ningun_movimiento_de_banco(escenario):
    """EL punto de toda la pantalla.

    Recargar el cobro por Cobranza acuña un 'DE' nuevo — eso fue lo que dejó
    $500 de más en Pichincha el 19/08. Mover no puede tocar el banco.
    """
    e = escenario
    with e["conn"].cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM scintela.transacciones_bancarias")
        antes = cur.fetchone()[0]

    e["cq"].mover_aplicacion(
        id_cheque=e["id_cheque"], id_fact_origen=e["id_origen"],
        id_fact_destino=e["id_destino"], usuario="test")

    with e["conn"].cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM scintela.transacciones_bancarias")
        despues = cur.fetchone()[0]
        cur.execute("SELECT TRIM(COALESCE(stat,'')), fechaout "
                    "  FROM scintela.cheque WHERE id_cheque = %s", (e["id_cheque"],))
        stat, fechaout = cur.fetchone()
    assert despues == antes, "mover NO puede crear un movimiento bancario"
    assert stat == "B", "el cobro sigue depositado"
    assert fechaout == date(2026, 7, 20), "ni le cambia la fecha de salida"


# ---------------------------------------------------------------- los frenos

def test_si_no_entra_en_la_de_destino_no_queda_nada_a_medias(escenario):
    """El freno que importa: la mitad que ya corrió tiene que volver atrás.

    Se mueve a una factura chica: `aplicar_a_factura` rechaza el importe que
    excede el saldo, y ahí la de origen YA está desaplicada. Si la transacción
    no fuera una sola, la factura vieja quedaría abierta y los 500 perdidos.
    """
    e = escenario
    with e["conn"].cursor() as cur:
        cur.execute(
            """INSERT INTO scintela.factura
               (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, usuario_crea)
               VALUES (%s, %s, %s, 1, 20.00, 0, 20.00, 'Z', 'test')
               RETURNING id_factura""",
            (990103, date(2026, 7, 15), CLI))
        id_chica = cur.fetchone()[0]

    antes = _factura(e["conn"], e["id_origen"])
    aplic_antes = _aplicaciones(e["conn"], e["id_cheque"])
    with pytest.raises(Exception):  # noqa: B017 — cualquier fallo sirve: el punto es el rollback
        e["cq"].mover_aplicacion(
            id_cheque=e["id_cheque"], id_fact_origen=e["id_origen"],
            id_fact_destino=id_chica, usuario="test")

    assert _factura(e["conn"], e["id_origen"]) == antes, (
        "la factura de origen quedó desaplicada a medias: los 500 se perdieron"
    )
    assert _aplicaciones(e["conn"], e["id_cheque"]) == aplic_antes


def test_no_se_mueve_a_la_factura_de_otro_cliente(escenario):
    e = escenario
    with e["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO scintela.cliente (codigo_cli, nombre) VALUES ('ZQS','OTRO') "
            "ON CONFLICT DO NOTHING")
        cur.execute(
            """INSERT INTO scintela.factura
               (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, usuario_crea)
               VALUES (990104, %s, 'ZQS', 1, 900.00, 0, 900.00, 'Z', 'test')
               RETURNING id_factura""", (date(2026, 7, 15),))
        id_ajena = cur.fetchone()[0]

    with pytest.raises(ValueError) as exc:
        e["cq"].mover_aplicacion(
            id_cheque=e["id_cheque"], id_fact_origen=e["id_origen"],
            id_fact_destino=id_ajena, usuario="test")
    assert "otro cliente" in str(exc.value)
    assert _aplicaciones(e["conn"], e["id_cheque"]) == [(e["id_origen"], 500.0)]


# ------------------------------------------------------------- el picker

def test_el_picker_trae_la_de_destino_y_no_la_de_origen(escenario):
    e = escenario
    destinos = e["cq"].facturas_destino_para_mover(CLI, e["id_origen"])
    ids = [d["id_factura"] for d in destinos]
    assert e["id_destino"] in ids
    assert e["id_origen"] not in ids
