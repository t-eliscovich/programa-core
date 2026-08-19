"""Tejeduría corre SOLA y avisa los kilos que nadie compró (TMT 2026-07-30).

Dueña: *"tejeduría tiene que correr sola"*. Antes la carga se disparaba SÓLO al
abrir /produccion-tejeduria-asinfo: si nadie entraba no cargaba nada, y si
entraban tres corría tres veces. Ahora la corre el hilo de fondo.

Los "kilos producidos y no comprados" de meses cerrados NO se avisan a propósito
(a7264f6: no son un pasivo, son el arranque de PC) — el test de abajo lo fija.
"""
from unittest.mock import patch

from modules.tejeduria_asinfo import service as tej


def test_correr_si_toca_carga_la_ventana_y_respeta_el_freno(monkeypatch):
    monkeypatch.setattr(tej, "_auto_ultimo_ts", 0.0)
    with patch.object(tej, "cargar_pendientes",
                      return_value={"creadas": 2, "importe": 1630.0}) as cp:
        r1 = tej.correr_si_toca()
        r2 = tej.correr_si_toca()   # enseguida: la frena el intervalo de 30 min
    assert r1["corrio"] is True
    assert r2["corrio"] is False
    # Barre la ventana entera, no sólo el mes en curso (TMT 2026-08-19: la
    # orden abierta en julio nunca fue vista y su pasivo sigue faltando).
    assert cp.call_count == tej.MESES_VENTANA_TOPE
    assert r1["creadas"] == 2 * tej.MESES_VENTANA_TOPE
    assert cp.call_args.kwargs["usuario"] == tej.MARCADOR_CARGA


def test_la_ventana_va_del_mas_viejo_al_mas_nuevo():
    import datetime as _dt
    assert tej._meses_a_barrer(_dt.date(2026, 8, 19), meses=3) == [
        (2026, 6), (2026, 7), (2026, 8)]
    # y cruza el año sin romperse
    assert tej._meses_a_barrer(_dt.date(2026, 1, 15), meses=3) == [
        (2025, 11), (2025, 12), (2026, 1)]


def test_correr_si_toca_se_puede_apagar_por_ambiente(monkeypatch):
    monkeypatch.setattr(tej, "_auto_ultimo_ts", 0.0)
    monkeypatch.setenv("TEJEDURIA_AUTO", "0")
    with patch.object(tej, "cargar_pendientes") as cp:
        assert tej.correr_si_toca()["corrio"] is False
    cp.assert_not_called()


def test_correr_si_toca_nunca_levanta(monkeypatch):
    monkeypatch.setattr(tej, "_auto_ultimo_ts", 0.0)

    def explota(*a, **k):
        raise RuntimeError("Asinfo se cayó")

    with patch.object(tej, "cargar_pendientes", explota):
        assert tej.correr_si_toca()["creadas"] == 0


def test_el_hilo_de_fondo_llama_a_tejeduria():
    """La regla que la dueña pidió: no depende de que alguien abra la pantalla."""
    import inspect

    from modules._lib import autocarga_facturas

    src = inspect.getsource(autocarga_facturas)
    assert "tejeduria_asinfo" in src and "correr_si_toca" in src


def test_no_avisa_los_kilos_de_meses_cerrados():
    """No es una deuda: es que las compras K de PC arrancan en mayo (a7264f6).

    La resta contra la producción de Asinfo —que tiene la historia completa— se
    mueve 12.000 kg con sólo correr la ventana un mes. La dueña hizo borrar la
    columna que lo mostraba; meterlo en la campanita sería lo mismo por la
    ventana. Si alguien lo vuelve a agregar, este test se cae.
    """
    assert not hasattr(tej, "kilos_sin_comprar")
    assert not hasattr(tej, "avisar_kilos_sin_comprar")
