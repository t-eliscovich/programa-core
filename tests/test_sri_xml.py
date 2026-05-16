"""Tests de modules/sri/xml.py — generador de factura electrónica."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from modules.sri import core, envio
from modules.sri.xml import (
    Comprador,
    DetalleLinea,
    Emisor,
    InfoFactura,
    Pago,
    calcular_totales,
    cod_iva_por_porcentaje,
    construir_xml_factura,
    validar_estructura_factura,
)

# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def emisor_intela():
    return Emisor(
        ruc="1790012345001",
        razon_social="TEXTILES INTELA SA",
        nombre_comercial="INTELA",
        dir_matriz="Av. Principal y Secundaria, Quito",
    )


@pytest.fixture
def comprador_ruc():
    return Comprador(
        identificacion="0992345678001",
        tipo_identificacion="04",
        razon_social="CLIENTE DE PRUEBA S.A.",
        direccion="Guayaquil",
        email="cliente@example.com",
    )


@pytest.fixture
def info_basica(emisor_intela, comprador_ruc):
    return InfoFactura(
        fecha_emision=date(2026, 4, 17),
        emisor=emisor_intela,
        comprador=comprador_ruc,
        detalles=[
            DetalleLinea(
                codigo_principal="TELA-A",
                descripcion="Tela algodón blanco",
                cantidad=10,
                precio_unitario=5.00,
                iva_porcentaje=15.0,
            ),
            DetalleLinea(
                codigo_principal="TELA-B",
                descripcion="Tela algodón azul",
                cantidad=4,
                precio_unitario=12.50,
                iva_porcentaje=15.0,
            ),
        ],
    )


@pytest.fixture
def clave_acceso():
    return core.generar_clave_acceso(
        fecha_emision=date(2026, 4, 17),
        tipo_comprobante=core.TIPO_FACTURA,
        ruc="1790012345001",
        ambiente=core.AMBIENTE_CERTIFICACION,
        secuencial=1,
        codigo_numerico="12345678",
    )


# =====================================================================
# cod_iva_por_porcentaje
# =====================================================================

class TestCodIvaPorPorcentaje:
    def test_15_pct_mapea_a_4(self):
        """El código SRI para IVA 15% (vigente desde 2024) es '4'."""
        assert cod_iva_por_porcentaje(15) == "4"

    def test_12_pct_mapea_a_2(self):
        """Histórico — algunas facturas anteriores a abril 2024."""
        assert cod_iva_por_porcentaje(12) == "2"

    def test_0_pct_mapea_a_0(self):
        assert cod_iva_por_porcentaje(0) == "0"

    def test_porcentaje_float_cercano_entero(self):
        assert cod_iva_por_porcentaje(15.0) == "4"
        assert cod_iva_por_porcentaje(12.00) == "2"

    def test_porcentaje_no_mapeado_explota(self):
        with pytest.raises(ValueError, match="no mapeado"):
            cod_iva_por_porcentaje(33)


# =====================================================================
# calcular_totales — lógica de agregación
# =====================================================================

class TestCalcularTotales:
    def test_factura_simple_15pct(self, info_basica):
        """10*5 + 4*12.50 = 50 + 50 = 100 subtotal, 15 IVA, 115 total."""
        t = calcular_totales(info_basica)
        assert t["subtotal_sin_impuestos"] == 100.00
        assert t["total_iva"] == 15.00
        assert t["importe_total"] == 115.00
        assert len(t["por_iva"]) == 1
        assert t["por_iva"][0]["porcentaje"] == 15.0
        assert t["por_iva"][0]["codigo_iva"] == "4"

    def test_factura_con_descuento(self, emisor_intela, comprador_ruc):
        info = InfoFactura(
            fecha_emision=date(2026, 4, 17),
            emisor=emisor_intela,
            comprador=comprador_ruc,
            detalles=[
                DetalleLinea(
                    codigo_principal="X", descripcion="item",
                    cantidad=1, precio_unitario=100.00,
                    descuento=10.00, iva_porcentaje=15.0,
                ),
            ],
        )
        t = calcular_totales(info)
        # base = 100 - 10 = 90; iva = 13.50; total = 103.50
        assert t["subtotal_sin_impuestos"] == 90.00
        assert t["total_descuento"] == 10.00
        assert t["total_iva"] == 13.50
        assert t["importe_total"] == 103.50

    def test_factura_con_dos_porcentajes_iva(self, emisor_intela, comprador_ruc):
        """Transitorio abril 2024: factura con líneas a 12% y 15%."""
        info = InfoFactura(
            fecha_emision=date(2024, 4, 1),  # fecha de transición
            emisor=emisor_intela,
            comprador=comprador_ruc,
            detalles=[
                DetalleLinea(
                    codigo_principal="V", descripcion="vieja",
                    cantidad=1, precio_unitario=100, iva_porcentaje=12.0,
                ),
                DetalleLinea(
                    codigo_principal="N", descripcion="nueva",
                    cantidad=1, precio_unitario=100, iva_porcentaje=15.0,
                ),
            ],
        )
        t = calcular_totales(info)
        assert t["subtotal_sin_impuestos"] == 200.00
        assert t["total_iva"] == 27.00  # 12 + 15
        # Dos bloques totalImpuesto separados
        assert len(t["por_iva"]) == 2
        pcts = {b["porcentaje"] for b in t["por_iva"]}
        assert pcts == {12.0, 15.0}

    def test_factura_con_iva_0(self, emisor_intela, comprador_ruc):
        """Exportaciones y alimentos básicos van con IVA 0%."""
        info = InfoFactura(
            fecha_emision=date(2026, 4, 17),
            emisor=emisor_intela,
            comprador=comprador_ruc,
            detalles=[
                DetalleLinea(
                    codigo_principal="EXP", descripcion="exportación",
                    cantidad=10, precio_unitario=100, iva_porcentaje=0,
                ),
            ],
        )
        t = calcular_totales(info)
        assert t["total_iva"] == 0.00
        assert t["importe_total"] == 1000.00
        assert t["por_iva"][0]["codigo_iva"] == "0"


# =====================================================================
# construir_xml_factura — estructura y contenido
# =====================================================================

class TestConstruirXmlFactura:
    def test_xml_es_parseable(self, info_basica, clave_acceso):
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1",
            tipo_emision="1",
            estab="001",
            pto_emi="001",
            secuencial="000000001",
            info=info_basica,
        )
        # No debe romper al parsear
        root = ET.fromstring(xml)
        assert root.tag == "factura"
        assert root.get("version") == "1.1.0"
        assert root.get("id") == "comprobante"

    def test_xml_tiene_declaracion_utf8(self, info_basica, clave_acceso):
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        # El firmador SRI necesita la declaración XML.
        assert xml.startswith("<?xml")
        assert "UTF-8" in xml[:100].upper()

    def test_info_tributaria_tiene_clave_acceso(self, info_basica, clave_acceso):
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        root = ET.fromstring(xml)
        ca = root.find("infoTributaria/claveAcceso")
        assert ca is not None
        assert ca.text == clave_acceso
        assert len(ca.text) == 49

    def test_fecha_emision_es_ddmmaaaa_con_barras(self, info_basica, clave_acceso):
        """infoFactura.fechaEmision usa DD/MM/AAAA (distinto de la clave)."""
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        root = ET.fromstring(xml)
        fe = root.find("infoFactura/fechaEmision")
        assert fe is not None
        assert fe.text == "17/04/2026"

    def test_total_con_impuestos_tiene_bloque_iva(self, info_basica, clave_acceso):
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        root = ET.fromstring(xml)
        ti = root.find("infoFactura/totalConImpuestos/totalImpuesto")
        assert ti is not None
        assert ti.find("codigo").text == "2"           # IVA
        assert ti.find("codigoPorcentaje").text == "4"  # 15%
        assert ti.find("baseImponible").text == "100.00"
        assert ti.find("valor").text == "15.00"

    def test_importe_total_suma_iva(self, info_basica, clave_acceso):
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        root = ET.fromstring(xml)
        it = root.find("infoFactura/importeTotal")
        assert it.text == "115.00"

    def test_hay_un_detalle_por_linea(self, info_basica, clave_acceso):
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        root = ET.fromstring(xml)
        detalles = root.findall("detalles/detalle")
        assert len(detalles) == 2
        # Primera línea: TELA-A, cantidad 10, precio 5
        assert detalles[0].find("codigoPrincipal").text == "TELA-A"
        assert detalles[0].find("cantidad").text == "10.00"
        assert detalles[0].find("precioUnitario").text == "5.00"
        assert detalles[0].find("precioTotalSinImpuesto").text == "50.00"

    def test_pagos_default_es_total_completo(self, info_basica, clave_acceso):
        """Si no se pasan pagos, se asume forma=20 con el total."""
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        root = ET.fromstring(xml)
        pagos = root.findall("infoFactura/pagos/pago")
        assert len(pagos) == 1
        assert pagos[0].find("formaPago").text == "20"
        assert pagos[0].find("total").text == "115.00"

    def test_pago_con_plazo_incluye_unidad_tiempo(self, info_basica, clave_acceso):
        info_basica.pagos = [
            Pago(forma_pago="20", total=115.00, plazo=30, unidad_tiempo="dias")
        ]
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        root = ET.fromstring(xml)
        pago = root.find("infoFactura/pagos/pago")
        assert pago.find("plazo").text == "30"
        assert pago.find("unidadTiempo").text == "dias"

    def test_email_del_comprador_se_agrega_a_info_adicional(
        self, info_basica, clave_acceso
    ):
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        root = ET.fromstring(xml)
        campos = root.findall("infoAdicional/campoAdicional")
        nombres = {c.get("nombre"): c.text for c in campos}
        assert nombres.get("Email") == "cliente@example.com"

    def test_caracteres_especiales_en_razon_social_se_escapan(
        self, emisor_intela, clave_acceso
    ):
        # Ampersand + ñ + acento → ElementTree debe escapar el &
        comprador = Comprador(
            identificacion="0992345678001",
            tipo_identificacion="04",
            razon_social="JUAN & ASOCIADOS — ÑANDÚ",
        )
        info = InfoFactura(
            fecha_emision=date(2026, 4, 17),
            emisor=emisor_intela,
            comprador=comprador,
            detalles=[DetalleLinea(
                codigo_principal="X", descripcion="item",
                cantidad=1, precio_unitario=1, iva_porcentaje=15,
            )],
        )
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info,
        )
        # El & no debe aparecer literal; debe ser &amp;
        assert " & " not in xml
        assert "&amp;" in xml
        # El re-parse debe devolver el original
        root = ET.fromstring(xml)
        rsc = root.find("infoFactura/razonSocialComprador")
        assert rsc.text == "JUAN & ASOCIADOS — ÑANDÚ"


# =====================================================================
# validar_estructura_factura — el "preflight" antes de firmar
# =====================================================================

class TestValidarEstructura:
    def test_xml_bien_formado_no_tiene_errores(self, info_basica, clave_acceso):
        xml = construir_xml_factura(
            clave_acceso=clave_acceso,
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        assert validar_estructura_factura(xml) == []

    def test_xml_malformado_reporta_error(self):
        errs = validar_estructura_factura("<factura><no-cerrado>")
        assert any("malformado" in e for e in errs)

    def test_xml_sin_info_tributaria_reporta(self):
        xml = '<?xml version="1.0"?><factura id="comprobante" version="1.1.0"/>'
        errs = validar_estructura_factura(xml)
        assert any("infoTributaria" in e for e in errs)

    def test_version_incorrecta_reporta(self):
        xml = '<?xml version="1.0"?><factura id="comprobante" version="2.0.0"/>'
        errs = validar_estructura_factura(xml)
        assert any("1.1.0" in e for e in errs)

    def test_clave_acceso_de_48_digitos_reporta(self, info_basica):
        # Armamos manualmente un XML con clave mala
        xml = construir_xml_factura(
            clave_acceso="1" * 49,  # primero armamos OK
            ambiente="1", tipo_emision="1",
            estab="001", pto_emi="001", secuencial="000000001",
            info=info_basica,
        )
        # Reemplazar por 48 dígitos
        xml_mal = xml.replace("1" * 49, "1" * 48, 1)
        errs = validar_estructura_factura(xml_mal)
        assert any("claveAcceso" in e and "49" in e for e in errs)


# =====================================================================
# envio — sólo los stubs y las URLs
# =====================================================================

class TestEnvioStubs:
    def test_url_recepcion_certificacion(self):
        assert "celcer.sri.gob.ec" in envio.url_recepcion("1")

    def test_url_recepcion_produccion(self):
        url = envio.url_recepcion("2")
        assert "cel.sri.gob.ec" in url
        assert "celcer" not in url

    def test_url_autorizacion_certificacion(self):
        assert "celcer.sri.gob.ec" in envio.url_autorizacion("1")

    def test_enviar_a_recepcion_xml_vacio_levanta(self):
        """Batch 11: envio.py ya está implementado. Reemplazamos el test de
        'stub levanta NoConfigurado' por validación de input vacío."""
        with pytest.raises(envio.EnvioFalloError):
            envio.enviar_a_recepcion(xml_firmado="", ambiente="1")

    def test_consultar_autorizacion_clave_invalida_levanta(self):
        """Batch 11: clave con longitud incorrecta = validación de input
        antes del HTTP call, falla predecible."""
        with pytest.raises(envio.EnvioFalloError):
            envio.consultar_autorizacion(clave_acceso="corto", ambiente="1")
