"""El estado de cuenta de un cliente, como IMAGEN, para mandarlo por WhatsApp.

TMT 2026-08-25, con Alex Velastegui por WhatsApp:

    Alex   — *"desde el pdf q genera no permite enviar por wsp"*
    Tamara — *"creo que foto y compartir como imagen si no?"*
    Alex   — *"es una opción sólo q la imagen es muy pequeña"*
    Tamara — *"agrando la imagen"*

Eso es exactamente esta función: la idea es de Tamara y el tamaño lo pone el
servidor en vez de la mano. El POR QUÉ una imagen le gana al PDF en un teléfono
está escrito en `imagen_motor`.

Es el hermano de `estado_cuenta_pdf` y comparte todo lo que se puede compartir:
la misma plantilla, el mismo criterio de nombre de archivo, el mismo navegador
y el mismo `SinMotor`. Cambia el formato y nada más. Las dos pantallas la usan:

    /informes/estado-cuenta/<cod>/imagen   → cualquier usuario logueado (Alex)
    /mi-cartera/cliente/<cod>/imagen       → el vendedor, acotado a SUS clientes
"""

from __future__ import annotations

from flask import render_template

from filters import today_ec
from modules._lib import imagen_motor
from modules.informes import estado_cuenta_pdf


def nombre_archivo(nombre_cliente: str, codigo_cli: str) -> str:
    """'Estado de cuenta MWI 25-08-2026.png'.

    El mismo criterio que el PDF —código y día, sin el nombre largo— por el
    mismo motivo, que está explicado entero en `estado_cuenta_pdf`: estos
    archivos no se abren de a uno. Se delega en vez de copiarse: si mañana se
    cambia cómo se nombran, se cambia en un solo lugar.
    """
    return estado_cuenta_pdf.nombre_archivo(nombre_cliente, codigo_cli, ext="png")


def cuantas_filas(data: dict) -> int:
    """Cuántos renglones va a tener la hoja.

    `imagen_motor` necesita saberlo ANTES de dibujar, porque la foto captura
    exactamente el alto de la ventana y lo que no entra se pierde sin avisar.
    Salen de los datos —que es lo único que se sabe de antemano— y se cuentan
    de más, no de menos.
    """
    return len(data.get("facturas") or []) + len(data.get("cheques") or [])


def generar(data: dict) -> bytes:
    """`estado_cuenta_cliente(...)` → los bytes de un PNG.

    Se renderiza el MISMO template que la impresión y que el PDF, con
    `imagen=True`. No hay una plantilla "para imagen": si mañana se corrige la
    hoja, la imagen se corrige sola. Lo único que hace ese flag es esconder el
    chrome de la app, que `@media print` esconde solo y `@media screen` no —
    ver `imagen_motor` para por qué la imagen se saca en modo pantalla.
    """
    cli = data.get("cliente") or {}
    html = render_template(
        "informes/estado_cuenta_lote_print.html",
        clientes=[data],
        titulo=f"{cli.get('nombre') or ''} ({cli.get('codigo_cli') or ''})".strip(),
        por="vendedor",
        n=1,
        imagen=True,
        # ⭐ El día, para el encabezado de la foto. TMT 2026-08-25: *"¿el nombre
        # es el mismo?"*. El archivo se llama igual que el PDF —código y día—
        # pero WhatsApp muestra el nombre de un DOCUMENTO y no el de una FOTO,
        # así que al mandar la imagen el día deja de verse. Adentro de la foto
        # sí se ve, y encima sin abrir nada: queda mejor que el nombre.
        #
        # Va sólo en la imagen. El papel no se toca: es la hoja que la oficina
        # usa todos los días y su fecha la pone la impresora.
        hoy=today_ec(),
    )
    return imagen_motor.desde_html(html, filas=cuantas_filas(data))
