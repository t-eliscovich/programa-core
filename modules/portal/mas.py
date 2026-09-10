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

def pedir_correccion(cod: str, nombre: str, vend: str, texto: str) -> bool:
    """Un aviso a la campanita de la oficina (y a la del vendedor, que es la
    misma campanita), con lo que el cliente escribió. NO toca la ficha."""
    from modules.avisos import queries as avisos

    texto = (texto or "").strip()[:600]
    if not texto:
        return False
    return avisos.avisar(
        fuente="portal", nivel="ok",
        titulo=f"{cod} pide corregir sus datos",
        detalle=f"{presentacion.nombre_lindo(nombre)} (vendedor {vend or '—'}) escribió desde el portal:\n{texto}",
        url=f"/clientes/{cod}/editar")
