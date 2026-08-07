"""La explicación del día: POR QUÉ subió (o bajó) la utilidad.

TMT 2026-08-04 (dueña): *"quiero agregar un check diario, quiero que cada día
puedas darme una explicación de por qué subió la utilidad. comparás balance de
mañana y a fin de día"*. Y sobre el residuo, sin lugar a interpretación:
*"no debería haber residuo.. y tenemos que ir entrenando esto"*.

## De qué se hace cargo esto que la grabadora no

`traza_utilidad` (mig 0150) ya contesta **qué componente** se movió: stock,
cartera, pasivos. Eso alcanza para descartar hipótesis, no para explicar.
Explicar es decir **qué documento** lo movió: la factura 12345, el cheque de
ELF, la compra de hilado.

Llegar a ese nivel tiene un obstáculo concreto: `scripts/import_dbf.py` borra
POSDAT, COMPRA, DOLARES, ACTIVOS y RETIROS **enteras** en cada sync, así que
`fecha_crea` queda pisada con la hora del sync y la pregunta "¿qué se cargó
hoy?" no tiene respuesta en esas tablas. La única forma que sobrevive a eso es
no preguntarle a la fila cuándo nació, sino **guardar una foto propia y
diffearla**. Lo que está en la foto nueva y no en la vieja, entró.

## El invariante

    Δ utilidad = Σ (aporte de cada movimiento)

sin residuo, siempre. Se cumple **por construcción**: si el detalle por
documento no llega a explicar el componente entero, la diferencia se guarda
como una fila sintética `#ajuste:<componente>`, con nombre y a la vista.

Eso NO es hacer trampa con la cuadratura — es lo contrario. Un `#ajuste` que
aparece es una tarea del entrenamiento: alguien tiene que mirar por qué ese
pedazo no se explica y escribir la regla. El día que no aparezca ninguno, el
sistema explica el 100% de lo que se movió. **Ese es el número que mide el
progreso, no la cuadratura.**

## Familias: lo que mueve la utilidad y lo que sólo la cambia de lugar

Una cobranza no genera utilidad: la factura baja $1.000 y el cheque sube
$1.000. Netea cero **solo**, sin que haya que emparejar documentos, porque
cada movimiento aporta con el signo de su componente. Por eso los movimientos
se agrupan en familias que *deberían* netear cero:

    cobranza  = factura que baja  +  cheque que entra
    depósito  = cheque que sale   +  banco que sube
    pago      = banco que baja    +  pasivo que baja

Cuando una familia NO netea cero, esa diferencia es la explicación de verdad:
una retención, un descuento, una nota de crédito, un cheque devuelto. Ahí es
donde hay que mirar.

## Gotchas que ya están adentro

· **Hora de ECUADOR** (UTC−5) para todo. El server corre en UTC: después de
  las 19:00 EC ya está en el día siguiente.
· **Los pasivos aportan al revés.** Si `totp` sube, la utilidad baja.
· **La maquinaria baja sola todos los días.** `activos_totales()` prorratea la
  amortización por día del mes (MENU.PRG). No es un error y tiene su propia
  regla para que no asuste.
· **La tarifa del stock revalúa TODO de un saque.** Por eso `dia_detalle`
  guarda `cantidad` y `precio` aparte: un Δ de stock se parte en "entraron
  kilos" (Δkg × precio viejo) y "cambió el $/kg" (kg nuevos × Δprecio).
"""
from __future__ import annotations

import logging
import os

import db

# ── El motor de la foto vive en `foto.py` ───────────────────────────────────
# Hasta la mig 0171 todo esto estaba acá adentro y corría dos veces por día.
# Ahora corre cada 5 minutos desde `traza.py` y la explicación del día es la
# SUMA de esas ventanas. Se re-exportan los nombres porque son los mismos de
# siempre: lo que cambió es dónde viven y cada cuánto se ejecutan.
from modules.informes.foto import (  # noqa: F401  (re-export)
    _CLAVE_BALANCE,
    COMPONENTES,
    ETIQUETAS,
    FAMILIAS,
    SIGNO,
    UMBRAL,
    _det_activos,
    _det_antic,
    _det_bancos,
    _det_caja,
    _det_cheques,
    _det_facturas,
    _det_totp,
    _det_uret,
    _det_vqx,
    _det_vsto,
    _f,
    _rows,
    ahora_ec,
    detalle,
    hoy_ec,
    regla,
)
from modules.informes.foto import diff as _diff  # noqa: F401  (re-export)
from modules.informes.foto import guardada as _foto_guardada  # noqa: F401

_LOG = logging.getLogger("programa_core.dia")

#: Horas de Ecuador en las que se clavan las dos capturas ancla.
#: TMT 2026-08-05: *"quiero este resumen a las 6.10"* — 18:10 de Quito. El
#: cierre pasa de las 19:00 a las 18:00 EC (la captura entra en el primer tick
#: del hilo después de la hora, así que sale ~18:00-18:02).
#: ⚠ El cierre más temprano deja fuera la última hora de fábrica, así que el
#: día de HOY y los anteriores no son estrictamente comparables. La ventana
#: sigue siendo de 24 h: es cierre-a-cierre, sólo que corrida una hora.
#: Se puede pisar por entorno con DIA_HORA_MANANA / DIA_HORA_CIERRE sin tocar
#: código, que es como se prueba un horario nuevo antes de fijarlo acá.
HORA_MANANA = 7
HORA_CIERRE = 18


def _hora(env: str, default: int) -> int:
    try:
        h = int(os.environ.get(env, str(default)))
    except (TypeError, ValueError):
        return default
    return h if 0 <= h <= 23 else default


# ── Captura: el ancla del día ───────────────────────────────────────────────

def capturar(momento: str = "manual", bal: dict | None = None) -> dict:
    """Marca el ANCLA del día sobre una foto de la traza.

    Hasta la mig 0171 esta función sacaba su propia foto de detalle y la
    diffeaba contra la anterior. Esa maquinaria ahora corre cada 5 minutos en
    `traza.registrar()`, así que acá queda sólo lo propio del día: la fila que
    le pone nombre a una de esas fotos —"la de las 07:00 del 6 de agosto"— y de
    la que cuelga la nota de la dueña.

    Los KILOS por etapa sí se guardan acá (mig 0162): no se pueden reconstruir
    para atrás, y la traza no guarda la tarifa de tejido ni la de terminado.

    Nunca levanta: si algo falla, se loguea y devuelve `{"ok": False}`. Esto
    cuelga del hilo de fondo y no puede tumbar nada.
    """
    res = {"ok": False, "id_captura": None, "id_traza": None,
           "movimientos": 0, "motivo": ""}
    try:
        from modules.informes import traza as _traza
        foto = _traza.registrar(origen="dia", momento=momento, bal=bal)
        if not foto.get("ok"):
            res["motivo"] = foto.get("motivo") or "no se pudo sacar la foto"
            return res
        bal = foto.get("bal") or bal
        comp = ((bal or {}).get("diagnostico") or {}).get("componentes") or {}

        fila = {"fecha_ec": hoy_ec(), "momento": (momento or "manual")[:20],
                "id_traza": foto.get("id_traza"),
                "utilidad": _f(comp.get("utilidad")),
                "patr_neto": round(_f(comp.get("patr")) - _f(comp.get("uret")), 2)}
        for c, _s in COMPONENTES:
            fila[c] = _f(comp.get(_CLAVE_BALANCE[c]))
        etapas = (bal or {}).get("stock_etapas") or {}
        for et in ("hilado", "tejido", "terminado"):
            e = etapas.get(et) or {}
            fila[f"{et}_kg"] = _f(e.get("kg")) or None
            fila[f"{et}_ukg"] = _f(e.get("ukg")) or None

        cols = list(fila.keys())
        campos = ", ".join(cols)
        marcas = ", ".join(f"%({c})s" for c in cols)
        cap = db.execute_returning(
            f"INSERT INTO scintela.dia_captura ({campos}) VALUES ({marcas}) "
            f"ON CONFLICT DO NOTHING RETURNING id_captura",
            fila,
        )
        if not cap:
            # El índice único de ancla ya tenía la captura de este momento: el
            # hilo de fondo pasó dos veces. No es un error — y la foto que se
            # sacó recién no se tira, queda como una foto más de la traza.
            res["motivo"] = "ya había captura de este momento"
            res["id_traza"] = foto.get("id_traza")
            return res
        res.update(ok=True, id_captura=cap["id_captura"],
                   id_traza=foto.get("id_traza"),
                   movimientos=int(foto.get("movimientos") or 0),
                   primera=bool(foto.get("primera")))
    except Exception as e:  # noqa: BLE001 -- cuelga del hilo de fondo
        _LOG.warning("dia: no pude capturar (%s)", e)
        res["motivo"] = str(e)[:150]
    return res

def correr_si_toca() -> dict:
    """Entrada del hilo de fondo. Clava las dos capturas ancla del día.

    La idempotencia NO se apoya en una variable de proceso (se pierde en cada
    restart, y el server reinicia): la garantiza el índice único
    `(fecha_ec, momento)` de la tabla. El hilo puede pasar cien veces.
    """
    res = {"capturado": "", "motivo": ""}
    if os.environ.get("DIA_EXPLICACION", "1").strip() == "0":
        res["motivo"] = "apagado"
        return res
    try:
        h = ahora_ec().hour
        hoy = hoy_ec()
        pendientes = []
        if h >= _hora("DIA_HORA_CIERRE", HORA_CIERRE):
            pendientes.append("cierre")
        if h >= _hora("DIA_HORA_MANANA", HORA_MANANA):
            pendientes.append("manana")
        if not pendientes:
            res["motivo"] = "todavía no son las 7"
            return res
        hechas = {r["momento"] for r in _rows(
            "SELECT momento FROM scintela.dia_captura WHERE fecha_ec = %s", (hoy,))}
        for m in pendientes:
            if m in hechas:
                continue
            r = capturar(m)
            if r.get("ok"):
                res["capturado"] = m
                if m == "cierre":
                    # La nota sale con la foto del cierre, no antes: es lo que
                    # esa foto explica.
                    res["nota"] = enviar_nota(hoy)
                _LOG.info("dia: captura '%s' de %s (%s movimientos)",
                          m, hoy, r.get("movimientos"))
                break
            res["motivo"] = r.get("motivo") or ""
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.warning("dia: correr_si_toca (%s)", e)
        res["motivo"] = str(e)[:150]
    return res


# ── El resumen para accionistas ─────────────────────────────────────────────
# TMT 2026-08-05 (dueña, viendo la primera versión): *"la explicación debería
# ser más: se produjo tanta tela, eso cambió de valor de x a x. se vendió tanto
# etc. algo más senior resumido digestible para accionistas"*.
#
# El desglose por componente y por documento contesta "¿de dónde salió cada
# peso?" — la pregunta del que audita. El accionista hace otra: **qué pasó en
# la fábrica**. Esa se contesta en KILOS y en tres frases, no en once
# componentes contables.
#
# Las tres cifras derivadas de abajo son la parte que hace que el resumen
# suene a fábrica y no a balance. Cada una sale de una identidad simple, y
# cada una lleva su supuesto escrito al lado — se marcan como ESTIMADAS en la
# pantalla porque un ajuste de inventario las corre sin avisar:
#
#   producción terminada ≈ Δ kg terminado + kg vendidos
#   consumo de hilado    ≈ kg comprados  − Δ kg hilado
#   cobrado              ≈ $ facturado   − Δ cartera de facturas

ETAPAS = (("hilado", "Hilado"), ("tejido", "Tejido crudo"),
          ("terminado", "Tela terminada"))


def ventas_del_dia(fecha) -> dict:
    """Lo facturado ESE día: {n, kg, us}. Por `fecha` del documento, no por
    `fecha_crea` (que el sync del dBase pisa). Las anuladas no cuentan, así que
    un resumen que se vuelve a mirar meses después dice la verdad de hoy."""
    r = _rows(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS us
          FROM scintela.factura
         WHERE fecha = %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """, (fecha,))
    d = r[0] if r else {}
    return {"n": int(d.get("n") or 0), "kg": _f(d.get("kg")), "us": _f(d.get("us"))}


def compras_del_dia(fecha) -> dict:
    """Lo comprado ESE día: {n, kg, us}. Mismo criterio de fecha."""
    r = _rows(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS us
          FROM scintela.compra
         WHERE fecha = %s
        """, (fecha,))
    d = r[0] if r else {}
    return {"n": int(d.get("n") or 0), "kg": _f(d.get("kg")), "us": _f(d.get("us"))}


def produccion_del_dia(fecha) -> dict:
    """Kilos PRODUCIDOS, DESPACHADOS y DESPERDICIADOS ese día.

    ⭐ TMT 2026-08-05: *"se produce 24/7, algo no sabés"*. Tenía razón.

    La primera versión DERIVABA la producción del stock
    (`Δ kg terminado + kg vendidos`). Está mal por tres motivos, y el error
    medido fue de 2,4× (5.280 kg derivados contra 12.838 reales el 04/08):

    1. **Hay medida directa**: `cantidad_fabricada` de las órdenes de
       fabricación que cierran en bodega 53. No hay por qué derivar nada.
    2. **El saldo de bodega no cuadra** contra producido − despachado: la
       pantalla `/produccion-terminado-asinfo` tiene una columna
       *"diferencia de medición"* que en agosto acumulaba +11.539 kg, más de
       un tercio de la producción del mes. Cualquier derivación arrastra eso.
    3. **Falta el desperdicio** — ~5 % en terminado. No está ni en el stock ni
       en las ventas: se evapora entre las dos puntas.

    Así que esto NO calcula nada: lee la fila del día de
    `terminado_asinfo.resumen()`, que ya modela
    `INICIAL + PRODUCIDO − VENDIDO = FINAL` con el desperdicio al costado.
    Duplicar la cuenta habría sido inventar un segundo número que discute con
    el primero.

    ⚠ Los kg se imputan al día en que la orden **CIERRA**, no a los días en
    que se produjo. Una orden que tejió tres días descarga todo en uno, así
    que el diario es grumoso aunque la planta no pare: un día en cero
    normalmente significa "no cerró ninguna orden", no "no se trabajó". Por
    eso se devuelve también el acumulado del mes, que es donde el ruido se
    promedia.
    """
    vacio = {"disponible": False}
    try:
        from modules.terminado_asinfo import service as _term
        r = _term.resumen(fecha.year, fecha.month)
    except Exception as e:  # noqa: BLE001 -- Asinfo caído no tumba la pantalla
        _LOG.warning("dia: no pude leer la producción (%s)", e)
        return vacio
    if not (r or {}).get("disponible"):
        return vacio
    dias = ((r.get("dias") or {}).get("filas")) or []
    clave = fecha.isoformat()
    hoy = next((f for f in dias if f.get("periodo") == clave), None)
    mes = (r.get("dias") or {}).get("total") or {}
    # Promedio de los días que SÍ cerraron órdenes: sin meta contra la cual
    # medir (la dueña: *"no tenemos meta"*), la referencia es la propia
    # tendencia. Los días en cero la hundirían sin significar nada.
    con_prod = [f for f in dias if _f(f.get("producido")) > 0]
    prom = (round(sum(_f(f["producido"]) for f in con_prod) / len(con_prod), 2)
            if con_prod else None)
    if not hoy:
        return {"disponible": True, "sin_fila": True, "mes": mes,
                "promedio_dia": prom, "dias_con_produccion": len(con_prod)}
    return {
        "disponible": True, "sin_fila": False,
        "producido": _f(hoy.get("producido")),
        "despachado": _f(hoy.get("vendido")),
        "desperdicio_kg": _f(hoy.get("desperdicio_kg")),
        "desperdicio_pct": hoy.get("desperdicio_pct"),
        "n_ofs": int(hoy.get("n_ofs") or 0),
        "inicial": hoy.get("inicial"), "final": hoy.get("final"),
        "otros": hoy.get("otros"),
        "mes": mes, "promedio_dia": prom,
        "dias_con_produccion": len(con_prod),
    }


def tejido_del_dia(fecha) -> dict:
    """Kilos de tela CRUDA que ENTRARON a bodega 52 ese día (tejeduría).

    TMT 2026-08-05: *"en producción deberíamos decir cuánto se tejió y cuánto
    se terminó"*. Son dos etapas distintas: el hilo se teje (crudo) y recién
    después se tiñe y se termina. Un día puede tejer mucho y terminar poco.

    🚨 **La fuente es el INGRESO A BODEGA, no las OFs cerradas.** La primera
    versión sumaba `cantidad_fabricada` de las OFs hoja cerradas en bodega 52
    y daba **0 kg** el 05/08 contra los **7.623** que mostraba
    `/produccion-tejeduria-asinfo` — dos números distintos para la misma
    pregunta, en dos pantallas de la misma app. Las OFs **subcuentan**: dejan
    afuera lo que está en máquina sin cerrar. La dueña ya había zanjado esto
    el 20/07 para el diario de tejeduría: *"tiene que sumar el ingreso a
    bodega (179.704), no las OFs cerradas (207.623)"*.

    Por eso esto llama a la **misma función** que esa pantalla
    (`asinfo.ingreso_bodega_por_dia(52, …)`) en vez de escribir una consulta
    propia: si mañana cambia el criterio, cambia en los dos lados a la vez.
    """
    vacio = {"disponible": False}
    try:
        from datetime import date as _date

        from modules.asinfo import service as _asinfo
        dias = _asinfo.ingreso_bodega_por_dia(
            52, _date(fecha.year, fecha.month, 1)) or []
    except Exception as e:  # noqa: BLE001 -- Asinfo caído no tumba la pantalla
        _LOG.warning("dia: no pude leer la tejeduría (%s)", e)
        return vacio
    if not dias:
        return vacio
    clave = fecha.isoformat()
    hoy = next((d for d in dias if str(d.get("dia") or "")[:10] == clave), None)
    return {
        "disponible": True,
        "kg": _f((hoy or {}).get("kg")),
        "mes_kg": round(sum(_f(d.get("kg")) for d in dias), 2),
    }


def _etapa(desde: dict, hasta: dict, et: str) -> dict:
    """Una etapa de stock entre dos capturas, con el valor partido en kilos y
    tarifa. `None` si esa captura no guardó los kilos (capturas viejas, o
    Asinfo caído)."""
    kg0, kg1 = desde.get(f"{et}_kg"), hasta.get(f"{et}_kg")
    p0, p1 = desde.get(f"{et}_ukg"), hasta.get(f"{et}_ukg")
    if kg0 is None or kg1 is None or p0 is None or p1 is None:
        return {}
    kg0, kg1, p0, p1 = _f(kg0), _f(kg1), _f(p0), _f(p1)
    return {
        "kg0": kg0, "kg1": kg1, "d_kg": round(kg1 - kg0, 2),
        "p0": p0, "p1": p1, "d_p": round(p1 - p0, 4),
        "us0": round(kg0 * p0, 2), "us1": round(kg1 * p1, 2),
        "d_us": round(kg1 * p1 - kg0 * p0, 2),
        "por_kilos": round((kg1 - kg0) * p0, 2),
        "por_tarifa": round(kg1 * (p1 - p0), 2),
    }


def resumen(fecha=None) -> dict:
    """El día contado como se lo cuenta a un accionista: qué se produjo, qué se
    vendió, cuánto cambió de valor la tela y qué quedó de resultado.

    🚨 TMT 2026-08-07: la ventana es la MISMA que la de la pantalla —
    `ventana()`, cierre de ayer contra cierre de hoy, 24 h—. Antes esta
    función comparaba la primera contra la última foto del MISMO día
    (07:00→18:00, once horas): la misma ventana de doce horas que `ventana()`
    había corregido el 05/08 por dejar afuera el turno noche. El error no se
    veía en pantalla porque el Δ utilidad grande sale de `explicar()`, que sí
    usaba `ventana()`; asomó cuando la nota del cierre por mail empezó a salir
    de acá y decía un "Hoy" distinto al de la pantalla, y encima siempre con
    el cartel *"tramo corto, no son 24 h"* — porque con las dos puntas del
    mismo día `dia_parcial` daba True SIEMPRE.
    """
    fecha = fecha or hoy_ec()
    out = {"fecha": fecha, "ok": False, "dia_parcial": False,
           "etapas": [], "frases": []}
    d, h = ventana(fecha)
    if not d or not h:
        return out
    v, c = ventas_del_dia(fecha), compras_del_dia(fecha)

    etapas = {}
    for et, rot in ETAPAS:
        e = _etapa(d, h, et)
        if e:
            e["rotulo"] = rot
            etapas[et] = e
            out["etapas"].append(dict(e, clave=et))

    por_kilos = round(sum(e["por_kilos"] for e in etapas.values()), 2)
    por_tarifa = round(sum(e["por_tarifa"] for e in etapas.values()), 2)
    d_stock = round(_f(h.get("vsto")) - _f(d.get("vsto")), 2)
    d_cartera = round(_f(h.get("facturas")) - _f(d.get("facturas")), 2)
    d_deuda = round(_f(h.get("totp")) - _f(d.get("totp")), 2)
    d_util = round(_f(h.get("utilidad")) - _f(d.get("utilidad")), 2)

    hil = etapas.get("hilado") or {}
    prod = produccion_del_dia(fecha)
    out.update({
        "ok": True, "desde": d, "hasta": h,
        # Lo setea `ventana()`: si el arranque es del mismo día, el tramo es
        # más corto que 24 h. Tiene que llegar hasta el mensaje de WhatsApp o
        # se manda un número que no se puede comparar contra otros días sin
        # que nadie lo sepa.
        "dia_parcial": bool(d.get("fecha_ec") == h.get("fecha_ec")),
        "d_utilidad": d_util, "ventas": v, "compras": c,
        "d_stock": d_stock, "por_kilos": por_kilos, "por_tarifa": por_tarifa,
        "d_cartera": d_cartera, "d_deuda": d_deuda,
        "produccion": prod,
        # Lo cobrado sigue siendo derivado (facturado − Δ cartera), pero acá la
        # identidad es exacta salvo retenciones y NC — se cruzó contra la plata
        # que entró a bancos+caja+cheques y dio $292 de diferencia sobre
        # $61.847. La producción, en cambio, ya NO se deriva: ver
        # `produccion_del_dia`.
        "cobrado": round(v["us"] - d_cartera, 2),
        "tarifa_quieta": abs(_f(hil.get("d_p"))) < 0.00005 if hil else None,
        # Precio realizado y margen: los dos números con los que se dirige una
        # fábrica. El costo de lo despachado se valúa a la tarifa de terminado.
        "precio_kg": (round(v["us"] / v["kg"], 4) if v["kg"] else None),
    })
    ter = etapas.get("terminado") or {}
    # ⚠ El costo se valúa con los kilos FACTURADOS, no con los despachados.
    # Verificado en vivo el 05/08: se habían facturado 545 kg pero despachado
    # 2.069 físicos (la mercadería sale un día y se factura otro), y el margen
    # salía **−134,7 %**. Son dos universos distintos: la plata viene de la
    # factura, así que el costo tiene que venir de los mismos kilos que esa
    # factura. Los despachados siguen contándose aparte, en la línea de
    # producción, que es donde significan algo.
    if ter.get("p1") and v["kg"]:
        costo = round(v["kg"] * _f(ter["p1"]), 2)
        out["costo_despachado"] = costo
        out["margen"] = round(v["us"] - costo, 2)
        out["margen_pct"] = (round(100.0 * out["margen"] / v["us"], 1)
                             if v["us"] else None)
    return out


# ── El mensaje de WhatsApp ──────────────────────────────────────────────────
# TMT 2026-08-05: *"me gustaría también mostrar la utilidad hasta hoy, algo para
# mandar chiquito e informativo a mí, andres y federico por whatsapp"*.
#
# ⭐ La utilidad del balance YA ES la del mes acumulada — `PATR − PATANT +
# retiros`, contra el último cierre. O sea que "la utilidad hasta hoy" no hay
# que calcularla: es el mismo número que la pantalla muestra arriba. Lo que
# cambia es el encuadre: el titular pasa a ser el ACUMULADO y el día queda
# como el aporte de la jornada.
#
# Se genera el texto y se muestra para copiar. **Programa Core no manda nada**:
# el mensaje sale del teléfono de quien aprieta, con su WhatsApp y su cuenta.

#: Ancho cómodo de lectura en un teléfono. Más que esto y WhatsApp corta las
#: líneas por su cuenta, en el lugar equivocado.
ANCHO_WA = 34

_MESES = ("ene", "feb", "mar", "abr", "may", "jun",
          "jul", "ago", "sep", "oct", "nov", "dic")
_DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

#: Nombres largos para el titular de la pantalla. `strftime('%B')` sale en
#: inglés salvo que el server tenga el locale es_* instalado — y no lo tiene.
MESES_LARGOS = {1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo",
                6: "junio", 7: "julio", 8: "agosto", 9: "septiembre",
                10: "octubre", 11: "noviembre", 12: "diciembre"}


def ventas_del_mes(fecha) -> dict:
    """Lo facturado en el mes hasta `fecha` inclusive."""
    r = _rows(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(kg), 0) AS kg,
               COALESCE(SUM(importe), 0) AS us
          FROM scintela.factura
         WHERE fecha >= date_trunc('month', %s::date)::date
           AND fecha <= %s
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """, (fecha, fecha))
    d = r[0] if r else {}
    return {"n": int(d.get("n") or 0), "kg": _f(d.get("kg")), "us": _f(d.get("us"))}


def _n(v, dec: int = 0) -> str:
    from filters import num_es
    return num_es(round(_f(v), dec), dec)


def porque_subio(e: dict, n: int = 3) -> list[dict]:
    """Las `n` líneas que explican el día + un "Resto", **sumando exacto**.

    Es el bloque *Por qué subió* de la pantalla. La última fila tiene que dar
    `e["d_utilidad"]` o el lector deja de creerle a la tabla: por eso el resto
    NO se descarta ni se redondea, se calcula como la diferencia contra el
    total. Ahí adentro caen los movimientos chicos y los traspasos, que netean
    cero de a pares.

    🚨 Las etiquetas son las que da el motor (`Venta facturada`, `Stock`,
    `Amortización del día`). Tentación a evitar: poner acá *"Costo de la tela
    vendida −$ 70.573"*, que es el número del margen. No es un movimiento: el
    componente de stock se mueve por lo que se produjo Y por lo que salió, y
    mezclarlos rompería la suma. La cuenta del margen va en el titular, en
    castellano, donde no promete cuadrar con nada.
    """
    total = _f((e or {}).get("d_utilidad"))
    filas = [r for r in ((e or {}).get("reglas") or [])
             if r.get("familia") in ("utilidad", "sin_explicar")
             and abs(_f(r.get("aporte"))) >= 1]
    filas.sort(key=lambda r: abs(_f(r.get("aporte"))), reverse=True)
    top = filas[:max(0, n)]
    out = [{"label": r.get("regla") or "—", "monto": round(_f(r.get("aporte")), 2),
            "familia": r.get("familia")} for r in top]
    resto = round(total - sum(x["monto"] for x in out), 2)
    if abs(resto) >= 1:
        out.append({"label": "Resto — stock, caja, bancos", "monto": resto,
                    "familia": "resto"})
    return out


def deuda_hoy(fecha=None) -> dict:
    """La deuda a proveedores (posdatados) y cuánto de eso vence pronto.

    TMT 2026-08-05: *"no decís nada de posdatados"*. La utilidad ya los tiene
    adentro — `totp` aporta con signo −1, una deuda nueva la baja — pero el
    **nivel** y el **vencimiento** no salían en ningún lado del día, y son la
    otra mitad de la pregunta de un accionista: no sólo *cuánto ganamos*, sino
    *cuánto hay que pagar y cuándo*.

    ⭐ Pasivos = `posdat` con **`banc = 0`** (los `banc = 9` son cheques ya
    emitidos, no deuda abierta). Es la misma definición que usa el Balance:
    si un día no coinciden, el que está mal es éste.
    """
    fecha = fecha or hoy_ec()
    r = _rows(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(importe), 0) AS total,
               COALESCE(SUM(importe) FILTER (WHERE fechad < %s), 0) AS vencido,
               COALESCE(SUM(importe) FILTER (
                   WHERE fechad >= %s AND fechad < %s + 7), 0) AS prox7,
               COALESCE(SUM(importe) FILTER (
                   WHERE fechad >= %s AND fechad < %s + 30), 0) AS prox30
          FROM scintela.posdat
         WHERE banc = 0 AND NOT COALESCE(anulada, FALSE)
        """, (fecha, fecha, fecha, fecha, fecha))
    d = r[0] if r else {}
    return {
        "n": int(d.get("n") or 0),
        "total": _f(d.get("total")),
        "vencido": _f(d.get("vencido")),
        "prox7": _f(d.get("prox7")),
        "prox30": _f(d.get("prox30")),
    }


def motores_del_dia(fecha, n: int = 3) -> list[dict]:
    """Las `n` reglas que MÁS movieron la utilidad del día, con su aporte.

    TMT 2026-08-05: la nota tiene que contestar *por qué* subió, no sólo
    *cuánto*. El motor ya lo sabe — `explicar()` agrupa cada movimiento por
    regla — así que esto es puro recorte y orden, sin una consulta nueva.

    ⭐ **Sólo entran las familias que mueven el resultado.** Un `traspaso`
    (cobranza, depósito, pago) netea cero *de a pares*, así que mostrar una
    punta sola —"Cheque recibido +$ 61.847"— haría parecer que la cobranza
    generó utilidad. No la genera: la cambia de lugar.

    ⭐ **`sin_explicar` SÍ entra.** Si lo que más movió la utilidad es un
    `#ajuste`, esconderlo sería mentir sobre el porqué justo el día que más
    importa saberlo. Es la misma regla que en la pantalla, donde el `#ajuste`
    baja el `explicado_pct` en vez de disimularse.
    """
    try:
        e = explicar(fecha)
    except Exception as exc:  # noqa: BLE001
        # La nota es un extra: si la explicación se cae, el resumen igual sale.
        _LOG.warning("dia: no pude armar los motores del día (%s)", exc)
        return []
    filas = [r for r in (e.get("reglas") or [])
             if r.get("familia") in ("utilidad", "sin_explicar")
             and abs(_f(r.get("aporte"))) >= 1]
    filas.sort(key=lambda r: abs(_f(r.get("aporte"))), reverse=True)
    return filas[:max(0, n)]


def _lineas_motores(motores: list[dict]) -> list[str]:
    """Las filas del bloque *Lo movió hoy*, de ANCHO_WA exacto.

    Dos detalles que sólo se ven en un teléfono:
    · **Los importes se alinean entre sí**, no cada uno contra el borde. El
      ancho del número se calcula sobre el bloque entero, así `$ 41.200` y
      `$  3.532` quedan en columna y se comparan de un vistazo. Alinear cada
      línea por su cuenta deja el `$` bailando.
    · **El nombre se corta con puntos suspensivos** si no entra: una línea de
      35 caracteres la parte WhatsApp donde se le antoja y el bloque deja de
      leerse en columna.
    """
    if not motores:
        return []
    nums = [_n(abs(_f(m.get("aporte")))) for m in motores]
    ancho_num = max(len(n) for n in nums)
    out = []
    for m, num in zip(motores, nums):  # noqa: B905
        signo = "+" if _f(m.get("aporte")) >= 0 else "−"
        monto = f"{signo}$ {num:>{ancho_num}}"
        sitio = ANCHO_WA - len(monto) - 1
        if sitio < 1:                  # un importe absurdo: que vaya solo
            out.append(monto[:ANCHO_WA])
            continue
        nombre = m.get("regla") or "—"
        if len(nombre) > sitio:
            nombre = nombre[:sitio - 1].rstrip() + "…"
        out.append(f"{nombre:<{sitio}} {monto}")
    return out


def ventanas_sin_cerrar(fecha=None) -> dict:
    """Las ventanas del día cuyos documentos no llegaron a dar el Δ.

    ⭐ TMT 2026-08-07, sobre el mismo aviso en la pantalla: *"esto no me gusta,
    me genera mucha duda"*. Un cartel permanente que no dice qué hacer es
    ansiedad; en la nota del cierre, en cambio, es un resumen del día que se
    mira una vez y se cierra. Mismo dato, distinto lugar.

    Las fotos anteriores a la grabadora NO cuentan: no es que no cierren, es
    que nunca tuvieron registro. Meterlas sería inventar 200 problemas.
    """
    fecha = fecha or hoy_ec()
    filas = _rows(
        """
        WITH v AS (
            SELECT t.id_traza, t.utilidad,
                   LAG(t.utilidad) OVER (ORDER BY t.creado_en, t.id_traza) AS ant
              FROM scintela.traza_utilidad t
             WHERE (t.creado_en AT TIME ZONE 'America/Guayaquil')::date = %s
        )
        SELECT v.id_traza,
               ROUND((v.utilidad - v.ant)::numeric, 2)          AS d,
               ROUND(COALESCE(SUM(m.aporte), 0)::numeric, 2)    AS explicado,
               COUNT(m.id_mov) FILTER (WHERE m.tipo = 'reconstruido') AS recon,
               COUNT(m.id_mov)                                   AS n
          FROM v
          LEFT JOIN scintela.dia_movimiento m ON m.id_traza = v.id_traza
         WHERE v.ant IS NOT NULL
         GROUP BY v.id_traza, v.utilidad, v.ant
        """, (fecha,))
    sueltas = [f for f in filas
               if int(f.get("n") or 0) > int(f.get("recon") or 0)
               and abs(_f(f.get("d")) - _f(f.get("explicado"))) >= 1]
    return {"n": len(sueltas),
            "monto": round(sum(abs(_f(f["d"]) - _f(f["explicado"]))
                               for f in sueltas), 2)}


def mensaje_whatsapp(fecha=None) -> str:
    """El resumen del día en texto plano, listo para pegar en WhatsApp.

    Reglas de formato que importan y no son obvias:
    · **Nada de tablas ni markdown raro.** WhatsApp sólo entiende `*negrita*`
      y `_cursiva_`. Una tabla con pipes se ve como basura en un teléfono.
    · **Una idea por línea**, ordenadas de más a menos importante: quien lo
      lee en el celular corta a la tercera.
    · **Sin líneas de relleno.** Si un dato no está, la línea no va — un cero
      o un guión ocupan lo mismo que un número y no dicen nada.
    """
    fecha = fecha or hoy_ec()
    r = resumen(fecha)
    if not r.get("ok"):
        return ""

    h = r["hasta"]
    rot = f"{_DIAS[fecha.weekday()]} {fecha.day} {_MESES[fecha.month - 1]}"
    L = [f"*INTELA · {rot}*", ""]

    # El titular es el ACUMULADO del mes; el día, su aporte.
    L.append(f"*Utilidad de {_MESES[fecha.month - 1]}: "
             f"$ {_n(h.get('utilidad'), 0)}*")
    d = r["d_utilidad"]
    L.append(f"{'Hoy +' if d >= 0 else 'Hoy −'}$ {_n(abs(d), 0)}")
    if r.get("dia_parcial"):
        L.append("_(tramo corto, no son 24 h)_")
    L.append("")

    # El porqué va ARRIBA de los kilos: es lo que un accionista pregunta
    # apenas ve el número, y el que lee en el celular corta a la tercera
    # línea. Si el día no movió nada explicable, el bloque entero no va —
    # misma regla que el resto: un título con nada abajo no dice nada.
    motores = motores_del_dia(fecha)
    if motores:
        L.append("*Lo movió hoy*")
        L.extend(_lineas_motores(motores))
        L.append("")

    p = r.get("produccion") or {}
    # Un 0 acá casi nunca significa "no se produjo": significa que todavía no
    # cerró ninguna orden. A media mañana es siempre 0. Misma regla que el
    # resto: si el dato no dice nada, la línea no va.
    if p.get("disponible") and not p.get("sin_fila") and _f(p.get("producido")):
        linea = f"Producción  {_n(p.get('producido'))} kg"
        mes = p.get("mes") or {}
        if mes.get("producido"):
            linea += f" · mes {_n(mes['producido'])}"
        L.append(linea)

    v, vm = r["ventas"], ventas_del_mes(fecha)
    if v["n"]:
        L.append(f"Ventas      $ {_n(v['us'])} · {_n(v['kg'])} kg")
    if vm["us"]:
        L.append(f"  mes       $ {_n(vm['us'])} · {_n(vm['kg'])} kg")
    if r.get("margen_pct") is not None:
        L.append(f"Margen      {_n(r['margen_pct'], 1)} %")
    if r.get("cobrado"):
        L.append(f"Cobrado     $ {_n(r['cobrado'])}")

    # Lo que no se pudo explicar por documento. Va al final y sólo si lo hay:
    # una línea que aparece todos los días entrena a no leerla.
    sc = ventanas_sin_cerrar(fecha)
    if sc["n"]:
        L.append("")
        L.append(f"_{sc['n']} ventana{'' if sc['n'] == 1 else 's'} sin explicar "
                 f"· $ {_n(sc['monto'])}_")

    return "\n".join(L).rstrip()


# ── Lectura: la explicación ─────────────────────────────────────────────────

# ── La nota del cierre, por mail ────────────────────────────────────────────

def destinatarios() -> list[dict]:
    """A quién le llega la nota. Se administran POR PANTALLA."""
    return _rows(
        "SELECT id_destinatario, correo, nombre, activo "
        "  FROM scintela.nota_destinatario ORDER BY activo DESC, correo")


def agregar_destinatario(correo: str, nombre: str, usuario: str) -> tuple[bool, str]:
    correo = (correo or "").strip().lower()
    if "@" not in correo or " " in correo or len(correo) > 200:
        return False, "Ese correo no parece un correo."
    try:
        db.execute(
            "INSERT INTO scintela.nota_destinatario (correo, nombre, usuario_crea) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (correo, (nombre or "").strip()[:80] or None, (usuario or "")[:40]))
        return True, f"{correo} agregado."
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude agregar destinatario (%s)", e)
        return False, "No se pudo agregar."


def cambiar_destinatario(id_destinatario: int, activo: bool) -> bool:
    try:
        db.execute("UPDATE scintela.nota_destinatario SET activo = %s "
                   " WHERE id_destinatario = %s", (bool(activo), int(id_destinatario)))
        return True
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude cambiar el destinatario (%s)", e)
        return False


def enviar_nota(fecha=None, forzar: bool = False) -> dict:
    """Manda la nota del día por mail. Nunca lanza.

    🚨 La idempotencia la da la BASE (`dia_captura.nota_enviada_en`), no una
    variable de proceso: el hilo de fondo pasa cada dos minutos y el server
    reinicia, así que cualquier memoria en RAM manda el mail dos veces. Misma
    lección que el índice único de las capturas ancla.

    El sello se pone ANTES de mandar y sólo si el UPDATE agarra la fila —así
    dos procesos simultáneos no pueden mandar los dos—. Si después el envío
    falla, se limpia para poder reintentar.
    """
    from modules._lib import mailer

    fecha = fecha or hoy_ec()
    res = {"ok": False, "motivo": "", "destinatarios": 0, "id": ""}
    try:
        cierre = _rows(
            "SELECT id_captura, nota_enviada_en FROM scintela.dia_captura "
            " WHERE fecha_ec = %s AND momento = 'cierre' LIMIT 1", (fecha,))
        if not cierre:
            res["motivo"] = "todavía no hay captura de cierre"
            return res
        idc = cierre[0]["id_captura"]
        if cierre[0].get("nota_enviada_en") and not forzar:
            res["motivo"] = "la nota de hoy ya se mandó"
            return res

        correos = [d["correo"] for d in destinatarios() if d.get("activo")]
        if not correos:
            res["motivo"] = "no hay destinatarios activos"
            return res
        if not mailer.habilitado():
            res["motivo"] = mailer.motivo_no_disponible()
            return res

        if not forzar:
            tomada = db.execute(
                "UPDATE scintela.dia_captura SET nota_enviada_en = CURRENT_TIMESTAMP "
                " WHERE id_captura = %s AND nota_enviada_en IS NULL", (idc,))
            if not tomada:
                res["motivo"] = "la nota de hoy ya se mandó"
                return res

        texto = mensaje_whatsapp(fecha)
        dia_txt = fecha.strftime("%d/%m") if hasattr(fecha, "strftime") else str(fecha)
        env = mailer.enviar(f"INTELA · cierre del {dia_txt}", texto, correos)
        if not env.get("ok"):
            if not forzar:
                # Se libera para poder reintentar en la vuelta siguiente.
                db.execute("UPDATE scintela.dia_captura SET nota_enviada_en = NULL "
                           " WHERE id_captura = %s", (idc,))
            res["motivo"] = env.get("motivo") or "no se pudo mandar"
            return res
        if forzar:
            db.execute("UPDATE scintela.dia_captura SET nota_enviada_en = "
                       "CURRENT_TIMESTAMP WHERE id_captura = %s", (idc,))
        res.update(ok=True, destinatarios=len(correos), id=env.get("id") or "")
    except Exception as e:  # noqa: BLE001 -- cuelga del hilo de fondo
        _LOG.warning("dia: no pude mandar la nota (%s)", e)
        res["motivo"] = str(e)[:150]
    return res


def capturas(fecha) -> list[dict]:
    return _rows(
        """
        SELECT *, TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil', 'HH24:MI') AS hora
          FROM scintela.dia_captura
         WHERE fecha_ec = %s
         ORDER BY creado_en ASC
        """, (fecha,))


def ventana(fecha) -> tuple[dict | None, dict | None]:
    """Las dos puntas del día: el cierre de AYER y el cierre de HOY.

    ⭐ TMT 2026-08-05, corrigiendo el diseño original: *"se produce 24/7"*.
    La primera versión comparaba la foto de las 07:00 contra la de las 19:00 —
    doce horas. Eso sirve para una oficina; para una planta que no para deja
    afuera el turno noche entero, que es donde cierra buena parte de las
    órdenes de fabricación. Peor: las ventas y las compras del día SÍ son de
    24 h, así que la comparación mezclaba dos ventanas distintas y hacía
    parecer que la fábrica producía la mitad de lo que produce.

    Ahora el día son **24 horas**: la última captura de ayer contra la última
    de hoy. Si no hay captura de ayer (el primer día, o el server estuvo
    caído), cae en la primera de hoy y se avisa, porque ese tramo es más corto
    y los números no son comparables contra los de otros días.
    """
    hoy = capturas(fecha)
    if not hoy:
        return None, None
    ayer = _rows(
        """
        SELECT *, TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil', 'HH24:MI') AS hora
          FROM scintela.dia_captura
         WHERE fecha_ec < %s
         ORDER BY creado_en DESC
         LIMIT 1
        """, (fecha,))
    if ayer:
        return ayer[0], hoy[-1]
    return (hoy[0], hoy[-1]) if len(hoy) > 1 else (None, None)


def _movimientos_ventana(desde: dict, hasta: dict) -> list[dict]:
    """Todo lo que se movió entre las dos anclas del día.

    Desde la mig 0171 los movimientos cuelgan de la FOTO de la traza, que corre
    cada 5 minutos: la ventana del día son todas las fotos POSTERIORES al ancla
    de arranque y hasta la de cierre, inclusive. Cruza la medianoche, así que
    se filtra por id y no por fecha.

    ⭐ Las capturas anteriores a la mig 0171 no tienen foto asociada y sus
    movimientos siguen colgando de `id_captura`. No se reescribe historia: se
    lee por donde está.
    """
    t0, t1 = desde.get("id_traza"), hasta.get("id_traza")
    if t0 and t1:
        return _rows(
            "SELECT * FROM scintela.dia_movimiento "
            " WHERE id_traza > %s AND id_traza <= %s "
            " ORDER BY ABS(aporte) DESC", (t0, t1))
    return _rows(
        "SELECT * FROM scintela.dia_movimiento "
        " WHERE id_captura > %s AND id_captura <= %s "
        " ORDER BY ABS(aporte) DESC",
        (desde.get("id_captura"), hasta.get("id_captura")))


def _renglones(movs: list[dict], desde: dict, hasta: dict) -> list[dict]:
    """Los movimientos del día agrupados IGUAL que en la traza.

    🚨 TMT 2026-08-07: la traza aprendió hoy a nombrar los hechos por
    `mov_doble` —"4 FA · JVL, AJT, NIF +1", "retenciones · 24 facturas",
    "CH → BC · KRH"— y la nota seguía agrupando por `regla`, que son los
    baldes genéricos de antes. O sea que lo único de todo esto que SALE de la
    app y lo leen otros tenía todos los defectos que la dueña hizo corregir en
    la pantalla: decía "11 facturas nuevas" cuando fue un totalizar deshecho y
    no se emitió ninguna, llamaba "Cheque depositado o dado de baja" a plata
    que no sabía qué hizo, y no juntaba nada.

    Es la regla de siempre: dos clasificadores = UNA función compartida. Acá
    se llama a la misma `traza.resumir()` que arma la pantalla y se devuelve
    con las claves que la nota ya usaba (`regla`, `familia`, `aporte`, `n`),
    así los consumidores no cambian — sólo mejoran los nombres.

    `d_utilidad=None` a propósito: el renglón "diferencia contra el Δ" lo pone
    la pantalla; acá el resto lo calcula `_top()`, que ya lo hacía.

    Fail-soft: si algo del agrupado falla, la nota sale con los baldes viejos.
    """
    try:
        from modules.informes import eventos as _ev
        from modules.informes import traza as _tz

        d0, d1 = (desde or {}).get("creado_en"), (hasta or {}).get("creado_en")
        idx = _ev.indice(_ev.de_la_ventana(d0, d1), _ev.transacciones(d0, d1))
        return [{"regla": g.get("texto") or "—", "familia": g.get("familia"),
                 "aporte": _f(g.get("aporte")), "n": int(g.get("n") or 0)}
                for g in _tz.resumir(movs, None, idx)]
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude agrupar como la traza (%s)", e)
        return []


def explicar(fecha=None) -> dict:
    """La explicación del día: Δ utilidad, quién lo movió, y qué falta explicar.

    La ventana son **24 horas** — cierre de ayer contra cierre de hoy. Ver
    `ventana()` para por qué no son las 07:00→19:00 del diseño original.
    """
    fecha = fecha or hoy_ec()
    caps = capturas(fecha)
    out = {
        "fecha": fecha, "capturas": caps, "ok": False, "motivo": "",
        "desde": None, "hasta": None, "d_utilidad": 0.0, "dia_parcial": False,
        "componentes": [], "movimientos": [], "familias": [],
        "sin_explicar": [], "residuo": 0.0, "explicado_pct": 100.0,
        "ventana_nula": False,
    }
    desde, hasta = ventana(fecha)
    if not desde or not hasta:
        out["motivo"] = ("Todavía no hay con qué comparar: hace falta una captura "
                         "de ayer o una segunda de hoy.")
        if caps:
            out["desde"] = caps[0]
        return out

    # Si el arranque es del mismo día, el tramo es más corto que 24 h y los
    # números no se pueden comparar contra los de otros días.
    out["dia_parcial"] = bool(desde.get("fecha_ec") == hasta.get("fecha_ec"))
    # 🚨 VENTANA NULA — las dos fotos son del MISMO minuto.
    # Pasa todos los días: la captura automática de la mañana y un "Capturar
    # ahora" caen juntos, o la app levanta y toma dos seguidas. Ahí el Δ de la
    # utilidad es 0 por construcción, pero las ventas y la producción del día
    # SÍ tienen números — y la pantalla los mostraba al lado, así que decía
    # "la utilidad quedó igual" arriba y "la venta dejó $ 49.226" abajo. Las
    # dos frases no pueden ser ciertas a la vez.
    # Con esta bandera la pantalla deja de afirmar sobre el día: el titular
    # pasa al acumulado del mes y lo derivado del Δ (lo cobrado, el porqué) no
    # se muestra. Lo MEDIDO del día —facturado, kilos, deuda— sigue.
    out["ventana_nula"] = bool(desde.get("hora") and
                               desde.get("hora") == hasta.get("hora"))
    out["desde"], out["hasta"] = desde, hasta
    out["d_utilidad"] = round(_f(hasta.get("utilidad")) - _f(desde.get("utilidad")), 2)

    for c, s in COMPONENTES:
        d = round(_f(hasta.get(c)) - _f(desde.get(c)), 2)
        if abs(d) < UMBRAL:
            continue
        out["componentes"].append({
            "col": c, "label": ETIQUETAS[c], "delta": d,
            "aporte": round(d * s, 2),
        })
    out["componentes"].sort(key=lambda x: abs(x["aporte"]), reverse=True)

    # Todos los movimientos de la ventana: las capturas POSTERIORES al
    # arranque y hasta el cierre, inclusive. Cruza la medianoche, así que se
    # filtra por id_captura y no por fecha_ec.
    movs = _movimientos_ventana(desde, hasta)
    out["movimientos"] = movs

    por_fam: dict[str, dict] = {}
    por_regla: dict[str, dict] = {}
    for m in movs:
        fam = m.get("familia") or "utilidad"
        f = por_fam.setdefault(fam, {"familia": fam, "aporte": 0.0, "n": 0})
        f["aporte"] = round(f["aporte"] + _f(m.get("aporte")), 2)
        f["n"] += 1
        r = por_regla.setdefault(m.get("regla") or "—", {
            "regla": m.get("regla") or "—", "familia": fam, "aporte": 0.0, "n": 0})
        r["aporte"] = round(r["aporte"] + _f(m.get("aporte")), 2)
        r["n"] += 1
    out["familias"] = sorted(por_fam.values(), key=lambda x: abs(x["aporte"]), reverse=True)
    # Los MISMOS renglones que la traza; si el agrupado falla, los baldes.
    out["reglas"] = (_renglones(movs, desde, hasta)
                     or sorted(por_regla.values(),
                               key=lambda x: abs(x["aporte"]), reverse=True))
    out["sin_explicar"] = [m for m in movs if (m.get("familia") == "sin_explicar")]

    total = round(sum(_f(m.get("aporte")) for m in movs), 2)
    out["residuo"] = round(out["d_utilidad"] - total, 2)
    ciego = round(sum(abs(_f(m.get("aporte"))) for m in out["sin_explicar"]), 2)
    bruto = round(sum(abs(_f(m.get("aporte"))) for m in movs), 2)
    out["explicado_pct"] = round(100.0 * (1 - ciego / bruto), 1) if bruto else 100.0
    out["ok"] = abs(out["residuo"]) < 1 and not out["sin_explicar"]
    return out


def racha_limpia(dias: int = 30) -> int:
    """Días consecutivos (hacia atrás desde ayer) sin nada sin explicar.

    Es la métrica del entrenamiento: mientras haya `#ajuste`, hay trabajo.
    """
    filas = _rows(
        """
        WITH dias AS (
            SELECT DISTINCT fecha_ec
              FROM scintela.dia_captura
             WHERE fecha_ec < (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date
             ORDER BY fecha_ec DESC
             LIMIT %s
        ), movs AS (
            -- Un movimiento se imputa al día de SU FOTO. Los anteriores a la
            -- mig 0171 no tienen foto y se imputan al día de su captura.
            SELECT COALESCE((t.creado_en AT TIME ZONE 'America/Guayaquil')::date,
                            c.fecha_ec) AS fecha_ec,
                   m.familia
              FROM scintela.dia_movimiento m
              LEFT JOIN scintela.traza_utilidad t ON t.id_traza   = m.id_traza
              LEFT JOIN scintela.dia_captura   c ON c.id_captura = m.id_captura
        )
        SELECT d.fecha_ec,
               COUNT(*) FILTER (WHERE m.familia = 'sin_explicar') AS ciegos
          FROM dias d
          LEFT JOIN movs m ON m.fecha_ec = d.fecha_ec
         GROUP BY d.fecha_ec
         ORDER BY d.fecha_ec DESC
        """, (max(1, min(365, dias)),))
    n = 0
    for f in filas:
        if int(f.get("ciegos") or 0):
            break
        n += 1
    return n


def guardar_nota(id_captura: int, nota: str) -> bool:
    """La nota que la dueña escribe cuando entiende algo que el sistema no."""
    try:
        db.execute("UPDATE scintela.dia_captura SET nota = %s WHERE id_captura = %s",
                   ((nota or "").strip()[:2000] or None, int(id_captura)))
        return True
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dia: no pude guardar la nota (%s)", e)
        return False
