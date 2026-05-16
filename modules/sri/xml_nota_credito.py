"""Generador XML para nota de crédito SRI — formato v1.1.0.

Escenario típico: una factura se autorizó al SRI y después hay que revertirla
(devolución, error del monto, descuento post-facto). La factura autorizada
NO se edita — se emite una nota de crédito contra ella.

La NC requiere:
    - Tipo de comprobante '04' en la clave de acceso.
    - `<docModificado>` + `<numDocModificado>` + `<fechaEmisionDocSustento>`
      referenciando la factura original.
    - `<motivo>` — texto legible de la razón.
    - Estructura casi idéntica a factura pero con `<notaCredito>` como root
      y `<infoNotaCredito>` en lugar de `<infoFactura>`.

Los helpers `Emisor`, `Comprador`, `DetalleLinea`, `Pago`, y
`cod_iva_por_porcentaje` vienen de `modules.sri.xml` — NC los reusa.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date

from modules.sri.core import fmt_cantidad, fmt_monto
from modules.sri.xml import (
    COD_IMPUESTO_IVA,
    Comprador,
    DetalleLinea,
    Emisor,
    cod_iva_por_porcentaje,
)

# =====================================================================
# Contrato
# =====================================================================

@dataclass
class InfoNotaCredito:
    """Data de nota de crédito. Similar a InfoFactura + refs al doc original."""
    emisor: Emisor
    comprador: Comprador
    fecha_emision: date

    # Referencia al documento que se modifica (normalmente una factura):
    cod_doc_modificado: str               # '01' factura, '04' otra NC, etc.
    num_doc_modificado: str               # "001-001-000000123" (ESTAB-PTOEMI-SECUENCIAL)
    fecha_emision_doc_sustento: date      # fecha de la factura original
    motivo: str                           # razón de la NC (texto libre)

    # Desglose
    detalles: list[DetalleLinea] = field(default_factory=list)
    valor_modificacion: float = 0.0       # total de la NC (suma de detalles)
    moneda: str = "DOLAR"
    info_adicional: dict[str, str] = field(default_factory=dict)


# =====================================================================
# Cálculos
# =====================================================================

def _subtotal_linea(linea: DetalleLinea) -> float:
    return round(linea.cantidad * linea.precio_unitario - linea.descuento, 2)


def _iva_linea(linea: DetalleLinea) -> float:
    return round(_subtotal_linea(linea) * (linea.iva_porcentaje / 100.0), 2)


def calcular_totales_nc(info: InfoNotaCredito) -> dict:
    """Agrupa subtotales por porcentaje de IVA. Mismo pattern que factura."""
    por_iva: dict[float, dict] = {}
    subtotal_sin_impuestos = 0.0
    total_descuento = 0.0

    for linea in info.detalles:
        sub = _subtotal_linea(linea)
        iva = _iva_linea(linea)
        subtotal_sin_impuestos += sub
        total_descuento += linea.descuento
        key = float(linea.iva_porcentaje)
        if key not in por_iva:
            por_iva[key] = {
                "codigo_iva": cod_iva_por_porcentaje(linea.iva_porcentaje),
                "base_imponible": 0.0,
                "valor": 0.0,
            }
        por_iva[key]["base_imponible"] += sub
        por_iva[key]["valor"] += iva

    por_iva_sorted = sorted(por_iva.values(), key=lambda b: b["codigo_iva"])
    for b in por_iva_sorted:
        b["base_imponible"] = round(b["base_imponible"], 2)
        b["valor"] = round(b["valor"], 2)

    total_iva = sum(b["valor"] for b in por_iva_sorted)
    valor_modificacion = round(subtotal_sin_impuestos + total_iva, 2)

    return {
        "subtotal_sin_impuestos": round(subtotal_sin_impuestos, 2),
        "total_descuento": round(total_descuento, 2),
        "por_iva": por_iva_sorted,
        "total_iva": round(total_iva, 2),
        "valor_modificacion": valor_modificacion,
    }


# =====================================================================
# XML builder
# =====================================================================

def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def construir_xml_nota_credito(
    *,
    clave_acceso: str,
    ambiente: str,
    tipo_emision: str,
    estab: str,
    pto_emi: str,
    secuencial: str,
    info: InfoNotaCredito,
) -> str:
    """Construye el XML completo de una nota de crédito SRI v1.1.0."""
    totales = calcular_totales_nc(info)

    root = ET.Element("notaCredito", {"id": "comprobante", "version": "1.1.0"})

    # --- infoTributaria — idéntico a factura salvo codDoc='04' -----------
    it = ET.SubElement(root, "infoTributaria")
    _sub(it, "ambiente", ambiente)
    _sub(it, "tipoEmision", tipo_emision)
    _sub(it, "razonSocial", info.emisor.razon_social)
    if info.emisor.nombre_comercial:
        _sub(it, "nombreComercial", info.emisor.nombre_comercial)
    _sub(it, "ruc", info.emisor.ruc)
    _sub(it, "claveAcceso", clave_acceso)
    _sub(it, "codDoc", "04")  # nota de crédito
    _sub(it, "estab", estab)
    _sub(it, "ptoEmi", pto_emi)
    _sub(it, "secuencial", secuencial)
    _sub(it, "dirMatriz", info.emisor.dir_matriz)

    # --- infoNotaCredito --------------------------------------------------
    inc = ET.SubElement(root, "infoNotaCredito")
    _sub(inc, "fechaEmision", info.fecha_emision.strftime("%d/%m/%Y"))
    if info.emisor.dir_establecimiento:
        _sub(inc, "dirEstablecimiento", info.emisor.dir_establecimiento)
    _sub(inc, "tipoIdentificacionComprador", info.comprador.tipo_identificacion)
    _sub(inc, "razonSocialComprador", info.comprador.razon_social)
    _sub(inc, "identificacionComprador", info.comprador.identificacion)
    if info.comprador.direccion:
        _sub(inc, "direccionComprador", info.comprador.direccion)
    if info.emisor.contribuyente_especial:
        _sub(inc, "contribuyenteEspecial", info.emisor.contribuyente_especial)
    _sub(inc, "obligadoContabilidad", info.emisor.obligado_contabilidad)
    _sub(inc, "rise", "")  # vacío si no aplica — el XSD lo pide opcional

    # Referencia al documento modificado — ESTA es la parte única de NC.
    _sub(inc, "codDocModificado", info.cod_doc_modificado)
    _sub(inc, "numDocModificado", info.num_doc_modificado)
    _sub(inc, "fechaEmisionDocSustento",
         info.fecha_emision_doc_sustento.strftime("%d/%m/%Y"))

    _sub(inc, "totalSinImpuestos", fmt_monto(totales["subtotal_sin_impuestos"]))

    # Totales con impuestos — estructura idéntica a factura.
    tci = ET.SubElement(inc, "totalConImpuestos")
    for bloque in totales["por_iva"]:
        ti = ET.SubElement(tci, "totalImpuesto")
        _sub(ti, "codigo", COD_IMPUESTO_IVA)
        _sub(ti, "codigoPorcentaje", bloque["codigo_iva"])
        _sub(ti, "baseImponible", fmt_monto(bloque["base_imponible"]))
        _sub(ti, "valor", fmt_monto(bloque["valor"]))

    _sub(inc, "valorModificacion", fmt_monto(totales["valor_modificacion"]))
    _sub(inc, "moneda", info.moneda)
    _sub(inc, "motivo", info.motivo)

    # --- detalles ---------------------------------------------------------
    dets = ET.SubElement(root, "detalles")
    for linea in info.detalles:
        d = ET.SubElement(dets, "detalle")
        _sub(d, "codigoInterno", linea.codigo_principal)  # NC usa codigoInterno
        _sub(d, "descripcion", linea.descripcion)
        _sub(d, "cantidad", fmt_cantidad(linea.cantidad))
        _sub(d, "precioUnitario", fmt_monto(linea.precio_unitario))
        _sub(d, "descuento", fmt_monto(linea.descuento))
        _sub(d, "precioTotalSinImpuesto", fmt_monto(_subtotal_linea(linea)))

        imps = ET.SubElement(d, "impuestos")
        imp = ET.SubElement(imps, "impuesto")
        _sub(imp, "codigo", COD_IMPUESTO_IVA)
        _sub(imp, "codigoPorcentaje", cod_iva_por_porcentaje(linea.iva_porcentaje))
        _sub(imp, "tarifa", fmt_monto(linea.iva_porcentaje))
        _sub(imp, "baseImponible", fmt_monto(_subtotal_linea(linea)))
        _sub(imp, "valor", fmt_monto(_iva_linea(linea)))

    # --- infoAdicional ----------------------------------------------------
    extras = dict(info.info_adicional)
    if info.comprador.email and "Email" not in extras:
        extras["Email"] = info.comprador.email
    if extras:
        ia = ET.SubElement(root, "infoAdicional")
        for nombre, valor in extras.items():
            el = ET.SubElement(ia, "campoAdicional", {"nombre": nombre})
            el.text = valor

    xml_bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return xml_bytes.decode("UTF-8")


# =====================================================================
# Validación estructural
# =====================================================================

_ORDEN_NC = ("infoTributaria", "infoNotaCredito", "detalles")
_ORDEN_INFO_NC = (
    "fechaEmision", "tipoIdentificacionComprador", "razonSocialComprador",
    "identificacionComprador", "obligadoContabilidad",
    "codDocModificado", "numDocModificado", "fechaEmisionDocSustento",
    "totalSinImpuestos", "totalConImpuestos",
    "valorModificacion", "moneda", "motivo",
)


def validar_estructura_nota_credito(xml_str: str) -> list[str]:
    """Valida estructura mínima. Si devuelve [], el XML es razonable.

    Si devuelve una lista de errores, el SRI probablemente los rechace.
    Útil para pre-flight antes de firmar y enviar.
    """
    errores = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        return [f"XML no parseable: {e}"]

    if root.tag != "notaCredito":
        errores.append(f"Root debe ser <notaCredito>, es <{root.tag}>")

    top = [c.tag for c in root if c.tag != "infoAdicional"]
    for i, esperado in enumerate(_ORDEN_NC):
        if i >= len(top) or top[i] != esperado:
            errores.append(
                f"Orden incorrecto en root: esperado {_ORDEN_NC}, encontrado {top}"
            )
            break

    inc = root.find("infoNotaCredito")
    if inc is None:
        errores.append("Falta <infoNotaCredito>")
    else:
        # Verificar campos críticos de referencia al doc original
        for campo in ("codDocModificado", "numDocModificado", "fechaEmisionDocSustento", "motivo"):
            if inc.find(campo) is None:
                errores.append(f"Falta <{campo}> en <infoNotaCredito>")

    dets = root.find("detalles")
    if dets is None or len(dets) == 0:
        errores.append("<detalles> vacío o ausente")

    return errores
