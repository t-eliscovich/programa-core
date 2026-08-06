"""Observación de texto libre EDITABLE por el usuario sobre un cheque.

TMT 2026-08-06. Alex pidió poder dejar una "observación" propia a un cheque
de cliente — por qué está postergado, quién avisó, un recordatorio corto —
y verla en tres lugares:

  · como `*` rojo al lado del importe en /cheques,
  · impresa en la hoja de LISTADO DE CHEQUES INGRESADOS
    (`ingresados_dia.html`, la que se lleva al banco),
  · impresa en el RESUMEN DE COBRANZA DEL DÍA (`resumen_dia.html`).

⚠ NO reutilizamos `scintela.cheque.observacion` a propósito. Esa columna es
una BITÁCORA append-only con tags que escribe el backend (`[E]` edición,
`[X]` error de carga, `[REBOTE]`, `[C]` concepto histórico,
`[ENDOSO a <prov>]`) y de la que hay ~15 callsites. Si mezcláramos free-text
de Alex ahí:

  1. El `*` rojo aparecería para CUALQUIER cheque que alguna vez se editó
     o rebotó (falso positivo permanente en la lista — inservible como
     señal visual).
  2. Las hojas impresas mostrarían al lado del cheque tags como
     `[REBOTE] cliente confirmó` en vez de "cliente confirmó" limpio.
  3. La próxima traza automática lo pisaría: los `UPDATE ... observacion =
     RIGHT(COALESCE(observacion||' | ','')||%s, 200)` truncan por izquierda
     al llegar a los 200 chars.

Por eso va columna propia — mismo criterio que usó Alex con el `concepto`
(ver 0158_cheque_concepto.sql / `concepto_cobro.py`). Es `TEXT NULL`
(sin cap arbitrario); si hace falta capar el largo, va en el form.

El deploy NO corre migraciones automáticamente: la columna se bootstrapea
en caliente en `bootstrap_columna()`, mismo patrón que `concepto_cobro`.
"""

from __future__ import annotations

import logging

import db as _db

_LOG = logging.getLogger("programa_core.cheques.nota_usuario")

_BOOTSTRAP_SQL = (
    "ALTER TABLE scintela.cheque "
    "ADD COLUMN IF NOT EXISTS nota_usuario TEXT"
)
_bootstrapped = False


def bootstrap_columna(conn=None) -> None:
    """Crea `scintela.cheque.nota_usuario` si falta. Idempotente y fail-soft.

    El deploy NO corre migraciones (ver skill `programa-core`), así que la
    columna se bootstrapea en caliente — mismo patrón que
    `concepto_cobro.bootstrap_columna` y `bancos.apertura.bootstrap`.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        _db.execute(_BOOTSTRAP_SQL, conn=conn)
    except Exception as exc:  # noqa: BLE001 -- fail-soft: nunca tumbar la vista
        _LOG.exception("bootstrap cheque.nota_usuario falló: %s", exc)
    _bootstrapped = True


def limpiar(valor) -> str | None:
    """Normaliza el texto: trim, colapsa espacios extremos, '' → None."""
    txt = (valor or "").strip()
    if not txt:
        return None
    return txt


def tiene_nota(valor) -> bool:
    """True si la nota es visible (no None, no vacía tras trim)."""
    return bool(limpiar(valor))
