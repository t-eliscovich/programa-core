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

    def _sigue_siendo_hoy(paso: str) -> bool:
        """Revisión 25/09: cada paso vuelve a mirar la fecha de Ecuador. Si la
        corrida cruzó la medianoche, el programa ya está en el mes nuevo y el
        paso guardaría números de ese mes con el rótulo del que cerró."""
        if today_ec() == hoy:
            return True
        res["alertas"].append(
            f"CIERRE: pasó la medianoche de Ecuador antes de {paso}; no se hizo "
            f"para no guardar números del mes nuevo. Rehacerlo a mano.")
        return False

    if _sigue_siendo_hoy("congelar los gastos"):
        try:
            res["pasos"]["gastos"] = congelar_gastos_del_mes(hoy)
        except Exception as e:  # noqa: BLE001
            res["alertas"].append(f"CIERRE: no se pudieron congelar los gastos del mes: {e}")

    if not _sigue_siendo_hoy("sacar la foto de cierre"):
        return res
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
        else:
            secciones = secciones_del_pdf(hoy.year, hoy.month)
            res["pasos"]["pdf_secciones"] = secciones
            total = len(cierres_paquete.PAGINAS)
            if secciones is not None and secciones < total:
                res["alertas"].append(
                    f"CIERRE: el PDF de cierre salió con {secciones} de {total} "
                    f"secciones — rehacerlo desde /admin/regenerar-snapshot/.")
    except Exception as e:  # noqa: BLE001
        res["alertas"].append(f"CIERRE: no se pudo verificar el PDF de cierre: {e}")

    for a in res["alertas"]:
        _LOG.warning(a)
    return res


def secciones_del_pdf(anio: int, mes: int) -> int | None:
    """Cuántas secciones entraron en el PDF guardado (None si no se sabe)."""
    import db

    fila = db.fetch_one(
        "SELECT paginas FROM scintela.cierre_paquete WHERE anio = %s AND mes = %s",
        (anio, mes),
    )
    if not fila or fila.get("paginas") is None:
        return None
    return int(fila["paginas"])


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


#: Columnas de la foto de cierre que un mes normal NUNCA deja en cero. Si el
#: ensayo saca alguna en 0, el mapeo balance → historia se rompió (agosto 2026
#: salió con banco = 0 y ustock = 0 por la rama en vivo y nadie lo vio).
COLUMNAS_QUE_NO_PUEDEN_SER_CERO = (
    "banco", "cart", "deuda", "patrimonio", "anticipos", "ustock", "uqui",
    "maquinaria", "realty", "stock", "kcom", "ucom", "ktej", "utej", "utin",
    "gasto", "gstotal", "kvent", "uvent", "usuti",
)


def ensayo(hoy: date | None = None, con_pdf: bool = True) -> dict:
    """ENSAYO del cierre nocturno — SÓLO LECTURA, no guarda nada.

    Tamara 2026-09-25: *"cómo podríamos hacer para buscar bugs o cosas que no
    van a quedar bien, la vez pasada todo estuvo mal"*. Corre HOY los mismos
    tres pasos que la última noche del mes, sin escribir:

      1. los gastos que se congelarían (tej/tin/adm del mes en curso);
      2. la fila de historia que guardaría la foto de cierre (rama en vivo,
         la misma de las 23:30), con sus chequeos: columnas en cero, activo
         que no cierra contra pasivo + patrimonio, retiros distintos de los
         del mes, gastos de la foto lejos de los que se congelan;
      3. el PDF de cierre armado en memoria (páginas, tamaño, segundos).

    Los flujos son del mes en curso hasta HOY (no el mes entero): sirve para
    encontrar lo que sale en cero, mal mapeado o roto, no para comparar montos.
    """
    import time

    from filters import today_ec

    hoy = hoy or today_ec()
    res: dict = {"hoy": str(hoy), "problemas": [], "gastos": None, "fila": None,
                 "pdf": None}

    try:
        from modules.informes.views import _gastos_mes_anterior_componentes

        g = _gastos_mes_anterior_componentes(meses_atras=0)
        res["gastos"] = {k: round(float(v or 0), 2) for k, v in g.items()}
        if sum(res["gastos"].values()) <= 0:
            res["problemas"].append("Los gastos del mes dan 0: no se podrían congelar.")
    except Exception as e:  # noqa: BLE001
        res["problemas"].append(f"Los gastos del mes no se pudieron calcular: {e}")

    fila = None
    try:
        from modules.informes.queries import crear_snapshot_historia, uret_mes_corriente

        foto = crear_snapshot_historia(hoy.year, hoy.month, usuario=USUARIO,
                                       dry_run=True, forzar_vivo=True)
        fila = foto.get("row")
        if not fila:
            res["problemas"].append(f"La foto de cierre no se pudo calcular: {foto.get('razon')}")
        else:
            res["fila"] = fila
            ceros = [c for c in COLUMNAS_QUE_NO_PUEDEN_SER_CERO
                     if not float(fila.get(c) or 0)]
            if ceros:
                res["problemas"].append("La foto de cierre sale con estas columnas en 0: "
                                        + ", ".join(ceros))
            from modules.admin_dbase.health_audit_view import _historia_balance_evaluar

            st, al = _historia_balance_evaluar(fila, "ensayo del cierre")
            res["balance"] = st
            if al:
                res["problemas"].append(
                    f"La foto de cierre no cierra: activo − (pasivo + patrimonio) = "
                    f"{st['delta']:+,.2f}")
            uret = float(uret_mes_corriente() or 0)
            if abs(float(fila.get("usret") or 0) - uret) > 1:
                res["problemas"].append(
                    f"Los retiros de la foto ({float(fila.get('usret') or 0):,.2f}) no son "
                    f"los del mes ({uret:,.2f}).")
            if res["gastos"]:
                g_total = sum(res["gastos"].values())
                gst = float(fila.get("gstotal") or 0)
                if g_total and abs(gst - g_total) > 0.05 * g_total:
                    res["problemas"].append(
                        f"Los gastos de la foto (gstotal {gst:,.2f}) y los que se congelan "
                        f"({g_total:,.2f}) difieren más de 5%.")
    except Exception as e:  # noqa: BLE001
        res["problemas"].append(f"La foto de cierre falló: {e}")

    if con_pdf:
        try:
            from modules.informes import cierres_paquete

            t0 = time.time()
            pdf, paginas = cierres_paquete.armar_pdf(hoy.year, hoy.month)
            res["pdf"] = {"secciones": paginas, "kb": round(len(pdf) / 1024),
                          "segundos": round(time.time() - t0, 1),
                          "de": len(cierres_paquete.PAGINAS)}
            if paginas < len(cierres_paquete.PAGINAS):
                res["problemas"].append(
                    f"El PDF salió con {paginas} de {len(cierres_paquete.PAGINAS)} secciones.")
        except Exception as e:  # noqa: BLE001
            res["problemas"].append(f"El PDF de cierre no se pudo armar: {e}")

    res["ok"] = not res["problemas"]
    return res
