"""Convertir una pantalla de Programa Core en PDF, sin escribir una segunda hoja.

TMT 2026-08-04 (dueña, sobre el portal de vendedores): quiere poder mandarle
el estado de cuenta al cliente por WhatsApp. En la calle no hay impresora; hay
WhatsApp.

⭐ POR QUÉ ESTE MÓDULO EXISTE Y NO UNA LIBRERÍA DE PDF

El primer intento fue armar el PDF en el celular (html2canvas + jsPDF). Anda,
y está mal: la hoja linda de la oficina vive entera en `@media print` —las
reglas de `estado_cuenta_lote_print.html`, 7,5pt, bordes finos, la tabla
densa— y html2canvas fotografía la pantalla, no el papel. Para que el PDF
saliera igual habría que copiar todas esas reglas a un `.como-impreso`, o sea
tener DOS versiones de la misma hoja. Dos plantillas divergen a la primera
corrección que se le hace a una sola, y esta hoja es la que se le manda al
cliente. Medido, además: salía rasterizado, 486 KB por dos páginas, sin texto
seleccionable, y con la columna Acum. cortada.

Lo que sí sabe imprimir esa hoja tal cual es un navegador, porque
`--print-to-pdf` renderiza con la hoja de estilos de IMPRESIÓN. Así que el PDF
lo hace un Chromium en modo headless a partir del MISMO HTML que ya
generamos. No hay segunda plantilla, no hay segunda verdad, y el archivo tiene
texto de verdad.

No agrega una dependencia pesada: el servidor es Windows Server y trae
Microsoft Edge (Chromium) de fábrica. Si mañana se muda a Linux, la lista de
candidatos ya contempla chromium/chrome. Y si no hay ninguno, el módulo lo
dice en vez de romper: `disponible()` es False y la pantalla esconde el botón.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_LOG = logging.getLogger("programa_core.pdf")

#: Escape hatch: si el binario está en un lugar raro, se setea por entorno y
#: no hay que tocar código.
VAR_ENTORNO = "PDF_MOTOR_BIN"

#: Windows Server trae Edge instalado; Chrome se contempla por si acaso.
_CANDIDATOS_WINDOWS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

#: En Linux/Mac se busca en el PATH.
_CANDIDATOS_POSIX = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "microsoft-edge", "microsoft-edge-stable",
)

#: Cuánto tarda en darse por vencido. Un estado de cuenta de 17 facturas tarda
#: ~1,5 s; el tope está para que un headless colgado no se lleve puesto un
#: worker de la app.
TIMEOUT_S = 30.0

#: Cuánto tiempo (de reloj de la página, no de reloj de pared) se le da al
#: navegador antes de imprimir.
#:
#: 🚨 TMT 2026-08-24: *"el botón mandar por whatsapp tarda mucho"*. Medido en
#: producción sobre /mi-cartera/cliente/ATE/pdf (13 facturas, 88 kB): **3,5 a
#: 5,2 segundos**. De eso, traer los datos y armar el HTML son **170 ms** — o
#: sea que TODO el tiempo es el navegador.
#:
#: Estaba en 5000. La hoja que se imprime no tiene ni una imagen, ni una
#: llamada de red (los `<script src="https://…">` se sacan antes, ver
#: `_para_imprimir_offline`), ni un `setTimeout`: son datos y CSS. Nada de eso
#: necesita cinco segundos de reloj virtual, y `--run-all-compositor-stages-
#: before-draw` hace que el proceso se quede esperando a que el presupuesto se
#: agote en vez de salir apenas la página está quieta.
#:
#: ⚠ Es un TECHO, no una espera fija: si la página termina antes, imprime
#: antes. Bajarlo no puede cortar una hoja a la mitad; lo que podría hacer, si
#: algún día la hoja incorporara algo lento, es imprimirla incompleta. Por eso
#: 2000 y no 500: sigue siendo diez veces lo que tarda en asentarse.
PRESUPUESTO_MS = 2000

#: Lo que el navegador hace al arrancar y que acá no sirve para nada: buscar
#: actualizaciones, sincronizar, mandar métricas, preguntar si quiere ser el
#: navegador por omisión. En un headless que vive tres segundos y se muere,
#: cada una de esas cosas es tiempo de arranque puro.
ARRANQUE_CALLADO = (
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-dev-shm-usage",
    "--disable-search-engine-choice-screen",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-default-browser-check",
    "--no-pings",
    "--disable-features=Translate,MediaRouter,OptimizationHints,AcceptCHFrame",
)

# La misma lección que la caché de la columna `vend` (2026-08-03) y que la del
# fracaso de Metabase (2026-07-29): un NO no puede vivir tanto como un SÍ. Si
# alguien instala Edge en el servidor, la app tiene que enterarse sola; si ya
# lo encontró, no hace falta volver a mirar el disco en cada request.
_BIN: str | None = None
_BIN_BUSCADO_EN: float = 0.0
TTL_NEGATIVO_S = 60.0


class SinMotor(RuntimeError):
    """No hay ningún navegador en el servidor para imprimir el PDF."""


def _resetear_cache() -> None:
    """Sólo para los tests."""
    global _BIN, _BIN_BUSCADO_EN
    _BIN, _BIN_BUSCADO_EN = None, 0.0


def binario() -> str | None:
    """Ruta al navegador que va a imprimir, o None si no hay ninguno."""
    global _BIN, _BIN_BUSCADO_EN
    if _BIN:
        return _BIN
    if _BIN_BUSCADO_EN and (time.monotonic() - _BIN_BUSCADO_EN) < TTL_NEGATIVO_S:
        return None

    _BIN_BUSCADO_EN = time.monotonic()
    forzado = (os.environ.get(VAR_ENTORNO) or "").strip()
    if forzado and Path(forzado).exists():
        _BIN = forzado
        return _BIN

    candidatos = _CANDIDATOS_WINDOWS if sys.platform.startswith("win") else ()
    for ruta in candidatos:
        if Path(ruta).exists():
            _BIN = ruta
            return _BIN
    for nombre in _CANDIDATOS_POSIX:
        ruta = shutil.which(nombre)
        if ruta:
            _BIN = ruta
            return _BIN

    _LOG.warning("No hay navegador para generar PDFs (probé %s)", VAR_ENTORNO)
    return None


def disponible() -> bool:
    """¿Se puede generar un PDF en este servidor? La pantalla pregunta esto
    antes de dibujar el botón: un botón que siempre falla es peor que no
    tenerlo."""
    return binario() is not None


def _para_imprimir_offline(html: str, static_dir: Path) -> str:
    """Deja el HTML listo para abrirse desde el disco, sin red.

    Dos cambios, los dos necesarios:

    · `/static/tailwind.css` es una ruta del servidor web; el navegador va a
      abrir un `file://`, así que se reescribe a la ruta absoluta en disco. Sin
      esto el PDF sale sin una sola regla de estilo — texto negro apilado.

    · Los `<script src="https://…">` (htmx viene de unpkg) se sacan. El
      headless corre en el servidor, que puede no tener salida a internet, y
      esperar a un script que nunca llega es la forma más común de que un
      render se quede colgado hasta el timeout. Para imprimir no hacen falta.
    """
    base = static_dir.resolve().as_uri().rstrip("/")
    html = html.replace('href="/static/', f'href="{base}/')
    html = html.replace('src="/static/', f'src="{base}/')
    return re.sub(
        r'<script[^>]+src="https?://[^"]*"[^>]*>\s*</script>', "", html,
        flags=re.IGNORECASE,
    )


def desde_html(html: str, static_dir: str | os.PathLike | None = None) -> bytes:
    """El HTML ya renderizado → los bytes de un PDF, con la hoja de IMPRESIÓN.

    Se imprime lo mismo que saldría de la impresora de la oficina porque es
    literalmente el mismo HTML pasado por el mismo motor de impresión.
    """
    exe = binario()
    if not exe:
        raise SinMotor(
            "El servidor no tiene un navegador instalado para generar el PDF."
        )

    static = Path(static_dir) if static_dir else Path(__file__).resolve().parents[2] / "static"
    with tempfile.TemporaryDirectory(prefix="pc-pdf-") as tmp:
        tmpd = Path(tmp)
        entrada = tmpd / "hoja.html"
        salida = tmpd / "hoja.pdf"
        entrada.write_text(_para_imprimir_offline(html, static), encoding="utf-8")

        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--disable-extensions",
            # Perfil propio y descartable: en el servidor puede haber una
            # sesión de Edge abierta y el perfil se lockea — el headless se
            # cuelga sin decir por qué.
            f"--user-data-dir={tmpd / 'perfil'}",
            # Sin encabezado ni pie del navegador: nada de "1/2" ni la URL del
            # archivo temporal en la hoja que ve el cliente.
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            f"--virtual-time-budget={PRESUPUESTO_MS}",
            f"--print-to-pdf={salida}",
            *ARRANQUE_CALLADO,
            entrada.resolve().as_uri(),
        ]
        try:
            subprocess.run(cmd, check=False, timeout=TIMEOUT_S,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise SinMotor("El navegador tardó demasiado en imprimir.") from e

        if not salida.exists() or salida.stat().st_size == 0:
            raise SinMotor("El navegador no devolvió ningún PDF.")
        return salida.read_bytes()
