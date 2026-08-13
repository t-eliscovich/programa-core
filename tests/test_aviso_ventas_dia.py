"""Cierre del día en la campanita (TMT 2026-07-30).

Dueña: *"agregar en la campanita, a fin de día, venta total kg y total facturas
$"* → *"plata arriba"*, *"18 hs Ecuador"*.
"""
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from modules.facturas import aviso_ventas as av


def _reset(monkeypatch):
    monkeypatch.setattr(av, "_ultimo_dia_avisado", None)


def _a_las(hora_ec: int) -> datetime:
    """Lo que devuelve `_ahora_ec()`: el reloj de Ecuador, no el del servidor."""
    return datetime(2026, 7, 30, hora_ec, 0, tzinfo=UTC)


def test_a_las_19_de_ecuador_avisa_con_la_plata_adelante(monkeypatch):
    _reset(monkeypatch)
    puestos = []
    with patch.object(av, "_ahora_ec", return_value=_a_las(19)), \
         patch.object(av, "today_ec", return_value=date(2026, 7, 30)), \
         patch.object(av, "totales_dia",
                      return_value={"n": 113, "importe": 116230.45, "kg": 13565.54}), \
         patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        r = av.correr_si_toca()
    assert r["avisado"] is True
    a = puestos[0]
    assert a["fuente"] == "ventas"
    assert a["titulo"] == "Ventas de hoy · $ 116.230,45"   # la plata, arriba
    assert a["detalle"] == "13.565,54 kg · 113 facturas"
    assert a["clave"] == "ventas:2026-07-30"
    assert a["url"] == "/facturas?desde=2026-07-30&hasta=2026-07-30"


def test_una_sola_factura_va_en_singular(monkeypatch):
    _reset(monkeypatch)
    puestos = []
    with patch.object(av, "_ahora_ec", return_value=_a_las(20)), \
         patch.object(av, "today_ec", return_value=date(2026, 7, 30)), \
         patch.object(av, "totales_dia",
                      return_value={"n": 1, "importe": 1000.0, "kg": 100.0}), \
         patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        av.correr_si_toca()
    assert puestos[0]["detalle"] == "100,00 kg · 1 factura"


def test_antes_de_las_18_de_ECUADOR_no_avisa(monkeypatch):
    """A las 22 UTC en Ecuador son las 17 — todavía no."""
    _reset(monkeypatch)
    with patch.object(av, "_ahora_ec", return_value=_a_las(17)), \
         patch("modules.avisos.avisar") as avisar:
        r = av.correr_si_toca()
    assert r["avisado"] is False and "18" in r["motivo"]
    avisar.assert_not_called()


def test_no_repite_el_aviso_en_el_mismo_dia(monkeypatch):
    _reset(monkeypatch)
    puestos = []
    with patch.object(av, "_ahora_ec", return_value=_a_las(19)), \
         patch.object(av, "today_ec", return_value=date(2026, 7, 30)), \
         patch.object(av, "totales_dia",
                      return_value={"n": 5, "importe": 10.0, "kg": 1.0}), \
         patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        av.correr_si_toca()
        av.correr_si_toca()   # el ciclo de fondo pasa cada 2 minutos
        av.correr_si_toca()
    assert len(puestos) == 1


def test_un_dia_sin_facturas_no_enciende_la_campanita(monkeypatch):
    _reset(monkeypatch)
    with patch.object(av, "_ahora_ec", return_value=_a_las(19)), \
         patch.object(av, "today_ec", return_value=date(2026, 7, 30)), \
         patch.object(av, "totales_dia",
                      return_value={"n": 0, "importe": 0.0, "kg": 0.0}), \
         patch("modules.avisos.avisar") as avisar:
        r = av.correr_si_toca()
    assert r["motivo"] == "sin facturas"
    avisar.assert_not_called()
    # …y el día NO queda marcado: si entra una factura a las 19:30, avisa.
    assert av._ultimo_dia_avisado is None


def test_se_puede_apagar_y_correr_la_hora_por_ambiente(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("VENTAS_AVISO", "0")
    with patch("modules.avisos.avisar") as avisar:
        assert av.correr_si_toca()["motivo"] == "apagado"
    avisar.assert_not_called()
    monkeypatch.delenv("VENTAS_AVISO")
    monkeypatch.setenv("VENTAS_AVISO_HORA", "20")
    assert av._hora_aviso() == 20
    monkeypatch.setenv("VENTAS_AVISO_HORA", "no-es-un-numero")
    assert av._hora_aviso() == 19


def test_el_universo_es_lo_facturado_del_dia_sin_anuladas():
    """NO es la cartera: una factura cobrada el mismo día fue venta igual."""
    with patch.object(av.db, "fetch_one",
                      return_value={"n": 3, "importe": 100, "kg": 10}) as f:
        av.totales_dia(date(2026, 7, 30))
    sql = f.call_args[0][0]
    assert "FROM scintela.factura" in sql
    assert "WHERE fecha = %s" in sql
    assert "COALESCE(stat, '') <> 'X'" in sql
    assert "saldo" not in sql          # cartera no


def test_totales_dia_fail_soft():
    def explota(*a, **k):
        raise RuntimeError("timeout")

    with patch.object(av.db, "fetch_one", explota):
        assert av.totales_dia(date(2026, 7, 30)) == {"n": 0, "importe": 0.0, "kg": 0.0}


def test_el_hilo_de_fondo_lo_llama():
    import inspect

    from modules._lib import autocarga_facturas

    src = inspect.getsource(autocarga_facturas)
    assert "aviso_ventas" in src and "correr_si_toca" in src


def test_la_hora_sale_del_reloj_de_ECUADOR_no_del_servidor():
    """El servidor corre en UTC, 5 h adelante: a las 21 UTC allá son las 16."""
    ahora_utc = datetime.now(UTC)
    assert av._ahora_ec().hour == (ahora_utc - timedelta(hours=5)).hour


# ── Los umbrales de kilos se retiraron (TMT 2026-08-13) ─────────────────────
# Dueña: *"si está pinned, ya no necesitamos anuncio por cada 5k, 10k, 15k y
# 20k goal"*. El número del día quedó fijo arriba de la campanita y en la
# pantalla de inicio (ver tests/test_ventas_hoy_recuadro.py), así que el aviso
# dejó de informar: sólo interrumpía.

def test_ya_no_hay_avisos_por_umbral_de_kilos():
    assert not hasattr(av, "correr_umbrales_kg")
    assert not hasattr(av, "UMBRALES_KG")
    # …y el ciclo de fondo tampoco lo llama.
    import inspect

    from modules._lib import autocarga_facturas

    src = inspect.getsource(autocarga_facturas)
    assert "correr_umbrales_kg" not in src
    assert "correr_si_toca" in src          # el cierre de las 19 SIGUE


def test_el_cierre_de_las_19_sigue_usando_su_propia_clave(monkeypatch):
    """Los dos avisos conviven: claves distintas, no se pisan entre sí."""
    _reset(monkeypatch)
    puestos = []
    with patch.object(av, "_ahora_ec", return_value=_a_las(19)), \
         patch.object(av, "today_ec", return_value=date(2026, 8, 7)), \
         patch.object(av, "totales_dia",
                      return_value={"n": 9, "importe": 1.0, "kg": 21_000.0}), \
         patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        av.correr_si_toca()
    assert puestos[0]["clave"] == "ventas:2026-08-07"
    assert not puestos[0]["clave"].startswith("ventas-kg:")



