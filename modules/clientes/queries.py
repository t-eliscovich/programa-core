"""Consultas de clientes."""
import db


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
             direccion1, direccion2, pago, cupo, vend,
             observacion, clave, stop, usuario_crea)
        VALUES (%s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, 'N', %s)
        RETURNING id_cliente, codigo_cli
        """,
        (
            codigo_cli, nombre[:200], (telefono or None) and telefono[:30],
            (ruc or None) and ruc[:16], (correo or None) and correo[:50],
            (direccion1 or None) and direccion1[:200],
            (direccion2 or None) and direccion2[:200],
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
    pago: str | None = None,
    cupo: int | None = None,
    vend: str | None = None,
    observacion: str | None = None,
    stop: str | None = None,
    usuario: str = "web",
) -> int:
    """Update de cliente. Sólo campos no-None se modifican."""
    campos = []
    params: list = []
    mapping = {
        "nombre": (nombre, 200),
        "ruc": (ruc, 16),
        "telefono": (telefono, 30),
        "correo": (correo, 50),
        "direccion1": (direccion1, 200),
        "direccion2": (direccion2, 200),
        "pago": (pago, 2),
        "vend": (vend, 50),
        "observacion": (observacion, 200),
        "stop": (stop, 1),
    }
    for col, (val, maxlen) in mapping.items():
        if val is not None:
            campos.append(f"{col} = %s")
            params.append(val[:maxlen] if val else None)
    if cupo is not None:
        campos.append("cupo = %s")
        params.append(cupo)
    if not campos:
        return 0
    campos.append("usuario_modifica = %s")
    params.append(usuario)
    params.append(codigo_cli.upper().strip())
    return db.execute(
        f"UPDATE scintela.cliente SET {', '.join(campos)} WHERE codigo_cli = %s",
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
    # Si tiene código, chequeamos FKs por código. Si NO tiene código,
    # asumimos que no puede tener facturas/cheques vinculados (no podrían
    # haberse cargado sin código del cliente).
    if codigo:
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
            WHERE COALESCE(saldo, 0) > 0
              AND (stat IS NULL OR stat IN ('Z','A','',' '))
            GROUP BY codigo_cli
        ) s ON s.codigo_cli = c.codigo_cli
        WHERE (%(incluir_inactivos)s OR COALESCE(c.activo, TRUE) = TRUE)
          AND (%(q)s IS NULL
               OR UPPER(c.codigo_cli) LIKE UPPER(%(like)s)
               OR UPPER(c.nombre)     LIKE UPPER(%(like)s)
               OR c.ruc LIKE %(like)s)
        -- TMT 2026-05-20 v2 — pedido dueña: "Clientes idem, sortear
        -- por codigo". Antes ordenaba por saldo DESC (= columna que
        -- ya se eliminó del listado).
        ORDER BY c.codigo_cli ASC
        LIMIT %(limite)s OFFSET %(offset)s
        """,
        {
            "q": q or None, "like": like, "limite": limite,
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
               COALESCE(activo, TRUE) AS activo
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
