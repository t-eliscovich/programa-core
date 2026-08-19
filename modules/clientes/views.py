"""Listado y CRUD de clientes."""

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import db
from auth import (
    registrar_bitacora,
    requiere_login,
    requiere_permiso,
    tiene_permiso,
)
from error_messages import flash_exc
from exports import csv_response
from parsers import parse_int

from . import grupos as grupos_mod
from . import queries

clientes_bp = Blueprint("clientes", __name__, template_folder="templates")


def _form_from_request() -> dict:
    """Extrae los campos del request.form para reusar en GET/POST de nuevo/editar."""
    return {
        "codigo_cli": (request.form.get("codigo_cli") or "").strip().upper(),
        "nombre": (request.form.get("nombre") or "").strip(),
        "ruc": (request.form.get("ruc") or "").strip(),
        "telefono": (request.form.get("telefono") or "").strip(),
        "correo": (request.form.get("correo") or "").strip(),
        "direccion1": (request.form.get("direccion1") or "").strip(),
        "direccion2": (request.form.get("direccion2") or "").strip(),
        "provincia": (request.form.get("provincia") or "").strip(),
        "canton": (request.form.get("canton") or "").strip(),
        "parroquia": (request.form.get("parroquia") or "").strip(),
        "pago": (request.form.get("pago") or "").strip(),
        "cupo": request.form.get("cupo") or "",
        "descuento": (request.form.get("descuento") or "").strip(),
        "vend": (request.form.get("vend") or "").strip(),
        "observacion": (request.form.get("observacion") or "").strip(),
        "grupo": (request.form.get("grupo") or "").strip().upper(),
    }


def _parse_descuento(raw: str) -> float | None:
    """'7' o '7,5' → float. Vacío/basura → None (el caller decide VACIAR)."""
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _safe_next_url(raw: str | None) -> str | None:
    """Validar que `next` sea una ruta interna (empieza con `/` y no `//`).

    Sin esto, alguien con un link malicioso podría redirigir al usuario a
    un sitio externo después de la operación. Acepta sólo URLs relativas
    al mismo host.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith("/") or raw.startswith("//"):
        return None
    return raw


@clientes_bp.route("/clientes/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("clientes.crear")
def nuevo():
    errores: list[str] = []
    # `?codigo=XXX` pre-carga el código (típicamente desde facturas/nueva
    # cuando el usuario tipeó un cliente que no existe).
    # `?next=/url` indica adónde volver tras guardar (default: /clientes).
    pre_codigo = (request.args.get("codigo") or "").strip().upper()
    next_url = _safe_next_url(request.args.get("next"))

    if request.method == "GET":
        form = {"codigo_cli": pre_codigo} if pre_codigo else {}
        return render_template(
            "clientes/form.html",
            form=form,
            errores=errores,
            modo="crear",
            next_url=next_url,
            pre_codigo=pre_codigo,
        )

    form = _form_from_request()
    # Solo Andrés / accionistas setean el cupo (perm cupos.editar). TMT 2026-07-09.
    cupo = parse_int(form["cupo"]) if tiene_permiso("cupos.editar") else None
    descuento = _parse_descuento(form["descuento"]) if tiene_permiso("cupos.editar") else None
    # POST también puede traer `next` como hidden — preferir ese sobre el
    # query string porque sobrevive al re-render con errores.
    next_url = _safe_next_url(request.form.get("next") or request.args.get("next"))

    if not form["codigo_cli"]:
        errores.append("Código requerido.")
    if not form["nombre"]:
        errores.append("Nombre requerido.")

    if errores:
        return render_template(
            "clientes/form.html",
            form=form,
            errores=errores,
            modo="crear",
            next_url=next_url,
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:3].upper()
        queries.crear(
            codigo_cli=form["codigo_cli"],
            nombre=form["nombre"],
            ruc=form["ruc"] or None,
            telefono=form["telefono"] or None,
            correo=form["correo"] or None,
            direccion1=form["direccion1"] or None,
            direccion2=form["direccion2"] or None,
            provincia=form["provincia"] or None,
            canton=form["canton"] or None,
            parroquia=form["parroquia"] or None,
            pago=form["pago"] or None,
            cupo=cupo,
            descuento=descuento,
            vend=form["vend"] or None,
            observacion=form["observacion"] or None,
            clave=clave,
            usuario=usuario,
        )
        flash(f"Cliente {form['codigo_cli']} creado.", "ok")
        if next_url:
            return redirect(next_url)
        return redirect(url_for("clientes.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "clientes/form.html",
            form=form,
            errores=errores,
            modo="crear",
            next_url=next_url,
        ), 400
    except Exception as e:
        errores.append(f"No pude crear el cliente: {e}")
        return render_template(
            "clientes/form.html",
            form=form,
            errores=errores,
            modo="crear",
            next_url=next_url,
        ), 500


@clientes_bp.route("/clientes/<codigo_cli>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def editar(codigo_cli: str):
    cli = queries.por_codigo(codigo_cli)
    if not cli:
        abort(404)
    errores: list[str] = []
    # `?next=/url` (o hidden en el POST): adónde volver tras guardar. Ej: el
    # botón "Editar cliente" del estado de cuenta manda de vuelta ahí. TMT 2026-07-09.
    next_url = _safe_next_url(request.form.get("next") or request.args.get("next"))

    if request.method == "GET":
        form = {
            "codigo_cli": cli["codigo_cli"],
            "nombre": cli.get("nombre") or "",
            "ruc": cli.get("ruc") or "",
            "telefono": cli.get("telefono") or "",
            "correo": cli.get("correo") or "",
            "direccion1": cli.get("direccion1") or "",
            "direccion2": cli.get("direccion2") or "",
            "provincia": cli.get("provincia") or "",
            "canton": cli.get("canton") or "",
            "parroquia": cli.get("parroquia") or "",
            "pago": cli.get("pago") or "",
            "cupo": cli.get("cupo") or "",
            "descuento": ("" if cli.get("descuento") is None else cli.get("descuento")),
            "vend": cli.get("vend") or "",
            "observacion": cli.get("observacion") or "",
            # El grupo NO vive en scintela.cliente sino en su tabla propia
            # (un cliente puede ser padre sin tener fila). Ver grupos.py.
            "grupo": grupos_mod.grupo_de(cli["codigo_cli"]) or "",
            "stop": cli.get("stop") or "N",
            "activo": cli.get("activo", True),
            # Para el link a "Cambiar código", que va por PK (el código puede
            # estar duplicado y no señala una ficha concreta). TMT 2026-08-04.
            "id_cliente": cli.get("id_cliente"),
        }
        return render_template("clientes/form.html", form=form, errores=errores, modo="editar", next_url=next_url)

    form = _form_from_request()
    # Solo Andrés / accionistas editan el cupo (perm cupos.editar); el resto
    # conserva el cupo actual del cliente. TMT 2026-07-09.
    # TMT 2026-08-04: campo vacío = "sacale el cupo" (VACIAR), no "no lo
    # toques" — mismo agujero que el vendedor de KET (ver queries.editar).
    if tiene_permiso("cupos.editar"):
        cupo = parse_int(form["cupo"]) if form["cupo"].strip() else queries.VACIAR
        descuento = _parse_descuento(form["descuento"]) if form["descuento"] else queries.VACIAR
    else:
        cupo = cli.get("cupo")
        descuento = cli.get("descuento")

    if not form["nombre"]:
        errores.append("Nombre requerido.")
    if errores:
        form["codigo_cli"] = cli["codigo_cli"]
        return render_template("clientes/form.html", form=form, errores=errores, modo="editar", next_url=next_url), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        # TMT 2026-08-04 (caso KET) — los campos van TAL CUAL, string vacío
        # incluido. Antes iban con `or None` y `queries.editar` interpreta
        # None como "no toques esta columna": desde la pantalla se podía
        # cambiar un valor por otro pero NUNCA borrarlo. KET tenía vendedor
        # FL1 en PC y ninguno en el dBase; la pantalla decía "actualizado" y
        # no cambiaba nada.
        n = queries.editar(
            cli["codigo_cli"],
            nombre=form["nombre"],
            ruc=form["ruc"],
            telefono=form["telefono"],
            correo=form["correo"],
            direccion1=form["direccion1"],
            direccion2=form["direccion2"],
            provincia=form["provincia"],
            canton=form["canton"],
            parroquia=form["parroquia"],
            pago=form["pago"],
            cupo=cupo,
            descuento=descuento,
            vend=form["vend"],
            observacion=form["observacion"],
            usuario=usuario,
        )
        # El flash tiene que decir la verdad: `editar` devuelve 0 cuando no
        # cambió ninguna columna.
        if n:
            flash(f"Cliente {cli['codigo_cli']} actualizado.", "ok")
        else:
            flash(
                f"Cliente {cli['codigo_cli']} — no había nada que cambiar "
                "(los datos ya estaban así).",
                "warn",
            )
        # El GRUPO se guarda aparte: no es una columna de `cliente` sino una
        # fila en `grupo_cliente`, y tiene permiso propio (`grupos.editar`).
        # Va DESPUES del UPDATE y con su propio flash para que un error de
        # grupo -- "el codigo XXX no es un cliente" -- no haga perder los
        # cambios de nombre/telefono que si eran validos.
        if tiene_permiso("grupos.editar"):
            _aplicar_grupo_del_form(cli["codigo_cli"], form.get("grupo", ""), usuario)
        if next_url:
            return redirect(next_url)
        return redirect(url_for("clientes.lista"))
    except Exception as e:
        errores.append(f"No pude actualizar: {e}")
        return render_template("clientes/form.html", form=form, errores=errores, modo="editar", next_url=next_url), 500


@clientes_bp.route("/clientes/<codigo_cli>/stop", methods=["POST"])
@requiere_login
@requiere_permiso("stop_cliente.editar")
def toggle_stop(codigo_cli: str):
    cli = queries.por_codigo(codigo_cli)
    if not cli:
        abort(404)
    set_stop = (request.form.get("set") or "").upper() == "S"
    motivo = (request.form.get("motivo") or "").strip()
    # Activar STOP requiere motivo. Sacar STOP no (es restitución).
    # TMT 2026-05-13.
    if set_stop and not motivo:
        flash(
            f"Motivo requerido para poner el cliente {codigo_cli} en STOP. Queda en bitácora.",
            "warn",
        )
        return redirect(url_for("clientes.lista"))
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.set_stop(codigo_cli, set_stop, usuario=usuario, motivo=motivo)
        flash(
            f"Cliente {codigo_cli} — stop {'ACTIVADO' if set_stop else 'DESACTIVADO'}.",
            "ok",
        )
    except Exception as e:
        flash_exc("No pude cambiar stop", e)
    return redirect(url_for("clientes.lista"))


@clientes_bp.route("/clientes/<codigo_cli>/quitar-stop", methods=["POST"])
@requiere_login
@requiere_permiso("stop_cliente.editar")
def quitar_stop(codigo_cli: str):
    """Quitar STOP rápido — botón inline en /clientes (TMT 2026-05-21 dueña).

    Diferente de `toggle_stop`: no requiere motivo (sacar STOP es restitución,
    no penalización). Idempotente: si ya está sin STOP, no rompe nada.
    """
    cli = queries.por_codigo(codigo_cli)
    if not cli:
        abort(404)
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.set_stop(codigo_cli, False, usuario=usuario, motivo="")
        flash(f"STOP quitado a {codigo_cli}.", "ok")
    except Exception as e:
        flash_exc("No pude quitar STOP", e)
    return redirect(url_for("clientes.lista"))


# ---------------------------------------------------------------------------
# Cambiar el CÓDIGO de una ficha — 3 pantallas (form → confirmar → aplicar)
# ---------------------------------------------------------------------------
# TMT 2026-08-04. `scintela.cliente` tiene 20 códigos duplicados (LEC = "Luis
# Ernesto Cañamar" Y "Lola Emperatriz Cisneros"; LUL = "Luis Llugla" Y "Luis
# Lopez"): el código son las iniciales del nombre, así que dos clientes con
# las mismas iniciales colisionan solos. 17 de esos 20 son dos clientes
# REALES — borrar uno pierde una ficha legítima; lo que hace falta es que uno
# de los dos pase a tener código propio, y no existía pantalla para eso.
#
# Todo va por `id_cliente` (PK) y no por código: con dos fichas `LEC` el
# código no señala una ficha concreta (mismo motivo que `eliminar`).


def _ficha_por_id(id_cliente: int) -> dict | None:
    return db.fetch_one(
        "SELECT id_cliente, codigo_cli, nombre, ruc, "
        "       COALESCE(activo, TRUE) AS activo "
        "FROM scintela.cliente WHERE id_cliente = %s",
        (int(id_cliente),),
    )


@clientes_bp.route("/clientes/<int:id_cliente>/cambiar-codigo", methods=["GET"])
@requiere_login
@requiere_permiso("clientes.editar")
def cambiar_codigo_form(id_cliente: int):
    """Paso 1 de 3: elegir el código nuevo."""
    cli = _ficha_por_id(id_cliente)
    if not cli:
        abort(404)
    codigo = (cli.get("codigo_cli") or "").strip().upper()
    try:
        movimientos = queries.movimientos_por_codigo(codigo)
        hermanas = [
            f for f in queries.fichas_con_el_codigo(codigo)
            if int(f.get("id_cliente") or 0) != int(id_cliente)
        ]
        sugerencias = queries.sugerir_codigos(cli.get("nombre") or "")
        error = None
    except Exception as e:  # noqa: BLE001 — la pantalla se muestra igual
        movimientos, hermanas, sugerencias, error = [], [], [], str(e)
    return render_template(
        "clientes/cambiar_codigo.html",
        cli=cli,
        movimientos=movimientos,
        total_movimientos=sum(m["n"] for m in movimientos),
        hermanas=hermanas,
        sugerencias=sugerencias,
        nuevo_codigo=(request.args.get("nuevo") or "").strip().upper(),
        error=error,
    )


@clientes_bp.route("/clientes/<int:id_cliente>/confirmar-cambio-codigo", methods=["POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def confirmar_cambio_codigo(id_cliente: int):
    """Paso 2 de 3: mostrar exactamente qué se mueve y cuánto, antes de tocar nada."""
    cli = _ficha_por_id(id_cliente)
    if not cli:
        abort(404)
    nuevo = (request.form.get("nuevo_codigo") or "").strip().upper()
    try:
        plan = queries.plan_cambio_codigo(id_cliente, nuevo)
    except ValueError as e:
        flash(str(e), "warn")
        return redirect(
            url_for("clientes.cambiar_codigo_form", id_cliente=id_cliente, nuevo=nuevo)
        )
    except Exception as e:
        flash_exc("No pude revisar el cambio de código", e)
        return redirect(url_for("clientes.cambiar_codigo_form", id_cliente=id_cliente))

    detalle = {
        "Ficha": f"#{plan['cliente'].get('id_cliente')}",
        "Cliente": (plan["cliente"].get("nombre") or "(sin nombre)"),
        "RUC": (plan["cliente"].get("ruc") or "—"),
        "Código actual": plan["codigo_actual"] or "(sin código)",
        "Código nuevo": plan["codigo_nuevo"],
        "Fichas que comparten el código actual": (
            f"{len(plan['fichas_hermanas']) + 1} "
            f"({', '.join((f.get('nombre') or '?') for f in plan['fichas_hermanas'])})"
            if plan["fichas_hermanas"] else "1 (sólo ésta)"
        ),
    }
    # Se listan TODAS las tablas, incluidas las que dan 0: el cero explícito
    # es la prueba de que no se está moviendo plata a espaldas de nadie.
    movimientos = [
        {"texto": f"{m['n']} {m['etiqueta']}", "detalle": m["tabla"]}
        for m in plan["movimientos"]
    ]
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Cambiar el código {plan['codigo_actual'] or '(sin código)'} → {plan['codigo_nuevo']}",
        mensaje=(
            f"Se renombra SÓLO la ficha #{plan['cliente'].get('id_cliente')} "
            f"({plan['cliente'].get('nombre') or 'sin nombre'}). No se mueve "
            "ningún movimiento: el código actual no tiene facturas, cheques, "
            "retenciones ni cobros colgando, y por eso el cambio es seguro."
        ),
        detalle_registro=detalle,
        movimientos=movimientos,
        titulo_movimientos=(
            f"Movimientos atados al código {plan['codigo_actual'] or '(sin código)'}"
        ),
        accion_url=url_for("clientes.aplicar_cambio_codigo", id_cliente=id_cliente),
        volver_url=url_for("clientes.cambiar_codigo_form", id_cliente=id_cliente),
        extras_hidden=[{"name": "nuevo_codigo", "value": plan["codigo_nuevo"]}],
        motivo_requerido=True,
        motivo_obligatorio=False,
        confirm_label=f"Cambiar a {plan['codigo_nuevo']}",
    )


@clientes_bp.route("/clientes/<int:id_cliente>/aplicar-cambio-codigo", methods=["POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def aplicar_cambio_codigo(id_cliente: int):
    """Paso 3 de 3: aplicar (una sola transacción)."""
    if not _ficha_por_id(id_cliente):
        abort(404)
    nuevo = (request.form.get("nuevo_codigo") or "").strip().upper()
    motivo = (request.form.get("motivo") or "").strip()
    try:
        usuario = (g.user or {}).get("username", "web")
        plan = queries.cambiar_codigo(
            id_cliente, nuevo, usuario=usuario, motivo=motivo
        )
    except ValueError as e:
        flash(str(e), "warn")
        return redirect(
            url_for("clientes.cambiar_codigo_form", id_cliente=id_cliente, nuevo=nuevo)
        )
    except Exception as e:
        flash_exc("No pude cambiar el código", e)
        return redirect(url_for("clientes.cambiar_codigo_form", id_cliente=id_cliente))

    # Bitácora explícita: el after_request global sólo guarda el form, y acá
    # importa dejar asentado el código VIEJO (que ya no está en ningún lado).
    registrar_bitacora(
        modulo="clientes",
        accion="cambiar_codigo",
        entidad="cliente",
        id_entidad=id_cliente,
        payload={
            "codigo_anterior": plan["codigo_actual"],
            "codigo_nuevo": plan["codigo_nuevo"],
            "motivo": motivo,
        },
        resumen=(
            f"Código de cliente {plan['codigo_actual'] or '(sin código)'} → "
            f"{plan['codigo_nuevo']} (ficha #{id_cliente})"
        ),
    )
    flash(
        f"Código cambiado: {plan['codigo_actual'] or '(sin código)'} → "
        f"{plan['codigo_nuevo']}. Se renombró sólo la ficha; no había "
        "movimientos que mover.",
        "ok",
    )
    return redirect(url_for("clientes.lista", q=plan["codigo_nuevo"]))


@clientes_bp.route("/clientes/<int:id_cliente>/eliminar", methods=["POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def eliminar(id_cliente: int):
    """Borra un cliente con confirmación, por PK.

    TMT 2026-05-20 v2 — pedido dueña: rows legacy sin codigo_cli
    también deben ser eliminables. Usamos id_cliente (PK) en lugar
    de codigo_cli para que NUNCA falle por URL malformada.
    """
    fila = (
        db.fetch_one(
            "SELECT codigo_cli, nombre FROM scintela.cliente WHERE id_cliente = %s",
            (int(id_cliente),),
        )
        or {}
    )
    if not fila:
        abort(404)
    label = (fila.get("codigo_cli") or "(sin código)") + " — " + (fila.get("nombre") or "(sin nombre)")
    try:
        queries.eliminar_por_id(int(id_cliente))
        flash(f"Cliente {label} eliminado.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude eliminar", e)
    return redirect(url_for("clientes.lista"))


@clientes_bp.route("/clientes/<codigo_cli>/activar", methods=["POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def toggle_activo(codigo_cli: str):
    """Soft-delete / re-activar (legacy DIFUNTOS).

    Marcar inactivo NO borra al cliente — sus facturas históricas, saldos
    vivos y movimientos quedan intactos. Sólo lo esconde de los autocompletes
    y la lista por default.
    """
    cli = queries.por_codigo(codigo_cli)
    if not cli:
        abort(404)
    activar = (request.form.get("set") or "").lower() in ("1", "s", "si", "true", "yes")
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.set_activo(codigo_cli, activar, usuario=usuario)
        flash(
            f"Cliente {codigo_cli} — {'REACTIVADO' if activar else 'marcado como INACTIVO (difunto)'}.",
            "ok",
        )
    except Exception as e:
        flash_exc("No pude cambiar activo", e)
    return redirect(url_for("clientes.lista"))


@clientes_bp.route("/clientes/contactos")
@requiere_login
@requiere_permiso("clientes.ver")
def contactos():
    """Directorio de contactos — agenda rápida con tel/email + acciones directas."""
    q = request.args.get("q", "").strip()
    try:
        filas = queries.directorio(q=q)
        resumen = queries.directorio_resumen()
        error = None
    except Exception as e:
        filas, resumen, error = [], {}, str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo_cli", "Código"),
                ("nombre", "Cliente"),
                ("telefono", "Teléfono"),
                ("correo", "Email"),
                ("stop", "Stop"),
                ("vend", "Vend"),
                ("provincia", "Provincia"),
            ],
            filename="directorio_contactos.csv",
        )

    return render_template(
        "clientes/contactos.html",
        filas=filas,
        q=q,
        resumen=resumen,
        error=error,
    )


def _con_grupo(filas: list[dict]) -> list[dict]:
    """Agrega la clave `grupo` a cada fila del listado. Una sola query.

    El grupo de un cliente es el código de su PADRE; y si el cliente ES el
    padre de un grupo, es su propio código — así el grupo ECH se ve igual en la
    fila de ECH que en las de sus hijos, que es como está escrito el cuaderno.
    Sin grupo, cadena vacía (nunca "None": ver el fix del nombre del 2026-06-10).
    """
    try:
        padres = grupos_mod.mapa_padres()
    except Exception:
        # La lista de clientes NO depende de los grupos: si `grupo_cliente` no
        # está (base vieja, fixture mínima), la pantalla sale igual sin la
        # columna llena en vez de tirar 500.
        padres = {}
    cabezas = set(padres.values())
    salida = []
    for fila in filas:
        fila = dict(fila)
        cod = (fila.get("codigo_cli") or "").strip().upper()
        fila["grupo"] = padres.get(cod) or (cod if cod in cabezas else "")
        salida.append(fila)
    return salida


@clientes_bp.route("/clientes")
@requiere_login
@requiere_permiso("clientes.ver")
def lista():
    q = request.args.get("q", "").strip()
    # TMT 2026-05-21 dueña: ya no diferenciamos activos/inactivos en /clientes.
    # Siempre incluir todos. Mantenemos `incluir_inactivos=True` hardcodeado
    # para no romper la firma de queries.buscar/contar (otros callers la usan).
    incluir_inactivos = True
    # TMT 2026-05-20 v2 — paginación pedido dueña.
    try:
        pag = max(1, int(request.args.get("pag") or 1))
    except (TypeError, ValueError):
        pag = 1
    POR_PAG = 200
    offset = (pag - 1) * POR_PAG
    try:
        filas = queries.buscar(q, incluir_inactivos=True, limite=POR_PAG, offset=offset)
        total = queries.contar(q, incluir_inactivos=True)
        error = None
    except Exception as e:
        filas, total, error = [], 0, str(e)
    # Columna GRUPO (TMT 2026-08-19, dueña). Se resuelve con UNA query para
    # toda la tabla, no una por fila: `grupo_cliente` tiene 24 filas hoy y ~130
    # después de cargar el cuaderno, así que el mapa entero entra en memoria y
    # sale más barato que joinearlo en la query grande de `buscar`.
    filas = _con_grupo(filas)
    total_pag = max(1, (total + POR_PAG - 1) // POR_PAG)

    if request.args.get("export") == "csv":
        # CSV trae todo, sin paginación.
        try:
            todos = _con_grupo(queries.buscar(q, incluir_inactivos=True, limite=100000, offset=0))
        except Exception:
            todos = filas
        return csv_response(
            todos,
            columnas=[
                ("codigo_cli", "Código"),
                ("nombre", "Cliente"),
                ("ruc", "RUC"),
                ("telefono", "Teléfono"),
                ("direccion1", "Dirección"),
                ("direccion2", "Dirección 2"),
                ("provincia", "Provincia"),
                ("canton", "Cantón"),
                ("parroquia", "Parroquia"),
                ("pago", "Pago"),
                ("vend", "Vend"),
                ("grupo", "Grupo"),
                ("stop", "Stop"),
                ("cupo", "Cupo"),
                ("saldo_total", "Saldo"),
                ("n_abiertas", "Fact. abiertas"),
            ],
            filename="clientes.csv",
        )

    return render_template(
        "clientes/lista.html",
        filas=filas,
        q=q,
        error=error,
        incluir_inactivos=incluir_inactivos,
        pag=pag,
        total_pag=total_pag,
        total=total,
        por_pag=POR_PAG,
    )


@clientes_bp.route("/clientes/<codigo_cli>/cuenta", methods=["GET"])
@requiere_login
@requiere_permiso("clientes.ver")
def cuenta(codigo_cli: str):
    """Cuenta corriente del cliente: timeline unificado de movimientos.

    Junta facturas, devoluciones, aplicaciones de cheque y retenciones
    en una sola lista ordenada por fecha, con saldo acumulado. Útil
    para clientes que pagan desordenado (TMT 2026-05-11: caso Bedón).
    """
    data = queries.cuenta_corriente(codigo_cli)
    if not data["cliente"]:
        abort(404)

    if request.args.get("export") == "csv":
        return csv_response(
            data["movimientos"],
            columnas=[
                ("fecha", "Fecha"),
                ("tipo", "Tipo"),
                ("doc", "Documento"),
                ("concepto", "Concepto"),
                ("debe", "Debe"),
                ("haber", "Haber"),
                ("saldo", "Saldo"),
            ],
            filename=f"cuenta_{codigo_cli.upper()}.csv",
        )

    return render_template("clientes/cuenta.html", data=data)


@clientes_bp.route("/clientes/mails-asinfo/refrescar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def refrescar_mails_asinfo():
    """Trae de Asinfo el catálogo de mails de clientes, a pedido.

    TMT 2026-08-03. Normalmente lo hace el cron diario (/admin/health/all),
    pero hace falta poder correrlo a mano: cuando alguien carga mails en el
    ERP y los quiere ver hoy, y para el primer llenado. Es idempotente y no
    escribe nada en la ficha del cliente — sólo la tabla espejo.
    """
    from modules.clientes import mail_asinfo

    forzar = (request.args.get("forzar") or "") == "1"
    if not forzar and mail_asinfo.esta_fresco():
        return jsonify({"ok": True, "salteado": "ya está fresco (usá ?forzar=1)"})
    try:
        return jsonify(mail_asinfo.refrescar())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


# ---------------------------------------------------------------------------
# Carga masiva de cupos — TMT 2026-08-05.
#
# La dueña completó el Excel "clientes-sin-cupo" (423 filas) con la columna
# CUPO A CARGAR y pidió subirlos todos ("los vacíos quedan en 0"). Cargar 423
# fichas a mano por /clientes/<cod>/editar es inviable → esta pantalla
# (regla operar-por-la-ui: si sólo se puede scripteando, falta UI).
#
# Flujo en dos pasos, mismo patrón que la carga de tejeduría: subir el xlsx →
# PREVIEW (qué cambia, qué queda igual, qué código no existe) → Confirmar.
# Nada se escribe hasta el Confirmar, y la escritura va por queries.editar()
# (misma función que la ficha: IS DISTINCT FROM, usuario_modifica, bitácora).
# ---------------------------------------------------------------------------

@clientes_bp.route("/clientes/cupos-carga", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cupos.editar")
def cupos_carga():
    from .cupos_xlsx import parse_cupos_xlsx

    if request.method == "GET":
        return render_template("clientes/cupos_carga.html", paso="subir", avisos=[])

    f = request.files.get("archivo")
    if f is None or not f.filename:
        return render_template(
            "clientes/cupos_carga.html", paso="subir",
            avisos=["Elegí un archivo .xlsx antes de subir."],
        ), 400
    filas, avisos = parse_cupos_xlsx(f.read())
    if not filas:
        return render_template(
            "clientes/cupos_carga.html", paso="subir", avisos=avisos,
        ), 400

    codigos = [x.codigo for x in filas]
    fichas = db.fetch_all(
        "SELECT codigo_cli, nombre, cupo FROM scintela.cliente "
        "WHERE codigo_cli = ANY(%s)",
        (codigos,),
    )
    por_codigo: dict[str, list[dict]] = {}
    for ficha in fichas:
        por_codigo.setdefault(ficha["codigo_cli"], []).append(ficha)

    preview = []
    for x in filas:
        encontradas = por_codigo.get(x.codigo, [])
        if not encontradas:
            estado = "no_encontrado"
            nombre, cupo_actual = None, None
        else:
            nombre = encontradas[0]["nombre"]
            cupo_actual = encontradas[0]["cupo"]
            # Código duplicado en la base (pares abiertos de la mig 0155):
            # el UPDATE va por codigo_cli, las DOS fichas quedan con el cupo.
            estado = "igual" if all(
                (ficha["cupo"] or 0) == x.cupo for ficha in encontradas
            ) else "cambia"
        preview.append({
            "codigo": x.codigo, "cupo_nuevo": x.cupo, "nombre": nombre,
            "cupo_actual": cupo_actual, "estado": estado,
            "fichas": len(encontradas),
        })
    orden = {"no_encontrado": 0, "cambia": 1, "igual": 2}
    preview.sort(key=lambda p: (orden[p["estado"]], p["codigo"]))
    resumen = {
        "cambia": sum(1 for p in preview if p["estado"] == "cambia"),
        "igual": sum(1 for p in preview if p["estado"] == "igual"),
        "no_encontrado": sum(1 for p in preview if p["estado"] == "no_encontrado"),
    }
    # El payload que viaja al Confirmar: sólo códigos que EXISTEN. Formato
    # COD=ENTERO por línea; el aplicar lo re-valida línea a línea.
    payload = "\n".join(
        f"{p['codigo']}={p['cupo_nuevo']}" for p in preview
        if p["estado"] != "no_encontrado"
    )
    return render_template(
        "clientes/cupos_carga.html", paso="confirmar", avisos=avisos,
        preview=preview, resumen=resumen, payload=payload,
        archivo=f.filename,
    )


@clientes_bp.route("/clientes/cupos-carga/aplicar", methods=["POST"])
@requiere_login
@requiere_permiso("cupos.editar")
def cupos_carga_aplicar():
    import re as _re

    lineas = [ln.strip() for ln in (request.form.get("payload") or "").splitlines() if ln.strip()]
    items: list[tuple[str, int]] = []
    for ln in lineas:
        m = _re.fullmatch(r"([A-Z0-9]{1,10})=(\d{1,9})", ln)
        if not m:
            abort(400)
        items.append((m.group(1), int(m.group(2))))
    if not items:
        flash("No había nada para aplicar.", "warn")
        return redirect(url_for("clientes.cupos_carga"))

    usuario = (g.user or {}).get("username", "web")
    cambiados = 0
    for codigo, cupo in items:
        cambiados += queries.editar(codigo, cupo=cupo, usuario=usuario)
    registrar_bitacora(
        modulo="clientes", accion="cupos_carga_masiva",
        payload={"filas": len(items), "cambiados": cambiados},
    )
    flash(
        f"Cupos cargados: {cambiados} ficha(s) actualizadas de {len(items)} filas del archivo.",
        "ok",
    )
    return redirect(url_for("clientes.lista"))


# ---------------------------------------------------------------------------
# GRUPOS DE CLIENTES
#
# TMT 2026-08-19 (duena, con las fotos del cuaderno de la oficina): *"Estos
# clientes son de un mismo grupo. en clientes una columna mas que diga grupo y
# ponemos el primero codigo que aparece en lista. Esto tiene que ser editable.
# (...) Usar tambien para impresion por grupos, estos clientes juntos"*.
#
# La tabla `scintela.grupo_cliente` existia desde mayo y ya la leian la cartera
# por grupo y el estado de cuenta agrupado -- pero se cargaba por SQL a mano,
# que es justo lo que prohibe la regla `operar-por-la-ui`. Esto es la UI que
# faltaba: el lapicito de la columna Grupo, el campo de la ficha y la carga
# masiva por Excel. La logica vive en `grupos.py`.
# ---------------------------------------------------------------------------


def _aplicar_grupo_del_form(codigo_cli: str, pedido: str, usuario: str) -> None:
    """Aplica el campo Grupo de la ficha. Vacio = sacarlo del grupo.

    No devuelve nada: avisa por flash, porque los dos caminos (lapicito de la
    lista y ficha) terminan en un redirect.
    """
    pedido = (pedido or "").strip().upper()
    actual = grupos_mod.grupo_de(codigo_cli) or ""
    if pedido == actual:
        return
    if not pedido:
        ok, msg = grupos_mod.quitar(codigo_cli, usuario=usuario)
    elif pedido == (codigo_cli or "").strip().upper():
        # Escribirse a si mismo es "que este grupo se llame como yo". Si el
        # cliente ya es cabeza no hay nada que hacer; si estaba adentro de otro
        # grupo, es sacarlo para que arme el suyo.
        ok, msg = grupos_mod.quitar(codigo_cli, usuario=usuario)
    else:
        ok, msg = grupos_mod.asignar(codigo_cli, pedido, usuario=usuario)
    flash(msg, "ok" if ok else "error")
    if ok:
        registrar_bitacora(
            modulo="clientes", accion="grupo_editar",
            payload={"codigo_cli": codigo_cli, "grupo": pedido or None},
        )


@clientes_bp.route("/clientes/<codigo_cli>/grupo", methods=["POST"])
@requiere_login
@requiere_permiso("grupos.editar")
def editar_grupo(codigo_cli: str):
    """El lapicito de la columna Grupo de /clientes."""
    if not queries.por_codigo(codigo_cli):
        abort(404)
    usuario = (g.user or {}).get("username", "web")
    _aplicar_grupo_del_form(codigo_cli, request.form.get("grupo") or "", usuario)
    destino = _safe_next_url(request.form.get("next"))
    return redirect(destino or url_for("clientes.lista"))


@clientes_bp.route("/clientes/grupos-carga", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("grupos.editar")
def grupos_carga():
    """Sube el Excel de grupos -> PREVIEW -> Confirmar.

    Mismo flujo en dos pasos que la carga de cupos: nada se escribe hasta el
    Confirmar, y el preview dice fila por fila que va a pasar.
    """
    from .grupos_xlsx import parse_grupos_xlsx

    if request.method == "GET":
        return render_template("clientes/grupos_carga.html", paso="subir", avisos=[])

    f = request.files.get("archivo")
    if f is None or not f.filename:
        return render_template(
            "clientes/grupos_carga.html", paso="subir",
            avisos=["Elegí un archivo .xlsx antes de subir."],
        ), 400
    filas, avisos = parse_grupos_xlsx(f.read())
    if not filas:
        return render_template(
            "clientes/grupos_carga.html", paso="subir", avisos=avisos,
        ), 400

    codigos = sorted({x.codigo for x in filas} | {x.grupo for x in filas})
    fichas = db.fetch_all(
        "SELECT UPPER(TRIM(codigo_cli)) AS cod, nombre FROM scintela.cliente "
        "WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)",
        (codigos,),
    ) or []
    nombre_de = {ficha["cod"]: ficha["nombre"] for ficha in fichas}
    actual = grupos_mod.mapa_padres()

    preview = []
    for x in filas:
        if x.codigo not in nombre_de:
            estado = "no_existe"
        elif x.grupo not in nombre_de:
            estado = "grupo_no_existe"
        elif actual.get(x.codigo) == x.grupo:
            estado = "igual"
        elif x.codigo in actual:
            estado = "cambia"
        else:
            estado = "nuevo"
        preview.append({
            "codigo": x.codigo, "grupo": x.grupo, "fila": x.fila,
            "nombre": nombre_de.get(x.codigo),
            "nombre_grupo": nombre_de.get(x.grupo),
            "grupo_actual": actual.get(x.codigo),
            "estado": estado,
        })
    orden = {"no_existe": 0, "grupo_no_existe": 1, "cambia": 2, "nuevo": 3, "igual": 4}
    preview.sort(key=lambda p: (orden[p["estado"]], p["grupo"], p["codigo"]))
    resumen = {k: sum(1 for p in preview if p["estado"] == k) for k in orden}
    resumen["grupos"] = len({
        p["grupo"] for p in preview if p["estado"] in ("nuevo", "cambia", "igual")
    })
    payload = "\n".join(
        f"{p['codigo']}={p['grupo']}" for p in preview
        if p["estado"] in ("nuevo", "cambia")
    )
    return render_template(
        "clientes/grupos_carga.html", paso="confirmar", avisos=avisos,
        preview=preview, resumen=resumen, payload=payload, archivo=f.filename,
    )


@clientes_bp.route("/clientes/grupos-carga/aplicar", methods=["POST"])
@requiere_login
@requiere_permiso("grupos.editar")
def grupos_carga_aplicar():
    import re as _re

    lineas = [ln.strip() for ln in (request.form.get("payload") or "").splitlines() if ln.strip()]
    items: list[tuple[str, str]] = []
    for ln in lineas:
        m = _re.fullmatch(r"([A-Z0-9]{1,10})=([A-Z0-9]{1,10})", ln)
        if not m:
            abort(400)
        items.append((m.group(1), m.group(2)))
    if not items:
        flash("No había nada para aplicar.", "warn")
        return redirect(url_for("clientes.grupos_carga"))

    usuario = (g.user or {}).get("username", "web")
    aplicados, fallados = 0, []
    for codigo, grupo in items:
        ok, msg = grupos_mod.asignar(codigo, grupo, usuario=usuario)
        if ok:
            aplicados += 1
        else:
            fallados.append(msg)
    registrar_bitacora(
        modulo="clientes", accion="grupos_carga_masiva",
        payload={"filas": len(items), "aplicados": aplicados},
    )
    flash(f"Grupos cargados: {aplicados} cliente(s) de {len(items)} filas.", "ok")
    # Los que fallaron se muestran uno por uno: son pocos y cada uno dice por
    # que. Un "hubo 3 errores" sin decir cuales obliga a rehacer el Excel a
    # ciegas.
    for msg in fallados[:20]:
        flash(msg, "error")
    return redirect(url_for("clientes.lista"))
