"""Tests del importador DBF → Postgres.

Garantizan que el mapping no se rompa silenciosamente. Si alguien cambia
una columna del DBF o de Postgres y olvida actualizar el script, el test
falla en CI.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_table_map_no_tiene_duplicados():
    """Cada DBF debe mapear a UNA tabla. La regla "1 tabla = 1 DBF" se
    relajó en 2026-05-06: scintela.transacciones_bancarias recibe múltiples
    DBFs (PICHINCH.DBF + INTER.DBF, uno por banco), separados por la
    estrategia `delete_where` en lugar de TRUNCATE total. Para evitar
    pisarse, cualquier entry que comparte pg_table DEBE definir
    `delete_where` con un filtro que limpie sólo sus propias filas.
    """
    from scripts.import_dbf import TABLE_MAP

    dbfs = list(TABLE_MAP.keys())
    assert len(dbfs) == len(set(dbfs)), "DBF duplicado en TABLE_MAP"

    # Tablas que aparecen >1 vez son legales sólo si TODAS sus entries
    # tienen `delete_where` (TRUNCATE total las pisaría entre sí).
    from collections import defaultdict
    por_tabla: dict[str, list[str]] = defaultdict(list)
    for dbf, cfg in TABLE_MAP.items():
        por_tabla[cfg["pg_table"]].append(dbf)

    for tabla, dbfs_de_tabla in por_tabla.items():
        if len(dbfs_de_tabla) <= 1:
            continue
        for dbf_name in dbfs_de_tabla:
            cfg = TABLE_MAP[dbf_name]
            assert cfg.get("delete_where"), (
                f"DBF {dbf_name!r} comparte pg_table {tabla!r} con otros "
                f"({dbfs_de_tabla}) pero no define `delete_where` — "
                "se pisarían entre sí en cada sync."
            )


def test_cada_entry_tiene_keys_minimas():
    """Cada entry de TABLE_MAP debe tener pg_table, mapper, criticidad, descripcion."""
    from scripts.import_dbf import TABLE_MAP

    requeridas = {"pg_table", "mapper", "criticidad", "descripcion"}
    for dbf, cfg in TABLE_MAP.items():
        faltantes = requeridas - set(cfg.keys())
        assert not faltantes, (
            f"Entry '{dbf}' no tiene las keys requeridas: faltan {faltantes}"
        )


def test_mappers_aceptan_dict_vacio_sin_explotar():
    """Los mappers deben tolerar inputs sin todas las columnas (DBF legacy
    pueden tener menos cols). No deben raisear, devuelven Nones."""
    from scripts.import_dbf import TABLE_MAP

    for dbf, cfg in TABLE_MAP.items():
        try:
            out = cfg["mapper"]({})
        except Exception as e:
            pytest.fail(f"Mapper '{dbf}' explotó con dict vacío: {e}")
        assert isinstance(out, dict), f"Mapper '{dbf}' no devuelve dict"
        assert "usuario_crea" in out, (
            f"Mapper '{dbf}' no incluye 'usuario_crea' (audit column requerida)"
        )
        assert out["usuario_crea"] == "dbf-import", (
            f"Mapper '{dbf}' tiene usuario_crea={out['usuario_crea']}, "
            "debe ser 'dbf-import' para auditar la fuente"
        )


def test_factura_mapper_caso_real():
    """Un caso real: factura con todos sus campos."""
    from scripts.import_dbf import _map_factura

    out = _map_factura({
        "NUMF": 12345,
        "FECHA": date(2026, 4, 30),
        "CLIENTE": "JTX",
        "KG": 1500.5,
        "IMPORTE": 12750.00,
        "ABONO": 5000.0,
        "SALDO": 7750.0,
        "STAT": "Z",
        "CONDIC": " ",
        "VENCIM": date(2026, 5, 30),
        "TIPO": "C",
        "CLAVE": "T",
        "PASE": "FAB",
    })

    assert out["numf"] == 12345
    assert out["fecha"] == date(2026, 4, 30)
    assert out["codigo_cli"] == "JTX"
    assert out["kg"] == 1500.5
    assert out["importe"] == 12750.0
    assert out["saldo"] == 7750.0
    assert out["stat"] == "Z"
    assert out["vencimiento"] == date(2026, 5, 30)
    assert out["pase"] == "FAB"
    assert out["usuario_crea"] == "dbf-import"


def test_pichincha_mapper_setea_no_banco_via_post_load():
    """PICHINCH map devuelve no_banco=None — se completa en post-load."""
    from scripts.import_dbf import _map_pichincha_trans

    out = _map_pichincha_trans({
        "FECHA": date(2026, 4, 30),
        "DOC": "DE",
        "CONCEPTO": "Depósito ch.MWT",
        "FECHAD": date(2026, 4, 30),
        "IMPORTE": 6000.0,
        "SALDO": 2267688.14,
        "STAT": "",
    })
    assert out["no_banco"] is None, (
        "El mapper de PICHINCH debe dejar no_banco=None — se asigna en post-load."
    )
    assert out["importe"] == 6000.0
    assert out["saldo"] == 2267688.14
    assert out["documento"] == "DE"


def test_iniciales_traduce_mes_a_num():
    """INICIALE.DBF tiene MES como 'ABR' — el mapper debe traducir a mesnum=4."""
    from scripts.import_dbf import _map_iniciales

    out = _map_iniciales({
        "MES": "ABR",
        "YY": 2026,
        "KPROG": 290000,
        "PRETOT": 1000000,
    })
    assert out["mesnum"] == 4
    assert out["mesnom"] == "ABR"
    assert out["yy"] == 2026
    assert out["kprog"] == 290000


def test_dolares_mapper_acepta_st_con_distintos_nombres():
    """En el DBF la columna 'ST' tiene un espacio raro — dbfread la expone
    como ST, ST_T o 'ST T' según versión. El mapper acepta cualquiera."""
    from scripts.import_dbf import _map_dolares

    for variante in ("ST", "ST_T", "ST T"):
        out = _map_dolares({
            "FECHA": date(2026, 4, 30),
            "CTA": "MA",
            "IMPORTE": 1000.0,
            variante: "X",
        })
        assert out["st"] == "X", (
            f"Mapper no leyó la columna ST cuando se llamaba {variante!r}"
        )


def test_helpers_de_coercion_son_robustos():
    """_str, _date, _num, _int aceptan basura sin explotar."""
    from scripts.import_dbf import _date, _int, _num, _str

    # _str
    assert _str(None) is None
    assert _str("") is None
    assert _str("  ") is None
    assert _str("hola") == "hola"
    assert _str("hola mundo", max_len=4) == "hola"

    # _num
    assert _num(None) is None
    assert _num("") is None
    assert _num("abc") is None
    assert _num(3.14) == 3.14
    assert _num("3.14") == 3.14
    assert _num(None, default=0) == 0

    # _int
    assert _int(None) is None
    assert _int("3") == 3
    assert _int("3.7") == 3   # truncates
    assert _int("abc") is None
    assert _int("abc", default=0) == 0

    # _date
    assert _date(None) is None
    assert _date("2026-04-30") is None   # no parsea strings — solo passes through
    assert _date(date(2026, 4, 30)) == date(2026, 4, 30)


def test_table_map_tiene_los_dbfs_criticos_para_balance():
    """Los 3 SUPER y los 8 CRITICOS para el balance deben estar mapeados.

    RETIROS.DBF + TINTO.DBF se agregaron en batch 9 (2026-05-06) — sin
    ellos, los retiros de mayo (dividendos) no llegan a Postgres y
    COL.QUI./KR del panel Resultados quedan en 0 aunque el dBase tenga
    data fresca. Los dos eran el reporte del gerente "no veo nada de mayo"
    y "materia prima/colorantes en cero".
    """
    from scripts.import_dbf import TABLE_MAP

    super_criticos = {"PICHINCH.DBF", "HISTORIA.DBF", "POSDAT.DBF"}
    criticos = {
        "ACTIVOS.DBF", "CHEQUES.DBF", "FACTURAS.DBF",
        "CAJA.DBF", "DOLARES.DBF", "INICIALE.DBF",
        "RETIROS.DBF", "TINTO.DBF",
    }
    todos = super_criticos | criticos
    presentes = set(TABLE_MAP.keys())
    faltantes = todos - presentes
    assert not faltantes, (
        f"Faltan DBFs críticos en TABLE_MAP: {faltantes}. Sin estos, "
        "el balance no refleja los números reales del dBase."
    )


def test_tinto_mapper_caso_real():
    """Caso real de TINTO.DBF (May 2026): batch JAS MEDIO con todos los
    campos numéricos por tipo de fabric (la mayoría None) + KG/IMPORTE.

    El bug que motivó este mapper fue COL.QUI. = 0 en el panel Resultados
    aunque TINTO.DBF tenía 82 registros frescos para mayo.
    """
    from scripts.import_dbf import _map_tinto

    out = _map_tinto({
        "FECHA": date(2026, 5, 6),
        "COD": "JME",
        "COLOR": "JAS MEDIO",
        "FRANELA": None, "MESSI": None, "JAMES": None,
        "JERSEY": None, "J3": None, "TOPER": None,
        "JLYC": None, "PIQUE": None, "FLYC": None,
        "FALSO": None, "OTROS": None, "KIANA": None,
        "TIPO": "",
        "KG": 517.0, "KGN": 496.0, "IMPORTE": 81,
        "STAT": "Z", "CLAVE": "A",
    })
    assert out["fecha"] == date(2026, 5, 6)
    assert out["cod"] == "JME"
    assert out["color"] == "JAS MEDIO"
    assert float(out["kg"]) == 517.0
    assert float(out["kgn"]) == 496.0
    assert float(out["importe"]) == 81.0
    assert out["stat"] == "Z"
    # tipo='' en DBF → None en Postgres (la columna es integer).
    assert out["tipo"] is None
    # Todos los kg-por-fabric None pasan limpios.
    assert out["franela"] is None
    assert out["jersey"] is None
    assert out["usuario_crea"] == "dbf-import"


def test_retiros_mapper_caso_real():
    """Caso real de RETIROS.DBF: registro de mayo 2026 con todos los
    campos. El bug que motivó este mapper (TMT 2026-05-06) era que
    RETIROS.DBF no estaba en TABLE_MAP, así que las filas de mayo
    nunca llegaban a Postgres aunque la copia del DBF estuviera fresca.
    """
    from scripts.import_dbf import _map_retiros

    out = _map_retiros({
        "FECHA": date(2026, 5, 6),
        "NB": 0,
        "RET": 39268.0,
        "DE": "OP",
        "CONCEPTO": "RR OP AC B.1",
        "CLAVE": "F",
    })
    assert out["fecha"] == date(2026, 5, 6)
    assert out["nb"] == 0
    assert float(out["ret"]) == 39268.0
    assert out["de"] == "OP"
    assert out["concepto"] == "RR OP AC B.1"
    assert out["clave"] == "F"
    assert out["usuario_crea"] == "dbf-import"


def test_retiros_mapper_acepta_nb_none():
    """En RETIROS.DBF, NB puede venir None (la mayoría de filas históricas).
    No tiene que romper — `nb` Postgres es nullable.
    """
    from scripts.import_dbf import _map_retiros

    out = _map_retiros({
        "FECHA": date(2026, 5, 6),
        "NB": None,
        "RET": 100.29,
        "DE": "FE",
        "CONCEPTO": "RR FERNANDA",
        "CLAVE": "",
    })
    assert out["nb"] is None
    assert out["clave"] is None  # _str de "" devuelve None


def test_lookup_no_banco_pichincha_default_es_1():
    """Cuando no hay banco con 'PICHINCHA' en su nombre, _lookup devuelve 1
    (convención del PRG legacy)."""
    import scripts.import_dbf as importer

    # Patch db.fetch_one para devolver None (no encontrado)
    original = importer.db.fetch_one
    importer.db.fetch_one = lambda *a, **k: None
    try:
        assert importer._lookup_no_banco_pichincha() == 1
    finally:
        importer.db.fetch_one = original
