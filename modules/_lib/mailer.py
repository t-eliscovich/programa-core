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


def enviar(asunto: str, texto: str, destinatarios: list[str],
           html: str = "") -> dict:
    """Manda el mail: **UNO POR DESTINATARIO**. Nunca lanza.

    🚨 TMT 2026-08-12: antes mandaba UN SOLO mail con todos los destinatarios
    juntos en el `To`, y una dirección sin verificar en SES hacía rebotar el
    envío ENTERO (`MessageRejected ... Email address is not verified ...`):
    esa noche nadie recibió la nota por culpa de una sola dirección. Ahora cada
    uno tiene su envío: el que falla se cae solo.

    Y de paso deja de ser una lista de correos a la vista de todos, que para
    mandarle algo a un cliente no era una opción.

    Si viene `html`, el mail va en las DOS versiones: el cliente moderno pinta
    el HTML y el viejo —o el que tiene las imágenes y el formato apagados— lee
    el texto plano. Nunca se manda HTML solo: un mail sin alternativa de texto
    puntúa peor en los filtros de spam, y este mail ya tuvo ese problema.

    Devuelve::

        {"ok": bool, "motivo": str, "id": str,
         "enviados": int, "fallidos": int,
         "detalle": [{"correo": str, "ok": bool, "motivo": str, "id": str}]}

    ⭐ `ok` es **"salió al menos uno"**, no "salieron todos". Es a propósito:
    el que llama marca la nota como mandada y, si `ok` es False, la libera para
    reintentar — y reintentar le mandaría el mail DE NUEVO a los que ya lo
    recibieron. Un fallo parcial no se esconde: va en `motivo` con el nombre de
    quien no lo recibió, y en `fallidos`.

    El `id` es el MessageId de SES del primer envío que salió, que es lo único
    que sirve si después hay que rastrear por qué un mail no llegó; los de cada
    uno están en `detalle`.

    Nota sobre volumen: esto es un `for` sin freno. Alcanza de sobra para la
    nota del cierre (un puñado de direcciones). El día que haya que mandarle a
    los cientos de clientes hace falta mirar el límite de envío de SES, que en
    producción arranca en unos pocos mensajes por segundo.
    """
    res = {"ok": False, "motivo": "", "id": "",
           "enviados": 0, "fallidos": 0, "detalle": []}
    destinos = [d.strip() for d in (destinatarios or []) if (d or "").strip()]
    if not destinos:
        res["motivo"] = "sin destinatarios"
        return res
    if not habilitado():
        res["motivo"] = motivo_no_disponible() or "envío no disponible"
        return res

    cuerpo = {"Text": {"Data": texto, "Charset": "UTF-8"}}
    if (html or "").strip():
        cuerpo["Html"] = {"Data": html, "Charset": "UTF-8"}
    mensaje = {"Subject": {"Data": asunto[:200], "Charset": "UTF-8"},
               "Body": cuerpo}

    try:
        import boto3

        cliente = boto3.client("ses", region_name=_region())
        de = remitente()
    except Exception as e:  # noqa: BLE001 -- cuelga del hilo de fondo
        _LOG.warning("mailer: no se pudo abrir SES (%s)", e)
        res["motivo"] = str(e)[:200]
        return res

    fallaron = []
    for correo in destinos:
        una = {"correo": correo, "ok": False, "motivo": "", "id": ""}
        try:
            r = cliente.send_email(
                Source=de,
                Destination={"ToAddresses": [correo]},
                Message=mensaje,
            )
            una.update(ok=True, id=str(r.get("MessageId") or ""))
            res["enviados"] += 1
            if not res["id"]:
                res["id"] = una["id"]
        except Exception as e:  # noqa: BLE001 -- uno que falla no frena al resto
            _LOG.warning("mailer: no se pudo mandar a %s (%s)", correo, e)
            una["motivo"] = str(e)[:200]
            res["fallidos"] += 1
            fallaron.append(correo)
        res["detalle"].append(una)

    res["ok"] = res["enviados"] > 0
    if fallaron:
        # El motivo NO se pisa cuando salió alguno: es la única forma de que un
        # fallo parcial se vea. Ver el docstring.
        primero = next(d["motivo"] for d in res["detalle"] if not d["ok"])
        res["motivo"] = (f"no le llegó a {', '.join(fallaron[:5])}"
                         + (f" y {len(fallaron) - 5} más" if len(fallaron) > 5 else "")
                         + (f" ({primero})" if primero else ""))
    return res
