"""Las hojas ya dibujadas, guardadas un rato: la misma hoja no se dibuja dos veces.

TMT 2026-08-26 (dueña): *"podemos hacer más rápido lo de mandar imagen, pdf y
whatsapp desde vendedor. tarda mucho tiempo"*.

⭐ POR QUÉ SOBRABAN NAVEGADORES

Mandar UN estado de cuenta por WhatsApp dispara hoy, en el peor caso, tres
dibujos del MISMO archivo:

  · el `pointerdown` del botón verde arranca la foto apenas el dedo apoya;
  · el vendedor toca "Imagen" al lado, que abre esa misma foto en otra pestaña;
  · y si algo salió mal y vuelve a tocar, otra vez.

Cada uno de esos costaba un navegador entero. Con esta caché, el primero lo
paga y los demás salen de memoria.

⭐ LA CLAVE ES EL HTML, NO EL CLIENTE

Y esto es lo único importante de este módulo. La clave NO es "el estado de
cuenta de ATE": es el **hash del HTML ya renderizado**. O sea que sólo se
reusa el archivo cuando la hoja es EXACTAMENTE la misma, letra por letra. Si
entró un cheque, si se aplicó una factura, si cambió el día del encabezado, el
HTML cambia, el hash cambia y se dibuja de nuevo.

Con una clave por cliente y un TTL habría un rato —minutos— en el que el
vendedor le manda al cliente un saldo viejo. Acá eso no puede pasar: armar el
HTML son 170 ms de los 3,5-5,2 s medidos, así que se paga siempre y lo que se
saltea es sólo el navegador. Nunca se sirve un número que no se acaba de
consultar.

El TTL de abajo no es para la frescura entonces, sino para la memoria: es
cuánto tiempo tiene sentido guardar un archivo que ya nadie va a volver a
pedir.
"""

from __future__ import annotations

import hashlib
import threading
import time

#: Cuánto vive una hoja dibujada. El envío por WhatsApp (preparar → tocar →
#: mandar) pasa en menos de un minuto; cinco alcanzan de sobra para el vendedor
#: que reintenta.
TTL_S = 300.0

#: Techo de memoria. Una foto de estado de cuenta pesa ~80-300 kB y un PDF
#: ~90 kB: 24 MB son unas cien hojas. El servidor es el mismo que atiende la
#: app, así que esto tiene que tener un techo y no una promesa.
MAX_BYTES = 24 * 1024 * 1024

_LOCK = threading.Lock()
_HOJAS: dict[str, tuple[float, bytes]] = {}
_PESO = 0


def clave(*partes) -> str:
    """El hash de todo lo que define la hoja: el HTML y cómo se dibuja."""
    h = hashlib.sha256()
    for p in partes:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def obtener(k: str) -> bytes | None:
    """La hoja ya dibujada, o `None` si no está o si venció."""
    global _PESO
    with _LOCK:
        item = _HOJAS.get(k)
        if not item:
            return None
        vence, datos = item
        if time.monotonic() > vence:
            del _HOJAS[k]
            _PESO -= len(datos)
            return None
        return datos


def guardar(k: str, datos: bytes) -> None:
    """Guarda la hoja. Si no entra, se tiran las más viejas."""
    global _PESO
    if not datos or len(datos) > MAX_BYTES:
        return
    with _LOCK:
        anterior = _HOJAS.pop(k, None)
        if anterior:
            _PESO -= len(anterior[1])
        _HOJAS[k] = (time.monotonic() + TTL_S, datos)
        _PESO += len(datos)
        ahora = time.monotonic()
        # Primero las vencidas —que no le sirven a nadie— y después, si sigue
        # sin entrar, las más viejas. `dict` conserva el orden de inserción.
        for viejo in [x for x, (v, _) in _HOJAS.items() if v < ahora and x != k]:
            _PESO -= len(_HOJAS.pop(viejo)[1])
        while _PESO > MAX_BYTES and len(_HOJAS) > 1:
            primero = next(iter(_HOJAS))
            if primero == k:       # la recién guardada no se tira a sí misma
                primero = next(x for x in _HOJAS if x != k)
            _PESO -= len(_HOJAS.pop(primero)[1])


def limpiar() -> None:
    """Vaciarla entera (los tests, y el health si alguna vez hace falta)."""
    global _PESO
    with _LOCK:
        _HOJAS.clear()
        _PESO = 0


def estado() -> dict:
    """Cuántas hojas hay guardadas y cuánto pesan — para el health."""
    with _LOCK:
        return {"hojas": len(_HOJAS), "bytes": _PESO}
