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

from modules._lib import cache_hojas

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


def desde_html(html: str, static_dir: str | os.PathLike | None = None, *,
                fondo: bool = False) -> bytes:
    """El HTML ya renderizado → los bytes de un PDF, con la hoja de IMPRESIÓN.

    Se imprime lo mismo que saldría de la impresora de la oficina porque es
    literalmente el mismo HTML pasado por el mismo motor de impresión.

    `fondo=True` (TMT 2026-08-31, paquete de cierre -- dueña: "pagina 1, no
    lo podemos mostrar igual que la pantalla de resultados?"): en vez de la
    hoja `@media print`, pide la hoja tal cual se ve en pantalla (media
    `screen`, con fondos). Sólo lo sabe hacer el camino del navegador YA
    PRENDIDO (`navegador.py`, habla CDP); el `subprocess` de más abajo no
    tiene forma de pedirle a `--print-to-pdf` que use media `screen`, así
    que si ese navegador no está disponible, `fondo=True` degrada solo a la
    hoja de impresión de siempre -- mejor eso que romper el paquete entero.

    ⭐ TMT 2026-08-26 (*"tarda mucho tiempo"*): antes de llegar al `subprocess`
    de más abajo se prueban los dos atajos, en este orden:

      1. **La caché** — si este MISMO HTML ya se imprimió hace poco, el archivo
         sale de memoria. Es el caso del botón de WhatsApp, que prepara la hoja
         al apoyar el dedo y la vuelve a pedir al tocar.
      2. **El navegador ya prendido** — la misma hoja, en una pestaña de un
         Chromium que no hay que levantar. Medido: 0,1 s contra 0,4 s acá,
         contra los 3,5-5,2 s que tarda en el servidor de Windows.

    Si los dos fallan queda el camino de siempre, intacto: un navegador por
    hoja. Este módulo NO puede salir más lento que ayer.
    """
    # ⚠ La importación va acá adentro y no arriba: `navegador` necesita a este
    # módulo para saber CUÁL es el navegador (`binario()`) y cómo dejar el HTML
    # listo para abrirse del disco, así que arriba sería un círculo.
    from modules._lib import navegador

    k = cache_hojas.clave("pdf", html, "fondo" if fondo else "plano")
    guardado = cache_hojas.obtener(k)
    if guardado:
        return guardado

    exe = binario()
    if not exe:
        raise SinMotor(
            "El servidor no tiene un navegador instalado para generar el PDF."
        )

    static = Path(static_dir) if static_dir else Path(__file__).resolve().parents[2] / "static"

    rapido = navegador.pdf(html, static, fondo=fondo)
    if rapido:
        cache_hojas.guardar(k, rapido)
        return rapido

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
            # ⚠ 5000 y NO menos. TMT 2026-08-24 pidió que el botón de WhatsApp
            # tardara menos y la primera idea fue bajar este techo a 2000 y
            # sacarle al navegador el trabajo de arranque que no sirve
            # (--disable-background-networking, --disable-component-update,
            # --disable-sync, --metrics-recording-only y compañía).
            #
            # MEDIDO EN PRODUCCIÓN, y salió al REVÉS: /mi-cartera/cliente/ATE/pdf
            # pasó de 3,5-5,2 s a 5,1 / 8,4 / 10,3 / 24 / 33 s — una corrida
            # rozando el TIMEOUT_S de 30, o sea a un segundo de devolver un 503
            # en la cara del vendedor. Se revirtió el mismo día.
            #
            # No se entendió POR QUÉ (el servidor es Windows y de acá no se lo
            # puede perfilar), así que lo único que queda anotado es el hecho:
            # este bloque de flags NO se vuelve a tocar a ciegas. Lo que sí
            # está medido es DÓNDE está el tiempo: traer los datos y armar el
            # HTML son 170 ms de los 3,5-5,2 s, todo el resto es levantar y
            # matar un navegador por cada PDF.
            #
            # ⭐ Ese camino —tener UNO prendido y hablarle por CDP, en vez de
            # afinarle los argumentos al que arranca de cero— se hizo el
            # 2026-08-26 y vive en `modules/_lib/navegador.py`. Este bloque de
            # abajo es el que corre cuando aquél no está: sigue siendo el piso
            # y por eso sigue intacto.
            "--virtual-time-budget=5000",
            f"--print-to-pdf={salida}",
            entrada.resolve().as_uri(),
        ]
        try:
            subprocess.run(cmd, check=False, timeout=TIMEOUT_S,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise SinMotor("El navegador tardó demasiado en imprimir.") from e

        if not salida.exists() or salida.stat().st_size == 0:
            raise SinMotor("El navegador no devolvió ningún PDF.")
        datos = salida.read_bytes()
        cache_hojas.guardar(k, datos)
        return datos
