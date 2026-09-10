"""Lo que el cliente ve desde "Perfil": sus datos y el pedido de
corrección. Rediseño 04/09/2026; el 09/09 la dueña sacó "cómo pagar",
"avisar un pago", "actividad" y "facturas pagadas" ("o no sirven o la info
está en otros lados"), y el 10/09 "pedidos" y "su año en kilos".

Regla de la casa: el portal no calcula plata. Lo que suma sale de
`informes.queries`; acá se arma la pantalla y se guarda el AVISO de
corrección de datos (que nunca escribe en la ficha: lo atiende la oficina).
"""
from __future__ import annotations

import logging

from . import presentacion

_LOG = logging.getLogger("programa_core.portal")



# ---------------------------------------------------------------------------
# Pedir que corrijan sus datos
# ---------------------------------------------------------------------------

CLAVE_MENSAJES_A = "portal_mensajes_a"


def mensajes_a() -> list[str]:
    """A quién le llegan por mail los mensajes del portal (mig 0248; se
    edita en /portal-aviso). Lista separada por comas; vacía = sólo campanita."""
    import db
    try:
        r = db.fetch_one("SELECT valor FROM scintela.nota_config WHERE clave = %s",
                         (CLAVE_MENSAJES_A,))
    except Exception as e:  # noqa: BLE001 -- sin la fila, sin mail
        _LOG.warning("portal: no pude leer a quién van los mensajes (%s)", e)
        return []
    return [m.strip() for m in ((r or {}).get("valor") or "").split(",") if m.strip()]


def guardar_mensajes_a(texto: str) -> None:
    import db
    limpio = ", ".join(m.strip() for m in (texto or "").split(",") if m.strip())
    db.execute(
        "INSERT INTO scintela.nota_config (clave, valor) VALUES (%s, %s) "
        "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
        (CLAVE_MENSAJES_A, limpio[:500]))


def mensaje_del_cliente(cod: str, nombre: str, vend: str, texto: str, correo: str = "") -> bool:
    """Lo que el cliente escribe en "Perfil" (texto libre: "la dirección
    cambió", "no me gusta el botón"…). Va a la campanita/Novedades de la
    oficina y, si hay lista, por mail (dueña 10/09: "quiero que me llegue a mí
    también"). NO toca la ficha."""
    from modules.avisos import queries as avisos

    texto = (texto or "").strip()[:600]
    if not texto:
        return False
    quien = presentacion.nombre_lindo(nombre) or cod
    ok = avisos.avisar(
        fuente="portal", nivel="ok",
        titulo=f"{cod} escribió desde el portal",
        detalle=f"{quien} (vendedor {vend or '—'}) escribió desde el portal:\n{texto}",
        url=f"/clientes/{cod}/editar")
    destinatarios = mensajes_a()
    if destinatarios:
        try:
            from modules._lib import mailer
            mailer.enviar(
                f"Portal · {cod} escribió: {texto[:60]}",
                f"{quien} ({cod}, vendedor {vend or '—'}) escribió desde el portal:\n\n{texto}\n\n"
                f"Ficha: https://programa.intela.com.ec/clientes/{cod}/editar",
                destinatarios, responder_a=(correo or "").strip())
        except Exception as e:  # noqa: BLE001 -- el mail nunca tumba la pantalla
            _LOG.warning("portal: no salió el mail del mensaje de %s (%s)", cod, e)
    return bool(ok)

