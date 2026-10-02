"""Análisis de cobranza (/analisis/cobranza, 30/09/2026)."""
from datetime import date

from modules.analisis import cobranza as cob

MESES = [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
HOY = date(2026, 9, 29)


def _cli(cod, **kw):
    base = dict(cod=cod, nombre=cod, vend="Intela", cupo=0, stop="", fac=0, ch=0,
                reb=0, n_reb=0, n_reb_hist=0, reb_dias=None, edad_max=None,
                venta90=9000, pagado=None, dias_plata=None)
    base.update(kw)
    return base


def _armar(clientes, por_mes=()):
    return cob.armar(list(clientes), list(por_mes), MESES, HOY)


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
    # Tamara 02/10: los meses en el motivo, iguales a los del panel de abajo.
    assert "En sep pagó a 90 días; en jul–ago, a 60" in c["motivos"]
    assert d["casa_meses"][2]["dias"] == 90


def test_saldo_chico_no_se_lista_pero_suma_a_la_cartera():
    d = _armar([_cli("CHI", fac=500, edad_max=10), _cli("GRA", fac=5000, edad_max=10)])
    assert [c["cod"] for c in d["filas"]] == ["GRA"]
    assert d["cartera"]["facturas"] == 5500


def test_cupo_no_pinta():
    d = _armar([_cli("AAA", fac=5000, edad_max=10, cupo=1000)])
    assert _uno(d, "AAA")["color"] == "verde"


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


def test_flecha():
    assert cob.flecha(100, 160, 100, 190) == "mejor"     # pagó lo viejo
    assert cob.flecha(81, 200, 100, 170) == "mejor"      # bajó la deuda 19% (GUG, KRH)
    assert cob.flecha(100, 200, 100, 170) == "peor"      # no pagó nada y envejeció
    assert cob.flecha(111, 225, 100, 229) == "peor"      # subió la deuda (MWI)
    assert cob.flecha(100, 180, 100, 175) == "igual"
    assert cob.flecha(0, None, 100, 175) == "mejor"      # pagó todo
    assert cob.flecha(100, 180, 0, None) is None


def test_cae_compra_20_por_ciento():
    assert cob.cae_compra(7900, 10000)
    assert not cob.cae_compra(8100, 10000)
    assert not cob.cae_compra(0, 2000)                   # muy poco antes


def test_marcar_pone_asterisco_y_flecha_solo_a_rojos():
    d = _armar([_cli("ROJ", fac=5000, edad_max=200), _cli("VER", fac=5000, edad_max=10)])
    trend = {"ROJ": {"ult": 1000, "ant": 9000}, "VER": {"ult": 1000, "ant": 9000}}
    ahora = {"ROJ": {"saldo": 5000, "edad": 200}, "VER": {"saldo": 5000, "edad": 10}}
    antes = {"ROJ": {"saldo": 5000, "edad": 170}, "VER": {"saldo": 5000, "edad": 40}}
    cob.marcar(d, trend, ahora, antes)
    assert _uno(d, "ROJ")["cae"] and _uno(d, "VER")["cae"]
    assert _uno(d, "ROJ")["flecha"] == "peor"
    assert _uno(d, "VER")["flecha"] is None
    p = next(p for p in d["puntos"] if p["k"] == "ROJ")
    assert p["cae"] == 89 and p["fl"] == "peor"


def test_evolucion_pagos_salen_de_los_cheques():
    from datetime import date
    meses = [date(2026, 7, 1), date(2026, 8, 1)]
    e = cob.evolucion({"A"}, [dict(cod="A", mes=meses[0], saldo=100, edad=10)], [],
                      [dict(cod="A", mes=meses[1], plata=800, dias=90)], meses, {},
                      pagos=[dict(cod="A", mes=meses[0], plata=1500)])
    assert e["A"]["pa"] == [1500, 0] and e["A"]["di"] == [None, 90]


def test_incobrable_deuda_de_mas_de_un_anio_y_no_compra():
    d = _armar([_cli("VIE", fac=5000, edad_max=400, venta90=0),
                _cli("COM", fac=5000, edad_max=400, venta90=9000),
                _cli("DEV", reb=1700, reb_dias=531, venta90=0)])
    assert _uno(d, "VIE")["incobrable"] and _uno(d, "DEV")["incobrable"]
    assert not _uno(d, "COM")["incobrable"]          # sigue comprando
    p = next(p for p in d["puntos"] if p["k"] == "VIE")
    assert p["i"]


def test_contra_si_mismo_con_un_solo_mes_antes():
    pm = [dict(cod="AAA", mes=MESES[1], plata=5000, dias=199),
          dict(cod="AAA", mes=MESES[2], plata=5000, dias=246)]
    c = _uno(_armar([_cli("AAA", fac=6000, edad_max=30)], pm), "AAA")
    assert "En sep pagó a 246 días; en ago, a 199" in c["motivos"]


def test_datos_usa_los_mismos_meses_que_el_panel(monkeypatch):
    """El semáforo y el panel de evolución leen el plazo de los mismos meses."""
    vistos = []

    def fake(sql, params=None):
        if sql is cob._SQL_MESES:
            vistos.append(params["desde"])
        return []
    monkeypatch.setattr(cob.db, "fetch_all", fake)
    cob.datos(date(2026, 10, 2))
    assert vistos == [date(2026, 7, 1)]


# ── El puntito del semáforo en otras pantallas (Tamara 02/10) ───────────────

def _fake_fetch(llamadas):
    def fake(sql, params=None):
        llamadas.append(sql)
        if sql is cob._SQL_CLIENTES:
            return [_cli("ROJ", fac=5000, edad_max=200), _cli("VIE", fac=5000, edad_max=400, venta90=0),
                    _cli(" ver ", fac=5000, edad_max=10)]
        return []
    return fake


def test_colores_da_el_color_de_cada_cliente_y_guarda_unos_minutos(monkeypatch):
    llamadas = []
    monkeypatch.setattr(cob.db, "fetch_all", _fake_fetch(llamadas))
    monkeypatch.setattr(cob, "_colores", {"t": 0.0, "d": None})
    c = cob.colores()
    assert c["ROJ"]["color"] == "rojo" and c["VIE"]["color"] == "inc"
    assert c["VER"]["color"] == "verde"          # el código se normaliza
    n = len(llamadas)
    cob.colores()                                 # dentro del TTL: no consulta
    assert len(llamadas) == n
    cob.colores(HOY)                              # con fecha: calcula y no guarda
    assert len(llamadas) > n


def test_de_cliente_listo_para_el_template(monkeypatch):
    monkeypatch.setattr(cob, "colores", lambda: {"ROJ": {"color": "rojo", "motivos": ["x"], "puntaje": 90}})
    s = cob.de_cliente(" roj ")
    assert s["nombre"] == "Rojo" and s["hex"] == "#b3362a" and s["motivos"] == ["x"]
    assert cob.de_cliente("NOP") is None
    assert cob.de_cliente(None) is None


def test_de_cliente_nunca_rompe_la_pantalla(monkeypatch):
    def boom():
        raise RuntimeError("sin base")
    monkeypatch.setattr(cob, "colores", boom)
    assert cob.de_cliente("ROJ") is None


def test_al_vendedor_el_incobrable_se_le_muestra_rojo(monkeypatch):
    monkeypatch.setattr(cob, "colores", lambda: {"VIE": {"color": "inc", "motivos": [], "puntaje": 99}})
    assert cob.de_cliente("VIE")["nombre"] == "Incobrable"
    v = cob.de_cliente("VIE", incobrable=False)
    assert (v["color"], v["nombre"], v["hex"]) == ("rojo", "Rojo", "#b3362a")
