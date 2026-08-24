"""Las facturas del día entran en minutos, no en media hora.

TMT 2026-08-24 (dueña), mirando la pantalla del día con varias facturas en
ámbar: *"pero raro.. hay demasiadas. podés chequear si lo de dos mins es
real"*. No era real. Medido ese día contra producción, sobre 68 facturas, la
demora entre "Asinfo la emitió" y "está en el programa" daba **mediana 8,8
min, 9 de cada 10 dentro de 14 min y la peor en 29,5 min**, con el ciclo real
en ~6 minutos: el `sleep` era de 120 s, pero la carga de facturas viajaba en
la MISMA fila que el sync de clientes, el de colores, los químicos, la
tejeduría, la traza y la foto del día.

Dos cosas lo arreglan y las dos se testean acá:

1. las facturas corren en su PROPIO hilo (`_loop_facturas`), sin esperar a
   nadie;
2. la carga le pide a Asinfo data FRESCA (`max_edad_secs`). Sin eso el cache
   de 5 minutos de `facturas_periodo` se comía dos de cada tres corridas y la
   demora nunca bajaba de 5 minutos, por más seguido que corriera el hilo.

Y de yapa: con el hilo vivo, abrir /facturas ya no espera a Asinfo.
"""
from __future__ import annotations

import inspect

from modules._lib import autocarga_facturas as ac


def test_las_facturas_tienen_su_propio_hilo():
    """Si vuelven a la fila del ciclo lento, la demora vuelve a los 9 min."""
    src = inspect.getsource(ac._loop_facturas)
    assert "_auto_cargar_facturas_hoy" in src
    # …y NADA del ciclo lento se cuela acá adentro (el docstring no cuenta:
    # ahí se nombran justamente los trabajos que se sacaron).
    src = src.replace(ac._loop_facturas.__doc__ or "", "")
    for ajeno in ("sync_asinfo", "colores_asinfo", "formulas_bridge",
                  "tejeduria_asinfo", "traza", "auto_refresco"):
        assert ajeno not in src, (
            f"{ajeno} se coló en el hilo de facturas: eso lo vuelve a hacer "
            "esperar y la demora sube"
        )


def test_el_ciclo_lento_ya_no_carga_las_facturas():
    """Una sola cosa la hace, y es la que tiene el reloj rápido."""
    assert "_auto_cargar_facturas_hoy" not in inspect.getsource(ac._loop)


def test_arrancan_los_dos_hilos():
    src = inspect.getsource(ac.start_auto_carga_thread)
    assert "target=_loop_facturas" in src
    assert "target=_loop" in src


def test_la_carga_le_pide_a_asinfo_data_fresca():
    """Con el cache de 5 min, correr cada 2 no sirve de nada.

    Va por AST y no por texto: un comentario que NOMBRA `max_edad_secs` no es
    lo mismo que pasárselo, y un candado de texto se lo come igual.
    """
    import ast
    import textwrap

    from modules.facturas import views

    arbol = ast.parse(textwrap.dedent(
        inspect.getsource(views._auto_cargar_facturas_hoy)))
    llamadas = [
        n for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "facturas_periodo"
    ]
    assert llamadas, "la carga ya no le pregunta a Asinfo por las facturas"
    for c in llamadas:
        assert any(k.arg == "max_edad_secs" for k in c.keywords), (
            "sin `max_edad_secs` la carga le pregunta al cache de 5 minutos y "
            "la factura recién emitida no aparece hasta que el cache venza"
        )


def test_facturas_periodo_respeta_la_edad_pedida(monkeypatch):
    """El parámetro no es decorativo: manda sobre el TTL."""
    from modules.asinfo import service

    llamadas = []

    def _card(*a, **k):
        llamadas.append(1)
        return [{"numero": "001-099-000000001", "kg": 1, "usd": 1,
                 "fecha": "2026-08-24", "cliente_codigo": "AJO"}]

    monkeypatch.setattr(service, "fetch_card_from_env", _card)
    monkeypatch.setattr(service, "_aplicar_sucursales",
                        lambda rows, *a, **k: rows)
    service.reset_facturas_cache()
    service.facturas_periodo("2026-08-24", "2026-08-24")
    assert len(llamadas) == 1
    # Sin pedir frescura: contesta el cache.
    service.facturas_periodo("2026-08-24", "2026-08-24")
    assert len(llamadas) == 1
    # Pidiendo data de menos de 0 s de vieja: vuelve a preguntar.
    service.facturas_periodo("2026-08-24", "2026-08-24", max_edad_secs=0)
    assert len(llamadas) == 2
    service.reset_facturas_cache()


def test_la_pantalla_no_espera_a_asinfo_si_el_hilo_esta_vivo():
    """*"cargar la página facturas es un poco lento"*: esto era ~1 s de más."""
    from modules.facturas import views

    src = inspect.getsource(views.lista)
    assert "hilo_de_facturas_vivo" in src
    assert "not hilo_de_facturas_vivo()" in src

    from modules.historial import views as hviews
    assert "hilo_de_facturas_vivo" in inspect.getsource(hviews)


def test_el_hilo_vivo_se_reconoce_por_el_nombre(monkeypatch):
    """Y si no está, la pantalla vuelve a cargar ella misma."""
    class _T:
        def __init__(self, name, vivo):
            self.name, self._v = name, vivo

        def is_alive(self):
            return self._v

    monkeypatch.setattr(ac.threading, "enumerate",
                        lambda: [_T("ciclo-de-fondo", True)])
    assert ac.hilo_de_facturas_vivo() is False
    monkeypatch.setattr(ac.threading, "enumerate",
                        lambda: [_T("auto-carga-facturas", False)])
    assert ac.hilo_de_facturas_vivo() is False
    monkeypatch.setattr(ac.threading, "enumerate",
                        lambda: [_T("auto-carga-facturas", True)])
    assert ac.hilo_de_facturas_vivo() is True
