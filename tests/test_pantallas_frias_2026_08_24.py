"""Las pantallas que tardaban 10 segundos NO eran lentas: estaban frías.

TMT 2026-08-24 (dueña): *"¿qué más podemos hacer más rápido?"*. Medidas las 33
pantallas del menú en producción, primera visita contra la siguiente:

    /produccion-terminado-asinfo    9,8 s   →   14 ms
    /inventario-rotativo            5,1 s   →   33 ms
    /informes/flujo-produccion      2,5 s   →   14 ms
    /stock/fabricacion-tc           1,7 s   →  1,7 s   ← ésta no cacheaba nada

Las tres primeras ya tenían cache: lo que faltaba era CALENTARLAS, como el
warmup ya hace con el balance y el flujo desde el 18/07. El que entraba justo
después de que vencía el TTL pagaba la carga entera.

La cuarta era otra cosa: `despachos_sin_of` iba a Asinfo sin cache NUNCA, y la
pagan tres pantallas (Inventario en proceso, Balance y Tejeduría) en cada
visita.
"""
from __future__ import annotations

import inspect

from modules._lib import warmup


def test_el_calentador_incluye_las_tres_pantallas_frias():
    src = inspect.getsource(warmup._warm_once)
    for pieza in ("inventario_rotativo", "terminado_asinfo",
                  "fabricacion_proceso", "stock_asinfo_lote_totales",
                  "despachos_sin_of"):
        assert pieza in src, (
            f"{pieza} salió del calentador: la pantalla que lo usa vuelve a "
            "tardar 10 segundos para el primero que entre"
        )


def test_calienta_por_la_pantalla_y_no_por_cada_consulta():
    """Si mañana la pantalla pide una consulta más, se calienta sola."""
    src = inspect.getsource(warmup._warm_once)
    assert "_rot.rotativo()" in src
    assert "_term.resumen(yy, mm)" in src


def test_el_calentador_no_se_cae_si_falta_un_modulo():
    """Fail-soft: un import roto no puede dejar la app sin calentar el resto."""
    src = inspect.getsource(warmup._warm_once)
    assert "warmup pantallas de stock" in src


# ── el cache de despachos_sin_of ───────────────────────────────────────────

def _fake_metabase(monkeypatch, filas, contador):
    from modules._lib import metabase_client

    def _fetch(*a, **k):
        contador.append(1)
        return filas

    monkeypatch.setattr(metabase_client, "fetch_dataset", _fetch)


FILA = {"numero": "OSM-1", "id_bodega": 51, "kg": 4860.0,
        "usuario": "mprima", "descripcion": "A PONCE", "creado": ""}


def test_la_segunda_visita_no_vuelve_a_preguntarle_a_asinfo(monkeypatch):
    from modules.asinfo import hilo_sin_of as hs

    hs.reset_cache()
    n = []
    _fake_metabase(monkeypatch, [FILA], n)
    primera = hs.despachos_sin_of()
    segunda = hs.despachos_sin_of()
    assert primera == segunda
    assert len(n) == 1, "preguntó dos veces: el cache no está funcionando"
    hs.reset_cache()


def test_el_cache_vence(monkeypatch):
    """Se prueba moviendo el RELOJ, no esperando dos minutos."""
    from modules.asinfo import hilo_sin_of as hs

    hs.reset_cache()
    n = []
    _fake_metabase(monkeypatch, [FILA], n)
    reloj = [1000.0]
    monkeypatch.setattr(hs._t, "monotonic", lambda: reloj[0])
    hs.despachos_sin_of()
    reloj[0] += 119
    hs.despachos_sin_of()
    assert len(n) == 1
    reloj[0] += 2                      # 121 s: venció
    hs.despachos_sin_of()
    assert len(n) == 2
    hs.reset_cache()


def test_no_cachea_el_silencio_de_asinfo(monkeypatch):
    """Vacío puede ser "no hay nada" o "no pude preguntar". No se guarda: si
    fuera lo segundo, sostendríamos un "todo bien" que nadie verificó."""
    from modules.asinfo import hilo_sin_of as hs

    hs.reset_cache()
    n = []
    _fake_metabase(monkeypatch, [], n)
    hs.despachos_sin_of()
    hs.despachos_sin_of()
    assert len(n) == 2
    hs.reset_cache()


def test_se_puede_apagar_sin_deploy(monkeypatch):
    from modules.asinfo import hilo_sin_of as hs

    hs.reset_cache()
    monkeypatch.setenv("HILO_SIN_OF_CACHE_SECS", "0")
    n = []
    _fake_metabase(monkeypatch, [FILA], n)
    hs.despachos_sin_of()
    hs.despachos_sin_of()
    assert len(n) == 2
    hs.reset_cache()


def test_cada_pregunta_tiene_su_propia_entrada(monkeypatch):
    """`dias=0` (el placeholder) y el barrido del vigía no se pisan."""
    from modules.asinfo import hilo_sin_of as hs

    hs.reset_cache()
    n = []
    _fake_metabase(monkeypatch, [FILA], n)
    hs.despachos_sin_of(dias=0)
    hs.despachos_sin_of(dias=3)
    assert len(n) == 2
    hs.despachos_sin_of(dias=0)
    assert len(n) == 2
    hs.reset_cache()


def test_las_telas_nuevas_del_rotativo_tambien_se_calientan():
    """02/09/2026: /inventario-rotativo seguía tardando 1,3 s en caliente —
    `rotativo()` estaba calentado pero `nuevos()` (otra ida a Asinfo) no."""
    src = inspect.getsource(warmup._warm_once)
    assert "_rot.nuevos()" in src
