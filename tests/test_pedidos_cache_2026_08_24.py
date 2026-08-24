"""/pedidos tardaba 3,6 s SIEMPRE. Tres consultas a Asinfo, ninguna cacheada.

TMT 2026-08-24 (dueña): *"¿qué más podemos hacer más rápido?"*. Medida en
producción, /pedidos era la pantalla más lenta de las que tardan siempre —no
sólo la primera vez—: `pendientes()`, `pedidos_por_color()` y
`acabados_por_producto()` iban a Asinfo en cada visita.

Los pedidos pendientes no cambian de un segundo al otro. Ahora valen 5 minutos
y el warmup los refresca antes de que venzan, así que la pantalla abre con el
dato ya traído.
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from modules.pedidos import service


@pytest.fixture(autouse=True)
def _limpio():
    service.reset_cache()
    yield
    service.reset_cache()


def _fila():
    return {"categoria": "Fleece", "tela": "Fleece 96", "codigo": "FE96CAF",
            "color": "CAF", "ped_kg": 447.0, "ped_rollos": 19.0, "ped_un": 0.0,
            "un_por_kg": None, "n_pedidos": 3, "n_clientes": 2,
            "mas_viejo": "2026-08-05", "inv_kg": 0.0, "prod_kg": 0.0,
            "n_ordenes": 0}


def test_la_segunda_visita_no_vuelve_a_preguntar():
    n = []

    def fake(_db, _sql, **_kw):
        n.append(1)
        return [_fila()], True

    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=fake):
        a, ok_a = service.pendientes()
        b, ok_b = service.pendientes()
    assert ok_a and ok_b
    assert a == b
    assert len(n) == 1, "le preguntó dos veces a Asinfo: el cache no anda"


def test_no_cachea_cuando_asinfo_no_contesta():
    """`disponible=False` es "no pude preguntar", no "no hay pedidos". Guardar
    eso 5 minutos sería sostener un cartel equivocado."""
    n = []

    def fake(_db, _sql, **_kw):
        n.append(1)
        return [], False

    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=fake):
        _, ok1 = service.pendientes()
        _, ok2 = service.pendientes()
    assert not ok1 and not ok2
    assert len(n) == 2


def test_el_cache_vence(monkeypatch):
    """Se prueba moviendo el RELOJ, no esperando cinco minutos."""
    n = []

    def fake(_db, _sql, **_kw):
        n.append(1)
        return [_fila()], True

    reloj = [500.0]
    monkeypatch.setattr(service._time, "monotonic", lambda: reloj[0])
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=fake):
        service.pendientes()
        reloj[0] += 299
        service.pendientes()
        assert len(n) == 1
        reloj[0] += 2
        service.pendientes()
    assert len(n) == 2


def test_las_filas_salen_COPIADAS():
    """`marcar_acabado` les escribe encima. Sin la copia, la segunda visita
    encontraría las filas de la primera ya pintadas — y peor: pintadas con un
    acabado que quizás ya cambió."""
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([_fila()], True)):
        a, _ = service.pendientes()
        a[0]["acabado"] = "TUB"
        b, _ = service.pendientes()
    assert "acabado" not in b[0]


def test_se_puede_apagar_sin_deploy(monkeypatch):
    monkeypatch.setenv("PEDIDOS_CACHE_SECS", "0")
    n = []

    def fake(_db, _sql, **_kw):
        n.append(1)
        return [_fila()], True

    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=fake):
        service.pendientes()
        service.pendientes()
    assert len(n) == 2


def test_las_tres_consultas_estan_cacheadas():
    for fn in (service.pendientes, service.pedidos_por_color,
               service.acabados_por_producto):
        assert "_cacheado(" in inspect.getsource(fn), (
            f"{fn.__name__} volvió a preguntarle a Asinfo en cada visita"
        )


def test_el_calentador_las_deja_listas():
    from modules._lib import warmup

    src = inspect.getsource(warmup._warm_once)
    for paso in ("pedidos_pendientes", "pedidos_por_color", "pedidos_acabados"):
        assert paso in src
