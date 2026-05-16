"""Tests del generador XML de notas de crédito SRI."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from modules.sri.xml import Comprador, DetalleLinea, Emisor
from modules.sri.xml_nota_credito import (
    InfoNotaCredito,
    calcular_totales_nc,
    construir_xml_nota_credito,
    validar_estructura_nota_credito,
)


def _emisor():
    return Emisor(
        ruc="1790012345001",
        razon_social="TEXTILES INTELA S.A.",
        nombre_comercial="INTELA",
        dir_matriz="Panamericana Sur km 25",
        obligado_contabilidad="SI",
    )


def _comprador():
    return Comprador(
        tipo_identificacion="04",
        razon_social="CLIENTE EJEMPLO S.A.",
        identificacion="1790098765001",
        direccion="Av. de los Shyris N32-114",
        email="cliente@ejemplo.com",
    )


def _detalle_simple(monto_bruto=115.0, iva_pct=15.0):
    base = round(monto_bruto / (1 + iva_pct / 100), 2)
    return DetalleLinea(
        codigo_principal="NC-REF-001",
        descripcion="Devolución parcial factura 001-001-123",
        cantidad=1.0,
        precio_unitario=base,
        descuento=0.0,
        iva_porcentaje=iva_pct,
    )


def _info_nc(motivo="Devolución de mercadería por falla de calidad"):
    return InfoNotaCredito(
        emisor=_emisor(),
        comprador=_comprador(),
        fecha_emision=date(2026, 4, 17),
        cod_doc_modificado="01",
        num_doc_modificado="001-001-000000123",
        fecha_emision_doc_sustento=date(2026, 4, 10),
        motivo=motivo,
        detalles=[_detalle_simple()],
    )


# ---------------------------------------------------------------------------
# Totales
# ---------------------------------------------------------------------------

def test_calcular_totales_una_linea():
    info = _info_nc()
    t = calcular_totales_nc(info)
    assert t["subtotal_sin_impuestos"] == 100.00
    assert t["total_iva"] == 15.00
    assert t["valor_modificacion"] == 115.00


def test_calcular_totales_varias_lineas_agrupa_por_iva():
    info = _info_nc()
    info.detalles = [
        DetalleLinea("a", "x", 1.0, 100.0, 0.0, 15.0),
        DetalleLinea("b", "y", 2.0, 50.0, 0.0, 15.0),
        DetalleLinea("c", "z", 1.0, 50.0, 0.0, 0.0),
    ]
    t = calcular_totales_nc(info)
    # 100 + 100 + 50 = 250 subtotal
    assert t["subtotal_sin_impuestos"] == 250.00
    # Dos buckets de IVA
    assert len(t["por_iva"]) == 2


def test_calcular_totales_sin_detalles_es_cero():
    info = _info_nc()
    info.detalles = []
    t = calcular_totales_nc(info)
    assert t["subtotal_sin_impuestos"] == 0
    assert t["valor_modificacion"] == 0


# ---------------------------------------------------------------------------
# XML builder
# ---------------------------------------------------------------------------

def test_xml_tiene_root_notaCredito():
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49,
        ambiente="1",
        tipo_emision="1",
        estab="001",
        pto_emi="001",
        secuencial="000000001",
        info=_info_nc(),
    )
    root = ET.fromstring(xml)
    assert root.tag == "notaCredito"
    assert root.get("version") == "1.1.0"
    assert root.get("id") == "comprobante"


def test_xml_codDoc_es_04():
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=_info_nc(),
    )
    root = ET.fromstring(xml)
    cod_doc = root.find("infoTributaria/codDoc")
    assert cod_doc is not None
    assert cod_doc.text == "04"


def test_xml_incluye_refs_al_doc_modificado():
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=_info_nc(),
    )
    root = ET.fromstring(xml)
    inc = root.find("infoNotaCredito")
    assert inc is not None
    assert inc.findtext("codDocModificado") == "01"
    assert inc.findtext("numDocModificado") == "001-001-000000123"
    assert inc.findtext("fechaEmisionDocSustento") == "10/04/2026"


def test_xml_incluye_motivo():
    motivo = "Error en cantidad facturada"
    info = _info_nc(motivo=motivo)
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=info,
    )
    root = ET.fromstring(xml)
    assert root.find("infoNotaCredito/motivo").text == motivo


def test_xml_fecha_emision_formato_con_barras():
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=_info_nc(),
    )
    root = ET.fromstring(xml)
    fecha = root.findtext("infoNotaCredito/fechaEmision")
    assert fecha == "17/04/2026"  # DD/MM/AAAA con barras


def test_xml_detalle_usa_codigoInterno_no_codigoPrincipal():
    """La NC usa <codigoInterno>, no <codigoPrincipal> (diferencia con factura)."""
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=_info_nc(),
    )
    root = ET.fromstring(xml)
    det = root.find("detalles/detalle")
    assert det is not None
    assert det.find("codigoInterno") is not None
    assert det.find("codigoPrincipal") is None


def test_xml_total_con_impuestos_tiene_bloque_iva():
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=_info_nc(),
    )
    root = ET.fromstring(xml)
    bloques = root.findall("infoNotaCredito/totalConImpuestos/totalImpuesto")
    assert len(bloques) == 1
    assert bloques[0].findtext("valor") == "15.00"


def test_xml_clave_acceso_embebida():
    clave = "1" * 49
    xml = construir_xml_nota_credito(
        clave_acceso=clave, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=_info_nc(),
    )
    root = ET.fromstring(xml)
    assert root.findtext("infoTributaria/claveAcceso") == clave


def test_xml_valor_modificacion_coincide_con_totales():
    info = _info_nc()
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=info,
    )
    root = ET.fromstring(xml)
    v = root.findtext("infoNotaCredito/valorModificacion")
    assert v == "115.00"


# ---------------------------------------------------------------------------
# Validación estructural
# ---------------------------------------------------------------------------

def test_validar_xml_bien_formado_sin_errores():
    xml = construir_xml_nota_credito(
        clave_acceso="0" * 49, ambiente="1", tipo_emision="1",
        estab="001", pto_emi="001", secuencial="000000001",
        info=_info_nc(),
    )
    assert validar_estructura_nota_credito(xml) == []


def test_validar_xml_no_parseable():
    errs = validar_estructura_nota_credito("<notaCredito><broken>")
    assert len(errs) >= 1
    assert "parseable" in errs[0].lower()


def test_validar_xml_con_root_incorrecto():
    errs = validar_estructura_nota_credito("<factura></factura>")
    assert any("Root debe ser" in e for e in errs)


def test_validar_xml_sin_detalles_detecta():
    xml = """<?xml version='1.0' encoding='UTF-8'?>
<notaCredito id="comprobante" version="1.1.0">
  <infoTributaria></infoTributaria>
  <infoNotaCredito>
    <codDocModificado>01</codDocModificado>
    <numDocModificado>001-001-000000123</numDocModificado>
    <fechaEmisionDocSustento>10/04/2026</fechaEmisionDocSustento>
    <motivo>x</motivo>
  </infoNotaCredito>
  <detalles></detalles>
</notaCredito>"""
    errs = validar_estructura_nota_credito(xml)
    assert any("detalles" in e.lower() for e in errs)


def test_validar_xml_faltan_refs_al_doc_modificado():
    xml = """<?xml version='1.0' encoding='UTF-8'?>
<notaCredito id="comprobante" version="1.1.0">
  <infoTributaria></infoTributaria>
  <infoNotaCredito>
    <motivo>x</motivo>
  </infoNotaCredito>
  <detalles><detalle></detalle></detalles>
</notaCredito>"""
    errs = validar_estructura_nota_credito(xml)
    assert any("codDocModificado" in e for e in errs)
    assert any("numDocModificado" in e for e in errs)
