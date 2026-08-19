"""Grupos de clientes: quien es hijo de quien, y como se edita.

TMT 2026-08-19 (duena, con las fotos del cuaderno de la oficina): *"Estos
clientes son de un mismo grupo. en clientes una columna mas que diga grupo y
ponemos el primero codigo que aparece en lista. Esto tiene que ser editable.
(...) Usar tambien para impresion por grupos, estos clientes juntos"*.

La tabla `scintela.grupo_cliente` ya existia y ya la leian la cartera por grupo
y el estado de cuenta agrupado; lo que faltaba era **como cargarla**: hasta hoy
se cargaba por SQL a mano, que es exactamente lo que la regla `operar-por-la-ui`
prohibe. Este modulo es la mitad pura + los cuatro accesos a base.

## La forma del dato (leerla antes de tocar nada)

Solo los HIJOS tienen fila: `(codigo_padre, codigo_hijo)`. El padre NO tiene
fila propia. Entonces:

  * cliente sin fila           -> sin grupo (o es padre, si alguien lo apunta)
  * cliente con fila como hijo -> pertenece al grupo `codigo_padre`
  * "el grupo de X"            -> COALESCE(padre_de(X), X)

Ese `COALESCE` es el contrato que ya usan `cartera/queries.py::aging_por_grupo`
e `informes/queries.py::estado_cuenta_clientes_saldos`. Se respeta tal cual.

## Por que se aplana (`_raiz`)

Nada impide escribir A->B y despues B->C: quedaria una cadena y "el grupo de A"
dependeria de cuantos saltos diera cada consulta. Las dos consultas de arriba
dan UN salto (`LEFT JOIN ... ON g.codigo_hijo = c.codigo_cli`), asi que una
cadena de dos les rompe el total: A imprime en el grupo B y B imprime en el
grupo C, con A y B separados aunque sean la misma casa.

Por eso al guardar se resuelve la RAIZ y se guarda contra ella, y si el cliente
que estas moviendo tenia hijos, los hijos se mudan con el. La tabla queda
siempre de dos niveles: un padre y sus hijos, sin nietos.
"""

from __future__ import annotations

import db


def _norm(cod) -> str:
    return str(cod or "").strip().upper()


def _raiz(codigo: str, padres: dict[str, str]) -> str:
    """Sube por la cadena padre->padre hasta la raiz, con freno de ciclo.

    `padres` es el mapa hijo->padre completo. El freno importa: la base vieja
    se cargo a mano y nada impedia escribir A->B y B->A, que sin el `visto`
    seria un while infinito adentro de un request.
    """
    visto = {codigo}
    actual = codigo
    while True:
        siguiente = padres.get(actual)
        if not siguiente or siguiente in visto:
            return actual
        visto.add(siguiente)
        actual = siguiente


def mapa_padres() -> dict[str, str]:
    """hijo -> padre, toda la tabla de una. Para no hacer N+1 en la lista."""
    filas = db.fetch_all(
        "SELECT codigo_hijo, codigo_padre FROM scintela.grupo_cliente"
    ) or []
    return {_norm(f["codigo_hijo"]): _norm(f["codigo_padre"]) for f in filas}


def grupo_de(codigo_cli: str) -> str | None:
    """Codigo del grupo de un cliente, o None si no esta en ninguno.

    Devuelve el propio codigo cuando el cliente es el PADRE de un grupo: para
    la pantalla, "ECH" y sus hijos muestran todos "ECH" en la columna Grupo, y
    el que no esta en ningun grupo muestra vacio.
    """
    cod = _norm(codigo_cli)
    if not cod:
        return None
    fila = db.fetch_one(
        "SELECT codigo_padre FROM scintela.grupo_cliente WHERE UPPER(TRIM(codigo_hijo)) = %s",
        (cod,),
    )
    if fila:
        return _norm(fila["codigo_padre"])
    hijo = db.fetch_one(
        "SELECT 1 AS x FROM scintela.grupo_cliente "
        " WHERE UPPER(TRIM(codigo_padre)) = %s LIMIT 1",
        (cod,),
    )
    return cod if hijo else None


def integrantes(codigo_grupo: str) -> list[str]:
    """Todos los codigos del grupo, el padre incluido, en orden alfabetico.

    Orden alfabetico por CODIGO, el mismo criterio que ya usan la lista de
    clientes del portal y las hojas impresas (ver skill portal-vendedores).
    """
    grp = _norm(codigo_grupo)
    if not grp:
        return []
    filas = db.fetch_all(
        "SELECT codigo_hijo FROM scintela.grupo_cliente "
        " WHERE UPPER(TRIM(codigo_padre)) = %s",
        (grp,),
    ) or []
    return sorted({grp} | {_norm(f["codigo_hijo"]) for f in filas})


def hermanos_de(codigo_cli: str) -> list[str]:
    """Los OTROS codigos del grupo del cliente (sin el mismo). [] si no tiene."""
    cod = _norm(codigo_cli)
    grp = grupo_de(cod)
    if not grp:
        return []
    return [c for c in integrantes(grp) if c != cod]


def existe_cliente(codigo_cli: str) -> bool:
    cod = _norm(codigo_cli)
    if not cod:
        return False
    return bool(db.fetch_one(
        "SELECT 1 AS x FROM scintela.cliente WHERE UPPER(TRIM(codigo_cli)) = %s LIMIT 1",
        (cod,),
    ))


def asignar(codigo_cli: str, codigo_padre: str, usuario: str = "web") -> tuple[bool, str]:
    """Mete a `codigo_cli` en el grupo de `codigo_padre`. (ok, mensaje).

    Valida ANTES de escribir: los dos codigos existen, no son el mismo, y el
    padre se resuelve a su raiz para que la tabla no crie nietos (ver docstring
    del modulo). Si el cliente ya era padre de otros, sus hijos se mudan con el
    -- si no, al mudarse el padre los hijos quedarian colgando de un codigo que
    ya no es cabeza de nada, y "el grupo de" de cada uno daria distinto.
    """
    hijo = _norm(codigo_cli)
    padre_pedido = _norm(codigo_padre)
    if not hijo:
        return False, "Falta el codigo del cliente."
    if not padre_pedido:
        return False, "Falta el codigo del grupo."
    if not existe_cliente(hijo):
        return False, f"El cliente {hijo} no existe."
    if not existe_cliente(padre_pedido):
        return False, f"El codigo de grupo {padre_pedido} no es un cliente."

    padres = mapa_padres()
    padre = _raiz(padre_pedido, padres)
    if padre == hijo:
        # Pasa de verdad: si B ya es hijo de A y pedis "A al grupo B", la raiz
        # de B es A. Meter a A adentro de A no es un error de tipeo del que
        # avisar y seguir: es la senal de que el grupo ya existe al reves.
        return False, (
            f"{hijo} ya es la cabeza del grupo de {padre_pedido} — "
            f"no puede ser hijo de su propio hijo."
        )

    nota = ""
    if padre != padre_pedido:
        nota = f" (el grupo de {padre_pedido} es {padre})"

    # Los hijos del que se muda se mudan con el.
    db.execute(
        "UPDATE scintela.grupo_cliente "
        "   SET codigo_padre = %s, fecha_modifica = now(), usuario_modifica = %s "
        " WHERE UPPER(TRIM(codigo_padre)) = %s",
        (padre, usuario, hijo),
    )
    db.execute(
        """
        INSERT INTO scintela.grupo_cliente
               (codigo_padre, codigo_hijo, fecha_crea, usuario_crea)
        VALUES (%s, %s, now(), %s)
        ON CONFLICT (codigo_hijo) DO UPDATE
           SET codigo_padre = EXCLUDED.codigo_padre,
               fecha_modifica = now(),
               usuario_modifica = EXCLUDED.usuario_crea
        """,
        (padre, hijo, usuario),
    )
    return True, f"{hijo} quedo en el grupo {padre}{nota}."


def quitar(codigo_cli: str, usuario: str = "web") -> tuple[bool, str]:
    """Saca al cliente de su grupo.

    Si era PADRE, se disuelve el grupo entero: sus hijos quedan sueltos. Es
    deliberado -- ascender a uno de los hijos a cabeza seria elegir por la
    duena cual de las empresas manda, y ese es justo el dato que ella tiene en
    el cuaderno y nosotros no.
    """
    cod = _norm(codigo_cli)
    if not cod:
        return False, "Falta el codigo del cliente."
    n_hijos = db.execute(
        "DELETE FROM scintela.grupo_cliente WHERE UPPER(TRIM(codigo_padre)) = %s",
        (cod,),
    )
    n_self = db.execute(
        "DELETE FROM scintela.grupo_cliente WHERE UPPER(TRIM(codigo_hijo)) = %s",
        (cod,),
    )
    if n_hijos:
        return True, f"Se disolvio el grupo {cod}: {n_hijos} cliente(s) quedaron sin grupo."
    if n_self:
        return True, f"{cod} quedo sin grupo."
    return True, f"{cod} ya no estaba en ningun grupo."
