"""Tests de /cheques/ingresados-dia — réplica de CHEQUING (BANCOS.PRG L429).

TMT 2026-08-03 (dueña, foto de la tirilla del FoxPro: "al parecer necesitamos
imprimir esto" → *LISTADO DE CHEQUES INGRESADOS EN FECHA: 31.07.26*).

Lo que fija el dBase y este test protege:
  · `SORT ON IMPORTE/D`  → orden por IMPORTE DESCENDENTE.
  · `LIST ALL FECHAD, …` → la fecha listada es FECHAD, no FECHA.
  · `SUM ALL IMPORTE`    → TOTAL NETO: los espejos de anticipo (NB=98) entran
                           negativos y restan (en la tirilla real, dos RTO de
                           −3.754 y −7.000 llevan el total a −374,82).
  · `FOR FECHING=FFF`    → filtra por día de INGRESO.
"""

from __future__ import annotations

from datetime import date

from modules.cheques import queries

FECHA = date(2026, 7, 31)


def _filas():
    """Calcado de la tirilla del 31/07/26 que trajo la dueña."""
    return [
        {"id_cheque": 1, "no_cheque": "1", "fechad": date(2026, 9, 15), "fecha": FECHA,
         "importe": 9379.58, "stat": "Z", "no_banco": 12, "codigo_cli": "AJO",
         "banco_emisor": "PACIFICO", "cliente": "AJO SA"},
        {"id_cheque": 2, "no_cheque": "2", "fechad": date(2026, 9, 2), "fecha": FECHA,
         "importe": 282.83, "stat": "Z", "no_banco": 32, "codigo_cli": "UXI",
         "banco_emisor": "INTERNACI", "cliente": "UXI"},
        {"id_cheque": 3, "no_cheque": "3", "fechad": date(2026, 8, 30), "fecha": FECHA,
         "importe": -3754.00, "stat": "Z", "no_banco": 98, "codigo_cli": "RTO",
         "banco_emisor": "ANTICIPO", "cliente": "RTO"},
        {"id_cheque": 4, "no_cheque": "4", "fechad": date(2026, 8, 30), "fecha": FECHA,
         "importe": -7000.00, "stat": "Z", "no_banco": 98, "codigo_cli": "RTO",
         "banco_emisor": "ANTICIPO", "cliente": "RTO"},
    ]


def _patch(monkeypatch, capturado=None):
    anterior = queries.db.fetch_all

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s and "order by c.importe desc" in s:
            if capturado is not None:
                capturado["sql"] = " ".join(sql.split())
                capturado["params"] = params
            return _filas()
        return anterior(sql, params, conn=conn)

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)


def test_total_es_neto_con_los_anticipos_negativos(monkeypatch):
    _patch(monkeypatch)
    r = queries.cheques_ingresados_dia(FECHA)
    assert r["n"] == 4
    # 9379.58 + 282.83 − 3754 − 7000 = −1091.59
    assert r["total"] == -1091.59
    assert r["fecha"] == FECHA


def test_ordena_por_importe_descendente_en_el_sql(monkeypatch):
    """El orden lo hace Postgres (SORT ON IMPORTE/D), no Python."""
    cap = {}
    _patch(monkeypatch, cap)
    queries.cheques_ingresados_dia(FECHA)
    assert "ORDER BY c.importe DESC" in cap["sql"]
    # ...y NO por fecha ni por id, que es lo que hace el resumen de cobranza.
    assert "ORDER BY c.id_cheque" not in cap["sql"]


def test_filtra_por_dia_de_ingreso_no_por_fecha_del_cheque(monkeypatch):
    """Comparte SQL_DIA_INGRESO con el resumen de cobranza — misma definición."""
    cap = {}
    _patch(monkeypatch, cap)
    queries.cheques_ingresados_dia(FECHA)
    sql = cap["sql"]
    assert " ".join(queries.SQL_DIA_INGRESO.split()) in sql
    assert "WHERE c.fecha = %(fecha)s" not in sql
    assert cap["params"]["fecha"] == FECHA
    assert cap["params"]["estados"] is None


def test_estado_filtra_por_stat(monkeypatch):
    cap = {}
    _patch(monkeypatch, cap)
    queries.cheques_ingresados_dia(FECHA, ("Z",))
    assert cap["params"]["estados"] == ["Z"]


def test_default_es_solo_en_cartera_Z(client, fake_db, monkeypatch):
    """TMT 2026-08-03 (dueña: "y solo estado Z no B").

    Este listado es el que se lleva al banco: sin ?estado sólo salen los que
    quedaron EN CARTERA. Los depositados (B/A) entran sólo con estado=todos.
    """
    import bcrypt

    cap = {}
    _patch(monkeypatch, cap)
    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})

    assert client.get("/cheques/ingresados-dia?fecha=2026-07-31").status_code == 200
    assert cap["params"]["estados"] == ["Z"]

    assert client.get(
        "/cheques/ingresados-dia?fecha=2026-07-31&estado=todos"
    ).status_code == 200
    assert cap["params"]["estados"] is None


def test_render_ingresados_dia(client, fake_db, monkeypatch):
    """La página renderiza con el N° de orden, la FECHAD y el total neto."""
    import bcrypt

    _patch(monkeypatch)
    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})

    resp = client.get("/cheques/ingresados-dia?fecha=2026-07-31&estado=todos")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "LISTADO DE CHEQUES INGRESADOS" in html
    assert "AJO" in html and "PACIFICO" in html
    # FECHAD (15/09/2026), no la fecha de ingreso (31/07/2026).
    assert "15/09/2026" in html
    # Formato EU y total neto negativo.
    assert "9.379,58" in html
    assert "-1.091,59" in html or "−1.091,59" in html


def test_render_ingresados_dia_vacio(client, fake_db, monkeypatch):
    import bcrypt

    anterior = queries.db.fetch_all

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s:
            return []
        return anterior(sql, params, conn=conn)

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)
    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})

    resp = client.get("/cheques/ingresados-dia")
    assert resp.status_code == 200
    assert "No hay cheques ingresados esa fecha" in resp.get_data(as_text=True)


def test_banco_se_resuelve_por_catalogo_no_por_el_texto(monkeypatch):
    """`cheque.banco` (texto) viene NULL en casi todo lo que carga PC.

    TMT 2026-08-03 (dueña: "banco es el banco del cheque, y estás seguro que
    esto está bien??"). La primera versión leía `c.banco` a secas y la columna
    salía vacía en 17 de 18 filas. El banco real vive en `no_banco` contra
    `scintela.banco`; el 98 se rotula ANTICIPO porque el catálogo lo tiene
    como UKN legacy.
    """
    cap = {}
    _patch(monkeypatch, cap)
    queries.cheques_ingresados_dia(FECHA)
    sql = cap["sql"]
    assert "FROM scintela.banco bco" in sql
    assert "'ANTICIPO'" in sql
