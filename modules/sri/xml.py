"""Generador de XML para factura electrónica SRI — formato v1.1.0.

Usa xml.etree.ElementTree de stdlib (sin dependencias nuevas). La firma
electrónica y el envío SOAP viven en firma.py y envio.py respectivamente;
este módulo sólo arma el XML "en crudo", listo para ser firmado.

Referencia:
    - Ficha técnica SRI "Comprobantes Electrónicos" v2.21.
    - XSD oficial factura versión 1.1.0.

Gotchas del XSD SRI:
    - Montos: punto como separador decimal, 2 decimales exactos.
    - Fechas en dirEmision (infoFactura.fechaEmision): formato DD/MM/AAAA
      (con barras), NO el AAAAMMDD que usa la clave de acceso.
    - razonSocial y descripción: el XSD los valida contra una lista limitada
      de caracteres — acentos y ñ pasan, pero símbolos como & requieren
      entidades XML (ElementTree lo hace automáticamente al serializar).
    - El elemento root debe tener `id="comprobante"` y `version="1.1.0"`.
    - El orden de los elementos importa: el XSD es secuencial (xs:sequence),
      no libre. Mantenerlo como está en las funciones de este archivo.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date

from modules.sri.core import fmt_cantidad, fmt_monto

# =====================================================================
# Códigos SRI — los más comunes
# =====================================================================

# Código impuesto (infoFactura.totalConImpuestos.totalImpuesto.codigo)
COD_IMPUESTO_IVA  = "2"
COD_IMPUESTO_ICE  = "3"
COD_IMPUESTO_IRBPNR = "5"

# Código porcentaje IVA (totalImpuesto.codigoPorcentaje)
# Los históricos y vigentes.
COD_IVA_0        = "0"    # IVA 0% (exento / tasa cero)
COD_IVA_12       = "2"    # histórico — facturas pre-2024
COD_IVA_14       = "3"    # transitorio
COD_IVA_15       = "4"    # vigente desde 2024-04
COD_IVA_NO_OBJ   = "6"
COD_IVA_EXENTO   = "7"
COD_IVA_5        = "8"

# Mapeo de porcentaje numérico → código SRI
_PCT_TO_COD_IVA = {
    0:  COD_IVA_0,
    5:  COD_IVA_5,
    12: COD_IVA_12,
    14: COD_IVA_14,
    15: COD_IVA_15,
}

# Tipo de identificación del comprador
ID_RUC              = "04"
ID_CEDULA           = "05"
ID_PASAPORTE        = "06"
ID_CONSUMIDOR_FINAL = "07"
ID_EXTRANJERO       = "08"

# Forma de pago — lista corta de las usadas normalmente
PAGO_EFECTIVO       = "01"
PAGO_COMPENSACION   = "15"
PAGO_TARJETA_DEBITO = "16"
PAGO_TARJETA_CREDITO = "19"
PAGO_OTROS_SISTEMA  = "20"   # transferencia bancaria, cheque — el más usado
PAGO_ENDOSO         = "21"


def cod_iva_por_porcentaje(pct) -> str:
    """Devuelve el codigoPorcentaje SRI para un IVA dado (0, 12, 14, 15, …).

    Caer aquí con un porcentaje no mapeado es un bug: agregar al dict
    _PCT_TO_COD_IVA cuando el SRI defina uno nuevo.
    """
    try:
        pct_int = int(round(float(pct)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"porcentaje IVA inválido: {pct!r}") from exc
    if pct_int not in _PCT_TO_COD_IVA:
        raise ValueError(
            f"IVA {pct_int}% no mapeado. Códigos SRI soportados: "
            f"{sorted(_PCT_TO_COD_IVA.keys())}"
        )
    return _PCT_TO_COD_IVA[pct_int]


# =====================================================================
# Dataclasses — el contrato de lo que necesita el generador
# =====================================================================

@dataclass
class Emisor:
    """Datos del emisor (la fábrica). Vienen de config fija, no de la DB."""
    ruc: str                    # 13 dígitos
    razon_social: str           # "REYES S.A." o similar
    nombre_comercial: str       # "INTELA"
    dir_matriz: str             # dirección fiscal
    dir_establecimiento: str | None = None  # si distinta, p.e. bodega
    obligado_contabilidad: str = "SI"        # "SI" o "NO"
    contribuyente_especial: str | None = None  # número de resolución si aplica


@dataclass
class Comprador:
    """Cliente. Se arma desde scintela.cliente al momento de emitir."""
    identificacion: str              # RUC/cédula/pasaporte limpio
    tipo_identificacion: str         # uno de ID_*
    razon_social: str                # nombre del cliente
    direccion: str | None = None
    email: str | None = None
    telefono: str | None = None


@dataclass
class DetalleLinea:
    """Una línea de la factura. kg/cantidad + precio unitario + IVA."""
    codigo_principal: str
    descripcion: str
    cantidad: float
    precio_unitario: float
    descuento: float = 0.0
    iva_porcentaje: float = 15.0   # default actual; usar 12 para histórico


@dataclass
class Pago:
    """Una forma de pago. Normalmente sólo una por factura en el flujo Intela."""
    forma_pago: str = PAGO_OTROS_SISTEMA   # "20" — transferencia/cheque
    total: float = 0.0
    plazo: int | None = None               # días de crédito
    unidad_tiempo: str = "dias"            # "dias" es el único valor usado


@dataclass
class InfoFactura:
    """Agrupa los campos específicos de un comprobante."""
    fecha_emision: date
    emisor: Emisor
    comprador: Comprador
    detalles: list[DetalleLinea]
    pagos: list[Pago] = field(default_factory=list)
    propina: float = 0.0
    info_adicional: dict[str, str] = field(default_factory=dict)
    moneda: str = "DOLAR"


# =====================================================================
# Cálculo de totales — puro, determinista, testeable
# =====================================================================

def _subtotal_linea(linea: DetalleLinea) -> float:
    """Base imponible de una línea = cantidad * precio - descuento."""
    bruto = round(linea.cantidad * linea.precio_unitario, 2)
    return round(bruto - linea.descuento, 2)


def _iva_linea(linea: DetalleLinea) -> float:
    """IVA de una línea = subtotal * iva%/100."""
    base = _subtotal_linea(linea)
    return round(base * linea.iva_porcentaje / 100, 2)


def calcular_totales(info: InfoFactura) -> dict:
    """Agrega los totales del comprobante a partir de los detalles.

    Devuelve un dict con las claves esperadas por la sección
    totalConImpuestos y el importeTotal. Agrupa por porcentaje de IVA —
    si una factura tiene líneas con 12% y con 15% (caso raro pero posible
    en el transitorio de abril 2024), salen dos bloques totalImpuesto.
    """
    por_iva: dict[float, dict] = {}
    subtotal_sin_imp = 0.0
    total_descuento = 0.0

    for linea in info.detalles:
        base = _subtotal_linea(linea)
        iva = _iva_linea(linea)
        subtotal_sin_imp += base
        total_descuento += linea.descuento

        key = round(linea.iva_porcentaje, 2)
        if key not in por_iva:
            por_iva[key] = {
                "porcentaje": key,
                "codigo_iva": cod_iva_por_porcentaje(key),
                "base_imponible": 0.0,
                "valor": 0.0,
            }
        por_iva[key]["base_imponible"] = round(por_iva[key]["base_imponible"] + base, 2)
        por_iva[key]["valor"] = round(por_iva[key]["valor"] + iva, 2)

    total_iva = round(sum(b["valor"] for b in por_iva.values()), 2)
    subtotal_sin_imp = round(subtotal_sin_imp, 2)
    total_descuento = round(total_descuento, 2)
    importe_total = round(subtotal_sin_imp + total_iva + info.propina, 2)

    return {
        "subtotal_sin_impuestos": subtotal_sin_imp,
        "total_descuento": total_descuento,
        "total_iva": total_iva,
        "importe_total": importe_total,
        "por_iva": list(por_iva.values()),
    }


# =====================================================================
# Construcción del XML
# =====================================================================

def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    """Shortcut: crear <tag>text</tag> dentro de parent."""
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def construir_xml_factura(
    *,
    clave_acceso: str,
    ambiente: str,
    tipo_emision: str,
    estab: str,
    pto_emi: str,
    secuencial: str,
    info: InfoFactura,
) -> str:
    """Construye el XML completo de una factura SRI v1.1.0.

    Devuelve el XML serializado como string UTF-8 (sin declaración —
    el firmador agrega <?xml ...?> antes de firmar). Si hace falta la
    declaración, llamar a `ET.tostring(..., xml_declaration=True)` o
    prependerla manualmente.
    """
    totales = calcular_totales(info)

    # <factura id="comprobante" version="1.1.0">
    root = ET.Element("factura", {"id": "comprobante", "version": "1.1.0"})

    # --- infoTributaria -------------------------------------------------
    it = ET.SubElement(root, "infoTributaria")
    _sub(it, "ambiente", ambiente)
    _sub(it, "tipoEmision", tipo_emision)
    _sub(it, "razonSocial", info.emisor.razon_social)
    if info.emisor.nombre_comercial:
        _sub(it, "nombreComercial", info.emisor.nombre_comercial)
    _sub(it, "ruc", info.emisor.ruc)
    _sub(it, "claveAcceso", clave_acceso)
    _sub(it, "codDoc", "01")    # factura
    _sub(it, "estab", estab)
    _sub(it, "ptoEmi", pto_emi)
    _sub(it, "secuencial", secuencial)
    _sub(it, "dirMatriz", info.emisor.dir_matriz)

    # --- infoFactura ----------------------------------------------------
    inf = ET.SubElement(root, "infoFactura")
    _sub(inf, "fechaEmision", info.fecha_emision.strftime("%d/%m/%Y"))
    if info.emisor.dir_establecimiento:
        _sub(inf, "dirEstablecimiento", info.emisor.dir_establecimiento)
    if info.emisor.contribuyente_especial:
        _sub(inf, "contribuyenteEspecial", info.emisor.contribuyente_especial)
    _sub(inf, "obligadoContabilidad", info.emisor.obligado_contabilidad)
    _sub(inf, "tipoIdentificacionComprador", info.comprador.tipo_identificacion)
    _sub(inf, "razonSocialComprador", info.comprador.razon_social)
    _sub(inf, "identificacionComprador", info.comprador.identificacion)
    if info.comprador.direccion:
        _sub(inf, "direccionComprador", info.comprador.direccion)
    _sub(inf, "totalSinImpuestos", fmt_monto(totales["subtotal_sin_impuestos"]))
    _sub(inf, "totalDescuento", fmt_monto(totales["total_descuento"]))

    # totalConImpuestos
    tci = ET.SubElement(inf, "totalConImpuestos")
    for bloque in totales["por_iva"]:
        ti = ET.SubElement(tci, "totalImpuesto")
        _sub(ti, "codigo", COD_IMPUESTO_IVA)
        _sub(ti, "codigoPorcentaje", bloque["codigo_iva"])
        _sub(ti, "baseImponible", fmt_monto(bloque["base_imponible"]))
        _sub(ti, "valor", fmt_monto(bloque["valor"]))

    _sub(inf, "propina", fmt_monto(info.propina))
    _sub(inf, "importeTotal", fmt_monto(totales["importe_total"]))
    _sub(inf, "moneda", info.moneda)

    # pagos
    pagos = info.pagos or [Pago(total=totales["importe_total"])]
    pg_root = ET.SubElement(inf, "pagos")
    for p in pagos:
        pg = ET.SubElement(pg_root, "pago")
        _sub(pg, "formaPago", p.forma_pago)
        _sub(pg, "total", fmt_monto(p.total))
        if p.plazo is not None:
            _sub(pg, "plazo", str(p.plazo))
            _sub(pg, "unidadTiempo", p.unidad_tiempo)

    # --- detalles -------------------------------------------------------
    dets = ET.SubElement(root, "detalles")
    for linea in info.detalles:
        d = ET.SubElement(dets, "detalle")
        _sub(d, "codigoPrincipal", linea.codigo_principal)
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

    # --- infoAdicional (opcional) ---------------------------------------
    extras = dict(info.info_adicional)
    # Siempre incluir email si el comprador lo tiene, para que el SRI/
    # el software receptor pueda reenviar.
    if info.comprador.email and "Email" not in extras:
        extras["Email"] = info.comprador.email
    if info.comprador.telefono and "Telefono" not in extras:
        extras["Telefono"] = info.comprador.telefono

    if extras:
        ia = ET.SubElement(root, "infoAdicional")
        for nombre, valor in extras.items():
            el = ET.SubElement(ia, "campoAdicional", {"nombre": nombre})
            el.text = valor

    # Serializar. UTF-8 con declaración XML — el firmador la necesita.
    xml_bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return xml_bytes.decode("UTF-8")


# =====================================================================
# Validación estructural (fallback si no hay lxml/XSD local)
# =====================================================================

# Los elementos obligatorios en orden. Si no coinciden, el SRI va a
# rechazar con "elemento X no declarado" — útil para atrapar bugs ANTES
# de mandar al SRI.
_ORDEN_FACTURA = ("infoTributaria", "infoFactura", "detalles")
_ORDEN_INFO_TRIB = (
    "ambiente", "tipoEmision", "razonSocial", "ruc", "claveAcceso",
    "codDoc", "estab", "ptoEmi", "secuencial", "dirMatriz",
)
_ORDEN_INFO_FACTURA = (
    "fechaEmision", "obligadoContabilidad",
    "tipoIdentificacionComprador", "razonSocialComprador",
    "identificacionComprador",
    "totalSinImpuestos", "totalDescuento", "totalConImpuestos",
    "propina", "importeTotal", "moneda", "pagos",
)


def validar_estructura_factura(xml_str: str) -> list[str]:
    """Chequea la estructura básica del XML. Devuelve lista de errores.

    Lista vacía = pasa la validación estructural. No sustituye la
    validación XSD del SRI, pero atrapa los errores más comunes (orden,
    elementos faltantes, claveAcceso de 48 dígitos en lugar de 49).
    """
    errs: list[str] = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        return [f"XML malformado: {e}"]

    if root.tag != "factura":
        errs.append(f"root debe ser <factura>, es <{root.tag}>")
    if root.get("version") != "1.1.0":
        errs.append(f"version debe ser 1.1.0, es {root.get('version')}")

    hijos = [c.tag for c in root]
    for esperado in _ORDEN_FACTURA:
        if esperado not in hijos:
            errs.append(f"falta <{esperado}>")

    it = root.find("infoTributaria")
    if it is not None:
        ca = it.find("claveAcceso")
        if ca is None or not ca.text or len(ca.text) != 49:
            errs.append("claveAcceso debe tener 49 dígitos")
        tags_it = [c.tag for c in it]
        for esperado in _ORDEN_INFO_TRIB:
            if esperado not in tags_it:
                errs.append(f"infoTributaria falta <{esperado}>")

    inf = root.find("infoFactura")
    if inf is not None:
        tags_inf = [c.tag for c in inf]
        for esperado in _ORDEN_INFO_FACTURA:
            if esperado not in tags_inf:
                errs.append(f"infoFactura falta <{esperado}>")

    dets = root.find("detalles")
    if dets is not None and len(dets) == 0:
        errs.append("detalles debe tener al menos un <detalle>")

    return errs
