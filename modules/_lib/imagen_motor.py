"""Convertir una pantalla de Programa Core en una IMAGEN (PNG).

⭐ POR QUÉ EXISTE, que no es obvio teniendo `pdf_motor` al lado

TMT 2026-08-25, después de tres arreglos del botón de WhatsApp que no
alcanzaron. Alex Velastegui, con el PDF ya generado en la mano: *"desde el pdf
q genera no permite enviar por wsp"*. Tamara: *"creo que foto y compartir como
imagen si no?"*. Alex: *"es una opción sólo q la imagen es muy pequeña"*.

Ahí está las dos mitades del problema y de la solución.

**Por qué la imagen y no el PDF.** En un teléfono, mandar una FOTO es
universal: lo sabe hacer cualquiera y lo permite cualquier aparato. Mandar un
DOCUMENTO no: hay que saber que existe la carpeta Descargas y encontrar el
"adjuntar documento" de WhatsApp. Por eso el vendedor llega al PDF y se queda
ahí. No es que el programa falle en ese punto —el archivo está— es que el paso
que sigue no existe para él. Una imagen se manda con el gesto que ya usa todos
los días: mantenerla apretada y elegir WhatsApp.

**Por qué el servidor y no una captura.** La captura que sacaba Tamara sale de
la PANTALLA del teléfono, que tiene 390 px de ancho: por eso *"la imagen es muy
pequeña"*. No hay nada que agrandar después, porque los píxeles no estaban.
Acá la hoja se dibuja a `ANCHO` px de entrada, así que nace grande.

⚠ EL TECHO, que conviene tener escrito: WhatsApp recomprime las fotos y le
achica el lado largo a ~1600 px. Un estado de cuenta de hasta ~40 facturas
entra en esos 1600 de alto y llega entero; más largo que eso, WhatsApp lo
achica y la letra empieza a apretarse. Si algún día hay que mandar carteras más
largas, la salida es partir la imagen en dos, no subir `ANCHO`.

Del navegador se encarga `pdf_motor`: es el mismo Chromium, encontrado de la
misma manera, y con el mismo `SinMotor` cuando no está. Acá cambia una sola
cosa: `--screenshot` en vez de `--print-to-pdf`.

⚠ Y con eso cambia el MEDIO: `--print-to-pdf` renderiza con `@media print` y
`--screenshot` con `@media screen`. No es un detalle — es la razón por la que
la plantilla necesita saber que la están sacando como imagen (el `imagen=True`
de `estado_cuenta_lote_print.html`): el chrome de la app que `@media print`
esconde solo, en pantalla hay que esconderlo a mano.
"""

from __future__ import annotations

import io
import logging
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

from modules._lib import pdf_motor
from modules._lib.pdf_motor import SinMotor

_LOG = logging.getLogger("programa_core.imagen")

#: Ancho de la hoja, en píxeles. 1100 entra la tabla de 8 columnas del estado
#: de cuenta sin apretarla y sigue por debajo de los ~1600 que respeta WhatsApp
#: (ver el techo en el docstring). Subirlo NO mejora nada: WhatsApp lo achica.
ANCHO = 1100

#: Cuánto alto se le da a la ventana por cada fila de la tabla. Las filas reales
#: miden ~38 px; se pide de más a propósito porque lo que sobra se RECORTA y lo
#: que falta se CORTA — y un estado de cuenta cortado por abajo es un estado de
#: cuenta mal, que además se manda sin que nadie lo note.
_ALTO_POR_FILA = 46

#: Piso y techo de la ventana. El techo existe porque el bitmap se paga en
#: memoria (ancho × alto × 4 bytes) y el servidor es el mismo que atiende la
#: app: 20.000 px son ~88 MB, y ahí se corta.
_ALTO_MIN = 900
_ALTO_MAX = 20000

#: Lo que se le suma al alto por el encabezado, los totales y los dos títulos.
_ALTO_FIJO = 700

#: Margen blanco que queda abajo después de recortar. Sin esto la última línea
#: queda pegada al borde y la imagen se ve cortada aunque esté entera.
_MARGEN = 24


def disponible() -> bool:
    """Mismo navegador que el PDF, misma respuesta."""
    return pdf_motor.disponible()


def alto_para(filas: int) -> int:
    """Qué tan alta abrir la ventana para que entren `filas` renglones.

    `--screenshot` captura EXACTAMENTE la ventana: lo que no entra no sale en
    la foto y no avisa. Por eso se pide de más y después se recorta.
    """
    alto = _ALTO_FIJO + max(0, filas) * _ALTO_POR_FILA
    return max(_ALTO_MIN, min(alto, _ALTO_MAX))


def _recortar(png: bytes) -> tuple[bytes, bool]:
    """Saca el blanco que sobra abajo. Devuelve (imagen, quedó_cortada).

    Se recorta SÓLO a lo alto: el ancho es el de la hoja y achicarlo movería
    las columnas de lugar según el cliente, que es justo lo que no queremos —
    dos estados de cuenta del mismo vendedor tienen que verse iguales.

    Arriba también se recorta. El `main` de la app deja ~45 px de aire que en
    la pantalla separan del encabezado y en una foto son borde muerto: se
    llevan lugar de la miniatura del chat, que es lo único que el cliente ve
    antes de decidir si la abre.

    El segundo valor es la alarma: si lo dibujado llega hasta el último píxel
    de la ventana, es casi seguro que abajo había MÁS y la ventana lo cortó.
    """
    with Image.open(io.BytesIO(png)) as abierta:
        im = abierta.convert("RGB")
        blanco = Image.new("RGB", im.size, (255, 255, 255))
        caja = ImageChops.difference(im, blanco).getbbox()
        if caja is None:          # todo blanco: no hay nada que recortar
            return png, False
        cortada = caja[3] >= im.height - 2
        arriba = max(0, caja[1] - _MARGEN)
        abajo = min(im.height, caja[3] + _MARGEN)
        salida = io.BytesIO()
        im.crop((0, arriba, im.width, abajo)).save(
            salida, format="PNG", optimize=True)
        return salida.getvalue(), cortada


def desde_html(html: str, filas: int = 0, static_dir=None) -> bytes:
    """El HTML ya renderizado → los bytes de un PNG.

    `filas` es cuántos renglones tiene la tabla; sale de los datos, no de
    medir el HTML. Ver `alto_para`.
    """
    exe = pdf_motor.binario()
    if not exe:
        raise SinMotor(
            "El servidor no tiene un navegador instalado para generar la imagen."
        )

    static = (Path(static_dir) if static_dir
              else Path(__file__).resolve().parents[2] / "static")
    alto = alto_para(filas)
    png = _sacar_foto(exe, html, static, alto)
    imagen, cortada = _recortar(png)

    # ⭐ La red: si la hoja llegó hasta el borde de abajo, se rehace UNA vez con
    # el doble de ventana. Es barato comparado con mandarle al cliente un
    # estado de cuenta al que le faltan las últimas facturas — que ya pasó con
    # el PDF el 04/08 (ver el comentario de `overflow` en el template) y es el
    # tipo de error que nadie ve hasta que el cliente reclama.
    if cortada and alto < _ALTO_MAX:
        _LOG.warning("La imagen salió cortada a %s px; se rehace más alta.", alto)
        png = _sacar_foto(exe, html, static, min(alto * 2, _ALTO_MAX))
        imagen, _ = _recortar(png)
    return imagen


def _sacar_foto(exe: str, html: str, static: Path, alto: int) -> bytes:
    """Una corrida del navegador: HTML en disco → PNG en disco → bytes."""
    with tempfile.TemporaryDirectory(prefix="pc-img-") as tmp:
        tmpd = Path(tmp)
        entrada = tmpd / "hoja.html"
        salida = tmpd / "hoja.png"
        entrada.write_text(
            pdf_motor._para_imprimir_offline(html, static), encoding="utf-8")

        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--disable-extensions",
            # Perfil propio y descartable, por lo mismo que el PDF: una sesión
            # de Edge abierta en el servidor lockea el perfil y el headless se
            # cuelga sin decir por qué.
            f"--user-data-dir={tmpd / 'perfil'}",
            # Sin la barra de scroll dibujada encima de la última columna.
            "--hide-scrollbars",
            f"--window-size={ANCHO},{alto}",
            # ⚠ Los mismos 5000 que el PDF, y por el mismo motivo: ver el
            # comentario de `--virtual-time-budget` en pdf_motor. Bajarlo se
            # probó allá y salió al revés.
            "--virtual-time-budget=5000",
            "--run-all-compositor-stages-before-draw",
            f"--screenshot={salida}",
            entrada.resolve().as_uri(),
        ]
        try:
            subprocess.run(cmd, check=False, timeout=pdf_motor.TIMEOUT_S,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise SinMotor("El navegador tardó demasiado en sacar la imagen.") from e

        if not salida.exists() or salida.stat().st_size == 0:
            raise SinMotor("El navegador no devolvió ninguna imagen.")
        return salida.read_bytes()
