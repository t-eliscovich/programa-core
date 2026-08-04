"""Consultas de clientes."""
import re

import db


class _Vaciar:
    """Sentinela para `editar()`: *poné esta columna en NULL*.

    En `editar()` (y en el resto de los `queries.editar` del repo) `None`
    significa **"no toques esta columna"** — los callers parciales dependen
    de eso. Para los campos de TEXTO alcanza con el string vacío (`""` →
    NULL), pero para los numéricos (`cupo`) no hay string vacío posible, así
    que va este sentinela. Es falsy a propósito para que el código que
    normaliza (`val[:maxlen] if val else None`) lo trate como vacío.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - sólo debug
        return "VACIAR"

    def __bool__(self) -> bool:
        return False


#: Pasalo como valor de un campo de `editar()` para ponerlo en NULL.
VACIAR = _Vaciar()

#: Los códigos de cliente son de hasta 3 caracteres (ver `maxlength` del form
#: y el `CHAR(3)` del dBase). La convención de la fábrica es "las iniciales
#: del nombre" — de ahí salen los duplicados.
_CODIGO_RE = re.compile(r"^[A-Z0-9]{1,3}$")

#: Tablas cuyo `codigo_cli` apunta al **CÓDIGO** del cliente, no a
#: `id_cliente`. Es la razón por la que renombrar un código no es un UPDATE
#: de una fila: toda la plata cuelga del string de 3 letras.
_TABLAS_CON_CODIGO_CLI = (
    ("scintela.factura", "facturas"),
    ("scintela.cheque", "cheques"),
    ("scintela.chequesxfact", "aplicaciones de cheque a factura"),
    ("scintela.retencion", "retenciones"),
    ("scintela.cobro", "cobros"),
)


def directorio(q: str = "") -> list[dict]:
    """Lista de contactos: código, nombre, teléfono, email, stop, vendedor.

    Sólo clientes con teléfono o email cargado (los demás no son
    contactables, no aportan al directorio). Ordenado por nombre alfabético.
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT codigo_cli, nombre,
               COALESCE(telefono, '')  AS telefono,
               COALESCE(correo, '')    AS correo,
               COALESCE(stop, 'N')     AS stop,
               COALESCE(vend, '')      AS vend,
               COALESCE(provincia, '') AS provincia
        FROM scintela.cliente
        WHERE (COALESCE(telefono, '') <> '' OR COALESCE(correo, '') <> '')
          AND (%(q)s IS NULL
               OR UPPER(codigo_cli) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(nombre, '')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(telefono, '')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(correo, '')) LIKE UPPER(%(like)s))
        ORDER BY nombre ASC, codigo_cli ASC
        """,
        {"q": q or None, "like": like},
    ) or []


def directorio_resumen() -> dict:
    """KPIs del directorio: cuántos clientes tienen tel/email/ambos/ninguno."""
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE COALESCE(telefono, '') <> '')                                  AS con_tel,
               COUNT(*) FILTER (WHERE COALESCE(correo, '')   <> '')                                  AS con_email,
               COUNT(*) FILTER (WHERE COALESCE(telefono, '') <> '' AND COALESCE(correo, '') <> '')   AS con_ambos,
               COUNT(*) FILTER (WHERE COALESCE(telefono, '') = ''  AND COALESCE(correo, '') = '')    AS sin_contacto
        FROM scintela.cliente
        """
    ) or {}
    return {
        "total":         int(row.get("total")         or 0),
        "con_tel":       int(row.get("con_tel")       or 0),
        "con_email":     int(row.get("con_email")     or 0),
        "con_ambos":     int(row.get("con_ambos")     or 0),
        "sin_contacto":  int(row.get("sin_contacto")  or 0),
    }


def por_codigo(codigo_cli: str) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_cliente, codigo_cli, nombre, telefono, ruc, correo,
               direccion1, direccion2, stop, cupo, fecha_cupo, clave,
               pase, no_banco, descuento, pago, observacion, vend,
               provincia, canton, parroquia,
               COALESCE(activo, TRUE) AS activo
        FROM scintela.cliente
        WHERE codigo_cli = %s
        """,
        (codigo_cli.upper().strip(),),
    )


def crear(
    *,
    codigo_cli: str,
    nombre: str,
    ruc: str | None = None,
    telefono: str | None = None,
    correo: str | None = None,
    direccion1: str | None = None,
    direccion2: str | None = None,
    provincia: str | None = None,
    canton: str | None = None,
    parroquia: str | None = None,
    pago: str | None = None,
    cupo: int | None = None,
    vend: str | None = None,
    observacion: str | None = None,
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Alta de cliente. Código único."""
    codigo_cli = codigo_cli.upper().strip()
    if not codigo_cli:
        raise ValueError("Código requerido.")
    if not nombre:
        raise ValueError("Nombre requerido.")
    if db.fetch_one("SELECT 1 x FROM scintela.cliente WHERE codigo_cli = %s", (codigo_cli,)):
        raise ValueError(f"Ya existe un cliente con código {codigo_cli!r}.")
    return db.execute_returning(
        """
        INSERT INTO scintela.cliente
            (codigo_cli, nombre, telefono, ruc, correo,
             direccion1, direccion2, provincia, canton, parroquia,
             pago, cupo, vend,
             observacion, clave, stop, usuario_crea)
        VALUES (%s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, 'N', %s)
        RETURNING id_cliente, codigo_cli
        """,
        (
            codigo_cli, nombre[:200], (telefono or None) and telefono[:30],
            (ruc or None) and ruc[:16], (correo or None) and correo[:50],
            (direccion1 or None) and direccion1[:200],
            (direccion2 or None) and direccion2[:200],
            (provincia or None) and provincia[:30],
            (canton or None) and canton[:30],
            (parroquia or None) and parroquia[:30],
            (pago or None) and pago[:2], cupo,
            (vend or None) and vend[:50],
            (observacion or None) and observacion[:200],
            (clave or None) and clave[:3],
            usuario,
        ),
    ) or {}


def editar(
    codigo_cli: str,
    *,
    nombre: str | None = None,
    ruc: str | None = None,
    telefono: str | None = None,
    correo: str | None = None,
    direccion1: str | None = None,
    direccion2: str | None = None,
    provincia: str | None = None,
    canton: str | None = None,
    parroquia: str | None = None,
    pago: str | None = None,
    cupo: "int | _Vaciar | None" = None,
    vend: str | None = None,
    observacion: str | None = None,
    stop: str | None = None,
    usuario: str = "web",
) -> int:
    """Update de cliente. Devuelve cuántas filas CAMBIARON de verdad.

    Semántica de cada parámetro (no cambió para los callers viejos):

    * ``None``  → **no se toca** esa columna. Es lo que usan los callers
      parciales que sólo mandan un subconjunto de campos.
    * ``""`` (string vacío) o :data:`VACIAR` → la columna se pone en **NULL**.
      O sea: se puede VACIAR un campo desde la pantalla.

    TMT 2026-08-04 — caso KET. El cliente KET tenía vendedor ``FL1`` en PC y
    en el dBase **no tiene vendedor**. Había que dejarlo sin vendedor para
    que las dos bases coincidan y no se le siga liquidando comisión a FL1.
    No se podía: la vista mandaba ``form["vend"] or None`` y acá el
    ``if val is not None`` salteaba la columna. La pantalla contestaba
    "Cliente KET actualizado" y no cambiaba nada — peor que fallar, porque
    miente. El fix es de los dos lados: la vista manda el string tal cual
    (vacío incluido) y acá el corte es por ``is None``, no por falsy.

    El UPDATE lleva además un ``IS DISTINCT FROM`` por columna tocada, así
    que si mandás exactamente los valores que la ficha ya tiene devuelve
    ``0`` y la pantalla puede decir la verdad ("no había nada que cambiar")
    en vez de inventar un "actualizado". Sin eso el rowcount era siempre 1
    (porque `usuario_modifica` cambiaba solo) y no había forma de saberlo.
    """
    sets: list[str] = []
    valores: list = []
    distintos: list[str] = []
    mapping = {
        "nombre": (nombre, 200),
        "ruc": (ruc, 16),
        "telefono": (telefono, 30),
        "correo": (correo, 50),
        "direccion1": (direccion1, 200),
        "direccion2": (direccion2, 200),
        "provincia": (provincia, 30),
        "canton": (canton, 30),
        "parroquia": (parroquia, 30),
        "pago": (pago, 2),
        "vend": (vend, 50),
        "observacion": (observacion, 200),
        "stop": (stop, 1),
    }
    for col, (val, maxlen) in mapping.items():
        # OJO: `is None` (no `if val`). `""`/VACIAR son falsy y SÍ tienen que
        # llegar al UPDATE — son el pedido de vaciar la columna.
        if val is None:
            continue
        sets.append(f"{col} = %s")
        valores.append(val[:maxlen] if val else None)
        distintos.append(f"{col} IS DISTINCT FROM %s")
    if cupo is not None:
        sets.append("cupo = %s")
        valores.append(None if isinstance(cupo, _Vaciar) else cupo)
        distintos.append("cupo IS DISTINCT FROM %s")
    if not sets:
        return 0
    sets.append("usuario_modifica = %s")
    # Orden de params: valores del SET, usuario, código del WHERE y otra vez
    # los valores para los IS DISTINCT FROM.
    params = [*valores, usuario, codigo_cli.upper().strip(), *valores]
    return db.execute(
        f"UPDATE scintela.cliente SET {', '.join(sets)} "
        f"WHERE codigo_cli = %s AND ({' OR '.join(distintos)})",
        tuple(params),
    )


def set_stop(codigo_cli: str, stop: bool, usuario: str = "web", motivo: str = "") -> int:
    """Setear el flag stop ('S' o 'N'). Si se pasa motivo, se deja traza en observacion."""
    val = "S" if stop else "N"
    if motivo:
        return db.execute(
            "UPDATE scintela.cliente "
            "SET stop=%s, observacion=COALESCE(observacion||' | ','')||%s, "
            "    usuario_modifica=%s "
            "WHERE codigo_cli=%s",
            (val, f"[{val}] {motivo[:100]}", usuario, codigo_cli.upper().strip()),
        )
    return db.execute(
        "UPDATE scintela.cliente SET stop=%s, usuario_modifica=%s WHERE codigo_cli=%s",
        (val, usuario, codigo_cli.upper().strip()),
    )


def set_activo(codigo_cli: str, activo: bool, usuario: str = "web") -> int:
    """Soft-delete / reactivar cliente (legacy DIFUNTOS).

    Setea `cliente.activo = activo`. El cliente sigue existiendo y todas
    sus facturas/cheques históricos se preservan. Sólo lo esconde de
    los autocompletes y de la lista por default.
    """
    return db.execute(
        """
        UPDATE scintela.cliente
           SET activo = %s,
               observacion = COALESCE(observacion||' | ','')||%s,
               usuario_modifica = %s
         WHERE codigo_cli = %s
        """,
        (
            bool(activo),
            f"[{'ACTIVO' if activo else 'INACTIVO/DIFUNTO'}]",
            usuario[:50],
            codigo_cli.upper().strip(),
        ),
    )


def eliminar_por_id(id_cliente: int) -> int:
    """Borra un cliente por PK (id_cliente). Rechaza si tiene FKs.

    TMT 2026-05-20 v2 — pedido dueña: "TAMBIEN LAS PRIMERAS ROWS
    DEBERIAN SER ELIMINABLES". Algunos clientes legacy tienen
    codigo_cli='' o NULL — la URL /clientes/<codigo>/eliminar
    fallaba con 404. Usamos id_cliente (PK) que SIEMPRE existe.
    """
    fila = db.fetch_one(
        "SELECT codigo_cli FROM scintela.cliente WHERE id_cliente = %s",
        (int(id_cliente),),
    ) or {}
    codigo = (fila.get("codigo_cli") or "").strip()
    # TMT 2026-08-03 — dueña, sobre los dos 'GUF' que le aparecían con el
    # MISMO saldo en el estado de cuenta: "el otro borralo por ahora".
    # No se podía: la guarda de abajo mira las FKs POR CÓDIGO, y GUF tiene
    # 55 facturas + 8 cheques, así que rechazaba borrar las DOS filas —
    # incluida la sobrante. Y ninguna otra pantalla sirve: `editar` y `stop`
    # resuelven por `codigo_cli`, y con el código repetido no hay forma de
    # decir a cuál de las dos filas te referís. `eliminar` es la única que
    # va por PK.
    #
    # Los movimientos apuntan al CÓDIGO, no al id_cliente. Así que mientras
    # quede al menos otra fila con ese código, borrar la sobrante no deja
    # nada huérfano: la factura y el cheque siguen resolviendo. La guarda
    # sólo tiene sentido cuando ésta es la ÚLTIMA fila del código.
    #
    # scintela.cliente tiene 25 códigos duplicados (3.973 filas / 3.948
    # códigos): dos empresas distintas compartiendo el mismo código de 3
    # letras. Ver STATS/_CTE_CLI en modules/comisiones/queries.py — ahí el
    # duplicado contaba la cobranza dos veces.
    queda_otra_fila_con_el_codigo = False
    if codigo:
        otras = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.cliente "
            "WHERE UPPER(TRIM(codigo_cli)) = %s AND id_cliente <> %s",
            (codigo.upper(), int(id_cliente)),
        ) or {}
        queda_otra_fila_con_el_codigo = int(otras.get("n") or 0) > 0

    # Si tiene código, chequeamos FKs por código. Si NO tiene código,
    # asumimos que no puede tener facturas/cheques vinculados (no podrían
    # haberse cargado sin código del cliente).
    if codigo and not queda_otra_fila_con_el_codigo:
        n_fact = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.factura WHERE UPPER(codigo_cli) = %s",
            (codigo.upper(),),
        ) or {}
        if int(n_fact.get("n") or 0) > 0:
            raise ValueError(
                f"No se puede eliminar: el cliente {codigo} tiene "
                f"{n_fact['n']} facturas registradas."
            )
        n_che = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.cheque WHERE UPPER(codigo_cli) = %s",
            (codigo.upper(),),
        ) or {}
        if int(n_che.get("n") or 0) > 0:
            raise ValueError(
                f"No se puede eliminar: el cliente {codigo} tiene "
                f"{n_che['n']} cheques registrados."
            )
    return db.execute(
        "DELETE FROM scintela.cliente WHERE id_cliente = %s",
        (int(id_cliente),),
    )


# Alias legacy: si algo llama eliminar(codigo) sigue funcionando.
def eliminar(codigo_cli: str) -> int:
    """Wrapper legacy — convierte código a id_cliente y llama eliminar_por_id."""
    fila = db.fetch_one(
        "SELECT id_cliente FROM scintela.cliente WHERE codigo_cli = %s",
        (codigo_cli.upper().strip(),),
    ) or {}
    if not fila:
        raise ValueError(f"Cliente {codigo_cli!r} no encontrado.")
    return eliminar_por_id(int(fila["id_cliente"]))


# ---------------------------------------------------------------------------
# Cambiar el CÓDIGO de una ficha  (TMT 2026-08-04)
# ---------------------------------------------------------------------------
# POR QUÉ EXISTE
# --------------
# `scintela.cliente` tiene 20 códigos DUPLICADOS: dos empresas distintas
# compartiendo el mismo código de 3 letras. La causa es estructural — el
# código son las INICIALES del nombre, así que dos clientes con las mismas
# iniciales colisionan solos. `LEC` es "Luis Ernesto Cañamar" **y** "Lola
# Emperatriz Cisneros"; `LUL` es "Luis Llugla" **y** "Luis Lopez".
#
# De esos 20, 17 son DOS CLIENTES REALES: borrar uno (que es lo único que
# había hasta ahora, `eliminar_por_id`) perdería una ficha legítima. Lo que
# hace falta es que UNO DE LOS DOS pase a tener código propio. Hay 13.729
# combinaciones de 3 letras libres; hasta hoy no existía ninguna pantalla
# para usarlas.
def _movimientos_sql() -> str:
    """UNION ALL que cuenta, de una, los movimientos de un código."""
    return " UNION ALL ".join(
        f"SELECT '{tabla}' AS tabla, COUNT(*) AS n FROM {tabla} "
        f"WHERE UPPER(TRIM(codigo_cli)) = %(cod)s"
        for tabla, _ in _TABLAS_CON_CODIGO_CLI
    )


def movimientos_por_codigo(codigo_cli: str, conn=None) -> list[dict]:
    """Cuántos movimientos cuelgan de un CÓDIGO, tabla por tabla.

    Devuelve SIEMPRE una fila por tabla (con n=0 si no hay), así la pantalla
    de confirmación puede mostrar el cero explícito: "0 facturas, 0 cheques"
    es información, no ausencia de información.
    """
    cod = (codigo_cli or "").upper().strip()
    if not cod:
        return [{"tabla": t, "etiqueta": e, "n": 0} for t, e in _TABLAS_CON_CODIGO_CLI]
    filas = db.fetch_all(_movimientos_sql(), {"cod": cod}, conn=conn) or []
    por_tabla = {f["tabla"]: int(f.get("n") or 0) for f in filas}
    return [
        {"tabla": t, "etiqueta": e, "n": por_tabla.get(t, 0)}
        for t, e in _TABLAS_CON_CODIGO_CLI
    ]


def fichas_con_el_codigo(codigo_cli: str, conn=None) -> list[dict]:
    """Todas las fichas que comparten un código (normalmente 1, a veces 2)."""
    cod = (codigo_cli or "").upper().strip()
    if not cod:
        return []
    return db.fetch_all(
        "SELECT id_cliente, codigo_cli, nombre, ruc "
        "FROM scintela.cliente WHERE UPPER(TRIM(codigo_cli)) = %s "
        "ORDER BY id_cliente",
        (cod,),
        conn=conn,
    ) or []


def sugerir_codigos(nombre: str, limite: int = 8, conn=None) -> list[str]:
    """Códigos de 3 letras LIBRES parecidos al nombre del cliente.

    Sin esto el usuario tipea a ciegas y come "ya existe" una y otra vez
    (17.576 combinaciones, 3.848 ocupadas). Proponemos las iniciales
    canónicas y después las dos primeras iniciales + cada letra del apellido
    y del abecedario, quedándonos sólo con las que hoy no existen.
    """
    palabras = [p for p in re.split(r"[^A-Z0-9]+", (nombre or "").upper()) if p]
    if not palabras:
        return []
    iniciales = "".join(p[0] for p in palabras)
    candidatos: list[str] = []
    if len(iniciales) >= 3:
        candidatos.append(iniciales[:3])
    par = (iniciales[:2] if len(iniciales) >= 2 else (palabras[0] + "X")[:2])
    for letra in palabras[-1][1:] + "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidatos.append(par + letra)
    unicos: list[str] = []
    vistos: set[str] = set()
    for c in candidatos:
        if len(c) == 3 and _CODIGO_RE.match(c) and c not in vistos:
            vistos.add(c)
            unicos.append(c)
    if not unicos:
        return []
    ocupados = {
        (f.get("c") or "")
        for f in (
            db.fetch_all(
                "SELECT UPPER(TRIM(codigo_cli)) AS c FROM scintela.cliente "
                "WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)",
                (unicos,),
                conn=conn,
            )
            or []
        )
    }
    return [c for c in unicos if c not in ocupados][:limite]


def plan_cambio_codigo(id_cliente: int, nuevo_codigo: str, conn=None) -> dict:
    """Valida el cambio de código y devuelve el plan. **No escribe nada.**

    Levanta `ValueError` con el motivo exacto si no se puede. La pantalla de
    confirmación y el `cambiar_codigo` real usan ESTA misma función, así que
    lo que se muestra es literalmente lo que se va a aplicar.

    QUÉ SE MUEVE — la decisión (TMT 2026-08-04)
    -------------------------------------------
    Los movimientos NO se mueven: **si el código tiene movimientos, se
    RECHAZA el cambio.** Elegido a conciencia sobre la alternativa de
    arrastrarlos en la misma transacción, porque:

    1. El caso real es un código DUPLICADO. `factura.codigo_cli='LEC'` no
       dice a cuál de las dos empresas pertenece la factura — no hay
       `id_cliente` en la factura ni en el cheque. Los movimientos de las
       dos empresas están MEZCLADOS bajo el mismo string y NO existe forma
       automática de separarlos.
    2. Con esa mezcla, las dos opciones automáticas son igual de falsas:
       arrastrar todo le regala a la ficha renombrada la plata de la otra
       empresa; dejar todo le regala a la que se queda con el código viejo
       la plata de la renombrada. Un UPDATE masivo de historia financiera,
       encima, no se deshace desde ninguna pantalla.
    3. Rechazar deja el sistema en un estado *conocido* y le devuelve la
       decisión a la persona, que es la única que sabe qué factura es de
       quién. El subconjunto que SÍ se resuelve solo — la ficha sin ningún
       movimiento, típicamente la que creó `/admin/clientes-import` o la
       carga de Asinfo y nunca facturó — es exactamente el caso donde
       renombrar es demostrablemente seguro.

    Mismo criterio del lado del DESTINO: si el código nuevo ya tiene una
    ficha (requisito duro: si no, se crea una colisión nueva) **o** tiene
    movimientos huérfanos colgando, se rechaza — mudar la ficha ahí la
    haría dueña de plata que no es suya.
    """
    nuevo = (nuevo_codigo or "").strip().upper()
    if not _CODIGO_RE.match(nuevo):
        raise ValueError(
            "El código nuevo tiene que ser de 1 a 3 letras o números, sin "
            "espacios ni símbolos (ej. LEE)."
        )
    fila = db.fetch_one(
        "SELECT id_cliente, codigo_cli, nombre, ruc "
        "FROM scintela.cliente WHERE id_cliente = %s",
        (int(id_cliente),),
        conn=conn,
    )
    if not fila:
        raise ValueError(f"No existe la ficha de cliente #{int(id_cliente)}.")
    actual = (fila.get("codigo_cli") or "").strip().upper()
    if nuevo == actual:
        raise ValueError(f"La ficha ya tiene el código {nuevo}.")

    # (1) El destino no puede estar ocupado por otra ficha.
    ocupa = db.fetch_one(
        "SELECT codigo_cli, nombre FROM scintela.cliente "
        "WHERE UPPER(TRIM(codigo_cli)) = %s LIMIT 1",
        (nuevo,),
        conn=conn,
    )
    if ocupa:
        quien = (ocupa.get("nombre") or "").strip() or "sin nombre"
        raise ValueError(
            f"Ya existe un cliente con código {nuevo} ({quien}). Elegí otro: "
            "mudar la ficha ahí crearía una colisión NUEVA, que es justo lo "
            "que estamos deshaciendo."
        )

    # (1-bis) Tampoco puede tener movimientos huérfanos.
    movs_destino = movimientos_por_codigo(nuevo, conn=conn)
    total_destino = sum(m["n"] for m in movs_destino)
    if total_destino:
        detalle = ", ".join(f"{m['n']} {m['etiqueta']}" for m in movs_destino if m["n"])
        raise ValueError(
            f"El código {nuevo} no tiene ficha, pero tiene movimientos "
            f"colgando ({detalle}). Si mudás la ficha ahí, este cliente pasa "
            "a ser dueño de plata que no es suya. Elegí otro código."
        )

    # (2) El código de ORIGEN no puede tener movimientos. Ver el docstring.
    movimientos = movimientos_por_codigo(actual, conn=conn)
    total = sum(m["n"] for m in movimientos)
    hermanas = [
        f for f in fichas_con_el_codigo(actual, conn=conn)
        if int(f.get("id_cliente") or 0) != int(id_cliente)
    ]
    if total:
        detalle = ", ".join(f"{m['n']} {m['etiqueta']}" for m in movimientos if m["n"])
        mezcla = (
            " Además el código lo comparten "
            f"{len(hermanas) + 1} fichas, así que ni siquiera se sabe cuáles "
            "de esos movimientos son de esta empresa."
            if hermanas else ""
        )
        raise ValueError(
            f"No puedo cambiar el código {actual}: tiene {detalle}. Los "
            "movimientos apuntan al CÓDIGO, no a la ficha, y no hay forma "
            "automática de saber cuáles corresponden a este cliente."
            + mezcla
            + " Renombrá la ficha que NO tiene movimientos, o reasigná los "
            "movimientos uno por uno desde su propia pantalla y volvé."
        )

    return {
        "cliente": dict(fila),
        "codigo_actual": actual,
        "codigo_nuevo": nuevo,
        "movimientos": movimientos,
        "total_movimientos": total,
        "fichas_hermanas": hermanas,
    }


def cambiar_codigo(
    id_cliente: int,
    nuevo_codigo: str,
    *,
    usuario: str = "web",
    motivo: str = "",
) -> dict:
    """Aplica el cambio de código. Va por PK, no por código.

    Por PK porque el caso de uso ES el código duplicado: con dos fichas
    `LEC`, `codigo_cli` no alcanza para señalar cuál de las dos querés
    renombrar (mismo motivo por el que `eliminar_por_id` va por PK).

    Todo — validación y UPDATE — corre dentro de UNA transacción: si algo
    falla, no queda a medias y nadie ve un código intermedio.
    """
    with db.tx() as conn:
        plan = plan_cambio_codigo(id_cliente, nuevo_codigo, conn=conn)
        traza = f"[CÓDIGO {plan['codigo_actual'] or '(vacío)'}→{plan['codigo_nuevo']}]"
        if motivo:
            traza = f"{traza} {motivo[:100]}"
        n = db.execute(
            """
            UPDATE scintela.cliente
               SET codigo_cli = %s,
                   observacion = LEFT(COALESCE(observacion||' | ','')||%s, 200),
                   usuario_modifica = %s
             WHERE id_cliente = %s
            """,
            (plan["codigo_nuevo"], traza, usuario[:50], int(id_cliente)),
            conn=conn,
        )
        if n != 1:
            # Rollback: preferimos no cambiar nada antes que dejar el
            # renombre a medias (o pisar más de una ficha).
            raise ValueError(
                f"El cambio de código afectó {n} filas en vez de 1 — lo revertí."
            )
        plan["filas"] = n
    return plan


def buscar(q: str = "", limite: int = 200, incluir_inactivos: bool = False,
           offset: int = 0) -> list[dict]:
    """Lista paginada con búsqueda por código/nombre/RUC.

    TMT 2026-05-20 v2 — agregado `offset` para paginación (pedido dueña:
    "mostrando 200 clientes, dejame ir a una proxima pantalla").

    Por default filtra clientes con `activo=False` (legacy DIFUNTOS).
    Pasar `incluir_inactivos=True` para verlos.

    Incluye el saldo total (suma de factura.saldo) calculado en la misma
    query — evitar N+1.
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    pref = f"{q}%" if q else None
    return db.fetch_all(
        """
        SELECT c.id_cliente, c.codigo_cli, c.nombre, c.telefono, c.ruc, c.stop, c.cupo,
               c.pago, c.vend, c.fecha_cupo,
               COALESCE(c.direccion1, '') AS direccion1,
               COALESCE(c.direccion2, '') AS direccion2,
               COALESCE(c.provincia, '')  AS provincia,
               COALESCE(c.canton, '')     AS canton,
               COALESCE(c.parroquia, '')  AS parroquia,
               COALESCE(c.activo, TRUE) AS activo,
               COALESCE(s.saldo_total, 0) AS saldo_total,
               COALESCE(s.n_abiertas, 0)  AS n_abiertas
        FROM scintela.cliente c
        LEFT JOIN (
            SELECT codigo_cli,
                   SUM(saldo)  AS saldo_total,
                   COUNT(*)    AS n_abiertas
            FROM scintela.factura
            -- TMT 2026-07-06 (audit estado de cuenta): cartera NETA — los
            -- saldos NEGATIVOS (NC/sobrepagos) netean, y el backfill de
            -- Asinfo no cuenta (mismo criterio que cartera/queries y TOTF).
            WHERE COALESCE(saldo, 0) <> 0
              AND (stat IS NULL OR stat IN ('Z','A','',' '))
              AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
            GROUP BY codigo_cli
        ) s ON s.codigo_cli = c.codigo_cli
        WHERE (%(incluir_inactivos)s OR COALESCE(c.activo, TRUE) = TRUE)
          AND (%(q)s IS NULL
               OR UPPER(c.codigo_cli) LIKE UPPER(%(like)s)
               OR UPPER(c.nombre)     LIKE UPPER(%(like)s)
               OR c.ruc LIKE %(like)s)
        -- TMT 2026-05-20 v2 — pedido dueña: "Clientes idem, sortear
        -- por codigo" → sin término de búsqueda se mantiene código ASC.
        -- TMT 2026-07-06 — queja dueña: buscar "edu" listaba EDUARDOs por
        -- NOMBRE y el cliente con CÓDIGO "EDU" ni aparecía en la 1ra página.
        -- Con término ranqueamos como la búsqueda global (Ctrl/Cmd+K →
        -- informes.buscar_clientes): (0) código exacto, (1) código que
        -- EMPIEZA con el término, (2) resto (nombre/RUC contienen);
        -- dentro de cada grupo saldo pendiente DESC y luego nombre.
        ORDER BY
          CASE
            WHEN %(q)s IS NULL                            THEN 3
            WHEN UPPER(c.codigo_cli) = UPPER(%(q)s)       THEN 0
            WHEN UPPER(c.codigo_cli) LIKE UPPER(%(pref)s) THEN 1
            ELSE 2
          END ASC,
          CASE WHEN %(q)s IS NULL THEN NULL
               ELSE COALESCE(s.saldo_total, 0) END DESC NULLS LAST,
          CASE WHEN %(q)s IS NULL THEN NULL
               ELSE c.nombre END ASC NULLS FIRST,
          c.codigo_cli ASC
        LIMIT %(limite)s OFFSET %(offset)s
        """,
        {
            "q": q or None, "like": like, "pref": pref, "limite": limite,
            "incluir_inactivos": bool(incluir_inactivos),
            "offset": int(offset or 0),
        },
    )


def contar(q: str = "", incluir_inactivos: bool = False) -> int:
    """COUNT(*) total para paginación (sin LIMIT)."""
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM scintela.cliente c
         WHERE (%(incluir_inactivos)s OR COALESCE(c.activo, TRUE) = TRUE)
           AND (%(q)s IS NULL
                OR UPPER(c.codigo_cli) LIKE UPPER(%(like)s)
                OR UPPER(c.nombre)     LIKE UPPER(%(like)s)
                OR c.ruc LIKE %(like)s)
        """,
        {"q": q or None, "like": like,
         "incluir_inactivos": bool(incluir_inactivos)},
    ) or {}
    return int(row.get("n") or 0)


def cuenta_corriente(codigo_cli: str) -> dict:
    """Timeline unificado de movimientos del cliente con saldo acumulado.

    Construye un libro mayor por cliente uniendo:
      - facturas emitidas        (DEBE = importe)
      - devoluciones             (HABER = -importe; factura con importe<0)
      - aplicaciones de cheques  (HABER = chequesxfact.importe)
      - retenciones aplicadas    (HABER = retencion.rete)

    Ordenado por fecha ASC. Saldo acumulado fila por fila — lo recalcula
    Python sobre las filas devueltas por la UNION.

    Devuelve dict {cliente, movimientos, saldo_actual, totales}.

    Caso de uso (TMT 2026-05-11): clientes que pagan desordenado (Bedón) —
    ver TODOS los movimientos en un solo lado, no separados como hoy en
    `estado_cuenta`. El gerente quiere reconstruir la historia.
    """
    cli = db.fetch_one(
        """
        SELECT codigo_cli, nombre, telefono, ruc, cupo, stop, pago, pase,
               COALESCE(activo, TRUE) AS activo,
               -- TMT 2026-06-07: dirección para mostrar/imprimir en la cuenta
               COALESCE(direccion1, '') AS direccion1,
               COALESCE(direccion2, '') AS direccion2,
               COALESCE(provincia, '')  AS provincia,
               COALESCE(canton, '')     AS canton
        FROM scintela.cliente
        WHERE codigo_cli = %s
        """,
        (codigo_cli.upper().strip(),),
    )
    if not cli:
        return {
            "cliente": None,
            "movimientos": [],
            "saldo_actual": 0.0,
            "totales": {"facturado": 0.0, "cobrado": 0.0, "retenido": 0.0},
        }

    # UNION de eventos. Cada evento tiene: fecha, tipo, doc, debe, haber,
    # concepto, ref_id, stat (para etiquetar anulaciones/rebotes).
    eventos = db.fetch_all(
        """
        -- Facturas (emisión, incluye devoluciones con importe<0)
        SELECT f.fecha,
               CASE
                 WHEN COALESCE(f.importe, 0) < 0 THEN 'DEV'
                 WHEN f.stat = 'X' THEN 'ANUL'
                 ELSE 'FAC'
               END                                       AS tipo,
               COALESCE(f.numf_completo, f.numf::text)   AS doc,
               CASE WHEN COALESCE(f.importe,0) >= 0
                    THEN COALESCE(f.importe, 0) ELSE 0 END  AS debe,
               CASE WHEN COALESCE(f.importe,0) <  0
                    THEN -COALESCE(f.importe, 0) ELSE 0 END AS haber,
               'Factura ' || COALESCE(f.numf::text, '')  AS concepto,
               -- TMT 2026-06-07: ref_id de facturas = numf (el número del
               -- dBase, único identificador visible). El link del estado de
               -- cuenta va a /facturas/<numf>, no al id interno (que puede
               -- colisionar con el numf de OTRA factura).
               COALESCE(NULLIF(f.numf, 0), f.id_factura)  AS ref_id,
               f.stat
          FROM scintela.factura f
         WHERE f.codigo_cli = %(codigo)s
           -- TMT 2026-07-02 (bug NJL, whatsapp NUEVO SISTEMA 01-jul):
           -- excluir asinfo-backfill. Son facturas históricas Asinfo que
           -- dBase ya cerró/borró y NO cuentan en TOTF/cartera. El fix
           -- gemelo vive en cartera/queries.py y cheques/queries.py
           -- (facturas_pendientes). Antes, /clientes/<cod>/cuenta las
           -- listaba y sumaba $565,93 de saldo fantasma p.ej. a NJL.
           AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'

        UNION ALL

        -- Aplicaciones de cheque a factura (chequesxfact.importe = abono)
        SELECT COALESCE(c.fecha, cf.fechaing)            AS fecha,
               'ABO'                                      AS tipo,
               COALESCE(c.no_cheque, cf.id_chequexfact::text)  AS doc,
               0                                          AS debe,
               COALESCE(cf.importe, 0)                    AS haber,
               'Cheque ' || COALESCE(c.no_cheque, '?')
                 || COALESCE(' → fact ' || f.numf::text, '') AS concepto,
               c.id_cheque                                AS ref_id,
               c.stat                                     AS stat
          FROM scintela.chequesxfact cf
          LEFT JOIN scintela.cheque  c ON c.id_cheque  = cf.id_cheque
          LEFT JOIN scintela.factura f ON f.id_factura = cf.id_fact
         WHERE cf.codigo_cli = %(codigo)s
           -- TMT 2026-06-23: las aplicaciones del espejo de anticipo (NB=98) NO
           -- se cuentan como abono nuevo — el crédito ya entró como evento ANT
           -- al crearse. Contarlas acá las duplicaría.
           AND COALESCE(c.no_banco, 0) <> 98
           -- TMT 2026-07-02 (bug NJL): si el cheque se aplicó a una factura
           -- asinfo-backfill (que arriba se excluye del listado), NO mostrar
           -- el ABO tampoco — sino el saldo queda negativo (fábrica debe al
           -- cliente). Cheques aplicados a facturas reales pasan igual.
           -- Nota: caso NJL cxf#1092 → id_fact=198938 (borrada) y
           -- id_cheque huérfano (chq=None). Chequesxfact huérfano de una
           -- factura backfill que se purgó dejó $384,22 flotando. Excluimos
           -- ambos: sin factura real (f.id_factura IS NULL) O backfill.
           AND f.id_factura IS NOT NULL
           AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'

        UNION ALL

        -- TMT 2026-06-23 (dueña): SALDO A FAVOR / anticipos del cliente.
        -- El espejo negativo NB=98 que genera la cobranza cuando un cheque
        -- excede las facturas. Entra como HABER (reduce el saldo del cliente),
        -- igual que en el dBase. importe es negativo → haber = -importe.
        SELECT COALESCE(c.fecha_crea::date, c.fecha)        AS fecha,
               'ANT'                                        AS tipo,
               COALESCE(c.no_cheque, '#' || c.id_cheque::text) AS doc,
               0                                            AS debe,
               -COALESCE(c.importe, 0)                      AS haber,
               'Anticipo / saldo a favor (sobrante de cobranza)' AS concepto,
               c.id_cheque                                  AS ref_id,
               c.stat                                       AS stat
          FROM scintela.cheque c
         WHERE c.codigo_cli = %(codigo)s
           AND COALESCE(c.no_banco, 0) = 98
           AND COALESCE(c.stat, '') <> 'X'

        UNION ALL

        -- Retenciones emitidas por el cliente sobre nuestras facturas
        SELECT r.fecha,
               'RET'                                      AS tipo,
               COALESCE('Ret. fact ' || r.numf::text, 'Retención') AS doc,
               0                                          AS debe,
               COALESCE(r.rete, 0)                        AS haber,
               'Retención sobre factura ' || COALESCE(r.numf::text, '') AS concepto,
               r.id_retencion                             AS ref_id,
               NULL                                       AS stat
          FROM scintela.retencion r
         WHERE r.codigo_cli = %(codigo)s

        ORDER BY 1, 2
        """,
        {"codigo": codigo_cli.upper().strip()},
    )

    # Computar saldo acumulado fila por fila (en Python — más simple que
    # window function y total transparente).
    movimientos = []
    saldo = 0.0
    tot_fac = tot_cob = tot_ret = 0.0
    for ev in eventos:
        debe = float(ev.get("debe") or 0)
        haber = float(ev.get("haber") or 0)
        saldo += debe - haber
        if ev["tipo"] in ("FAC",):
            tot_fac += debe
        if ev["tipo"] == "DEV":
            tot_fac -= haber  # devolución reduce facturado
        if ev["tipo"] == "ABO":
            tot_cob += haber
        if ev["tipo"] == "ANT":
            tot_cob += haber  # el anticipo es plata recibida del cliente
        if ev["tipo"] == "RET":
            tot_ret += haber
        movimientos.append({
            "fecha":    ev["fecha"],
            "tipo":     ev["tipo"],
            "doc":      ev.get("doc") or "",
            "concepto": ev.get("concepto") or "",
            "debe":     debe,
            "haber":    haber,
            "saldo":    saldo,
            "ref_id":   ev.get("ref_id"),
            "stat":     ev.get("stat") or "",
        })

    return {
        "cliente": cli,
        "movimientos": movimientos,
        "saldo_actual": saldo,
        "totales": {
            "facturado": tot_fac,
            "cobrado":   tot_cob,
            "retenido":  tot_ret,
            "n_movimientos": len(movimientos),
        },
    }
