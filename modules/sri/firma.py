"""Firma electrónica XAdES-BES del XML SRI.

Implementación con `signxml` + `cryptography.pkcs12`. El .p12 de firma se
carga cuando se configuran las env vars (`SRI_P12_PATH`, `SRI_P12_PASSWORD`);
sin ellas, `firmar_xml` levanta `FirmaNoConfiguradaError` — NO silent-fail,
para que nadie publique facturas sin firmar creyendo que están firmadas.

El SRI pide XAdES-BES con:
    - canonicalización exclusiva (exc-c14n)
    - SHA1 digest
    - RSA-SHA1 signature method
    - tres referencias en la firma: al <factura>, a <KeyInfo>, a <SignedProperties>
    - el <ds:Signature> se embebe como hijo del root <factura>

Gotchas conocidos (documentados para cuando haya que debuggear rechazos):
    - El .p12 trae cert + private key juntos. La clave viene por mail aparte
      del emisor (BanEcuador/Security Data/Uanataca).
    - `SigningTime` en SignedProperties debe ser la hora de firmado, no la
      fecha de emisión — pueden diferir si el usuario guardó borrador.
    - Si hay más de un cert en el .p12 (CA + emisor), signxml puede agarrar
      el equivocado. Usar el cert primario, no `_extra_certs`.
    - El SRI exige caracteres latin-1 en algunos campos; el UTF-8 del XML
      previo se respeta en la firma sólo si la canonicalización es correcta.

Env vars (leídos por `firmar_xml_de_env()`):
    SRI_P12_PATH       — ruta al .p12 (local file system o Secrets Manager)
    SRI_P12_PASSWORD   — contraseña del .p12
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG = logging.getLogger("programa_core.sri.firma")


class FirmaNoConfiguradaError(NotImplementedError):
    """Se llamó a firmar_xml sin .p12 configurado.

    Hereda de NotImplementedError para que el try/except por defecto del
    app no la atrape como si fuera un error de datos — es una falla de
    configuración y tiene que llegar al operador.
    """


class FirmaFalloError(RuntimeError):
    """La firma cryptográfica misma falló (p12 corrupto, pwd mal, etc)."""


def _signxml_available() -> bool:
    """signxml es dep opcional hasta que haya .p12."""
    try:
        import signxml  # noqa: F401
        return True
    except ImportError:
        return False


def firmar_xml(
    *,
    xml_str: str,
    p12_path: str,
    p12_password: str,
) -> str:
    """Firma el XML con el .p12 dado. Devuelve el XML firmado.

    Raises:
        FirmaNoConfiguradaError: si signxml no está instalado o el .p12 no
            existe en `p12_path`.
        FirmaFalloError: si el .p12 no se puede abrir (contraseña mal), o si
            el XML de entrada no parsea.
    """
    if not xml_str or not xml_str.strip():
        raise FirmaFalloError("XML vacío — nada que firmar.")

    if not _signxml_available():
        raise FirmaNoConfiguradaError(
            "signxml no está instalado. Agregar a requirements.txt: "
            "signxml==3.2.2 ; cryptography>=42.0 ; lxml>=5.0"
        )

    p12 = Path(p12_path).expanduser()
    if not p12.exists():
        raise FirmaNoConfiguradaError(
            f"El .p12 no existe en {p12}. Configurar SRI_P12_PATH con una "
            "ruta válida, o montarlo desde Secrets Manager."
        )

    try:
        from cryptography.hazmat.primitives.serialization import pkcs12
        from lxml import etree
        from signxml import XMLSigner, methods
    except ImportError as e:
        raise FirmaNoConfiguradaError(
            f"Deps de firma faltantes: {e}. Instalar: pip install signxml cryptography lxml"
        ) from e

    try:
        with p12.open("rb") as f:
            p12_bytes = f.read()
        pwd_bytes = p12_password.encode() if isinstance(p12_password, str) else p12_password
        key, cert, _extra_certs = pkcs12.load_key_and_certificates(p12_bytes, pwd_bytes)
    except Exception as e:
        raise FirmaFalloError(f"No se pudo cargar el .p12: {e}") from e

    if key is None or cert is None:
        raise FirmaFalloError(".p12 no contiene clave privada o certificado.")

    try:
        root = etree.fromstring(xml_str.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        raise FirmaFalloError(f"XML de entrada no parsea: {e}") from e

    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    )
    try:
        signed = signer.sign(root, key=key, cert=cert)
    except Exception as e:
        raise FirmaFalloError(f"Firma falló: {e}") from e

    return etree.tostring(signed, encoding="utf-8", xml_declaration=True).decode("utf-8")


def firmar_xml_de_env(xml_str: str) -> str:
    """Helper que lee p12_path/password de env."""
    p12_path = os.environ.get("SRI_P12_PATH", "").strip()
    p12_password = os.environ.get("SRI_P12_PASSWORD", "")
    if not p12_path:
        raise FirmaNoConfiguradaError(
            "SRI_P12_PATH no está configurada. Agregar al .env antes de firmar."
        )
    return firmar_xml(xml_str=xml_str, p12_path=p12_path, p12_password=p12_password)


def firma_configurada() -> bool:
    """True si la firma está lista (deps + p12 + pwd). Útil para la UI."""
    if not _signxml_available():
        return False
    p12_path = os.environ.get("SRI_P12_PATH", "").strip()
    if not p12_path:
        return False
    try:
        return Path(p12_path).expanduser().exists()
    except OSError:
        return False
