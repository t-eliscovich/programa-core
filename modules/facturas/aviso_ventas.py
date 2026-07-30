"""Aviso de CIERRE DEL DÍA: cuánto se vendió hoy, en plata y en kilos.

TMT 2026-07-30 (dueña): *"agregar en la campanita, a fin de día, venta total kg
y total facturas $"* → y, viendo el borrador: *"plata arriba"*, *"18 hs
Ecuador"*.

Queda así en la campanita:

    ✅  Ventas de hoy · $ 116.230,45              30/07 18:00
        13.565,54 kg · 113 facturas

Decisiones que valen la pena recordar:

· **18:00 de ECUADOR**, no del servidor (que corre en UTC, 5 horas adelante).
  Por eso la hora sale de `_ahora_ec()` y no de `datetime.now()`.
· **Uno solo por día.** La clave del aviso es `ventas:<fecha>`, así que el
  ciclo de fondo puede pasar cien veces después de las 18:00 y entra una vez.
· **Si no se facturó nada, no avisa.** Un domingo no tiene por qué encender la
  campanita para decir cero.
· El universo es *lo facturado con fecha de hoy, sin las anuladas* — NO la
  cartera: una factura cobrada el mismo día sale de la cartera pero fue venta
  igual. Las notas de crédito del día restan, así que el número es la venta
  NETA.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import db
from filters import num_es, today_ec

_LOG = logging.getLogger("programa_core.aviso_ventas")

# Hora de Ecuador a partir de la cual se manda el cierre del día.
HORA_AVISO = 18

_ultimo_dia_avisado: str | None = None


def _ahora_ec() -> datetime:
    """Ahora en Ecuador (UTC-5, sin horario de verano) — igual que today_ec()."""
    return datetime.now(UTC) - timedelta(hours=5)


def _hora_aviso() -> int:
    try:
        h = int(os.environ.get("VENTAS_AVISO_HORA", str(HORA_AVISO)))
    except (TypeError, ValueError):
        return HORA_AVISO
    return h if 0 <= h <= 23 else HORA_AVISO


def totales_dia(fecha) -> dict:
    """{n, importe, kg} de lo facturado ese día. Fail-soft: ceros."""
    try:
        row = db.fetch_one(
            """
            SELECT COUNT(*)                    AS n,
                   COALESCE(SUM(importe), 0)   AS importe,
                   COALESCE(SUM(kg), 0)        AS kg
              FROM scintela.factura
             WHERE fecha = %s
               AND COALESCE(stat, '') <> 'X'
            """,
            (fecha,),
        ) or {}
    except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
        _LOG.warning("no pude calcular las ventas del día: %s", e)
        return {"n": 0, "importe": 0.0, "kg": 0.0}
    return {
        "n": int(row.get("n") or 0),
        "importe": float(row.get("importe") or 0),
        "kg": float(row.get("kg") or 0),
    }


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo. Nunca levanta."""
    global _ultimo_dia_avisado
    res = {"avisado": False, "motivo": ""}
    if os.environ.get("VENTAS_AVISO", "1").strip() == "0":
        res["motivo"] = "apagado"
        return res
    try:
        hoy = today_ec()
        clave_dia = hoy.isoformat()
        if _ultimo_dia_avisado == clave_dia:
            res["motivo"] = "ya avisado hoy"
            return res
        if _ahora_ec().hour < _hora_aviso():
            res["motivo"] = "todavía no son las 18"
            return res

        t = totales_dia(hoy)
        if not t["n"]:
            # Sin facturas no hay nada que contar (y no se marca el día como
            # avisado: si entra una factura a las 19, el aviso sale igual).
            res["motivo"] = "sin facturas"
            return res

        from modules.avisos import avisar

        avisar(
            fuente="ventas",
            titulo=f"Ventas de hoy · $ {num_es(t['importe'], 2)}",
            detalle=(f"{num_es(t['kg'], 2)} kg · {t['n']} "
                     f"factura{'' if t['n'] == 1 else 's'}"),
            importe=round(t["importe"], 2), cantidad=t["n"],
            url=f"/facturas?desde={clave_dia}&hasta={clave_dia}",
            clave=f"ventas:{clave_dia}",
        )
        _ultimo_dia_avisado = clave_dia
        res["avisado"] = True
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("aviso de ventas del día: %s", e)
        res["motivo"] = str(e)[:120]
    return res
