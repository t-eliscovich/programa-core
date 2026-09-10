"""Los datos que el cliente carga en el portal (mig 0249).

Dueña 10/09/2026: en el primer ingreso, después de elegir la clave, el
cliente completa correo para las facturas, celular, fijo y la dirección de
entrega SEGMENTADA (ciudad, calle principal, número, calle secundaria,
referencia). Es obligatorio: hasta que no lo completa no ve el resto del
portal. Después lo edita desde Perfil.

Reglas ("que no lo dejen vacío o alguna restricción así"):
  · correo con forma de correo;
  · celular de Ecuador: 10 dígitos empezando en 09 (o +593 9…); el fijo es
    opcional, y si viene, 7 a 10 dígitos;
  · ciudad, calle principal, número y calle secundaria obligatorios (mínimo
    2 letras); "S/N" vale como número; referencia opcional.

Se guarda SIEMPRE aparte de la ficha (`portal_datos_cliente`): lo que el
cliente dijo, con la foto de lo que teníamos (`previo`). La oficina decide
qué pasar a la ficha desde /clientes/datos-del-portal.
"""
from __future__ import annotations

import json
import logging
import re

import db

_LOG = logging.getLogger("programa_core.portal")

CAMPOS = ("correo_facturas", "celular", "telefono", "ciudad", "calle_principal",
          "numero", "calle_secundaria", "referencia")
OBLIGATORIOS = ("correo_facturas", "celular", "ciudad", "calle_principal", "numero",
                "calle_secundaria")
LARGOS = {"correo_facturas": 120, "celular": 20, "telefono": 20, "ciudad": 60,
          "calle_principal": 120, "numero": 20, "calle_secundaria": 120, "referencia": 200}

_MAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _digitos(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def celular_ok(v: str) -> str:
    """Devuelve el celular normalizado (09XXXXXXXX) o '' si no sirve."""
    d = _digitos(v)
    if d.startswith("593"):
        d = "0" + d[3:]
    return d if len(d) == 10 and d.startswith("09") else ""


def fijo_ok(v: str) -> str:
    """Un fijo de Ecuador: 7 dígitos (local) a 10 (con código de área). '' si
    no sirve. Vacío es válido: no todos tienen."""
    d = _digitos(v)
    if d.startswith("593"):
        d = "0" + d[3:]
    return d if 7 <= len(d) <= 10 else ""


def validar(form) -> tuple[dict, dict]:
    """``(datos, errores)``: los datos limpios y, por campo, qué está mal.
    Sin errores, los datos están listos para guardar."""
    datos = {c: (form.get(c) or "").strip()[:LARGOS[c]] for c in CAMPOS}
    errores: dict[str, str] = {}
    for c in OBLIGATORIOS:
        if len(datos[c]) < 2:
            errores[c] = "Falta completar."
    if datos["correo_facturas"] and not _MAIL_RE.match(datos["correo_facturas"]):
        errores["correo_facturas"] = "No parece un correo."
    if datos["celular"] and not celular_ok(datos["celular"]):
        errores["celular"] = "Tiene que ser un celular de Ecuador: 09 y 8 números más."
    if datos["telefono"] and not fijo_ok(datos["telefono"]):
        errores["telefono"] = "Un fijo tiene entre 7 y 10 números (o déjelo vacío)."
    if not errores:
        datos["celular"] = celular_ok(datos["celular"])
        datos["telefono"] = fijo_ok(datos["telefono"]) if datos["telefono"] else ""
        datos["correo_facturas"] = datos["correo_facturas"].lower()
    return datos, errores


def leer(cod: str) -> dict | None:
    try:
        return db.fetch_one(
            "SELECT * FROM scintela.portal_datos_cliente WHERE UPPER(TRIM(codigo_cli)) = %s",
            ((cod or "").strip().upper(),))
    except Exception as e:  # noqa: BLE001 -- sin la tabla (mig 0249), sin datos
        _LOG.warning("portal: no pude leer los datos cargados de %s (%s)", cod, e)
        return None


def completo(cod: str) -> bool:
    """¿Ya cargó sus datos? Si la tabla no está (la mig 0249 todavía no
    corrió) o la base no contesta, se deja pasar: el freno nunca puede dejar
    a todos los clientes afuera por plomería."""
    try:
        return db.fetch_one(
            "SELECT 1 FROM scintela.portal_datos_cliente WHERE UPPER(TRIM(codigo_cli)) = %s",
            ((cod or "").strip().upper(),)) is not None
    except Exception as e:  # noqa: BLE001
        _LOG.warning("portal: no pude saber si %s cargó sus datos (%s)", cod, e)
        return True


def precarga(fic: dict, mail_portal: str = "") -> dict:
    """Lo que teníamos, para que corrija y no tipee de cero. La dirección
    vieja viene en un solo campo (del dBase): va entera en "calle principal"
    y el cliente la reparte — adivinar el número y la calle B de un texto
    tipeado se rompe seguro."""
    fic = fic or {}
    tel = (fic.get("telefono") or "").strip()
    return {
        "correo_facturas": (mail_portal or fic.get("correo") or "").strip(),
        "celular": tel if celular_ok(tel) else "",
        "telefono": "" if celular_ok(tel) else (tel if fijo_ok(tel) else ""),
        "ciudad": (fic.get("canton") or "").strip().title(),
        "calle_principal": (fic.get("direccion1") or "").strip(),
        "numero": "",
        "calle_secundaria": "",
        "referencia": (fic.get("direccion2") or "").strip()
        if (fic.get("direccion2") or "").strip().upper() not in (fic.get("direccion1") or "").upper()
        else "",
    }


def guardar(cod: str, datos: dict, previo: dict | None = None) -> None:
    """Crea o actualiza la fila del cliente. `previo` (la ficha de hoy) se
    guarda sólo la primera vez: es la foto contra la que se compara."""
    cod = (cod or "").strip().upper()
    db.execute(
        """
        INSERT INTO scintela.portal_datos_cliente
               (codigo_cli, correo_facturas, celular, telefono, ciudad,
                calle_principal, numero, calle_secundaria, referencia, previo)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (codigo_cli) DO UPDATE SET
               correo_facturas  = EXCLUDED.correo_facturas,
               celular          = EXCLUDED.celular,
               telefono         = EXCLUDED.telefono,
               ciudad           = EXCLUDED.ciudad,
               calle_principal  = EXCLUDED.calle_principal,
               numero           = EXCLUDED.numero,
               calle_secundaria = EXCLUDED.calle_secundaria,
               referencia       = EXCLUDED.referencia,
               actualizado_en   = now(),
               aplicado_en      = NULL,
               aplicado_por     = NULL
        """,
        (cod, datos["correo_facturas"], datos["celular"], datos.get("telefono") or None,
         datos["ciudad"], datos["calle_principal"], datos["numero"], datos["calle_secundaria"],
         datos.get("referencia") or None,
         json.dumps({k: (previo or {}).get(k) or "" for k in
                     ("correo", "telefono", "direccion1", "direccion2", "canton", "provincia")},
                    ensure_ascii=False)),
    )


def direccion_en_una_linea(d: dict) -> str:
    """Cómo se escribe la dirección en Ecuador: 'Calle A N° y Calle B, ref'."""
    partes = f"{d.get('calle_principal') or ''} {d.get('numero') or ''}".strip()
    if d.get("calle_secundaria"):
        partes += f" y {d['calle_secundaria']}"
    if d.get("referencia"):
        partes += f", {d['referencia']}"
    return partes.strip()


# ---------------------------------------------------------------------------
# La oficina: qué cargaron, qué cambió, pasar a la ficha, Excel para Asinfo
# ---------------------------------------------------------------------------

def cargados() -> list[dict]:
    """Todo lo que cargaron los clientes, con lo que tiene la ficha HOY al
    lado, para ver qué cambió. Del más reciente al más viejo."""
    return db.fetch_all(
        """
        SELECT d.*, COALESCE(c.nombre, '') AS nombre, COALESCE(c.vend, '') AS vend,
               COALESCE(c.correo, '')     AS ficha_correo,
               COALESCE(c.telefono, '')   AS ficha_telefono,
               COALESCE(c.direccion1, '') AS ficha_direccion1,
               COALESCE(c.direccion2, '') AS ficha_direccion2,
               COALESCE(c.canton, '')     AS ficha_canton
          FROM scintela.portal_datos_cliente d
          LEFT JOIN scintela.cliente c ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(d.codigo_cli))
         ORDER BY d.actualizado_en DESC
        """
    ) or []


def con_diferencias(fila: dict) -> dict:
    """Qué difiere entre lo cargado y la ficha: correo y teléfono (que la
    oficina puede pasar a la ficha) y la dirección (que va a Asinfo por
    plantilla: el sync la pisa cada hora, ver clientes-sync-asinfo)."""
    tel_ficha = _digitos(fila.get("ficha_telefono") or "")
    return {
        "correo": (fila.get("correo_facturas") or "").strip().lower()
                  != (fila.get("ficha_correo") or "").strip().lower(),
        "telefono": tel_ficha not in (fila.get("celular") or "", fila.get("telefono") or ""),
        "direccion": direccion_en_una_linea(fila).upper()
                     != (fila.get("ficha_direccion1") or "").strip().upper(),
    }


def pasar_a_la_ficha(cod: str, usuario: str) -> int:
    """Pone en la ficha de Programa Core el correo y el celular que cargó el
    cliente (lo que el sync de Asinfo NO pisa). La dirección no: la pisa el
    sync cada hora, tiene que entrar por Asinfo."""
    from modules.clientes import queries as cq

    d = leer(cod)
    if not d:
        return 0
    n = cq.editar(cod, correo=d["correo_facturas"], telefono=d["celular"], usuario=usuario)
    db.execute(
        "UPDATE scintela.portal_datos_cliente SET aplicado_en = now(), aplicado_por = %s "
        " WHERE UPPER(TRIM(codigo_cli)) = %s",
        ((usuario or "")[:30], (cod or "").strip().upper()))
    return n


COLUMNAS_EXCEL = (
    ("codigo_cli", "Código"), ("nombre", "Cliente"), ("correo_facturas", "Correo facturas"),
    ("celular", "Celular"), ("telefono", "Teléfono fijo"), ("ciudad", "Ciudad"),
    ("calle_principal", "Calle principal"), ("numero", "Número"),
    ("calle_secundaria", "Calle secundaria"), ("referencia", "Referencia"),
    ("actualizado_en", "Cargado el"),
)


def excel(filas: list[dict]) -> bytes:
    """La plantilla para Asinfo: una fila por cliente, la dirección
    SEGMENTADA en columnas (calle A · número · calle B · referencia). Cuando
    Asinfo mande su plantilla, se ajustan los encabezados acá."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Datos del portal"
    ws.append([t for _, t in COLUMNAS_EXCEL])
    for c in ws[1]:
        c.font = Font(bold=True)
    for f in filas:
        fila = []
        for k, _ in COLUMNAS_EXCEL:
            v = f.get(k)
            if k == "actualizado_en" and v is not None:
                v = v.strftime("%d/%m/%Y %H:%M") if hasattr(v, "strftime") else str(v)
            fila.append(v if v is not None else "")
        ws.append(fila)
    for col, ancho in zip(ws.columns, (9, 34, 30, 13, 13, 16, 30, 12, 30, 34, 17), strict=False):
        ws.column_dimensions[col[0].column_letter].width = ancho
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
