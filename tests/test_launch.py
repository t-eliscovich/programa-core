"""`scripts/launch.py` — el lanzador sin PowerShell (fase 3 del plan de memoria)."""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def launch():
    spec = importlib.util.spec_from_file_location("launch", ROOT / "scripts" / "launch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pone_las_variables_de_maquina_sin_pisar_las_que_ya_estan(launch, monkeypatch):
    monkeypatch.setenv("YA_ESTABA", "mia")
    monkeypatch.delenv("NUEVA", raising=False)
    n = launch.cargar_entorno({"YA_ESTABA": "del registro", "NUEVA": "x"})
    assert n == 1
    assert os.environ["YA_ESTABA"] == "mia" and os.environ["NUEVA"] == "x"


def test_fuera_de_windows_el_registro_esta_vacio(launch):
    if os.name != "nt":
        assert launch.variables_de_maquina() == {}


def test_rota_los_logs_viejos_y_deja_los_nuevos(launch, tmp_path):
    viejo, nuevo = tmp_path / "oficina-2026-08-01.log", tmp_path / "oficina-2026-09-05.log"
    viejo.write_text("x")
    nuevo.write_text("y")
    ahora = time.time()
    os.utime(viejo, (ahora - 20 * 86400, ahora - 20 * 86400))
    assert launch.rotar_logs(tmp_path, dias=14, ahora=ahora) == 1
    assert not viejo.exists() and nuevo.exists()


def test_el_log_lleva_el_nombre_y_el_dia(launch, tmp_path):
    assert launch.archivo_de_log("portal", tmp_path, date(2026, 9, 6)).name == "portal-2026-09-06.log"


def test_redirige_stdout_stderr_y_logging_al_archivo(launch, tmp_path):
    import logging

    out, err = sys.stdout, sys.stderr
    ruta = tmp_path / "x.log"
    f = launch.redirigir_salida(ruta)
    try:
        print("hola")
        print("uy", file=sys.stderr)
        logging.getLogger("prueba").info("desde logging")
    finally:
        sys.stdout, sys.stderr = out, err
        f.close()
        logging.basicConfig(force=True)
    texto = ruta.read_text()
    assert "hola" in texto and "uy" in texto and "desde logging" in texto


def test_dice_que_variable_obligatoria_falta(launch):
    assert launch.faltantes({"DB_HOST": "h", "DB_NAME": "n", "DB_USER": "", "DB_PASSWORD": "p"}) == ["DB_USER"]
    assert launch.faltantes({v: "x" for v in launch.OBLIGATORIAS}) == []


def test_un_nombre_desconocido_no_arranca_nada(launch):
    with pytest.raises(SystemExit):
        launch.preparar("erp")


def test_el_portal_prende_el_modo_y_su_puerto(launch, tmp_path, monkeypatch):
    out, err = sys.stdout, sys.stderr
    monkeypatch.setattr(launch, "LOGS", tmp_path)
    monkeypatch.setattr(launch, "variables_de_maquina", lambda: {})
    monkeypatch.setattr(launch, "archivo_de_log", lambda nombre, carpeta=tmp_path, hoy=None: tmp_path / f"{nombre}.log")
    # setenv y no delenv: delenv de una variable que NO está no anota nada y
    # el MODO=portal se filtraba a los tests que venían después (la app entera
    # quedaba en modo portal y contestaba "No encontrado").
    monkeypatch.setenv("MODO", "")
    monkeypatch.setenv("PUERTO_APP", "")
    try:
        assert launch.preparar("portal") == 5004
    finally:
        sys.stdout, sys.stderr = out, err
    assert os.environ["MODO"] == "portal" and os.environ["PUERTO_APP"] == "5004"
    assert "arranque portal" in (tmp_path / "portal.log").read_text()
