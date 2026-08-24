"""En qué MODO arranca este proceso: el ERP de siempre, o el portal del cliente.

TMT 2026-08-24, planificando el portal del cliente
(`PLAN_PORTAL_CLIENTE_2026_08_24.md`). El portal va a estar abierto a internet
y el ERP no. Las tres opciones que se miraron fueron: meterlo adentro con un
candado, hacer una app aparte, o esto — **el mismo código y la misma base, pero
un proceso que levanta sólo el pedazo del portal**.

Ganó esta porque:

- **Reusa sin duplicar.** El estado de cuenta sale de la misma función y la
  misma hoja que ven la oficina y los vendedores. Dos plantillas del mismo
  documento divergen a la primera corrección.
- **Un agujero no llega a la plata.** En el proceso del portal las pantallas de
  cobranza, posdatados y balance no existen. No dependemos de que el candado
  esté bien escrito: dependemos de que la ruta no exista, que es mucho más
  difícil de arruinar.
- **Deploy conocido.** Mismo repo, mismo CI, mismo servidor. Un servicio más.

Se prende con la variable de entorno `MODO=portal`. Cualquier otro valor —o
ninguno— es el ERP de siempre, que es el default a propósito: si alguien se
equivoca escribiendo la variable, el que arranca es el programa de la oficina,
que ya está protegido por login y permisos. Al revés sería peor.
"""
from __future__ import annotations

import os

#: El valor que prende el portal. Uno solo, en minúsculas, sin sinónimos: la
#: variable la escribe una persona una vez y tiene que ser fácil de verificar.
PORTAL = "portal"


def actual() -> str:
    """El modo, normalizado. Vacío si no está seteado."""
    return (os.environ.get("MODO") or "").strip().lower()


def es_portal() -> bool:
    """¿Este proceso es el portal del cliente?

    Fail-safe hacia el lado seguro: sólo el valor exacto `portal` lo prende.
    """
    return actual() == PORTAL


def nombre() -> str:
    """Cómo se llama este proceso, para los logs y el health."""
    return "portal del cliente" if es_portal() else "Programa Core"
