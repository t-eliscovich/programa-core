"""Un navegador PRENDIDO todo el tiempo, para no pagar el arranque en cada hoja.

TMT 2026-08-26 (dueña): *"podemos hacer más rápido lo de mandar imagen, pdf y
whatsapp desde vendedor. tarda mucho tiempo"*.

⭐ DÓNDE ESTABA EL TIEMPO, que ya estaba medido

`pdf_motor` deja escrito el resultado de la única medición seria que se hizo en
producción: de los 3,5-5,2 s que tarda `/mi-cartera/cliente/<cod>/pdf`, traer
los datos y armar el HTML son **170 ms**. Todo el resto es LEVANTAR Y MATAR UN
NAVEGADOR, una vez por archivo. Y ahí mismo quedó anotado el camino:

    "El camino que queda es tener UNO prendido y hablarle por CDP, no afinarle
     los argumentos al que arranca de cero."

Eso es este módulo. El navegador se levanta UNA vez —en un hilo de fondo, al
arrancar la app— y se queda prendido; cada hoja es una pestaña nueva que se
abre, se imprime y se cierra. Medido acá con el mismo HTML de 40 filas:

    arrancar un navegador por hoja  ·  0,40 s   (en Windows: 3,5 a 5,2 s)
    una pestaña en el que ya está   ·  0,10 s

⚠ Y el PDF que sale es EL MISMO ARCHIVO. Verificado byte por byte contra el que
devuelve `--print-to-pdf`: los únicos 6 bytes que cambian son la hora dentro de
`/CreationDate`. La foto (`Page.captureScreenshot` contra `--screenshot`) sale
idéntica, sin una sola diferencia. No es una segunda forma de dibujar la hoja:
es el mismo motor de Chromium, manejado por el cable de adentro en vez de por
la línea de comandos.

⭐ LA REGLA QUE HACE QUE ESTO NO PUEDA SALIR PEOR QUE ANTES

Este módulo NUNCA hace esperar a un usuario por un arranque. El navegador lo
levanta un hilo de fondo; el request sólo lo USA si ya está listo. Si no está
—porque todavía no levantó, porque se murió, porque en ese servidor el modo
CDP no anda— las funciones de acá devuelven `None` EN EL ACTO y el que llamó
sigue por el camino de siempre, el de `subprocess`. O sea:

    · si el navegador persistente anda  → la hoja sale 10 veces más rápido
    · si no anda                        → sale exactamente como salía ayer

Por eso no hay ningún `raise` que llegue al usuario y por eso el arranque vive
en un hilo: un `--remote-debugging-port` bloqueado por una política de Windows
tiene que costar un renglón en el log, no 15 segundos en la cara del vendedor.

⭐ POR QUÉ EL WEBSOCKET ESTÁ ESCRITO A MANO

CDP se habla por websocket y el `pip install` del deploy es un freno duro: si
falla, el deploy sale con `exit 1` y la app queda abajo (ver deploy.yml). Meter
una librería nueva para 60 líneas de protocolo es poner ese riesgo en el camino
crítico de todos los deploys, incluidos los que no tienen nada que ver con
esto. `_Ws` de acá abajo habla exactamente lo que CDP necesita —un handshake,
tramas de texto enmascaradas, tramas partidas— y nada más.
"""

from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from modules._lib import pdf_motor

_LOG = logging.getLogger("programa_core.navegador")

#: Apagar el navegador persistente sin tocar código: todo vuelve a salir por
#: `subprocess`, como antes del 26/08.
VAR_APAGAR = "PDF_NAVEGADOR_PERSISTENTE"

#: Cuánto se le da al navegador para levantar. Lo paga el hilo de fondo, nunca
#: un request.
ARRANQUE_S = 20.0

#: Techo por hoja, de punta a punta. Una hoja tarda ~0,1-0,3 s; esto está para
#: que una pestaña colgada no se coma un worker.
DOC_S = 20.0

#: Techo de UNA lectura del websocket. Más corto que `DOC_S` a propósito: si el
#: navegador se cuelga sin cerrar el socket, esto es lo que tarda en darse
#: cuenta y devolver la hoja por el camino de siempre.
LECTURA_S = 10.0

#: ⚠ YA NO SE APAGA POR FALTA DE USO. Hasta el 05/09/2026 se apagaba a los
#: 15 min sin uso (IDLE_S = 900) y el latido lo volvía a prender 30 s después
#: — un ciclo apagar/prender cuatro veces por hora que no ahorraba nada y
#: que, como el apagado mataba sólo al padre, fabricó los 1.525 huérfanos
#: que dejaron el servidor sin memoria. Ahora queda prendido y se RECICLA una
#: vez por día, a `RECICLAR_A` hora Ecuador (después del reinicio nocturno
#: de Metabase), por si Chromium crece. Ver
#: docs/PLAN_MEMORIA_SERVIDOR_2026_09_05.md, fase 1.
RECICLAR_A = (2, 35)

#: Después de un fallo no se vuelve a intentar por un rato: si en este servidor
#: el modo CDP no anda, que no se note en nada más que un renglón del log.
REINTENTO_S = 300.0

#: Cada cuánto mira el hilo de fondo si hay que levantar o apagar.
_LATIDO_S = 30.0


class _Ws:
    """El mínimo websocket de cliente que hace falta para hablar CDP.

    Sólo texto, sólo hacia un `127.0.0.1` que ya nos contestó el handshake.
    Nada de extensiones, ni de compresión, ni de reconexión: si algo sale mal
    se tira el navegador entero y se levanta otro.
    """

    def __init__(self, host: str, puerto: int, ruta: str, timeout: float):
        self.s = socket.create_connection((host, puerto), timeout=timeout)
        self.s.settimeout(timeout)
        clave = base64.b64encode(os.urandom(16)).decode()
        pedido = (
            f"GET {ruta} HTTP/1.1\r\n"
            f"Host: {host}:{puerto}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {clave}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.s.sendall(pedido.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            trozo = self.s.recv(4096)
            if not trozo:
                raise OSError("el navegador cortó el handshake")
            buf += trozo
        cabecera, resto = buf.split(b"\r\n\r\n", 1)
        if b" 101" not in cabecera.split(b"\r\n")[0]:
            raise OSError(f"el navegador no aceptó el websocket: {cabecera[:80]!r}")
        self.buf = resto

    def _leer(self, n: int) -> bytes:
        while len(self.buf) < n:
            trozo = self.s.recv(65536)
            if not trozo:
                raise OSError("el navegador cerró la conexión")
            self.buf += trozo
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def enviar(self, texto: str) -> None:
        datos = texto.encode()
        mascara = os.urandom(4)
        n = len(datos)
        if n < 126:
            cab = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            cab = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            cab = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        # El cliente SIEMPRE enmascara (RFC 6455): sin esto Chrome corta.
        self.s.sendall(
            cab + mascara + bytes(b ^ mascara[i % 4] for i, b in enumerate(datos)))

    def recibir(self) -> str:
        """Un mensaje completo, juntando las tramas partidas si vino en varias.

        Una foto de 300 kB llega en pedazos: si se lee sólo la primera trama, el
        JSON queda cortado por la mitad.
        """
        partes: list[bytes] = []
        while True:
            b0, b1 = self._leer(2)
            fin, op, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._leer(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._leer(8))[0]
            cuerpo = self._leer(n)
            if op == 0x8:                       # close
                raise OSError("el navegador cerró el websocket")
            if op in (0x9, 0xA):                # ping/pong: no son un mensaje
                continue
            partes.append(cuerpo)
            if fin:
                return b"".join(partes).decode()

    def cerrar(self) -> None:
        try:
            self.s.close()
        except OSError:            # pragma: no cover - cerrar no puede fallar feo
            pass


def _carpeta_de(app_pid: int) -> Path:
    """Dónde vive el perfil del navegador de UN proceso de la app.

    El nombre lleva el pid de la APP —no el del navegador— a propósito: es lo
    que permite que un arranque distinga su basura de la del proceso hermano
    que está vivo al lado (la oficina en el 5002 y el portal en el 5004 corren
    el mismo código).
    """
    return Path(tempfile.gettempdir()) / f"pc-nav-{app_pid}"


def _proceso_vivo(pid: int) -> bool:
    """¿Existe todavía ese proceso?

    ⚠ En Windows `os.kill(pid, 0)` NO pregunta: MATA (la doc de Python lo dice
    con todas las letras — cualquier señal que no sea CTRL_C/CTRL_BREAK termina
    el proceso). Por eso ahí se pregunta con `tasklist`.
    """
    if sys.platform.startswith("win"):
        return bool(_imagen_de(pid))
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:      # existe, pero es de otro usuario
        return True
    return True


def _imagen_de(pid: int) -> str:
    """El nombre del ejecutable de un pid en Windows, o '' si no existe."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 -- si tasklist no está, no se mata nada
        return ""
    linea = (r.stdout or "").strip().splitlines()
    if not linea or "," not in linea[0]:
        return ""
    return linea[0].split(",")[0].strip().strip('"').lower()


def _es_ese_navegador(pid: int, exe: str) -> bool:
    """¿El pid guardado sigue siendo NUESTRO navegador?

    Los pid se reciclan. Antes de matar a alguien se confirma que el que está
    en ese número es el mismo programa que anotamos, no el Excel de un
    contador que agarró el número libre.
    """
    # El basename a mano y no `Path(exe).name`: la ruta puede venir con barras
    # de Windows y en un Linux `Path` no las reconoce como separador.
    nombre = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if sys.platform.startswith("win"):
        return _imagen_de(pid) == nombre
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return nombre in cmd


#: Los prefijos de las carpetas temporales de NUESTROS navegadores. Todo
#: proceso del navegador lleva el `--user-data-dir` en su línea de comando
#: (Chromium se lo pasa a cada hijo), así que por acá se reconoce a los
#: nuestros sin tocar el Chrome que alguien dejó abierto en el servidor.
PREFIJOS_NUESTROS = ("pc-nav-", "pc-pdf-", "pc-img-")


def correr_y_matar_el_arbol(cmd: list[str], timeout: float) -> None:
    """`subprocess.run(cmd, timeout=...)` pero, si se pasa, mata el ÁRBOL.

    🚨 LA FUGA DEL 05/09/2026 (Andrés: *"está super lento el sistema"*).
    Windows no mata a los hijos cuando muere el padre. `subprocess.run` con
    `timeout` —y `Popen.terminate()`/`kill()`— matan SÓLO al proceso del
    navegador; sus hijos (renderer, GPU, utility, crashpad) quedan vivos
    para siempre, ~6 MB cada uno. Este módulo apagaba el navegador cada 15
    min sin uso y lo volvía a prender, y cada apagado dejaba media docena
    de huérfanos: en diez días fueron **1.525 procesos `chrome` con 8,9 GB
    privados** en un servidor de 4 GB, paginando a disco, con TODO lento —
    y culpamos a Metabase porque en la lista por proceso cada chrome era
    chiquito. Por eso `matar()` y los dos `subprocess` de pdf_motor e
    imagen_motor pasan por acá: `taskkill /T` en Windows, y en Linux el
    kill de siempre (ahí los huérfanos los adopta init y salen solos).
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _matar_pid(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover -- ya se intentó todo
            pass
        raise


_RE_DIR = re.compile(r"--user-data-dir=(\S+)")

#: Cómo se llama el ejecutable de un navegador. Un proceso que no se llama
#: así NO se toca aunque lleve `pc-pdf-` en la línea de comando (el shell
#: que lanzó un test, por ejemplo — pasó, y se llevó puesto al pytest).
_NOMBRES_NAVEGADOR = ("chrome", "msedge", "chromium", "google-chrome")


def _es_navegador(nombre: str | None) -> bool:
    return (nombre or "").lower().startswith(_NOMBRES_NAVEGADOR)


def _carpeta_en(cmdline: str, nombre: str | None = "chrome") -> str | None:
    """La carpeta temporal nuestra que lleva esa línea de comando, o None."""
    if not _es_navegador(nombre):
        return None
    m = _RE_DIR.search(cmdline)
    if not m:
        return None
    ruta = m.group(1).strip('"')
    return ruta if any(pref in ruta for pref in PREFIJOS_NUESTROS) else None


def _es_huerfano(ruta: str, edad_s: float, mi_dir: str | None) -> bool:
    """Decide si un proceso nuestro con esa carpeta sobra.

    · `pc-nav-<pid>`: sobra si el proceso dueño ya no existe, o si el dueño
      soy yo y la carpeta no es la del navegador que tengo prendido. La del
      HERMANO vivo (oficina/portal) no se toca: su latido barre la suya.
    · `pc-pdf-*` / `pc-img-*`: son de un navegador de un solo uso; si sigue
      vivo pasado el timeout, es un hijo que sobrevivió al kill del padre.
    """
    nombre = ruta.replace("\\", "/").split("pc-", 1)[1]  # nav-123/perfil
    if nombre.startswith("nav-"):
        try:
            dueno = int(nombre.split("-", 1)[1].split("/", 1)[0])
        except ValueError:
            return True
        if dueno == os.getpid():
            return not (mi_dir and mi_dir.replace("\\", "/") in ruta.replace("\\", "/"))
        return not _es_proceso_de_la_app(dueno)
    return edad_s > pdf_motor.TIMEOUT_S + 30


def _es_proceso_de_la_app(pid: int) -> bool:
    """¿Ese pid está vivo Y es un python (uno de los procesos de la app)?

    Windows reusa los pids enseguida: el dueño "vivo" de una carpeta vieja
    puede ser hoy un svchost cualquiera, y con `_proceso_vivo` a secas esos
    huérfanos quedaban protegidos para siempre (05/09: 106 chrome que el
    barrido no tocaba).
    """
    if not _proceso_vivo(pid):
        return False
    try:
        import psutil

        return psutil.Process(pid).name().lower().startswith("python")
    except Exception:  # noqa: BLE001 -- sin psutil, vale lo de antes
        return True


def barrer_procesos_huerfanos(mi_dir: str | None) -> int:
    """Mata los procesos de navegadores nuestros que sobran (ver `_es_huerfano`).

    Reconoce a los nuestros por el `--user-data-dir` con uno de los prefijos
    de `PREFIJOS_NUESTROS`; un Chrome abierto a mano en el servidor no lo
    tiene y no se toca. Devuelve cuántos mató. Fail-soft: sin psutil, cero.
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return 0
    ahora = time.time()
    muertos = 0
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            ruta = _carpeta_en(" ".join(p.info.get("cmdline") or []), p.info.get("name"))
            if not ruta:
                continue
            if _es_huerfano(ruta, ahora - float(p.info.get("create_time") or ahora), mi_dir):
                p.kill()
                muertos += 1
        except Exception:  # noqa: BLE001 -- ya murió, o no es nuestro de matar
            continue
    if muertos:
        _LOG.warning("Se mataron %s procesos huérfanos de navegadores nuestros", muertos)
    return muertos


def contar_procesos_nuestros() -> int:
    """Cuántos procesos de navegadores nuestros hay vivos (para el health)."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if _carpeta_en(" ".join(p.info.get("cmdline") or []), p.info.get("name")):
                n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


def _barrer_huerfanos() -> None:
    """Mata los navegadores que quedaron de procesos de la app que ya no están.

    ⭐ POR QUÉ HACE FALTA: el deploy para la app con `Stop-Process -Force` (ver
    deploy.yml), y eso NO se lleva a los hijos. Sin este barrido, cada deploy
    dejaría un headless prendido para siempre: dos por deploy, ~100 MB cada
    uno, en el mismo servidor que atiende la app. Una mejora de velocidad que
    se come el servidor a la semana no es una mejora.

    Sólo toca las carpetas de procesos MUERTOS: la del hermano vivo no se
    toca, y la propia tampoco.
    """
    yo = os.getpid()
    for carpeta in Path(tempfile.gettempdir()).glob("pc-nav-*"):
        try:
            dueno = int(carpeta.name.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if dueno == yo or _proceso_vivo(dueno):
            continue
        _matar_lo_anotado(carpeta)
        shutil.rmtree(carpeta, ignore_errors=True)
    # Y los que no dejaron apunte (los hijos que sobrevivieron a un kill del
    # padre, los del camino de subprocess): se reconocen por la carpeta.
    barrer_procesos_huerfanos(mi_dir=str(_NAV.perfil) if _NAV.perfil else None)


def _matar_lo_anotado(carpeta: Path) -> None:
    """Mata el navegador anotado en esa carpeta, si el pid sigue siendo él.

    Callado a propósito cuando no hay nada anotado: una carpeta sin apunte es
    una que ya se limpió, no un problema.
    """
    try:
        lineas = (carpeta / "navegador.pid").read_text(encoding="utf-8").splitlines()
        pid, exe = int(lineas[0]), lineas[1]
    except (OSError, IndexError, ValueError):
        return
    try:
        if _es_ese_navegador(pid, exe):
            _matar_pid(pid)
            _LOG.info("Se mató el navegador huérfano %s (%s)", pid, exe)
    except Exception as e:  # noqa: BLE001 -- limpiar no puede romper nada
        _LOG.warning("no se pudo matar el navegador %s: %s", pid, e)


def _matar_pid(pid: int) -> None:
    """Mata el proceso Y SUS HIJOS (`/T`). Ver `correr_y_matar_el_arbol`."""
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=15)
    else:
        os.kill(pid, signal.SIGTERM)


class _Navegador:
    """El navegador prendido y el cable para hablarle. Uno por proceso."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.ws: _Ws | None = None
        self.dir: Path | None = None
        self.ultimo_uso = 0.0
        self.proximo_intento = 0.0
        self._id = 0
        self._arranques = 0
        #: La carpeta del perfil del navegador prendido AHORA (lo que llevan
        #: sus hijos en `--user-data-dir`); es lo que el barrido no toca.
        self.perfil: Path | None = None
        #: Hasta cuándo puede durar la hoja que se está dibujando ahora. Lo
        #: pone `hoja()` y lo respetan TODOS los comandos: si no, cada uno
        #: podría esperar su propio techo y la suma no la mira nadie.
        self._limite = 0.0

    # -- vida del proceso ---------------------------------------------------

    def vivo(self) -> bool:
        return bool(self.ws and self.proc and self.proc.poll() is None)

    def arrancar(self) -> bool:
        """Levanta el navegador. Lo llama el hilo de fondo, NUNCA un request."""
        if apagado():
            return False
        if self.vivo():
            return True
        if time.monotonic() < self.proximo_intento:
            return False
        exe = pdf_motor.binario()
        if not exe:
            return False
        self.proximo_intento = time.monotonic() + REINTENTO_S
        try:
            _barrer_huerfanos()
            self._levantar(exe)
        except Exception as e:  # noqa: BLE001 -- nunca frena nada
            _LOG.warning("No se pudo dejar el navegador prendido (%s); "
                         "las hojas siguen saliendo de a un navegador por vez.", e)
            self.matar()
            return False
        self.proximo_intento = 0.0
        self.ultimo_uso = time.monotonic()
        _LOG.info("Navegador prendido para PDFs e imágenes (pid %s)",
                  self.proc.pid if self.proc else "?")
        return True

    def _levantar(self, exe: str) -> None:
        # La carpeta lleva el pid de ESTE proceso: es lo que le deja al próximo
        # arranque distinguir su basura de la del hermano que sigue vivo.
        self.dir = _carpeta_de(os.getpid())
        # Si en esta misma carpeta quedó anotado un navegador de una vida
        # anterior de este pid, se lo mata ANTES de borrarle el apunte: si no,
        # queda prendido y sin nadie que sepa que existe.
        _matar_lo_anotado(self.dir)
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        # El perfil lleva un número de ARRANQUE: los hijos que sobrevivan a
        # un apagado llevan el perfil viejo en su línea de comando, y así el
        # barrido los distingue del navegador que está prendido ahora (con
        # una sola carpeta `perfil` eran iguales y no se podían tocar).
        self._arranques += 1
        perfil = self.dir / f"perfil-{self._arranques}"
        self.perfil = perfil
        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--disable-extensions",
            # ⚠ Las MISMAS banderas que el camino de `subprocess`, y por los
            # mismos motivos (ver pdf_motor). Lo único que se agrega es el
            # puerto de CDP; el bloque de flags de arranque que se probó el
            # 24/08 y salió al revés NO se toca.
            f"--user-data-dir={perfil}",
            # 0 = que elija él y lo escriba en DevToolsActivePort. Un puerto
            # fijo choca con el otro proceso de la app (oficina 5002 / portal
            # 5004) y con un navegador que haya quedado colgado de antes.
            "--remote-debugging-port=0",
            "about:blank",
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Quién es y qué es, para que el próximo arranque lo pueda matar si
        # este proceso se muere sin despedirse (un deploy, por ejemplo).
        (self.dir / "navegador.pid").write_text(
            f"{self.proc.pid}\n{exe}\n", encoding="utf-8")
        archivo = perfil / "DevToolsActivePort"
        limite = time.monotonic() + ARRANQUE_S
        puerto, ruta = 0, ""
        while time.monotonic() < limite:
            if self.proc.poll() is not None:
                raise OSError(f"el navegador se cerró solo (código {self.proc.returncode})")
            if archivo.exists():
                lineas = archivo.read_text(encoding="utf-8").splitlines()
                if len(lineas) >= 2 and lineas[0].strip().isdigit():
                    puerto, ruta = int(lineas[0].strip()), lineas[1].strip()
                    break
            time.sleep(0.05)
        if not puerto:
            raise OSError("el navegador no abrió el puerto de control a tiempo")
        self.ws = _Ws("127.0.0.1", puerto, ruta, timeout=LECTURA_S)

    def matar(self) -> None:
        if self.ws:
            self.ws.cerrar()
            self.ws = None
        if self.proc:
            # El ÁRBOL, no sólo el padre: ver `correr_y_matar_el_arbol`.
            try:
                _matar_pid(self.proc.pid)
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 -- si no se deja, se lo mata
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001  # pragma: no cover
                    pass
            self.proc = None
        if self.dir:
            carpeta, self.dir, self.perfil = self.dir, None, None
            # Lo que haya quedado con ESA carpeta en la línea de comando
            # (self.dir ya es None: para el barrido, ninguna es "la mía").
            try:
                barrer_procesos_huerfanos(mi_dir=None)
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(carpeta, ignore_errors=True)

    # -- CDP ----------------------------------------------------------------

    def _cmd(self, metodo: str, params: dict | None = None,
             sesion: str | None = None) -> dict:
        assert self.ws is not None
        self._id += 1
        mio = self._id
        msg = {"id": mio, "method": metodo, "params": params or {}}
        if sesion:
            msg["sessionId"] = sesion
        self.ws.enviar(json.dumps(msg))
        limite = min(self._limite or (time.monotonic() + DOC_S),
                     time.monotonic() + DOC_S)
        while time.monotonic() < limite:
            r = json.loads(self.ws.recibir())
            if r.get("id") != mio:
                continue          # un evento de la pestaña; acá no se usan
            if "error" in r:
                raise OSError(f"{metodo}: {r['error']}")
            return r.get("result") or {}
        raise OSError(f"{metodo}: el navegador no contestó")

    def _esperar_carga(self, sesion: str, limite: float) -> None:
        """Hasta que la hoja esté ENTERA, preguntándole a la página.

        Se pregunta en vez de escuchar `Page.loadEventFired` a propósito: el
        evento del `about:blank` con el que nace la pestaña puede llegar
        después de que pedimos la hoja de verdad, y ahí se saca la foto de una
        página en blanco. `readyState` no se puede confundir.
        """
        while time.monotonic() < limite:
            r = self._cmd("Runtime.evaluate", {
                "expression": "document.readyState + '|' + location.href",
                "returnByValue": True,
            }, sesion)
            valor = ((r.get("result") or {}).get("value") or "")
            estado, _, href = valor.partition("|")
            if estado == "complete" and href.startswith("file:"):
                return
            time.sleep(0.02)
        raise OSError("la hoja no terminó de cargar")

    def _alto_del_contenido(self, sesion: str) -> int | None:
        """Cuántos píxeles de alto tiene lo DIBUJADO, preguntado a la página.

        `scrollHeight` del `<html>` no sirve: devuelve el alto de la ventana
        cuando el contenido es más corto. El del `<body>` sí mide el
        contenido (verificado contra la hoja real de una factura: 953 px de
        contenido en una ventana de 1988). `None` si no contesta un número —
        el que llama sigue con la ventana estimada.
        """
        try:
            r = self._cmd("Runtime.evaluate", {
                "expression": "String(document.body ? Math.ceil(Math.max("
                              "document.body.scrollHeight, "
                              "document.body.getBoundingClientRect().bottom)) : 0)",
                "returnByValue": True,
            }, sesion)
            alto = int((r.get("result") or {}).get("value") or "")
            return alto if alto > 0 else None
        except (OSError, ValueError, TypeError):
            return None

    def hoja(self, html: str, static: Path, *, medidas: tuple[int, int] | None,
             formato: str, fondo: bool = False) -> bytes:
        """Una pestaña: abre el HTML, saca el archivo y se cierra.

        `formato` es 'pdf' o 'png'. `medidas` sólo la usa el PNG (la foto sale
        del tamaño de la ventana; el PDF, del tamaño de la hoja de papel).

        `fondo=True` (sólo PDF, TMT 2026-08-31, paquete de cierre): en vez de
        imprimir con la hoja `@media print` ("lista plana", pedido dueña
        2026-05-27), renderiza en media `screen` -- la hoja tal cual se ve en
        pantalla, colores y todo -- y pide los fondos al navegador
        (`printBackground: True`). Es la excepción puntual para la página de
        Resultados del paquete PDF ("mostrar igual que la pantalla"); el resto
        de la app sigue saliendo por el camino de siempre.
        """
        limite = self._limite = time.monotonic() + DOC_S
        carpeta = Path(tempfile.mkdtemp(prefix="pc-hoja-"))
        objetivo = None
        try:
            entrada = carpeta / "hoja.html"
            entrada.write_text(
                pdf_motor._para_imprimir_offline(html, static), encoding="utf-8")
            objetivo = self._cmd("Target.createTarget", {"url": "about:blank"})["targetId"]
            sesion = self._cmd("Target.attachToTarget", {
                "targetId": objetivo, "flatten": True})["sessionId"]
            self._cmd("Page.enable", {}, sesion)
            if medidas:
                ancho, alto = medidas
                self._cmd("Emulation.setDeviceMetricsOverride", {
                    "width": ancho, "height": alto,
                    "deviceScaleFactor": 1, "mobile": False}, sesion)
                # La barra de scroll dibujada encima de la última columna es el
                # `--hide-scrollbars` del otro camino.
                self._cmd("Emulation.setScrollbarsHidden", {"hidden": True}, sesion)
            self._cmd("Page.navigate", {"url": entrada.resolve().as_uri()}, sesion)
            self._esperar_carga(sesion, limite)
            if fondo and formato == "pdf":
                self._cmd("Emulation.setEmulatedMedia", {"media": "screen"}, sesion)
            if formato == "png":
                # ⭐ TMT 2026-08-27 (dueña: "¿podemos mejorar?"). La ventana se
                # abría con el alto ESTIMADO por `alto_para` — que estima para
                # arriba a propósito, porque lo que falta se corta. Medido en
                # producción con una factura real: contenido de 953 px en una
                # ventana de 1988. El navegador dibuja y codifica un bitmap
                # del DOBLE de lo que hay, y la PIL de después lo recorre
                # entero para recortarlo.
                #
                # Acá el navegador está PRENDIDO y se le puede PREGUNTAR a la
                # página cuánto mide, que es la diferencia con el camino de
                # `--screenshot` (una corrida ciega). Se mide, se achica la
                # ventana al contenido más un margen, y recién ahí la foto.
                # El margen evita además que lo dibujado toque el borde de
                # abajo — que es la alarma por la que `imagen_motor` rehace
                # la hoja al doble: con la medida real no se rehace nunca.
                #
                # Si la página no contesta un número (no debería pasar), la
                # foto sale con la ventana estimada, como hasta hoy.
                if medidas:
                    alto_real = self._alto_del_contenido(sesion)
                    if alto_real:
                        alto_real = max(300, min(alto_real + 60, 20000))
                        if alto_real != medidas[1]:
                            self._cmd("Emulation.setDeviceMetricsOverride", {
                                "width": medidas[0], "height": alto_real,
                                "deviceScaleFactor": 1, "mobile": False}, sesion)
                r = self._cmd("Page.captureScreenshot", {"format": "png"}, sesion)
            else:
                # ⚠ Estos tres parámetros son los que hacen que el archivo salga
                # IGUAL al de `--print-to-pdf --no-pdf-header-footer`: sin
                # encabezado ni pie del navegador, sin fondos (que es lo que
                # hace la impresión por defecto) y respetando el `@page` de la
                # hoja. Verificado byte por byte.
                r = self._cmd("Page.printToPDF", {
                    "printBackground": fondo,
                    "displayHeaderFooter": False,
                    "preferCSSPageSize": True}, sesion)
            datos = base64.b64decode(r["data"])
            if not datos:
                raise OSError("el navegador devolvió un archivo vacío")
            return datos
        finally:
            if objetivo:
                try:
                    self._cmd("Target.closeTarget", {"targetId": objetivo})
                except Exception:  # noqa: BLE001 -- la pestaña se cierra sola al morir
                    pass
            shutil.rmtree(carpeta, ignore_errors=True)


_NAV = _Navegador()
_HILO: threading.Thread | None = None


def apagado() -> bool:
    """¿Está apagado el navegador persistente por variable de entorno?"""
    return (os.environ.get(VAR_APAGAR, "1") or "").strip() == "0"


def _usar(html: str, static: Path, *, medidas: tuple[int, int] | None,
          formato: str, fondo: bool = False) -> bytes | None:
    """La hoja por el navegador que YA está prendido, o `None` sin drama.

    `None` quiere decir "por acá no salió": el que llamó sigue por el camino
    de `subprocess`. Nunca levanta un navegador ni espera a que levante — eso
    es del hilo de fondo.
    """
    if apagado() or not _NAV.vivo():
        return None
    with _NAV.lock:
        if not _NAV.vivo():        # se murió mientras esperábamos el lock
            return None
        try:
            datos = _NAV.hoja(html, static, medidas=medidas, formato=formato,
                               fondo=fondo)
            _NAV.ultimo_uso = time.monotonic()
            return datos
        except Exception as e:  # noqa: BLE001 -- cualquier cosa: al camino viejo
            _LOG.warning("El navegador prendido falló (%s); la hoja sale por el "
                         "camino de siempre.", e)
            _NAV.matar()
            # ⚠ Y no se levanta otro EN SEGUIDA. Un navegador que falla puede
            # fallar otra vez, y cada intento fallido le cuesta al vendedor la
            # espera de darse cuenta ANTES de caer al camino viejo. Se apaga por
            # `REINTENTO_S` y en ese rato las hojas salen como salían ayer.
            _NAV.proximo_intento = time.monotonic() + REINTENTO_S
            return None


def pdf(html: str, static: Path, *, fondo: bool = False) -> bytes | None:
    """Los bytes del PDF, o `None` si el navegador persistente no está.

    `fondo=True`: media `screen` + fondos reales, en vez de la hoja de
    impresión (ver `hoja()`)."""
    return _usar(html, static, medidas=None, formato="pdf", fondo=fondo)


def png(html: str, static: Path, ancho: int, alto: int) -> bytes | None:
    """Los bytes del PNG, o `None` si el navegador persistente no está."""
    return _usar(html, static, medidas=(ancho, alto), formato="png")


#: El día (EC) en que ya se recicló, para hacerlo una sola vez por día.
_reciclado_el = None


def toca_reciclar(ahora=None) -> bool:
    """¿Es la hora del reciclado diario y todavía no se hizo hoy?

    Vale desde `RECICLAR_A` hasta el fin de ese mismo día: si a esa hora el
    proceso estaba abajo (un deploy), se recicla en el primer latido de
    después. Hoy = el día de Ecuador, no el del reloj del server (UTC).
    """
    global _reciclado_el
    from datetime import UTC, datetime, timedelta

    ahora = ahora or (datetime.now(UTC) - timedelta(hours=5))
    if (ahora.hour, ahora.minute) < RECICLAR_A:
        return False
    if _reciclado_el == ahora.date():
        return False
    _reciclado_el = ahora.date()
    return True


def _latido() -> None:
    """Lo levanta si hace falta y lo recicla una vez por día."""
    while True:
        try:
            # Cada latido barre lo que sobra: es barato (una pasada por la
            # lista de procesos) y es lo que hubiera evitado los 1.525 chrome
            # del 05/09.
            barrer_procesos_huerfanos(mi_dir=str(_NAV.perfil) if _NAV.perfil else None)
            if _NAV.vivo():
                if toca_reciclar():
                    with _NAV.lock:
                        _LOG.info("Navegador reciclado (una vez por día).")
                        _NAV.matar()
                        _NAV.arrancar()
            else:
                with _NAV.lock:
                    _NAV.arrancar()
        except Exception as e:  # noqa: BLE001 -- el hilo no se muere nunca
            _LOG.warning("latido del navegador: %s", e)
        time.sleep(_LATIDO_S)


def arrancar_en_segundo_plano() -> bool:
    """Deja el navegador prendido desde que arranca la app. Devuelve si arrancó
    el hilo (no si el navegador levantó: eso pasa después y no frena a nadie)."""
    global _HILO
    if _HILO is not None or apagado():
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    _HILO = threading.Thread(target=_latido, name="navegador-pdf", daemon=True)
    _HILO.start()
    return True


def estado() -> dict:
    """Para el health: si está prendido y hace cuánto que no se usa."""
    return {
        "prendido": _NAV.vivo(),
        "apagado_por_env": apagado(),
        "segundos_sin_uso": (round(time.monotonic() - _NAV.ultimo_uso, 1)
                             if _NAV.ultimo_uso else None),
    }


atexit.register(_NAV.matar)
