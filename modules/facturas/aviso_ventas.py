"""Avisos de VENTAS del día: los umbrales de kilos y el cierre de las 19.

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

── UMBRALES DE KILOS (TMT 2026-08-07, dueña) ────────────────────────────────

*"otra notificación de facturas cuando la venta del día sobrepase 10kg y
cuando sobrepase 15kg y luego 20kg… si se acumula, una notificación que diga
ventas del día x 15.410kg por x$ como la del final de día pero entre la jornada
laboral"* — o sea: enterarse de cómo viene el día SIN esperar a las 18.

    ✅  Ventas del día · $ 128.410,20              07/08 11:24
        15.410,50 kg · 104 facturas

Los umbrales son **10.000 / 15.000 / 20.000 kg** (confirmado por la dueña: un
día normal cierra en 13.500-16.000 kg, así que la escalera tiene sentido).

Decisiones que valen la pena recordar:

· **Sólo entre las 08:00 y las 19:00 de Ecuador.** De 19 en adelante manda el
  cierre del día, que no se toca; antes de las 8 no hay a quién avisarle.
· **Se avisa el umbral MÁS ALTO cruzado, no todos.** Si a las 9 entra un lote
  que salta de 4.000 a 16.000 kg de una, sale UN aviso (el de 15.000), no dos.
  Los de abajo se dan por vistos: la campanita informa, no lleva la cuenta.
· **La memoria del "hasta dónde ya avisé" está en la BASE, no en una variable
  de proceso** — se lee del `clave` de los avisos ya puestos
  (`ventas-kg:<fecha>:<umbral>`). Un restart del server a media mañana no tiene
  que hacer que la campanita repita los umbrales de la mañana. Y aunque los
  repitiera, el `ON CONFLICT (clave)` de `avisar()` los frena igual: son DOS
  candados, a propósito.
· **Mismo universo y mismo formato que el cierre** — la dueña compara los dos
  números de un vistazo, así que salen de la MISMA función (`totales_dia`).
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import db
from filters import num_es, today_ec

_LOG = logging.getLogger("programa_core.aviso_ventas")

# Hora de Ecuador a partir de la cual se manda el cierre del día.
# TMT 2026-08-13: pasa de 18 a 19, junto con el cierre de la utilidad
# (`dia.HORA_CIERRE`) — *"a veces se pasan de horario para facturar"*.
HORA_AVISO = 19

# Umbrales de kilos vendidos en el día (dueña 2026-08-07). Ordenados de menor a
# mayor: el código se apoya en eso para quedarse con el más alto cruzado.
UMBRALES_KG = (10_000.0, 15_000.0, 20_000.0)

# La jornada laboral: fuera de esta franja no se avisan umbrales. El tope
# coincide con HORA_AVISO a propósito — a las 18 en punto el que habla es el
# cierre del día, y no queremos los dos avisos en el mismo minuto.
HORA_DESDE_KG = 8
HORA_HASTA_KG = 19

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


def _umbrales_kg() -> tuple[float, ...]:
    """Los umbrales, de menor a mayor. `VENTAS_KG_UMBRALES=8000,12000` los pisa."""
    crudo = (os.environ.get("VENTAS_KG_UMBRALES") or "").strip()
    if not crudo:
        return UMBRALES_KG
    vals = []
    for parte in crudo.split(","):
        try:
            v = float(parte.strip())
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    return tuple(sorted(set(vals))) if vals else UMBRALES_KG


def _franja_kg() -> tuple[int, int]:
    """(desde, hasta) en hora de Ecuador. Si vienen al revés, se ignoran."""
    def _leer(nombre: str, x: int) -> int:
        try:
            v = int(os.environ.get(nombre, str(x)))
        except (TypeError, ValueError):
            return x
        return v if 0 <= v <= 23 else x

    desde = _leer("VENTAS_KG_DESDE", HORA_DESDE_KG)
    hasta = _leer("VENTAS_KG_HASTA", HORA_HASTA_KG)
    if desde >= hasta:
        return HORA_DESDE_KG, HORA_HASTA_KG
    return desde, hasta


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


def _clave_umbral(clave_dia: str, umbral: float) -> str:
    return f"ventas-kg:{clave_dia}:{int(umbral)}"


def ultimo_umbral_avisado(clave_dia: str) -> float:
    """Hasta qué umbral de kilos ya avisamos HOY, leído de los avisos puestos.

    Se pregunta a la base y no a una variable de módulo a propósito: el server
    se reinicia (deploy, restart del Scheduled Task) y una variable de proceso
    volvería a cero a media mañana, con lo que la campanita repetiría los
    umbrales que ya cantó. Fail-soft a 0 sin miedo: el segundo candado es el
    `ON CONFLICT (clave)` de `avisar()`, que igual no deja entrar el repetido.
    """
    try:
        row = db.fetch_one(
            """
            SELECT MAX(NULLIF(SPLIT_PART(clave, ':', 3), '')::numeric) AS u
              FROM scintela.aviso
             WHERE clave LIKE %s
            """,
            (f"ventas-kg:{clave_dia}:%",),
        ) or {}
    except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
        _LOG.warning("no pude leer el último umbral avisado: %s", e)
        return 0.0
    return float(row.get("u") or 0)


def correr_umbrales_kg() -> dict:
    """Avisa cuando los kilos del día cruzan un umbral. Nunca levanta.

    Manda UN aviso por cruce: el del umbral más alto superado que todavía no se
    haya avisado hoy. Los de abajo quedan tapados (si el día salta de 4.000 a
    16.000 kg de un lote, sale el de 15.000 y nada más).
    """
    res = {"avisado": False, "umbral": 0.0, "motivo": ""}
    if os.environ.get("VENTAS_AVISO", "1").strip() == "0":
        res["motivo"] = "apagado"
        return res
    try:
        desde, hasta = _franja_kg()
        h = _ahora_ec().hour
        if not (desde <= h < hasta):
            res["motivo"] = f"fuera de la jornada ({desde}-{hasta})"
            return res

        hoy = today_ec()
        clave_dia = hoy.isoformat()
        t = totales_dia(hoy)
        if not t["n"]:
            res["motivo"] = "sin facturas"
            return res

        ya = ultimo_umbral_avisado(clave_dia)
        cruzados = [u for u in _umbrales_kg() if t["kg"] >= u > ya]
        if not cruzados:
            res["motivo"] = "no cruzó ningún umbral nuevo"
            return res
        umbral = max(cruzados)

        from modules.avisos import avisar

        avisar(
            fuente="ventas",
            titulo=f"Ventas del día · $ {num_es(t['importe'], 2)}",
            detalle=(f"{num_es(t['kg'], 2)} kg · {t['n']} "
                     f"factura{'' if t['n'] == 1 else 's'}"),
            importe=round(t["importe"], 2), cantidad=t["n"],
            url=f"/facturas?desde={clave_dia}&hasta={clave_dia}",
            clave=_clave_umbral(clave_dia, umbral),
        )
        res["avisado"] = True
        res["umbral"] = umbral
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("aviso de umbral de kilos: %s", e)
        res["motivo"] = str(e)[:120]
    return res


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
