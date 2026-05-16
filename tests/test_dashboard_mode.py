"""Tests del switch DASHBOARD_MODE (parallel vs sequential) y la instrumentación."""
from __future__ import annotations

import os
import time

import pytest

from modules.dashboard import views as dv


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    # No queremos que un test cambie env var y afecte otros.
    monkeypatch.delenv("DASHBOARD_MODE", raising=False)


def test_default_mode_es_parallel():
    assert dv._dashboard_mode() == "parallel"


def test_mode_sequential_leido_desde_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MODE", "sequential")
    assert dv._dashboard_mode() == "sequential"


@pytest.mark.parametrize("value", ["sequential", "seq", "serial", "SEQUENTIAL"])
def test_mode_acepta_aliases(monkeypatch, value):
    monkeypatch.setenv("DASHBOARD_MODE", value)
    assert dv._dashboard_mode() == "sequential"


@pytest.mark.parametrize("value", ["parallel", "something-weird", "", "true"])
def test_mode_fallback_a_parallel(monkeypatch, value):
    monkeypatch.setenv("DASHBOARD_MODE", value)
    assert dv._dashboard_mode() == "parallel"


def test_run_with_fallback_exito():
    res, ms, err = dv._run_with_fallback((lambda: 42, None), "test")
    assert res == 42
    assert err is None
    assert ms >= 0


def test_run_with_fallback_excepcion_devuelve_default():
    def boom():
        raise ValueError("x")
    res, ms, err = dv._run_with_fallback((boom, "default"), "test")
    assert res == "default"
    assert err == "ValueError"
    assert ms >= 0


def test_parallel_modo_parallel_retorna_mismos_resultados(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MODE", "parallel")
    tasks = {
        "a": (lambda: 1, None),
        "b": (lambda: 2, None),
        "c": (lambda: 3, None),
    }
    result = dv._parallel(tasks)
    assert result == {"a": 1, "b": 2, "c": 3}


def test_parallel_modo_sequential_retorna_mismos_resultados(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MODE", "sequential")
    tasks = {
        "a": (lambda: 1, None),
        "b": (lambda: 2, None),
        "c": (lambda: 3, None),
    }
    result = dv._parallel(tasks)
    assert result == {"a": 1, "b": 2, "c": 3}


def test_parallel_modo_sequential_corre_en_orden(monkeypatch):
    """Si es secuencial, el orden de ejecución es determinista."""
    monkeypatch.setenv("DASHBOARD_MODE", "sequential")
    order: list[str] = []
    def _mk(name):
        def _f():
            order.append(name)
            return name
        return _f
    dv._parallel({
        "a": (_mk("a"), None),
        "b": (_mk("b"), None),
        "c": (_mk("c"), None),
    })
    assert order == ["a", "b", "c"]


def test_parallel_modo_parallel_es_mas_rapido_con_io_sleep(monkeypatch):
    """Sanity — con I/O simulado via sleep, paralelo debería ser más rápido."""
    monkeypatch.setenv("DASHBOARD_MODE", "sequential")
    tasks = {f"t{i}": (lambda: (time.sleep(0.02), 1)[1], 0) for i in range(4)}

    t0 = time.perf_counter()
    dv._parallel(tasks)
    seq_ms = (time.perf_counter() - t0) * 1000

    monkeypatch.setenv("DASHBOARD_MODE", "parallel")
    t0 = time.perf_counter()
    dv._parallel(tasks)
    par_ms = (time.perf_counter() - t0) * 1000

    # Con 4 tareas de 20ms cada una: seq ~80ms, par ~20-30ms.
    # Toleramos CI lento: par debe ser al menos 1.5x más rápido.
    assert par_ms * 1.5 < seq_ms, f"par={par_ms:.0f}ms seq={seq_ms:.0f}ms"


def test_parallel_excepcion_no_rompe_resultado_agregado(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MODE", "sequential")
    def boom():
        raise ValueError("x")
    result = dv._parallel({
        "ok": (lambda: "valor", None),
        "fail": (boom, "fallback"),
    })
    assert result == {"ok": "valor", "fail": "fallback"}


def test_parallel_callable_sin_tuple_tambien_funciona(monkeypatch):
    """Un valor que no es tuple se toma como (fn, None)."""
    monkeypatch.setenv("DASHBOARD_MODE", "sequential")
    result = dv._parallel({"a": lambda: 99})
    assert result == {"a": 99}


def test_parallel_tasks_vacio_no_rompe():
    result = dv._parallel({})
    assert result == {}
