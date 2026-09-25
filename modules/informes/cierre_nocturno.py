"""El CIERRE DE MES se hace la última noche del mes, no el día 1.

Tamara 2026-09-25: *"el pdf de cierre debería suceder 11.30 también, ídem con
los gastos"*. Hasta hoy el cierre lo sacaba la tarea del día 1 a las 06:00 EC
con el balance EN VIVO, o sea con el programa ya parado en el mes nuevo: los
flujos del mes (compras, tejeduría, tintorería, gastos, retiros, utilidad) se
leían del mes que ARRANCA — casi todo en cero — y quedaban guardados como si
fueran del mes que cerró. Es lo mismo que rompió el cierre de agosto 2026.

Ahora, el último día del mes, la tarea de la foto diaria de las 23:30 EC
(`scripts/foto_diaria_cron.py`) corre además esto, en orden:

  1. Congela los GASTOS del mes (`scintela.gastos_mes_manual`) con la misma
     cuenta que después muestra /informes/gastos como "Gastos mes anterior".
  2. Saca la FOTO DE CIERRE (`crear_snapshot_historia`, forzada, rama en vivo
     del mismo día), que además arma el PDF de cierre (`cierres_paquete`).
  3. Verifica que el PDF quedó guardado.

Nada de esto frena al resto: cada paso es independiente y lo que falla queda
en `alertas` (el cron sale con código 1 y el health del día 1 lo canta).
El PDF necesita la app de Flask (lo arma pidiéndole las pantallas); por eso
`crear_app` se pasa desde afuera — en el cron se construye la app con los
hilos de fondo apagados.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date

_LOG = logging.getLogger(__name__)

USUARIO = "cierre-nocturno"


def es_ultimo_dia(hoy: date) -> bool:
    return hoy.day == calendar.monthrange(hoy.year, hoy.month)[1]


def congelar_gastos_del_mes(hoy: date) -> dict:
    """Congela tej/tin/adm del mes que termina HOY (meses_atras=0: a esta hora
    el mes ya corrió entero, la amortización incluida)."""
    from modules.informes import queries
    from modules.informes.views import _gastos_mes_anterior_componentes

    g = _gastos_mes_anterior_componentes(meses_atras=0)
    if sum(float(v or 0) for v in g.values()) <= 0:
        raise RuntimeError("los gastos del mes dieron 0: no se congela un cero")
    return queries.gastos_mes_manual_set(
        f"{hoy.year:04d}-{hoy.month:02d}", g["tej"], g["tin"], g["adm"],
        usuario=USUARIO,
    )


def cerrar_mes_de_noche(hoy: date | None = None) -> dict:
    """Corre el cierre si HOY es el último día del mes. Nunca levanta."""
    from filters import today_ec

    hoy = hoy or today_ec()
    res: dict = {"corrio": False, "periodo": f"{hoy.year:04d}-{hoy.month:02d}",
                 "pasos": {}, "alertas": []}
    if not es_ultimo_dia(hoy):
        return res
    res["corrio"] = True

    try:
        res["pasos"]["gastos"] = congelar_gastos_del_mes(hoy)
    except Exception as e:  # noqa: BLE001
        res["alertas"].append(f"CIERRE: no se pudieron congelar los gastos del mes: {e}")

    try:
        from modules.informes.queries import crear_snapshot_historia

        foto = crear_snapshot_historia(hoy.year, hoy.month, usuario=USUARIO, forzar=True)
        res["pasos"]["foto"] = {k: foto.get(k) for k in
                                ("aplicado", "id_historia", "patrimonio", "usuti", "razon")}
        if not foto.get("aplicado"):
            res["alertas"].append(f"CIERRE: la foto de cierre no se guardó: {foto.get('razon')}")
    except Exception as e:  # noqa: BLE001
        res["alertas"].append(f"CIERRE: la foto de cierre falló: {e}")

    try:
        from modules.informes import cierres_paquete

        tiene_pdf = cierres_paquete.obtener(hoy.year, hoy.month) is not None
        res["pasos"]["pdf"] = tiene_pdf
        if not tiene_pdf:
            res["alertas"].append(
                "CIERRE: el PDF de cierre no quedó guardado — se arma a mano desde "
                "/admin/regenerar-snapshot/ ANTES de la medianoche.")
    except Exception as e:  # noqa: BLE001
        res["alertas"].append(f"CIERRE: no se pudo verificar el PDF de cierre: {e}")

    for a in res["alertas"]:
        _LOG.warning(a)
    return res


def debe_pisar_el_cierre(filas: list[dict], fecha_cierre: date) -> bool:
    """La tarea del día 1 (06:00 EC), ¿rehace la foto del último día?

    Sólo si lo que hay es una foto diaria DE PASO (tomada antes de las 23:00 EC
    de ese día, p. ej. porque alguien abrió una pantalla a media tarde). Si hay
    una foto de la noche, o cualquier otra fila (el cierre nocturno, una
    reconstrucción a mano), NO se toca: rehacerla el día 1 mete los flujos del
    mes nuevo. `fecha_crea` está en UTC (23:00 EC = 04:00 UTC del día siguiente).
    Sin filas no hay nada que pisar (el cierre se crea igual).
    """
    from datetime import datetime, time, timedelta

    if not filas:
        return False
    limite_utc = datetime.combine(fecha_cierre + timedelta(days=1), time(4, 0))
    for f in filas:
        if (f.get("usuario_crea") or "") != "snapshot-diario":
            return False
        creada = f.get("fecha_crea")
        if creada is not None and getattr(creada, "tzinfo", None) is not None:
            creada = creada.replace(tzinfo=None)
        if creada is not None and creada >= limite_utc:
            return False
    return True
