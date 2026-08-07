"""Reescribe con el formato nuevo los renglones de stock ya guardados.

TMT 2026-08-06, viendo una ventana vieja abierta: *"esto sigue sin
funcionar..."*. No estaba roto — el renglón único de stock ya andaba en las
ventanas nuevas. Lo que pasa es que **los movimientos son filas guardadas**:
cada uno lleva el texto con el que se calculó, y las ventanas de la mañana se
grabaron cuando el stock todavía salía partido en cuatro o cinco renglones:

    +21,65 kg a $ 5,2591/kg · Stock terminado: entró tela terminada    113,86
    -21,65 kg a $ 3,5591/kg · Stock tejido: entraron/salieron kilos    -77,05
    $ 5,2591 → $ 5,2591 sobre 312.508 kg · cambió el $/kg              -13,29
    Stock terminado · Stock: redondeo de la partición                   13,29

Reprocesarlos se puede y sin inventar nada: `traza_utilidad` guarda, en CADA
foto, el valor del stock y los kilos de las tres etapas. Con la foto y la
anterior alcanza para recalcular exactamente lo mismo que calcularía el motor
de hoy — de dónde a dónde se movió la tela y cuánto aportó.

⚠ Sólo toca `componente = 'vsto'`. El resto de los movimientos (facturas,
cheques, caja) ya estaban bien y no se reescribe historia porque sí.

Idempotente: si una foto ya tiene el formato nuevo, se saltea.
"""

from __future__ import annotations

import os

#: Las etapas en el orden en que la tela las recorre. Igual que en
#: `modules/informes/foto.py` — si cambia una, cambian las dos.
ORDEN_ETAPAS = ("hilado", "tejido", "terminado")
UMBRAL = 0.01


def _texto(dkg: dict) -> str:
    """De dónde a dónde. Misma regla que `foto._texto_stock`."""
    bajaron = [e for e in ORDEN_ETAPAS if (dkg.get(e) or 0) < -1]
    subieron = [e for e in ORDEN_ETAPAS if (dkg.get(e) or 0) > 1]
    if bajaron and subieron:
        return f"{' y '.join(bajaron)} → {' y '.join(subieron)}"
    if subieron:
        return f"entró a {' y '.join(subieron)}"
    if bajaron:
        return f"salió de {' y '.join(bajaron)}"
    return "movimiento de stock"


def run(conn) -> None:
    cur = conn.cursor()

    # Las fotos que tienen renglones de stock con el formato viejo. `#stock` y
    # `#stock:tarifa` son los del formato nuevo: esas ya están.
    cur.execute(
        """
        SELECT DISTINCT id_traza
          FROM scintela.dia_movimiento
         WHERE componente = 'vsto'
           AND id_traza IS NOT NULL
           AND doc_id NOT IN ('#stock', '#stock:tarifa')
         ORDER BY id_traza
        """
    )
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        cur.close()
        return

    # La foto y la anterior, en una sola pasada.
    cur.execute(
        """
        SELECT id_traza, vsto, hilado_kg, tejido_kg, terminado_kg,
               LAG(vsto)         OVER w AS vsto_ant,
               LAG(hilado_kg)    OVER w AS hilado_ant,
               LAG(tejido_kg)    OVER w AS tejido_ant,
               LAG(terminado_kg) OVER w AS terminado_ant
          FROM scintela.traza_utilidad
        WINDOW w AS (ORDER BY creado_en, id_traza)
        """
    )
    fotos = {r[0]: r for r in cur.fetchall()}

    rehechas = 0
    for idt in ids:
        f = fotos.get(idt)
        if not f or f[5] is None:          # sin foto anterior: no hay Δ posible
            continue
        aporte = round(float(f[1] or 0) - float(f[5] or 0), 2)
        dkg = {et: float(f[2 + i] or 0) - float(f[6 + i] or 0)
               for i, et in enumerate(ORDEN_ETAPAS)}

        cur.execute(
            "DELETE FROM scintela.dia_movimiento "
            " WHERE id_traza = %s AND componente = 'vsto'", (idt,))
        if abs(aporte) < UMBRAL:
            rehechas += 1
            continue                        # se movió menos de un centavo
        cur.execute(
            """
            INSERT INTO scintela.dia_movimiento
                (id_traza, componente, tipo, doc_id, etiqueta,
                 delta, aporte, regla, familia)
            VALUES (%s, 'vsto', 'cambio', '#stock', %s, %s, %s, 'Stock', 'utilidad')
            """,
            (idt, _texto(dkg), aporte, aporte),
        )
        rehechas += 1
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    stock reprocesado en {rehechas} ventana(s)")
