"""Matcher bidireccional: extracto del banco (REAL) ↔ scintela.transacciones_bancarias (BANCSIS).

TMT 2026-05-22 — Para cada movimiento del banco real intentamos encontrar la
transacción equivalente en BANCSIS. Devolvemos 3 grupos:

    matched         — match exacto o probable (mismo signo C/D, monto ± $1, fecha ± 5 días)
    real_only       — solo está en REAL (no en BANCSIS)  → Mov 1 de la ecuación de saldo
    bancsis_only    — solo está en BANCSIS (no en REAL)  → Mov 2

Verificación de saldo:
    SALDO_REAL_final = SALDO_BANCSIS_final + Σ(real_only) - Σ(bancsis_only)
(tolerancia ± $100)

Mapeo de tipos:
    Banco REAL Tipo='C' (crédito → entra plata)  ↔  BANCSIS documento IN ('DE','TR','AC','NC')
    Banco REAL Tipo='D' (débito  → sale plata)   ↔  BANCSIS documento IN ('CH','ND','DB')

Persistencia: los matches confirmados quedan en scintela.banco_conciliacion_match
y NO vuelven a aparecer en sesiones siguientes (el matcher los excluye).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import db
from modules.conciliacion.parser_banco import MovBanco


# Mapping tipo banco ↔ documento BANCSIS
_DOCS_CREDITO = ("DE", "TR", "AC", "NC")  # entra plata
_DOCS_DEBITO = ("CH", "ND", "DB")          # sale plata
_BANCO_PICHINCHA_NO = 10  # confirmado en /bancos/10 (2026-05-22)


@dataclass
class MovBancsis:
    """Fila de scintela.transacciones_bancarias (BANCSIS)."""

    id_transaccion: int
    fecha: date
    documento: str
    concepto: str
    importe: float
    numreferencia: str
    no_banco: int
    saldo: float | None

    @property
    def tipo_real(self) -> str:
        """Mapeo inverso: ¿qué Tipo C/D del banco le correspondería?"""
        if self.documento in _DOCS_CREDITO:
            return "C"
        if self.documento in _DOCS_DEBITO:
            return "D"
        return "?"


@dataclass
class Match:
    """Un movimiento REAL del banco emparejado con un MovBancsis."""

    real: MovBanco
    bancsis: MovBancsis
    score: float       # 0 = perfecto. Mayor = más drift.
    razon: str         # explicación legible


@dataclass
class ConciliacionBanco:
    matches: list[Match] = field(default_factory=list)
    real_only: list[MovBanco] = field(default_factory=list)
    bancsis_only: list[MovBancsis] = field(default_factory=list)
    # Saldos del extracto
    saldo_real_final: Decimal = Decimal(0)
    saldo_real_fecha: date | None = None
    saldo_bancsis_final: float = 0.0
    saldo_bancsis_fecha: date | None = None
    # Totales por grupo (signados)
    total_real_only_signed: float = 0.0
    total_bancsis_only_signed: float = 0.0


def _tiene_migration_47() -> bool:
    """¿Corrió la migration 0047 (columnas deshecho_en/metodo)?

    Cacheado por proceso. Si la migration no corrió todavía, el código
    sigue andando con la lógica vieja (sin soft-undo, sin método).
    """
    if hasattr(_tiene_migration_47, "_cache"):
        return _tiene_migration_47._cache
    row = db.fetch_one(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'scintela'
           AND table_name = 'banco_conciliacion_match'
           AND column_name = 'deshecho_en'
        """
    )
    _tiene_migration_47._cache = bool(row)
    return _tiene_migration_47._cache


def _ya_conciliadas(no_banco: int, desde: date, hasta: date) -> tuple[set[int], set[tuple]]:
    """Devuelve:
        set de id_transaccion (BANCSIS) ya conciliados (y NO deshechos)
        set de firma REAL (fecha, documento, monto str, tipo) ya conciliados
    """
    filtro_undo = "AND deshecho_en IS NULL" if _tiene_migration_47() else ""
    rows = db.fetch_all(
        f"""
        SELECT id_transaccion, real_fecha, real_documento, real_monto, real_tipo
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s
           {filtro_undo}
           AND (real_fecha IS NULL OR real_fecha BETWEEN %s AND %s)
        """,
        (no_banco, desde - timedelta(days=30), hasta + timedelta(days=30)),
    ) or []
    ids_bancsis: set[int] = set()
    firmas_real: set[tuple] = set()
    for r in rows:
        if r.get("id_transaccion"):
            ids_bancsis.add(int(r["id_transaccion"]))
        if r.get("real_fecha") and r.get("real_documento"):
            firmas_real.add((
                r["real_fecha"],
                (r.get("real_documento") or "").strip(),
                f"{Decimal(str(r.get('real_monto') or 0)):.2f}",
                (r.get("real_tipo") or "").strip().upper(),
            ))
    return ids_bancsis, firmas_real


def cargar_bancsis(no_banco: int, desde: date, hasta: date) -> list[MovBancsis]:
    """Trae todas las transacciones BANCSIS del banco en el rango."""
    rows = db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, concepto, importe,
               numreferencia, no_banco, saldo
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND fecha BETWEEN %s AND %s
         ORDER BY fecha ASC, id_transaccion ASC
        """,
        (no_banco, desde, hasta),
    ) or []
    return [
        MovBancsis(
            id_transaccion=int(r["id_transaccion"]),
            fecha=r["fecha"],
            documento=str(r.get("documento") or "").strip().upper(),
            concepto=str(r.get("concepto") or "").strip(),
            importe=float(r.get("importe") or 0),
            numreferencia=str(r.get("numreferencia") or "").strip(),
            no_banco=int(r.get("no_banco") or 0),
            saldo=float(r.get("saldo")) if r.get("saldo") is not None else None,
        )
        for r in rows
    ]


def _firma_real(m: MovBanco) -> tuple:
    return (
        m.fecha,
        m.documento.strip(),
        f"{Decimal(m.monto):.2f}",
        (m.tipo or "").upper(),
    )


def _es_tipo_compatible(tipo_real: str, doc_bancsis: str) -> bool:
    if tipo_real == "C":
        return doc_bancsis in _DOCS_CREDITO
    if tipo_real == "D":
        return doc_bancsis in _DOCS_DEBITO
    return False


def matchear_extracto_banco(
    movs_real: Iterable[MovBanco],
    no_banco: int = _BANCO_PICHINCHA_NO,
    dias_tolerancia: int = 5,
    monto_tolerancia: float = 1.0,
) -> ConciliacionBanco:
    """Cross-reference REAL vs BANCSIS bidireccional.

    Args:
        movs_real: parsed del xlsx Pichincha
        no_banco: 1 (Pichincha) — default
        dias_tolerancia: ventana de fechas para matching probable
        monto_tolerancia: USD, diff máxima de monto para considerar match

    Returns:
        ConciliacionBanco con matches, real_only, bancsis_only y saldos.
    """
    movs_real = list(movs_real)
    if not movs_real:
        return ConciliacionBanco()

    fechas_real = [m.fecha for m in movs_real]
    desde = min(fechas_real)
    hasta = max(fechas_real)

    # Cargamos BANCSIS en una ventana más amplia para absorver drift.
    ventana = timedelta(days=dias_tolerancia)
    bancsis = cargar_bancsis(no_banco, desde - ventana, hasta + ventana)

    # Excluimos los ya conciliados.
    ids_excl, firmas_excl = _ya_conciliadas(no_banco, desde, hasta)
    bancsis = [b for b in bancsis if b.id_transaccion not in ids_excl]
    movs_real_filtrados = [m for m in movs_real if _firma_real(m) not in firmas_excl]

    res = ConciliacionBanco()

    # Greedy: para cada REAL, buscar el mejor match no usado en BANCSIS.
    bancsis_usado: set[int] = set()

    for real in movs_real_filtrados:
        candidatos: list[tuple[float, MovBancsis]] = []
        for bk in bancsis:
            if bk.id_transaccion in bancsis_usado:
                continue
            if not _es_tipo_compatible(real.tipo, bk.documento):
                continue
            diff_monto = abs(float(real.monto) - bk.importe)
            if diff_monto > monto_tolerancia:
                continue
            diff_dias = abs((real.fecha - bk.fecha).days)
            if diff_dias > dias_tolerancia:
                continue
            score = diff_dias + diff_monto * 10  # cada $1 de drift = 10 días equivalentes
            candidatos.append((score, bk))
        if candidatos:
            candidatos.sort(key=lambda t: t[0])
            score, bk = candidatos[0]
            diff_dias = abs((real.fecha - bk.fecha).days)
            diff_monto = abs(float(real.monto) - bk.importe)
            if diff_dias == 0 and diff_monto < 0.01:
                razon = f"Match exacto · BANCSIS #{bk.id_transaccion} ({bk.documento})"
            else:
                razon = (
                    f"Match probable · BANCSIS #{bk.id_transaccion} ({bk.documento}) "
                    f"· Δfecha {diff_dias}d · Δmonto ${diff_monto:.2f}"
                )
            res.matches.append(Match(real=real, bancsis=bk, score=score, razon=razon))
            bancsis_usado.add(bk.id_transaccion)
        else:
            res.real_only.append(real)

    # BANCSIS sin match.
    for bk in bancsis:
        if bk.id_transaccion not in bancsis_usado:
            res.bancsis_only.append(bk)

    # Saldos.
    if movs_real:
        # El "saldo final" del REAL es el saldo de la ÚLTIMA fila del xlsx
        # (ordenadas por fecha desc cronológicamente en el extracto). Tomamos
        # el saldo del registro de mayor fecha (y dentro de la misma fecha, el
        # último según orden en el archivo).
        ultimo = movs_real[-1]
        res.saldo_real_final = ultimo.saldo
        res.saldo_real_fecha = ultimo.fecha
        # Buscar saldo BANCSIS al final del rango.
        sb = db.fetch_one(
            """
            SELECT saldo, fecha
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
               AND fecha <= %s
               AND saldo IS NOT NULL
             ORDER BY fecha DESC, id_transaccion DESC
             LIMIT 1
            """,
            (no_banco, hasta),
        )
        if sb:
            res.saldo_bancsis_final = float(sb.get("saldo") or 0)
            res.saldo_bancsis_fecha = sb.get("fecha")

    # Totales signados de los grupos solo.
    for m in res.real_only:
        sign = 1 if m.tipo == "C" else -1
        res.total_real_only_signed += sign * float(m.monto)
    for b in res.bancsis_only:
        sign = 1 if b.documento in _DOCS_CREDITO else -1
        res.total_bancsis_only_signed += sign * b.importe

    return res


def confirmar_match(
    no_banco: int,
    real: MovBanco,
    id_transaccion: int | None,
    estado: str = "matched",
    usuario: str = "web",
    metodo: str = "matched_auto",
    conn=None,
) -> int:
    """Inserta un match (o aceptación unilateral) en banco_conciliacion_match.

    estado: 'matched' | 'real_only_ok' | 'bancsis_only_ok'.
    metodo: 'matched_auto' | 'matched_manual' | 'created_from_real' |
            'real_only_ok' | 'bancsis_only_ok'.

    Idempotente: el unique index (no_banco, real_fecha, real_documento, real_monto, real_tipo)
    WHERE deshecho_en IS NULL + ON CONFLICT DO NOTHING evita duplicados activos.
    Si la firma estaba conciliada y deshecha, se puede re-insertar.

    Si la migration 0047 no corrió todavía, omitimos la columna `metodo`.
    """
    if _tiene_migration_47():
        return db.execute(
            """
            INSERT INTO scintela.banco_conciliacion_match (
                no_banco, estado, metodo,
                real_fecha, real_concepto, real_documento, real_monto, real_tipo,
                real_codigo, real_oficina,
                id_transaccion, usuario
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                no_banco, estado, metodo,
                real.fecha, real.concepto, real.documento,
                real.monto, real.tipo,
                real.codigo, real.oficina,
                id_transaccion, usuario,
            ),
            conn=conn,
        )
    # Fallback pre-migration: schema sin columna `metodo`.
    return db.execute(
        """
        INSERT INTO scintela.banco_conciliacion_match (
            no_banco, estado,
            real_fecha, real_concepto, real_documento, real_monto, real_tipo,
            real_codigo, real_oficina,
            id_transaccion, usuario
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            no_banco, estado,
            real.fecha, real.concepto, real.documento,
            real.monto, real.tipo,
            real.codigo, real.oficina,
            id_transaccion, usuario,
        ),
        conn=conn,
    )


def confirmar_bancsis_only(
    no_banco: int,
    id_transaccion: int,
    usuario: str = "web",
) -> int:
    """Aceptar que un mov BANCSIS NO está en REAL (legítima diferencia)."""
    if _tiene_migration_47():
        return db.execute(
            """
            INSERT INTO scintela.banco_conciliacion_match
                (no_banco, estado, metodo, id_transaccion, usuario)
            VALUES (%s, 'bancsis_only_ok', 'bancsis_only_ok', %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (no_banco, id_transaccion, usuario),
        )
    return db.execute(
        """
        INSERT INTO scintela.banco_conciliacion_match
            (no_banco, estado, id_transaccion, usuario)
        VALUES (%s, 'bancsis_only_ok', %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (no_banco, id_transaccion, usuario),
    )


def confirmar_real_only(
    no_banco: int,
    real: MovBanco,
    usuario: str = "web",
) -> int:
    """Aceptar que un mov REAL NO está en BANCSIS (legítima diferencia,
    sin crear la tx BANCSIS)."""
    return confirmar_match(
        no_banco=no_banco,
        real=real,
        id_transaccion=None,
        estado="real_only_ok",
        metodo="real_only_ok",
        usuario=usuario,
    )


# ─── Fase B (2026-05-23) — Crear tx BANCSIS desde un real_only ────────────


def _documento_bancsis_desde_tipo(tipo: str) -> str:
    """Mapea Tipo C/D del extracto al documento BANCSIS canónico.

    Real Tipo='C' (entrada) → 'DE' (depósito) por defecto. El usuario podrá
    cambiarlo después editando la tx si fuera TR, NC, etc.
    Real Tipo='D' (salida) → 'CH' (cheque emitido) por defecto.
    """
    t = (tipo or "").strip().upper()
    if t == "C":
        return "DE"
    if t == "D":
        return "CH"
    raise ValueError(f"Tipo banco desconocido: {tipo!r}")


def crear_transaccion_desde_real(
    no_banco: int,
    real: MovBanco,
    usuario: str = "web",
    documento: str | None = None,
    no_cta: str | None = None,
) -> dict:
    """Crea una tx en BANCSIS a partir de un mov real_only y la deja conciliada.

    Atómico: insert tx + recompute saldos + insert match en una sola db.tx().
    Si la fila se inserta al medio (fecha pasada) dispara walk-forward para
    mantener `transacciones_bancarias.saldo` consistente.

    Args:
        no_banco: banco destino.
        real: el MovBanco del extracto que queremos materializar.
        usuario: para auditoría.
        documento: si querés forzar 'TR' o 'NC' en vez del default ('DE'/'CH').
        no_cta: cuenta opcional dentro del banco.

    Returns:
        {id_transaccion, saldo_nuevo, match_insertado}
    """
    import bank_helpers

    doc = (documento or _documento_bancsis_desde_tipo(real.tipo)).upper()
    concepto = (real.concepto or "")[:50] or f"Extracto {real.tipo} #{real.documento}"
    numref = None
    if real.documento:
        try:
            numref = int(str(real.documento).strip().lstrip("0") or "0")
        except ValueError:
            numref = None

    with db.tx() as conn:
        ins = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco,
            no_cta=no_cta,
            fecha=real.fecha,
            documento=doc,
            importe=float(real.monto),
            concepto=concepto,
            numreferencia=numref,
            usuario=usuario,
        )
        new_id = ins.get("id_transaccion")

        # Walk-forward: si la fila quedó al medio, recomputar saldos posteriores.
        bank_helpers.recompute_saldos_desde(
            conn,
            no_banco=no_banco,
            no_cta=no_cta,
            ancla_id=int(new_id),
        )

        # Persistir match en la misma tx.
        n = confirmar_match(
            no_banco=no_banco,
            real=real,
            id_transaccion=int(new_id) if new_id else None,
            estado="matched",
            usuario=usuario,
            metodo="created_from_real",
            conn=conn,
        )

    return {
        "id_transaccion": new_id,
        "saldo_nuevo": ins.get("saldo_nuevo"),
        "saldo_anterior": ins.get("saldo_anterior"),
        "documento": doc,
        "match_insertado": bool(n),
    }


# ─── Fase D (2026-05-23) — Match manual, romper match, historial, undo ────


def match_manual(
    no_banco: int,
    real: MovBanco,
    id_transaccion: int,
    usuario: str = "web",
) -> int:
    """Fuerza un match REAL ↔ BANCSIS sin pasar por el scorer.

    Usado desde el modal "Match manual" cuando el matcher no acertó
    (por ejemplo, drift de fecha > 5 días o monto > $1).
    """
    return confirmar_match(
        no_banco=no_banco,
        real=real,
        id_transaccion=int(id_transaccion),
        estado="matched",
        usuario=usuario,
        metodo="matched_manual",
    )


def romper_match(
    match_id: int,
    usuario: str = "web",
) -> int:
    """Marca una fila de banco_conciliacion_match como deshecha.

    Si la migration 0047 corrió: soft-undo (UPDATE deshecho_en).
    Si NO corrió: hard-delete (la fila desaparece — sin audit trail pero
    el mov vuelve a aparecer en el próximo upload).
    """
    if _tiene_migration_47():
        return db.execute(
            """
            UPDATE scintela.banco_conciliacion_match
               SET deshecho_en = CURRENT_TIMESTAMP,
                   deshecho_por = %s
             WHERE id = %s
               AND deshecho_en IS NULL
            """,
            (usuario[:50], int(match_id)),
        )
    # Fallback pre-migration: hard delete
    return db.execute(
        "DELETE FROM scintela.banco_conciliacion_match WHERE id = %s",
        (int(match_id),),
    )


def historial(
    no_banco: int | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    incluir_deshechos: bool = False,
    limit: int = 200,
) -> list[dict]:
    """Lista conciliaciones (matches + aceptaciones) para la vista de historial."""
    where = ["1=1"]
    params: list = []
    if no_banco is not None:
        where.append("bcm.no_banco = %s")
        params.append(int(no_banco))
    if desde is not None:
        where.append("(bcm.real_fecha >= %s OR bcm.creado_en::date >= %s)")
        params.extend([desde, desde])
    if hasta is not None:
        where.append("(bcm.real_fecha <= %s OR bcm.creado_en::date <= %s)")
        params.extend([hasta, hasta])
    tiene_47 = _tiene_migration_47()
    if not incluir_deshechos and tiene_47:
        where.append("bcm.deshecho_en IS NULL")
    params.append(int(limit))

    if tiene_47:
        select_extra = "bcm.metodo, bcm.deshecho_en, bcm.deshecho_por,"
    else:
        select_extra = "NULL::text AS metodo, NULL::timestamp AS deshecho_en, NULL::text AS deshecho_por,"

    rows = db.fetch_all(
        f"""
        SELECT bcm.id,
               bcm.no_banco,
               bcm.estado,
               {select_extra}
               bcm.real_fecha,
               bcm.real_concepto,
               bcm.real_documento,
               bcm.real_monto,
               bcm.real_tipo,
               bcm.id_transaccion,
               bcm.usuario,
               bcm.creado_en,
               tb.documento  AS bancsis_documento,
               tb.importe    AS bancsis_importe,
               tb.fecha      AS bancsis_fecha,
               tb.concepto   AS bancsis_concepto
          FROM scintela.banco_conciliacion_match bcm
          LEFT JOIN scintela.transacciones_bancarias tb
            ON tb.id_transaccion = bcm.id_transaccion
         WHERE {" AND ".join(where)}
         ORDER BY bcm.creado_en DESC, bcm.id DESC
         LIMIT %s
        """,
        tuple(params),
    ) or []
    return [dict(r) for r in rows]


def candidatos_match_manual(
    no_banco: int,
    fecha_real: date,
    monto_real: float,
    tipo_real: str,
    ventana_dias: int = 30,
    ventana_monto: float = 50.0,
    limit: int = 30,
) -> list[dict]:
    """BANCSIS filas candidatas para hacer match manual.

    Más laxo que el scorer: ±30 días, ±$50, mismo Tipo C/D. Ordenado por
    'cercanía' (suma absoluta de drift de fecha y monto, igual al scorer).
    """
    doc_filter_in = _DOCS_CREDITO if (tipo_real or "").upper() == "C" else _DOCS_DEBITO
    ya_excluido_clause = "AND deshecho_en IS NULL" if _tiene_migration_47() else ""

    rows = db.fetch_all(
        f"""
        SELECT id_transaccion, fecha, documento, concepto, importe,
               numreferencia,
               ABS(EXTRACT(DAY FROM fecha - %s))::int AS diff_dias,
               ABS(importe - %s) AS diff_monto
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND fecha BETWEEN %s AND %s
           AND ABS(importe - %s) <= %s
           AND UPPER(TRIM(documento)) = ANY(%s)
           AND id_transaccion NOT IN (
              SELECT id_transaccion
                FROM scintela.banco_conciliacion_match
               WHERE id_transaccion IS NOT NULL
                 {ya_excluido_clause}
           )
         ORDER BY diff_dias ASC, diff_monto ASC
         LIMIT %s
        """,
        (
            fecha_real,
            float(monto_real),
            int(no_banco),
            fecha_real - timedelta(days=ventana_dias),
            fecha_real + timedelta(days=ventana_dias),
            float(monto_real),
            float(ventana_monto),
            list(doc_filter_in),
            int(limit),
        ),
    ) or []
    return [
        {
            "id_transaccion": int(r["id_transaccion"]),
            "fecha": r["fecha"].isoformat() if r.get("fecha") else None,
            "documento": (r.get("documento") or "").strip(),
            "concepto": (r.get("concepto") or "").strip(),
            "importe": float(r.get("importe") or 0),
            "numreferencia": (r.get("numreferencia") or ""),
            "diff_dias": int(r.get("diff_dias") or 0),
            "diff_monto": float(r.get("diff_monto") or 0),
        }
        for r in rows
    ]
