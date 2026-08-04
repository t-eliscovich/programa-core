"""Producción de TERMINADO (Asinfo, bodega 53) — el movimiento del stock.

TMT 2026-08-04, en cuatro mensajes de la dueña:
  1. *"hacé una copia de tejeduría asinfo y ponéme un terminado asinfo"*
  2. *"no hay compras"*
  3. *"no quiero artículos, quiero cantidades por día, así entendemos por qué
     el balance o la duda de utilidad"*
  4. *"merma llamalo desperdicio y poné inicial producido vendido final"* ·
     *"para cada mes"*

O sea: la pantalla no es un listado de producción, es el **movimiento del
stock de terminado**, que es justo lo que mueve el balance:

        INICIAL + PRODUCIDO − VENDIDO = FINAL

y al costado el DESPERDICIO, que no es un movimiento de esta bodega sino la
pérdida del proceso (kg de tela cruda que entraron menos kg de terminado que
salieron). Dos tablas con las mismas columnas: una fila por MES del año, y
abajo el mes elegido abierto día por día para ir a buscar qué día pasó.

Es la hermana de `modules/tejeduria_asinfo`, pero sin nada del maquilero
(tarifas $/kg, match contra compras tipo K, auto-carga de compra + pasivo):
en bodega 53 todas las OFs son de `jproduccion`, no hay tercero, no hay
factura que cruzar.

DE DÓNDE SALE CADA COLUMNA
  · producido / desperdicio → `asinfo.fabricacion_flujo_por_mes/_por_dia(53)`,
    que es LA MISMA query del informe Flujo de producción con un GROUP BY. La
    suma de las filas da exacto el número de aquella pantalla.
  · vendido → `asinfo.despacho_fisico_por_mes/_por_dia(53)`: el despacho físico
    que salió por la puerta (no la factura, que es un documento y corre con
    otro timing).
  · inicial → foto REAL del stock de bodega 53 en Asinfo
    (`inventario_por_etapa_a_fecha`), UNA sola vez, al arranque de la tabla.
  · final → inicial + producido − vendido, y el final de una fila es el inicial
    de la siguiente.

POR QUÉ EL INICIAL SE ANCLA UNA SOLA VEZ Y NO FILA POR FILA
Cada foto as-of es una query pesada sobre `saldo_producto_lote`: doce anclas
serían doce queries de varios segundos cada una y la pantalla tardaría un
minuto en abrir. Con un ancla y el encadenado, son 3 queries en total. El
precio es que si en el medio hubo un movimiento que no es producción ni
despacho (una devolución de cliente, un ajuste de inventario), el encadenado
se separa de la realidad — por eso la tab pide ADEMÁS la foto real al final
del período y muestra la diferencia si no cierra. Un descuadre ahí no es un
bug de la pantalla: es exactamente la clase de cosa que la dueña quería poder
ver cuando el balance no le cierra.

Todo fail-soft: si Asinfo no contesta, `disponible=False` y la tab muestra el
aviso en vez de romperse.
"""
from datetime import date, timedelta

from filters import today_ec
from modules.asinfo import service as asinfo_service

#: Bodega de Asinfo que produce el terminado. Constante y no parámetro: si
#: algún día hace falta otra, se agrega una pantalla, no un query param (que
#: se interpolaría en el SQL de Metabase).
BODEGA_TERMINADO = 53


def _fin_de_mes_anterior(anio: int, mes: int) -> str:
    """Último día del mes ANTERIOR a (anio, mes), en ISO. Es la fecha de corte
    del stock inicial: la foto se toma con `fecha <= corte`, inclusive."""
    return (date(int(anio), int(mes), 1) - timedelta(days=1)).isoformat()


def _stock_terminado_a_fecha(fecha_iso: str):
    """Kg de bodega 53 en Asinfo al cierre de `fecha_iso`, o None si el ERP no
    contestó. None y no 0.0: un cero se leería como "no había stock"."""
    foto = asinfo_service.inventario_por_etapa_a_fecha(fecha_iso) or {}
    if not foto.get("disponible"):
        return None
    return round(float(foto.get("terminada") or 0.0), 2)


def _encadenar(periodos: list[dict], vendido_por_periodo: dict, inicial):
    """Arma las filas de una tabla encadenando el stock.

    `periodos` = [{periodo, n_ofs, issued, fab}] ordenado. `inicial` puede ser
    None (Asinfo no dio la foto): en ese caso las columnas de stock quedan en
    None y la tabla igual muestra producido / vendido / desperdicio.
    """
    filas = []
    saldo = inicial
    for p in periodos:
        clave = p["periodo"]
        producido = round(float(p.get("fab") or 0.0), 2)
        consumido = round(float(p.get("issued") or 0.0), 2)
        vendido = round(float(vendido_por_periodo.get(clave) or 0.0), 2)
        desperdicio = round(consumido - producido, 2)
        final = (None if saldo is None
                 else round(saldo + producido - vendido, 2))
        filas.append({
            "periodo": clave,
            "n_ofs": int(p.get("n_ofs") or 0),
            "inicial": saldo,
            "producido": producido,
            "vendido": vendido,
            "final": final,
            "desperdicio_kg": desperdicio,
            # Sin crudo consumido no hay porcentaje posible. None y no 0.0: un
            # 0,00% se lee como "no hubo desperdicio", que es otra cosa.
            "desperdicio_pct": (round(100.0 * desperdicio / consumido, 2)
                                if consumido > 0 else None),
        })
        saldo = final
    return filas


def _totalizar(filas: list[dict]) -> dict:
    """Fila TOTAL: los flujos se suman, el stock se toma de las puntas."""
    producido = round(sum(f["producido"] for f in filas), 2)
    vendido = round(sum(f["vendido"] for f in filas), 2)
    desperdicio = round(sum(f["desperdicio_kg"] for f in filas), 2)
    consumido = producido + desperdicio
    return {
        "periodo": "",
        "n_ofs": sum(f["n_ofs"] for f in filas),
        "inicial": filas[0]["inicial"] if filas else None,
        "producido": producido,
        "vendido": vendido,
        "final": filas[-1]["final"] if filas else None,
        "desperdicio_kg": desperdicio,
        "desperdicio_pct": (round(100.0 * desperdicio / consumido, 2)
                            if consumido > 0 else None),
    }


def _tabla(periodos, vendido, inicial):
    filas = _encadenar(periodos, vendido, inicial)
    return {"filas": filas, "total": _totalizar(filas)}


def resumen(anio: int, mes: int) -> dict:
    """Las dos tablas de la pantalla.

    Devuelve:
        {disponible, anio, mes,
         meses: {filas, total},   # una fila por mes de `anio`
         dias:  {filas, total},   # una fila por día de (anio, mes)
         control: {fecha, real, calculado, dif} | None}

    `control` compara el FINAL encadenado del último mes contra la foto real
    del stock en Asinfo a esa fecha. Si no cierra, la pantalla lo dice.
    """
    anio, mes = int(anio), int(mes)

    meses_raw = asinfo_service.fabricacion_flujo_por_mes(BODEGA_TERMINADO, anio)
    dias_raw = asinfo_service.fabricacion_flujo_por_dia(BODEGA_TERMINADO, anio, mes)
    vendido_mes = asinfo_service.despacho_fisico_por_mes(anio, BODEGA_TERMINADO)
    vendido_dia = asinfo_service.despacho_fisico_por_dia(anio, mes, BODEGA_TERMINADO)

    # Un ancla por tabla, cada una con la foto REAL de su arranque.
    meses = _tabla(meses_raw, vendido_mes,
                   _stock_terminado_a_fecha(_fin_de_mes_anterior(anio, 1)))
    dias = _tabla(dias_raw, vendido_dia,
                  _stock_terminado_a_fecha(_fin_de_mes_anterior(anio, mes)))

    # Control: ¿el encadenado del año llega al stock que Asinfo dice tener al
    # cierre del último mes con movimiento?
    control = None
    if meses["filas"] and meses["total"]["final"] is not None:
        ultimo = meses["filas"][-1]["periodo"]          # 'YYYY-MM'
        try:
            y, m = (int(x) for x in ultimo.split("-")[:2])
            # Cierre del último mes = víspera del primero del siguiente. Pero
            # el mes en curso todavía no cerró: pedirle a Asinfo la foto al
            # 31/08 estando a 4 de agosto devuelve igual el stock de HOY y el
            # rótulo miente. Se corta en el día que sea más chico.
            sig_y, sig_m = (y + 1, 1) if m == 12 else (y, m + 1)
            corte = min(_fin_de_mes_anterior(sig_y, sig_m),
                        today_ec().isoformat())
        except (ValueError, TypeError):
            corte = None
        real = _stock_terminado_a_fecha(corte) if corte else None
        if real is not None:
            calculado = meses["total"]["final"]
            control = {"fecha": corte, "real": real, "calculado": calculado,
                       "dif": round(calculado - real, 2)}

    return {
        "disponible": bool(meses["filas"] or dias["filas"]),
        "anio": anio,
        "mes": mes,
        "meses": meses,
        "dias": dias,
        "control": control,
    }
