"""Tests de /cheques/resumen-dia — resumen_cobranza_dia() + render del template.

La vista es réplica MEJORADA del FINAL (ALTAS.PRG): una tabla, UNA fila por
ingreso del día, con las facturas que pagó (fecha, numf, importe, abonado,
saldo resultante del snapshot chequesxfact) y flag T/A. No toca Postgres:
monkeypatchea db.fetch_all.
"""

from __future__ import annotations

from datetime import date, datetime

from modules.cheques import queries

FECHA = date(2026, 7, 7)


def _rows_cheques():
    """Un depósito Pichincha que totaliza 1 factura, un cheque que abona 2,
    un efectivo sin aplicar y un espejo anticipo NB=98 negativo."""
    return [
        {
            "id_cheque": 1, "no_cheque": None, "importe": 4000.0,
            "fecha": FECHA, "fechad": FECHA, "no_banco": 90, "stat": "B",
            "doc_banco": "0481", "fecha_crea": datetime(2026, 7, 7, 14, 30),
            "usuario_crea": "alex", "banco_emisor": "", "codigo_cli": "SJG",
            "cliente": "SAN JORGE",
        },
        {
            "id_cheque": 2, "no_cheque": "H2520", "importe": 655.93,
            "fecha": FECHA, "fechad": date(2026, 8, 7), "no_banco": 32, "stat": "Z",
            "doc_banco": None, "fecha_crea": datetime(2026, 7, 7, 15, 0),
            "usuario_crea": "alex", "banco_emisor": "PICHINCHA", "codigo_cli": "VCL",
            "cliente": "VILLA CLARA",
        },
        {
            "id_cheque": 3, "no_cheque": None, "importe": 100.0,
            "fecha": FECHA, "fechad": FECHA, "no_banco": 99, "stat": "C",
            "doc_banco": None, "fecha_crea": datetime(2026, 7, 7, 16, 0),
            "usuario_crea": "alex", "banco_emisor": "", "codigo_cli": "BED",
            "cliente": "BEDON",
        },
        {
            "id_cheque": 4, "no_cheque": None, "importe": -50.0,
            "fecha": FECHA, "fechad": FECHA, "no_banco": 98, "stat": "Z",
            "doc_banco": None, "fecha_crea": datetime(2026, 7, 7, 16, 5),
            "usuario_crea": "alex", "banco_emisor": "", "codigo_cli": "BED",
            "cliente": "BEDON",
        },
    ]


def _rows_aplic():
    return [
        # id_cheque 1 totaliza la 171899 (saldo quedó 0)
        {
            "id_cheque": 1, "aplicado": 4000.0, "tipo": None,
            "abono_f": 4000.0, "saldo_f": 0.0, "stat_f": "T",
            "numf": 171899, "numf_completo": "001-171899",
            "fact_fecha": date(2026, 5, 5), "fact_importe": 4000.0,
            "fact_saldo": 0.0,
        },
        # id_cheque 2 abona dos: una queda saldo a favor (neg), otra abonada
        {
            "id_cheque": 2, "aplicado": 300.0, "tipo": None,
            "abono_f": 3000.0, "saldo_f": -1320.07, "stat_f": "A",
            "numf": 174479, "numf_completo": "001-174479",
            "fact_fecha": date(2026, 5, 5), "fact_importe": 1679.93,
            "fact_saldo": -1320.07,
        },
        {
            "id_cheque": 2, "aplicado": 355.93, "tipo": None,
            "abono_f": 337.95, "saldo_f": 1358.07, "stat_f": "A",
            "numf": 174522, "numf_completo": "001-174522",
            "fact_fecha": date(2026, 5, 5), "fact_importe": 1696.02,
            "fact_saldo": 1358.07,
        },
    ]


def _patch_db(monkeypatch):
    """Patchea db.fetch_all SOLO para las queries del resumen; delega el
    resto (seguridad.permiso del login, etc.) al fetch_all vigente."""
    anterior = queries.db.fetch_all

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s:
            return _rows_cheques()
        if "from scintela.chequesxfact cxf" in s:
            return _rows_aplic()
        return anterior(sql, params, conn=conn)

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)


def test_resumen_cobranza_dia_buckets_y_flags(monkeypatch):
    _patch_db(monkeypatch)
    r = queries.resumen_cobranza_dia(FECHA)

    # buckets: CH = NB<90 ó 98 (cheque + espejo anticipo), DE = 90/91, EF = 99
    assert r["n_cheques"] == 2
    assert r["n_depositos"] == 1
    assert r["n_efectivo"] == 1
    assert r["total_depositos"] == 4000.0
    assert r["total_efectivo"] == 100.0
    assert r["total_cheques"] == round(655.93 - 50.0, 2)
    assert r["total_general"] == round(4000 + 100 + 655.93 - 50, 2)

    # lista plana en orden de carga
    ingresos = r["ingresos"]
    assert [i["id_cheque"] for i in ingresos] == [1, 2, 3, 4]

    por_id = {i["id_cheque"]: i for i in ingresos}
    assert por_id[1]["medio"] == "DEP.PICH."
    assert por_id[2]["medio"] == "CHEQUE"
    assert por_id[3]["medio"] == "EFECTIVO"
    assert por_id[4]["medio"] == "ANTICIPO"

    # flag paga: T si TODAS totalizadas, A si abonó, '' sin aplicaciones
    assert por_id[1]["paga"] == "T"
    assert por_id[2]["paga"] == "A"
    assert por_id[3]["paga"] == ""

    # total aplicado por ingreso
    assert por_id[2]["total_aplicado"] == round(300.0 + 355.93, 2)

    # snapshot: el saldo resultante viene de chequesxfact.saldo_f
    apps = por_id[2]["aplicaciones"]
    assert [a["saldo_f"] for a in apps] == [-1320.07, 1358.07]


def test_render_resumen_dia(client, fake_db, monkeypatch):
    """La página renderiza con medio, facturas (numf, abonado, saldo) y T/A."""
    import bcrypt

    _patch_db(monkeypatch)

    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})

    resp = client.get("/cheques/resumen-dia?fecha=2026-07-07")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "DEP.PICH." in html
    assert "EFECTIVO" in html
    assert "ANTICIPO" in html
    # facturas con numf + saldo resultante (formato EU: coma decimal)
    assert "001-171899" in html
    assert "001-174479" in html
    assert "-1.320,07" in html
    assert "0,00" in html  # saldo cero SE MUESTRA (pedido de la dueña)
    # totales
    assert "4.000,00" in html
    # badges T/A
    assert ">T</span>" in html
    assert ">A</span>" in html


def test_render_resumen_dia_vacio(client, fake_db, monkeypatch):
    import bcrypt

    anterior = queries.db.fetch_all

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela." in s:
            return []
        return anterior(sql, params, conn=conn)

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)

    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})

    resp = client.get("/cheques/resumen-dia")
    assert resp.status_code == 200
    assert "Sin cobranza registrada" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# TMT 2026-08-03 (dueña: "resumen cobranza del día está trayendo dbf imports,
# es erróneo" / "lo de KOR estaría bien, lo anterior no debería mostrar").
#
# El resumen filtraba por `cheque.fecha`. Para lo que carga PC eso está bien
# (el alta colapsa fecha := fecha_recibido), pero para lo que viene del dBase
# `fecha` es la fecha DEL CHEQUE (posdatado) y el día de ingreso es FECHING
# (ALTAS.PRG L30 `FECHING WITH DD`; MODIFICA.PRG L674 filtra el día por
# FECHING). El 03/08/2026 aparecieron 10 cheques recibidos en junio/julio
# posdatados a esa fecha.
#
# El test corre el WHERE REAL de la query contra SQLite (la expresión es
# ANSI y se evalúa igual), así que si alguien vuelve a `c.fecha = %s` falla.
# ---------------------------------------------------------------------------

def _sql_del_resumen(monkeypatch) -> str:
    """Captura el SQL de cheques que emite resumen_cobranza_dia()."""
    capturado = {}
    anterior = queries.db.fetch_all

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s:
            capturado["sql"] = sql
            return []
        if "from scintela." in s:
            return []
        return anterior(sql, params, conn=conn)

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)
    queries.resumen_cobranza_dia(FECHA)
    assert "sql" in capturado, "resumen_cobranza_dia no consultó scintela.cheque"
    return capturado["sql"]


_CHEQUES_SQLITE = [
    # (id, usuario_crea, fecha, fecha_recibido, fechaing) — fechas ISO
    # 1) dbf-import: recibido 09/06, POSDATADO al 03/08 → NO es cobranza del 03/08
    (1, "dbf-import", "2026-08-03", None, "2026-06-09"),
    # 2) dbf-import: recibido el 03/08 en el dBase → SÍ entra
    (2, "dbf-import", "2026-09-15", None, "2026-08-03"),
    # 3) PC (andres, caso KOR): fecha_recibido = 03/08 → SÍ entra
    (3, "andres", "2026-08-03", "2026-08-03", None),
    # 4) PC: cheque recibido el 03/08 pero con fechad futura → SÍ entra
    (4, "andres", "2026-08-03", "2026-08-03", "2026-08-20"),
    # 5) PC: recibido otro día → NO entra
    (5, "andres", "2026-07-30", "2026-07-30", None),
    # 6) dbf-import sin FECHING → fallback a `fecha` → entra
    (6, "dbf-import", "2026-08-03", None, None),
]


def _ids_que_devuelve(sql: str, fecha_iso: str) -> set[int]:
    """Corre el WHERE real contra SQLite y devuelve los id_cheque que pasan."""
    import re
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE cheque (id_cheque INT, no_cheque TEXT, importe REAL, "
        "fecha TEXT, fechad TEXT, no_banco INT, stat TEXT, doc_banco TEXT, "
        "concepto TEXT, "
        "fecha_crea TEXT, usuario_crea TEXT, clave TEXT, fecha_recibido TEXT, "
        "fechaing TEXT, fechaout TEXT, banco TEXT, codigo_cli TEXT)"
    )
    con.execute("CREATE TABLE cliente (codigo_cli TEXT, nombre TEXT)")
    for idc, usr, fecha, frec, fing in _CHEQUES_SQLITE:
        con.execute(
            "INSERT INTO cheque (id_cheque, importe, fecha, fechad, no_banco, "
            "stat, usuario_crea, fecha_recibido, fechaing, banco, codigo_cli) "
            "VALUES (?, 100, ?, ?, 10, 'Z', ?, ?, ?, 'PICHINCHA', 'KOR')",
            (idc, fecha, fecha, usr, frec, fing),
        )
    con.commit()

    sql_lite = sql.replace("scintela.", "").replace("%s", "?")
    # SQLite no tiene NOT IN sobre NULL-safe COALESCE distinto; el resto es ANSI.
    sql_lite = re.sub(r"\s+", " ", sql_lite)
    filas = con.execute(sql_lite, (fecha_iso,)).fetchall()
    cols = [d[0] for d in con.execute(sql_lite, (fecha_iso,)).description]
    i = cols.index("id_cheque")
    return {f[i] for f in filas}


def test_resumen_dia_no_trae_posdatados_del_dbase(monkeypatch):
    """Un cheque del dBase posdatado a HOY no es cobranza de HOY (FECHING manda)."""
    sql = _sql_del_resumen(monkeypatch)
    assert _ids_que_devuelve(sql, "2026-08-03") == {2, 3, 4, 6}


def test_resumen_dia_trae_el_dbf_import_en_su_dia_de_ingreso(monkeypatch):
    """El posdatado sí aparece el día que ENTRÓ (FECHING = 09/06)."""
    sql = _sql_del_resumen(monkeypatch)
    assert _ids_que_devuelve(sql, "2026-06-09") == {1}


# ---------------------------------------------------------------------------
# TMT 2026-08-04 (Alex: "en el caso de MTM los dos fueron abonos a factura,
# pero refleja sin aplicar facturas … esto se entrega a contabilidad y no van
# a saber qué hacer con el 'sin aplicar facturas'").
#
# Alex aplicó los dos depósitos de MTM a la factura 177617 y 16 minutos
# después corrió TOTALIZAR del estado de cuenta, que BORRA los vínculos a
# propósito. El resumen leía sólo `chequesxfact` → imprimía "sin aplicar a
# facturas" sobre un cobro que SÍ se aplicó. La dueña: "aunque se totalice la
# cuenta quiero que en cobranza quede guardado qué factura se pagó" y "me
# gustaría que me digas a qué factura fue aplicada realmente".
#
# Se testea sobre el HTML RENDERIZADO (no sobre el dict), que es lo que ve
# contabilidad.
# ---------------------------------------------------------------------------

_MTM_FECHA = date(2026, 8, 3)


def _patch_db_mtm(monkeypatch):
    anterior = queries.db.fetch_all

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s:
            return [{
                "id_cheque": 101712, "no_cheque": None, "importe": 300.0,
                "fecha": _MTM_FECHA, "fechad": _MTM_FECHA, "no_banco": 90,
                "stat": "B", "doc_banco": "116526248", "concepto": None,
                "fecha_crea": datetime(2026, 8, 3, 12, 8),
                "usuario_crea": "alex", "clave": "ALE", "banco_emisor": "",
                "codigo_cli": "MTM", "cliente": "MULTITELAS",
                "fecha_recibido": _MTM_FECHA, "fechaing": _MTM_FECHA,
            }]
        if "from scintela.chequesxfact cxf" in s:
            return []           # el TOTALIZAR los borró
        if "totalizar_estado_cuenta" in s:
            return [{"codigo_cli": "MTM", "cuando": _MTM_FECHA}]
        if "from scintela.mov_doble" in s:
            return [
                {"tipo": "cheque_creado", "origen_id": 101712,
                 "destino_id": 101712, "importe": 300.0, "batch_id": None,
                 "metadata": {"codigo_cli": "MTM"}},
                {"tipo": "cheque_aplicado_a_factura", "origen_id": 101712,
                 "destino_id": 276593, "importe": 300.0, "batch_id": None,
                 "metadata": {"numf": 177617, "saldo_factura_post": 1759.28,
                              "stat_factura_post": "A"}},
            ]
        return anterior(sql, params, conn=conn)

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)


def test_render_mtm_dice_la_factura_y_no_sin_aplicar(client, fake_db, monkeypatch):
    import bcrypt

    _patch_db_mtm(monkeypatch)
    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})

    resp = client.get("/cheques/resumen-dia?fecha=2026-08-03")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "sin aplicar a facturas" not in html
    assert "177617" in html                 # a QUÉ factura fue aplicada
    assert "TOTALIZAR" in html              # y por qué el vínculo no está
    assert "1.759,28" in html               # el saldo que quedó AL APLICAR
