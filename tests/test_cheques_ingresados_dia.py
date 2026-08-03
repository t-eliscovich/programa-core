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


def test_el_corte_es_por_MEDIO_no_por_estado(monkeypatch):
    """TMT 2026-08-03, tres pasos con la dueña mirando la pantalla.

    "y solo estado Z no B" → "es siempre Z, no hay que seleccionar" → y al
    final, señalando el cheque de KOR depositado ese mismo día, **"este
    falta"**. Lo que sobraba en el listado no eran los depositados: eran los
    DEP.PICH. (NB=90), que no son cheques. Filtrar por `stat` tapaba eso y de
    paso se comía un cheque legítimo.

    El listado NO filtra por estado; filtra por MEDIO, con la misma expresión
    que parte los buckets del resumen de cobranza.
    """
    cap = {}
    _patch(monkeypatch, cap)
    queries.cheques_ingresados_dia(FECHA)
    sql = cap["sql"]
    assert queries.SQL_ES_CHEQUE in sql
    assert "c.stat = ANY" not in sql
    assert "estados" not in (cap["params"] or {})


def test_el_conteo_coincide_con_el_bucket_CHEQUES_del_resumen(monkeypatch):
    """La dueña cruza las dos pantallas: "¿cómo puede haber 15 ingresos y acá
    16 cheques?? hay algo mal". Tenía razón — y esta es la barrera para que no
    vuelva a pasar: las dos parten el universo del día con la MISMA regla, así
    que sobre las mismas filas tienen que dar el mismo número."""
    filas = _filas() + [
        # Un DEP.PICH. y un efectivo del mismo día: NO son cheques, y ninguna
        # de las dos pantallas los cuenta como tales.
        {"id_cheque": 9, "no_cheque": None, "fechad": FECHA, "fecha": FECHA,
         "importe": 300.0, "stat": "B", "no_banco": 90, "codigo_cli": "MTM",
         "banco_emisor": "DEP.PICH.", "cliente": "MTM", "doc_banco": "1",
         "fecha_crea": None, "usuario_crea": "alex", "clave": "ALE",
         "fecha_recibido": FECHA, "fechaing": None},
        {"id_cheque": 10, "no_cheque": None, "fechad": FECHA, "fecha": FECHA,
         "importe": 50.0, "stat": "C", "no_banco": 99, "codigo_cli": "BED",
         "banco_emisor": "EFECTIVO", "cliente": "BED", "doc_banco": None,
         "fecha_crea": None, "usuario_crea": "alex", "clave": "ALE",
         "fecha_recibido": FECHA, "fechaing": None},
    ]

    def es_cheque(f):
        return (f.get("no_banco") or 0) not in (90, 91, 99)

    anterior = queries.db.fetch_all

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s and "order by c.importe desc" in s:
            return [f for f in filas if es_cheque(f)]   # lo hace el WHERE en Postgres
        if "from scintela.cheque c" in s:
            return filas
        if "from scintela.chequesxfact cxf" in s:
            return []
        return anterior(sql, params, conn=conn)

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)

    listado = queries.cheques_ingresados_dia(FECHA)
    resumen = queries.resumen_cobranza_dia(FECHA)
    assert listado["n"] == resumen["n_cheques"] == 4
    assert round(listado["total"], 2) == round(resumen["total_cheques"], 2)
    # ...y el depósito y el efectivo siguen contándose en SU bucket.
    assert resumen["n_depositos"] == 1 and resumen["n_efectivo"] == 1


def test_render_ingresados_dia(client, fake_db, monkeypatch):
    """La página renderiza con el N° de orden, la FECHAD y el total neto."""
    import bcrypt

    _patch(monkeypatch)
    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})

    resp = client.get("/cheques/ingresados-dia?fecha=2026-07-31")
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


def test_las_dos_pantallas_comparten_la_barra_de_filtro():
    """Los botones Ver / Imprimir viven en UN solo lugar.

    TMT 2026-08-03 (dueña: "los botones te quedaron distintos"). Cada pantalla
    tenía su copia de la barra y en tres pasadas se separaron: distinto ancho,
    distinto padding, Imprimir verde en una y outline en la otra. Si dos cosas
    tienen que verse iguales, comparten el código — un "espeja a la otra" en un
    comentario no se mantiene solo.
    """
    from pathlib import Path

    tpl = Path(__file__).resolve().parent.parent / "modules/cheques/templates/cheques"
    partial = (tpl / "_tabs_informes_dia.html").read_text(encoding="utf-8")
    assert ">Ver</button>" in partial and ">Imprimir</button>" in partial

    for nombre in ("resumen_dia.html", "ingresados_dia.html"):
        html = (tpl / nombre).read_text(encoding="utf-8")
        assert '{% include "cheques/_tabs_informes_dia.html" %}' in html, nombre
        assert ">Ver</button>" not in html, f"{nombre} volvió a tener su propia barra"
        assert ">Imprimir</button>" not in html, f"{nombre} volvió a tener su propio botón"
