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
precio es que si en el medio la bodega se movió por algo que producción y
despacho no explican, el encadenado se separa de la realidad — por eso la tab
pide ADEMÁS la foto real al final del período y muestra la diferencia si no
cierra. Un descuadre ahí no es un bug de la pantalla: es exactamente la clase
de cosa que la dueña quería poder ver cuando el balance no le cierra.

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


def _encadenar(periodos: list[dict], vendido_por_periodo: dict,
               movimiento_por_periodo: dict, inicial):
    """Arma las filas de una tabla encadenando el stock.

    `periodos` = [{periodo, n_ofs, issued, fab}] ordenado. `inicial` puede ser
    None (Asinfo no dio la foto): en ese caso las columnas de stock quedan en
    None y la tabla igual muestra los flujos.

    `otros` es la columna que en pantalla se llama DIF. DE MEDICIÓN:

        otros = (ingreso_real − producido) − (egreso_real − vendido)

    Con eso el encadenado CIERRA POR CONSTRUCCIÓN con el stock de Asinfo,
    porque `inicial + ingreso_real − egreso_real` es exactamente la identidad
    telescópica de `saldo_producto_lote`. Sin la columna, el final calculado se
    separaba 33 t del real en 8 meses y no había dónde ver por qué.

    ⚠ QUÉ ES Y QUÉ NO ES (TMT 2026-08-04, deep dive que pidió la dueña después
    de *"sí, esto me suena raro"*). La primera versión de esta pantalla la
    llamaba "otros mov." y decía que eran devoluciones, reprocesos y ajustes.
    Es FALSO. El perfil por tamaño de julio
    (`/admin/debug-terminado-otros?perfil=2026-07`) devolvió CERO movimientos
    de 100 kg o más: 14.611 ingresos de 19,6 kg promedio —un rollo— y 98% de
    ellos altas de lote. No hay ninguna devolución grande que ir a buscar.
    La causa es que `saldo_producto_lote` es una FOTO DIARIA: el rollo que se
    produce y se despacha el mismo día no queda contado ni en `fab` ni en
    despacho, pero la bodega sí lo registró. En 2026 la bodega movió 1.949.445
    de ingreso y 1.950.129 de egreso (neto −684) contra 2.076.668 producidos y
    2.112.416 vendidos declarados (neto −35.748): las DOS puntas declaradas
    corren cortas (6,1% y 7,7%) y casi se cancelan; el residuo de 1,6% es esta
    columna. El balance NO se ve afectado: usa la foto real del stock.
    Por eso el nombre es "dif. de medición" y la celda va en gris, no en ámbar.
    """
    hay_mov = bool(movimiento_por_periodo)
    filas = []
    saldo = inicial
    for p in periodos:
        clave = p["periodo"]
        producido = round(float(p.get("fab") or 0.0), 2)
        consumido = round(float(p.get("issued") or 0.0), 2)
        vendido = round(float(vendido_por_periodo.get(clave) or 0.0), 2)
        desperdicio = round(consumido - producido, 2)

        mov = movimiento_por_periodo.get(clave) or {}
        # Sin la fuente de movimientos no inventamos un "otros": la tabla
        # vuelve al encadenado simple (producido − vendido) y la columna queda
        # en None para que se vea que ese dato no está.
        otros_ing = (round(float(mov.get("ingreso") or 0.0) - producido, 2)
                     if hay_mov else None)
        otros_egr = (round(float(mov.get("egreso") or 0.0) - vendido, 2)
                     if hay_mov else None)
        otros = None if not hay_mov else round(otros_ing - otros_egr, 2)

        neto = producido - vendido + (otros or 0.0)
        final = None if saldo is None else round(saldo + neto, 2)
        filas.append({
            "periodo": clave,
            "n_ofs": int(p.get("n_ofs") or 0),
            "inicial": saldo,
            "producido": producido,
            "vendido": vendido,
            "otros": otros,
            "otros_ingreso": otros_ing,
            "otros_egreso": otros_egr,
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
    def _suma(campo):
        vals = [f[campo] for f in filas if f[campo] is not None]
        return round(sum(vals), 2) if vals else None

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
        "otros": _suma("otros"),
        "otros_ingreso": _suma("otros_ingreso"),
        "otros_egreso": _suma("otros_egreso"),
        "final": filas[-1]["final"] if filas else None,
        "desperdicio_kg": desperdicio,
        "desperdicio_pct": (round(100.0 * desperdicio / consumido, 2)
                            if consumido > 0 else None),
    }


def _tabla(periodos, vendido, movimiento, inicial):
    filas = _encadenar(periodos, vendido, movimiento, inicial)
    return {"filas": filas, "total": _totalizar(filas)}


def _todos_los_periodos(fabricacion: list[dict], *fuentes) -> list[dict]:
    """La lista de períodos de una tabla = la UNIÓN de las tres fuentes.

    TMT 2026-08-04. La primera versión iteraba SÓLO los períodos con
    fabricación, y eso escondía días enteros: el 01 y el 02 de agosto tuvieron
    despachos y movimientos de bodega pero ninguna OF cerrada, así que no
    aparecían — que es exactamente lo que la dueña preguntó ("¿y que el 1 o el
    2 de agosto no se terminó nada?"). Peor: sus kg quedaban afuera del
    encadenado, así que la tabla por día no cerraba con la tabla por mes (el
    03/08 mostraba −7.808 de otros mov. contra +8.925 del mes).

    Un período sin fabricación entra con fab/issued en 0 — que es la verdad:
    no se cerró ninguna orden ese día, pero la bodega se movió igual.
    """
    por_clave = {f["periodo"]: dict(f) for f in fabricacion}
    for fuente in fuentes:
        for clave in (fuente or {}):
            por_clave.setdefault(clave, {"periodo": clave, "n_ofs": 0,
                                         "fab": 0.0, "issued": 0.0})
    return [por_clave[k] for k in sorted(por_clave)]


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
    mov_mes = asinfo_service.movimiento_bodega_por_mes(BODEGA_TERMINADO, anio)
    mov_dia = asinfo_service.movimiento_bodega_por_dia(BODEGA_TERMINADO, anio, mes)

    # Un ancla por tabla, cada una con la foto REAL de su arranque. Y los
    # períodos son la UNIÓN de las tres fuentes: un día con despacho pero sin
    # OF cerrada TAMBIÉN es una fila (ver `_todos_los_periodos`).
    meses = _tabla(_todos_los_periodos(meses_raw, vendido_mes, mov_mes),
                   vendido_mes, mov_mes,
                   _stock_terminado_a_fecha(_fin_de_mes_anterior(anio, 1)))
    dias = _tabla(_todos_los_periodos(dias_raw, vendido_dia, mov_dia),
                  vendido_dia, mov_dia,
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
