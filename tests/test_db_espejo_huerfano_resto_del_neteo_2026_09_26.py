"""El RESTO de un neteo no es un espejo huérfano. 26/09/2026, MMM y MSJ.

El 25/09 Andrés neteó en el estado de cuenta 10 cheques de MMM ($30.902,52)
contra 18 anticipos ($32.253,61) y 2 de MSJ ($6.571,99) contra 8 ($7.108,39).
Los anticipos sumaban más, así que el neteo dejó el sobrante como saldo a
favor nuevo: un espejo NB=98 "RESTO" (−1.351,09 y −536,40) con padre = el
primer anticipo neteado, que queda en X por el mismo neteo.

El vigía `espejo-huerfano` pregunta "espejo vivo con padre en X" y los cantó
HIGH, mandando a anularlos por error de carga: eso le borraba a los dos
clientes un saldo a favor REAL y subía la utilidad $1.887,49 de una sola punta.

Lo que protege este archivo: el RESTO vivo de un neteo vigente NO suena; el
huérfano de verdad (caso HOM 19/08) SIGUE sonando; y si el neteo se deshace
pero el RESTO quedara vivo, vuelve a sonar.

Corren con `pytest -m db`.
"""
from __future__ import annotations

import pytest

CLI = "ZRS"


def _limpiar(conn) -> None:
    cur = conn.cursor()
    for sql in (
        "DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s",
        "DELETE FROM scintela.mov_doble WHERE metadata ->> 'codigo_cli' = %s",
        "DELETE FROM scintela.cheque WHERE codigo_cli = %s",
        "DELETE FROM scintela.cliente WHERE codigo_cli = %s",
    ):
        try:
            cur.execute(sql, (CLI,))
        except Exception:  # noqa: BLE001
            conn.rollback()
    conn.commit()


def _huerfanos_del_cliente() -> list[int]:
    from modules.admin_dbase.health_audit_view import espejos_huerfanos_filas
    return [int(f["id_cheque"]) for f in espejos_huerfanos_filas()
            if (f.get("codigo_cli") or "").strip() == CLI]


def _stat(id_cheque: int) -> str:
    import db
    return (db.fetch_one("SELECT stat FROM scintela.cheque WHERE id_cheque=%s",
                         (id_cheque,))["stat"] or "").strip().upper()


@pytest.fixture
def cuenta(real_db_conn, migrated_db, monkeypatch):
    """Un cheque de 300 y dos anticipos de −250 y −100: sobran 50."""
    import db
    from modules.cheques import queries as chq

    monkeypatch.setattr(chq, "asegurar_fecha_abierta", lambda *a, **k: None)
    _limpiar(real_db_conn)
    ch = {}
    with db.tx() as c:
        db.execute("INSERT INTO scintela.cliente (codigo_cli, nombre)"
                   " VALUES (%s, 'Prueba resto')", (CLI,), conn=c)
        ch["CH"] = db.fetch_one(
            "INSERT INTO scintela.cheque (no_cheque, fecha, fechad, codigo_cli,"
            " importe, no_banco, stat)"
            " VALUES ('3100','2026-09-01','2026-09-01',%s,300,10,'Z') RETURNING id_cheque",
            (CLI,), conn=c)["id_cheque"]
        for no, imp in [("A1", -250), ("A2", -100)]:
            ch[no] = db.fetch_one(
                "INSERT INTO scintela.cheque (no_cheque, fecha, fechad, codigo_cli,"
                " importe, no_banco, banco, stat)"
                " VALUES (%s,'2026-09-01','2026-09-01',%s,%s,98,'ANTICIPO','Z')"
                " RETURNING id_cheque",
                (no, CLI, imp), conn=c)["id_cheque"]
    yield ch
    _limpiar(real_db_conn)


def _netear(ch) -> dict:
    from modules.cheques import queries as chq
    return chq.netear_cheques_con_anticipos(
        codigo_cli=CLI, ids_cheques=[ch["CH"]],
        ids_anticipos=[ch["A1"], ch["A2"]], usuario="test")


@pytest.mark.db
def test_el_resto_de_un_neteo_no_suena(cuenta):
    res = _netear(cuenta)
    id_resto = res["id_residuo"]
    assert id_resto, "el neteo tenía que dejar el sobrante de 50 como RESTO"
    assert _stat(id_resto) == "Z" and _stat(cuenta["A1"]) == "X"

    assert _huerfanos_del_cliente() == [], (
        "el vigía cantó el RESTO del neteo como huérfano — y su consejo "
        "(anular por error de carga) le borra al cliente un saldo a favor real"
    )


@pytest.mark.db
def test_el_huerfano_de_verdad_sigue_sonando(cuenta):
    """Caso HOM: espejo vivo, padre anulado, sin neteo que lo explique."""
    import db
    with db.tx() as c:
        db.execute("UPDATE scintela.cheque SET stat='X' WHERE id_cheque=%s",
                   (cuenta["A1"],), conn=c)
        id_esp = db.fetch_one(
            "INSERT INTO scintela.cheque (no_cheque, fecha, fechad, codigo_cli,"
            " importe, no_banco, banco, stat, id_cheque_padre)"
            " VALUES ('', '2026-09-02','2026-09-02',%s,-250,98,'ANTICIPO','Z',%s)"
            " RETURNING id_cheque",
            (CLI, cuenta["A1"]), conn=c)["id_cheque"]
    assert _huerfanos_del_cliente() == [id_esp]


@pytest.mark.db
def test_si_el_neteo_se_deshace_y_el_resto_queda_vivo_vuelve_a_sonar(cuenta):
    """El permiso lo da el neteo ACTIVO, no el rótulo "RESTO"."""
    import db
    res = _netear(cuenta)
    id_resto = res["id_residuo"]
    with db.tx() as c:
        # Un deshacer que se olvidara del RESTO: evento reversado, RESTO vivo,
        # padre de nuevo en X (lo forzamos para aislar el caso).
        db.execute(
            "UPDATE scintela.mov_doble SET estado='reversado' "
            " WHERE tipo='neteo_estado_cuenta' AND metadata->>'id_residuo'=%s",
            (str(id_resto),), conn=c)
    assert _stat(id_resto) == "Z"
    assert _huerfanos_del_cliente() == [id_resto]


def test_el_aviso_dice_que_el_resto_del_neteo_no_se_toca():
    from modules.admin_dbase.health_audit_view import _evaluar_espejos_huerfanos
    alerts, _ = _evaluar_espejos_huerfanos(
        [{"id_cheque": 1, "codigo_cli": "HOM", "importe": -1}])
    assert "RESTO" in alerts[0]["por_que"] and "no se toca" in alerts[0]["por_que"]
