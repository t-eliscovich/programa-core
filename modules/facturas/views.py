"""Listado y detalle de facturas."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

import db
from auth import requiere_login, requiere_permiso
from error_messages import flash_exc, humanize
from exports import csv_response

from . import queries

facturas_bp = Blueprint("facturas", __name__, template_folder="templates")


def _parse_date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_monto(s: str):
    s = (s or "").strip()
    if s == "":
        return None
    if "," in s and s.count(",") == 1:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


@facturas_bp.route("/facturas/nueva", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def nueva():
    """Crear una factura nueva. Preserva reglas de ALTAS.PRG:

    - stat inicial 'A', saldo = importe, abono = 0
    - vencimiento = fecha + cliente.pago días (default 30) si no se indica
    - numf auto = MAX(numf)+1 si se deja vacío
    """
    errores: list[str] = []
    form: dict = {}

    # Datalist de clientes para autocomplete — bajo costo, <2000 items.
    try:
        from modules.autocomplete.queries import clientes_para_datalist
        clientes_datalist = clientes_para_datalist()
    except Exception:
        clientes_datalist = []

    # Sugerir siguiente numf + fecha hoy para GET
    if request.method == "GET":
        try:
            form["numf_sugerido"] = queries.proximo_numf()
        except Exception:
            form["numf_sugerido"] = ""
        # Fecha default en DD/MM/YYYY — formato que la contadora espera tipear.
        form["fecha"] = datetime.now().date().strftime("%d/%m/%Y")
        # Restaurar campos pre-cargados via query string (ej. cuando se
        # redirige de /clientes/nuevo después de crear un cliente nuevo
        # que disparó este form). Cualquier campo del form aparece en
        # request.args sobrescribe el default.
        for k in ("fecha", "codigo_cli", "kg", "importe", "numf",
                  "vencimiento", "condic", "tipo", "numf_completo"):
            if request.args.get(k):
                form[k] = request.args.get(k)
        # Si veníamos de crear un cliente, el flash "Cliente XYZ creado"
        # ya lo manda clientes.nuevo. Acá NO duplicamos — sino aparecen
        # dos toasts en pantalla. La banner azul en el form alcanza para
        # comunicar el contexto de "veníamos de otra pantalla".
        return render_template("facturas/nueva.html", form=form, errores=errores,
                               clientes_datalist=clientes_datalist)

    # POST — validar
    fecha = _parse_date(request.form.get("fecha"))
    codigo_cli = (request.form.get("codigo_cli") or "").strip().upper()
    kg = _parse_monto(request.form.get("kg"))
    importe = _parse_monto(request.form.get("importe"))
    numf_raw = (request.form.get("numf") or "").strip()
    numf = int(numf_raw) if numf_raw.isdigit() else None
    venci = _parse_date(request.form.get("vencimiento"))
    condic = (request.form.get("condic") or "").strip()[:2] or None
    tipo = (request.form.get("tipo") or "").strip()[:2] or None
    numf_completo = (request.form.get("numf_completo") or "").strip() or None
    # Devolución: el dBase la trata como factura con kg/importe negativos
    # (MODIFICA.PRG:1195 — NT.DEVOL = NUMF>0 AND IMPORTE<0). Si el usuario
    # tipea valores positivos, le ponemos el signo menos automáticamente.
    # Si los tipea ya negativos, los respetamos tal cual.
    devolucion = bool(request.form.get("devolucion"))
    if devolucion:
        if kg is not None and kg > 0:
            kg = -kg
        if importe is not None and importe > 0:
            importe = -importe

    if fecha is None:
        errores.append("Fecha inválida.")
    if not codigo_cli:
        errores.append("Código de cliente requerido.")
    elif not db.fetch_one(
        "SELECT 1 AS x FROM scintela.cliente WHERE codigo_cli = %s",
        (codigo_cli,),
    ):
        # Cliente no existe → flujo guiado: mandamos al usuario a
        # /clientes/nuevo con el código pre-cargado, y guardamos los datos
        # ya tipeados del form en el `next` URL para restaurar la factura
        # cuando el cliente se cree. TMT 2026-05-11: pidió que el flujo
        # sea automático en vez de tener que cargar manualmente el cliente.
        # Permisos viven en g.permisos (top-level), NO en g.user["permisos"]
        # — convención canónica del skill programa-core.
        _permisos = getattr(g, "permisos", set()) or set()
        if "clientes.crear" in _permisos or "*" in _permisos:
            from urllib.parse import urlencode
            restore_args = {
                "fecha": request.form.get("fecha") or "",
                "codigo_cli": codigo_cli,
                "kg": request.form.get("kg") or "",
                "importe": request.form.get("importe") or "",
                "numf": numf_raw,
                "vencimiento": request.form.get("vencimiento") or "",
                "condic": condic or "",
                "tipo": tipo or "",
                "numf_completo": numf_completo or "",
                "vuelta": "1",
            }
            restore_args = {k: v for k, v in restore_args.items() if v}
            next_url = url_for("facturas.nueva") + "?" + urlencode(restore_args)
            flash(
                f"El cliente {codigo_cli} no existe — completá los datos "
                "para crearlo y después seguís con la factura.",
                "warning",
            )
            return redirect(
                url_for("clientes.nuevo", codigo=codigo_cli, next=next_url)
            )
        # Sin permiso clientes.crear, mantener el error clásico.
        errores.append(f"El cliente {codigo_cli!r} no existe.")
    # Validación de signo: venta normal pide positivos, devolución pide
    # negativos (= la mercadería vuelve, los $ regresan al cliente).
    if devolucion:
        if importe is None or importe >= 0:
            errores.append("Devolución: importe debe ser distinto de cero.")
        if kg is None or kg > 0:
            errores.append("Devolución: kg debe ser distinto de cero.")
    else:
        if importe is None or importe <= 0:
            errores.append("Importe requerido (mayor que cero).")
        if kg is None or kg < 0:
            errores.append("Kg requerido (no puede ser negativo).")

    # Preservar lo que cargó el usuario para re-renderizar el form
    form.update({
        "fecha": request.form.get("fecha"),
        "codigo_cli": codigo_cli,
        "kg": request.form.get("kg"),
        "importe": request.form.get("importe"),
        "numf": numf_raw,
        "vencimiento": request.form.get("vencimiento"),
        "condic": condic or "",
        "tipo": tipo or "",
        "numf_completo": numf_completo or "",
        "devolucion": devolucion,
    })

    if errores:
        return render_template("facturas/nueva.html", form=form, errores=errores,
                               clientes_datalist=clientes_datalist), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:2].upper()
        creada = queries.crear(
            fecha=fecha,
            codigo_cli=codigo_cli,
            kg=kg, importe=importe,
            numf=numf,
            vencimiento=venci,
            condic=condic, tipo=tipo,
            numf_completo=numf_completo,
            clave=clave,
            usuario=usuario,
        )
        etiqueta = "Devolución" if devolucion else "Factura"
        flash(f"{etiqueta} N° {creada.get('numf')} creada.", "ok")
        return redirect(url_for("facturas.detalle", id_factura=creada["id_factura"]))
    except Exception as e:
        # TMT 2026-05-14 (#37): humanizar antes de mostrar.
        import logging as _logging
        _logging.getLogger("programa_core.facturas").exception(
            "facturas.nueva falló"
        )
        errores.append(f"No pude crear la factura: {humanize(e)}")
        return render_template("facturas/nueva.html", form=form, errores=errores,
                               clientes_datalist=clientes_datalist), 500


@facturas_bp.route("/facturas/<int:id_factura>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def editar(id_factura: int):
    """Edición *blanda* de una factura emitida.

    Sólo se puede tocar abono / condic / observacion. Para corregir importe,
    cliente, fecha, kg, numf → anular y reemitir (regla Ecuador).
    """
    fact = queries.por_id(id_factura)
    if not fact:
        abort(404)
    if (fact.get("stat") or "").upper() in queries.STATS_ANULADAS:
        flash("La factura está anulada/eliminada — no se puede editar.", "warn")
        return redirect(url_for("facturas.detalle", id_factura=id_factura))

    errores: list[str] = []
    form: dict = {
        "abono": str(fact.get("abono") or 0),
        "condic": fact.get("condic") or "",
        "observacion": "",
    }

    if request.method == "POST":
        abono_str = request.form.get("abono")
        abono = _parse_monto(abono_str)
        condic = (request.form.get("condic") or "").strip().upper()[:2] or None
        observacion = (request.form.get("observacion") or "").strip() or None

        if abono is None:
            errores.append("Abono inválido.")
        elif float(abono) < 0:
            errores.append("El abono no puede ser negativo.")

        form.update({
            "abono": abono_str,
            "condic": condic or "",
            "observacion": observacion or "",
        })

        if errores:
            return render_template(
                "facturas/editar.html",
                fact=fact, form=form, errores=errores,
            ), 400
        try:
            usuario = (g.user or {}).get("username", "web")
            res = queries.editar(
                id_factura,
                abono=abono,
                condic=condic,
                observacion=observacion,
                usuario=usuario,
            )
            flash(
                f"Factura editada — saldo nuevo: $ {res['saldo']:,.2f} (stat: {res['stat_nuevo']}).",
                "ok",
            )
            return redirect(url_for("facturas.detalle", id_factura=id_factura))
        except ValueError as e:
            errores.append(str(e))
            return render_template(
                "facturas/editar.html",
                fact=fact, form=form, errores=errores,
            ), 400
        except Exception as e:
            # TMT 2026-05-14 (#37): humanizar antes de mostrar.
            import logging as _logging
            _logging.getLogger("programa_core.facturas").exception(
                "facturas.editar falló id=%s", id_factura
            )
            errores.append(f"Error al editar: {humanize(e)}")
            return render_template(
                "facturas/editar.html",
                fact=fact, form=form, errores=errores,
            ), 500

    return render_template("facturas/editar.html", fact=fact, form=form, errores=errores)


@facturas_bp.route("/facturas/<int:id_factura>/confirmar-anulacion", methods=["GET"])
@requiere_login
@requiere_permiso("facturas.anular")
def confirmar_anulacion(id_factura: int):
    """Paso 1 del 2-step undo: muestra el resumen + pide motivo antes de anular."""
    fact = queries.por_id(id_factura)
    if not fact:
        abort(404)
    if fact.get("stat") == "Y":
        flash("La factura ya está anulada.", "warn")
        return redirect(url_for("facturas.detalle", id_factura=id_factura))
    detalle = {
        "N° factura": fact.get("numf_completo") or fact.get("numf"),
        "Fecha": (fact.get("fecha").strftime("%d/%m/%Y") if fact.get("fecha") else "—"),
        "Cliente": f"{fact.get('codigo_cli', '')} — {fact.get('cliente') or ''}",
        "Importe": f"$ {fact.get('importe') or 0}",
        "Saldo actual": f"$ {fact.get('saldo') or 0}",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Anular factura {fact.get('numf_completo') or fact.get('numf')}",
        mensaje=(
            f"Vas a anular la factura {fact.get('numf_completo') or fact.get('numf')} "
            f"del cliente {fact.get('codigo_cli')} por $ {fact.get('importe') or 0}."
        ),
        detalle_registro=detalle,
        accion_url=url_for("facturas.anular", id_factura=id_factura),
        volver_url=url_for("facturas.detalle", id_factura=id_factura),
        motivo_requerido=True,
        confirm_label="Confirmar anulación",
    )


@facturas_bp.route("/facturas/<int:id_factura>/anular", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.anular")
def anular(id_factura: int):
    motivo = (request.form.get("motivo") or "").strip()
    # Motivo opcional — la dueña puede dejarlo vacío (ej. "error de carga"
    # implícito). TMT 2026-05-13.
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.anular(id_factura, motivo=motivo, usuario=usuario)
        flash(f"Factura {id_factura} anulada.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude anular", e)
    return redirect(url_for("facturas.detalle", id_factura=id_factura))


@facturas_bp.route("/facturas/<int:id_factura>")
@requiere_login
@requiere_permiso("facturas.ver")
def detalle(id_factura: int):
    fact = queries.por_id(id_factura)
    if not fact:
        abort(404)
    aplicaciones = queries.cheques_aplicados(id_factura)
    retenciones = queries.retenciones_aplicadas(fact["codigo_cli"], fact["numf"])
    total_aplicado = sum(float(a["aplicado"] or 0) for a in aplicaciones)
    total_retenido = sum(float(r["rete"] or 0) for r in retenciones)
    # Recientes — best-effort, no rompe el detalle si falla.
    try:
        from modules.recientes import queries as rec
        rec.registrar(
            "factura", id_factura,
            etiqueta=f"Factura {fact.get('numf_completo') or fact.get('numf')} · {fact.get('cliente') or fact.get('codigo_cli','')}",
        )
    except Exception:
        pass
    return render_template(
        "facturas/detalle.html",
        fact=fact,
        aplicaciones=aplicaciones,
        retenciones=retenciones,
        total_aplicado=total_aplicado,
        total_retenido=total_retenido,
    )


@facturas_bp.route("/facturas")
@requiere_login
@requiere_permiso("facturas.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    cliente = request.args.get("cliente", "").strip()
    def _parse_num(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return float(str(s).replace(",", "."))
        except ValueError:
            return None
    monto_min = _parse_num(request.args.get("monto_min"))
    monto_max = _parse_num(request.args.get("monto_max"))
    solo_abiertas = request.args.get("abiertas") == "1"
    # Vista canónica:
    #  - cartera (Z+A vivas)
    #  - estado (= antes "todas"; muestra todo el universo, filtrable por ?estado=)
    #  - canceladas (T)
    #  - eliminadas (X, Y legacy)
    # TMT 2026-05-19 (pedido dueña): default ahora es 'cartera' (no 'todas').
    # 'todas' renombrado a 'estado' con un filtro dropdown adentro.
    vista = (request.args.get("vista") or "cartera").lower()
    # Back-compat: si todavía llega ?vista=todas, lo mapeamos a 'estado'.
    if vista == "todas":
        vista = "estado"
    if vista not in ("cartera", "estado", "canceladas", "eliminadas"):
        vista = "cartera"
    # Filtro de estado (solo aplica en vista='estado'). Acepta los stats
    # canónicos: Z (cartera), A (parcial), T (cancelada), X/Y (eliminada).
    # TMT 2026-05-19 v8 — pedido dueña: permitir filtrar por VARIOS estados
    # a la vez. ?estado=Z&estado=A → checkboxes. Lista vacía = todos.
    # Back-compat: `?estado=Z` solo (legacy) sigue funcionando porque
    # getlist captura el valor único como lista de 1.
    estados_raw = request.args.getlist("estado")
    estados_filtro = [
        s.upper().strip() for s in estados_raw
        if s and s.upper().strip() in ("Z", "A", "T", "X", "Y")
    ]
    # De-dup preservando orden — útil si el form reenvía duplicados.
    seen: set[str] = set()
    estados_filtro = [s for s in estados_filtro if not (s in seen or seen.add(s))]
    # Compat con el flag scalar viejo (templates / código externo que
    # consume `estado`).
    estado_filtro = estados_filtro[0] if len(estados_filtro) == 1 else ""
    # Por default mostrar TODAS las facturas (sin tope de 500).
    # Si en el futuro la base crece a > 100k filas y se vuelve lento,
    # se puede pasar `?limite=500` para acotar. Pedido TMT 2026-05-14.
    try:
        limite = int(request.args.get("limite") or 100000)
    except (TypeError, ValueError):
        limite = 100000
    try:
        filas = queries.buscar(
            q, desde, hasta, solo_abiertas,
            vista=vista, limite=limite,
            cliente=cliente, monto_min=monto_min, monto_max=monto_max,
            estado=estado_filtro,
            estados=estados_filtro,
        )
        conteos = queries.conteos_por_vista()
        error = None
    except Exception as e:
        filas, conteos, error = [], {}, str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("numf", "N° Factura"),
                ("fecha", "Fecha"),
                ("codigo_cli", "Cliente"),
                ("cliente", "Nombre"),
                ("kg", "Kg"),
                ("importe", "Importe"),
                ("abono", "Abono"),
                ("saldo", "Saldo"),
                ("stat", "Stat"),
            ],
            filename=f"facturas_{vista}.csv",
        )

    total_importe = sum(float(r["importe"] or 0) for r in filas)
    total_saldo   = sum(float(r["saldo"]   or 0) for r in filas)
    return render_template(
        "facturas/lista.html",
        filas=filas, q=q, desde=desde, hasta=hasta,
        cliente=cliente, monto_min=monto_min, monto_max=monto_max,
        solo_abiertas=solo_abiertas,
        vista=vista, conteos=conteos,
        estado=estado_filtro,
        estados=estados_filtro,
        total_importe=total_importe, total_saldo=total_saldo,
        error=error,
    )


# =====================================================================
# Carga masiva CSV — batch 13. Mismas columnas que crear() / ALTAS.PRG.
# =====================================================================

FACTURAS_CSV_COLS = [
    # (campo, header legible, required)
    ("fecha",          "Fecha",         True),
    ("codigo_cli",     "Código cliente", True),
    ("kg",             "Kg",            True),
    ("importe",        "Importe",       True),
    ("numf",           "N° factura",    False),
    ("vencimiento",    "Vencimiento",   False),
    ("numf_completo",  "N° completo",   False),
    ("tipo",           "Tipo",          False),
    ("condic",         "Condición",     False),
    ("clave",          "Clave",         False),
]


@facturas_bp.route("/facturas/cargar-csv", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def cargar_csv():
    """Subir CSV con múltiples facturas. Mismos campos que ALTAS.PRG.

    GET con ?plantilla=1 devuelve un CSV vacío con los headers.
    POST procesa el archivo y muestra el reporte per-fila.
    """
    from csv_upload import plantilla_csv, procesar_csv

    if request.args.get("plantilla") == "1":
        csv_str = plantilla_csv(FACTURAS_CSV_COLS)
        resp = _plain_csv_response(csv_str, "plantilla_facturas.csv")
        return resp

    if request.method == "POST":
        f = request.files.get("archivo")
        if not f or not f.filename:
            flash("Subí un archivo CSV.", "warn")
            return redirect(url_for("facturas.cargar_csv"))
        raw = f.read()
        from . import queries as q_facturas
        result = procesar_csv(
            raw, FACTURAS_CSV_COLS, q_facturas.crear,
            usuario=(g.user or {}).get("username", "web"),
        )
        tono = "ok" if result.error == 0 else "warn"
        flash(f"Procesadas {result.total} filas — {result.ok} ok, {result.error} con error.", tono)
        return render_template(
            "facturas/cargar_csv_resultado.html",
            result=result, cols=FACTURAS_CSV_COLS,
        )
    return render_template("facturas/cargar_csv.html", cols=FACTURAS_CSV_COLS)


def _plain_csv_response(csv_str: str, filename: str):
    from flask import Response
    resp = Response("\ufeff" + csv_str, mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
