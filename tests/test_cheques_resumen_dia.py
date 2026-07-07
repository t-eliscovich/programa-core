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
