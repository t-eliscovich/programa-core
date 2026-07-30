"""Tejeduría corre SOLA y avisa los kilos que nadie compró (TMT 2026-07-30).

Dueña: *"tejeduría tiene que correr sola"*. Antes la carga se disparaba SÓLO al
abrir /produccion-tejeduria-asinfo: si nadie entraba no cargaba nada, y si
entraban tres corría tres veces. Ahora la corre el hilo de fondo.

Los "kilos producidos y no comprados" de meses cerrados NO se avisan a propósito
(a7264f6: no son un pasivo, son el arranque de PC) — el test de abajo lo fija.
"""
from unittest.mock import patch

from modules.tejeduria_asinfo import service as tej


def test_correr_si_toca_carga_el_mes_y_respeta_el_freno(monkeypatch):
    monkeypatch.setattr(tej, "_auto_ultimo_ts", 0.0)
    with patch.object(tej, "cargar_pendientes",
                      return_value={"creadas": 2, "importe": 1630.0}) as cp:
        r1 = tej.correr_si_toca()
        r2 = tej.correr_si_toca()   # enseguida: la frena el intervalo de 30 min
    assert r1["corrio"] is True and r1["creadas"] == 2
    assert r2["corrio"] is False
    assert cp.call_count == 1
    # Corre sobre el mes EN CURSO y se marca como carga automática.
    assert cp.call_args.kwargs["usuario"] == tej.MARCADOR_CARGA


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
