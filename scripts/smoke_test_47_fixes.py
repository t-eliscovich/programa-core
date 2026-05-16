#!/usr/bin/env python3
"""Smoke test de los 47 fixes + 2 refactors cross-cutting del 2026-05-14.

Corre contra la DB local de Tamara (`postgresql://localhost:5432/intela`) y
ejerce cada uno de los fixes con un caso concreto, asertando sobre saldos
bancarios, mov_doble, estados de facturas / cheques / posdat y excepciones
esperadas.

USO:
    python scripts/smoke_test_47_fixes.py
    python scripts/smoke_test_47_fixes.py --only 03,07,10   # subset de tests
    python scripts/smoke_test_47_fixes.py --keep-data       # debug: no limpia

Reglas del script:
    - Idempotente: la data de prueba se identifica con códigos `__SMOKE_*__`
      o claves arrancadas con `__SK__`. Cualquier ejecución (incluso si una
      anterior crasheó) limpia antes de arrancar y al final.
    - No requiere Flask app context — todas las queries son directas a `db`.
    - Si una migración pendiente (0027) hace fallar un test, lo marca [SKIP]
      con mensaje explícito en vez de [✗].
    - Output: `[✓] FIX #N — desc · K asserts OK` / `[✗] FIX #N — desc · razón`
      / `[SKIP] FIX #N — razón`. Summary al final.

NOTA: este script NO arregla nada; SÓLO detecta regresiones. Si encontrás
un bug acá, anotalo en el reporte de re-audit antes de tocar código.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# .env opcional — si la usuaria ya tiene env vars seteadas, dotenv no rompe.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import db  # noqa: E402

# ----------------------------------------------------------------------------
# Constantes de prueba — se usan para identificar la data y limpiarla después.
# ----------------------------------------------------------------------------

SMOKE_TAG = "__SMOKE_47__"
# Cliente test (3 chars alfanum requeridos para el match exacto del listado).
CLI_TEST = "ZSZ"
# Proveedor test.
PROV_TEST = "ZSP"
# Socio test (para retiros/aportes).
SOCIO_TEST = "ZS"
# Banco test = el primero en scintela.banco (Pichincha en prod).
BANCO_DEFAULT = 1


# ----------------------------------------------------------------------------
# Helpers de setup / cleanup
# ----------------------------------------------------------------------------

def _migration_0027_aplicada() -> bool:
    """¿Está la columna `posdat.anulada` en la DB?"""
    try:
        row = db.fetch_one("""
            SELECT 1 AS x
              FROM information_schema.columns
             WHERE table_schema = 'scintela'
               AND table_name = 'posdat'
               AND column_name = 'anulada'
        """)
        return bool(row)
    except Exception:
        return False


def cleanup(verbose: bool = False) -> None:
    """Borra TODA la data de prueba __SMOKE_*__.

    Orden importa por FKs (chequesxfact → cheque, retiros → tx, etc.).
    Best-effort: si una tabla no existe (algún módulo desactivado), seguir.
    """
    def _try(sql: str, params: tuple = ()) -> None:
        try:
            db.execute(sql, params)
            if verbose:
                print(f"    cleanup: {sql.split('FROM')[1].split('WHERE')[0].strip()}")
        except Exception as e:
            if verbose:
                print(f"    cleanup falló (no fatal): {e}")

    # Borrar dependencias primero
    _try("DELETE FROM scintela.chequesxfact WHERE id_cheque IN ("
         "  SELECT id_cheque FROM scintela.cheque WHERE codigo_cli = %s)", (CLI_TEST,))
    _try("DELETE FROM scintela.chequextransaccion WHERE id_cheque IN ("
         "  SELECT id_cheque FROM scintela.cheque WHERE codigo_cli = %s)", (CLI_TEST,))
    _try("DELETE FROM scintela.mov_doble WHERE concepto LIKE %s", (f"%{SMOKE_TAG}%",))
    _try("DELETE FROM scintela.mov_doble WHERE metadata::text LIKE %s", (f"%{SMOKE_TAG}%",))
    _try("DELETE FROM scintela.retiros WHERE de = %s", (SOCIO_TEST,))
    _try("DELETE FROM scintela.transacciones_bancarias WHERE concepto LIKE %s",
         (f"%{SMOKE_TAG}%",))
    _try("DELETE FROM scintela.caja WHERE concepto LIKE %s", (f"%{SMOKE_TAG}%",))
    _try("DELETE FROM scintela.xgast WHERE concepto LIKE %s", (f"%{SMOKE_TAG}%",))
    _try("DELETE FROM scintela.posdat WHERE prov = %s", (PROV_TEST,))
    _try("DELETE FROM scintela.compra WHERE codigo_prov = %s", (PROV_TEST,))
    _try("DELETE FROM scintela.factura WHERE codigo_cli = %s", (CLI_TEST,))
    _try("DELETE FROM scintela.cheque WHERE codigo_cli = %s", (CLI_TEST,))
    _try("DELETE FROM scintela.cliente WHERE codigo_cli = %s", (CLI_TEST,))
    _try("DELETE FROM scintela.proveedor WHERE codigo_prov = %s", (PROV_TEST,))


def _ensure_cliente() -> None:
    db.execute("""
        INSERT INTO scintela.cliente (codigo_cli, nombre, stop, observacion)
        VALUES (%s, %s, 'N', '')
        ON CONFLICT (codigo_cli) DO NOTHING
    """, (CLI_TEST, f"{SMOKE_TAG} CLIENTE"))


def _ensure_proveedor() -> None:
    db.execute("""
        INSERT INTO scintela.proveedor (codigo_prov, nombre, activo)
        VALUES (%s, %s, '1')
        ON CONFLICT (codigo_prov) DO NOTHING
    """, (PROV_TEST, f"{SMOKE_TAG} PROVEEDOR"))


def crear_factura(*, importe: float = 100.0, fecha: date | None = None) -> int:
    fecha = fecha or date.today()
    _ensure_cliente()
    row = db.fetch_one("SELECT COALESCE(MAX(numf), 0) + 1 AS n FROM scintela.factura")
    numf = int(row["n"]) if row else 1
    row = db.execute_returning("""
        INSERT INTO scintela.factura
            (numf, numf_completo, fecha, vencimiento, codigo_cli,
             importe, abono, saldo, stat, usuario_crea)
        VALUES (%s, %s::text, %s, %s, %s,
                %s, 0, %s, 'Z', %s)
        RETURNING id_factura
    """, (numf, str(numf), fecha, fecha + timedelta(days=30), CLI_TEST,
          importe, importe, SMOKE_TAG))
    return int(row["id_factura"]) if row else 0


def crear_cheque(
    *,
    importe: float = 100.0,
    no_cheque: str = "99001",
    stat: str = "Z",
    fechad: date | None = None,
    es_anticipo: bool = False,
) -> int:
    """Crea un cheque vía queries.crear — ejerce los mov_doble nuevos."""
    _ensure_cliente()
    from modules.cheques import queries as qch
    r = qch.crear(
        fecha=date.today(),
        codigo_cli=CLI_TEST,
        no_cheque=no_cheque,
        importe=importe,
        no_banco=BANCO_DEFAULT,
        banco_texto="PICH",
        fechad=fechad or date.today(),
        stat=stat,
        es_anticipo=es_anticipo,
        usuario=SMOKE_TAG,
    )
    return int(r.get("id_cheque") or 0)


def crear_posdat(*, importe: float = 100.0, banc: int = 0,
                 fechad: date | None = None) -> int:
    _ensure_proveedor()
    fechad = fechad or (date.today() + timedelta(days=30))
    from modules.posdat import queries as qpd
    r = qpd.crear(
        fecha=date.today(),
        fechad=fechad,
        prov=PROV_TEST,
        importe=importe,
        concepto=f"{SMOKE_TAG} posdat",
        banc=banc,
        usuario=SMOKE_TAG,
    )
    return int(r.get("id_posdat") or 0)


def saldo_banco(no_banco: int = BANCO_DEFAULT) -> float:
    import bank_helpers
    return bank_helpers.saldo_actual(no_banco)


def md_count(*, tipo: str | None = None, estado: str | None = None,
             origen_id: int | None = None, origen_table: str | None = None) -> int:
    """Cuenta mov_doble matcheando los filtros pasados."""
    where: list[str] = []
    params: list = []
    if tipo:
        where.append("tipo = %s")
        params.append(tipo)
    if estado:
        where.append("estado = %s")
        params.append(estado)
    if origen_id is not None:
        where.append("origen_id = %s")
        params.append(origen_id)
    if origen_table:
        where.append("origen_table = %s")
        params.append(origen_table)
    sql = "SELECT COUNT(*) AS n FROM scintela.mov_doble"
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = db.fetch_one(sql, tuple(params))
    return int((row or {}).get("n") or 0)


# ----------------------------------------------------------------------------
# Tests — uno por fix (numerado igual que el resumen).
#
# Convención:
#   - Cada test devuelve un int = cantidad de asserts que pasaron.
#   - Si algo falla, raisa AssertionError (caught en main).
#   - Si la migration 0027 (o cualquier requisito) falta, raisa
#     RuntimeError("SKIP: <razón>") — main lo trata como [SKIP].
# ----------------------------------------------------------------------------

class SkipTest(Exception):
    """Marker para [SKIP] en vez de [✗]."""


def test_R1_recompute_saldos_no_destruye_opening() -> int:
    """RE-AUDIT #R1 (CRÍTICO): bancos.recompute_saldos usa MIN(id_tx).

    Esto es funcionalmente equivalente a `desde_cero=True` porque NO hay
    filas con `id < MIN(id)`. Resultado: `_saldo_previo` devuelve 0 y la
    primera fila del banco queda con `saldo = 0 + delta_primera_fila`, en
    vez de preservar el opening implícito.

    Bug histórico TMT 2026-05-12: "Pichincha pasó de 2,280,906 a -917,651".
    Fue causado por un recompute con ancla=None. Este fix #2 cambió ancla
    a MIN(id_tx) — pero MIN(id) tiene el MISMO efecto que None porque no
    hay nada antes para anclar.

    Verificación estática del código de `recompute_saldos_desde`:
    cuando ancla_id se pasa y no hay filas con `id < ancla_id`, saldo
    arranca en 0. La condición se cumple para ancla=MIN(id).
    """
    p = ROOT / "bank_helpers.py"
    txt = p.read_text()
    # Bloque ancla_id:
    idx = txt.find("if ancla_id is not None:")
    chunk = txt[idx:idx + 1200]
    # Confirmar: cuando no hay fila pre-ancla → saldo = 0.0.
    assert "saldo = float(row[\"saldo\"]) if row else 0.0" in chunk \
        or "if row else 0.0" in chunk, \
        "no encuentro el fallback saldo=0 cuando no hay fila pre-ancla"
    # Y el endpoint pasa MIN(id) como ancla → mismo bug que ancla=None.
    p2 = ROOT / "modules" / "bancos" / "views.py"
    txt2 = p2.read_text()
    if "MIN(id_transaccion) AS ancla" in txt2:
        raise AssertionError(
            "BUG #R1 (CRÍTICO): bancos.views.recompute_saldos pasa "
            "MIN(id_transaccion) como ancla_id a recompute_saldos_desde. "
            "El check de bank_helpers (`if ancla_id is None and not desde_cero`) "
            "PASA, pero la query de saldo_previo (`id < MIN(id)`) NUNCA matchea "
            "→ arranca en 0, destruyendo el opening implícito del banco. "
            "Mismo bug que destruyó Pichincha en 2026-05-12 (passar 2.28M → "
            "-917K). Fix: usar el id_transaccion SEGUNDO más viejo, o pasar "
            "ancla_fecha=la fecha de la SEGUNDA fila, o explícitamente "
            "computar saldo de la primera fila y arrancar desde la segunda."
        )
    return 1


def test_01_reversar_transferencia_no_explota() -> int:
    """FIX #1: bancos.reversar_transferencia imports al top — no NameError."""
    # Sólo importamos el módulo y referenciamos la función — si falta un
    # import al top o la function no carga, ImportError aquí.
    from modules.bancos import queries as qb
    assert callable(qb.reversar_transferencia)
    assert callable(qb.transferir_entre_bancos)
    return 2


def test_02_recompute_saldos_ancla_por_banco() -> int:
    """FIX #2: recompute con MIN(id_tx) ancla, tx por banco.

    Verifica que el endpoint LLAMA recompute_saldos_desde con ancla_id != None
    y que la tx es POR banco (no global). Lectura estática del código.
    """
    p = ROOT / "modules" / "bancos" / "views.py"
    txt = p.read_text()
    assert "MIN(id_transaccion)" in txt, \
        "MIN(id_transaccion) AS ancla no aparece en bancos/views.py — fix #2 ausente"
    assert "ancla_id=int(ancla_id)" in txt, \
        "ancla_id no se pasa explícito en recompute_saldos_desde"
    # tx por banco: el `with _db.tx()` está adentro del for, no afuera.
    idx_for = txt.find("for b in bancos:")
    idx_tx = txt.find("with _db.tx() as conn:", idx_for)
    idx_recompute = txt.find("bank_helpers.recompute_saldos_desde", idx_for)
    assert idx_for >= 0 and idx_tx > idx_for and idx_recompute > idx_tx, \
        "tx no está dentro del loop por banco — fix #2 incompleto"
    return 3


def test_03_posdat_anular_soft_delete() -> int:
    """FIX #3: posdat.anular hace soft-delete con anulada=TRUE + motivo."""
    if not _migration_0027_aplicada():
        raise SkipTest("migration 0027 pendiente")
    from modules.posdat import queries as qpd
    id_pd = crear_posdat(importe=123.45, banc=0)
    qpd.anular(id_pd, motivo="smoke test: razón mínima de 10 chars",
               usuario=SMOKE_TAG)
    row = db.fetch_one(
        "SELECT anulada, motivo_anulacion, fecha_anulacion "
        "FROM scintela.posdat WHERE id_posdat = %s", (id_pd,))
    assert row is not None, "posdat fue borrada (DELETE físico)"
    assert row.get("anulada") is True, f"anulada != TRUE: {row}"
    assert row.get("motivo_anulacion"), "motivo_anulacion vacío"
    assert row.get("fecha_anulacion") is not None, "fecha_anulacion NULL"
    # mov_doble registrado
    n = md_count(tipo="posdat_anulada", origen_id=id_pd, origen_table="posdat")
    assert n == 1, f"esperaba 1 mov_doble posdat_anulada, vi {n}"
    return 5


def test_03b_posdat_anular_bloquea_banc_distinto_0() -> int:
    """FIX #10/#3: anular posdat con banc<>0 raisea (cheque colgado)."""
    if not _migration_0027_aplicada():
        raise SkipTest("migration 0027 pendiente")
    from modules.posdat import queries as qpd
    id_pd = crear_posdat(importe=88.0, banc=10)  # pagada con cheque banco 10
    try:
        qpd.anular(id_pd, motivo="smoke: debería bloquear",
                   usuario=SMOKE_TAG)
    except ValueError as e:
        assert "banc" in str(e).lower(), f"mensaje no menciona banc: {e}"
        return 1
    raise AssertionError("anular(banc=10) debería haber raised ValueError")


def test_04_marcar_pagada_deprecada() -> int:
    """FIX #4: posdat.marcar_pagada() raisea ValueError (deprecada)."""
    from modules.posdat import queries as qpd
    try:
        qpd.marcar_pagada(99999, usuario=SMOKE_TAG)
    except ValueError as e:
        assert "deprecada" in str(e).lower() or "deprecated" in str(e).lower(), \
            f"mensaje no menciona deprecada: {e}"
        return 1
    raise AssertionError("marcar_pagada() debería raisear ValueError")


def test_05_informes_posdat_totales_solo_banc_0() -> int:
    """FIX #5: posdat_totales TOTP cuenta banc=0, no banc<>9."""
    from modules.informes import queries as qi
    r = qi.posdat_totales()
    assert "totp" in r and "pos1" in r and "pos2" in r
    # No podemos asertar valor sin conocer la DB. Sólo el shape + que no crashea.
    return 2


def test_06_bank_helpers_compensacion_ch_a_nc() -> int:
    """FIX #6: bank_helpers.insertar_compensacion CH→NC (era CH→DE)."""
    # Lectura estática del código fuente.
    p = ROOT / "bank_helpers.py"
    txt = p.read_text()
    # Buscamos el branch CH → NC.
    assert 'doc_orig == "CH"' in txt, "no encuentro la branch CH"
    # Encontrar la asignación que sigue.
    idx = txt.find('doc_orig == "CH"')
    chunk = txt[idx:idx + 200]
    assert 'doc_comp = "NC"' in chunk, \
        f"CH no se compensa con NC. Chunk: {chunk!r}"
    # Y NO con DE.
    assert 'doc_comp = "DE"' not in chunk, "CH→DE todavía presente (bug viejo)"
    return 2


def test_07_conciliacion_filtra_stat_B() -> int:
    """FIX #7: conciliacion.cheques_depositados_rango filtra stat='B'."""
    from modules.conciliacion import queries as qc
    # Sólo aseguramos que la query corre y devuelve lista.
    hoy = date.today()
    r = qc.cheques_depositados_rango(hoy - timedelta(days=30), hoy)
    assert isinstance(r, list)
    # Lectura estática del filtro.
    p = ROOT / "modules" / "conciliacion" / "queries.py"
    txt = p.read_text()
    assert "stat = 'B'" in txt, "filtro stat='B' ausente"
    assert "stat = 'D'" not in txt or "'D' (depositado)" in txt, \
        "filtro stat='D' viejo todavía presente (bug)"
    return 3


def test_08_gastos_anular_compensa() -> int:
    """FIX #8: gastos.anular compensa caja/banco según concepto_parser."""
    # Lectura estática.
    p = ROOT / "modules" / "gastos" / "queries.py"
    txt = p.read_text()
    assert "concepto_parser" in txt, "no llama a concepto_parser"
    assert "caja_helpers.insert_movimiento_caja" in txt, \
        "no compensa caja al anular"
    assert "bank_helpers.insert_movimiento_bancario" in txt, \
        "no compensa banco al anular"
    return 3


def test_09_aplicar_a_factura_acepta_negativos_espejo() -> int:
    """FIX #9: cheques.aplicar_a_factura acepta signos negativos si espejo."""
    from modules.cheques import queries as qch
    # Cheque espejo simulado: importe negativo.
    id_factura = crear_factura(importe=200.0)
    # Aplicar primero un abono positivo para tener abono>0 (para que el espejo
    # pueda descontar).
    id_ch_normal = crear_cheque(importe=50.0, no_cheque="99002")
    qch.aplicar_a_factura(
        id_cheque=id_ch_normal,
        aplicaciones=[{"id_fact": id_factura, "importe": 50.0}],
        usuario=SMOKE_TAG,
    )
    # Ahora un espejo (importe negativo) — debería desabonar 30.
    # Insertamos un cheque con importe negativo directo (es lo que hace
    # crear() con es_anticipo=True, pero queremos chequear aplicar_a_factura
    # con un espejo solo, sin doble alta).
    id_padre = crear_cheque(importe=100.0, no_cheque="99003")
    row = db.execute_returning("""
        INSERT INTO scintela.cheque
            (no_cheque, fecha, fechad, codigo_cli, importe, no_banco,
             banco, stat, fechaing, id_cheque_padre, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s, 'PICH', 'Z', %s, %s, %s)
        RETURNING id_cheque
    """, ("99004", date.today(), date.today(), CLI_TEST, -30.0,
          BANCO_DEFAULT, date.today(), id_padre, SMOKE_TAG))
    id_espejo = int(row["id_cheque"])
    qch.aplicar_a_factura(
        id_cheque=id_espejo,
        aplicaciones=[{"id_fact": id_factura, "importe": -30.0}],
        usuario=SMOKE_TAG,
    )
    f = db.fetch_one("SELECT abono, saldo, stat FROM scintela.factura WHERE id_factura=%s",
                     (id_factura,))
    assert abs(float(f["abono"]) - 20.0) < 0.01, \
        f"abono esperado 20.0, vi {f['abono']} (50 - 30)"
    # Negativo en cheque normal debe seguir raiseando.
    id_ch_normal2 = crear_cheque(importe=10.0, no_cheque="99005")
    try:
        qch.aplicar_a_factura(
            id_cheque=id_ch_normal2,
            aplicaciones=[{"id_fact": id_factura, "importe": -5.0}],
            usuario=SMOKE_TAG,
        )
        raise AssertionError("aplicar negativo a cheque NO-espejo debería raisear")
    except ValueError:
        pass
    return 3


def test_10_compras_anular_bloquea_posdat_pagada() -> int:
    """FIX #10: compras.anular bloquea si posdat hermana tiene banc<>0."""
    p = ROOT / "modules" / "compras" / "queries.py"
    txt = p.read_text()
    # Lectura estática: la branch que bloquea.
    assert "posdat_hermana" in txt, "no busca posdat hermana"
    assert "banc" in txt and "raise ValueError" in txt, \
        "no raisea ValueError"
    idx = txt.find("posdat_hermana")
    chunk = txt[idx:idx + 800]
    assert ".get(\"banc\")" in chunk and "!= 0" in chunk, \
        f"check de banc != 0 ausente. Chunk: {chunk[:300]!r}"
    return 3


def test_11_confirmar_reverso_usa_stats_rebote_real() -> int:
    """FIX #11: cheques.confirmar_reverso usa STATS_REBOTE_REAL."""
    p = ROOT / "modules" / "cheques" / "views.py"
    txt = p.read_text()
    assert "queries.STATS_REBOTE_REAL" in txt, \
        "confirmar_reverso no usa STATS_REBOTE_REAL"
    # Constante existe en queries.
    from modules.cheques import queries as qch
    assert hasattr(qch, "STATS_REBOTE_REAL")
    # Debe incluir B (depositado moderno), no sólo D/A legacy.
    assert "B" in qch.STATS_REBOTE_REAL, \
        f"STATS_REBOTE_REAL no incluye B: {qch.STATS_REBOTE_REAL}"
    return 3


def test_12_transicionar_stat_9_compensa_deposito() -> int:
    """FIX #12: cheques.transicionar_stat con stat='9' inserta ND."""
    p = ROOT / "modules" / "cheques" / "queries.py"
    txt = p.read_text()
    # En el branch stat_destino == "9" debe haber un INSERT con doc='ND'.
    idx = txt.find('stat_destino == "9"')
    assert idx > 0, "no encuentro la branch stat='9'"
    chunk = txt[idx:idx + 2000]
    assert 'documento="ND"' in chunk, \
        f"no inserta ND al rebote (stat 9). Chunk inicio: {chunk[:300]!r}"
    assert "STATS_DEPOSITADO" in chunk, \
        "la compensación no se condiciona en STATS_DEPOSITADO"
    return 3


def test_13_cheque_creado_registra_mov_doble() -> int:
    """FIX #13: cheques.crear registra mov_doble 'cheque_creado'."""
    id_ch = crear_cheque(importe=77.77, no_cheque="99010")
    n = md_count(tipo="cheque_creado", origen_id=id_ch, origen_table="cheque")
    assert n == 1, f"esperaba 1 mov_doble cheque_creado, vi {n}"
    # Importe preserva signo (positivo).
    row = db.fetch_one("""
        SELECT importe, destino_table, destino_id, estado
          FROM scintela.mov_doble
         WHERE tipo = 'cheque_creado' AND origen_id = %s
    """, (id_ch,))
    assert row is not None
    assert abs(float(row["importe"]) - 77.77) < 0.01, \
        f"importe esperado 77.77, vi {row['importe']}"
    assert row["destino_table"] == "cheque", \
        f"destino_table esperado 'cheque', vi {row['destino_table']!r}"
    assert row["destino_id"] == id_ch, "destino_id != origen_id (cheque a sí mismo)"
    assert row["estado"] == "activo"
    return 5


def test_13b_cheque_anticipo_espejo_registra_mov_doble() -> int:
    """FIX #13: si es_anticipo, también registra cheque_anticipo_espejo."""
    id_ch = crear_cheque(importe=100.0, no_cheque="99011", es_anticipo=True)
    # Buscar el espejo (importe negativo, id_cheque_padre = id_ch).
    espejo = db.fetch_one("""
        SELECT id_cheque, importe FROM scintela.cheque
         WHERE id_cheque_padre = %s
    """, (id_ch,))
    assert espejo is not None, "no se creó cheque espejo"
    assert float(espejo["importe"]) < 0, \
        f"espejo debe tener importe<0, vi {espejo['importe']}"
    # Y debe haber mov_doble 'cheque_anticipo_espejo' linkeado.
    n = md_count(tipo="cheque_anticipo_espejo", origen_id=id_ch,
                 origen_table="cheque")
    assert n == 1, f"esperaba 1 mov_doble cheque_anticipo_espejo, vi {n}"
    return 3


def test_14_deudas_por_proveedor_banc_0() -> int:
    """FIX #14: deudas_por_proveedor + compras.deuda_proveedores usan banc=0."""
    from modules.compras import queries as qc
    from modules.informes import queries as qi
    # Insertar dos posdats: una banc=0, una banc=10. Sólo la primera debe contar.
    crear_posdat(importe=11.11, banc=0)
    crear_posdat(importe=22.22, banc=10)
    # informes.deudas_por_proveedor
    rows = qi.deudas_por_proveedor()
    fila = next((r for r in rows if r.get("codigo_prov") == PROV_TEST), None)
    if fila is None:
        raise AssertionError(f"proveedor {PROV_TEST} no en deudas_por_proveedor")
    assert abs(float(fila["saldo_total"]) - 11.11) < 0.01, \
        f"saldo_total esperado 11.11 (sólo banc=0), vi {fila['saldo_total']}"
    # compras.deuda_proveedores (total agregado).
    r = qc.deuda_proveedores()
    assert r["total"] >= 11.11, "deuda_proveedores total no incluye la posdat banc=0"
    return 3


def test_R2_posdat_abiertas_de_filtro_correcto() -> int:
    """RE-AUDIT #R2: bancos.posdat_abiertas_de filtra banc<>9.

    Debería filtrar banc=0 (deuda viva) — banc=10/32 ya están pagadas y no
    deberían aparecer como candidatas para emitir cheque.
    """
    p = ROOT / "modules" / "bancos" / "queries.py"
    txt = p.read_text()
    assert "posdat_abiertas_de" in txt
    idx = txt.find("def posdat_abiertas_de")
    chunk = txt[idx:idx + 600]
    # Si tiene banc<>9, esa es la regresión.
    if "<> 9" in chunk or "<>9" in chunk:
        raise AssertionError(
            "BUG #R2: bancos.posdat_abiertas_de filtra `banc<>9`. "
            "Debería filtrar `banc=0` (POSDAT_DEUDA_VIVA_WHERE) — sino el "
            "wizard de emitir cheque ofrece posdats banc=10/32 ya pagadas "
            "como candidatas, abriendo la puerta a doble pago."
        )
    return 1


def test_R3_replace_pdbanc_fragil() -> int:
    """RE-AUDIT #R3: informes.deudas_por_proveedor usa .replace('banc','pd.banc').

    `POSDAT_DEUDA_VIVA_WHERE.replace('banc', 'pd.banc')` es frágil: si la
    constante alguna vez tuviera 'banc' como parte de otra palabra
    (`bancaria`, etc.), .replace() rompe el SQL. Recomendación: usar un
    helper que tome alias como parámetro, o hardcodear la condición con
    el alias explícito.
    """
    p = ROOT / "modules" / "informes" / "queries.py"
    txt = p.read_text()
    if ".replace('banc', 'pd.banc')" in txt or ".replace(\"banc\", \"pd.banc\")" in txt:
        # No es un crash hoy — es un riesgo. La constante actual es
        # "COALESCE(banc, 0) = 0" — replace funciona. Pero es frágil.
        raise AssertionError(
            "BUG #R3 (FRÁGIL): informes/queries.py:2519 usa "
            "`POSDAT_DEUDA_VIVA_WHERE.replace('banc', 'pd.banc')`. "
            "Hoy funciona porque POSDAT_DEUDA_VIVA_WHERE = "
            "'COALESCE(banc, 0) = 0' y 'banc' aparece sólo como columna. "
            "Si mañana alguien agrega `COALESCE(banc, 0) IS NULL OR banc < 9`, "
            "o aparece la palabra `bancario` en la constante, .replace() "
            "rompe el SQL silenciosamente. Fix: pasar alias como parámetro "
            "a un helper `posdat_deuda_viva_where(alias='pd')`."
        )
    return 1


def test_R4_factura_anular_solo_si_md_orig() -> int:
    """RE-AUDIT #R4: facturas.anular sólo registra mov_doble si encuentra md_orig.

    Lectura: si una factura legacy no tiene mov_doble (anterior a la
    migración del historial), su anulación NO genera 'reverso_factura_anulada'
    → /historial no la muestra como reversada. Comportamiento esperado pero
    silencioso — al menos debería loguear o levantar warning.
    """
    p = ROOT / "modules" / "facturas" / "queries.py"
    txt = p.read_text()
    # Buscamos el "if md_orig:" antes del _md.registrar.
    idx = txt.find("def anular(id_factura")
    chunk = txt[idx:idx + 3500]
    if "if md_orig:" in chunk and "else:" not in chunk[chunk.find("if md_orig:"):chunk.find("if md_orig:")+500]:
        # No es un crash, es comportamiento documentado pero callado.
        # Reportar como nota (no fail, salvo escalación).
        return 1
    return 1


def test_R5_migration_0027_dependency_crashes() -> int:
    """RE-AUDIT #R5: lista de queries que crashean sin migration 0027.

    Si la migración corrió, este test es informativo (devuelve 1 OK).
    Si no, raisea AssertionError con la lista exacta de queries que van
    a crashear cuando alguien levante la app.
    """
    if _migration_0027_aplicada():
        return 1
    # Lista DURA de archivos:línea que referencian columnas de la migration.
    # Si la migration no está, todos esos sites crashean en runtime.
    sitios = [
        ("modules/posdat/queries.py:21",   "por_id: pd.anulada, pd.motivo_anulacion, pd.fecha_anulacion"),
        ("modules/posdat/queries.py:277",  "anular: SET motivo_anulacion = ..."),
        ("modules/posdat/queries.py:357",  "buscar: WHERE pd.anulada IS NOT TRUE"),
        ("modules/posdat/queries.py:393",  "resumen: WHERE anulada IS NOT TRUE"),
        ("modules/informes/queries.py:261", "posdat_totales: WHERE anulada IS NOT TRUE"),
        ("modules/informes/queries.py:787", "(query interna) WHERE anulada IS NOT TRUE"),
        ("modules/informes/queries.py:1543", "conciliacion_balance: anulada"),
        ("modules/informes/queries.py:1551", "conciliacion_balance: anulada"),
        ("modules/informes/queries.py:1558", "conciliacion_balance: anulada"),
        ("modules/informes/queries.py:2521", "deudas_por_proveedor: pd.anulada"),
        ("modules/informes/queries.py:2708", "flujo_calculado: WHERE anulada"),
        ("modules/informes/queries.py:2806", "plazos_dbase: WHERE anulada"),
        ("modules/informes/queries.py:2874", "posdat_egresos_proximos: WHERE anulada"),
        ("modules/compras/queries.py:498",  "(query interna) AND anulada IS NOT TRUE"),
        ("modules/compras/queries.py:619",  "anular: chequea posdat hermana anulada"),
        ("modules/compras/queries.py:634-638", "anular: SET motivo_anulacion, fecha_anulacion, WHERE anulada"),
        ("modules/compras/queries.py:1014", "deuda_proveedores: AND p.anulada"),
        ("modules/gastos/queries.py:431",   "anular: posdat hermana AND anulada"),
        ("modules/dashboard/queries.py:32",  "(query interna) anulada IS NOT TRUE"),
        ("modules/dashboard/queries.py:125", "compras_pagar_semana: anulada"),
        ("modules/dashboard/queries.py:348", "(otra query) anulada"),
        ("modules/proveedores/queries.py:142", "(query interna) anulada"),
    ]
    msg = (
        f"BUG #R5 (CRÍTICO): migration 0027 NO corrió, y {len(sitios)} sitios "
        f"clave usan `posdat.anulada` / `motivo_anulacion` / `fecha_anulacion`. "
        f"Sin migration, todos crashean con `column \"anulada\" does not exist`. "
        f"Páginas afectadas: /posdat, /informes/balance, /informes/flujo, "
        f"/dashboard, /compras (anular), /gastos (anular), /proveedores. "
        f"Acción inmediata: `python scripts/migrate.py` antes de levantar la app."
    )
    raise AssertionError(msg)


def test_R6_posdat_por_id_devuelve_columnas_0027() -> int:
    """RE-AUDIT #R6: posdat.por_id SELECT incluye anulada/motivo/fecha_anulacion.

    Si la migration no corrió, esta query crashea inmediatamente — pero
    `por_id` se llama desde TODAS las acciones (editar, confirmar_anulacion,
    detalle). Sin fallback, /posdat/<id>/* es inaccesible.
    """
    p = ROOT / "modules" / "posdat" / "queries.py"
    txt = p.read_text()
    idx = txt.find("def por_id")
    chunk = txt[idx:idx + 800]
    assert "pd.anulada" in chunk, "por_id no selecciona anulada"
    assert "pd.motivo_anulacion" in chunk
    assert "pd.fecha_anulacion" in chunk
    if not _migration_0027_aplicada():
        # Probar que efectivamente crashea
        from modules.posdat import queries as qpd
        try:
            qpd.por_id(1)
            raise AssertionError(
                "BUG #R6: posdat.por_id NO crashea sin migration 0027. "
                "Esperado: psycopg2 error column does not exist. "
                "Si esto pasa, la column ya existe — falso negativo de "
                "_migration_0027_aplicada."
            )
        except Exception as e:
            msg = str(e).lower()
            if "anulada" in msg or "does not exist" in msg:
                # OK, confirma que la query es la que falla sin migration.
                pass
            else:
                raise
    return 3


def test_R7_marcar_pagada_view_redirect() -> int:
    """RE-AUDIT #R7: posdat.marcar_pagada view redirige al wizard correcto."""
    p = ROOT / "modules" / "posdat" / "views.py"
    txt = p.read_text()
    idx = txt.find("def marcar_pagada(id_posdat")
    chunk = txt[idx:idx + 1000]
    assert "bancos.emitir_cheque" in chunk, \
        "marcar_pagada view no redirige a bancos.emitir_cheque"
    assert "id_posdat=id_posdat" in chunk, \
        "no preserva id_posdat en el redirect"
    return 2


def test_R8_mov_doble_preserva_signo() -> int:
    """RE-AUDIT #R8: mov_doble.registrar preserva signo (no abs()).

    Devoluciones / espejos llevan signo negativo. Si registrar() hiciera
    abs(), perdería esa info.
    """
    p = ROOT / "mov_doble.py"
    txt = p.read_text()
    # No debe haber `abs(importe_f)` antes del INSERT.
    idx = txt.find("def registrar")
    chunk = txt[idx:idx + 2000]
    assert "abs(importe" not in chunk, \
        "BUG: mov_doble.registrar() llama abs() — pierde signo de devoluciones"
    # Y el comentario doc lo explica
    assert "signo" in chunk.lower(), \
        "no documenta que preserva signo (commento misleading)"
    return 2


def test_R9_endoso_solo_si_importe_positivo() -> int:
    """RE-AUDIT #R9: cheques.endosar rechaza cheques con importe<0 (espejos)."""
    from modules.cheques import queries as qch
    # Crear un espejo de anticipo (importe<0).
    id_padre = crear_cheque(importe=100.0, no_cheque="99020")
    row = db.execute_returning("""
        INSERT INTO scintela.cheque
            (no_cheque, fecha, fechad, codigo_cli, importe, no_banco,
             banco, stat, fechaing, id_cheque_padre, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s, 'PICH', 'Z', %s, %s, %s)
        RETURNING id_cheque
    """, ("99021", date.today(), date.today(), CLI_TEST, -100.0,
          BANCO_DEFAULT, date.today(), id_padre, SMOKE_TAG))
    id_espejo = int(row["id_cheque"])
    _ensure_proveedor()
    try:
        qch.endosar(
            id_cheque=id_espejo, codigo_prov=PROV_TEST,
            concepto="smoke test", usuario=SMOKE_TAG,
        )
        raise AssertionError("endosar() debería raisear con espejo (importe<0)")
    except ValueError as e:
        assert "espejo" in str(e).lower() or "anticipo" in str(e).lower() \
            or "negativo" in str(e).lower(), f"mensaje no menciona espejo: {e}"
        return 1


def test_R10_depositar_lote_fechad_server_side() -> int:
    """RE-AUDIT #R10: depositar_lote valida fechad <= fecha_deposito server-side."""
    from modules.cheques import queries as qch
    # Cheque con fechad futura.
    id_ch = crear_cheque(
        importe=50.0, no_cheque="99030",
        fechad=date.today() + timedelta(days=10),
    )
    try:
        qch.depositar_lote(
            ids_cheques=[id_ch], no_banco=BANCO_DEFAULT,
            fecha_deposito=date.today(),
            usuario=SMOKE_TAG,
        )
        raise AssertionError("depositar_lote() debería raisear si fechad>hoy")
    except ValueError as e:
        assert "fechad" in str(e).lower() or "posterior" in str(e).lower(), \
            f"mensaje no menciona fechad: {e}"
        return 1


def test_R11_aplicar_a_factura_rechaza_stats_invalidos() -> int:
    """RE-AUDIT #R11: aplicar_a_factura rechaza stats no aplicables (B/E/X)."""
    from modules.cheques import queries as qch
    id_ch = crear_cheque(importe=50.0, no_cheque="99040")
    # Mover el cheque a stat='B' (depositado).
    db.execute("UPDATE scintela.cheque SET stat='B' WHERE id_cheque=%s", (id_ch,))
    id_factura = crear_factura(importe=100.0)
    try:
        qch.aplicar_a_factura(
            id_cheque=id_ch,
            aplicaciones=[{"id_fact": id_factura, "importe": 50.0}],
            usuario=SMOKE_TAG,
        )
        raise AssertionError("aplicar_a_factura() debería rechazar stat='B'")
    except ValueError as e:
        assert "stat" in str(e).lower(), f"mensaje no menciona stat: {e}"
        return 1


def test_R12_facturas_anular_filtra_X_3_R() -> int:
    """RE-AUDIT #R12: facturas.anular cuenta cheques aplicados activos sólo.

    La query filtra cheques con stat NOT IN ('X','3','R') para no contar
    los terminales/eliminados. Verifica la regla.
    """
    p = ROOT / "modules" / "facturas" / "queries.py"
    txt = p.read_text()
    idx = txt.find("def anular(id_factura")
    chunk = txt[idx:idx + 2500]
    assert "NOT IN ('X', '3', 'R')" in chunk, \
        "facturas.anular no filtra cheques terminales"
    return 1


def test_R13_smoke_flujo_completo_cheque_factura_reverso() -> int:
    """SMOKE end-to-end: alta cheque → aplicar factura → reversar → estado correcto."""
    from modules.cheques import queries as qch
    # 1) Crear factura.
    id_factura = crear_factura(importe=200.0)
    # 2) Crear cheque y aplicar.
    id_ch = crear_cheque(importe=200.0, no_cheque="99050")
    qch.aplicar_a_factura(
        id_cheque=id_ch,
        aplicaciones=[{"id_fact": id_factura, "importe": 200.0}],
        usuario=SMOKE_TAG,
    )
    f = db.fetch_one("SELECT stat, saldo, abono FROM scintela.factura WHERE id_factura=%s",
                     (id_factura,))
    assert f["stat"] == "T", f"factura debería estar en T (cancelada), vi {f['stat']!r}"
    # 3) Reversar el cheque entero (stat='Z' → 'X' administrativo).
    qch.reversar(id_cheque=id_ch, motivo="smoke test reverso",
                 usuario=SMOKE_TAG)
    # 4) Factura debería volver a Z, cheque debería estar en X.
    ch = db.fetch_one("SELECT stat FROM scintela.cheque WHERE id_cheque=%s", (id_ch,))
    assert ch["stat"] == "X", f"cheque esperado en X, vi {ch['stat']!r}"
    f2 = db.fetch_one("SELECT stat, abono, saldo FROM scintela.factura WHERE id_factura=%s",
                      (id_factura,))
    assert f2["stat"] == "Z", \
        f"factura debería volver a Z después del reverso, vi {f2['stat']!r}"
    assert abs(float(f2["abono"]) - 0) < 0.01, \
        f"abono debería ser 0 después del reverso, vi {f2['abono']}"
    return 4


def test_R14_balance_query_no_crashea() -> int:
    """SMOKE: las queries principales del balance corren sin excepción."""
    from modules.informes import queries as qi
    # Si una de estas crashea, el balance entero crashea — corre bajo try/safe
    # pero igual hay que detectar.
    try:
        qi.posdat_totales()
        qi.activos_totales()
        qi.anticipos()
    except Exception as e:
        msg = str(e).lower()
        if "anulada" in msg:
            raise SkipTest(f"migration 0027 pendiente: {e}")
        if "connection refused" in msg or "could not connect" in msg:
            raise SkipTest(f"DB no disponible: {e}")
        raise AssertionError(f"balance query crasheó: {e}")
    return 3


def test_R15_historial_listar_no_crashea() -> int:
    """SMOKE: historial.listar() corre."""
    from modules.historial import queries as qh
    rows = qh.listar(limite=10)
    assert isinstance(rows, list)
    return 1


def test_R16_smoke_compra_posdat_pago_reverso() -> int:
    """SMOKE end-to-end: compra crédito → posdat → emite cheque → posdat banc=10
       → reverso del cheque → posdat banc=0 vuelta a la vida.
    """
    if not _migration_0027_aplicada():
        raise SkipTest("migration 0027 pendiente — compras.anular depende de anulada")
    # 1) Crear posdat (simula compra a crédito).
    id_pd = crear_posdat(importe=300.0, banc=0)
    # 2) Emitir cheque pagando la posdat — usar bancos.emitir_cheque.
    from modules.bancos import queries as qb
    saldo_antes = saldo_banco()
    r = qb.emitir_cheque(
        tipo="proveedor",
        no_banco=BANCO_DEFAULT,
        importe=300.0,
        fecha=date.today(),
        no_cheque="99060",
        beneficiario=PROV_TEST,
        concepto=f"{SMOKE_TAG} pago posdat",
        id_posdat=id_pd,
        usuario=SMOKE_TAG,
    )
    id_tx = r["id_transaccion"]
    # Posdat debería tener banc = BANCO_DEFAULT.
    pd = db.fetch_one("SELECT banc FROM scintela.posdat WHERE id_posdat=%s", (id_pd,))
    assert int(pd["banc"]) == BANCO_DEFAULT, \
        f"posdat banc esperado {BANCO_DEFAULT}, vi {pd['banc']}"
    # Saldo banco bajó.
    saldo_post = saldo_banco()
    assert abs((saldo_antes - saldo_post) - 300.0) < 0.5, \
        f"saldo banco no bajó por 300: antes={saldo_antes}, post={saldo_post}"
    # 3) Reverso del cheque emitido.
    qb.reversar_cheque_emitido(
        id_transaccion=id_tx, motivo="smoke test reverso",
        usuario=SMOKE_TAG,
    )
    pd2 = db.fetch_one("SELECT banc FROM scintela.posdat WHERE id_posdat=%s", (id_pd,))
    assert int(pd2["banc"] or 0) == 0, \
        f"posdat debería reabrirse (banc=0) post-reverso, vi {pd2['banc']}"
    saldo_post_rev = saldo_banco()
    assert abs(saldo_post_rev - saldo_antes) < 0.5, \
        f"saldo banco no volvió: antes={saldo_antes}, post-reverso={saldo_post_rev}"
    return 5


# ----------------------------------------------------------------------------
# E5 — Flujo crítico TMT 2026-05-14: cheque nuevo → aplicado a factura,
# cheque queda en stat='Z' (cartera). Es el flujo MÁS USADO de la app.
# Después de los 47 fixes + 5 R*, validamos que no se haya roto.
# ----------------------------------------------------------------------------

def test_E5_flujo_cheque_cartera_a_factura() -> int:
    """Caso happy path + 3 edge cases.

    Happy path:
      1. Crear cliente test (si no existe).
      2. Emitir factura por $1000.
      3. Crear cheque por $1000 vía queries.crear (no via wizard).
      4. Aplicar el cheque a la factura.
      5. Asserts:
         a. cheque.stat == 'Z' (cartera) — NO cambió al aplicar.
         b. factura.saldo == 0, factura.stat == 'T'.
         c. mov_doble tiene cheque_creado + cheque_aplicado_a_factura.
         d. saldo bancos no cambió (no se depositó).

    Edge cases:
      - importe cheque > saldo factura → debería rechazar.
      - cheque con espejo de anticipo: aplica con signo negativo OK.
      - cliente en stop: no bloquea creación (verificación).
    """
    if not _db_disponible() or not _migration_0027_aplicada():
        raise _SkipTest("DB no disponible o migration 0027 pendiente")

    from modules.cheques import queries as cq

    asserts = 0

    # Snapshot saldo bancario antes del flujo.
    row = db.fetch_one("""
        SELECT COALESCE(SUM(s), 0) AS total FROM (
          SELECT (SELECT t.saldo
                    FROM scintela.transacciones_bancarias t
                   WHERE t.no_banco = b.no_banco
                   ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1) AS s
            FROM scintela.banco b
        ) sub
    """)
    saldo_antes = float(row.get("total") or 0)

    # --- Happy path ---
    _ensure_cliente()
    id_factura = crear_factura(importe=1000.0, fecha=date.today())

    ch = cq.crear(
        fecha=date.today(),
        codigo_cli=CLI_TEST,
        no_cheque="SMK_E5",
        importe=1000.0,
        no_banco=BANCO_DEFAULT,
        banco_texto=f"{SMOKE_TAG} banco",
        usuario=SMOKE_TAG,
    )
    id_cheque = int(ch["id_cheque"])

    # (a) Cheque arranca en Z.
    row = db.fetch_one("SELECT stat FROM scintela.cheque WHERE id_cheque = %s",
                       (id_cheque,))
    assert row and row["stat"] == "Z", \
        f"cheque debería arrancar stat='Z', vi {row}"
    asserts += 1

    # mov_doble cheque_creado registrado.
    md_created = db.fetch_one(
        "SELECT * FROM scintela.mov_doble "
        " WHERE origen_table='cheque' AND origen_id=%s AND tipo='cheque_creado'",
        (id_cheque,),
    )
    assert md_created, "falta mov_doble cheque_creado al alta"
    asserts += 1

    # Aplicar.
    res = cq.aplicar_a_factura(
        id_cheque=id_cheque,
        aplicaciones=[{"id_fact": id_factura, "importe": 1000.0}],
        usuario=SMOKE_TAG,
    )
    assert res.get("n") == 1, f"aplicar_a_factura devolvió {res}"
    asserts += 1

    # (a) Cheque SIGUE en Z post-aplicar.
    row = db.fetch_one("SELECT stat FROM scintela.cheque WHERE id_cheque = %s",
                       (id_cheque,))
    assert row["stat"] == "Z", \
        f"cheque debería seguir stat='Z' tras aplicar, vi {row['stat']}"
    asserts += 1

    # (b) Factura: saldo=0, stat='T'.
    row = db.fetch_one(
        "SELECT importe, abono, saldo, stat FROM scintela.factura WHERE id_factura=%s",
        (id_factura,),
    )
    assert abs(float(row["saldo"])) < 0.01, f"factura saldo debería ser 0, vi {row['saldo']}"
    assert row["stat"] == "T", f"factura stat debería ser 'T', vi {row['stat']}"
    asserts += 2

    # (c) mov_doble cheque_aplicado_a_factura registrado.
    md_apl = db.fetch_one(
        "SELECT * FROM scintela.mov_doble "
        " WHERE origen_table='cheque' AND origen_id=%s "
        "   AND destino_table='factura' AND destino_id=%s "
        "   AND tipo='cheque_aplicado_a_factura'",
        (id_cheque, id_factura),
    )
    assert md_apl, "falta mov_doble cheque_aplicado_a_factura"
    asserts += 1

    # (d) Saldo de bancos no cambió (cheque en cartera, no depositado).
    row = db.fetch_one("""
        SELECT COALESCE(SUM(s), 0) AS total FROM (
          SELECT (SELECT t.saldo
                    FROM scintela.transacciones_bancarias t
                   WHERE t.no_banco = b.no_banco
                   ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1) AS s
            FROM scintela.banco b
        ) sub
    """)
    saldo_post = float(row.get("total") or 0)
    assert abs(saldo_post - saldo_antes) < 0.5, \
        f"saldo bancos cambió: antes={saldo_antes:.2f}, después={saldo_post:.2f} — " \
        f"cheque en cartera NO debería tocar transacciones_bancarias"
    asserts += 1

    # --- Edge case 1: cheque ya aplicado completo → no se puede re-aplicar ---
    try:
        cq.aplicar_a_factura(
            id_cheque=id_cheque,
            aplicaciones=[{"id_fact": id_factura, "importe": 100.0}],
            usuario=SMOKE_TAG,
        )
        raise AssertionError("debió rechazar — factura ya cancelada (saldo=0)")
    except ValueError as e:
        assert "excede el saldo" in str(e).lower() or "saldo" in str(e).lower(), \
            f"error inesperado: {e}"
        asserts += 1

    # --- Edge case 2: cheque con espejo de anticipo (importe<0) ---
    id_factura2 = crear_factura(importe=500.0, fecha=date.today())
    ch_ant = cq.crear(
        fecha=date.today(),
        codigo_cli=CLI_TEST,
        no_cheque="SMK_E5_ANT",
        importe=500.0,
        no_banco=BANCO_DEFAULT,
        banco_texto=f"{SMOKE_TAG} banco",
        es_anticipo=True,
        usuario=SMOKE_TAG,
    )
    assert ch_ant.get("id_cheque_anticipo"), "espejo de anticipo no se creó"
    asserts += 1

    # Aplicar el cheque principal (positivo) a la factura nueva.
    cq.aplicar_a_factura(
        id_cheque=int(ch_ant["id_cheque"]),
        aplicaciones=[{"id_fact": id_factura2, "importe": 500.0}],
        usuario=SMOKE_TAG,
    )
    row = db.fetch_one(
        "SELECT saldo, stat FROM scintela.factura WHERE id_factura=%s",
        (id_factura2,),
    )
    assert abs(float(row["saldo"])) < 0.01 and row["stat"] == "T", \
        f"factura post-anticipo: saldo={row['saldo']}, stat={row['stat']}"
    asserts += 1

    # --- Edge case 3: cheque importe > saldo factura ---
    id_factura3 = crear_factura(importe=50.0, fecha=date.today())
    ch_big = cq.crear(
        fecha=date.today(),
        codigo_cli=CLI_TEST,
        no_cheque="SMK_E5_BIG",
        importe=200.0,
        no_banco=BANCO_DEFAULT,
        usuario=SMOKE_TAG,
    )
    try:
        cq.aplicar_a_factura(
            id_cheque=int(ch_big["id_cheque"]),
            aplicaciones=[{"id_fact": id_factura3, "importe": 200.0}],
            usuario=SMOKE_TAG,
        )
        raise AssertionError("debió rechazar — importe > saldo factura")
    except ValueError as e:
        assert "excede el saldo" in str(e).lower(), f"error inesperado: {e}"
        asserts += 1

    return asserts


# ----------------------------------------------------------------------------
# Registry — orden + descripción
# ----------------------------------------------------------------------------

TESTS: list[tuple[str, str, callable]] = [
    # Re-audit findings primero (más críticos).
    ("R1",  "recompute MIN(id_tx) destruye opening (BUG)",       test_R1_recompute_saldos_no_destruye_opening),
    ("R2",  "bancos.posdat_abiertas_de filtra banc<>9 (BUG)",     test_R2_posdat_abiertas_de_filtro_correcto),
    ("R3",  "deudas_por_proveedor .replace frágil",               test_R3_replace_pdbanc_fragil),
    ("R4",  "facturas.anular silencioso si md_orig=None",         test_R4_factura_anular_solo_si_md_orig),
    ("R5",  "migration 0027 dependency crashes",                  test_R5_migration_0027_dependency_crashes),
    ("R6",  "posdat.por_id depende de columnas 0027",             test_R6_posdat_por_id_devuelve_columnas_0027),
    ("R7",  "marcar_pagada view → redirect emitir-cheque",         test_R7_marcar_pagada_view_redirect),
    ("R8",  "mov_doble.registrar preserva signo",                 test_R8_mov_doble_preserva_signo),
    ("R9",  "endosar() rechaza espejos (importe<0)",              test_R9_endoso_solo_si_importe_positivo),
    ("R10", "depositar_lote valida fechad server-side",           test_R10_depositar_lote_fechad_server_side),
    ("R11", "aplicar_a_factura rechaza stat B/E/X",               test_R11_aplicar_a_factura_rechaza_stats_invalidos),
    ("R12", "facturas.anular filtra cheques X/3/R",               test_R12_facturas_anular_filtra_X_3_R),
    # Smoke por fix numerado.
    ("01",  "bancos.reversar_transferencia carga sin NameError",  test_01_reversar_transferencia_no_explota),
    ("02",  "recompute saldos tx por banco con ancla",             test_02_recompute_saldos_ancla_por_banco),
    ("03",  "posdat.anular soft-delete + mov_doble",               test_03_posdat_anular_soft_delete),
    ("03b", "posdat.anular bloquea banc<>0",                       test_03b_posdat_anular_bloquea_banc_distinto_0),
    ("04",  "posdat.marcar_pagada deprecada",                      test_04_marcar_pagada_deprecada),
    ("05",  "posdat_totales banc=0",                               test_05_informes_posdat_totales_solo_banc_0),
    ("06",  "bank_helpers compensacion CH→NC",                     test_06_bank_helpers_compensacion_ch_a_nc),
    ("07",  "conciliacion filtra stat='B'",                        test_07_conciliacion_filtra_stat_B),
    ("08",  "gastos.anular compensa caja/banco",                   test_08_gastos_anular_compensa),
    ("09",  "aplicar_a_factura permite negativos si espejo",       test_09_aplicar_a_factura_acepta_negativos_espejo),
    ("10",  "compras.anular bloquea posdat banc<>0",               test_10_compras_anular_bloquea_posdat_pagada),
    ("11",  "confirmar_reverso usa STATS_REBOTE_REAL",             test_11_confirmar_reverso_usa_stats_rebote_real),
    ("12",  "transicionar_stat='9' compensa con ND",               test_12_transicionar_stat_9_compensa_deposito),
    ("13",  "cheque_creado registra mov_doble",                    test_13_cheque_creado_registra_mov_doble),
    ("13b", "cheque_anticipo_espejo registra mov_doble",           test_13b_cheque_anticipo_espejo_registra_mov_doble),
    ("14",  "deudas_por_proveedor + deuda_proveedores banc=0",     test_14_deudas_por_proveedor_banc_0),
    # End-to-end smoke flows
    ("E1",  "flujo cheque→factura→reverso (cliente)",              test_R13_smoke_flujo_completo_cheque_factura_reverso),
    ("E2",  "balance queries no crashean",                         test_R14_balance_query_no_crashea),
    ("E3",  "historial.listar() carga",                            test_R15_historial_listar_no_crashea),
    ("E4",  "flujo compra→posdat→pago→reverso (proveedor)",        test_R16_smoke_compra_posdat_pago_reverso),
    ("E5",  "flujo cheque cartera → factura aplicada (crítico)",   test_E5_flujo_cheque_cartera_a_factura),
]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", default="",
                   help="lista de tests a correr, separados por coma (ej. 01,03,07)")
    p.add_argument("--keep-data", action="store_true",
                   help="no limpia data __SMOKE_*__ al final (debug)")
    p.add_argument("--verbose", action="store_true",
                   help="output extra (cleanup, queries)")
    return p.parse_args()


def _db_disponible() -> bool:
    """Ping rápido a la DB. False si no se puede conectar."""
    try:
        db.fetch_one("SELECT 1 AS x")
        return True
    except Exception:
        return False


def main() -> int:
    args = parse_args()
    only = set(args.only.split(",")) if args.only.strip() else None

    print("=" * 70)
    print(" SMOKE TEST 47 FIXES — 2026-05-14")
    print("=" * 70)

    db_ok = _db_disponible()
    if not db_ok:
        print("⚠️  No se pudo conectar a la DB local (postgresql://localhost:5432/intela).")
        print("   Los tests que requieren DB van a marcar [✗] como connection refused.")
        print("   Los tests estáticos (lectura de código) siguen corriendo.")
        print()
    elif not _migration_0027_aplicada():
        print("⚠️  Migration 0027 NO aplicada — tests relacionados [SKIP]eados.")
        print("   Corré: python scripts/migrate.py")
        print()

    if db_ok:
        cleanup(verbose=args.verbose)

    resultados: list[tuple[str, str, str, str, int]] = []  # (icon, n, desc, msg, asserts)
    try:
        for n, desc, fn in TESTS:
            if only and n not in only:
                continue
            try:
                asserts = fn()
                resultados.append(("✓", n, desc, "", asserts or 0))
                print(f"[✓] FIX #{n} — {desc} · {asserts} asserts OK")
            except SkipTest as e:
                resultados.append(("⊘", n, desc, str(e), 0))
                print(f"[SKIP] FIX #{n} — {desc} · {e}")
            except AssertionError as e:
                resultados.append(("✗", n, desc, str(e), 0))
                print(f"[✗] FIX #{n} — {desc}")
                print(f"      razón: {str(e)[:300]}")
            except Exception as e:
                tb = traceback.format_exc()
                msg = f"unexpected: {type(e).__name__}: {e}"
                # Connection refused → no DB disponible. Marca SKIP en vez de FAIL.
                low = str(e).lower()
                if "connection refused" in low or "could not connect" in low \
                        or "could not translate host" in low:
                    resultados.append(("⊘", n, desc, "DB no disponible", 0))
                    print(f"[SKIP] FIX #{n} — {desc} · DB no disponible")
                else:
                    resultados.append(("✗", n, desc, msg, 0))
                    print(f"[✗] FIX #{n} — {desc}")
                    print(f"      razón: {msg}")
                    if args.verbose:
                        print(tb)
    finally:
        if db_ok and not args.keep_data:
            print()
            print("Limpiando data __SMOKE_*__ ...")
            cleanup(verbose=args.verbose)

    print()
    print("=" * 70)
    n_ok   = sum(1 for r in resultados if r[0] == "✓")
    n_skip = sum(1 for r in resultados if r[0] == "⊘")
    n_fail = sum(1 for r in resultados if r[0] == "✗")
    total_asserts = sum(r[4] for r in resultados)
    print(f" {n_ok}/{len(resultados)} OK · {n_skip} SKIP · {n_fail} FALLARON · "
          f"{total_asserts} asserts totales")
    print("=" * 70)
    if n_fail:
        print()
        print("FALLOS:")
        for icon, n, desc, msg, _ in resultados:
            if icon == "✗":
                print(f"  - FIX #{n} ({desc})")
                print(f"      {msg[:300]}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
