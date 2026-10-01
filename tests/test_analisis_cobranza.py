"""Análisis de cobranza (/analisis/cobranza, 30/09/2026)."""
from datetime import date

from modules.analisis import cobranza as cob

MESES = [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
HOY = date(2026, 9, 29)


def _cli(cod, **kw):
    base = dict(cod=cod, nombre=cod, vend="Casa", cupo=0, stop="", fac=0, ch=0,
                reb=0, n_reb=0, n_reb_hist=0, reb_dias=None, edad_max=None,
                venta90=9000, pagado=None, dias_plata=None)
    base.update(kw)
    return base


def _armar(clientes, por_mes=()):
    return cob.armar(list(clientes), list(por_mes), [], MESES, HOY)


def _uno(d, cod):
    return next(c for c in d["filas"] if c["cod"] == cod)


def test_factura_vieja_es_rojo():
    d = _armar([_cli("AAA", fac=5000, edad_max=160)])
    assert _uno(d, "AAA")["color"] == "rojo"


def test_factura_de_110_dias_es_amarillo():
    d = _armar([_cli("AAA", fac=5000, edad_max=110)])
    assert _uno(d, "AAA")["color"] == "amar"


def test_rebote_reciente_es_amarillo_y_viejo_rojo():
    d = _armar([_cli("NUE", fac=5000, edad_max=10, reb=1000, reb_dias=3),
                _cli("VIE", fac=5000, edad_max=10, reb=1000, reb_dias=45)])
    assert _uno(d, "NUE")["color"] == "amar"
    assert _uno(d, "VIE")["color"] == "rojo"


def test_sin_compras_con_facturas_es_rojo_pero_solo_cheques_no():
    d = _armar([_cli("FAC", fac=5000, edad_max=20, venta90=0),
                _cli("CHQ", ch=5000, venta90=0)])
    assert _uno(d, "FAC")["color"] == "rojo"
    assert _uno(d, "CHQ")["color"] == "verde"


def test_debe_mas_que_su_plazo_es_amarillo():
    # 9.000 en 90 días = 100/día; debe 20.000 = 200 días; paga a 100 → 2 veces.
    d = _armar([_cli("AAA", fac=20000, edad_max=30, pagado=50000, dias_plata=100)])
    c = _uno(d, "AAA")
    assert round(c["veces"], 2) == 2.0 and c["color"] == "amar"


def test_contra_si_mismo_detecta_que_empeoro():
    pm = [dict(cod="AAA", mes=MESES[0], plata=5000, dias=60),
          dict(cod="AAA", mes=MESES[1], plata=5000, dias=60),
          dict(cod="AAA", mes=MESES[2], plata=5000, dias=90)]
    d = _armar([_cli("AAA", fac=6000, edad_max=30)], pm)
    c = _uno(d, "AAA")
    assert (c["antes"], c["ultimo"], c["cambio"]) == (60, 90, 30)
    assert c["color"] == "amar"
    assert d["casa_meses"][2]["dias"] == 90


def test_saldo_chico_no_se_lista_pero_suma_a_la_cartera():
    d = _armar([_cli("CHI", fac=500, edad_max=10), _cli("GRA", fac=5000, edad_max=10)])
    assert [c["cod"] for c in d["filas"]] == ["GRA"]
    assert d["cartera"]["facturas"] == 5500


def test_cupo_no_pinta_pero_se_lista():
    d = _armar([_cli("AAA", fac=5000, edad_max=10, cupo=1000)])
    assert _uno(d, "AAA")["color"] == "verde"
    assert [c["cod"] for c in d["cupos"]] == ["AAA"]


def test_puntaje_ordena_por_antiguedad():
    d = _armar([_cli("NUE", fac=5000, edad_max=10), _cli("MED", fac=5000, edad_max=50),
                _cli("VIE", fac=5000, edad_max=90)])
    assert _uno(d, "VIE")["puntaje"] > _uno(d, "MED")["puntaje"] > _uno(d, "NUE")["puntaje"]


def test_la_pantalla_tiene_permiso_propio():
    import inspect

    from modules.analisis import views
    src = inspect.getsource(views.cobranza)
    assert 'requiere_permiso("analisis.cobranza")' in src
    assert all(m["url"] != "/analisis/cobranza" for m in views.MENU)
    from pathlib import Path
    base = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text()
    assert 'href="/analisis/cobranza"' in base
    assert "tiene_permiso('analisis.cobranza')" in base
    from config.roles import ROLES
    permisos = dict(ROLES)
    assert "analisis.cobranza" in permisos["INT"]
    assert "analisis.cobranza" not in permisos["Gerente"]


def test_puntos_del_grafico_solo_con_saldo_y_con_motivo():
    d = _armar([_cli("AAA", fac=5000, edad_max=160),
                _cli("CER", fac=0, edad_max=None)])
    ks = [p["k"] for p in d["puntos"]]
    assert "AAA" in ks and "CER" not in ks
    p = next(p for p in d["puntos"] if p["k"] == "AAA")
    assert p["c"] == "rojo" and p["m"][0].startswith("Factura impaga")


def test_rompe_varias_reglas_muestra_todas_rojo_primero():
    d = _armar([_cli("DOS", fac=5000, edad_max=160, reb=1000, reb_dias=3)])
    c = _uno(d, "DOS")
    assert c["color"] == "rojo"
    assert c["motivos"] == ["Factura impaga de 160 días",
                            "Cheque devuelto de 1.000 sin reemplazar hace 3 días"]


def test_devuelto_viejo_no_se_repite_como_reciente():
    d = _armar([_cli("VIE", fac=5000, edad_max=10, reb=1000, reb_dias=45)])
    assert _uno(d, "VIE")["motivos"] == [
        "Cheque devuelto de 1.000 sin reemplazar hace 45 días"]


def test_devuelto_de_mas_de_7_dias_ya_es_rojo():
    d = _armar([_cli("OCH", fac=5000, edad_max=10, reb=1000, reb_dias=8)])
    assert _uno(d, "OCH")["color"] == "rojo"


def test_resumen_por_vendedor():
    d = _armar([_cli("AAA", fac=5000, edad_max=160, vend="PPR"),
                _cli("BBB", fac=5000, edad_max=10, vend="EDG")])
    assert d["resumen_vend"]["PPR"]["rojo"]["n"] == 1
    assert d["resumen_vend"]["EDG"]["rojo"]["n"] == 0
    assert d["resumen_vend"]["PPR"]["rojo"]["pct"] == 100


def test_evolucion_color_por_mes():
    from datetime import date
    meses = [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]
    evol = [dict(cod="AAA", mes=meses[0], saldo=1000, edad=90),
            dict(cod="AAA", mes=meses[1], saldo=2000, edad=120),
            dict(cod="AAA", mes=meses[2], saldo=3000, edad=160),
            dict(cod="ZZZ", mes=meses[0], saldo=10, edad=1)]
    compras = [dict(cod="AAA", mes=date(2026, 5, 1), compro=500)]
    pm = [dict(cod="AAA", mes=meses[1], plata=800, dias=95)]
    e = cob.evolucion({"AAA"}, evol, compras, pm, meses, {"AAA": "amar"})
    assert "ZZZ" not in e
    a = e["AAA"]
    assert a["s"] == [1000, 2000, 3000]
    # jun: 90 días y compró en mayo -> verde; jul: 120 -> amarillo;
    # ago: el mes en curso toma el color de hoy.
    assert a["c"] == ["verde", "amar", "amar"]
    assert a["pa"] == [0, 800, 0] and a["di"] == [None, 95, None]


def test_evolucion_debe_y_no_compra_es_rojo():
    from datetime import date
    meses = [date(2026, 8, 1), date(2026, 9, 1)]
    e = cob.evolucion({"B"}, [dict(cod="B", mes=meses[0], saldo=900, edad=20)],
                      [], [], meses, {})
    assert e["B"]["c"][0] == "rojo"


def test_evolucion_sin_saldo_no_tiene_factura_vieja():
    from datetime import date
    meses = [date(2026, 9, 1), date(2026, 10, 1)]
    e = cob.evolucion({"DCA"}, [dict(cod="DCA", mes=meses[0], saldo=-50, edad=163)],
                      [], [], meses, {})
    assert e["DCA"]["e"][0] is None
