"""Los vínculos cheque↔factura que borró un TOTALIZAR, para poder encontrarlos.

TMT 2026-09-16 (dueña, mirando la factura 177617 de MTM cancelada y sin un
solo cheque aplicado): *"no borres vínculos!!"* — y después, sobre cómo
verlos: **"o sea totalizamos pero si quiero puedo encontrar el vínculo"**.

El TOTALIZAR re-liquida FIFO toda la cuenta del cliente y borra las filas de
`chequesxfact` a propósito (decisión del 06/07/2026: después del reparto el
vínculo viejo apuntaría a una factura que ese cheque ya no paga). Lo que
faltaba no era conservarlos vivos: era **poder encontrarlos**.

Antes de borrarlos, el totalizar los duerme en `mov_doble.metadata->'links'`.
Esto los despierta para mostrarlos como HISTORIAL en la ficha de la factura y
en la del cheque. No mueve ningún número: es la misma plata, ya contada en el
abono.

⚠ Las 8 corridas anteriores al 19/08/2026 no guardaron el detalle (15
vínculos): de ésas no hay nada que mostrar, y no se inventa.

⚠ Si el par cheque↔factura volvió a aplicarse después, NO es historial: eso
ya se ve en "Aplicaciones de cheques". Por eso el `NOT EXISTS`.
"""
from __future__ import annotations

import db

# El link dormido trae el id del cheque, el de la factura y el importe que
# ese cheque le aplicaba. El resto (número, banco, fecha) se resuelve contra
# las tablas vivas: si el cheque cambió de estado desde entonces, queremos el
# estado de HOY, no la foto vieja.
_SQL_BASE = """
    SELECT m.id_mov_doble,
           m.fecha_creacion::date        AS fecha_totalizar,
           m.usuario                     AS usuario_totalizar,
           (l->>'id_cheque')::bigint     AS id_cheque,
           (l->>'id_fact')::bigint       AS id_fact,
           (l->>'importe')::numeric      AS aplicado,
           ch.no_cheque,
           ch.fecha                      AS cheque_fecha,
           ch.importe                    AS cheque_importe,
           ch.stat                       AS cheque_stat,
           COALESCE(NULLIF(ch.banco, ''), b.nombre, '') AS cheque_banco,
           f.numf,
           f.numf_completo,
           f.codigo_cli
      FROM scintela.mov_doble m
      CROSS JOIN LATERAL jsonb_array_elements(m.metadata -> 'links') l
      LEFT JOIN scintela.cheque ch ON ch.id_cheque = (l->>'id_cheque')::bigint
      LEFT JOIN scintela.banco  b  ON b.no_banco   = ch.no_banco
      LEFT JOIN scintela.factura f ON f.id_factura = (l->>'id_fact')::bigint
     WHERE m.tipo = 'totalizar_estado_cuenta'
       AND m.metadata ? 'links'
       AND {filtro}
       AND NOT EXISTS (
             SELECT 1 FROM scintela.chequesxfact x
              WHERE x.id_cheque = (l->>'id_cheque')::bigint
                AND x.id_fact   = (l->>'id_fact')::bigint)
     ORDER BY m.fecha_creacion DESC, (l->>'id_cheque')::bigint
"""


def _leer(filtro: str, valor: int) -> list[dict]:
    """Best-effort: un historial que falla no puede voltear una ficha."""
    try:
        return db.fetch_all(_SQL_BASE.format(filtro=filtro), (valor,))
    except Exception as _e:                              # pragma: no cover
        from modules._lib.silencios import avisar
        avisar(__name__, "_leer", _e)
        return []


def de_la_factura(id_factura: int) -> list[dict]:
    """Cheques que pagaban esta factura hasta que corrió el totalizar."""
    return _leer("(l->>'id_fact')::bigint = %s", int(id_factura))


def del_cheque(id_cheque: int) -> list[dict]:
    """Facturas que pagaba este cheque hasta que corrió el totalizar."""
    return _leer("(l->>'id_cheque')::bigint = %s", int(id_cheque))
