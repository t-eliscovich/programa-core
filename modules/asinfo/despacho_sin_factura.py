"""Mercadería que salió de la fábrica y todavía no tiene factura.

TMT 2026-08-11. La dueña vio la utilidad caer $ 9.111 a las 10:10 y el renglón
del stock no ayudaba. Lo que había pasado: salieron 988 kg de tela terminada
(DES-000095134, "TONO CEN S/M ADJUNTO FOTO") y la factura no entró. Baja el
stock, no sube la cartera, y la utilidad se come el costo entero sin el ingreso.

**Esto NO es una campanita, y aprenderlo costó un aviso.** La primera versión
avisaba a las 17:00 con todo lo que ese día seguía sin factura. Estrenó con 6
despachos y 2.227 kg y quedó feo: de los 6, cuatro se facturaron esa misma
tarde y dos estaban anulados — *"no me gustó el aviso, parecía que algo estaba
mal cuando no era el caso"*. Y era esperable, no mala suerte: los 6 tenían
menos de 41 minutos de vida, y de 488 despachos medidos el 93 % se factura
dentro de la hora. Un ⚠ sobre el curso normal de la tarde entrena a ignorar el
panel.

**Durante el día no se dice NADA, en ningún lado.** Se evaluó mostrarlo como
contexto en `/informes/dia` —un renglón en kilos, sin ⚠— y la dueña lo descartó
igual: *"durante el día un renglón, esto no"*. Mientras la factura todavía
puede entrar, el dato no es información, es ruido con forma de información.

**Al día siguiente sí es un problema**: un despacho que amaneció sin factura ya
no se está por cargar (`sin_cargar_de_dias_anteriores`). Ese, y sólo ese, va a
la campanita. Medido: en los 90 días previos al 11/08 no hubo NINGUNO, así que
este aviso habla únicamente cuando de verdad hay algo roto.

**Un aviso por despacho, no uno por día.** La clave `desp-sin-factura:<numero>`
lo anuncia una vez y nunca más: si nadie lo carga, el mismo despacho no tiene
por qué gritar todas las mañanas hasta que alguien lo silencie a fuerza de
ignorarlo. Mismo criterio que `hilo_sin_of`.

**Los anulados NO cuentan, y esto costó un error.** La primera lectura contó 32
despachos "sin factura" desde el 01/08 y los reportó como 5.434 kg fuera del
balance. Al cruzarlos, 28 estaban ANULADOS (`fecha_anulacion` cargada, estado 0,
varios con "Despacho anulado Pedido: PDCL-…" en la glosa): nunca salieron de la
fábrica, así que no les corresponde factura y la mercadería volvió a bodega.
Mirar el flag de facturación sin mirar si el despacho sigue vivo convierte la
operación normal de un depósito en una alarma. Por eso `fecha_anulacion IS NULL`
va en la query y no en un filtro de Python: es parte de la definición.

🚨 **El reloj de Asinfo está en UTC; `fecha_creacion` NO.** Verificado en vivo
el 11/08: `GETDATE()` devolvió `2026-08-12 00:12` cuando en Ecuador eran las
19:12, y el último despacho de ese día figuraba a las 17:51 — hora de Ecuador.
O sea que las dos puntas de la comparación viven en husos distintos. Con
`CAST(GETDATE() AS date)` el "hoy" se adelantaba cinco horas: a partir de las
19:00 EC la consulta dejaba de ver el día en curso y devolvía cero, y la
antigüedad de cada despacho salía 5 h de más. Por eso todo lo que en esta
consulta signifique "ahora" pasa por `_AHORA_EC`, nunca por `GETDATE()` pelado.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger("programa_core.asinfo.despacho_sin_factura")

#: Hora de Ecuador a la que se pregunta por lo que amaneció sin factura. A las
#: 8 hay alguien para cargarlo; antes es un aviso al vacío.
HORA_AVISO = 8

#: Hasta dónde se mira para atrás. Con un aviso por despacho no hay riesgo de
#: repetirse, y 30 días alcanzan de sobra: en los 90 previos al 11/08 no hubo
#: ni uno solo.
DIAS_ATRAS = 30

#: "Ahora" en hora de Ecuador, dicho en SQL Server. Ver el 🚨 del docstring:
#: `GETDATE()` está en UTC y `fecha_creacion` en hora local, así que
#: compararlos crudos corre el día cinco horas.
_AHORA_EC = "DATEADD(hour, -5, GETDATE())"

_lock = threading.Lock()


def _ahora_ec() -> datetime:
    """Ahora en Ecuador (UTC−5, sin horario de verano) — igual que today_ec()."""
    return datetime.now(UTC) - timedelta(hours=5)


def _hora() -> int:
    try:
        h = int(os.environ.get("DESPACHO_SIN_FACTURA_HORA", str(HORA_AVISO)))
    except (TypeError, ValueError):
        return HORA_AVISO
    return h if 0 <= h <= 23 else HORA_AVISO


def pendientes() -> list[dict]:
    """Despachos vivos, sin factura, de los últimos `DIAS_ATRAS` días.

    Fail-soft: `[]` si Asinfo no habla — ni la pantalla ni el ciclo de fondo se
    caen porque el ERP esté lento.

    `fecha_anulacion IS NULL` es parte de la definición: un despacho anulado
    volvió a bodega y no le corresponde factura (ver el docstring del módulo).
    """
    from modules._lib import metabase_client

    sql = f"""
        SELECT dc.numero                                        AS numero,
               CONVERT(varchar(10), dc.fecha_creacion, 120)     AS dia,
               CONVERT(varchar(16), dc.fecha_creacion, 120)     AS creado,
               DATEDIFF(minute, dc.fecha_creacion, {_AHORA_EC}) AS minutos,
               LTRIM(RTRIM(ISNULL(dc.descripcion, '')))         AS nota,
               (SELECT ROUND(SUM(ISNULL(d.cantidad, 0)), 2)
                  FROM detalle_despacho_cliente d
                 WHERE d.id_despacho_cliente = dc.id_despacho_cliente) AS kg
          FROM despacho_cliente dc
         WHERE dc.fecha_creacion >= DATEADD(day, -{DIAS_ATRAS},
                                            CAST({_AHORA_EC} AS date))
           AND dc.fecha_anulacion IS NULL
           AND dc.indicador_generado_factura = 0
         ORDER BY dc.fecha_creacion
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=500)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("no pude leer los despachos sin factura: %s", e)
        return []

    out = []
    for r in rows or []:
        try:
            kg = float(r.get("kg") or 0)
        except (TypeError, ValueError):
            continue
        if kg <= 0:
            continue
        out.append({
            "numero": str(r.get("numero") or "").strip(),
            "dia": str(r.get("dia") or "").strip(),
            "kg": round(kg, 2),
            "nota": " ".join(str(r.get("nota") or "").split()),
            "creado": str(r.get("creado") or "").strip()[-5:],
            "horas": round(float(r.get("minutos") or 0) / 60.0, 1),
        })
    return out


def sin_cargar_de_dias_anteriores() -> list[dict]:
    """Los que amanecieron sin factura. Esto sí es un problema."""
    hoy = _ahora_ec().date().isoformat()
    return [c for c in pendientes() if c["dia"] and c["dia"] < hoy]


def _titulo(c: dict) -> str:
    from filters import num_es
    return (f"Despacho sin factura del {c['dia']} · "
            f"{num_es(c['kg'], 0)} kg · {c['numero']}")


def _detalle(c: dict) -> str:
    from filters import num_es
    nota = f" · {c['nota']}" if c["nota"] else ""
    return (f"Salió el {c['dia']} a las {c['creado']} y sigue sin factura. "
            f"{num_es(c['kg'], 1)} kg{nota}")


def revisar_si_toca() -> dict:
    """A partir de las 08:00 EC, un aviso por cada despacho que amaneció sin
    factura. Idempotente por número: cada uno se anuncia una sola vez."""
    res = {"avisados": 0, "casos": 0, "motivo": ""}
    if os.environ.get("DESPACHO_SIN_FACTURA", "1").strip() == "0":
        res["motivo"] = "apagado"
        return res
    with _lock:
        if _ahora_ec().hour < _hora():
            res["motivo"] = f"todavía no son las {_hora()}"
            return res
        try:
            casos = sin_cargar_de_dias_anteriores()
        except Exception as e:  # noqa: BLE001
            _LOG.warning("revisión falló: %s", e)
            res["motivo"] = str(e)[:200]
            return res
        res["casos"] = len(casos)
        if not casos:
            # Lo normal. Un día en que se cargó todo no enciende nada.
            res["motivo"] = "no quedó nada sin cargar"
            return res

        from modules.avisos import queries as avisos
        for c in casos:
            if avisos.avisar(
                fuente="ventas",
                nivel="alerta",
                titulo=_titulo(c)[:200],
                detalle=_detalle(c),
                cantidad=1,
                # TMT 2026-08-19 (dueña): el aviso llevaba a /informes/dia
                # (la explicación de la utilidad), que pide `informes.ver` —
                # justo lo que NO tiene el rol que carga las facturas. Va al
                # cuadre del día, que es donde se ve el despacho sin factura
                # y donde se resuelve.
                url=f"/facturas/dia?fecha={c['dia']}",
                clave=f"desp-sin-factura:{c['numero']}",
            ):
                res["avisados"] += 1
    if res["avisados"]:
        _LOG.info("despachos sin factura de días anteriores: %s", res["avisados"])
    return res
