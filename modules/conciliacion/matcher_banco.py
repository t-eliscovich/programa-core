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


def _ya_conciliadas(no_banco: int, desde: date, hasta: date) -> tuple[set[int], set[tuple]]:
    """Devuelve:
        set de id_transaccion (BANCSIS) ya conciliados
        set de firma REAL (fecha, documento, monto str, tipo) ya conciliados
    """
    rows = db.fetch_all(
        """
        SELECT id_transaccion, real_fecha, real_documento, real_monto, real_tipo
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s
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
) -> int:
    """Inserta un match (o aceptación unilateral) en banco_conciliacion_match.

    estado: 'matched' | 'real_only_ok' | 'bancsis_only_ok'.

    Idempotente: el unique index (no_banco, real_fecha, real_documento, real_monto, real_tipo)
    + ON CONFLICT DO NOTHING evita duplicados.
    """
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
    )


def confirmar_bancsis_only(
    no_banco: int,
    id_transaccion: int,
    usuario: str = "web",
) -> int:
    """Aceptar que un mov BANCSIS NO está en REAL (legítima diferencia)."""
    return db.execute(
        """
        INSERT INTO scintela.banco_conciliacion_match
            (no_banco, estado, id_transaccion, usuario)
        VALUES (%s, 'bancsis_only_ok', %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (no_banco, id_transaccion, usuario),
    )
