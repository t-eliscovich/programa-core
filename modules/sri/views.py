"""Rutas para facturación electrónica SRI.

Scope MVP:
    - POST /sri/factura/<id_factura>/generar  — arma el XML y crea
      factura_electronica en estado borrador. NO firma ni envía todavía.
    - GET  /sri/factura_electronica/<id>      — detalle del comprobante SRI.
    - GET  /sri/factura_electronica/<id>/xml  — download/preview del XML crudo.

La firma (.p12) y el envío SOAP al SRI están stubbeados en modules/sri/
firma.py y envio.py. Cuando se implementen, se agregan acá las rutas
/firmar y /enviar.
"""
from __future__ import annotations

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from config.emisor import (
    get_ambiente_default,
    get_emisor,
    get_estab,
    get_pto_emi,
)
from error_messages import flash_exc
from modules.facturas import queries as facturas_q
from modules.sri import queries as sri_q
from modules.sri.core import generar_clave_acceso
from modules.sri.xml import (
    Comprador,
    DetalleLinea,
    InfoFactura,
    Pago,
    calcular_totales,
    construir_xml_factura,
    validar_estructura_factura,
)

sri_bp = Blueprint("sri", __name__, template_folder="templates")


# =====================================================================
# Helpers: construir el contrato InfoFactura desde la DB
# =====================================================================

def _tipo_identificacion_de_cliente(ruc: str | None) -> str:
    """Heurística corta para elegir el código SRI de identificación.

    13 dígitos → RUC ("04"), 10 → cédula ("05"), otro → consumidor final ("07").
    Es un fallback razonable para el MVP; cuando se pida rigor, agregar un
    campo `tipo_identificacion` a scintela.cliente.
    """
    s = "".join(c for c in (ruc or "") if c.isdigit())
    if len(s) == 13:
        return "04"
    if len(s) == 10:
        return "05"
    return "07"


def _construir_info_factura(fact: dict) -> InfoFactura:
    """A partir de la fila de scintela.factura + cliente, arma InfoFactura.

    Por ahora representamos toda la factura como UNA línea ("Tela varios kg")
    porque el modelo legacy no tiene items detallados. Cuando se agregue
    scintela.factura_detalle (no en el scope de hoy), reemplazar esta
    función con una que lea las líneas reales.
    """
    emisor = get_emisor()

    ruc_cli = (fact.get("ruc") or "").strip()
    tipo_id = _tipo_identificacion_de_cliente(ruc_cli)
    comprador = Comprador(
        identificacion=ruc_cli or "9999999999999",
        tipo_identificacion=tipo_id,
        razon_social=(fact.get("cliente") or "CONSUMIDOR FINAL").strip(),
        direccion=None,
        telefono=(fact.get("telefono") or None),
    )

    # Importe almacenado = precio con IVA incluido (convención Intela).
    # Desglosamos para el XML: base = total / (1 + iva/100); IVA = total - base.
    importe_total = float(fact.get("importe") or 0)
    iva_pct = 15.0  # default SRI 2024+
    base = round(importe_total / (1 + iva_pct / 100), 2)
    precio_unit = base  # una sola línea, cantidad = kg
    kg = float(fact.get("kg") or 0)
    if kg > 0:
        precio_unit = round(base / kg, 6)
    else:
        # Sin kg declarado, modelamos como "1 servicio de $base".
        kg = 1.0

    linea = DetalleLinea(
        codigo_principal="TELA",
        descripcion="Tela (kg)",
        cantidad=kg,
        precio_unitario=precio_unit,
        iva_porcentaje=iva_pct,
    )

    # Pago único, crédito si hay vencimiento posterior a fecha.
    pagos = []
    venci = fact.get("vencimiento")
    fecha_emi = fact.get("fecha")
    plazo_dias = None
    if venci and fecha_emi:
        try:
            plazo_dias = max(0, (venci - fecha_emi).days)
        except TypeError:
            plazo_dias = None
    pagos.append(Pago(total=importe_total, plazo=plazo_dias))

    return InfoFactura(
        fecha_emision=fecha_emi,
        emisor=emisor,
        comprador=comprador,
        detalles=[linea],
        pagos=pagos,
    )


# =====================================================================
# Rutas
# =====================================================================

@sri_bp.route("/sri/factura/<int:id_factura>/generar", methods=["POST"])
@requiere_login
@requiere_permiso("sri.emitir")
def generar(id_factura: int):
    """Genera el XML SRI de una factura y lo persiste como borrador.

    No firma. No envía. Devuelve redirect a la vista del comprobante.
    """
    fact = facturas_q.por_id(id_factura)
    if not fact:
        abort(404)

    # Una factura anulada no se debe emitir electrónicamente.
    if (fact.get("stat") or "").upper() == "Y":
        flash("No se puede generar XML SRI: la factura está anulada.", "warn")
        return redirect(url_for("facturas.detalle", id_factura=id_factura))

    try:
        info = _construir_info_factura(fact)
    except Exception as e:
        flash_exc("No pude armar el XML", e)
        return redirect(url_for("facturas.detalle", id_factura=id_factura))

    ambiente = get_ambiente_default()
    estab = get_estab()
    pto_emi = get_pto_emi()
    tipo_comprobante = "01"  # factura
    tipo_emision = "1"       # normal

    # Secuencial: próximo disponible en nuestra tabla para ese establecimiento.
    secuencial_int = sri_q.proximo_secuencial(estab, pto_emi, tipo_comprobante)
    secuencial_str = str(secuencial_int).zfill(9)

    try:
        clave = generar_clave_acceso(
            fecha_emision=info.fecha_emision,
            tipo_comprobante=tipo_comprobante,
            ruc=info.emisor.ruc,
            ambiente=ambiente,
            estab=estab,
            pto_emi=pto_emi,
            secuencial=secuencial_int,
            tipo_emision=tipo_emision,
        )
    except ValueError as e:
        flash_exc("No pude generar la clave de acceso", e)
        return redirect(url_for("facturas.detalle", id_factura=id_factura))

    xml_str = construir_xml_factura(
        clave_acceso=clave,
        ambiente=ambiente,
        tipo_emision=tipo_emision,
        estab=estab,
        pto_emi=pto_emi,
        secuencial=secuencial_str,
        info=info,
    )

    errores_estructurales = validar_estructura_factura(xml_str)
    if errores_estructurales:
        flash(
            "XML generado pero con errores de estructura: "
            + "; ".join(errores_estructurales[:3]),
            "warn",
        )

    totales = calcular_totales(info)

    try:
        usuario = (g.user or {}).get("username", "web")
        registro = sri_q.crear_borrador(
            id_factura=id_factura,
            clave_acceso=clave,
            ambiente=ambiente,
            estab=estab,
            pto_emi=pto_emi,
            secuencial=secuencial_str,
            fecha_emision=info.fecha_emision,
            totales=totales,
            xml_generado=xml_str,
            tipo_comprobante=tipo_comprobante,
            tipo_emision=tipo_emision,
            usuario=usuario,
        )
    except ValueError as e:
        flash(str(e), "warn")
        return redirect(url_for("facturas.detalle", id_factura=id_factura))
    except Exception as e:
        flash_exc("No pude guardar el comprobante SRI", e)
        return redirect(url_for("facturas.detalle", id_factura=id_factura))

    flash(
        f"XML SRI generado (clave {clave[:8]}…{clave[-4:]}). "
        "Estado: borrador. Falta firmar y enviar.",
        "ok",
    )
    return redirect(url_for(
        "sri.detalle_comprobante",
        id_factura_electronica=registro["id_factura_electronica"],
    ))


@sri_bp.route("/sri/factura_electronica/<int:id_factura_electronica>")
@requiere_login
@requiere_permiso("sri.ver")
def detalle_comprobante(id_factura_electronica: int):
    """Detalle del comprobante electrónico: clave, estado, totales, link al XML."""
    fe = sri_q.por_id(id_factura_electronica)
    if not fe:
        abort(404)
    fact = facturas_q.por_id(fe["id_factura"]) if fe.get("id_factura") else None
    errores_estructurales = validar_estructura_factura(fe.get("xml_generado") or "")
    return render_template(
        "sri/detalle.html",
        fe=fe,
        fact=fact,
        errores_estructurales=errores_estructurales,
    )


@sri_bp.route("/sri/factura_electronica/<int:id_factura_electronica>/xml")
@requiere_login
@requiere_permiso("sri.ver")
def xml_raw(id_factura_electronica: int):
    """Devuelve el XML crudo — útil para inspección humana y debugging SRI."""
    xml = sri_q.obtener_xml(id_factura_electronica)
    if xml is None:
        abort(404)
    # text/xml para que el browser lo muestre formateado; inline con nombre
    # sugerido para que "Guardar como" ponga algo razonable.
    resp = Response(xml, mimetype="application/xml; charset=utf-8")
    resp.headers["Content-Disposition"] = (
        f'inline; filename="factura_sri_{id_factura_electronica}.xml"'
    )
    return resp


# =====================================================================
# Ciclo de vida: firmar, enviar, consultar autorización
# =====================================================================

@sri_bp.route("/sri/factura_electronica/<int:id_factura_electronica>/firmar", methods=["POST"])
@requiere_login
@requiere_permiso("sri.emitir")
def firmar(id_factura_electronica: int):
    """Firma el XML con el .p12 configurado (SRI_P12_PATH/SRI_P12_PASSWORD).

    Sin .p12 configurado, flashea un error claro. Cuando haya .p12, esta
    ruta ya queda funcional sin cambios en views.py — es todo env vars.
    """
    from modules.sri import firma as sri_firma

    fe = sri_q.por_id(id_factura_electronica)
    if not fe:
        abort(404)
    if fe.get("estado") not in ("borrador", "rechazado"):
        flash("Este comprobante no está en estado firmable.", "warn")
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    xml = fe.get("xml_generado") or ""
    if not xml:
        flash("El comprobante no tiene XML generado todavía.", "error")
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    try:
        xml_firmado = sri_firma.firmar_xml_de_env(xml)
    except sri_firma.FirmaNoConfiguradaError as e:
        flash_exc("Firma no configurada", e)
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))
    except sri_firma.FirmaFalloError as e:
        flash_exc("La firma falló", e)
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    sri_q.guardar_firma(
        id_fe=id_factura_electronica,
        xml_firmado=xml_firmado,
        usuario=g.user["username"] if g.get("user") else "web",
    )
    flash("Comprobante firmado correctamente.", "ok")
    return redirect(url_for("sri.detalle_comprobante",
                            id_factura_electronica=id_factura_electronica))


@sri_bp.route("/sri/factura_electronica/<int:id_factura_electronica>/enviar", methods=["POST"])
@requiere_login
@requiere_permiso("sri.emitir")
def enviar(id_factura_electronica: int):
    """POST del XML firmado al SRI (recepción + autorización en una sola request).

    Flow:
        1. Validar que está firmado.
        2. POST a /recepcion.
        3. Si RECIBIDA, esperar 2-3s y GET /autorizacion.
        4. Actualizar estado según respuesta.
    """
    import time as _t

    from modules.sri import envio as sri_envio

    fe = sri_q.por_id(id_factura_electronica)
    if not fe:
        abort(404)
    if not fe.get("xml_firmado"):
        flash("Primero hay que firmar el comprobante.", "warn")
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    ambiente = fe.get("ambiente") or "1"
    usuario = g.user["username"] if g.get("user") else "web"

    sri_q.marcar_enviado(id_fe=id_factura_electronica, usuario=usuario)

    try:
        resp_rec = sri_envio.enviar_a_recepcion(
            xml_firmado=fe["xml_firmado"], ambiente=ambiente,
        )
    except (sri_envio.EnvioNoConfiguradoError, sri_envio.EnvioFalloError) as e:
        flash_exc("Error en Recepción SRI", e)
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    sri_q.actualizar_respuesta_recepcion(
        id_fe=id_factura_electronica, respuesta=resp_rec, usuario=usuario,
    )

    if resp_rec.get("estado") != "RECIBIDA":
        flash("El SRI devolvió el comprobante. Ver detalle del error.", "warn")
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    # Esperar a que SRI procese (típicamente 1-3s).
    _t.sleep(3)

    try:
        resp_aut = sri_envio.consultar_autorizacion(
            clave_acceso=fe["clave_acceso"], ambiente=ambiente,
        )
    except (sri_envio.EnvioNoConfiguradoError, sri_envio.EnvioFalloError) as e:
        # Mensaje en 2 partes: humanize + sufijo de acción que el user puede tomar.
        from error_messages import humanize as _hum
        flash(
            f"Error en Autorización SRI: {_hum(e)} Reintentá con el botón 'Consultar'.",
            "warn",
        )
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    sri_q.actualizar_respuesta_autorizacion(
        id_fe=id_factura_electronica, respuesta=resp_aut, usuario=usuario,
    )

    estado = resp_aut.get("estado")
    if estado == "AUTORIZADO":
        flash("¡Comprobante autorizado por el SRI!", "ok")
    elif estado == "EN PROCESO":
        flash("Aún EN PROCESO. Reintentá con el botón 'Consultar' en unos minutos.", "warn")
    else:
        flash(f"SRI respondió: {estado}. Ver detalle del error.", "error")

    return redirect(url_for("sri.detalle_comprobante",
                            id_factura_electronica=id_factura_electronica))


@sri_bp.route("/sri/factura_electronica/<int:id_factura_electronica>/consultar", methods=["POST"])
@requiere_login
@requiere_permiso("sri.emitir")
def consultar(id_factura_electronica: int):
    """Re-consulta la autorización. Útil cuando enviar() quedó EN PROCESO."""
    from modules.sri import envio as sri_envio

    fe = sri_q.por_id(id_factura_electronica)
    if not fe:
        abort(404)
    if fe.get("estado") not in ("enviado", "rechazado"):
        flash("Sólo se puede consultar un comprobante que fue enviado al SRI.", "warn")
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    try:
        resp = sri_envio.consultar_autorizacion(
            clave_acceso=fe["clave_acceso"],
            ambiente=fe.get("ambiente") or "1",
        )
    except (sri_envio.EnvioNoConfiguradoError, sri_envio.EnvioFalloError) as e:
        flash_exc("Error consultando al SRI", e)
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    usuario = g.user["username"] if g.get("user") else "web"
    sri_q.actualizar_respuesta_autorizacion(
        id_fe=id_factura_electronica, respuesta=resp, usuario=usuario,
    )
    flash(f"SRI respondió: {resp.get('estado')}", "ok" if resp.get("estado") == "AUTORIZADO" else "warn")
    return redirect(url_for("sri.detalle_comprobante",
                            id_factura_electronica=id_factura_electronica))


# =====================================================================
# Notas de crédito
# =====================================================================

@sri_bp.route("/sri/factura_electronica/<int:id_factura_electronica>/nota-credito",
              methods=["GET"])
@requiere_login
@requiere_permiso("sri.emitir")
def nc_form(id_factura_electronica: int):
    """Formulario para emitir una nota de crédito contra una factura autorizada.

    Regla Ecuador: NC sólo contra facturas YA AUTORIZADAS. Una factura en
    borrador/firmado/rechazado NO genera NC (se regenera/re-envía).
    """
    fe = sri_q.por_id(id_factura_electronica)
    if not fe:
        abort(404)
    if fe.get("estado") != "autorizado":
        flash("Sólo se puede emitir NC contra una factura autorizada por el SRI.", "warn")
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))
    fact = facturas_q.por_id(fe["id_factura"])
    return render_template("sri/nc_form.html", fe=fe, fact=fact)


@sri_bp.route("/sri/factura_electronica/<int:id_factura_electronica>/nota-credito",
              methods=["POST"])
@requiere_login
@requiere_permiso("sri.emitir")
def nc_generar(id_factura_electronica: int):
    """POST del form → genera el XML de NC, lo persiste en borrador.

    Inputs del form:
        - motivo (requerido)
        - valor_modificacion (requerido, opcional si la NC anula todo)
    """
    from datetime import date as _date

    from modules.sri.xml import Comprador, DetalleLinea
    from modules.sri.xml_nota_credito import (
        InfoNotaCredito,
        calcular_totales_nc,
        construir_xml_nota_credito,
        validar_estructura_nota_credito,
    )

    fe = sri_q.por_id(id_factura_electronica)
    if not fe:
        abort(404)
    if fe.get("estado") != "autorizado":
        flash("Sólo se puede emitir NC contra una factura autorizada.", "warn")
        return redirect(url_for("sri.detalle_comprobante",
                                id_factura_electronica=id_factura_electronica))

    fact = facturas_q.por_id(fe["id_factura"])
    if not fact:
        abort(404)

    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("El motivo es obligatorio para emitir una NC.", "error")
        return redirect(url_for("sri.nc_form",
                                id_factura_electronica=id_factura_electronica))

    try:
        valor_mod = float((request.form.get("valor_modificacion") or "").replace(",", "."))
    except ValueError:
        flash("Valor de modificación inválido.", "error")
        return redirect(url_for("sri.nc_form",
                                id_factura_electronica=id_factura_electronica))

    if valor_mod <= 0:
        flash("El valor de modificación debe ser mayor a cero.", "error")
        return redirect(url_for("sri.nc_form",
                                id_factura_electronica=id_factura_electronica))

    # Armar InfoNotaCredito reusando Emisor + Comprador de la factura original.
    emisor_obj = get_emisor()
    iva_pct = float(fe.get("iva_porcentaje") or 15.0)

    # Nota de crédito simplificada: una sola línea "REFERENCIA" por el valor
    # de modificación solicitado. El SRI acepta eso; el monto iva se recalcula.
    base_sin_iva = round(valor_mod / (1 + iva_pct / 100.0), 2)
    unit = base_sin_iva  # cantidad=1
    detalle = DetalleLinea(
        codigo_principal=fact.get("numf") or "REF",
        descripcion=f"NC s/ factura {fact.get('numf_completo') or fact.get('numf')}",
        cantidad=1.0,
        precio_unitario=unit,
        descuento=0.0,
        iva_porcentaje=iva_pct,
    )

    info = InfoNotaCredito(
        emisor=emisor_obj,
        comprador=Comprador(
            tipo_identificacion=_tipo_identificacion_de_cliente(fact.get("ruc")),
            razon_social=fact.get("cliente") or "CONSUMIDOR FINAL",
            identificacion=fact.get("ruc") or "9999999999999",
            direccion=fact.get("direccion") or "",
            email=fact.get("email") or "",
            telefono=fact.get("telefono") or "",
        ),
        fecha_emision=_date.today(),
        cod_doc_modificado="01",
        num_doc_modificado=fact.get("numf_completo") or str(fact.get("numf") or ""),
        fecha_emision_doc_sustento=fe.get("fecha_emision") or _date.today(),
        motivo=motivo,
        detalles=[detalle],
    )

    ambiente = get_ambiente_default()
    estab = get_estab()
    pto_emi = get_pto_emi()
    secuencial_int = sri_q.proximo_secuencial(estab, pto_emi, "04")
    secuencial_str = str(secuencial_int).zfill(9)

    try:
        clave = generar_clave_acceso(
            fecha_emision=info.fecha_emision,
            tipo_comprobante="04",
            ruc=info.emisor.ruc,
            ambiente=ambiente,
            estab=estab,
            pto_emi=pto_emi,
            secuencial=secuencial_int,
            tipo_emision="1",
        )
    except ValueError as e:
        flash_exc("No pude generar la clave de acceso", e)
        return redirect(url_for("sri.nc_form",
                                id_factura_electronica=id_factura_electronica))

    xml_str = construir_xml_nota_credito(
        clave_acceso=clave,
        ambiente=ambiente,
        tipo_emision="1",
        estab=estab,
        pto_emi=pto_emi,
        secuencial=secuencial_str,
        info=info,
    )

    errs = validar_estructura_nota_credito(xml_str)
    if errs:
        flash("XML generado pero con errores: " + "; ".join(errs[:3]), "warn")

    totales = calcular_totales_nc(info)
    totales_db = {
        "subtotal_sin_impuestos": totales["subtotal_sin_impuestos"],
        "iva_porcentaje": iva_pct,
        "total_iva": totales["total_iva"],
        "valor_modificacion": totales["valor_modificacion"],
    }

    try:
        usuario = (g.user or {}).get("username", "web")
        res = sri_q.crear_borrador_nota_credito(
            id_factura_origen=fe["id_factura"],
            clave_acceso=clave,
            ambiente=ambiente,
            estab=estab,
            pto_emi=pto_emi,
            secuencial=secuencial_str,
            fecha_emision=info.fecha_emision,
            totales=totales_db,
            xml_generado=xml_str,
            num_doc_modificado=info.num_doc_modificado,
            fecha_emision_doc_sustento=info.fecha_emision_doc_sustento,
            motivo=motivo,
            usuario=usuario,
        )
    except ValueError as e:
        flash(str(e), "warn")
        return redirect(url_for("sri.nc_form",
                                id_factura_electronica=id_factura_electronica))
    except Exception as e:
        flash_exc("No pude guardar la NC", e)
        return redirect(url_for("sri.nc_form",
                                id_factura_electronica=id_factura_electronica))

    flash(f"Nota de crédito generada en borrador. Clave {clave[:8]}…{clave[-4:]}", "ok")
    return redirect(url_for("sri.detalle_comprobante",
                            id_factura_electronica=res.get("id_factura_electronica", 0)))
