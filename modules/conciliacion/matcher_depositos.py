"""Matcher de depósitos pendientes (Excel) vs transacciones_bancarias.

TMT 2026-05-20 — la dueña sube un .xlsx con depósitos que el sistema cree
que ingresaron al banco. Este módulo cruza esos depósitos contra los
movimientos reales del banco en `scintela.transacciones_bancarias`
(documentos 'DE' = depósito y 'TR' = transferencia recibida).

Categorías:
    VERDE    : match exacto (importe + fecha + referencia coinciden)
    AMARILLO : match probable (importe + fecha cercana ± N días)
    ROJO     : sin match (probable error, duplicado, o ya reversado)

La UI muestra cada categoría en un color y permite acciones de resolución:
    - VERDE   → ya está conciliado, sin acción
    - AMARILLO → confirmar el match manualmente
    - ROJO    → marcar como "no entró" / duplicado / etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

import db

from modules.conciliacion.parser_xlsx import DepositoPendiente


@dataclass
class MovBancario:
    """Una fila de scintela.transacciones_bancarias."""
    id_transaccion: int
    fecha: date
    documento: str
    concepto: str
    importe: float
    numreferencia: str
    no_banco: int

    @classmethod
    def from_row(cls, row: dict) -> "MovBancario":
        return cls(
            id_transaccion=int(row["id_transaccion"]),
            fecha=row["fecha"],
            documento=(row.get("documento") or "").strip(),
            concepto=(row.get("concepto") or "").strip(),
            importe=float(row.get("importe") or 0),
            numreferencia=(row.get("numreferencia") or "").strip(),
            no_banco=int(row.get("no_banco") or 0),
        )


@dataclass
class FilaConciliada:
    """Un depósito del Excel matched (o no) contra el banco."""
    deposito: DepositoPendiente
    estado: str  # 'verde' | 'amarillo' | 'rojo'
    match: MovBancario | None = None
    razon: str = ""
    # Múltiples candidatos en amarillo — para que la UI ofrezca elegir.
    candidatos: list[MovBancario] = field(default_factory=list)


@dataclass
class ConciliacionResultado:
    filas: list[FilaConciliada] = field(default_factory=list)
    n_verde: int = 0
    n_amarillo: int = 0
    n_rojo: int = 0
    total_verde: float = 0.0
    total_amarillo: float = 0.0
    total_rojo: float = 0.0


def transacciones_en_rango(desde: date, hasta: date,
                           no_banco: int | None = None) -> list[MovBancario]:
    """Trae todos los DE/TR/AC/NC en el rango de fechas dado.

    DE = depósito, TR = transferencia recibida, AC = abono crédito, NC = nota
    crédito. Todos representan ingresos al banco. Si `no_banco` se especifica,
    filtra a ese banco; sino busca en todos.
    """
    sql = """
        SELECT id_transaccion, fecha, documento, concepto, importe,
               numreferencia, no_banco
          FROM scintela.transacciones_bancarias
         WHERE documento IN ('DE', 'TR', 'AC', 'NC')
           AND fecha BETWEEN %s AND %s
    """
    params: list = [desde, hasta]
    if no_banco is not None:
        sql += " AND no_banco = %s"
        params.append(no_banco)
    sql += " ORDER BY fecha ASC, id_transaccion ASC"
    rows = db.fetch_all(sql, tuple(params)) or []
    return [MovBancario.from_row(r) for r in rows]


def _ref_match(dep_codigo: str, mov_ref: str, mov_concepto: str) -> bool:
    """¿La referencia del depósito está en la transacción del banco?"""
    cod = (dep_codigo or "").strip().lstrip("0")
    if not cod or len(cod) < 4:
        return False
    ref = (mov_ref or "").strip().lstrip("0")
    if ref == cod:
        return True
    # buscar en el concepto del banco (puede traer "DEPOSITO 15289222")
    return cod in (mov_concepto or "")


def _importe_match(dep_valor: Decimal, mov_importe: float, tol: float = 0.01) -> bool:
    return abs(float(dep_valor) - mov_importe) <= tol


def _categoria(dep: DepositoPendiente,
               matches_perfectos: list[MovBancario],
               matches_probables: list[MovBancario]) -> FilaConciliada:
    if matches_perfectos:
        # match exacto: 1 sólo o el primero
        m = matches_perfectos[0]
        return FilaConciliada(
            deposito=dep, estado="verde", match=m,
            razon=f"Match exacto en banco #{m.no_banco} ({m.fecha:%d/%m/%Y}, {m.documento})",
            candidatos=matches_perfectos,
        )
    if matches_probables:
        # 1 candidato → amarillo. >1 → amarillo con varios.
        m = matches_probables[0]
        if len(matches_probables) == 1:
            razon = (
                f"Importe coincide pero la fecha difiere "
                f"({m.fecha:%d/%m/%Y} en banco vs {dep.fecha:%d/%m/%Y} en sistema)."
            )
        else:
            razon = f"{len(matches_probables)} movimientos del banco con el mismo importe."
        return FilaConciliada(
            deposito=dep, estado="amarillo", match=m, razon=razon,
            candidatos=matches_probables,
        )
    return FilaConciliada(
        deposito=dep, estado="rojo", match=None,
        razon="No hay movimiento en el banco con este importe en el rango buscado.",
    )


def matchear_depositos(
    depositos: Iterable[DepositoPendiente],
    movimientos: Iterable[MovBancario],
    dias_tolerancia: int = 5,
) -> ConciliacionResultado:
    """Matchea cada depósito contra los movimientos del banco.

    Reglas (más estrictas → menos estrictas):
      1. **Verde**: importe ± 0.01 Y fecha exacta (mismo día) Y referencia
         coincide (numreferencia o concepto contiene el código).
      2. **Verde**: importe ± 0.01 Y fecha exacta. (Sin referencia pero coincide día.)
      3. **Amarillo**: importe ± 0.01 Y fecha dentro de ± `dias_tolerancia`.
      4. **Rojo**: importe no encontrado en el rango.

    Cada movimiento bancario se "consume" tras ser asignado a un match
    verde — para evitar que dos depósitos pidan el mismo movimiento del
    banco. Para amarillo no consumimos (la UI confirma manualmente).
    """
    movs = list(movimientos)
    usados: set[int] = set()
    resultado = ConciliacionResultado()

    for dep in depositos:
        if dep.fecha is None:
            # Sin fecha → no se puede matchear, va a rojo
            resultado.filas.append(FilaConciliada(
                deposito=dep, estado="rojo",
                razon="Depósito sin fecha en el Excel.",
            ))
            continue

        # Buscar matches
        verde_estrictos: list[MovBancario] = []
        verde_fecha_exacta: list[MovBancario] = []
        amarillos: list[MovBancario] = []

        for m in movs:
            if m.id_transaccion in usados:
                continue
            if not _importe_match(dep.valor, m.importe):
                continue
            dt = (m.fecha - dep.fecha).days if m.fecha and dep.fecha else 999
            if dt == 0:
                if _ref_match(dep.codigo, m.numreferencia, m.concepto):
                    verde_estrictos.append(m)
                else:
                    verde_fecha_exacta.append(m)
            elif abs(dt) <= dias_tolerancia:
                amarillos.append(m)

        # Decidir
        if verde_estrictos:
            fila = _categoria(dep, verde_estrictos, [])
            usados.add(fila.match.id_transaccion)
        elif verde_fecha_exacta:
            fila = _categoria(dep, verde_fecha_exacta, [])
            usados.add(fila.match.id_transaccion)
        elif amarillos:
            fila = _categoria(dep, [], amarillos)
        else:
            fila = _categoria(dep, [], [])

        resultado.filas.append(fila)

    # Totales
    for f in resultado.filas:
        v = float(f.deposito.valor)
        if f.estado == "verde":
            resultado.n_verde += 1
            resultado.total_verde += v
        elif f.estado == "amarillo":
            resultado.n_amarillo += 1
            resultado.total_amarillo += v
        else:
            resultado.n_rojo += 1
            resultado.total_rojo += v

    return resultado
