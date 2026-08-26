"""El estado de cuenta de VARIOS clientes en una consulta — y que dé lo mismo.

⭐ TMT 2026-08-26 (dueña): *"algo más que dure mucho tiempo y podamos bajar"*.

Imprimir los estados de cuenta de todos los clientes con saldo hacía SIETE
consultas por cliente: 3.644 para 520 clientes, cada una yendo por la red hasta
el RDS. Ahora son seis en total, con la lista de códigos adentro del `WHERE`.

El riesgo de un cambio así no es que salga lento: es que las filas terminen
**en el cliente equivocado**, y eso en una hoja que se le manda al cliente no
se nota hasta que reclama. Por eso estos tests miran una sola cosa desde dos
lados:

  · con una base de VERDAD (`@pytest.mark.db`): pedir el lote de dos clientes
    tiene que devolver, cliente por cliente, exactamente lo mismo que pedirlos
    de a uno. Es la comparación que no se puede simular con un fake.
  · sin base: que las filas de dos clientes que vienen mezcladas en una sola
    respuesta se repartan bien y no se le crucen a nadie.
"""
from __future__ import annotations

from datetime import date

import pytest

# ---------------------------------------------------------------------------
# Sin base: el reparto de las filas
# ---------------------------------------------------------------------------


def _fake_db(monkeypatch, *, clientes, facturas, cheques, anticipos, totales):
    """Devuelve por `fetch_all` lo que pediría cada una de las seis consultas."""
    import db

    def _fetch_all(sql, params=None, conn=None):
        plano = " ".join(sql.split()).lower()
        if "from scintela.cliente c" in plano:
            return clientes
        if "id_factura, numf, numf_completo" in plano:
            return facturas
        if "sum(kg)" in plano:
            return totales["facturas"]
        if "= 98" in plano and "sum(importe)" in plano:
            return totales["anticipos"]
        if "as por_cobrar" in plano:
            return totales["cheques"]
        if "= 98" in plano:
            return anticipos
        return cheques

    monkeypatch.setattr(db, "fetch_all", _fetch_all)
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)


def _armar(monkeypatch):
    """Dos clientes, con las filas MEZCLADAS como vienen de la base."""
    from modules.informes import queries as iq

    _fake_db(
        monkeypatch,
        clientes=[{"codigo_cli": "AAA", "nombre": "UNO"},
                  {"codigo_cli": "BBB", "nombre": "DOS"}],
        facturas=[{"codigo_cli": "AAA", "id_factura": 1, "numf": 11, "saldo": 100.0},
                  {"codigo_cli": "BBB", "id_factura": 2, "numf": 22, "saldo": 200.0},
                  {"codigo_cli": "AAA", "id_factura": 3, "numf": 33, "saldo": 300.0}],
        cheques=[{"codigo_cli": "BBB", "id_cheque": 9, "stat": "Z", "importe": 50.0},
                 {"codigo_cli": "AAA", "id_cheque": 8, "stat": "B", "importe": 60.0}],
        anticipos=[{"codigo_cli": "BBB", "id_cheque": 7, "importe": -25.0}],
        totales={
            "facturas": [{"codigo_cli": "AAA", "kg": 5, "saldo": 400.0},
                         {"codigo_cli": "BBB", "kg": 9, "saldo": 200.0}],
            "cheques": [{"codigo_cli": "AAA", "total": 60.0, "por_cobrar": 0.0},
                        {"codigo_cli": "BBB", "total": 50.0, "por_cobrar": 50.0}],
            "anticipos": [{"codigo_cli": "BBB", "anticipo_raw": -25.0,
                           "n_anticipos": 1}],
        },
    )
    return iq.estado_cuenta_lote(["AAA", "BBB"])


def test_cada_fila_termina_en_SU_cliente(monkeypatch):
    """El error que este cambio podía introducir: la factura de uno en la hoja
    del otro. No hay error, no hay pantalla en rojo — hay un cliente que
    recibe el saldo de un tercero."""
    lote = _armar(monkeypatch)

    assert sorted(lote) == ["AAA", "BBB"]
    assert [f["numf"] for f in lote["AAA"]["facturas"]] == [11, 33]
    assert [f["numf"] for f in lote["BBB"]["facturas"]] == [22]
    assert [c["id_cheque"] for c in lote["AAA"]["cheques"]] == [8]
    assert [c["id_cheque"] for c in lote["BBB"]["cheques"]] == [9]
    assert lote["AAA"]["anticipos"] == []
    assert [a["id_cheque"] for a in lote["BBB"]["anticipos"]] == [7]


def test_los_totales_tambien_van_a_SU_cliente(monkeypatch):
    lote = _armar(monkeypatch)

    assert lote["AAA"]["totales"]["kg"] == 5
    assert lote["BBB"]["totales"]["kg"] == 9
    assert lote["AAA"]["totales"]["cheques_por_cobrar"] == 0.0
    assert lote["BBB"]["totales"]["cheques_por_cobrar"] == 50.0
    # El saldo a favor es de BBB y de nadie más.
    assert lote["AAA"]["totales"]["saldo_a_favor"] == 0
    assert lote["BBB"]["totales"]["saldo_a_favor"] == 25.0
    assert lote["BBB"]["totales"]["saldo_neto"] == 175.0


def test_el_cliente_sin_facturas_no_hereda_las_del_otro(monkeypatch):
    """`GROUP BY` no inventa grupos vacíos: el cliente sin una sola factura no
    trae fila de totales. Antes eso era un agregado sin agrupar que devolvía
    ceros, así que tiene que seguir dando cero — y NO los del vecino."""
    from modules.informes import queries as iq

    _fake_db(
        monkeypatch,
        clientes=[{"codigo_cli": "AAA", "nombre": "UNO"},
                  {"codigo_cli": "VAC", "nombre": "SIN NADA"}],
        facturas=[{"codigo_cli": "AAA", "id_factura": 1, "numf": 11}],
        cheques=[], anticipos=[],
        totales={"facturas": [{"codigo_cli": "AAA", "kg": 5, "saldo": 400.0}],
                 "cheques": [], "anticipos": []},
    )
    lote = iq.estado_cuenta_lote(["AAA", "VAC"])

    assert lote["VAC"]["facturas"] == []
    assert lote["VAC"]["totales"]["kg"] == 0
    assert lote["VAC"]["totales"]["saldo"] == 0
    assert lote["VAC"]["totales"]["saldo_neto"] == 0
    assert lote["AAA"]["totales"]["kg"] == 5


def test_los_cheques_salen_marcados_cliente_por_cliente(monkeypatch):
    """La marca `por_cobrar` es fail-closed: la hoja filtra por ella, así que
    un cheque sin marcar DESAPARECE del papel sin avisar (TMT 2026-08-05)."""
    lote = _armar(monkeypatch)

    for datos in lote.values():
        for c in datos["cheques"]:
            assert isinstance(c["por_cobrar"], bool)
    assert lote["BBB"]["cheques"][0]["por_cobrar"] is True     # Z, en cartera
    assert lote["AAA"]["cheques"][0]["por_cobrar"] is False    # B, depositado


def test_un_cliente_que_no_existe_no_esta_en_el_lote(monkeypatch):
    from modules.informes import queries as iq

    _fake_db(monkeypatch, clientes=[], facturas=[], cheques=[], anticipos=[],
             totales={"facturas": [], "cheques": [], "anticipos": []})
    assert iq.estado_cuenta_lote(["NOP"]) == {}
    # …y el de a uno sigue devolviendo la forma vacía completa, que es lo que
    # esperan las pantallas (ver `totales_estado_cuenta_en_cero`).
    vacio = iq.estado_cuenta_cliente("NOP")
    assert vacio["cliente"] is None
    assert vacio["facturas"] == [] and vacio["cheques"] == []
    assert vacio["totales"] == iq.totales_estado_cuenta_en_cero()


def test_sin_codigos_no_se_consulta_nada(monkeypatch):
    """Un vendedor sin clientes no tiene que disparar seis consultas con una
    lista vacía adentro."""
    import db
    from modules.informes import queries as iq

    def _explota(*a, **k):
        raise AssertionError("consultó la base sin códigos que buscar")

    monkeypatch.setattr(db, "fetch_all", _explota)
    monkeypatch.setattr(db, "fetch_one", _explota)
    assert iq.estado_cuenta_lote([]) == {}
    assert iq.estado_cuenta_lote(None) == {}


def test_un_codigo_repetido_se_pide_una_sola_vez(monkeypatch):
    """La lista de la impresión puede traer el mismo cliente dos veces (un
    grupo mal armado). Pedirlo dos veces a la base no cambia el resultado pero
    duplica el trabajo."""
    from modules.informes import queries as iq

    vistos: list = []
    import db

    def _fetch_all(sql, params=None, conn=None):
        vistos.append(params)
        return [{"codigo_cli": "AAA", "nombre": "UNO"}] if "cliente c" in sql else []

    monkeypatch.setattr(db, "fetch_all", _fetch_all)
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)
    iq.estado_cuenta_lote(["AAA", "AAA", "", None])
    assert vistos[0] == (["AAA"],)


# ---------------------------------------------------------------------------
# Con base de verdad: el lote y el de a uno tienen que dar LO MISMO
# ---------------------------------------------------------------------------


#: ⚠ IDS FIJOS Y ALTOS, y no `MAX(id) + 1`.
#:
#: 🚨 26/08/2026, y me tumbó el CI de `main`: con `MAX(id) + 1` sobre la base de
#: tests —donde `scintela.cliente` viene VACÍA— este test se llevaba los ids 1,
#: 2 y 3… que son exactamente los que la secuencia le iba a dar después a
#: `test_integration_flows`. Tres tests que no tienen nada que ver con éste
#: fallaban con "duplicate key" y el mensaje no nombraba a nadie.
#:
#: La regla, que vale para cualquier test que escriba en la base compartida:
#: **no le pises los números a la secuencia**. Un id alto y fijo no se lo pelea
#: con nadie, y de paso se ve de un vistazo que la fila es de un test.
_ID = 990_000

#: Los tres clientes de este test. Se borran ANTES y DESPUÉS: la base es
#: compartida y lo que queda sembrado se lo come el test siguiente.
_CODIGOS = ("LT1", "LT2", "LT3")


def _limpiar(cur) -> None:
    for tabla in ("cheque", "factura", "cliente"):
        cur.execute(f"DELETE FROM scintela.{tabla} WHERE codigo_cli = ANY(%s)",
                    (list(_CODIGOS),))


def _sembrar(cur) -> None:
    _limpiar(cur)
    for i, (cod, nombre) in enumerate((("LT1", "CLIENTE UNO"), ("LT2", "CLIENTE DOS"),
                                       ("LT3", "CLIENTE SIN MOVIMIENTOS"))):
        cur.execute(
            "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, ruc,"
            " vend, activo) VALUES (%s, %s, %s, %s, %s, TRUE)",
            (_ID + i, cod, nombre, "1790000000001", "PPR"))
    # LT1: dos facturas vivas y una anulada (que NO tiene que salir).
    for i, (numf, imp, saldo, stat) in enumerate((
            (900001, 100.0, 100.0, "Z"), (900002, 250.0, 50.0, "Z"),
            (900003, 999.0, 999.0, "X"))):
        cur.execute(
            "INSERT INTO scintela.factura (id_factura, numf, fecha, codigo_cli,"
            " kg, importe, abono, saldo, stat, vencimiento, retencion)"
            " VALUES (%s, %s, %s, 'LT1', %s, %s, %s, %s, %s, %s, 0)",
            (_ID + i, numf, date(2026, 8, 1 + i), 10.0 * (i + 1), imp,
             imp - saldo, saldo, stat, date(2026, 10, 1)))
    # LT2: una factura y un cheque en cartera + un anticipo (espejo NB=98).
    cur.execute(
        "INSERT INTO scintela.factura (id_factura, numf, fecha, codigo_cli, kg,"
        " importe, abono, saldo, stat, vencimiento, retencion)"
        " VALUES (%s, 900010, %s, 'LT2', 7.0, 700.0, 0, 700.0, 'Z', %s, 0)",
        (_ID + 10, date(2026, 8, 5), date(2026, 11, 3)))
    cur.execute(
        "INSERT INTO scintela.cheque (id_cheque, no_cheque, fecha, fechad,"
        " codigo_cli, importe, no_banco, banco, stat)"
        " VALUES (%s, 'C900010', %s, %s, 'LT2', 300.0, 1, 'PICHINCHA', 'Z')",
        (_ID, date(2026, 8, 5), date(2026, 9, 5)))
    cur.execute(
        "INSERT INTO scintela.cheque (id_cheque, no_cheque, fecha, fechad,"
        " codigo_cli, importe, no_banco, banco, stat)"
        " VALUES (%s, 'ANTICIPO', %s, %s, 'LT2', -120.0, 98, 'ANTICIPO', 'Z')",
        (_ID + 1, date(2026, 8, 6), date(2026, 8, 6)))


@pytest.fixture
def sembrado(real_db_conn, migrated_db):
    """Siembra los tres clientes y los BORRA al terminar.

    🚨 El `commit` es necesario —las consultas que se miden abren su propia
    conexión y no verían una transacción abierta— y por eso mismo el borrado
    del final no es optativo: sin él, estas filas se quedan en la base que
    comparten todos los tests `@pytest.mark.db`."""
    with real_db_conn.cursor() as cur:
        _sembrar(cur)
    real_db_conn.commit()
    try:
        yield real_db_conn
    finally:
        with real_db_conn.cursor() as cur:
            _limpiar(cur)
        real_db_conn.commit()


@pytest.mark.db
def test_el_lote_devuelve_LO_MISMO_que_pedirlos_de_a_uno(sembrado):
    """⭐ El test que sostiene todo el cambio.

    No compara "más o menos": compara el diccionario ENTERO —facturas, cheques,
    anticipos y los veinte números de `totales`— cliente por cliente. Si algún
    día alguien toca una de las seis consultas y se olvida del `GROUP BY`, o
    agrupa por la columna equivocada, esto se pone rojo antes de que un cliente
    reciba la hoja de otro.
    """
    from modules.informes import queries as iq

    cods = ["LT1", "LT2", "LT3"]
    lote = iq.estado_cuenta_lote(cods)
    for cod in cods:
        assert lote[cod] == iq.estado_cuenta_cliente(cod), cod


@pytest.mark.db
def test_el_lote_trae_lo_que_tiene_que_traer(sembrado):
    """Y que la comparación de arriba no sea "dos veces vacío"."""
    from modules.informes import queries as iq

    lote = iq.estado_cuenta_lote(["LT1", "LT2", "LT3"])

    # LT1: las dos vivas, sin la anulada.
    assert [f["numf"] for f in lote["LT1"]["facturas"]] == [900001, 900002]
    assert float(lote["LT1"]["totales"]["saldo"]) == 150.0
    # LT2: su factura, su cheque en cartera y su saldo a favor.
    assert [f["numf"] for f in lote["LT2"]["facturas"]] == [900010]
    assert [c["no_cheque"] for c in lote["LT2"]["cheques"]] == ["C900010"]
    assert lote["LT2"]["cheques"][0]["por_cobrar"] is True
    assert [a["no_cheque"] for a in lote["LT2"]["anticipos"]] == ["ANTICIPO"]
    assert lote["LT2"]["totales"]["saldo_a_favor"] == 120.0
    assert lote["LT2"]["totales"]["saldo_neto"] == 580.0
    # LT3 existe pero no tiene nada: sale en el lote, con todo en cero.
    assert lote["LT3"]["cliente"]["codigo_cli"] == "LT3"
    assert lote["LT3"]["facturas"] == [] and lote["LT3"]["cheques"] == []
    assert lote["LT3"]["totales"]["saldo"] == 0
