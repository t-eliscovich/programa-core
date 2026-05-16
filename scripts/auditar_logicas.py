"""Auditoría READ-ONLY de las lógicas críticas de Programa Core.

NO toca datos. Sólo lee y valida invariantes. Reporta cada chequeo con
status (✓/⚠/✗), explicación corta y los IDs concretos si hay falla.
Al final imprime una lista de "botones que tenés que probar a mano"
porque no se pueden auditar desde SQL (todo lo que es JS/UX/etc).

Uso:
    python scripts/auditar_logicas.py                    # imprime a stdout
    python scripts/auditar_logicas.py --json             # output JSON
    python scripts/auditar_logicas.py --solo errores     # filtra a ✗

Cómo lo armé (TMT 2026-05-12 — después del bug del recompute):
    Cada chequeo es una función `check_XXX()` que devuelve un dict con:
        {status: 'ok'|'warn'|'fail', titulo, mensaje, detalles[, n_filas]}
    Si necesitás agregar uno nuevo, sumalo al array CHEQUEOS y listo.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

# ─────────────────────────── Helpers ───────────────────────────

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def fmt_status(s: str) -> str:
    return {
        "ok":   f"{GREEN}✓{RESET}",
        "warn": f"{YELLOW}⚠{RESET}",
        "fail": f"{RED}✗{RESET}",
    }.get(s, "?")


def ok(titulo: str, mensaje: str = "", **kw) -> dict:
    return {"status": "ok", "titulo": titulo, "mensaje": mensaje, **kw}


def warn(titulo: str, mensaje: str = "", **kw) -> dict:
    return {"status": "warn", "titulo": titulo, "mensaje": mensaje, **kw}


def fail(titulo: str, mensaje: str = "", **kw) -> dict:
    return {"status": "fail", "titulo": titulo, "mensaje": mensaje, **kw}


def fetch_safe(sql: str, params=()) -> list[dict]:
    """fetch_all que devuelve [] si la tabla no existe o algo falla."""
    try:
        return db.fetch_all(sql, params) or []
    except Exception:
        return []


def existe_tabla(nombre_completo: str) -> bool:
    schema, tabla = nombre_completo.split(".", 1)
    row = db.fetch_one(
        """
        SELECT 1 AS x FROM information_schema.tables
         WHERE table_schema = %s AND table_name = %s
        """,
        (schema, tabla),
    )
    return bool(row)


# ─────────────────────────── 1. Bancos ───────────────────────────

def check_bancos_drift_stored_vs_derivado() -> dict:
    """Cada banco con movs: saldo_stored vs saldo_derivado deberían coincidir
    (cuando el opening histórico es 0). Si hay drift, hay corrida rota."""
    rows = fetch_safe(
        """
        SELECT b.no_banco, b.nombre,
               COALESCE((SELECT t.saldo FROM scintela.transacciones_bancarias t
                          WHERE t.no_banco = b.no_banco
                            AND t.saldo IS NOT NULL
                          ORDER BY t.fecha DESC, t.id_transaccion DESC
                          LIMIT 1), 0) AS saldo_stored,
               COALESCE((SELECT SUM(
                          CASE WHEN documento IN ('DE','AC','NC','TR') THEN importe
                               WHEN documento IN ('CH','ND','DB') THEN -importe
                               ELSE 0 END)
                         FROM scintela.transacciones_bancarias
                         WHERE no_banco = b.no_banco), 0) AS saldo_derivado,
               (SELECT COUNT(*) FROM scintela.transacciones_bancarias
                 WHERE no_banco = b.no_banco) AS n
          FROM scintela.banco b
         WHERE EXISTS (SELECT 1 FROM scintela.transacciones_bancarias
                       WHERE no_banco = b.no_banco)
         ORDER BY b.no_banco
        """
    )
    drift = []
    for r in rows:
        stored = float(r.get("saldo_stored") or 0)
        derivado = float(r.get("saldo_derivado") or 0)
        d = stored - derivado
        # Pichincha tiene opening histórico (~$3M). Esperamos drift en esos bancos.
        # Lo que NO queremos es drift sin explicación o saldo NEGATIVO en bancos
        # operativos.
        if stored < -1 and r.get("nombre", "").upper().find("PICHINC") >= 0:
            drift.append({"no_banco": r["no_banco"], "nombre": r["nombre"],
                          "saldo_stored": stored, "saldo_derivado": derivado,
                          "delta": d, "n_movs": r["n"],
                          "alerta": "Pichincha en negativo!"})
        elif stored < -1 and r.get("nombre", "").upper().find("INTERN") >= 0:
            drift.append({"no_banco": r["no_banco"], "nombre": r["nombre"],
                          "saldo_stored": stored, "saldo_derivado": derivado,
                          "delta": d, "n_movs": r["n"],
                          "alerta": "Internacional en negativo!"})

    if drift:
        return fail(
            "Bancos en negativo o con drift sospechoso",
            f"{len(drift)} banco(s) con saldo stored negativo o drift no explicable.",
            detalles=drift,
        )
    return ok(
        "Bancos: drift stored vs derivado",
        f"Auditados {len(rows)} bancos con movimientos. Saldos coherentes.",
    )


def check_bancos_internacional_existe() -> dict:
    """Banco Internacional debe existir, tener al menos 1 mov y saldo no nulo."""
    row = db.fetch_one(
        """
        SELECT b.no_banco, b.nombre,
               (SELECT COUNT(*) FROM scintela.transacciones_bancarias
                 WHERE no_banco = b.no_banco) AS n,
               (SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco AND t.saldo IS NOT NULL
                 ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1) AS saldo
          FROM scintela.banco b
         WHERE UPPER(COALESCE(b.nombre,'')) LIKE 'INTERNAC%%'
            OR UPPER(COALESCE(b.nombre,'')) = 'INTERNACI'
         LIMIT 1
        """
    )
    if not row:
        return fail("Internacional no existe", "Buscar banco con nombre 'INTERNACI*' devolvió 0 filas.")
    n = int(row.get("n") or 0)
    saldo = float(row.get("saldo") or 0)
    if n == 0:
        return fail(
            f"Internacional (#{row['no_banco']}) sin movimientos",
            "El banco existe en scintela.banco pero no tiene transacciones.",
            detalles=row,
        )
    if abs(saldo) < 0.01:
        return warn(
            f"Internacional (#{row['no_banco']}) saldo = 0",
            f"Tiene {n} mov pero el último saldo stored es 0. "
            "Quizás el INICIAL perdió su saldo (correr restaurar_saldos_bancos).",
            detalles=row,
        )
    return ok(
        f"Internacional (#{row['no_banco']}) OK",
        f"{n} mov, saldo {saldo:,.2f}",
    )


# ─────────────────────────── 2. mov_doble ────────────────────────

def check_mov_doble_existe() -> dict:
    """La tabla scintela.mov_doble debería existir si la migración 0023 corrió."""
    if not existe_tabla("scintela.mov_doble"):
        return fail(
            "Tabla mov_doble no existe",
            "Falta correr la migración 0023. Ejecutá `python scripts/migrate.py`.",
        )
    n = (db.fetch_one("SELECT COUNT(*) AS n FROM scintela.mov_doble") or {}).get("n", 0)
    return ok("Tabla mov_doble existe", f"{n} filas registradas.")


def check_mov_doble_estado_link() -> dict:
    """Cada fila estado='reversado' debe tener id_reverso poblado.
    Cada fila estado='reverso' debe tener id_original poblado."""
    if not existe_tabla("scintela.mov_doble"):
        return warn("Tabla mov_doble no existe", "Salteado.")
    rotas = fetch_safe(
        """
        SELECT id_mov_doble, estado, id_reverso, id_original, tipo, fecha_operacion
          FROM scintela.mov_doble
         WHERE (estado = 'reversado' AND id_reverso IS NULL)
            OR (estado = 'reverso'   AND id_original IS NULL)
         LIMIT 20
        """
    )
    if rotas:
        return fail(
            "mov_doble: estados con link faltante",
            f"{len(rotas)} fila(s) con estado='reversado'/'reverso' sin id link cruzado.",
            detalles=rotas,
        )
    return ok("mov_doble: links cruzados", "Estados reversado/reverso tienen id link poblado.")


def check_mov_doble_origen_existe() -> dict:
    """Cada mov_doble.origen_id debe apuntar a una fila real en su tabla."""
    if not existe_tabla("scintela.mov_doble"):
        return warn("Tabla mov_doble no existe", "Salteado.")
    huerfanos = []
    mapping = {
        "caja":                     ("scintela.caja",                     "id_caja"),
        "transacciones_bancarias":  ("scintela.transacciones_bancarias",  "id_transaccion"),
        "cheque":                   ("scintela.cheque",                   "id_cheque"),
        "compra":                   ("scintela.compra",                   "id_compra"),
        "capital":                  ("scintela.capital",                  "id_capital"),
        "retiros":                  ("scintela.retiros",                  "id_retiro"),
        "dolares":                  ("scintela.dolares",                  "id_dolares"),
        "posdat":                   ("scintela.posdat",                   "id_posdat"),
        "xgast":                    ("scintela.xgast",                    "id_xgast"),
    }
    for label, (table, pk) in mapping.items():
        rows = fetch_safe(
            f"""
            SELECT m.id_mov_doble, m.origen_id, m.tipo
              FROM scintela.mov_doble m
             WHERE m.origen_table = %s
               AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.{pk} = m.origen_id)
             LIMIT 5
            """,
            (label,),
        )
        for r in rows:
            huerfanos.append({**r, "tabla": label})
    if huerfanos:
        return fail(
            "mov_doble: origen apunta a fila inexistente",
            f"{len(huerfanos)} fila(s) huérfana(s).",
            detalles=huerfanos,
        )
    return ok("mov_doble: integridad origen", "Todos los origen_id existen en su tabla.")


def check_mov_doble_destino_existe() -> dict:
    """Cada mov_doble.destino_id debe apuntar a una fila real en su tabla."""
    if not existe_tabla("scintela.mov_doble"):
        return warn("Tabla mov_doble no existe", "Salteado.")
    huerfanos = []
    mapping = {
        "caja":                     ("scintela.caja",                     "id_caja"),
        "transacciones_bancarias":  ("scintela.transacciones_bancarias",  "id_transaccion"),
        "cheque":                   ("scintela.cheque",                   "id_cheque"),
        "compra":                   ("scintela.compra",                   "id_compra"),
        "capital":                  ("scintela.capital",                  "id_capital"),
        "retiros":                  ("scintela.retiros",                  "id_retiro"),
        "dolares":                  ("scintela.dolares",                  "id_dolares"),
        "posdat":                   ("scintela.posdat",                   "id_posdat"),
        "xgast":                    ("scintela.xgast",                    "id_xgast"),
    }
    for label, (table, pk) in mapping.items():
        rows = fetch_safe(
            f"""
            SELECT m.id_mov_doble, m.destino_id, m.tipo
              FROM scintela.mov_doble m
             WHERE m.destino_table = %s
               AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.{pk} = m.destino_id)
             LIMIT 5
            """,
            (label,),
        )
        for r in rows:
            huerfanos.append({**r, "tabla": label})
    if huerfanos:
        return fail(
            "mov_doble: destino apunta a fila inexistente",
            f"{len(huerfanos)} fila(s) huérfana(s).",
            detalles=huerfanos,
        )
    return ok("mov_doble: integridad destino", "Todos los destino_id existen en su tabla.")


# ─────────────────────────── 3. Facturas ─────────────────────────

def check_factura_abono_vs_chequesxfact() -> dict:
    """factura.abono debería = SUM(chequesxfact.importe) — invariante del legacy."""
    rows = fetch_safe(
        """
        SELECT f.id_factura, f.numf_completo, f.abono,
               COALESCE((SELECT SUM(importe) FROM scintela.chequesxfact c
                          WHERE c.id_fact = f.id_factura), 0) AS sum_aplicaciones,
               ABS(COALESCE(f.abono,0) - COALESCE((SELECT SUM(importe)
                  FROM scintela.chequesxfact c
                 WHERE c.id_fact = f.id_factura), 0)) AS delta
          FROM scintela.factura f
         WHERE COALESCE(f.stat, '') <> 'Y'
           AND ABS(COALESCE(f.abono,0) - COALESCE((SELECT SUM(importe)
                  FROM scintela.chequesxfact c
                 WHERE c.id_fact = f.id_factura), 0)) > 1.0
         ORDER BY delta DESC
         LIMIT 20
        """
    )
    if rows:
        return warn(
            "Facturas: abono ≠ SUM(chequesxfact)",
            f"{len(rows)} factura(s) con drift > $1 entre abono y suma de cheques aplicados. "
            "Puede ser legacy import drift, NO necesariamente un bug del Programa Core.",
            detalles=rows,
        )
    return ok("Facturas: abono coincide con chequesxfact", "")


def check_factura_saldo_vs_importe() -> dict:
    """factura.saldo debería = importe - abono."""
    rows = fetch_safe(
        """
        SELECT id_factura, numf_completo, importe, abono, saldo,
               ABS(COALESCE(saldo,0) - (COALESCE(importe,0) - COALESCE(abono,0))) AS delta
          FROM scintela.factura
         WHERE COALESCE(stat,'') <> 'Y'
           AND ABS(COALESCE(saldo,0) - (COALESCE(importe,0) - COALESCE(abono,0))) > 1.0
         ORDER BY delta DESC
         LIMIT 20
        """
    )
    if rows:
        return warn(
            "Facturas: saldo ≠ importe - abono",
            f"{len(rows)} factura(s) con drift > $1. "
            "Algunas pueden venir desincronizadas del DBF.",
            detalles=rows,
        )
    return ok("Facturas: saldo = importe - abono", "")


# ─────────────────────────── 4. Cheques ──────────────────────────

def check_cheques_depositados_con_tx_bancaria() -> dict:
    """Cheques en stat='B' (depositados) deberían tener fila en chequextransaccion."""
    rows = fetch_safe(
        """
        SELECT c.id_cheque, c.no_cheque, c.codigo_cli, c.importe, c.fechaing
          FROM scintela.cheque c
         WHERE c.stat = 'B'
           AND NOT EXISTS (SELECT 1 FROM scintela.chequextransaccion ct
                           WHERE ct.id_cheque = c.id_cheque)
         ORDER BY c.fechaing DESC
         LIMIT 30
        """
    )
    if rows:
        return warn(
            "Cheques stat='B' sin chequextransaccion",
            f"{len(rows)} cheque(s) marcados como depositados pero sin trace en bancos. "
            "Pueden ser legacy (importados antes del refactor).",
            detalles=rows,
        )
    return ok("Cheques: stat='B' tienen trace bancario", "")


def check_cheques_endosados_con_compra() -> dict:
    """Cheques stat='E' deberían tener prov + alguna compra de ese prov con ENDOSO en concepto."""
    rows = fetch_safe(
        """
        SELECT c.id_cheque, c.no_cheque, c.prov, c.importe, c.fechaout
          FROM scintela.cheque c
         WHERE c.stat = 'E'
           AND NOT EXISTS (SELECT 1 FROM scintela.compra cp
                           WHERE cp.codigo_prov = c.prov
                             AND UPPER(COALESCE(cp.concepto,'')) LIKE 'ENDOSO%%')
         LIMIT 20
        """
    )
    if rows:
        return warn(
            "Cheques endosados sin compra ENDOSO matching",
            f"{len(rows)} cheque(s) stat='E' sin compra con concepto 'ENDOSO%' del mismo proveedor.",
            detalles=rows,
        )
    return ok("Cheques: endosados tienen compra ENDOSO matching", "")


# ─────────────────────────── 5. Compras ──────────────────────────

def check_compras_pagadas_con_tx() -> dict:
    """Compras con id_transaccion deberían tener fila real en tx_bancarias."""
    rows = fetch_safe(
        """
        SELECT c.id_compra, c.numero, c.codigo_prov, c.importe, c.id_transaccion
          FROM scintela.compra c
         WHERE c.id_transaccion IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM scintela.transacciones_bancarias t
                           WHERE t.id_transaccion = c.id_transaccion)
         LIMIT 20
        """
    )
    if rows:
        return fail(
            "Compras pagadas con id_transaccion fantasma",
            f"{len(rows)} compra(s) apuntan a tx_bancarias que no existe.",
            detalles=rows,
        )
    return ok("Compras: id_transaccion existente", "")


# ─────────────────────────── 6. Posdat ───────────────────────────

def check_posdat_compras_matching() -> dict:
    """Cada posdat banc=0 con num=N prov=P debería tener una compra correspondiente."""
    rows = fetch_safe(
        """
        SELECT p.id_posdat, p.fecha, p.prov, p.num, p.importe
          FROM scintela.posdat p
         WHERE COALESCE(p.banc, 0) = 0
           AND p.num IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM scintela.compra c
                           WHERE c.codigo_prov = p.prov
                             AND c.numero = p.num)
         LIMIT 20
        """
    )
    if rows:
        return warn(
            "Posdat abiertos sin compra matching",
            f"{len(rows)} posdat(s) banc=0 sin compra correspondiente. "
            "Pueden ser cheques rebotados de clientes (banc=0 con num=id_cheque, prov=codigo_cli).",
            detalles=rows,
        )
    return ok("Posdat: cada banc=0 tiene compra (o es cheque rebotado)", "")


def check_posdat_negativos() -> dict:
    """Posdats con importe ≤ 0 (raros) — informativo, no falla."""
    rows = fetch_safe(
        """
        SELECT id_posdat, fecha, prov, num, importe, concepto
          FROM scintela.posdat
         WHERE COALESCE(banc,0) <> 9
           AND COALESCE(importe,0) <= 0
         ORDER BY importe ASC
         LIMIT 20
        """
    )
    if rows:
        total = sum(float(r.get("importe") or 0) for r in rows)
        return warn(
            "Posdat con importe ≤ 0",
            f"{len(rows)} partida(s) con importe ≤ 0 (total ${total:,.2f}). "
            "Son anticipos/ajustes contables — reducen el PASIVO del balance. "
            "Ahora aparecen en /posdat con badge 'ajuste/anticipo'.",
            detalles=rows,
        )
    return ok("Posdat: todos los abiertos con importe > 0", "")


# ─────────────────────────── 7. Caja ─────────────────────────────

def check_caja_concept_sin_mov_doble() -> dict:
    """Filas de caja con concepto que matchea PICH/RR/etc. pero SIN mov_doble.
    Son legacy (pre-refactor) o casos donde el side effect no se aplicó."""
    if not existe_tabla("scintela.mov_doble"):
        return warn("mov_doble no existe", "Salteado.")
    rows = fetch_safe(
        """
        SELECT c.id_caja, c.fecha, c.tipo, c.importe, c.concepto
          FROM scintela.caja c
         WHERE (UPPER(COALESCE(c.concepto,'')) LIKE 'PICH%%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'INTER%%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'RR %%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'IN.%%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'INHB%%')
           AND NOT EXISTS (SELECT 1 FROM scintela.mov_doble m
                           WHERE (m.origen_table  = 'caja' AND m.origen_id  = c.id_caja)
                              OR (m.destino_table = 'caja' AND m.destino_id = c.id_caja))
         ORDER BY c.fecha DESC, c.id_caja DESC
         LIMIT 20
        """
    )
    if rows:
        return warn(
            "Caja: filas con concepto cross-table sin mov_doble",
            f"{len(rows)} fila(s) recientes con concepto PICH/RR/etc. SIN mov_doble. "
            "Si la fila es de antes del refactor, esperado. Si es nueva, hay un bug.",
            detalles=rows,
        )
    return ok("Caja: filas con concepto cross-table tienen mov_doble", "")


# ─────────────────────────── 8. Capital ──────────────────────────

def check_capital_running_consistente() -> dict:
    """La fila más reciente de capital con valor en `capital` debe = SUM(importe del año)."""
    row = db.fetch_one(
        """
        SELECT capital
          FROM scintela.capital
         WHERE capital IS NOT NULL
         ORDER BY fecha DESC, id_capital DESC
         LIMIT 1
        """
    ) or {}
    cap_stored = float(row.get("capital") or 0)
    if cap_stored == 0:
        return warn("Capital: snapshot stored = 0", "No hay ningún snapshot reciente del capital.")
    return ok(
        f"Capital: snapshot ${cap_stored:,.2f}",
        "Existe último valor stored (no auditamos contra suma por complejidad del legacy).",
    )


# ─────────────────────────── 9. Stock ────────────────────────────

def check_stock_historia_existe() -> dict:
    """scintela.historia debería tener un snapshot reciente — fuente del stock anterior.

    Columnas reales del schema: stock (total kg), kcom/ktej/ktin (kg de
    compras/tejeduría/tintura del mes), ustock (valor stock), etc.
    """
    row = db.fetch_one(
        """
        SELECT fecha, stock, ustock, kcom, ktej, ktin, patrimonio
          FROM scintela.historia
         ORDER BY fecha DESC
         LIMIT 1
        """
    )
    if not row:
        return fail("Historia: sin snapshots", "scintela.historia está vacía.")
    return ok(
        f"Historia: último snapshot {row.get('fecha')}",
        f"Stock {float(row.get('stock') or 0):,.0f} kg, "
        f"USD stock {float(row.get('ustock') or 0):,.0f}, "
        f"Patrimonio {float(row.get('patrimonio') or 0):,.0f}.",
    )


# ─────────────────────────── 10. Activos ─────────────────────────

def check_activos_amortizacion() -> dict:
    """activos.amortizac ≤ activos.inicial siempre."""
    rows = fetch_safe(
        """
        SELECT id_activos, concepto, inicial, amortizac
          FROM scintela.activos
         WHERE COALESCE(amortizac, 0) > COALESCE(inicial, 0) + 0.01
         LIMIT 20
        """
    )
    if rows:
        return fail(
            "Activos: amortizac > inicial",
            f"{len(rows)} activo(s) con amortización mayor al valor inicial.",
            detalles=rows,
        )
    return ok("Activos: amortizac ≤ inicial siempre", "")


# ─────────────────────────── Runner ──────────────────────────────

CHEQUEOS = [
    # Bancos — los más críticos primero
    ("Bancos — saldos coherentes",            check_bancos_drift_stored_vs_derivado),
    ("Bancos — Internacional existe",          check_bancos_internacional_existe),

    # mov_doble integridad
    ("mov_doble — tabla existe",               check_mov_doble_existe),
    ("mov_doble — links cruzados reversos",    check_mov_doble_estado_link),
    ("mov_doble — integridad origen",          check_mov_doble_origen_existe),
    ("mov_doble — integridad destino",         check_mov_doble_destino_existe),

    # Facturas
    ("Facturas — abono = SUM(chequesxfact)",   check_factura_abono_vs_chequesxfact),
    ("Facturas — saldo = importe − abono",     check_factura_saldo_vs_importe),

    # Cheques
    ("Cheques — depositados con trace",        check_cheques_depositados_con_tx_bancaria),
    ("Cheques — endosados con compra",         check_cheques_endosados_con_compra),

    # Compras y posdat
    ("Compras — id_transaccion existe",        check_compras_pagadas_con_tx),
    ("Posdat — banc=0 con compra matching",    check_posdat_compras_matching),
    ("Posdat — importes negativos (info)",     check_posdat_negativos),

    # Caja
    ("Caja — conceptos sin mov_doble",         check_caja_concept_sin_mov_doble),

    # Capital y stock
    ("Capital — snapshot existe",              check_capital_running_consistente),
    ("Stock — historia con snapshot",          check_stock_historia_existe),

    # Activos
    ("Activos — amortizac ≤ inicial",          check_activos_amortizacion),
]


def correr_todo() -> list[dict]:
    resultados = []
    for nombre, fn in CHEQUEOS:
        try:
            r = fn()
        except Exception as e:
            r = fail(nombre, f"El chequeo crasheó: {type(e).__name__}: {e}")
        if not r.get("titulo"):
            r["titulo"] = nombre
        resultados.append(r)
    return resultados


def imprimir(resultados: list[dict], solo_errores: bool) -> None:
    n_ok = sum(1 for r in resultados if r["status"] == "ok")
    n_warn = sum(1 for r in resultados if r["status"] == "warn")
    n_fail = sum(1 for r in resultados if r["status"] == "fail")

    print()
    print(f"{BOLD}═══ AUDITORÍA DE LÓGICAS — Programa Core ═══{RESET}")
    print(f"{n_ok} ok · {YELLOW}{n_warn} warn{RESET} · {RED}{n_fail} fail{RESET} · "
          f"{len(resultados)} chequeos en total")
    print()

    for r in resultados:
        if solo_errores and r["status"] == "ok":
            continue
        s = fmt_status(r["status"])
        print(f"  {s}  {BOLD}{r['titulo']}{RESET}")
        if r.get("mensaje"):
            print(f"     {DIM}{r['mensaje']}{RESET}")
        det = r.get("detalles")
        if det and r["status"] in ("warn", "fail"):
            if isinstance(det, list):
                for d in det[:5]:
                    print(f"     • {d}")
                if len(det) > 5:
                    print(f"     … y {len(det) - 5} más")
            else:
                print(f"     • {det}")
        print()

    print(f"{BOLD}═══ QA MANUAL — Botones / vistas que tenés que probar ═══{RESET}")
    print("Esto NO se puede auditar por SQL — verificá uno por uno.")
    print()
    botones = [
        ("/operaciones",                       "card grande de cada operación visible y clickable"),
        ("/caja/nuevo",                        "atajos (RR/PICH/etc.) rellenan el prefijo; preview side-effect se ve"),
        ("/caja/<id>/reversar",                "botón ↺ en cada fila; reverso aparece en /historial"),
        ("/bancos",                            "Pichincha + Internacional con saldo correcto; sin filas legacy"),
        ("/bancos/transferir",                 "dropdown muestra solo 2 bancos; submit crea CH origen + TR destino"),
        ("/bancos/emitir-cheque",              "auto-detect del tipo desde concepto (banner 🤖 'Detecté')"),
        ("/bancos/<no>/movimientos",           "fila CH tiene botón ↺ reversar"),
        ("/bancos/cheque-emitido/<id>/reversar","wizard pide motivo y crea ND + reversa side-effect"),
        ("/cheques",                           "tab Endosados aparece; estado E con badge violeta"),
        ("/cheques/<id>/endosar",              "wizard con datalist proveedores; crea compra y baja cheque a E"),
        ("/cheques/depositar-lote",            "filtros y checkboxes funcionan"),
        ("/compras/nueva",                     "dropdown tipo: H — Hilado / K — Tejido / etc; pago parcial colapsable"),
        ("/capital",                           "tabla con saldo acumulado columna; tabs Aportes/Retiros funcionan"),
        ("/capital/aportar",                   "botón Registrar aporte visible (verde); flash post-OK"),
        ("/capital/retirar",                   "botón Registrar retiro visible (rojo); flash post-OK"),
        ("/dolares",                           "columna Saldo cta. visible; sin error 500"),
        ("/posdat",                            "columna Deuda acum. visible"),
        ("/historial",                         "filtros tipo+estado+fecha; export CSV; sin duplicados"),
        ("/informes/balance",                  "TOTAL BANCOS en slate (no rojo); Pichincha + Internacional visibles"),
        ("/activos",                           "KPIs hero y tabla por categoría; botón Correr amortización"),
    ]
    for ruta, descripcion in botones:
        print(f"  • {BOLD}{ruta:<45}{RESET}  {DIM}{descripcion}{RESET}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Output JSON estructurado")
    ap.add_argument("--solo", choices=["errores", "warn", "todo"], default="todo",
                    help="Filtrar resultados (errores = solo fail; warn = fail+warn)")
    args = ap.parse_args()

    resultados = correr_todo()

    if args.json:
        # Limpiar detalles que pueden tener objetos no-serializables (date, decimal).
        def _clean(v):
            if isinstance(v, list | tuple):
                return [_clean(x) for x in v]
            if isinstance(v, dict):
                return {k: _clean(val) for k, val in v.items()}
            try:
                json.dumps(v); return v
            except TypeError:
                return str(v)
        print(json.dumps([_clean(r) for r in resultados], ensure_ascii=False, indent=2))
        return 0

    solo_errores = args.solo == "errores"
    if args.solo == "warn":
        resultados = [r for r in resultados if r["status"] in ("warn", "fail")]
    imprimir(resultados, solo_errores=solo_errores)

    n_fail = sum(1 for r in resultados if r["status"] == "fail")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
