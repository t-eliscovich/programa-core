"""Lo que el VENDEDOR ve y puede hacer sobre el acceso de su cliente al portal.

TMT 2026-08-24, `PLAN_PORTAL_CLIENTE_2026_08_24.md`. El portal del cliente
entra con el código de 3 letras y el RUC, que son los dos datos públicos —el
RUC está impreso en cada factura y se consulta gratis en el SRI—. Se eligió a
propósito no ponerle un chequeo previo, porque le cargaría fricción al 100% de
los clientes para atajar un caso raro.

⭐ **El control es éste: el vendedor lo ve y le corta el acceso.** Es la mitad
que hace que la decisión de arriba sea defendible, así que va en la v1 y no
después. Y va donde el vendedor ya está parado —la ficha de su cliente— y no
en una pantalla nueva que nadie abre.

El vendedor conoce a sus clientes uno por uno: es quien de verdad puede darse
cuenta de que algo está raro. Una pantalla no.

⚠ Todo fail-soft: si las tablas del portal todavía no existen (el deploy corre
las migraciones, pero hay una ventana), la ficha del vendedor tiene que seguir
abriendo igual. Un bloque de más no puede tumbar la pantalla con la que el
vendedor trabaja todo el día.
"""
from __future__ import annotations

import logging

import db

_LOG = logging.getLogger("programa_core.mi_cartera.portal")

#: Cuántos ingresos se le muestran. Es un vistazo, no una auditoría: la
#: bitácora completa vive en `scintela.portal_ingreso`.
ULTIMOS = 5


def estado(codigo_cli: str) -> dict:
    """Cómo está el acceso de este cliente al portal.

    ``{"tiene": bool, "activo": bool, "eligio_clave": bool, "primer_ingreso": …,
       "ultimo_ingreso": …, "mail": str, "mail_cambiado": bool,
       "cortado_por": str, "ingresos": [...]}``

    `tiene` es False cuando el cliente nunca entró: ahí la ficha no muestra
    nada. Un bloque que dice "todavía no entró" en 400 fichas es ruido.
    """
    vacio = {"tiene": False, "activo": False, "eligio_clave": False,
             "primer_ingreso": None, "ultimo_ingreso": None, "mail": "",
             "mail_cambiado": False, "cortado_por": "", "ingresos": []}
    cod = (codigo_cli or "").strip().upper()
    if not cod:
        return vacio
    try:
        fila = db.fetch_one(
            "SELECT activo, clave_hash IS NOT NULL AS eligio_clave, mail, "
            "       mail_cambiado, cortado_por, primer_ingreso_en, "
            "       ultimo_ingreso_en "
            "  FROM scintela.portal_acceso "
            " WHERE UPPER(TRIM(codigo_cli)) = %s", (cod,))
        if not fila:
            return vacio
        ingresos = db.fetch_all(
            "SELECT resultado, con_que, ip, creado_en "
            "  FROM scintela.portal_ingreso "
            " WHERE UPPER(TRIM(codigo_cli)) = %s "
            " ORDER BY id_portal_ingreso DESC LIMIT %s", (cod, ULTIMOS)) or []
    except Exception as e:  # noqa: BLE001 -- la ficha del vendedor no se cae
        _LOG.warning("mi-cartera: no pude leer el portal de %s (%s)", cod, e)
        return vacio

    return {
        "tiene": True,
        "activo": bool(fila.get("activo")),
        "eligio_clave": bool(fila.get("eligio_clave")),
        "primer_ingreso": fila.get("primer_ingreso_en"),
        "ultimo_ingreso": fila.get("ultimo_ingreso_en"),
        "mail": (fila.get("mail") or "").strip(),
        "mail_cambiado": bool(fila.get("mail_cambiado")),
        "cortado_por": (fila.get("cortado_por") or "").strip(),
        "ingresos": [dict(r) for r in ingresos],
    }


def cortar(codigo_cli: str, quien: str) -> tuple[bool, str]:
    """El vendedor le cierra el acceso al cliente.

    NO borra la fila: queda el rastro de que existió y de quién lo cortó
    —reversar no es eliminar—, y así el cliente que llame puede volver a
    entrar sin perder su historia.
    """
    cod = (codigo_cli or "").strip().upper()
    if not cod:
        return False, "Falta el cliente."
    try:
        db.execute(
            "UPDATE scintela.portal_acceso "
            "   SET activo = false, cortado_por = %s, cortado_en = now() "
            " WHERE UPPER(TRIM(codigo_cli)) = %s",
            ((quien or "")[:60], cod))
        return True, "Le cerramos el acceso al portal."
    except Exception as e:  # noqa: BLE001
        _LOG.warning("mi-cartera: no pude cortar el portal de %s (%s)", cod, e)
        return False, "No se pudo. Probá de nuevo."


def reabrir(codigo_cli: str, quien: str) -> tuple[bool, str]:
    """Y lo vuelve a abrir — cortar sin poder deshacer es una trampa.

    Se le limpian también los intentos fallidos: el que llama para que le
    reabran no tiene que quedar trabado además.
    """
    cod = (codigo_cli or "").strip().upper()
    if not cod:
        return False, "Falta el cliente."
    try:
        db.execute(
            "UPDATE scintela.portal_acceso "
            "   SET activo = true, cortado_por = NULL, cortado_en = NULL, "
            "       intentos_fallidos = 0, bloqueado_hasta = NULL "
            " WHERE UPPER(TRIM(codigo_cli)) = %s", (cod,))
        return True, "Le abrimos el acceso de nuevo."
    except Exception as e:  # noqa: BLE001
        _LOG.warning("mi-cartera: no pude reabrir el portal de %s (%s)", cod, e)
        return False, "No se pudo. Probá de nuevo."
