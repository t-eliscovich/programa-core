"""Totalizar → deshacer → reponer, contra Postgres REAL. 17/09/2026.

Los tests del 16/09 miraban el código (inspect.getsource); ninguno corría el
ciclo entero, y el ciclo entero estaba roto:

  · Deshacer un totalizar (↺) reponía los vínculos viejos SIN sacar los que
    el totalizar nuevo re-aplicó → cada cheque aplicado DOS veces (X 200 de
    100, Y 100 de 50) — el health de sobre-aplicadas lo cantaba después.
  · El botón "volver a ponerlos en sus facturas" repartía sobre las facturas
    que TENÍAN vínculo, no sobre las que el totalizar tocó: la factura más
    vieja, que se llevó el abono, seguía sin cheque (MTM 176310/176386;
    57 facturas, $118.604 en producción).
  · Un cheque que figuraba en dos corridas del historial entraba dos veces
    como cobro (MHC, 9 cheques), y uno ya re-aplicado a OTRA factura entraba
    entero de nuevo — y el botón quedaba visible para siempre.

Corren con `pytest -m db` contra el Postgres embebido de vista_local.py.
"""
from __future__ import annotations

import pytest

CLI = "ZZT"


def _limpiar(conn) -> None:
    cur = conn.cursor()
    for sql in (
        "DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s",
        "DELETE FROM scintela.chequesxfact_totalizado WHERE codigo_cli = %s",
        "DELETE FROM scintela.mov_doble WHERE metadata ->> 'codigo_cli' = %s",
        "DELETE FROM scintela.factura WHERE codigo_cli = %s",
        "DELETE FROM scintela.cheque WHERE codigo_cli = %s",
        "DELETE FROM scintela.cliente WHERE codigo_cli = %s",
    ):
        try:
            cur.execute(sql, (CLI,))
        except Exception:  # noqa: BLE001 — la tabla del historial puede no existir aún
            conn.rollback()
    conn.commit()


@pytest.fixture
def cuenta(real_db_conn, migrated_db):
    """f1 (vieja) Z sin abono; f2 A con 100 del cheque X; f3 A con 50 del Y.

    El totalizar mueve el abono a f1 (T) y f2 (A 50): el reparto CAMBIA de
    factura para los dos cheques, que es el caso que rompía todo.
    """
    import db
    from modules._lib import vinculos_totalizar as _vt

    _vt.asegurar_tabla()
    _limpiar(real_db_conn)
    f, ch = {}, {}
    with db.tx() as c:
        db.execute("INSERT INTO scintela.cliente (codigo_cli, nombre)"
                   " VALUES (%s, 'Prueba totalizar')", (CLI,), conn=c)
        for numf, fecha, imp, ab, st in [(1, "2026-05-01", 100, 0, "Z"),
                                          (2, "2026-06-01", 200, 100, "A"),
                                          (3, "2026-07-01", 300, 50, "A")]:
            f[numf] = db.fetch_one(
                "INSERT INTO scintela.factura (numf, fecha, codigo_cli, importe,"
                " abono, saldo, stat, usuario_crea)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,'test') RETURNING id_factura",
                (numf, fecha, CLI, imp, ab, imp - ab, st), conn=c)["id_factura"]
        for no, imp, fecha in [("X", 100, "2026-06-10"), ("Y", 50, "2026-07-10")]:
            ch[no] = db.fetch_one(
                "INSERT INTO scintela.cheque (no_cheque, fecha, fechad, codigo_cli,"
                " importe, no_banco, stat, fechaing)"
                " VALUES (%s,%s,%s,%s,%s,10,'B',%s) RETURNING id_cheque",
                (no, fecha, fecha, CLI, imp, fecha), conn=c)["id_cheque"]
        for no, numf, imp in [("X", 2, 100), ("Y", 3, 50)]:
            db.execute(
                "INSERT INTO scintela.chequesxfact (id_cheque, id_fact, fechaing,"
                " codigo_cli, importe, no_banco, tipo)"
                " VALUES (%s,%s,CURRENT_DATE,%s,%s,10,'CH')",
                (ch[no], f[numf], CLI, imp), conn=c)
    yield {"f": f, "ch": ch}
    _limpiar(real_db_conn)


def _vinculos() -> set[tuple[str, int, float]]:
    import db
    return {(r["no_cheque"], r["numf"], float(r["importe"])) for r in db.fetch_all(
        "SELECT c.no_cheque, f.numf, x.importe FROM scintela.chequesxfact x"
        " JOIN scintela.cheque c USING (id_cheque)"
        " JOIN scintela.factura f ON f.id_factura = x.id_fact"
        " WHERE x.codigo_cli = %s", (CLI,))}


def _facturas() -> list[tuple[int, str, float]]:
    import db
    return [(r["numf"], r["stat"], float(r["abono"])) for r in db.fetch_all(
        "SELECT numf, stat, abono FROM scintela.factura WHERE codigo_cli = %s"
        " ORDER BY numf", (CLI,))]


def _sobre_aplicados() -> list:
    import db
    return db.fetch_all(
        "SELECT c.no_cheque FROM scintela.cheque c"
        " JOIN scintela.chequesxfact x USING (id_cheque)"
        " WHERE c.codigo_cli = %s GROUP BY c.no_cheque, c.importe"
        " HAVING SUM(x.importe) > c.importe", (CLI,))


def _ultimo_totalizar() -> int:
    import db
    return db.fetch_one(
        "SELECT id_mov_doble FROM scintela.mov_doble"
        " WHERE tipo = 'totalizar_estado_cuenta' AND metadata ->> 'codigo_cli' = %s"
        " ORDER BY id_mov_doble DESC LIMIT 1", (CLI,))["id_mov_doble"]


@pytest.mark.db
def test_db_el_totalizar_reaplica_y_deja_anotado_que_reaplico(cuenta):
    import db
    from modules.informes import queries as iq

    res = iq.totalizar_estado_cuenta_ejecutar(CLI, usuario="test")
    assert res["n_links_borrados"] == 2 and res["n_links_reaplicados"] == 2
    assert _vinculos() == {("X", 1, 100.0), ("Y", 2, 50.0)}
    assert _facturas() == [(1, "T", 100.0), (2, "A", 50.0), (3, "Z", 0.0)]
    md = db.fetch_one("SELECT metadata FROM scintela.mov_doble WHERE id_mov_doble = %s",
                      (_ultimo_totalizar(),))["metadata"]
    assert len(md["ids_reaplicados"]) == 2


@pytest.mark.db
def test_db_deshacer_deja_los_vinculos_exactamente_como_antes(cuenta):
    """⭐ EL CASO. Antes del arreglo: X→f1, X→f2, Y→f2, Y→f3 — todo doble."""
    from modules.informes import queries as iq

    iq.totalizar_estado_cuenta_ejecutar(CLI, usuario="test")
    res = iq.totalizar_reverso_ejecutar(_ultimo_totalizar(), usuario="test")
    assert res["n_links_sacados"] == 2 and res["n_links_repuestos"] == 2
    assert _vinculos() == {("X", 2, 100.0), ("Y", 3, 50.0)}
    assert _facturas() == [(1, "Z", 0.0), (2, "A", 100.0), (3, "A", 50.0)]
    assert _sobre_aplicados() == []


@pytest.mark.db
def test_db_deshacer_saca_tambien_lo_que_un_reponer_puso_despues(cuenta):
    """Totalizar (viejo: sin re-aplicar) → reponer → deshacer: limpio igual."""
    import db
    from modules._lib import vinculos_totalizar as _vt
    from modules.informes import queries as iq

    iq.totalizar_estado_cuenta_ejecutar(CLI, usuario="test")
    db.execute("DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s", (CLI,))
    _vt.reponer_cliente(CLI, "test")
    assert _vinculos() == {("X", 1, 100.0), ("Y", 2, 50.0)}
    iq.totalizar_reverso_ejecutar(_ultimo_totalizar(), usuario="test")
    assert _vinculos() == {("X", 2, 100.0), ("Y", 3, 50.0)}
    assert _sobre_aplicados() == []


@pytest.mark.db
def test_db_reponer_alcanza_a_la_factura_vieja_que_se_llevo_el_abono(cuenta):
    """Cuenta totalizada a la vieja (sin vínculos): el botón tiene que dar el
    mismo reparto que hoy da el totalizar, y la f1 —que nunca tuvo cheque—
    no puede quedar T sin nadie que la pague. El abono que no vino de un
    cobro de PC (los 20 de f2) queda sin vínculo, a propósito."""
    import db
    from modules._lib import vinculos_totalizar as _vt
    from modules.informes import queries as iq

    iq.totalizar_estado_cuenta_ejecutar(CLI, usuario="test")
    db.execute("DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s", (CLI,))
    db.execute("UPDATE scintela.factura SET abono = abono + 20, saldo = saldo - 20"
               " WHERE codigo_cli = %s AND numf = 2", (CLI,))
    assert _vt.cuantos_por_reponer(CLI) == 2
    res = _vt.reponer_cliente(CLI, "test")
    assert res["vinculos"] == 2
    assert _vinculos() == {("X", 1, 100.0), ("Y", 2, 50.0)}
    assert _facturas() == [(1, "T", 100.0), (2, "A", 70.0), (3, "Z", 0.0)]
    # Segunda vez: no hay nada que hacer y el botón ya no está.
    assert _vt.cuantos_por_reponer(CLI) == 0
    assert _vt.reponer_cliente(CLI, "test")["vinculos"] == 0
    assert _sobre_aplicados() == []


@pytest.mark.db
def test_db_un_cheque_en_dos_corridas_es_un_solo_cobro(cuenta):
    """MHC: el mismo pedazo guardado por dos corridas no se aplica dos veces,
    y un cheque re-aplicado a mano a otra factura no vuelve a entrar entero."""
    import db
    from modules._lib import vinculos_totalizar as _vt
    from modules.informes import queries as iq

    iq.totalizar_estado_cuenta_ejecutar(CLI, usuario="test")
    db.execute("DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s", (CLI,))
    # Una "segunda corrida" activa que guardó los mismos pedazos.
    db.execute(
        f"INSERT INTO {_vt.TABLA} (id_mov_doble, fecha_totalizar, codigo_cli,"
        " id_cheque, id_fact, importe, fechaing, no_banco, tipo)"
        f" SELECT id_mov_doble + 1000000, fecha_totalizar, codigo_cli, id_cheque,"
        " id_fact, importe, fechaing, no_banco, tipo"
        f" FROM {_vt.TABLA} WHERE codigo_cli = %s", (CLI,))
    # Y X ya está vinculado a mano, por 60, a f2.
    db.execute(
        "INSERT INTO scintela.chequesxfact (id_cheque, id_fact, fechaing,"
        " codigo_cli, importe, no_banco, tipo)"
        " VALUES (%s,%s,CURRENT_DATE,%s,60,10,'CH')",
        (cuenta["ch"]["X"], cuenta["f"][2], CLI))
    _vt.reponer_cliente(CLI, "test")
    # A X le quedaban 40 por vincular; van a f1. Y va a lo que queda de f2... no:
    # f1 tiene 100 de abono sin vínculo → X 40 + Y 50 (FIFO por fecha del cobro).
    assert _vinculos() == {("X", 2, 60.0), ("X", 1, 40.0), ("Y", 1, 50.0)}
    assert _sobre_aplicados() == []


@pytest.mark.db
def test_db_una_corrida_deshecha_no_vuelve_al_historial(cuenta):
    import db
    from modules._lib import vinculos_totalizar as _vt
    from modules.informes import queries as iq

    iq.totalizar_estado_cuenta_ejecutar(CLI, usuario="test")
    iq.totalizar_reverso_ejecutar(_ultimo_totalizar(), usuario="test")
    n = db.fetch_one(f"SELECT COUNT(*) AS n FROM {_vt.TABLA} WHERE codigo_cli = %s",
                     (CLI,))["n"]
    assert n == 0
    # El backfill del arranque (que antes no filtraba) tampoco la trae.
    _vt.backfill_desde_metadata()
    n = db.fetch_one(f"SELECT COUNT(*) AS n FROM {_vt.TABLA} WHERE codigo_cli = %s",
                     (CLI,))["n"]
    assert n == 0
    assert _vt.cuantos_por_reponer(CLI) == 0
