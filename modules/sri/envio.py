"""Envío SOAP del XML firmado al SRI.

Implementación con `requests` + envelope SOAP a mano. Elegimos requests en
lugar de zeep porque:
    - zeep parsea el WSDL al import, lo que agrega 5-10s al primer request y
      rompe tests que no tienen internet.
    - El WSDL del SRI cambia rara vez; escribir el envelope a mano es ~30
      líneas y no tiene magia.
    - Manejar reintentos + timeout + rate limit es más transparente.

El SRI expone dos web services por comprobante:
    - Recepción     → POST XML firmado. Responde RECIBIDA | DEVUELTA.
    - Autorización  → POLL por clave de acceso. Responde AUTORIZADO | NO AUTORIZADO | EN PROCESO.

Env vars:
    SRI_AMBIENTE                 — '1' certificación (default), '2' producción.
    SRI_HTTP_TIMEOUT_SEG         — default 30. El WS a veces es lento.
    SRI_REINTENTOS               — default 3. Para transient errors (5xx, timeout).
    SRI_REINTENTO_BACKOFF_SEG    — default 2. Crece exponencial: 2, 4, 8.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
from datetime import datetime

_LOG = logging.getLogger("programa_core.sri.envio")


# URLs por ambiente — expuestas para tests y para documentación.
WS_RECEPCION_CERT = (
    "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/"
    "RecepcionComprobantesOffline"
)
WS_AUTORIZACION_CERT = (
    "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/"
    "AutorizacionComprobantesOffline"
)
WS_RECEPCION_PROD = (
    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/"
    "RecepcionComprobantesOffline"
)
WS_AUTORIZACION_PROD = (
    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/"
    "AutorizacionComprobantesOffline"
)


class EnvioNoConfiguradoError(NotImplementedError):
    """requests no disponible o configuración inválida."""


class EnvioFalloError(RuntimeError):
    """El WS del SRI rechazó el request. Ver .mensajes."""


def url_recepcion(ambiente: str) -> str:
    return WS_RECEPCION_CERT if ambiente == "1" else WS_RECEPCION_PROD


def url_autorizacion(ambiente: str) -> str:
    return WS_AUTORIZACION_CERT if ambiente == "1" else WS_AUTORIZACION_PROD


def _timeout_seg() -> int:
    try:
        return int(os.environ.get("SRI_HTTP_TIMEOUT_SEG", "30"))
    except ValueError:
        return 30


def _reintentos() -> int:
    try:
        return int(os.environ.get("SRI_REINTENTOS", "3"))
    except ValueError:
        return 3


def _backoff_seg() -> int:
    try:
        return int(os.environ.get("SRI_REINTENTO_BACKOFF_SEG", "2"))
    except ValueError:
        return 2


# Envelopes SOAP ——————————————————————————————————————————————————————————————
#
# Nota: el SRI espera el XML firmado codificado en base64 dentro de <xml>. El
# namespace del WS cambió alguna vez; si rompe un futuro cambio, comparar con
# el WSDL en vivo.

_ENVELOPE_RECEPCION = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ec="http://ec.gob.sri.ws.recepcion">
  <soapenv:Header/>
  <soapenv:Body>
    <ec:validarComprobante>
      <xml>{xml_b64}</xml>
    </ec:validarComprobante>
  </soapenv:Body>
</soapenv:Envelope>"""

_ENVELOPE_AUTORIZACION = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ec="http://ec.gob.sri.ws.autorizacion">
  <soapenv:Header/>
  <soapenv:Body>
    <ec:autorizacionComprobante>
      <claveAccesoComprobante>{clave}</claveAccesoComprobante>
    </ec:autorizacionComprobante>
  </soapenv:Body>
</soapenv:Envelope>"""


def _http_post(url: str, envelope: str) -> str:
    """POST SOAP con reintentos exponenciales. Devuelve el body como str.

    Reintenta sólo en 5xx, ConnectionError, Timeout — NO en 4xx, que son
    errores del cliente (envelope malformado) y reintentar no arregla.
    """
    try:
        import requests
    except ImportError as e:
        raise EnvioNoConfiguradoError(
            "requests no está instalado. Agregar a requirements.txt: requests>=2.32"
        ) from e

    headers = {
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": "",
    }
    intentos = _reintentos()
    backoff = _backoff_seg()
    ultimo: Exception | None = None
    for i in range(intentos):
        try:
            resp = requests.post(
                url, data=envelope.encode("utf-8"),
                headers=headers, timeout=_timeout_seg(),
            )
            if resp.status_code == 200:
                return resp.text
            if 500 <= resp.status_code < 600:
                ultimo = EnvioFalloError(f"SRI 5xx: {resp.status_code} body={resp.text[:400]}")
            else:
                # 4xx: error del cliente, no reintentar.
                raise EnvioFalloError(
                    f"SRI {resp.status_code}: {resp.text[:400]}"
                )
        except requests.Timeout as e:
            ultimo = e
            _LOG.warning("SRI timeout intento %d/%d", i + 1, intentos)
        except requests.ConnectionError as e:
            ultimo = e
            _LOG.warning("SRI connection error intento %d/%d: %s", i + 1, intentos, e)
        if i < intentos - 1:
            time.sleep(backoff * (2 ** i))
    raise EnvioFalloError(f"SRI sin respuesta tras {intentos} intentos: {ultimo}")


def enviar_a_recepcion(*, xml_firmado: str, ambiente: str = "1") -> dict:
    """POST del XML firmado al endpoint de Recepción.

    Returns:
        { estado: 'RECIBIDA' | 'DEVUELTA', mensajes: [...], raw: str }

    Raises:
        EnvioNoConfiguradoError: requests no instalado.
        EnvioFalloError: HTTP 4xx, o sin respuesta tras reintentos.
    """
    if not xml_firmado:
        raise EnvioFalloError("XML firmado vacío.")
    xml_b64 = base64.b64encode(xml_firmado.encode("utf-8")).decode("ascii")
    envelope = _ENVELOPE_RECEPCION.format(xml_b64=xml_b64)
    body = _http_post(url_recepcion(ambiente), envelope)
    return _parse_recepcion(body)


def consultar_autorizacion(*, clave_acceso: str, ambiente: str = "1") -> dict:
    """GET-style POLL por clave de acceso.

    Returns:
        { estado: 'AUTORIZADO' | 'NO AUTORIZADO' | 'EN PROCESO',
          numero_autorizacion: str | None,
          fecha_autorizacion: datetime | None,
          mensajes: [...],
          raw: str }
    """
    if not clave_acceso or len(clave_acceso) != 49:
        raise EnvioFalloError(f"clave_acceso inválida: {clave_acceso!r} (esperado 49 dígitos)")
    envelope = _ENVELOPE_AUTORIZACION.format(clave=clave_acceso)
    body = _http_post(url_autorizacion(ambiente), envelope)
    return _parse_autorizacion(body)


# Parsing de respuestas —————————————————————————————————————————————————————
#
# El SRI devuelve XML con un namespace que varía. Usamos regex para buscar
# tags específicos — parser minimal y explícito.

_RE_ESTADO = re.compile(r"<estado>([^<]+)</estado>", re.I)
_RE_NUM_AUT = re.compile(r"<numeroAutorizacion>([^<]+)</numeroAutorizacion>", re.I)
_RE_FECHA_AUT = re.compile(r"<fechaAutorizacion>([^<]+)</fechaAutorizacion>", re.I)
_RE_MSG = re.compile(
    r"<mensaje>\s*<identificador>([^<]*)</identificador>\s*"
    r"<mensaje>([^<]*)</mensaje>\s*"
    r"(?:<informacionAdicional>([^<]*)</informacionAdicional>\s*)?"
    r"(?:<tipo>([^<]*)</tipo>\s*)?"
    r"</mensaje>",
    re.I | re.S,
)


def _parse_mensajes(body: str) -> list[dict]:
    msgs = []
    for m in _RE_MSG.finditer(body):
        msgs.append({
            "identificador": (m.group(1) or "").strip(),
            "mensaje": (m.group(2) or "").strip(),
            "informacionAdicional": (m.group(3) or "").strip() if m.group(3) else "",
            "tipo": (m.group(4) or "").strip() if m.group(4) else "",
        })
    return msgs


def _parse_recepcion(body: str) -> dict:
    m = _RE_ESTADO.search(body)
    estado = m.group(1).strip().upper() if m else "DESCONOCIDO"
    return {
        "estado": estado,       # RECIBIDA | DEVUELTA | DESCONOCIDO
        "mensajes": _parse_mensajes(body),
        "raw": body,
    }


def _parse_autorizacion(body: str) -> dict:
    m_est = _RE_ESTADO.search(body)
    estado = m_est.group(1).strip().upper() if m_est else "DESCONOCIDO"
    m_num = _RE_NUM_AUT.search(body)
    num = m_num.group(1).strip() if m_num else None
    m_fec = _RE_FECHA_AUT.search(body)
    fecha: datetime | None = None
    if m_fec:
        raw = m_fec.group(1).strip()
        # SRI devuelve "2026-04-17T14:35:12.345-05:00" o similar. Parseamos
        # lo más tolerante posible.
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                fecha = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    return {
        "estado": estado,   # AUTORIZADO | NO AUTORIZADO | EN PROCESO | DESCONOCIDO
        "numero_autorizacion": num,
        "fecha_autorizacion": fecha,
        "mensajes": _parse_mensajes(body),
        "raw": body,
    }
