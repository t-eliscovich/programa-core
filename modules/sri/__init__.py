"""Módulo SRI — facturación electrónica Ecuador.

Submódulos:
    core  — helpers puros (clave de acceso, validaciones, helpers de RUC).
    xml   — generación del XML conforme al XSD 2.1.0 del SRI.
    firma — (STUB) firma electrónica con .p12, pendiente de contratar certificado.
    envio — (STUB) SOAP contra SRI para recepción/autorización, pendiente de firma.
    queries — persistencia en scintela.factura_electronica.
    views — rutas HTTP (montadas en modules/facturas cuando hay botón).

Contrato:
    Este módulo opera hoy en ambiente=1 (certificación). El ambiente es un
    parámetro, no una constante global — el mismo código sirve para producción
    cuando la firma esté lista y el RUC esté habilitado.
"""
