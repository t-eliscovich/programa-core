"""Las VISITAS de todo el mundo: qué pantalla abrió cada uno, y cuándo.

TMT 2026-08-26 (dueña): *"¿podríamos medir cuánto usa cada vendedor la
aplicación? ¿y qué movimientos hace?"*. TMT 2026-09-14: *"hace un buen
trabajo de medición"* — sumar a la oficina (Intela) como un tercer mundo,
al lado de vendedores y clientes.

La segunda mitad ya estaba: todo lo que un usuario CAMBIA queda en
`scintela.bitacora_acciones` desde la migración 0004. La primera no, y para un
vendedor es casi todo lo que hace — mirar su cartera, abrir la ficha de un
cliente, imprimir un estado de cuenta. Son GET, y la bitácora audita sólo
escrituras a propósito (`auth._should_audit`): la bitácora sigue siendo SÓLO
escrituras, esto es aparte.

Por eso las visitas van a una tabla propia (`scintela.uso_pantalla`, migración
0232) y este hook las escribe.

Qué se registra
---------------
Todo el que entró logueado — vendedor u oficina (Accionista, Administrador,
cobranza, contabilidad) — y, desde el 04/09/2026 (TMT: *"así vemos qué hacen
una vez que lancemos"*), los CLIENTES en el portal: la misma tabla, con
`usuario = 'portal:<código>'` para que no se mezclen jamás con un username de
la oficina, `vend` vacío y `codigo_cli` con el cliente.

Los tres mundos (vendedor / oficina / portal) nunca se leen juntos: las
consultas de vendedores exigen `vend` cargado, las de oficina lo exigen
vacío, y las del portal filtran por el prefijo — ver `queries.pantallas(...,
ambito=)`. Antes del 14/09/2026 la oficina no se medía (era sólo lo que se
había preguntado); ahora si hace falta volver a excluirla alcanza con
restaurar el chequeo de `vendedor_de()` en `hay_que_registrar()`.

Y sólo los GET que terminaron en 200 con un endpoint real: un 404 no es una
pantalla que alguien haya usado, y un redirect se cuenta en el destino.

⚠ Best-effort, como la bitácora: si el INSERT falla, el vendedor no se entera.
Medir el uso no puede tumbar la pantalla con la que trabaja todo el día.
"""
from __future__ import annotations

import logging

from flask import g, request

import db
from scope_vendedor import vendedor_de

_LOG = logging.getLogger("programa_core.uso")

#: Rutas que no son "una pantalla que alguien abrió".
RUTAS_QUE_NO_CUENTAN: tuple[str, ...] = (
    "/static",
    "/favicon",
    "/healthz",
    "/robots.txt",
)

#: Endpoint → cómo se llama la pantalla para una persona. Se resuelve al LEER
#: (la tabla guarda el endpoint), así que cambiarle el texto a una fila la
#: cambia también para todo lo ya registrado.
NOMBRES: dict[str, str] = {
    "mi_cartera.inicio": "Inicio",
    "mi_cartera.clientes": "Sus clientes",
    "mi_cartera.cliente": "Ficha de un cliente",
    "mi_cartera.despachos": "Despachos de un cliente",
    "mi_cartera.despacho": "Un despacho",
    "mi_cartera.factura": "Una factura",
    "mi_cartera.factura_hoja": "Factura para imprimir",
    "mi_cartera.factura_pdf": "Factura en PDF",
    "mi_cartera.factura_imagen": "Factura en foto",
    "mi_cartera.imprimir": "Estado de cuenta para imprimir",
    "mi_cartera.imprimir_todos": "Todos los estados de cuenta",
    "mi_cartera.pdf": "Estado de cuenta en PDF",
    "mi_cartera.imagen": "Estado de cuenta en foto",
    "mi_cartera.pedidos": "Pedidos de sus clientes",
    "mi_cartera.comision": "Su comisión",
    "mi_cartera.metas": "Metas de venta",
    "mi_cartera.prueba_envio": "Prueba de envío",
    "analisis.competencia": "Competencia",
    "analisis.mis_telas": "Telas paradas de sus clientes",
    "analisis.mis_telas_csv": "Telas paradas en Excel",
    "analisis.mis_telas_xlsx": "Telas paradas en Excel",
    "analisis.mi_hoja": "Su hoja de la competencia",
    "analisis.mi_hoja_csv": "Su hoja en Excel",
    "analisis.saldos_imprimir": "Saldos para imprimir",
    "analisis.saldos_imprimir_pdf": "Saldos en PDF",
    # El portal del cliente.
    "portal.inicio": "Entrada",
    "portal.estado_cuenta": "Su estado de cuenta",
    "portal.factura": "Una factura",
    "portal.factura_papel_cliente": "Factura para imprimir",
    "portal.mis_pagos": "Sus pagos",
    "portal.estado_cuenta_imprimir": "Estado de cuenta para imprimir",
    "portal.estado_cuenta_pdf_": "Estado de cuenta en PDF",
    "portal.mis_cuentas": "Elegir la cuenta",
}

#: Así empieza `usuario` cuando el que miró es un cliente en el portal.
PORTAL = "portal:"

#: Las pantallas que terminan en un papel: se imprimen, se bajan o se mandan
#: por WhatsApp. La dueña quiere el número aparte — es el trabajo de campo.
PAPELES: frozenset[str] = frozenset({
    "mi_cartera.imprimir",
    "mi_cartera.imprimir_todos",
    "mi_cartera.pdf",
    "mi_cartera.imagen",
    "mi_cartera.factura_hoja",
    "mi_cartera.factura_pdf",
    "mi_cartera.factura_imagen",
    "analisis.mis_telas_csv",
    "analisis.mis_telas_xlsx",
    "analisis.mi_hoja_csv",
    "analisis.saldos_imprimir",
    "analisis.saldos_imprimir_pdf",
    "portal.factura_papel_cliente",
    "portal.estado_cuenta_imprimir",
    "portal.estado_cuenta_pdf_",
})


def nombre_de(pantalla: str | None) -> str:
    """El nombre de la pantalla para mostrar. Si no lo conozco, el endpoint."""
    if not pantalla:
        return "—"
    return NOMBRES.get(pantalla, pantalla)


def es_papel(pantalla: str | None) -> bool:
    """¿Esa pantalla termina en algo que se imprime o se manda?"""
    return bool(pantalla) and pantalla in PAPELES


def dispositivo_de(user_agent: str | None) -> str:
    """'celular' o 'computadora', mirando el navegador.

    Alcanza con `Mobi`: lo mandan Chrome y Safari de Android y de iPhone. No
    vale la pena una librería para separar dos casos.
    """
    ua = (user_agent or "")
    return "celular" if ("Mobi" in ua or "Android" in ua) else "computadora"


def cliente_del_portal() -> str:
    """El cliente logueado en el portal, o vacío. En la oficina siempre vacío."""
    import modo
    if not modo.es_portal():
        return ""
    from modules.portal.views import cliente_actual
    return cliente_actual()


def hay_que_registrar(response) -> bool:
    """¿Esta respuesta es una pantalla que alguien logueado (vendedor u
    oficina) o un cliente en el portal abrió?

    Antes sólo contaban los vendedores (`vendedor_de(...)`); desde el
    14/09/2026 cuenta cualquiera con sesión — el chequeo de `vend` se movió a
    las consultas (`queries.resumen` exige `vend`, `queries.resumen_intela`
    exige que NO lo tenga), que es donde había que separarlos.
    """
    if request.method != "GET" or response.status_code != 200:
        return False
    if not request.endpoint:            # 404: no es una pantalla
        return False
    ruta = request.path or ""
    if any(ruta.startswith(p) for p in RUTAS_QUE_NO_CUENTAN):
        return False
    return bool(g.get("user") or cliente_del_portal())


def registrar_uso_after_request(response):
    """after_request: si un vendedor abrió una pantalla, queda anotada."""
    try:
        if not hay_que_registrar(response):
            return response
        cliente = cliente_del_portal()
        if cliente:
            usuario, vend, codigo_cli = PORTAL + cliente, "", cliente
        else:
            usuario = (g.get("user") or {}).get("username") or "anon"
            vend = vendedor_de(g.get("user"))
            vistas = request.view_args or {}
            codigo_cli = (vistas.get("codigo_cli") or "").strip().upper() or None
        db.execute(
            """
            INSERT INTO scintela.uso_pantalla
                (usuario, vend, ruta, pantalla, codigo_cli, dispositivo, ip)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                usuario[:40],
                vend[:10] or None,
                (request.path or "")[:200],
                (request.endpoint or "")[:60],
                codigo_cli and codigo_cli[:20],
                dispositivo_de(request.headers.get("User-Agent")),
                (request.remote_addr or "")[:45] or None,
            ),
        )
    except Exception:  # noqa: BLE001 — nunca romper el request por medir
        _LOG.debug("no pude registrar el uso de %s", request.path, exc_info=True)
    return response
