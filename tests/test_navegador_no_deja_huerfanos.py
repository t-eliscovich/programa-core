"""El navegador de los PDFs no deja procesos huérfanos — `modules/_lib/navegador.py`.

🚨 05/09/2026 (Andrés: *"está super lento el sistema, se queda pensando"*):
el servidor tenía **1.525 procesos `chrome` con 8,9 GB privados** en 4 GB de
RAM. Windows no mata a los hijos cuando muere el padre, y el navegador se
apagaba (`terminate()`, sólo el padre) cada 15 min sin uso y se volvía a
prender: media docena de huérfanos por vuelta, diez días. Y se le echó la
culpa a Metabase porque en la lista por proceso cada chrome era chiquito.

Lo que se protege acá:
  1. Apagar el navegador mata el ÁRBOL (taskkill /T en Windows).
  2. El `subprocess` con timeout de pdf_motor/imagen_motor también.
  3. El barrido reconoce a los nuestros por el `--user-data-dir` y decide
     bien cuál sobra: los de un proceso muerto, los míos que no son el
     prendido, los de un solo uso que pasaron el timeout — y NO el del
     hermano vivo ni un Chrome abierto a mano.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from modules._lib import navegador, pdf_motor


def test_apagar_mata_el_arbol_no_solo_al_padre(monkeypatch):
    matados = []
    monkeypatch.setattr(navegador, "_matar_pid", lambda pid: matados.append(pid))
    monkeypatch.setattr(navegador, "barrer_procesos_huerfanos", lambda mi_dir=None: 0)

    class _Proc:
        pid = 4242

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    nav = navegador._Navegador()
    nav.proc = _Proc()
    nav.matar()
    assert matados == [4242]
    assert nav.proc is None


def test_correr_con_timeout_mata_el_arbol():
    matados = []
    original = navegador._matar_pid

    def _espia(pid):
        matados.append(pid)
        original(pid)

    navegador._matar_pid = _espia
    try:
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            navegador.correr_y_matar_el_arbol(cmd, timeout=0.5)
        assert time.monotonic() - t0 < 10
        assert len(matados) == 1
    finally:
        navegador._matar_pid = original


def test_correr_sin_timeout_no_mata_nada(monkeypatch):
    monkeypatch.setattr(navegador, "_matar_pid", lambda pid: pytest.fail("no había que matar"))
    navegador.correr_y_matar_el_arbol([sys.executable, "-c", "pass"], timeout=20)


def test_pdf_motor_e_imagen_motor_usan_el_timeout_que_mata_el_arbol():
    import inspect

    from modules._lib import imagen_motor
    for mod in (pdf_motor, imagen_motor):
        src = inspect.getsource(mod)
        assert "correr_y_matar_el_arbol" in src, mod.__name__
        assert "subprocess.run(cmd" not in src, mod.__name__


def test_reconoce_la_carpeta_nuestra_en_la_linea_de_comando():
    assert navegador._carpeta_en(
        r'chrome.exe --headless=new --user-data-dir=C:\Temp\pc-nav-123\perfil about:blank'
    ) == r"C:\Temp\pc-nav-123\perfil"
    assert navegador._carpeta_en(
        "chrome --type=renderer --user-data-dir=/tmp/pc-pdf-abc/perfil"
    ) == "/tmp/pc-pdf-abc/perfil"
    # Un Chrome abierto a mano en el servidor: NO es nuestro.
    assert navegador._carpeta_en(r'chrome.exe --user-data-dir=C:\Users\x\AppData\Local\Google') is None
    assert navegador._carpeta_en("chrome.exe --headless=new") is None
    # Y un proceso que no es un navegador, tampoco, diga lo que diga.
    assert navegador._carpeta_en("bash -c --user-data-dir=/tmp/pc-pdf-x/perfil", "bash") is None


def test_cual_sobra(monkeypatch):
    yo = os.getpid()
    monkeypatch.setattr(navegador, "_es_proceso_de_la_app", lambda pid: pid == 777)
    es = navegador._es_huerfano
    # De un proceso de la app que ya no existe: sobra.
    assert es(r"C:\Temp\pc-nav-999\perfil", 5, None) is True
    # Del hermano VIVO (portal/oficina): no se toca.
    assert es(r"C:\Temp\pc-nav-777\perfil", 5, None) is False
    # Mío, y es el prendido: no se toca.
    assert es(f"/tmp/pc-nav-{yo}/perfil-3", 5, f"/tmp/pc-nav-{yo}/perfil-3") is False
    # Mío, pero de un ARRANQUE anterior (otro perfil): sobra.
    assert es(f"/tmp/pc-nav-{yo}/perfil-2", 5, f"/tmp/pc-nav-{yo}/perfil-3") is True
    # De un solo uso: sobra sólo pasado el timeout.
    assert es("/tmp/pc-pdf-abc/perfil", 3, None) is False
    assert es("/tmp/pc-img-abc/perfil", pdf_motor.TIMEOUT_S + 31, None) is True


class _FakeProc:
    def __init__(self, pid, name, cmdline, edad_s):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "cmdline": cmdline.split(),
                     "create_time": time.time() - edad_s}
        self.muerto = False

    def kill(self):
        self.muerto = True


def _con_procesos(monkeypatch, procs):
    import types

    fake = types.SimpleNamespace(process_iter=lambda attrs: iter(procs))
    monkeypatch.setitem(sys.modules, "psutil", fake)


def test_el_barrido_mata_lo_que_sobra_y_solo_eso(monkeypatch):
    monkeypatch.setattr(navegador, "_es_proceso_de_la_app", lambda pid: pid == 777)
    procs = [
        # Hijos de un navegador de un proceso de la app ya muerto: sobran.
        _FakeProc(1, "chrome.exe", r"chrome.exe --type=renderer --user-data-dir=C:\T\pc-nav-999\perfil", 5),
        _FakeProc(2, "chrome.exe", r"chrome.exe --type=gpu-process --user-data-dir=C:\T\pc-nav-999\perfil", 5),
        # Del hermano vivo: se queda.
        _FakeProc(3, "msedge.exe", r"msedge.exe --user-data-dir=C:\T\pc-nav-777\perfil", 5),
        # De un PDF de un solo uso que pasó el timeout: sobra.
        _FakeProc(4, "chrome.exe", r"chrome.exe --user-data-dir=C:\T\pc-pdf-x1\perfil", 500),
        # De un PDF que está saliendo AHORA: se queda.
        _FakeProc(5, "chrome.exe", r"chrome.exe --user-data-dir=C:\T\pc-pdf-x2\perfil", 3),
        # Un Chrome abierto a mano en el servidor: ni se mira.
        _FakeProc(6, "chrome.exe", r"chrome.exe --user-data-dir=C:\Users\t\AppData\Local\Google", 9999),
        # Un shell que lleva la palabra en su línea de comando NO es un navegador.
        _FakeProc(7, "bash", "bash -c echo --user-data-dir=/tmp/pc-pdf-test/perfil", 9999),
        _FakeProc(8, "python.exe", "python run.py", 9999),
    ]
    _con_procesos(monkeypatch, procs)
    assert navegador.barrer_procesos_huerfanos(mi_dir=None) == 3
    assert [p.pid for p in procs if p.muerto] == [1, 2, 4]


def test_contar_ve_los_nuestros(monkeypatch):
    procs = [
        _FakeProc(1, "chrome.exe", "chrome.exe --user-data-dir=/tmp/pc-nav-1/perfil", 5),
        _FakeProc(2, "chrome.exe", "chrome.exe --user-data-dir=/tmp/pc-img-1/perfil", 5),
        _FakeProc(3, "chrome.exe", "chrome.exe --user-data-dir=/home/x/.config", 5),
        _FakeProc(4, "bash", "bash --user-data-dir=/tmp/pc-nav-1/perfil", 5),
    ]
    _con_procesos(monkeypatch, procs)
    assert navegador.contar_procesos_nuestros() == 2


def test_el_latido_barre_aunque_el_navegador_este_vivo():
    import inspect

    src = inspect.getsource(navegador._latido)
    assert "barrer_procesos_huerfanos" in src


def test_el_dueno_de_una_carpeta_tiene_que_ser_un_python(monkeypatch):
    """Windows reusa los pids: un svchost con el pid viejo no es el hermano."""
    import types

    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: True)

    class _P:
        def __init__(self, pid):
            self._n = {1: "python.exe", 2: "svchost.exe"}[pid]

        def name(self):
            return self._n

    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=_P))
    assert navegador._es_proceso_de_la_app(1) is True
    assert navegador._es_proceso_de_la_app(2) is False
    monkeypatch.setattr(navegador, "_proceso_vivo", lambda pid: False)
    assert navegador._es_proceso_de_la_app(1) is False


def test_cada_arranque_usa_un_perfil_distinto(monkeypatch, tmp_path):
    """Los hijos del navegador anterior llevan el perfil viejo: por eso se
    los puede barrer sin tocar al que está prendido."""
    monkeypatch.setattr(navegador, "_carpeta_de", lambda pid: tmp_path / "pc-nav-1")
    monkeypatch.setattr(navegador, "_matar_lo_anotado", lambda d: None)
    vistos = []

    class _Proc:
        pid = 99

        def poll(self):
            return None

    def _popen(cmd, **kw):
        vistos.append([c for c in cmd if c.startswith("--user-data-dir=")][0])
        (tmp_path / "pc-nav-1" / vistos[-1].split("=", 1)[1].rsplit("/", 1)[-1]).mkdir(parents=True, exist_ok=True)
        raise OSError("hasta acá llega el test")

    monkeypatch.setattr(navegador.subprocess, "Popen", _popen)
    nav = navegador._Navegador()
    for _ in range(2):
        with pytest.raises(OSError):
            nav._levantar("/bin/falso")
    assert vistos[0].endswith("perfil-1") and vistos[1].endswith("perfil-2")
    assert nav.perfil is not None and str(nav.perfil).endswith("perfil-2")
