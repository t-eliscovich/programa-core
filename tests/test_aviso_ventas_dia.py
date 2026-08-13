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


# ── Umbrales de kilos durante la jornada (TMT 2026-08-07) ────────────────────
# Dueña: *"otra notificación de facturas cuando la venta del día sobrepase
# 10kg y cuando sobrepase 15kg y luego 20kg… entre la jornada laboral"*
# (miles de kilos, confirmado en el chat).

def _umbral_corrido(hora_ec, kg, *, ya=0.0, n=104, importe=128410.20):
    """Corre correr_umbrales_kg() con el reloj, los totales y el "ya avisado"
    puestos a mano. Devuelve (resultado, avisos_puestos)."""
    puestos = []
    with patch.object(av, "_ahora_ec", return_value=_a_las(hora_ec)), \
         patch.object(av, "today_ec", return_value=date(2026, 8, 7)), \
         patch.object(av, "totales_dia",
                      return_value={"n": n, "importe": importe, "kg": kg}), \
         patch.object(av, "ultimo_umbral_avisado", return_value=ya), \
         patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        r = av.correr_umbrales_kg()
    return r, puestos


def test_al_cruzar_los_10000_kg_avisa_con_el_mismo_formato_que_el_cierre():
    r, puestos = _umbral_corrido(11, kg=10_240.5, importe=86_120.0, n=71)
    assert r["avisado"] is True and r["umbral"] == 10_000.0
    a = puestos[0]
    assert a["fuente"] == "ventas"
    assert a["titulo"] == "Ventas del día · $ 86.120,00"    # la plata, arriba
    assert a["detalle"] == "10.240,50 kg · 71 facturas"
    assert a["url"] == "/facturas?desde=2026-08-07&hasta=2026-08-07"
    assert a["clave"] == "ventas-kg:2026-08-07:10000"


def test_los_tres_umbrales_son_10000_15000_y_20000_kg():
    """Miles de kilos: un día normal cierra en 13.500-16.000 kg."""
    assert av.UMBRALES_KG == (10_000.0, 15_000.0, 20_000.0)


def test_el_15000_avisa_recien_cuando_ya_se_habia_avisado_el_10000():
    r, puestos = _umbral_corrido(14, kg=15_410.5, ya=10_000.0)
    assert r["umbral"] == 15_000.0
    assert puestos[0]["detalle"].startswith("15.410,50 kg")
    assert puestos[0]["clave"] == "ventas-kg:2026-08-07:15000"


def test_un_lote_grande_que_salta_dos_umbrales_manda_UN_solo_aviso():
    """De 4.000 a 16.000 kg de una: sale el de 15.000, NO también el de 10.000."""
    r, puestos = _umbral_corrido(9, kg=16_000.0, ya=0.0)
    assert len(puestos) == 1
    assert r["umbral"] == 15_000.0


def test_no_repite_el_umbral_que_ya_aviso():
    r, puestos = _umbral_corrido(15, kg=15_800.0, ya=15_000.0)
    assert r["avisado"] is False and puestos == []
    assert "umbral" in r["motivo"]


def test_debajo_del_primer_umbral_no_dice_nada():
    r, puestos = _umbral_corrido(10, kg=9_999.0)
    assert r["avisado"] is False and puestos == []


def test_fuera_de_la_jornada_laboral_no_avisa_umbrales():
    """Antes de las 8 no hay a quién avisarle; de 19 en adelante manda el cierre."""
    for hora in (7, 19, 20, 23):
        r, puestos = _umbral_corrido(hora, kg=21_000.0)
        assert r["avisado"] is False, hora
        assert "jornada" in r["motivo"], hora
        assert puestos == []
    # …y a las 8 en punto sí.
    r, _ = _umbral_corrido(8, kg=21_000.0)
    assert r["avisado"] is True and r["umbral"] == 20_000.0


def test_un_dia_sin_facturas_no_enciende_la_campanita_por_umbral():
    with patch.object(av, "_ahora_ec", return_value=_a_las(11)), \
         patch.object(av, "today_ec", return_value=date(2026, 8, 7)), \
         patch.object(av, "totales_dia",
                      return_value={"n": 0, "importe": 0.0, "kg": 0.0}), \
         patch("modules.avisos.avisar") as avisar:
        r = av.correr_umbrales_kg()
    assert r["motivo"] == "sin facturas"
    avisar.assert_not_called()


def test_el_hasta_donde_avise_se_lee_de_la_BASE_no_de_una_variable():
    """Un restart del server a media mañana no puede repetir los umbrales."""
    with patch.object(av.db, "fetch_one", return_value={"u": 15000}) as f:
        assert av.ultimo_umbral_avisado("2026-08-07") == 15_000.0
    sql, params = f.call_args[0][0], f.call_args[0][1]
    assert "FROM scintela.aviso" in sql and "clave LIKE" in sql
    assert params == ("ventas-kg:2026-08-07:%",)
    # …y si la lectura falla, cae a 0: el ON CONFLICT (clave) de avisar() es el
    # segundo candado, así que no se duplica igual.
    def explota(*a, **k):
        raise RuntimeError("timeout")

    with patch.object(av.db, "fetch_one", explota):
        assert av.ultimo_umbral_avisado("2026-08-07") == 0.0


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


def test_umbrales_y_franja_configurables_por_ambiente(monkeypatch):
    monkeypatch.setenv("VENTAS_KG_UMBRALES", "8000, 12000")
    assert av._umbrales_kg() == (8_000.0, 12_000.0)
    monkeypatch.setenv("VENTAS_KG_UMBRALES", "no-es-un-numero")
    assert av._umbrales_kg() == av.UMBRALES_KG      # basura → los de siempre
    monkeypatch.delenv("VENTAS_KG_UMBRALES")
    monkeypatch.setenv("VENTAS_KG_DESDE", "9")
    monkeypatch.setenv("VENTAS_KG_HASTA", "17")
    assert av._franja_kg() == (9, 17)
    monkeypatch.setenv("VENTAS_KG_DESDE", "20")     # dados vuelta → se ignoran
    assert av._franja_kg() == (av.HORA_DESDE_KG, av.HORA_HASTA_KG)
    monkeypatch.setenv("VENTAS_AVISO", "0")
    assert av.correr_umbrales_kg()["motivo"] == "apagado"


def test_el_hilo_de_fondo_llama_tambien_a_los_umbrales():
    import inspect

    from modules._lib import autocarga_facturas

    src = inspect.getsource(autocarga_facturas)
    assert "correr_umbrales_kg" in src
