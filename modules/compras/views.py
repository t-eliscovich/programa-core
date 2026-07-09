"""Listado y alta de compras (facturas de proveedor)."""
from datetime import datetime

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
from parsers import parse_bool, parse_date, parse_int, parse_monto

from . import queries

compras_bp = Blueprint("compras", __name__, template_folder="templates")


def _bancos() -> list[dict]:
    try:
        return db.fetch_all("SELECT no_banco, nombre FROM scintela.banco ORDER BY no_banco")
    except Exception:
        return []


@compras_bp.route("/compras/nueva", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("compras.crear")
def nueva():
    """Alta de compra. Si no es contado (default), crea fila en posdat."""
    errores: list[str] = []
    form: dict = {}

    try:
        from modules.autocomplete.queries import proveedores_para_datalist
        proveedores_datalist = proveedores_para_datalist()
    except Exception:
        proveedores_datalist = []

    if request.method == "GET":
        form["numero_sugerido"] = queries.proximo_numero()
        form["fecha"] = datetime.now().date().strftime("%d/%m/%Y")
        # Restaurar campos via query string — si veníamos de crear un
        # proveedor nuevo, /proveedores/nuevo nos redirige con los datos
        # del form anterior en el query. TMT 2026-05-13.
        for k in ("fecha", "codigo_prov", "importe", "kg", "numero",
                  "tipo", "comprobante", "concepto", "fechad",
                  "no_banco", "cuenta", "pago_parcial"):
            if request.args.get(k):
                form[k] = request.args.get(k)
        for k in ("pagada", "es_anticipo_dolares"):
            if request.args.get(k):
                form[k] = request.args.get(k) in ("1", "true", "True", "on")
        return render_template("compras/nueva.html", form=form, errores=errores,
                               bancos=_bancos(), proveedores_datalist=proveedores_datalist)

    fecha = parse_date(request.form.get("fecha"))
    codigo_prov = (request.form.get("codigo_prov") or "").strip().upper()
    importe = parse_monto(request.form.get("importe"))
    kg = parse_monto(request.form.get("kg"))
    numero_raw = (request.form.get("numero") or "").strip()
    numero = int(numero_raw) if numero_raw.isdigit() else None
    tipo = (request.form.get("tipo") or "").strip()[:3] or None
    comprobante = (request.form.get("comprobante") or "").strip()[:100] or None
    concepto = (request.form.get("concepto") or "").strip()[:200] or None
    fechad = parse_date(request.form.get("fechad"))
    no_banco = parse_int(request.form.get("no_banco"))
    pagada = parse_bool(request.form.get("pagada"))
    cuenta = (request.form.get("cuenta") or "").strip().lower() or None
    pago_parcial = parse_monto(request.form.get("pago_parcial"))
    es_anticipo_dolares = parse_bool(request.form.get("es_anticipo_dolares"))

    if fecha is None:
        errores.append("Fecha inválida.")
    if not codigo_prov:
        errores.append("Código de proveedor requerido.")
    elif not db.fetch_one(
        "SELECT 1 AS x FROM scintela.proveedor WHERE codigo_prov = %s", (codigo_prov,)
    ):
        # Proveedor no existe → flujo guiado: mandamos al usuario a
        # /proveedores/nuevo con el código pre-cargado, y guardamos los
        # datos ya tipeados del form en el `next` URL para restaurar la
        # compra cuando el proveedor se cree. TMT 2026-05-13.
        # Mismo pattern que facturas → clientes.nuevo.
        _permisos = getattr(g, "permisos", set()) or set()
        if "proveedores.crear" in _permisos or "*" in _permisos:
            from urllib.parse import urlencode
            restore_args = {
                "fecha": request.form.get("fecha") or "",
                "codigo_prov": codigo_prov,
                "importe": request.form.get("importe") or "",
                "kg": request.form.get("kg") or "",
                "numero": numero_raw,
                "tipo": tipo or "",
                "comprobante": comprobante or "",
                "concepto": concepto or "",
                "fechad": request.form.get("fechad") or "",
                "no_banco": request.form.get("no_banco") or "",
                "cuenta": cuenta or "",
                "pago_parcial": request.form.get("pago_parcial") or "",
                "pagada": "1" if pagada else "",
                "es_anticipo_dolares": "1" if es_anticipo_dolares else "",
            }
            restore_args = {k: v for k, v in restore_args.items() if v}
            next_url = url_for("compras.nueva") + "?" + urlencode(restore_args)
            flash(
                f"El proveedor {codigo_prov} no existe — completá los datos "
                "para crearlo y después seguís con la compra.",
                "warning",
            )
            return redirect(
                url_for("proveedores.nuevo", codigo=codigo_prov, next=next_url)
            )
        # Sin permiso proveedores.crear, mantener el error clásico.
        errores.append(f"El proveedor {codigo_prov!r} no existe.")
    # TMT 2026-05-26 dueña: 'borra esta alerta, puede ser 0'. Compras
    # con importe 0 son válidas (muestras, regalos, compras pendientes
    # de facturar). Solo rechazamos None / inválido.
    if importe is None:
        errores.append("Importe inválido.")

    form.update({
        "fecha": request.form.get("fecha"),
        "codigo_prov": codigo_prov,
        "importe": request.form.get("importe"),
        "kg": request.form.get("kg"),
        "numero": numero_raw,
        "tipo": tipo or "",
        "comprobante": comprobante or "",
        "concepto": concepto or "",
        "fechad": request.form.get("fechad"),
        "no_banco": request.form.get("no_banco"),
        "pagada": pagada,
        "cuenta": cuenta or "",
        "pago_parcial": request.form.get("pago_parcial") or "",
        "es_anticipo_dolares": es_anticipo_dolares,
    })

    if errores:
        return render_template(
            "compras/nueva.html", form=form, errores=errores,
            bancos=_bancos(), proveedores_datalist=proveedores_datalist,
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:3].upper()
        c = queries.crear(
            fecha=fecha, codigo_prov=codigo_prov,
            importe=importe, kg=kg,
            tipo=tipo, comprobante=comprobante, numero=numero,
            concepto=concepto, fechad=fechad,
            no_banco=no_banco, clave=clave,
            pagada=pagada, cuenta=cuenta,
            pago_parcial=pago_parcial,
            es_anticipo_dolares=es_anticipo_dolares,
            usuario=usuario,
        )
        # Pista de stock — si la compra tiene kg, decir dónde se ven
        # acumulados (panel STOCK del balance) para que la dueña no tenga
        # que adivinar dónde se sumaron. TMT 2026-05-12.
        tipo_upper = (tipo or "").upper().strip()
        nombre_stock = {"H": "Hilado", "K": "Tejido",
                        "T": "Tintura", "Q": "Químicos"}.get(tipo_upper)
        sufijo_stock = ""
        if kg and float(kg) > 0 and nombre_stock:
            sufijo_stock = (
                f" Se sumaron {float(kg):.2f} kg al stock de {nombre_stock} — "
                f"verlo en /informes/balance (panel STOCK)."
            )
        if c.get("pago_parcial"):
            flash(
                f"Compra N° {c.get('numero')} registrada con pago parcial "
                f"de $ {c.get('pago_parcial'):.2f} (saldo $ {c.get('saldo_posdat'):.2f}).{sufijo_stock}",
                "ok",
            )
        else:
            flash(
                f"Compra N° {c.get('numero')} registrada.{sufijo_stock}",
                "ok",
            )
        return redirect(url_for("compras.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "compras/nueva.html", form=form, errores=errores,
            bancos=_bancos(), proveedores_datalist=proveedores_datalist,
        ), 400
    except Exception as e:
        # TMT 2026-05-14 (#37): no exponer detalle crudo de psycopg2 al
        # usuario. Mensaje humanizado + log al backend.
        import logging as _logging
        _logging.getLogger("programa_core.compras").exception(
            "compras.nueva falló"
        )
        errores.append(f"No pude registrar la compra: {humanize(e)}")
        return render_template(
            "compras/nueva.html", form=form, errores=errores,
            bancos=_bancos(), proveedores_datalist=proveedores_datalist,
        ), 500


@compras_bp.route("/compras/<int:id_compra>/confirmar-anulacion", methods=["GET"])
@requiere_login
@requiere_permiso("compras.anular")
def confirmar_anulacion(id_compra: int):
    """Paso 1 del 2-step: resumen + motivo antes de anular la compra."""
    c = queries.por_id(id_compra)
    if not c:
        abort(404)
    if (c.get("stat") or "").upper() == "Y":
        flash("La compra ya está anulada.", "warn")
        return redirect(url_for("compras.lista"))
    detalle = {
        "N° compra": c.get("numero") or c.get("id_compra"),
        "Fecha": (c.get("fecha").strftime("%d/%m/%Y") if c.get("fecha") else "—"),
        "Proveedor": f"{c.get('codigo_prov', '')} — {c.get('proveedor') or ''}",
        "Comprobante": c.get("comprobante") or "—",
        "Importe": f"$ {c.get('importe') or 0}",
        "Vencimiento": (c.get("fechad").strftime("%d/%m/%Y") if c.get("fechad") else "—"),
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Anular compra N° {c.get('numero') or id_compra}",
        mensaje=(
            f"Vas a anular la compra N° {c.get('numero') or id_compra} "
            f"del proveedor {c.get('codigo_prov')} por $ {c.get('importe') or 0}. "
            "La obligación de pago (posdat) asociada se borra junto con la compra."
        ),
        detalle_registro=detalle,
        accion_url=url_for("compras.anular", id_compra=id_compra),
        volver_url=url_for("compras.lista"),
        motivo_requerido=True,
        confirm_label="Confirmar anulación",
    )


@compras_bp.route("/compras/<int:id_compra>/anular", methods=["POST"])
@requiere_login
@requiere_permiso("compras.anular")
def anular(id_compra: int):
    motivo = (request.form.get("motivo") or "").strip()
    # Motivo opcional acá — la dueña puede dejarlo vacío si no aplica
    # (a diferencia del rebote de cheque, donde sí es crítico). TMT 2026-05-13.
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.anular(id_compra, motivo=motivo, usuario=usuario)
        flash(f"Compra {id_compra} anulada.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude anular", e)
    return redirect(url_for("compras.lista"))


@compras_bp.route("/compras/<int:id_compra>/convertir-anticipo", methods=["POST"])
@requiere_login
@requiere_permiso("compras.crear")
def convertir_anticipo(id_compra: int):
    """Convierte una compra tipo='A' (anticipo) a otro tipo (K/H/Q/C).

    POST form fields:
      - nuevo_tipo: K | H | Q | C
      - kg (opcional, sólo si nuevo_tipo='K' y queremos producción)
      - motivo (REQUERIDO — cambia la categoría de una compra ya pagada,
        queda en bitácora)
    """
    nuevo_tipo = (request.form.get("nuevo_tipo") or "").strip().upper()
    kg = parse_monto(request.form.get("kg"))
    motivo = (request.form.get("motivo") or "").strip()  # opcional. TMT 2026-05-13.
    try:
        usuario = (g.user or {}).get("username", "web")
        out = queries.convertir_anticipo(
            id_compra,
            nuevo_tipo=nuevo_tipo, kg=kg, motivo=motivo, usuario=usuario,
        )
        if out.get("es_produccion"):
            flash(
                f"Anticipo convertido a producción ({nuevo_tipo}, {kg} kg).",
                "ok",
            )
        else:
            flash(f"Anticipo convertido a tipo {nuevo_tipo}.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude convertir el anticipo", e)
    return redirect(request.referrer or url_for("compras.lista"))


@compras_bp.route("/compras/<int:id_compra>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("compras.editar")
def editar(id_compra: int):
    """Edición *blanda* de una compra. Para corregir importe/fechad de
    compra ya pagada → anular y reemitir."""
    c = queries.por_id(id_compra)
    if not c:
        abort(404)
    if (c.get("stat") or "").upper() == "Y":
        flash("Compra anulada — no se puede editar.", "warn")
        return redirect(url_for("compras.lista"))

    pagada = c.get("id_transaccion") is not None
    errores: list[str] = []
    form = {
        "concepto": c.get("concepto") or "",
        "comprobante": c.get("comprobante") or "",
        "fechad": c.get("fechad").strftime("%d/%m/%Y") if c.get("fechad") else "",
        "importe": str(c.get("importe") or 0),
        "observacion": "",
    }
    if request.method == "POST":
        concepto = (request.form.get("concepto") or "").strip()[:200] or None
        comprobante = (request.form.get("comprobante") or "").strip()[:100] or None
        fechad = parse_date(request.form.get("fechad"))
        importe_str = request.form.get("importe")
        importe = parse_monto(importe_str)
        observacion = (request.form.get("observacion") or "").strip() or None

        if importe is None:
            errores.append("Importe inválido.")

        form.update({
            "concepto": concepto or "",
            "comprobante": comprobante or "",
            "fechad": request.form.get("fechad") or "",
            "importe": importe_str,
            "observacion": observacion or "",
        })

        if errores:
            return render_template(
                "compras/editar.html",
                compra=c, form=form, errores=errores, pagada=pagada,
            ), 400

        try:
            usuario = (g.user or {}).get("username", "web")
            res = queries.editar(
                id_compra,
                concepto=concepto,
                comprobante=comprobante,
                fechad=fechad,
                importe=float(importe) if importe is not None else None,
                observacion=observacion,
                usuario=usuario,
            )
            flash(
                f"Compra editada — importe: $ {res['importe_nuevo']:,.2f} "
                f"({'pagada' if res['pagada'] else 'pendiente'}).",
                "ok",
            )
            return redirect(url_for("compras.lista"))
        except ValueError as e:
            errores.append(str(e))
            return render_template(
                "compras/editar.html",
                compra=c, form=form, errores=errores, pagada=pagada,
            ), 400

    return render_template(
        "compras/editar.html", compra=c, form=form, errores=errores, pagada=pagada,
    )


@compras_bp.route("/compras")
@requiere_login
@requiere_permiso("compras.ver")
def lista():
    """Pantalla Compras — todas las compras menos la producción de tejido."""
    return _pantalla_compras("compras", "Compras", "compras.lista")


@compras_bp.route("/produccion-tejeduria")
@requiere_login
@requiere_permiso("compras.ver")
def produccion_tejeduria():
    """Pantalla Producción Tejeduría — solo la producción de tejido."""
    return _pantalla_compras(
        "produccion", "Producción Tejeduría", "compras.produccion_tejeduria",
    )


def _pantalla_compras(vista, titulo, endpoint_actual):
    """Lógica compartida de Compras y Producción Tejeduría. La `vista` la
    fija la ruta (no el query string); `endpoint_actual` es el endpoint de
    esta misma pantalla, para que el form y los botones apunten acá.
    Federico 2026-05-22.
    """
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    tipo = (request.args.get("tipo") or "").strip().upper() or None
    incluir_anuladas = request.args.get("anuladas") == "1"
    # Federico 2026-05-22 — "Solo INTELA" (q=KK) muestra la producción
    # propia; no aplica en la vista Compras, que justamente excluye la
    # producción. Si se entra a Compras con ese filtro prendido, se apaga.
    if vista == "compras" and q.upper() == "KK":
        q = ""
    try:
        filas = queries.buscar(
            q, desde, hasta,
            incluir_anuladas=incluir_anuladas,
            vista=vista,
            tipo=tipo,
        )
        error = None
    except Exception as e:
        filas, error = [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"),
                ("codigo_prov", "Cód. prov"),
                ("proveedor", "Proveedor"),
                ("tipo", "Tipo"),
                ("comprobante", "Comprobante"),
                ("numero", "Número"),
                ("kg", "Kg"),
                ("importe", "Importe"),
                ("concepto", "Concepto"),
                ("banco", "Banco"),
                ("fechad", "F. dep."),
                ("stat", "Estado"),
            ],
            filename="compras.csv",
        )

    # TMT 2026-05-20 PASADA 6 Federico #15 — total real del filtro sin
    # LIMIT. Si las filas visibles == universo, no hay diferencia. Si
    # están truncadas a 500 (limit), el header sigue mostrando el total
    # real del filtro, no el sumatorio de las 500 visibles.
    try:
        agg = queries.total_buscar(
            q, desde, hasta,
            incluir_anuladas=incluir_anuladas, vista=vista,
            tipo=tipo,
        )
        total_importe = agg["total"]
        total_kg = agg["total_kg"]
    except Exception:
        total_importe = sum(float(r["importe"] or 0) for r in filas
                            if (r.get("stat") or "").upper() != "Y")
        total_kg = sum(float(r["kg"] or 0) for r in filas
                       if (r.get("stat") or "").upper() != "Y")
    # Federico 2026-05-22 - proveedores para el datalist (sugerencias)
    # del campo Proveedor del filtro de /compras.
    try:
        proveedores = queries.proveedores_para_filtro(vista)
    except Exception:
        proveedores = []

    return render_template(
        "compras/lista.html",
        filas=filas, q=q, desde=desde, hasta=hasta, tipo=tipo,
        total_importe=total_importe, total_kg=total_kg,
        incluir_anuladas=incluir_anuladas,
        vista=vista,
        titulo=titulo,
        endpoint_actual=endpoint_actual,
        proveedores=proveedores,
        error=error,
    )


# =====================================================================
# Carga masiva CSV — batch 13. Mismos campos que crear() / ALTAS.PRG.
# =====================================================================

COMPRAS_CSV_COLS = [
    ("fecha",       "Fecha",          True),
    ("codigo_prov", "Código proveedor", True),
    ("importe",     "Importe",        True),
    ("kg",          "Kg",             False),
    ("numero",      "N° compra",      False),
    ("comprobante", "N° comprobante", False),
    ("concepto",    "Concepto",       False),
    ("tipo",        "Tipo",           False),
    ("fechad",      "Vencimiento",    False),
    ("no_banco",    "N° banco",       False),
    ("clave",       "Clave",          False),
    ("pagada",      "Pagada",         False),
]


@compras_bp.route("/compras/cargar-csv", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("compras.crear")
def cargar_csv():
    from csv_upload import plantilla_csv, procesar_csv

    if request.args.get("plantilla") == "1":
        from flask import Response
        csv_str = plantilla_csv(COMPRAS_CSV_COLS)
        resp = Response("\ufeff" + csv_str, mimetype="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = 'attachment; filename="plantilla_compras.csv"'
        return resp

    if request.method == "POST":
        f = request.files.get("archivo")
        if not f or not f.filename:
            flash("Subí un archivo CSV.", "warn")
            return redirect(url_for("compras.cargar_csv"))
        raw = f.read()
        result = procesar_csv(
            raw, COMPRAS_CSV_COLS, queries.crear,
            usuario=(g.user or {}).get("username", "web"),
        )
        tono = "ok" if result.error == 0 else "warn"
        flash(f"Procesadas {result.total} filas — {result.ok} ok, {result.error} con error.", tono)
        return render_template(
            "compras/cargar_csv_resultado.html",
            result=result, cols=COMPRAS_CSV_COLS,
        )
    return render_template("compras/cargar_csv.html", cols=COMPRAS_CSV_COLS)
