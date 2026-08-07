"""Mandar un mail desde Programa Core. Hoy, uno solo: la nota del cierre.

TMT 2026-08-06: *"la nota diaria por mail"*, y sobre el transporte, SES.

Por qué SES y no SMTP: la app corre en un EC2 con rol IAM, así que **no hay
ninguna contraseña que guardar en ningún lado** — boto3 toma las credenciales
del rol solo. Un SMTP de Gmail habría exigido una contraseña de aplicación
metida como variable de entorno en el server, o sea un secreto más que cuidar
y que rotar.

Reglas de esta casa:

· **Fail-soft, siempre.** Esto cuelga del hilo de fondo. Un mail que no sale no
  puede tumbar la captura del cierre ni la app. Se loguea y se sigue.
· **No sabe QUÉ manda.** Recibe asunto, texto y destinatarios. Quién arma la
  nota es `informes/dia.py`; quién decide cuándo, el hilo de fondo.
· **Se apaga por entorno** (`MAIL_ENVIAR=0`) sin tocar código, igual que
  `DIA_EXPLICACION`.
"""
from __future__ import annotations

import logging
import os

_LOG = logging.getLogger("programa_core.mailer")

#: De dónde sale. Tiene que estar verificado en SES o el envío rebota.
REMITENTE_DEFAULT = "no-reply@intela.com.ec"


def _region() -> str:
    return (os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-2")


def remitente() -> str:
    """De qué dirección sale. Entorno primero, después la base.

    El entorno manda para poder pisarlo en una emergencia sin tocar datos; la
    base es lo normal, porque cambiar de dirección no puede requerir entrar al
    server (mig 0176).
    """
    del_entorno = (os.environ.get("MAIL_REMITENTE") or "").strip()
    if del_entorno:
        return del_entorno
    try:
        import db

        r = db.fetch_one("SELECT valor FROM scintela.nota_config "
                         " WHERE clave = 'remitente'")
        if r and (r.get("valor") or "").strip():
            return r["valor"].strip()
    except Exception as e:  # noqa: BLE001 -- sin base, el default
        _LOG.warning("mailer: no pude leer el remitente (%s)", e)
    return REMITENTE_DEFAULT


def guardar_remitente(correo: str) -> tuple[bool, str]:
    correo = (correo or "").strip().lower()
    if "@" not in correo or " " in correo or len(correo) > 200:
        return False, "Ese correo no parece un correo."
    try:
        import db

        db.execute("INSERT INTO scintela.nota_config (clave, valor) "
                   "VALUES ('remitente', %s) "
                   "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
                   (correo,))
        return True, f"Ahora sale de {correo}."
    except Exception as e:  # noqa: BLE001
        _LOG.warning("mailer: no pude guardar el remitente (%s)", e)
        return False, "No se pudo guardar."


def habilitado() -> bool:
    """¿Está prendido y hay con qué mandar?"""
    if os.environ.get("MAIL_ENVIAR", "1").strip() == "0":
        return False
    try:
        import boto3  # noqa: F401
    except Exception:  # noqa: BLE001 -- sin boto3 no se manda, y no es un error
        return False
    return True


def motivo_no_disponible() -> str:
    """Por qué no se puede mandar, en castellano, para mostrarlo en pantalla."""
    if os.environ.get("MAIL_ENVIAR", "1").strip() == "0":
        return "El envío está apagado por entorno (MAIL_ENVIAR=0)."
    try:
        import boto3  # noqa: F401
    except Exception:  # noqa: BLE001
        return ("Falta boto3 en el server. Entra con el requirements.txt en el "
                "próximo deploy.")
    return ""


def enviar(asunto: str, texto: str, destinatarios: list[str]) -> dict:
    """Manda UN mail de texto plano. Nunca lanza.

    Devuelve `{"ok": bool, "motivo": str, "id": str}` — el `id` es el
    MessageId de SES, que es lo único que sirve si después hay que rastrear
    por qué un mail no llegó.
    """
    res = {"ok": False, "motivo": "", "id": ""}
    destinos = [d.strip() for d in (destinatarios or []) if (d or "").strip()]
    if not destinos:
        res["motivo"] = "sin destinatarios"
        return res
    if not habilitado():
        res["motivo"] = motivo_no_disponible() or "envío no disponible"
        return res
    try:
        import boto3

        r = boto3.client("ses", region_name=_region()).send_email(
            Source=remitente(),
            Destination={"ToAddresses": destinos},
            Message={
                "Subject": {"Data": asunto[:200], "Charset": "UTF-8"},
                "Body": {"Text": {"Data": texto, "Charset": "UTF-8"}},
            },
        )
        res.update(ok=True, id=str(r.get("MessageId") or ""))
    except Exception as e:  # noqa: BLE001 -- cuelga del hilo de fondo
        _LOG.warning("mailer: no se pudo mandar (%s)", e)
        res["motivo"] = str(e)[:200]
    return res
