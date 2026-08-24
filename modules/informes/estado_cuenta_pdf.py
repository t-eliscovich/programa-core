"""El estado de cuenta de un cliente, como PDF, para mandárselo por WhatsApp.

TMT 2026-08-04 (dueña): quería el botón para el vendedor en la calle, y a los
dos minutos: *"dejá esto de enviar por WhatsApp para todos los usuarios, no
sólo vendedores — quizás Alex le puede mandar al cliente también"*. Así que la
generación vive acá, en `informes`, que es de donde sale el estado de cuenta
de la oficina, y la usan las dos pantallas:

    /informes/estado-cuenta/<cod>/pdf   → cualquier usuario logueado (Alex)
    /mi-cartera/cliente/<cod>/pdf       → el vendedor, acotado a SUS clientes

La única diferencia entre las dos es de dónde sale el permiso para ver a ese
cliente. El PDF es el mismo archivo, byte por byte, porque sale del mismo
template que ya se imprime — ver `pdf_motor` para por qué se genera con un
navegador y no con una librería de PDF.
"""

from __future__ import annotations

import re
import unicodedata

from flask import render_template

from filters import today_ec
from modules._lib import pdf_motor

#: Lo que WhatsApp le muestra al cliente. Se ve antes de abrir el archivo, así
#: que dice de qué es y de quién, no "documento.pdf".
_PREFIJO = "Estado de cuenta"


def nombre_archivo(nombre_cliente: str, codigo_cli: str) -> str:
    """'Estado de cuenta MWI 24-08-2026.pdf'.

    ⭐ TMT 2026-08-24: *"cuando descargan el archivo tiene que tener de nombre
    ese archivo el código del cliente y el día"*.

    El motivo es el de siempre con estos archivos: no se abren de a uno. El
    vendedor manda cinco estados de cuenta en una tarde y en el celular de
    quien los recibe —y en la carpeta de descargas del que los manda— quedan
    todos con el mismo nombre salvo por el nombre largo del cliente. Con el
    CÓDIGO adelante se ordenan solos y se reconocen sin abrirlos; con la FECHA
    se distingue el que se mandó hoy del que se mandó la semana pasada, que es
    la pregunta que aparece cuando el cliente discute un saldo.

    La fecha va dd-mm-aaaa y no con barras: la barra es separador de carpetas
    y rompe el archivo en Android, en Windows y en el mail.

    ⚠ El nombre largo del cliente NO va. La primera versión lo dejaba en el
    medio ("...MWI MARIO W INNOVANOVENTA S.A 24-08-2026.pdf") pensando en que
    es lo que el cliente ve en el chat antes de abrirlo, y Tamara lo cortó ese
    mismo día: *"que el archivo que mando sea cod de cliente y día"*. Con el
    nombre adentro el archivo mide 60 caracteres y WhatsApp lo muestra
    cortado, justo por el final — que es donde está la fecha.

    `nombre_cliente` se sigue recibiendo y se ignora a propósito: la firma la
    usan las dos rutas (la oficina y el portal) y sacarla no agrega nada.

    Sin tildes ni signos: el archivo pasa por WhatsApp, mail y el disco de
    quien lo reciba, y un nombre con caracteres raros se rompe en alguno de
    los tres. Sin código queda "CLIENTE": nunca un archivo sin identificar.
    """
    # El código son 3 letras: se le saca TODO lo que no sea letra o número
    # (una barra en el nombre de un archivo es un separador de carpetas).
    cod = re.sub(r"[^A-Za-z0-9]", "", _limpiar(codigo_cli)).upper() or "CLIENTE"
    dia = today_ec().strftime("%d-%m-%Y")
    return f"{_PREFIJO} {cod} {dia}.pdf"


def _limpiar(texto: str) -> str:
    """Sin tildes, sin signos y sin espacios de más."""
    t = unicodedata.normalize("NFKD", (texto or "").strip())
    t = t.encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^A-Za-z0-9 .-]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def generar(data: dict) -> bytes:
    """`estado_cuenta_cliente(...)` → los bytes del PDF.

    Se renderiza el MISMO template que la impresión en lote —el que ya usan la
    oficina y el portal— con un solo cliente adentro. No hay una plantilla
    "para PDF": si mañana se corrige la hoja, el PDF se corrige solo.
    """
    cli = data.get("cliente") or {}
    html = render_template(
        "informes/estado_cuenta_lote_print.html",
        clientes=[data],
        titulo=f"{cli.get('nombre') or ''} ({cli.get('codigo_cli') or ''})".strip(),
        por="vendedor",
        n=1,
    )
    return pdf_motor.desde_html(html)
