"""Los estados de un cheque, definidos UNA sola vez.

La estructura y el significado los pone el dBase (`MODIFICA.PRG`), que parte
los estados en dos familias y sobre ellas escribe todas sus reglas:

    CART   = 'Z123PD'   el cheque SIGUE SIENDO NUESTRO (cartera)
    ENBANC = 'BVWIJK'   SALIÓ de cartera: está en el banco

Todo lo demás del módulo se deriva de la tabla de acá abajo: las familias, las
etiquetas de los menús, y los tests que cruzan una definición contra otra.
Antes cada una de esas cosas se escribía por separado y se atrasaban entre
ellas — el 11/08/2026 eso costó tres bugs en un día (dos "→1" idénticos en el
menú, un cheque de Internacional que se eliminaba al rebotar, y la pantalla de
confirmación diciendo "no afecta al cliente" cuando sí lo afectaba).

⭐ **No se agregan estados muertos.** `W`, `J` y `K` son códigos de bancos
viejos del DBF: cero cheques en producción (censo del 11/08/2026 sobre 3.753
cheques) y ninguna pantalla que los produzca. Estaban en la familia sólo porque
el dBase los tenía. Decisión de la dueña: se sacan. Si algún día aparece uno,
el vigía de `/admin/health` lo va a marcar como estado desconocido.
"""
from __future__ import annotations

from typing import NamedTuple

#: Las dos familias del dBase, tal cual, para poder compararnos contra ellas.
DBASE_CART = "Z123PD"
DBASE_ENBANC = "BVWIJK"

#: Fuera de las dos familias, el dBase acepta tres estados más (FIL1:
#: `STF $ CART+ENBANC+'9XC'`): rebotado, eliminado y cobrado en caja.
DBASE_OTROS = "9XC"

#: ⭐ TODA diferencia contra el dBase se declara acá, con su motivo. El test la
#: exige: así una divergencia es una decisión escrita y no un olvido.
NO_USADOS = {
    "W": "código de banco viejo del DBF — 0 cheques (censo 11/08/2026)",
    "J": "código de banco viejo del DBF — 0 cheques (censo 11/08/2026)",
    "K": "código de banco viejo del DBF — 0 cheques (censo 11/08/2026)",
}

#: Estados que Programa Core tiene y el dBase no.
AGREGADOS = {
    "A": "acreditado: lo trajo el import del dBase. 0 cheques hoy, pero "
         "/informes/estado-cuenta lo trata junto con B como 'no por cobrar'.",
    "E": "endosado a proveedor — flujo propio de Programa Core (endoso_prov).",
    "R": "reversado legacy: sólo en filas viejas importadas.",
    "T": "cobrado total legacy: sólo en filas viejas importadas.",
}


class Estado(NamedTuple):
    """Una letra, qué significa, y de qué lado está."""

    letra: str
    nombre: str      #: cómo se llama en los menús
    que_es: str      #: qué significa, en una línea
    familia: str     #: cartera · banco · caja · terminal


#: ⭐ LA TABLA. Todo lo demás sale de acá.
ESTADOS: dict[str, Estado] = {
    e.letra: e for e in [
        # ── Cartera (CART del dBase): el cheque todavía es nuestro ──────────
        Estado("Z", "Cartera",      "Ingresado, esperando su fecha para depositar", "cartera"),
        Estado("P", "Postergar",    "Postergado: se acordó cobrarlo más adelante",  "cartera"),
        Estado("D", "Daniela",      "En gestión de cobranza de Daniela",            "cartera"),
        Estado("1", "Devuelto",     "El banco lo devolvió — sigue siendo deuda del cliente", "cartera"),
        Estado("2", "Devuelto 2°",  "Devuelto por segunda vez",                     "cartera"),
        Estado("3", "Rechazo 3°",   "Devuelto por tercera vez — no se intenta más", "cartera"),
        # ── En banco (ENBANC del dBase): salió de cartera ───────────────────
        Estado("B", "Depositar",    "Depositado en Pichincha",                      "banco"),
        Estado("I", "Depositar",    "Depositado en el Internacional",               "banco"),
        Estado("V", "Re-depositar", "Un devuelto que se volvió a depositar",        "banco"),
        Estado("A", "Acreditado",   "Acreditado (dato viejo del dBase)",            "banco"),
        # ── Fuera de las dos familias del dBase ─────────────────────────────
        Estado("C", "Caja",         "Cobrado en efectivo — entró a caja",           "caja"),
        Estado("9", "Sin fondos",   "Rebotó: el banco lo rechazó",                  "terminal"),
        Estado("E", "Endosar",      "Endosado a un proveedor",                      "terminal"),
        Estado("X", "Anular",       "Eliminado — no cuenta para nada",              "terminal"),
        Estado("R", "Reversado",    "Reversado (dato viejo del dBase)",             "terminal"),
        Estado("T", "Cobrado",      "Cobrado por completo (dato viejo del dBase)",  "terminal"),
    ]
}


def _familia(nombre: str) -> tuple[str, ...]:
    return tuple(e.letra for e in ESTADOS.values() if e.familia == nombre)


#: El cheque sigue siendo nuestro. Equivale a CART del dBase.
EN_CARTERA = _familia("cartera")
#: Salió de cartera y está en el banco. Equivale a ENBANC, menos los muertos.
EN_BANCO = _familia("banco")
#: Ni cartera ni banco: la plata entró por caja.
EN_CAJA = _familia("caja")

#: A qué estados de banco se puede LLEGAR hoy depositando. Es `EN_BANCO` menos
#: `A`, que sólo existe en filas viejas del dBase y no es destino de ninguna
#: pantalla. Estaba tipeado como `("B","V","I")` en tres lugares distintos.
DESTINOS_DEPOSITO = tuple(x for x in EN_BANCO if x not in AGREGADOS)

#: Cómo se llama cada estado en los menús de "cambiar estado".
LABEL_CORTO_ESTADO = {e.letra: e.nombre for e in ESTADOS.values()}

#: Qué significa cada estado, para tooltips y pantallas de ayuda.
QUE_ES_ESTADO = {e.letra: e.que_es for e in ESTADOS.values()}


def que_es(letra: str) -> str:
    """Una línea explicando el estado. Para no escribirla en cada template."""
    e = ESTADOS.get((letra or "").upper().strip())
    return e.que_es if e else "Estado desconocido"
