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


# ═══════════════════════════════════════════════════════════════════════════
# LA MATRIZ DEL dBASE, Y EN QUÉ NOS APARTAMOS DE ELLA
# ═══════════════════════════════════════════════════════════════════════════
# `MODIFICA.PRG` no guarda una tabla de transiciones: guarda seis filtros que
# RECHAZAN el cambio. Si alguno da verdadero, el dBase muestra el error y no
# graba (`IF &FIL1 .OR. … .OR. &FIL6 → DO ERROR`).
#
#     FIL1 = .NOT. STF $ CART+ENBANC+'9XC'      destino inexistente
#     FIL2 = STI $ '123'  .AND. .NOT. STF $ STI+'XVJCP'
#     FIL3 = STI $ ENBANC .AND. .NOT. STF $ STI+'12PD'
#     FIL4 = STI = '9'    .AND. .NOT. STF $ '93'
#     FIL5 = STI = 'C'    .AND. STF # 'C'
#     FIL6 = STI $ 'ZPD'  .AND. STF $ '123'
#
# (En FoxPro `$` es SUBSTRING: `STF $ "12"` = "STF está contenido en '12'".)


def dbase_permite(sti: str, stf: str) -> bool:
    """¿El dBase dejaba pasar de `sti` a `stf`? Los seis filtros, literales."""
    sti = (sti or "").upper().strip()
    stf = (stf or "").upper().strip()
    fil1 = stf not in (DBASE_CART + DBASE_ENBANC + DBASE_OTROS)
    fil2 = (sti in "123") and (stf not in sti + "XVJCP")
    fil3 = (sti in DBASE_ENBANC) and (stf not in sti + "12PD")
    fil4 = (sti == "9") and (stf not in "93")
    fil5 = (sti == "C") and (stf != "C")
    fil6 = (sti in "ZPD") and (stf in "123")
    return not (fil1 or fil2 or fil3 or fil4 or fil5 or fil6)


#: Cómo se lee el primer valor de cada diferencia:
#:   "decidido"  → así queda, con el motivo al lado
#:   "a_decidir" → la dueña todavía no lo resolvió; el test la deja pasar pero
#:                 la lista sale por pantalla en `/admin/health` y acá
DIFERENCIAS_TRANSICIONES: dict[tuple[str, str], tuple[str, str]] = {
    # ── Programa Core PERMITE y el dBase no ────────────────────────────────
    ("Z", "1"): ("decidido", "dueña 2026-05-20 'no veo 1 y 2 en el dropdown': "
                             "marcar devuelto sin pasar por depósito+reverso. FIL6 lo prohibía."),
    ("P", "1"): ("decidido", "idem Z→1 (dueña 2026-05-20)."),
    ("D", "1"): ("decidido", "idem Z→1 (dueña 2026-05-20)."),
    ("1", "2"): ("decidido", "la secuencia de devueltos 1→2→3 es de Programa Core; "
                             "el dBase la manejaba a mano."),
    ("2", "3"): ("decidido", "idem 1→2."),
    ("1", "Z"): ("decidido", "volver a cartera un devuelto que el cliente regularizó."),
    ("2", "Z"): ("decidido", "idem 1→Z."),
    ("3", "Z"): ("decidido", "idem 1→Z."),
    ("1", "D"): ("decidido", "pasarle el devuelto a Daniela para gestión."),
    ("2", "D"): ("decidido", "idem 1→D."),
    ("3", "D"): ("decidido", "idem 1→D."),
    ("1", "9"): ("decidido", "el asistente de rebote también corre sobre un devuelto "
                             "(2° rebote): emite la ND y marca al cliente."),
    ("2", "9"): ("decidido", "idem 1→9."),
    ("B", "9"): ("decidido", "el asistente de rebote: el dBase pedía cargar la ND a mano "
                             "en BANCOS.PRG; acá la emite el mismo paso."),
    ("I", "9"): ("decidido", "idem B→9."),
    ("B", "X"): ("decidido", "anular por error de carga compensa el depósito en el banco "
                             "(`anular_por_error_de_carga`); el dBase no tenía ese camino."),
    ("I", "X"): ("decidido", "idem B→X."),

    # ── El dBase PERMITE y Programa Core no ────────────────────────────────
    # a) Salir de un depositado por un cambio de etiqueta pelado dejaría el
    #    depósito colgado en el banco. Es la regla explícita de PC: de un
    #    depositado sólo se sale por rebote (9), anulación (X) o devuelto (1),
    #    que son los tres que TOCAN el banco.
    **{(a, b): ("decidido", "salir de un depositado sin tocar el banco dejaría el "
                            "depósito colgado; PC sale por 9/X/1, que compensan.")
       for a in "BIV" for b in "2DP"},
    # b) Restaurar un eliminado a cualquier cosa. PC lo deja volver sólo a
    #    cartera (Z/P/D), donde no hay plata que reconstruir.
    **{("X", b): ("decidido", "un eliminado vuelve sólo a cartera: revivirlo directo a "
                              "depositado o a caja tendría que rehacer movimientos de plata.")
       for b in "123 9BCIV".replace(" ", "")},
    ("9", "3"): ("decidido", "en PC el 9 no es un estado donde el cheque se queda: el "
                             "asistente ya lo deja en 1 o en 3 según de dónde venía."),
    ("D", "9"): ("decidido", "un cheque en cartera nunca fue al banco: no puede rebotar. "
                             "Su reverso es administrativo (→X)."),
    ("P", "9"): ("decidido", "idem D→9."),

    # (Las 8 que quedaban "a_decidir" las aprobó la dueña el 11/08/2026, así que
    # dejaron de ser diferencias: cobrar en efectivo un devuelto (1/2/3→C),
    # re-depositar un devuelto de 2ª o 3ª (2/3→V) y depositar marcando V desde
    # cartera (Z/P/D→V). Las tres eran del dBase.)
}


def diferencias_con_el_dbase(transiciones_pc: dict[str, set[str]]) -> dict:
    """Compara la matriz de PC contra la del dBase, sobre el alfabeto común.

    Devuelve {"de_mas": [...], "de_menos": [...], "sin_declarar": [...]}.
    El alfabeto común excluye los estados que sólo tiene uno de los dos: esos
    ya están explicados en NO_USADOS / AGREGADOS.
    """
    dbase_alfabeto = set(DBASE_CART) | set(DBASE_ENBANC) | set(DBASE_OTROS)
    comun = sorted(set(ESTADOS) & dbase_alfabeto)
    de_mas, de_menos = [], []
    for a in comun:
        pc = set(transiciones_pc.get(a, ())) & set(comun)
        db = {b for b in comun if b != a and dbase_permite(a, b)}
        de_mas += [(a, b) for b in sorted(pc - db)]
        de_menos += [(a, b) for b in sorted(db - pc)]
    todas = de_mas + de_menos
    return {
        "de_mas": de_mas,
        "de_menos": de_menos,
        "sin_declarar": [t for t in todas if t not in DIFERENCIAS_TRANSICIONES],
        "a_decidir": [t for t in todas
                      if DIFERENCIAS_TRANSICIONES.get(t, ("", ""))[0] == "a_decidir"],
    }
