"""Salidas de material que Asinfo no bajó del saldo (Tamara 2026-10-01)."""
from __future__ import annotations

from unittest.mock import patch

from modules.asinfo import salidas_sin_saldo as sss

FILAS = [
    {"doc": "SM-000112721", "bodega": 51, "orden": "OSM-000010962",
     "fecha": "2026-09-23 14:48", "lotes": 109, "sin_bajar": 108,
     "kg_sin_bajar": 2916, "repetidos": 0},
    {"doc": "SM-000112399", "bodega": 51, "orden": "OSM-000010926",
     "fecha": "2026-09-19 06:18", "lotes": 60, "sin_bajar": 59,
     "kg_sin_bajar": 1593, "repetidos": 59},
]


ING = [
    {"doc": "BOD-000002371/IM-0000591", "bodega": 51, "fecha": "2026-09-11 15:50",
     "lotes": 29, "kg_de_mas": 789.38},
]


CUADRA = [
    {"bodega": 51, "saldo": 1000, "movimientos": 1000 - 2300, "diferencia": 2300,
     "stock_de_mas": 0},
    {"bodega": 52, "saldo": 500, "movimientos": 500, "diferencia": 0, "stock_de_mas": 0},
]


def _fake(salidas=FILAS, ingresos=(), ok=True, cuadre=CUADRA):
    def f(db, sql, **k):
        if "AS diferencia" in sql:
            return list(cuadre), ok
        return (list(ingresos) if "operacion = 1" in sql else list(salidas)), ok
    return f


def test_detecta_y_suma():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=(FILAS, True)):
        r = sss.detectar(10)
    assert r["ok"] and len(r["salidas"]) == 2
    assert r["lotes_sin_bajar"] == 167 and r["kg_sin_bajar"] == 4509
    assert r["salidas"][0]["bodega"] == "Hilo"


def test_alerta_y_un_aviso_por_salida():
    avisos = []
    with patch("modules._lib.metabase_client.fetch_dataset_estado", side_effect=_fake()), \
         patch("modules.avisos.queries.avisar", side_effect=lambda **k: avisos.append(k) or True):
        h = sss.health(10)
    assert h["ok"] is False and h["alerts"][0]["category"] == "salidas_sin_bajar_saldo"
    assert [a["clave"] for a in avisos] == ["salida-sin-saldo:SM-000112721", "salida-sin-saldo:SM-000112399"]
    assert "108 de 109 lotes no bajaron" in avisos[0]["titulo"]
    assert "también están en otra salida" in avisos[1]["titulo"]


def test_sin_problemas_ok():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", side_effect=_fake([], [])):
        h = sss.health(10)
    assert h["ok"] is True and h["alerts"] == []


def test_asinfo_no_contesta_no_alarma():
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=([], False)):
        h = sss.health(10)
    assert h["ok"] is True and h["stats"]["sin_datos"] is True


def test_la_consulta_compara_el_saldo_contra_la_fecha_de_la_salida():
    q = sss._sql(10)
    assert "s.saldo - k.kx > 0.5" in q           # saldo contra movimientos
    assert "u.rn = 1" in q                        # se le anota a la última salida
    assert "WHERE b = 51" in q          # repetidos sólo en hilo
    assert "indicador_anulacion" in q


def test_health_all_lo_incluye():
    import inspect

    from modules.admin_dbase import health_audit_view as hav
    src = inspect.getsource(hav.health_all)
    assert "_sss.health_reciente()" in src and 'data29["ok"]' in src


def test_ingreso_sumado_dos_veces_alerta_y_avisa():
    avisos = []
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=_fake([], ING)), \
         patch("modules.avisos.queries.avisar", side_effect=lambda **k: avisos.append(k) or True):
        h = sss.health(10)
    assert h["ok"] is False
    assert [a["category"] for a in h["alerts"]] == ["ingresos_sumados_dos_veces"]
    assert h["stats"]["kg_ingreso_de_mas"] == 789.38
    assert [a["clave"] for a in avisos] == ["ingreso-doble:BOD-000002371/IM-0000591"]
    assert "29 lotes" in avisos[0]["titulo"] and "Hilo" in avisos[0]["titulo"]


def test_las_dos_alertas_juntas():
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=_fake(FILAS, ING)), \
         patch("modules.avisos.queries.avisar", return_value=True):
        h = sss.health(10)
    assert {a["category"] for a in h["alerts"]} == {
        "salidas_sin_bajar_saldo", "ingresos_sumados_dos_veces"}


def test_detectar_ingresos_falla_sin_levantar():
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=RuntimeError("boom")):
        r = sss.detectar_ingresos(30)
    assert r["ok"] is False and r["ingresos"] == []
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([{"doc": "X", "bodega": "no"}], True)):
        assert sss.detectar_ingresos(30)["ingresos"] == []


def test_la_consulta_de_ingresos_compara_saldo_contra_movimientos():
    q = sss._sql_ingresos(30)
    assert "k.nsal = 0" in q and "s.saldo - k.kx > 0.5" in q
    assert "ip.operacion = 1" in q and "indicador_anulacion" in q


def test_si_solo_contesta_una_consulta_igual_alerta():
    def f(db, sql, **k):
        if "operacion = 1" in sql:
            return list(ING), True
        if "AS diferencia" in sql:
            return list(CUADRA), True
        return [], False
    with patch("modules._lib.metabase_client.fetch_dataset_estado", side_effect=f), \
         patch("modules.avisos.queries.avisar", side_effect=RuntimeError("sin base")):
        h = sss.health(10)
    assert [a["category"] for a in h["alerts"]] == ["ingresos_sumados_dos_veces"]
    assert h["stats"]["salidas"] == []


DESCUADRE = [
    {"bodega": 51, "saldo": 2202034, "movimientos": 2144885, "diferencia": 57149,
     "stock_de_mas": 51740},
    {"bodega": 52, "saldo": 254354, "movimientos": 206950, "diferencia": 47404,
     "stock_de_mas": 7308},
    {"bodega": 53, "saldo": 318328, "movimientos": 318298, "diferencia": 30,
     "stock_de_mas": 52},
]


def test_cuadre_marca_lo_que_pasa_de_la_base():
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=(DESCUADRE + [{"bodega": "x"}], True)):
        c = sss.cuadre()
    b = {x["id"]: x for x in c["bodegas"]}
    assert b[51]["descuadrada"] and b[51]["de_mas_nuevo"] == 51740
    assert b[52]["stock_de_mas"] == 7308 and b[52]["diferencia"] == 47404
    assert b[52]["descuadrada"] and b[53]["descuadrada"] is False


def test_cuadre_falla_sin_levantar():
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=RuntimeError("boom")):
        assert sss.cuadre()["ok"] is False
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], False)):
        assert sss.cuadre()["ok"] is False


def test_bodega_descuadrada_alerta_y_avisa_cada_mil_kg():
    avisos = []
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=_fake([], [], cuadre=DESCUADRE)), \
         patch("modules.avisos.queries.avisar", side_effect=lambda **k: avisos.append(k) or True):
        h = sss.health(10)
    assert [a["category"] for a in h["alerts"]] == ["bodega_descuadrada"]
    assert [a["clave"] for a in avisos] == ["stock-de-mas:51:51", "stock-de-mas:52:7"]
    assert "tela cruda tiene 7,308 kg de más" in avisos[1]["titulo"]
    assert len(h["stats"]["cuadre"]) == 3


def test_corre_solo_cada_media_hora(monkeypatch):
    llamadas = []
    monkeypatch.setattr(sss, "health", lambda **k: llamadas.append(k) or {
        "ok": False, "alerts": [{"category": "bodega_descuadrada"}]})
    from modules.asinfo import kardex_bodegas
    calentados = []
    monkeypatch.setattr(kardex_bodegas, "calentar", lambda: calentados.append(1) or True)
    monkeypatch.setattr(sss, "_auto_ultimo", None)
    monkeypatch.delenv("SALIDAS_SALDO_AUTO", raising=False)
    monkeypatch.setenv("SALIDAS_SALDO_SECS", "nada")
    r = sss.correr_si_toca()
    assert r["corrio"] and r["alertas"] == ["bodega_descuadrada"]
    assert sss.correr_si_toca()["corrio"] is False, "no repite antes de 30 min"
    assert llamadas == [{"avisar": True}]
    assert calentados == [1], "de paso deja listo el mes a mes del flujo"


def test_corre_solo_se_apaga_y_no_se_cae(monkeypatch):
    monkeypatch.setattr(sss, "_auto_ultimo", None)
    monkeypatch.setenv("SALIDAS_SALDO_AUTO", "0")
    assert sss.correr_si_toca()["corrio"] is False
    monkeypatch.setenv("SALIDAS_SALDO_AUTO", "1")
    monkeypatch.setenv("SALIDAS_SALDO_SECS", "600")

    def boom(**k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sss, "health", boom)
    assert sss.correr_si_toca()["corrio"] is False


def test_el_hilo_de_fondo_lo_llama():
    import inspect

    from modules._lib import autocarga_facturas as af
    assert "_sss.correr_si_toca()" in inspect.getsource(af._loop)



class _Avisos:
    """Campanita falsa: avisos abiertos por prefijo, y lo que se resolvió."""

    def __init__(self, abiertos):
        self.abiertos, self.resueltos, self.archivados = abiertos, [], []

    def abiertos_por_clave(self, prefijo):
        return [a for a in self.abiertos if a["clave"].startswith(prefijo)]

    def resolver(self, id_aviso, *, titulo, detalle=None):
        self.resueltos.append((id_aviso, titulo))
        return True

    def archivar(self, id_aviso, usuario="web", deshacer=False):
        self.archivados.append(id_aviso)
        return True


def _con_avisos(monkeypatch, abiertos):
    from modules.avisos import queries as aq
    fake = _Avisos(abiertos)
    for n in ("abiertos_por_clave", "resolver", "archivar"):
        monkeypatch.setattr(aq, n, getattr(fake, n))
    return fake


def test_avisa_cuando_asinfo_arregla_una_salida(monkeypatch):
    fake = _con_avisos(monkeypatch, [
        {"id_aviso": 1, "clave": "salida-sin-saldo:SM-000112721"},
        {"id_aviso": 2, "clave": "salida-sin-saldo:SM-000112399"},
    ])
    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([{"doc": "SM-000112399", "lotes": 60}], True)):
        hechas = sss.avisar_arreglos({"ok": True}, {"ok": False}, {"ok": False})
    assert hechas == ["salida-sin-saldo:SM-000112721"]
    assert fake.resueltos == [(1, "Asinfo arregló SM-000112721: los lotes ya bajaron del saldo")]


def test_avisa_cuando_arregla_un_ingreso(monkeypatch):
    fake = _con_avisos(monkeypatch, [
        {"id_aviso": 3, "clave": "ingreso-doble:BOD-000002371/IM-0000591"},
        {"id_aviso": 4, "clave": "ingreso-doble:BOD-000002368/IM-0000607"},
    ])
    queda = [{"doc": "BOD-000002368/IM-0000607", "bodega": 51, "fecha": "x",
              "lotes": 1, "kg_de_mas": 27.22}]
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=(queda, True)):
        hechas = sss.avisar_arreglos({"ok": False}, {"ok": True}, {"ok": False})
    assert hechas == ["ingreso-doble:BOD-000002371/IM-0000591"]
    assert "ya no lo cuenta dos veces" in fake.resueltos[0][1]


def test_avisa_cuando_baja_el_stock_de_mas(monkeypatch):
    fake = _con_avisos(monkeypatch, [
        {"id_aviso": 5, "clave": "stock-de-mas:51:51"},
        {"id_aviso": 6, "clave": "stock-de-mas:52:7"},
        {"id_aviso": 7, "clave": "stock-de-mas:53:3"},
        {"id_aviso": 8, "clave": "stock-de-mas:99:1"},
        {"id_aviso": 9, "clave": "stock-de-mas:roto"},
        {"id_aviso": 10, "clave": "cuadre:51:54"},
    ])
    cua = {"ok": True, "bodegas": [
        {"id": 51, "bodega": "Hilo", "de_mas_nuevo": 30500, "descuadrada": True},
        {"id": 52, "bodega": "Tela cruda", "de_mas_nuevo": 20, "descuadrada": False},
        {"id": 53, "bodega": "Terminado", "de_mas_nuevo": 3500, "descuadrada": True},
    ]}
    hechas = sss.avisar_arreglos({"ok": False}, {"ok": False}, cua)
    assert hechas == ["stock-de-mas:51:51", "stock-de-mas:52:7"]
    assert "bajó a 30,500 kg de más" in fake.resueltos[0][1]
    assert "ya no tiene kilos de más" in fake.resueltos[1][1]
    assert fake.archivados == [10], "los avisos viejos del cuadre se archivan sin festejar"


def test_avisar_arreglos_no_se_cae(monkeypatch):
    from modules.avisos import queries as aq

    def boom(prefijo):
        raise RuntimeError("sin base")
    monkeypatch.setattr(aq, "abiertos_por_clave", boom)
    assert sss.avisar_arreglos({"ok": True}, {"ok": True}, {"ok": True}) == []


def test_si_asinfo_no_contesta_no_resuelve_nada(monkeypatch):
    fake = _con_avisos(monkeypatch, [
        {"id_aviso": 1, "clave": "salida-sin-saldo:SM-1"},
        {"id_aviso": 2, "clave": "ingreso-doble:BOD-1/IM-1"},
    ])
    with patch("modules._lib.metabase_client.fetch_dataset_estado", return_value=([], False)):
        assert sss.avisar_arreglos({"ok": True}, {"ok": True}, {"ok": False}) == []
    assert fake.resueltos == []


def test_la_consulta_de_salidas_viejas_limpia_los_numeros():
    q = sss._sql_docs(["SM-000112721", "x'; DROP TABLE y--", ""])
    assert "'SM-000112721'" in q and "DROP" not in q
    assert "''" in sss._sql_docs([])


def test_falla_solo_si_el_saldo_sigue_positivo():
    q = sss._sql(10)
    assert "s.saldo > 0.5" in q
