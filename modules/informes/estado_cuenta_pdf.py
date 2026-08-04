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

from modules._lib import pdf_motor

#: Lo que WhatsApp le muestra al cliente. Se ve antes de abrir el archivo, así
#: que dice de qué es y de quién, no "documento.pdf".
_PREFIJO = "Estado de cuenta"


def nombre_archivo(nombre_cliente: str, codigo_cli: str) -> str:
    """'Estado de cuenta - MARIO W INNOVANOVENTA.pdf'.

    Sin tildes ni signos: el archivo pasa por WhatsApp, mail y el disco de
    quien lo reciba, y un nombre con caracteres raros se rompe en alguno de
    los tres. Si el nombre queda vacío, cae al código: nunca un archivo sin
    nombre.
    """
    base = unicodedata.normalize("NFKD", (nombre_cliente or "").strip())
    base = base.encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9 .-]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()[:60].strip(" .-")
    return f"{_PREFIJO} - {base or (codigo_cli or 'cliente').upper()}.pdf"


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
