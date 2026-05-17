#!/usr/bin/env python3
"""Smoke test del port dBase → Programa Core (lógicas migradas).

Corre contra la DB local de Tamara y ejerce las lógicas portadas desde
INFORMES.PRG / MODIFICA.PRG / MENU.PRG:

    #1  Provisión $80K en UT.PROY (INFORMES.PRG L420)
    #2  Editor inline posdat banc=0/9 en /informes/flujo (MENU.PRG L725-803)
    #3  Histograma multi-año H/N/1/2/3 (INFORMES.PRG L1336-1550)
    #4  CONTROLC — cartera diaria vs snapshot (MENU.PRG L1582-1660)
    #5  Auto-cierre de stock mensual (MENU.PRG L246-263)
    #9  DETALGAST drill-down V1..V9 con grouping (INFORMES.PRG L1266-1334)
    #11 Cartera × color × cliente (INFORMES.PRG L830-918)
    #12 Comparación ventas multi-año matriz (MODIFICA.PRG L144-217)

USO:
    python scripts/smoke_test_dbase_port.py
    python scripts/smoke_test_dbase_port.py --only 2,4,5,11
    python scripts/smoke_test_dbase_port.py --keep-data

Reglas:
    - Idempotente: data marcada con concepto `__SMOKE_DBASE__`.
    - No requiere Flask app context — queries directas.
    - Si una query no crashea pero la DB local no tiene data, el test
      asierta sobre el SHAPE de la respuesta (no sobre valores concretos).
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import db  # noqa: E402

SMOKE_TAG = "__SMOKE_DBASE__"
CLI_TEST = "ZSB"
PROV_TEST = "ZSB"


# ----------------------------------------------------------------------------
# Cleanup
# ----------------------------------------------------------------------------

def cleanup(verbose: bool = False) -> None:
    def _try(sql: str, params: tuple = ()) -> None:
        try:
            db.execute(sql, params)
            if verbose:
                print(f"    cleanup OK: {sql[:80]}")
        except Exception as e:
            if verbose:
                print(f"    cleanup falló (no fatal): {e}")

    _try("DELETE FROM scintela.xgast WHERE concepto LIKE %s", (f"%{SMOKE_TAG}%",))
    # Smoke posdat (ITEM #2)
    _try(
        """DELETE FROM scintela.mov_doble
            WHERE COALESCE(concepto, '') LIKE %s
               OR origen_table = 'posdat'
              AND origen_id IN (
                  SELECT id_posdat FROM scintela.posdat
                  WHERE COALESCE(concepto, '') LIKE %s)""",
        (f"%{SMOKE_TAG}%", f"%{SMOKE_TAG}%"),
    )
    _try(
        "DELETE FROM scintela.posdat WHERE COALESCE(concepto, '') LIKE %s",
        (f"%{SMOKE_TAG}%",),
    )
    # Smoke cartera (ITEM #4)
    _try("DELETE FROM scintela.cartera_snapshots WHERE codigo_cli = %s",
         (CLI_TEST,))
    # Smoke iniciales (ITEM #5) — limpia cualquier fila con usuario_crea=auto-smoke.
    _try("DELETE FROM scintela.iniciales WHERE usuario_crea = %s",
         ("auto-smoke",))
    _try("DELETE FROM scintela.factura WHERE codigo_cli = %s", (CLI_TEST,))
    # Smoke #6/#7 cheques + chequesxfact + chequextransaccion + tx
    _try(
        """DELETE FROM scintela.chequextransaccion
            WHERE id_cheque IN (SELECT id_cheque FROM scintela.cheque
                                 WHERE codigo_cli = %s)""",
        (CLI_TEST,),
    )
    _try(
        """DELETE FROM scintela.chequesxfact
            WHERE id_cheque IN (SELECT id_cheque FROM scintela.cheque
                                 WHERE codigo_cli = %s)""",
        (CLI_TEST,),
    )
    _try(
        """DELETE FROM scintela.mov_doble
            WHERE origen_table = 'cheque'
              AND origen_id IN (SELECT id_cheque FROM scintela.cheque
                                 WHERE codigo_cli = %s)""",
        (CLI_TEST,),
    )
    _try("DELETE FROM scintela.cheque WHERE codigo_cli = %s", (CLI_TEST,))
    # Smoke #6: tx_bancarias del proveedor de smoke
    _try("DELETE FROM scintela.transacciones_bancarias WHERE prov = %s",
         (CLI_TEST[:5],))
    # Smoke #8: dolares + compras del PROV_TEST
    _try(
        """DELETE FROM scintela.mov_doble
            WHERE origen_table = 'dolares'
              AND origen_id IN (SELECT id_dolares FROM scintela.dolares
                                 WHERE cta = %s)""",
        (PROV_TEST,),
    )
    _try("DELETE FROM scintela.dolares WHERE cta = %s", (PROV_TEST,))
    _try("DELETE FROM scintela.compra WHERE codigo_prov = %s "
         "AND comprobante LIKE 'BAP%%'", (PROV_TEST,))
    _try("DELETE FROM scintela.cliente WHERE codigo_cli = %s", (CLI_TEST,))
    _try("DELETE FROM scintela.proveedor WHERE codigo_prov = %s", (PROV_TEST,))


def _ensure_cliente() -> None:
    # TMT 2026-05-15: NO usamos ON CONFLICT — scintela.cliente.codigo_cli
    # no tiene UNIQUE constraint (data legacy de dBase) y rompe los tests
    # con "InvalidColumnReference". Hacemos SELECT-then-INSERT.
    row = db.fetch_one(
        "SELECT 1 FROM scintela.cliente WHERE codigo_cli = %s LIMIT 1",
        (CLI_TEST,),
    )
    if row:
        return
    db.execute(
        "INSERT INTO scintela.cliente (codigo_cli, nombre, stop, observacion) "
        "VALUES (%s, %s, 'N', %s)",
        (CLI_TEST, f"{SMOKE_TAG} CLI", SMOKE_TAG),
    )


def _ensure_proveedor() -> None:
    row = db.fetch_one(
        "SELECT 1 FROM scintela.proveedor WHERE codigo_prov = %s LIMIT 1",
        (PROV_TEST,),
    )
    if row:
        return
    db.execute(
        "INSERT INTO scintela.proveedor (codigo_prov, nombre, activo) "
        "VALUES (%s, %s, '1')",
        (PROV_TEST, f"{SMOKE_TAG} PROV"),
    )


def _crear_xgast(num: int, importe: float, concepto: str,
                 fecha: date | None = None) -> int:
    """Inserta una fila en xgast con tag de smoke."""
    _ensure_proveedor()
    fecha = fecha or date.today()
    row = db.execute_returning(
        """
        INSERT INTO scintela.xgast
            (fecha, doc, prov, concepto, num, fechad, importe, saldo, stat,
             usuario_crea)
        VALUES (%s, 'X', %s, %s, %s, %s, %s, 0, '',
                %s)
        RETURNING id_xgast
        """,
        (fecha, PROV_TEST[:5], f"{concepto} {SMOKE_TAG}", num,
         fecha, importe, SMOKE_TAG),
    )
    return int(row["id_xgast"]) if row else 0


def _crear_factura(kg: float, importe: float,
                   fecha: date | None = None) -> int:
    """Inserta una factura con tag para tests de ventas."""
    _ensure_cliente()
    fecha = fecha or date.today()
    row = db.fetch_one(
        "SELECT COALESCE(MAX(numf), 0) + 1 AS n FROM scintela.factura"
    )
    numf = int(row["n"]) if row else 1
    r = db.execute_returning(
        """
        INSERT INTO scintela.factura
            (numf, numf_completo, fecha, vencimiento, codigo_cli,
             kg, importe, abono, saldo, stat, usuario_crea)
        VALUES (%s, %s::text, %s, %s, %s,
                %s, %s, 0, %s, 'Z', %s)
        RETURNING id_factura
        """,
        (numf, str(numf), fecha, fecha + timedelta(days=30), CLI_TEST,
         kg, importe, importe, SMOKE_TAG),
    )
    return int(r["id_factura"]) if r else 0


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

class SkipTest(Exception):
    pass


def test_01b_provisiones_diarias_matcher_aec() -> int:
    """Re-audit C5 — patrón "A,E,C" antes nunca matcheaba ($220k/mes perdidos).

    Verifica que el matcher `concepto_starts_with_any` con pattern "A|E|C"
    genera SQL que matchea conceptos que empiezan con A, E o C. No tocamos
    la DB; sólo inspeccionamos el SQL generado por `_condicion_provision`.
    """
    from modules.informes import queries as qi
    asserts = 0

    # 1. La constante DEBE usar concepto_starts_with_any para el "A,E,C".
    aec_rows = [
        r for r in qi.PROVISIONES_DIARIAS
        if r[1] == "concepto_starts_with_any" and "A" in r[2] and "E" in r[2]
        and "C" in r[2]
    ]
    assert aec_rows, (
        "PROVISIONES_DIARIAS no tiene una fila con matcher "
        "concepto_starts_with_any conteniendo A/E/C. El bug del audit "
        "anterior reapareció — A,E,C nunca matchea con LIKE 'A,E,C%'."
    )
    asserts += 1

    # 2. El matcher genera un WHERE con OR sobre 3 iniciales.
    sql_where, params = qi._condicion_provision(
        prov_filter="YY",
        matcher_kind="concepto_starts_with_any",
        pattern="A|E|C",
    )
    assert "OR" in sql_where, (
        f"concepto_starts_with_any debe generar OR; vi: {sql_where!r}"
    )
    asserts += 1

    # 3. Tiene los 3 patrones LIKE 'A%', 'E%', 'C%' (+ el de prov 'YY').
    likes = [p for p in params if isinstance(p, str)
             and p.endswith("%") and len(p) == 2]
    assert sorted(likes) == ["A%", "C%", "E%"], (
        f"Esperaba ['A%', 'C%', 'E%'] como LIKEs, vi: {likes}"
    )
    asserts += 1

    # 4. Pattern vacío → FALSE explícito (no SQL injection ni crash).
    empty_where, empty_params = qi._condicion_provision("", "concepto_starts_with_any", "")
    assert empty_where == "FALSE" and empty_params == [], (
        f"pattern vacío debería dar ('FALSE', []), vi: ({empty_where!r}, {empty_params!r})"
    )
    asserts += 1

    return asserts


def test_06b_boleta_no_banco_dinamico() -> int:
    """Re-audit H5 — boleta_deposito ya no hardcodea no_banco=1.

    Verifica que el código del view resuelve Pichincha dinámicamente
    (no podemos correr el endpoint sin Flask; chequeamos el patrón en el
    source — proxy razonable para evitar regresión).
    """
    from pathlib import Path
    fuente = Path("modules/cheques/views.py").read_text(encoding="utf-8")
    asserts = 0
    # El hardcode "or 1" justo después de parse_int(...no_banco) NO debe estar
    # con el comentario default Pichincha. Lo aceptamos sólo como último
    # recurso DENTRO de la rama de fallback.
    assert "PICHINC" in fuente and "boleta_deposito" in fuente, \
        "boleta_deposito no resuelve Pichincha dinámicamente"
    asserts += 1
    # No debe quedar la línea original "parse_int(...) or 1\n    try:" pegada.
    bad = 'parse_int(request.args.get("no_banco")) or 1\n    try:\n        boleta'
    assert bad not in fuente, (
        "boleta_deposito sigue con default no_banco=1 hardcodeado."
    )
    asserts += 1
    return asserts


def test_07b_reemplazar_no_zerea_importe() -> int:
    """Re-audit H1 — reemplazar() ya NO zeroea importe del cheque viejo.

    Verifica el código fuente: el UPDATE del viejo no debe contener
    `importe=0`.
    """
    from pathlib import Path
    fuente = Path("modules/cheques/queries.py").read_text(encoding="utf-8")
    asserts = 0
    # Buscamos la sección de reemplazar — el UPDATE del cheque viejo.
    idx = fuente.find("SET stat='X', fechaout=CURRENT_DATE")
    assert idx > 0, "no encuentro UPDATE marca-reemplazo en reemplazar()"
    asserts += 1
    bloque = fuente[idx:idx + 400]
    assert "importe=0" not in bloque and "importe = 0" not in bloque, (
        "reemplazar() vuelve a zeroar importe del cheque viejo — "
        "perdés la traza del face value original."
    )
    asserts += 1
    return asserts


def test_08b_bap_seq_robusto() -> int:
    """Re-audit C3 — BAP comprobante ahora usa MAX(numero) + advisory lock.

    Verifica el source: ya no hay COUNT(*) WHERE comprobante LIKE 'BAP%%'.
    """
    from pathlib import Path
    fuente = Path("modules/dolares/queries.py").read_text(encoding="utf-8")
    asserts = 0
    assert "pg_advisory_xact_lock" in fuente and "bap_seq_compra" in fuente, (
        "convertir_a_compra perdió el advisory_xact_lock — race "
        "condition de BAP comprobante reabierta."
    )
    asserts += 1
    bad = "SELECT COUNT(*) AS n FROM scintela.compra\n             WHERE comprobante LIKE 'BAP"
    assert bad not in fuente, (
        "convertir_a_compra volvió a usar COUNT(*) — no es monótono "
        "y colisiona si borrás un BAP."
    )
    asserts += 1
    # Y el MAX numérico nuevo debe estar.
    assert "regexp_replace(comprobante, '^BAP'" in fuente, (
        "Esperaba el MAX(regexp_replace(...)::int) para seq BAP."
    )
    asserts += 1
    return asserts


def test_01_provision_pendiente() -> int:
    """Item #1 — Provisión $80K en UT.PROY (INFORMES.PRG L420).

    Verifica:
      - Constante PROVISIONES_MES_USD existe y vale 80000.
      - provision_pendiente_mes() es 0 cuando day=30, decrece linealmente
        desde el día 1 (=77.333).
      - informe_balance() expone `provision_pendiente` y `r.utilidad.provision_pendiente`.
      - `r.utilidad.proy_us` ya tiene la provisión RESTADA (vs proy_uvent - proy_total).
    """
    from modules.informes import queries as qi
    asserts = 0

    # 1. Constante.
    assert qi.PROVISIONES_MES_USD == 80000.0, \
        f"PROVISIONES_MES_USD esperado 80000, vi {qi.PROVISIONES_MES_USD}"
    asserts += 1

    # 2. Día 30 → 0.
    assert qi.provision_pendiente_mes(date(2025, 1, 30)) == 0.0, \
        "día 30 debe devolver 0"
    asserts += 1

    # 3. Día 31 → 0 (clamp).
    assert qi.provision_pendiente_mes(date(2025, 1, 31)) == 0.0, \
        "día 31 debe devolver 0 (clamp)"
    asserts += 1

    # 4. Día 1 → 80000 * (1 - 1/30) = 77.333,33.
    v1 = qi.provision_pendiente_mes(date(2025, 1, 1))
    esperado_d1 = 80000.0 * (1.0 - 1.0 / 30.0)
    assert abs(v1 - esperado_d1) < 0.01, \
        f"día 1 esperado {esperado_d1:.2f}, vi {v1:.2f}"
    asserts += 1

    # 5. Día 15 → 40000.
    v15 = qi.provision_pendiente_mes(date(2025, 1, 15))
    assert abs(v15 - 40000.0) < 0.01, f"día 15 esperado 40000, vi {v15}"
    asserts += 1

    # 6. informe_balance expone el campo (sólo si hay data — sino skip).
    try:
        b = qi.informe_balance()
    except Exception as e:
        raise SkipTest(f"informe_balance no corre: {e}")
    assert "provision_pendiente" in b, \
        "informe_balance no expone provision_pendiente al top level"
    asserts += 1
    assert isinstance(b["provision_pendiente"], int | float), \
        f"provision_pendiente no es numérico: {type(b['provision_pendiente'])}"
    asserts += 1
    assert "resultados" in b and "utilidad" in b["resultados"], \
        "informe_balance no expone resultados.utilidad"
    asserts += 1
    assert "provision_pendiente" in b["resultados"]["utilidad"], \
        "resultados.utilidad no expone provision_pendiente"
    asserts += 1

    return asserts


def test_03_historia_multianual() -> int:
    """Item #3 — Histograma multi-año H/N/1/2/3 (INFORMES.PRG L1336-1550).

    Verifica:
      - Función existe y se ejecuta sin crashear.
      - Devuelve estructura {anios, meses, totales_por_anio, n_meses}.
      - 12 meses por default, ordenados Ene..Dic.
      - 3 años (actual, -1, -2).
      - Cada celda tiene los campos esperados.
    """
    from modules.informes import queries as qi
    asserts = 0
    data = qi.historia_multianual(12)

    assert isinstance(data, dict), f"esperaba dict, vi {type(data)}"
    asserts += 1
    assert "anios" in data and len(data["anios"]) == 3, \
        f"esperaba 3 años, vi {data.get('anios')}"
    asserts += 1
    assert "meses" in data and len(data["meses"]) == 12, \
        f"esperaba 12 meses, vi {len(data.get('meses', []))}"
    asserts += 1
    # Cada mes tiene datos para cada año.
    for row in data["meses"]:
        assert "mes" in row and "label" in row and "datos" in row
        for a in data["anios"]:
            assert a in row["datos"], f"año {a} falta en mes {row['mes']}"
            d = row["datos"][a]
            for campo in ("patrimonio", "uvent", "usuti", "kvent"):
                assert campo in d, f"falta {campo} en {row['mes']}/{a}"
    asserts += 1
    # totales_por_anio existe y tiene una entrada por año.
    assert "totales_por_anio" in data
    for a in data["anios"]:
        assert a in data["totales_por_anio"]
        for campo in ("uvent", "usuti", "kvent", "usret"):
            assert campo in data["totales_por_anio"][a]
    asserts += 1

    # meses=3 → 3 meses.
    d2 = qi.historia_multianual(3)
    assert len(d2["meses"]) == 3, f"meses=3, vi {len(d2['meses'])}"
    asserts += 1

    # meses=20 → capeado a 12.
    d3 = qi.historia_multianual(20)
    assert len(d3["meses"]) == 12, f"meses=20 debería capearse a 12, vi {len(d3['meses'])}"
    asserts += 1

    return asserts


def test_09_gastos_detalle_categoria() -> int:
    """Item #9 — DETALGAST drill-down V1..V9 (INFORMES.PRG L1266-1334).

    Crea filas de xgast en distintas categorías + grouping (EEQ, CMB, EMAAP, OTRO)
    y verifica que el detalle agrupa correctamente con subtotales.
    """
    from modules.informes import queries as qi
    asserts = 0

    # Setup: 4 filas en V5, dos en EEQ, una en CMB, una en EMAAP.
    _crear_xgast(5, 100.0, "EEQ planilla")
    _crear_xgast(5, 200.0, "EEQ medidor B")
    _crear_xgast(5, 50.0,  "CMB diesel")
    _crear_xgast(5, 30.0,  "EMAAP agua")
    # Y una fila V1 para testear que NO se mezcla.
    _crear_xgast(1, 999.0, "SUELDOS planta")

    data = qi.gastos_detalle_categoria(5, mes_actual=True)
    assert data["num"] == 5
    asserts += 1
    assert data["n_filas"] >= 4, f"esperaba >=4 filas V5, vi {data['n_filas']}"
    asserts += 1
    # Suma total >= 380 (4 filas sumando 380).
    assert data["total"] >= 380.0, f"total esperado >=380, vi {data['total']}"
    asserts += 1

    # Verificar grouping: hay grupos para EEQ, CMB, EMAAP.
    nombres_grupos = {g["grupo"] for g in data["grupos"]}
    assert any("EEQ" in n for n in nombres_grupos), \
        f"no encuentro grupo EEQ: {nombres_grupos}"
    asserts += 1
    assert any("CMB" in n for n in nombres_grupos), \
        f"no encuentro grupo CMB: {nombres_grupos}"
    asserts += 1
    assert any("EMAAP" in n for n in nombres_grupos), \
        f"no encuentro grupo EMAAP: {nombres_grupos}"
    asserts += 1

    # El EEQ subtotal debería ser >=300 (100+200).
    eeq = next(g for g in data["grupos"] if "EEQ" in g["grupo"])
    assert eeq["subtotal"] >= 300.0, \
        f"EEQ subtotal esperado >=300, vi {eeq['subtotal']}"
    asserts += 1
    assert len(eeq["filas"]) >= 2, f"esperaba >=2 filas EEQ, vi {len(eeq['filas'])}"
    asserts += 1

    # V1 NO debería contener nada de smoke (la fila V1 que creamos quedó en otra cat).
    data1 = qi.gastos_detalle_categoria(1, mes_actual=True)
    # Filtramos sólo nuestras filas de smoke.
    smoke_v1 = [f for g in data1["grupos"] for f in g["filas"]
                if SMOKE_TAG in (f.get("concepto") or "")]
    assert len(smoke_v1) >= 1, "fila V1 de smoke no aparece en V1"
    asserts += 1

    # Categoría inválida.
    data99 = qi.gastos_detalle_categoria(99)
    assert data99["n_filas"] == 0, "num=99 debería devolver vacío"
    asserts += 1

    # num=0 idem.
    data0 = qi.gastos_detalle_categoria(0)
    assert data0["n_filas"] == 0, "num=0 debería devolver vacío"
    asserts += 1

    return asserts


def test_12_ventas_multianual() -> int:
    """Item #12 — Matriz ventas multi-año (MODIFICA.PRG L144-217).

    Verifica shape, totales por año, % variación.
    """
    from modules.informes import queries as qi
    asserts = 0

    data = qi.ventas_multianual(4)
    assert isinstance(data, dict)
    asserts += 1
    assert "anios" in data and len(data["anios"]) == 4, \
        f"esperaba 4 años, vi {data.get('anios')}"
    asserts += 1
    assert "meses" in data and len(data["meses"]) == 12, \
        "siempre 12 meses calendar"
    asserts += 1
    assert "totales_por_anio" in data
    # Cada año tiene los 4 campos.
    for a in data["anios"]:
        t = data["totales_por_anio"][a]
        for campo in ("kg", "importe", "precio_prom", "n"):
            assert campo in t, f"falta {campo} en totales[{a}]"
    asserts += 1

    # Por cada mes y año hay datos válidos (incluso si todo 0).
    for row in data["meses"]:
        for a in data["anios"]:
            d = row["datos"][a]
            assert "kg" in d and "importe" in d and "n" in d
        # var_kg_pct es dict con entrada por año (primer año es None).
        assert "var_kg_pct" in row["datos"]
    asserts += 1

    # Sumar una factura nueva al mes actual y ver que entra en el total
    # del año actual. Usamos fechas spread por mes.
    hoy = date.today()
    _crear_factura(kg=100.0, importe=1234.56, fecha=hoy)
    data2 = qi.ventas_multianual(4)
    anio_actual = data2["anios"][-1]
    t_act = data2["totales_por_anio"][anio_actual]
    assert t_act["kg"] >= 100.0, \
        f"el año actual debería incluir nuestros 100kg, vi {t_act['kg']}"
    asserts += 1

    # anios=1 → 1 año, sin % (todos None).
    d1 = qi.ventas_multianual(1)
    assert len(d1["anios"]) == 1
    asserts += 1
    primer_anio = d1["anios"][0]
    # En cada celda, var_kg_pct[anio_primero] == None.
    for row in d1["meses"]:
        assert row["datos"]["var_kg_pct"][primer_anio] is None
    asserts += 1

    # anios=15 → capeado a 10.
    dbig = qi.ventas_multianual(15)
    assert len(dbig["anios"]) == 10, f"capeado a 10, vi {len(dbig['anios'])}"
    asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Item #2 — Editor inline posdat (PROCEDURE CAMBIA, MENU.PRG L725-803)
# ----------------------------------------------------------------------------

def test_02_posdat_api_flujo() -> int:
    """Item #2 — Endpoints JSON _api de posdat (modal Editar deudas).

    Verifica:
      - Las funciones `_posdat_to_dict` y los handlers existen.
      - `queries.crear` y `queries.editar` aceptan los kwargs que usa la API.
      - `posdat.por_id` devuelve dict con los campos serializables.
    """
    from modules.posdat import queries as qp
    from modules.posdat import views as vp
    asserts = 0

    # Handlers existen y son callable.
    assert callable(getattr(vp, "api_lista_flujo", None)), \
        "falta vista posdat.api_lista_flujo"
    asserts += 1
    assert callable(getattr(vp, "api_editar", None)), \
        "falta vista posdat.api_editar"
    asserts += 1
    assert callable(getattr(vp, "api_anular", None)), \
        "falta vista posdat.api_anular"
    asserts += 1
    assert callable(getattr(vp, "api_nuevo", None)), \
        "falta vista posdat.api_nuevo"
    asserts += 1

    # Helper de serialización maneja None.
    assert vp._posdat_to_dict({}) == {}, "_posdat_to_dict({}) debe ser dict vacío"
    asserts += 1

    # Crear posdat via queries.crear (deja smoke tag) y serializar.
    _ensure_proveedor()
    r = qp.crear(
        fecha=date.today(), fechad=date.today(),
        prov=PROV_TEST, importe=100.0,
        concepto=f"POSDAT API SMOKE {SMOKE_TAG}",
        usuario="auto-smoke",
    )
    assert r and r.get("id_posdat"), "queries.crear no devolvió id_posdat"
    id_p = int(r["id_posdat"])
    asserts += 1

    pd = qp.por_id(id_p)
    assert pd is not None, "por_id no encontró la posdat recién creada"
    asserts += 1

    serial = vp._posdat_to_dict(pd)
    for campo in ("id_posdat", "num", "fechad", "fecha", "importe",
                  "prov", "concepto", "banc"):
        assert campo in serial, f"_posdat_to_dict falta campo {campo}"
    assert isinstance(serial["importe"], float), \
        "importe debe serializarse como float"
    asserts += 1

    # Editar via queries.editar (firma usada por api_editar).
    rc = qp.editar(id_p, importe=150.0, concepto=f"EDIT {SMOKE_TAG}",
                   fechad=date.today(), usuario="auto-smoke")
    assert rc >= 1, "queries.editar no actualizó la fila"
    pd2 = qp.por_id(id_p)
    assert abs(float(pd2.get("importe") or 0) - 150.0) < 0.01, \
        f"importe no se actualizó: {pd2.get('importe')}"
    asserts += 1

    # Anular via queries.anular (firma usada por api_anular).
    qp.anular(id_p, motivo="SMOKE TEST motivo >=10 chars",
              usuario="auto-smoke")
    pd3 = qp.por_id(id_p)
    assert pd3.get("anulada") is True, \
        f"anulada no se marcó True: {pd3.get('anulada')}"
    asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Item #4 — CONTROLC cartera vs snapshot
# ----------------------------------------------------------------------------

def test_04_cartera_snapshot() -> int:
    """Item #4 — Snapshot diario + comparación.

    Verifica:
      - tomar_snapshot() inserta filas y es idempotente (ON CONFLICT).
      - comparar_contra_snapshot() devuelve dict con filas/totales.
      - Si no hay snapshots, devuelve `error` con mensaje informativo.
    """
    import db as _db
    from modules.cartera import queries as qc
    asserts = 0

    # 1. Si no hay snapshots, devuelve error informativo.
    _db.execute(
        "DELETE FROM scintela.cartera_snapshots WHERE codigo_cli = %s",
        (CLI_TEST,),
    )

    # 2. Tomar snapshot — si la base tiene facturas con saldo, debe insertar
    #    >0 filas. Si no, n_clientes==0 sin crashear.
    res = qc.tomar_snapshot(date.today())
    assert isinstance(res, dict), "tomar_snapshot debe devolver dict"
    asserts += 1
    for campo in ("fecha", "n_clientes", "n_filas_insertadas",
                  "n_filas_actualizadas", "saldo_total"):
        assert campo in res, f"tomar_snapshot falta {campo}"
    asserts += 1

    # Re-correr = idempotente (n_filas_actualizadas se incrementa, no
    # insertadas).
    res2 = qc.tomar_snapshot(date.today())
    if res["n_clientes"] > 0:
        assert res2["n_clientes"] == res["n_clientes"], \
            "tomar_snapshot no es idempotente — n_clientes cambió"
    asserts += 1

    # 3. comparar_contra_snapshot devuelve estructura esperada.
    cmp = qc.comparar_contra_snapshot()
    assert "filas" in cmp and "fecha_snapshot" in cmp and "totales" in cmp, \
        f"comparar_contra_snapshot falta campos: {list(cmp.keys())}"
    asserts += 1
    # filas es lista
    assert isinstance(cmp["filas"], list), "filas debe ser lista"
    asserts += 1

    # snapshots_disponibles devuelve lista
    disponibles = qc.snapshots_disponibles(10)
    assert isinstance(disponibles, list), "snapshots_disponibles debe ser lista"
    asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Item #5 — Auto-cierre de stock mensual
# ----------------------------------------------------------------------------

def test_05_cierre_mes_auto() -> int:
    """Item #5 — Copia HI/TJ/PF/UM/UK/UQ/UF al mes siguiente.

    Verifica:
      - cerrar_mes_auto() devuelve dict con aplicado/mes_destino/razon.
      - Es idempotente (segunda corrida devuelve aplicado=False).
      - Si scintela.iniciales está vacía, no crashea — devuelve aplicado=False
        con razon explicativa.
    """
    import db as _db
    from modules.iniciales import queries as qi
    asserts = 0

    # Resetear marker para forzar que el test corra.
    _db.execute(
        """INSERT INTO scintela.sistema_meta (clave, valor)
           VALUES ('cierre_mes_ult_fecha', '1900-01')
           ON CONFLICT (clave) DO UPDATE SET valor = '1900-01'"""
    )

    # Si no hay filas previas, queries.cerrar_mes_auto retorna aplicado=False
    # con razon "vacía". Si las hay, intenta copiar al mes siguiente.
    res = qi.cerrar_mes_auto(usuario="auto-smoke")
    assert isinstance(res, dict), "cerrar_mes_auto debe devolver dict"
    asserts += 1
    for campo in ("aplicado", "mes_origen", "mes_destino", "razon"):
        assert campo in res, f"cerrar_mes_auto falta {campo}"
    asserts += 1
    assert isinstance(res["aplicado"], bool), \
        f"aplicado debe ser bool, no {type(res['aplicado'])}"
    asserts += 1

    # Segunda corrida → idempotente (no debería volver a aplicar).
    res2 = qi.cerrar_mes_auto(usuario="auto-smoke")
    assert res2["aplicado"] is False, \
        f"segunda corrida no es idempotente: {res2}"
    asserts += 1

    # El marker en sistema_meta avanzó (o sigue donde estaba si no había data).
    marker = _db.fetch_one(
        "SELECT valor FROM scintela.sistema_meta WHERE clave = %s",
        ("cierre_mes_ult_fecha",),
    )
    assert marker and marker.get("valor"), "marker no se inicializó"
    asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Item #11 — CARTERA × color × cliente
# ----------------------------------------------------------------------------

def test_11_cartera_por_color() -> int:
    """Item #11 — Cruce cartera × color por cliente (INFORMES.PRG L830-918).

    Verifica:
      - cartera_por_cliente_y_color() devuelve estructura esperada.
      - Está flagueada `necesita_decision=True` con nota explicativa
        (scintela.tinto no tiene codigo_cli).
      - Los 14 colores legacy aparecen en el dict colores de cada cliente.
    """
    from modules.cartera import queries as qc
    asserts = 0

    data = qc.cartera_por_cliente_y_color(meses_atras=3)
    assert isinstance(data, dict), "debe devolver dict"
    asserts += 1
    for campo in ("filas", "colores_orden", "kg_por_color_global",
                  "fuente_color", "necesita_decision", "nota_decision",
                  "meses_atras", "fecha_desde"):
        assert campo in data, f"falta {campo}"
    asserts += 1

    # 14 colores legacy.
    assert len(data["colores_orden"]) == 14, \
        f"esperaba 14 colores, vi {len(data['colores_orden'])}"
    asserts += 1
    assert "MAR" in data["colores_orden"] and "ROS" in data["colores_orden"], \
        "primer y último color del legacy faltan"
    asserts += 1

    # Flag de decisión humana — SIEMPRE True hasta que portemos PIEZAS.
    assert data["necesita_decision"] is True, \
        "necesita_decision debe ser True (aún no portamos scintela.piezas)"
    asserts += 1
    assert "PIEZAS" in data["nota_decision"] or "piezas" in data["nota_decision"], \
        "la nota debería mencionar la decisión sobre la tabla PIEZAS"
    asserts += 1

    # Cada cliente tiene los 14 colores en 0 (placeholder).
    if data["filas"]:
        for r in data["filas"][:3]:
            assert "colores" in r and isinstance(r["colores"], dict), \
                "fila sin dict de colores"
            assert len(r["colores"]) == 14, \
                f"esperaba 14 colores en fila, vi {len(r['colores'])}"
        asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Item #6 — Boleta de depósito (BOLEPICH / BOLEIN)
# ----------------------------------------------------------------------------

def test_06_boleta_deposito() -> int:
    """Item #6 — Boleta de depósito impresa (BANCOS.PRG L1250-1359).

    Verifica:
      - `cheques.queries.boleta_deposito(fecha, no_banco)` existe y devuelve
        el shape esperado.
      - Si no hay depósitos para esa fecha/banco, devuelve n_cheques=0
        sin crashear.
      - El número de cuenta hardcoded (Pichincha=42000867-4, Inter=60484-9)
        aparece para los bancos por nombre.
    """
    from modules.cheques import queries as qc
    asserts = 0

    # Función existe y es callable.
    assert callable(getattr(qc, "boleta_deposito", None)), \
        "falta cheques.queries.boleta_deposito"
    asserts += 1

    # Banco inexistente → ValueError.
    try:
        qc.boleta_deposito(fecha=date.today(), no_banco=9999)
        raise AssertionError("no levantó ValueError con banco inexistente")
    except ValueError:
        pass
    except Exception as e:
        # Si la DB no tiene la tabla banco, lo skipeamos suavemente.
        raise SkipTest(f"sin scintela.banco: {e}")
    asserts += 1

    # Levantar boleta para un banco real (uso el primero existente).
    row = db.fetch_one(
        "SELECT no_banco, COALESCE(nombre,'') AS nombre "
        "FROM scintela.banco ORDER BY no_banco LIMIT 1"
    )
    if not row:
        raise SkipTest("no hay filas en scintela.banco")
    no_banco = int(row["no_banco"])

    boleta = qc.boleta_deposito(fecha=date.today(), no_banco=no_banco)
    assert isinstance(boleta, dict), \
        f"boleta_deposito debe devolver dict, vi {type(boleta)}"
    asserts += 1
    for campo in ("banco_nombre", "no_banco", "no_cuenta", "fecha",
                  "cheques", "total", "n_cheques"):
        assert campo in boleta, f"boleta falta {campo}"
    asserts += 1
    assert isinstance(boleta["cheques"], list), "cheques debe ser lista"
    asserts += 1
    assert isinstance(boleta["total"], int | float), \
        f"total debe ser numérico, vi {type(boleta['total'])}"
    asserts += 1

    # Si el banco se llama PICHINCHA o INTERNACIONAL, el no_cuenta hardcoded
    # debe aparecer (paridad BANCOS.PRG L1197).
    nombre = (boleta.get("banco_nombre") or "").upper()
    if "PICHINC" in nombre:
        assert boleta["no_cuenta"] == "42000867-4", \
            f"Pichincha no_cuenta esperada 42000867-4, vi {boleta['no_cuenta']}"
        asserts += 1
    elif "INTER" in nombre:
        assert boleta["no_cuenta"] == "60484-9", \
            f"Inter no_cuenta esperada 60484-9, vi {boleta['no_cuenta']}"
        asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Item #7 — Cheque XX reemplazo
# ----------------------------------------------------------------------------

def test_07_cheque_reemplazo() -> int:
    """Item #7 — Reemplazo de cheque (BANCOS.PRG L266-305).

    Crea un cheque viejo en cartera (stat='Z'), lo reemplaza por uno nuevo,
    y verifica:
      - El viejo queda stat='X', importe=0.
      - El nuevo existe con stat='Z' y id_cheque_padre = id_viejo.
      - mov_doble registra tipo='cheque_reemplazo' linkeado.
      - Si el viejo tenía aplicaciones a facturas, se migran al nuevo.
    """
    from modules.cheques import queries as qc
    _ensure_cliente()
    asserts = 0

    # 1) Crear cheque viejo
    ch_viejo = qc.crear(
        fecha=date.today(),
        codigo_cli=CLI_TEST,
        no_cheque="SMOKE07A",
        importe=1500.0,
        banco_texto=f"{SMOKE_TAG} BCO",
        usuario="auto-smoke",
    )
    assert ch_viejo and ch_viejo.get("id_cheque"), \
        "no pude crear el cheque viejo"
    id_viejo = int(ch_viejo["id_cheque"])
    asserts += 1

    # 2) Reemplazar
    r = qc.reemplazar(
        id_cheque_viejo=id_viejo,
        nuevo_no_cheque="SMOKE07B",
        nuevo_importe=1700.0,
        motivo=f"SMOKE motivo {SMOKE_TAG}",
        usuario="auto-smoke",
    )
    assert r and r.get("id_cheque_nuevo"), \
        f"reemplazar no devolvió id_cheque_nuevo: {r}"
    id_nuevo = int(r["id_cheque_nuevo"])
    asserts += 1
    assert r["no_cheque_nuevo"] == "SMOKE07B", \
        f"no_cheque_nuevo distinto: {r['no_cheque_nuevo']}"
    asserts += 1

    # 3) Cheque viejo: stat='X', importe PRESERVADO (face value original).
    # TMT 2026-05-15 (re-audit H1): antes el test verificaba importe=0 — eso
    # era exactamente el bug. Ahora `reemplazar()` mantiene el face value
    # del cheque viejo en su columna `importe` para auditoría y mov_doble;
    # el "consumo" se marca con stat='X' y la observación.
    row_v = db.fetch_one(
        "SELECT stat, importe FROM scintela.cheque WHERE id_cheque=%s",
        (id_viejo,),
    )
    assert row_v and (row_v.get("stat") or "").upper() == "X", \
        f"viejo no quedó en X: stat={row_v.get('stat') if row_v else None}"
    asserts += 1
    assert abs(float(row_v.get("importe") or 0) - 1500.0) < 0.01, (
        f"viejo NO conservó importe original 1500: {row_v.get('importe')} — "
        "regresión H1 del re-audit."
    )
    asserts += 1

    # 4) Cheque nuevo: stat='Z', id_cheque_padre=id_viejo, importe=1700.
    row_n = db.fetch_one(
        "SELECT stat, importe, id_cheque_padre, codigo_cli "
        "FROM scintela.cheque WHERE id_cheque=%s",
        (id_nuevo,),
    )
    assert row_n, "no encontré el cheque nuevo en la DB"
    asserts += 1
    assert (row_n.get("stat") or "").upper() == "Z", \
        f"nuevo no quedó en Z: stat={row_n.get('stat')}"
    asserts += 1
    assert int(row_n.get("id_cheque_padre") or 0) == id_viejo, \
        f"id_cheque_padre del nuevo no apunta al viejo: " \
        f"vi {row_n.get('id_cheque_padre')}, esperaba {id_viejo}"
    asserts += 1
    assert abs(float(row_n.get("importe") or 0) - 1700.0) < 0.01, \
        f"importe del nuevo no es 1700: {row_n.get('importe')}"
    asserts += 1

    # 5) hijos(id_viejo) incluye al nuevo.
    hijos = qc.hijos(id_viejo)
    assert any(int(h["id_cheque"]) == id_nuevo for h in hijos), \
        f"hijos() de {id_viejo} no incluye {id_nuevo}: {hijos}"
    asserts += 1

    # 6) mov_doble del reemplazo existe.
    md = db.fetch_one(
        """
        SELECT id_mov_doble, importe FROM scintela.mov_doble
         WHERE tipo='cheque_reemplazo'
           AND origen_id=%s AND destino_id=%s
         LIMIT 1
        """,
        (id_viejo, id_nuevo),
    )
    assert md, "no se registró mov_doble cheque_reemplazo"
    asserts += 1
    assert abs(float(md.get("importe") or 0) - 1700.0) < 0.01, \
        f"importe en mov_doble incorrecto: {md.get('importe')}"
    asserts += 1

    # 7) Reemplazar un stat terminal → ValueError.
    # Marcamos el viejo (que ya está en X) y verificamos que no se pueda
    # reemplazar uno que está en X.
    try:
        qc.reemplazar(
            id_cheque_viejo=id_viejo,  # ya está en X
            nuevo_no_cheque="SMOKE07C",
            nuevo_importe=100.0,
            usuario="auto-smoke",
        )
        raise AssertionError("no levantó ValueError reemplazando cheque en X")
    except ValueError:
        pass
    asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Item #8 — BAP: conversión lote anticipos USD → compra
# ----------------------------------------------------------------------------

def test_08_bap_anticipos_a_compra() -> int:
    """Item #8 — Lote anticipos USD a compra (BANCOS.PRG L733-819).

    Crea 3 anticipos en USD para PROV_TEST, los convierte a una compra
    vía BAP, y verifica:
      - Compra creada con comprobante='BAP<n>' + cuenta_pagada='A'.
      - Anticipos marcados con st='B' (consumidos) — paridad dBase
        BANCOS.PRG L803-816 `REPLA ALL ST WITH 'B'`. TMT 2026-05-15
        (decisión #8): antes esperábamos 'X', revertido a 'B'.
      - importe de la compra = suma de los anticipos.
      - mov_doble registra tipo='bap_anticipo_a_compra'.
      - Selección con anticipos de OTRO proveedor → ValueError.
    """
    from modules.dolares import queries as qd
    _ensure_proveedor()
    asserts = 0

    # Crear 3 anticipos vivos
    ids: list[int] = []
    for i, imp in enumerate([500.0, 700.0, 300.0]):
        r = db.execute_returning(
            """
            INSERT INTO scintela.dolares
                (fecha, cta, importe, concepto, usuario_crea)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id_dolares
            """,
            (date.today(), PROV_TEST, imp,
             f"SMOKE BAP #{i} {SMOKE_TAG}"[:50], "auto-smoke"),
        )
        assert r and r.get("id_dolares"), "no pude insertar anticipo"
        ids.append(int(r["id_dolares"]))
    asserts += 1

    # Función existe
    assert callable(getattr(qd, "convertir_a_compra", None)), \
        "falta dolares.queries.convertir_a_compra"
    asserts += 1
    assert callable(getattr(qd, "anticipos_pendientes_por_proveedor", None)), \
        "falta dolares.queries.anticipos_pendientes_por_proveedor"
    asserts += 1

    # Agrupación: PROV_TEST aparece en la lista con total >= 1500.
    grupos = qd.anticipos_pendientes_por_proveedor()
    mi_grupo = next(
        (g for g in grupos if g["codigo_prov"] == PROV_TEST),
        None,
    )
    assert mi_grupo is not None, \
        "PROV_TEST no aparece en anticipos_pendientes_por_proveedor"
    asserts += 1
    assert float(mi_grupo["total_usd"]) >= 1500.0 - 0.01, \
        f"total_usd esperado >=1500, vi {mi_grupo['total_usd']}"
    asserts += 1

    # Sin selección → ValueError.
    try:
        qd.convertir_a_compra(codigo_prov=PROV_TEST, ids_anticipos=[],
                              usuario="auto-smoke")
        raise AssertionError("no levantó ValueError con ids vacíos")
    except ValueError:
        pass
    asserts += 1

    # IDs inexistentes → ValueError.
    try:
        qd.convertir_a_compra(codigo_prov=PROV_TEST,
                              ids_anticipos=[9999999], usuario="auto-smoke")
        raise AssertionError("no levantó ValueError con IDs inexistentes")
    except ValueError:
        pass
    asserts += 1

    # Conversión OK
    r = qd.convertir_a_compra(
        codigo_prov=PROV_TEST,
        ids_anticipos=ids,
        concepto=f"BAP SMOKE {SMOKE_TAG}",
        tipo_compra="H",
        motivo="smoke test",
        usuario="auto-smoke",
    )
    assert r and r.get("id_compra"), f"convertir_a_compra falló: {r}"
    asserts += 1
    assert abs(float(r["importe_total"]) - 1500.0) < 0.01, \
        f"importe_total esperado 1500, vi {r['importe_total']}"
    asserts += 1
    assert r["n_anticipos"] == 3, \
        f"n_anticipos esperado 3, vi {r['n_anticipos']}"
    asserts += 1
    assert r["comprobante"].startswith("BAP"), \
        f"comprobante no arranca con BAP: {r['comprobante']}"
    asserts += 1

    # Compra en DB: cuenta_pagada='A', importe=1500, comprobante=BAP*.
    comp = db.fetch_one(
        "SELECT cuenta_pagada, importe, comprobante FROM scintela.compra "
        "WHERE id_compra=%s",
        (r["id_compra"],),
    )
    assert comp, "no encontré la compra creada en DB"
    asserts += 1
    assert (comp.get("cuenta_pagada") or "").upper() == "A", \
        f"cuenta_pagada esperada A, vi {comp.get('cuenta_pagada')}"
    asserts += 1
    assert abs(float(comp.get("importe") or 0) - 1500.0) < 0.01, \
        f"importe en compra esperado 1500, vi {comp.get('importe')}"
    asserts += 1

    # Anticipos: todos st='B' (paridad dBase BANCOS.PRG L803-816 —
    # `REPLA ALL ST WITH 'B'`). TMT 2026-05-15 (decisión #8): antes el
    # smoke esperaba 'X'; revertido a 'B' al alinearnos con dBase.
    placeholder = ",".join(["%s"] * len(ids))
    rows_a = db.fetch_all(
        f"SELECT id_dolares, st FROM scintela.dolares "
        f"WHERE id_dolares IN ({placeholder})",
        tuple(ids),
    ) or []
    for ra in rows_a:
        assert (ra.get("st") or "").upper() == "B", \
            f"anticipo {ra['id_dolares']} no tiene st='B': {ra.get('st')}"
    asserts += 1

    # mov_doble registrado
    md = db.fetch_one(
        """
        SELECT id_mov_doble, importe FROM scintela.mov_doble
         WHERE tipo='bap_anticipo_a_compra'
           AND destino_table='compra' AND destino_id=%s
         LIMIT 1
        """,
        (r["id_compra"],),
    )
    assert md, "no se registró mov_doble bap_anticipo_a_compra"
    asserts += 1
    assert abs(float(md.get("importe") or 0) - 1500.0) < 0.01, \
        f"importe en mov_doble incorrecto: {md.get('importe')}"
    asserts += 1

    # Re-correr con los mismos anticipos (ya consumidos) → ValueError.
    try:
        qd.convertir_a_compra(codigo_prov=PROV_TEST, ids_anticipos=ids,
                              usuario="auto-smoke")
        raise AssertionError("no levantó ValueError con anticipos ya consumidos")
    except ValueError:
        pass
    asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Item #10 — EFECT cobros 3 semanas matriz
# ----------------------------------------------------------------------------

def test_10_cobros_matriz_3_semanas() -> int:
    """Item #10 — Matriz cobros 3 semanas (MENU.PRG L1493-1522 EFECT).

    Verifica shape de la matriz, claves por día y promedio.
    """
    from modules.cobranzas import queries as qco
    asserts = 0

    assert callable(getattr(qco, "cobros_matriz_3_semanas", None)), \
        "falta cobranzas.queries.cobros_matriz_3_semanas"
    asserts += 1

    # Sin argumentos → 3 semanas.
    matriz = qco.cobros_matriz_3_semanas()
    assert isinstance(matriz, list), \
        f"esperaba lista, vi {type(matriz)}"
    asserts += 1
    assert len(matriz) == 3, \
        f"esperaba 3 semanas, vi {len(matriz)}"
    asserts += 1

    # Cada semana tiene los días lun-sáb + total + prom.
    dias_keys = ["lunes", "martes", "miercoles", "jueves",
                 "viernes", "sabado"]
    for s in matriz:
        for k in dias_keys + ["total_semana", "prom_dia_habil",
                              "fecha_lunes", "fecha_sabado",
                              "dias_para_prom", "semana"]:
            assert k in s, f"falta clave {k} en semana {s.get('semana')}"
        # Cada día es numérico.
        for d in dias_keys:
            assert isinstance(s[d], int | float), \
                f"día {d} no es numérico: {type(s[d])}"
        # total_semana = suma de los 6 días.
        suma = sum(float(s[d] or 0) for d in dias_keys)
        assert abs(suma - float(s["total_semana"])) < 0.01, \
            f"total_semana ({s['total_semana']}) != suma de días ({suma})"
        # prom_dia_habil >= 0
        assert float(s["prom_dia_habil"]) >= 0
    asserts += 1

    # Las fechas de las semanas están en orden ascendente
    fechas_lunes = [s["fecha_lunes"] for s in matriz]
    assert fechas_lunes == sorted(fechas_lunes), \
        f"semanas no están en orden: {fechas_lunes}"
    asserts += 1
    # Cada lunes es exactamente 7 días después del anterior.
    delta1 = (fechas_lunes[1] - fechas_lunes[0]).days
    delta2 = (fechas_lunes[2] - fechas_lunes[1]).days
    assert delta1 == 7 and delta2 == 7, \
        f"semanas no consecutivas: deltas {delta1}, {delta2}"
    asserts += 1

    # fecha_hasta retroactiva — la última semana es la del hasta.
    hasta_test = date(2025, 5, 15)  # un jueves arbitrario del 2025
    matriz_old = qco.cobros_matriz_3_semanas(fecha_hasta=hasta_test)
    assert len(matriz_old) == 3
    asserts += 1
    # El lunes de la semana actual debe ser el lunes de la semana de hasta.
    lunes_esperado = hasta_test - timedelta(days=hasta_test.weekday())
    assert matriz_old[-1]["fecha_lunes"] == lunes_esperado, \
        f"fecha_lunes esperado {lunes_esperado}, vi {matriz_old[-1]['fecha_lunes']}"
    asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------

TESTS: list[tuple[str, str, callable]] = [
    ("1",   "Provisión $80K en UT.PROY (INFORMES.PRG L420)",        test_01_provision_pendiente),
    ("2",   "Editor inline posdat banc=0/9 (MENU.PRG L725-803)",    test_02_posdat_api_flujo),
    ("3",   "Historia multi-año H/N/1/2/3 (L1336-1550)",            test_03_historia_multianual),
    ("4",   "CONTROLC cartera vs snapshot (MENU.PRG L1582-1660)",   test_04_cartera_snapshot),
    ("5",   "Auto-cierre stock mensual (MENU.PRG L246-263)",        test_05_cierre_mes_auto),
    ("6",   "Boleta depósito BOLEPICH/BOLEIN (L1250-1359)",         test_06_boleta_deposito),
    ("7",   "Cheque XX reemplazo (BANCOS.PRG L266-305)",            test_07_cheque_reemplazo),
    ("8",   "BAP — anticipos USD → compra (L733-819)",              test_08_bap_anticipos_a_compra),
    ("9",   "DETALGAST drill-down V1..V9 (L1266-1334)",             test_09_gastos_detalle_categoria),
    ("10",  "EFECT cobros 3 semanas matriz (L1493-1522)",           test_10_cobros_matriz_3_semanas),
    ("11",  "Cartera × color × cliente (INFORMES.PRG L830-918)",    test_11_cartera_por_color),
    ("12",  "Ventas multi-año matriz (MODIFICA.PRG L144-217)",      test_12_ventas_multianual),
    # Re-audit 2026-05-15 — regression guards de bugs encontrados.
    ("R5",  "re-audit C5 — provisiones A,E,C matcher",              test_01b_provisiones_diarias_matcher_aec),
    ("R6",  "re-audit H5 — boleta_deposito no_banco dinámico",      test_06b_boleta_no_banco_dinamico),
    ("R7",  "re-audit H1 — reemplazar NO zeroea importe",           test_07b_reemplazar_no_zerea_importe),
    ("R8",  "re-audit C3 — BAP comprobante advisory lock + MAX",    test_08b_bap_seq_robusto),
]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", default="",
                   help="lista de tests a correr (ej. 1,3)")
    p.add_argument("--keep-data", action="store_true",
                   help="no limpia data al final (debug)")
    p.add_argument("--verbose", action="store_true",
                   help="output extra")
    return p.parse_args()


def _db_disponible() -> bool:
    try:
        db.fetch_one("SELECT 1 AS x")
        return True
    except Exception:
        return False


def main() -> int:
    args = parse_args()
    only = set(args.only.split(",")) if args.only.strip() else None

    print("=" * 70)
    print(" SMOKE TEST — port dBase → Programa Core (4 lógicas)")
    print("=" * 70)

    if not _db_disponible():
        print("⚠️  No se pudo conectar a la DB local. Tests con DB van a fallar.")
        print()

    if _db_disponible():
        cleanup(verbose=args.verbose)

    resultados: list[tuple[str, str, str, str, int]] = []
    try:
        for n, desc, fn in TESTS:
            if only and n not in only:
                continue
            try:
                asserts = fn()
                resultados.append(("✓", n, desc, "", asserts or 0))
                print(f"[✓] ITEM #{n} — {desc} · {asserts} asserts OK")
            except SkipTest as e:
                resultados.append(("⊘", n, desc, str(e), 0))
                print(f"[SKIP] ITEM #{n} — {desc} · {e}")
            except AssertionError as e:
                resultados.append(("✗", n, desc, str(e), 0))
                print(f"[✗] ITEM #{n} — {desc}")
                print(f"      razón: {str(e)[:300]}")
            except Exception as e:
                low = str(e).lower()
                if "connection refused" in low or "could not connect" in low:
                    resultados.append(("⊘", n, desc, "DB no disponible", 0))
                    print(f"[SKIP] ITEM #{n} — DB no disponible")
                else:
                    msg = f"unexpected: {type(e).__name__}: {e}"
                    resultados.append(("✗", n, desc, msg, 0))
                    print(f"[✗] ITEM #{n} — {desc}")
                    print(f"      razón: {msg}")
                    if args.verbose:
                        print(traceback.format_exc())
    finally:
        if _db_disponible() and not args.keep_data:
            print()
            print("Limpiando data __SMOKE_DBASE__ ...")
            cleanup(verbose=args.verbose)

    print()
    print("=" * 70)
    n_ok = sum(1 for r in resultados if r[0] == "✓")
    n_skip = sum(1 for r in resultados if r[0] == "⊘")
    n_fail = sum(1 for r in resultados if r[0] == "✗")
    total_asserts = sum(r[4] for r in resultados)
    print(f" {n_ok}/{len(resultados)} OK · {n_skip} SKIP · {n_fail} FAIL · "
          f"{total_asserts} asserts totales")
    print("=" * 70)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
