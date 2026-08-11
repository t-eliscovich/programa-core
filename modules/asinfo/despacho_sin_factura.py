"""Mercadería que salió hoy y a las 17:00 todavía no tiene factura.

TMT 2026-08-11. La dueña vio la utilidad caer $ 9.111 a las 10:10 y el renglón
del stock no ayudaba. Lo que había pasado: salieron 988 kg de tela terminada
(DES-000095134, "TONO CEN S/M ADJUNTO FOTO") y la factura no entró. Baja el
stock, no sube la cartera, y la utilidad se come el costo entero sin el ingreso.

**El umbral está MEDIDO, no elegido.** Se cruzaron los 488 despachos vivos
desde el 03/08 contra `factura_cliente.id_despacho_cliente`:

    130 de 488  facturados en ≤ 5 min
    454 de 488  (93 %)   en ≤ 1 h
    484 de 488  (99,2 %) en ≤ 4 h
      4 de 488  pasaron de 4 h — el más lento, 7 h 33 min

O sea que a media tarde un despacho sin factura ya es raro. La dueña eligió
**las 17:00 de Ecuador**: *"hacelo 5pm, así les das tiempo de cargar"* — una
hora antes del cierre de ventas de las 18, para que quede margen de arreglarlo
el mismo día y para no encimarse con ese aviso.

**Los anulados NO cuentan, y esto costó un error.** La primera lectura contó 32
despachos "sin factura" desde el 01/08 y los reportó como 5.434 kg fuera del
balance. Al cruzarlos, 28 estaban ANULADOS (`fecha_anulacion` cargada, estado 0,
varios con "Despacho anulado Pedido: PDCL-…" en la glosa): nunca salieron de la
fábrica, así que no les corresponde factura y la mercadería volvió a bodega.
Mirar el flag de facturación sin mirar si el despacho sigue vivo convierte la
operación normal de un depósito en una alarma. Por eso `fecha_anulacion IS NULL`
va en la query y no en un filtro de Python: es parte de la definición.

**Un aviso por día, no uno por despacho.** A esa hora lo que importa es la
lista completa —"¿qué quedó sin facturar hoy?"— y no un aviso por cada uno.
La clave `desp-sin-factura:<fecha>` lo hace idempotente aunque el ciclo pase
cien veces entre las 17 y la medianoche.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger("programa_core.asinfo.despacho_sin_factura")

#: Hora de Ecuador a la que se pregunta. Dueña 2026-08-11: 17:00, una hora
#: antes del cierre de ventas, "así les das tiempo de cargar".
HORA_AVISO = 17

#: Bodega de producto terminado: es lo que sale a cliente.
BODEGA_PT = 53

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


def sin_factura_de_hoy() -> list[dict]:
    """Despachos de HOY, vivos, sin factura. Fail-soft: [] si Asinfo no habla.

    `fecha_anulacion IS NULL` es parte de la definición: un despacho anulado
    volvió a bodega y no le corresponde factura (ver el docstring del módulo).
    """
    from modules._lib import metabase_client

    sql = """
        SELECT dc.numero                                        AS numero,
               CONVERT(varchar(16), dc.fecha_creacion, 120)     AS creado,
               DATEDIFF(minute, dc.fecha_creacion, GETDATE())   AS minutos,
               LTRIM(RTRIM(ISNULL(dc.descripcion, '')))         AS nota,
               (SELECT ROUND(SUM(ISNULL(d.cantidad, 0)), 2)
                  FROM detalle_despacho_cliente d
                 WHERE d.id_despacho_cliente = dc.id_despacho_cliente) AS kg
          FROM despacho_cliente dc
         WHERE dc.fecha_creacion >= CAST(GETDATE() AS date)
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
            "kg": round(kg, 2),
            "nota": " ".join(str(r.get("nota") or "").split()),
            "creado": str(r.get("creado") or "").strip()[-5:],
            "horas": round(float(r.get("minutos") or 0) / 60.0, 1),
        })
    return out


def _titulo(casos: list[dict]) -> str:
    from filters import num_es
    kg = sum(c["kg"] for c in casos)
    cuantos = ("Un despacho salió hoy sin factura" if len(casos) == 1
               else f"{len(casos)} despachos salieron hoy sin factura")
    return f"{cuantos} · {num_es(kg, 0)} kg"


def _detalle(casos: list[dict]) -> str:
    """La lista pelada. El título ya dice cuántos y cuántos kilos son, y en un
    buzón lo que no se lee de un vistazo no se lee (dueña 2026-08-11: *"está
    muy largo el mensaje"*)."""
    from filters import num_es

    lineas = []
    for c in casos:
        nota = f" · {c['nota']}" if c["nota"] else ""
        lineas.append(f"{c['creado']} · {c['numero']} · "
                      f"{num_es(c['kg'], 1)} kg{nota}")
    return "\n".join(lineas)


def correr_si_toca() -> dict:
    """A partir de las 17:00 EC, UN aviso con lo que quedó sin facturar."""
    res = {"avisado": False, "casos": 0, "motivo": ""}
    if os.environ.get("DESPACHO_SIN_FACTURA", "1").strip() == "0":
        res["motivo"] = "apagado"
        return res
    with _lock:
        ahora = _ahora_ec()
        if ahora.hour < _hora():
            res["motivo"] = f"todavía no son las {_hora()}"
            return res
        try:
            casos = sin_factura_de_hoy()
        except Exception as e:  # noqa: BLE001
            _LOG.warning("revisión falló: %s", e)
            res["motivo"] = str(e)[:200]
            return res
        res["casos"] = len(casos)
        if not casos:
            # Un día en que se facturó todo no tiene por qué encender nada.
            res["motivo"] = "se facturó todo"
            return res

        from modules.avisos import queries as avisos
        res["avisado"] = avisos.avisar(
            fuente="ventas",
            nivel="alerta",
            titulo=_titulo(casos)[:200],
            detalle=_detalle(casos),
            cantidad=len(casos),
            url="/informes/dia",
            clave=f"desp-sin-factura:{ahora.date().isoformat()}",
        )
    if res["avisado"]:
        _LOG.info("despachos sin factura: %s caso(s)", len(casos))
    return res
