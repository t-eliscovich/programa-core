"""💾 Guardar en Producir (Tamara 16/10/2026): "hice la mitad y quiero seguir
más tarde… que se mantenga hasta que mande"."""
from contextlib import contextmanager
from unittest.mock import patch

from modules.pedidos import borrador, service, views
from tests.test_pedidos_mandar_a_hacer_2026_10_15 import _fake_asinfo, _login, _ped


class _DB:
    def __init__(self, filas=None, falla=False):
        self.filas, self.falla, self.hechos = filas or [], falla, []

    def fetch_all(self, sql, params=None, conn=None):
        if self.falla:
            raise RuntimeError("no existe la tabla")
        return self.filas

    def execute(self, sql, params=None, conn=None):
        if self.falla:
            raise RuntimeError("no existe la tabla")
        self.hechos.append((" ".join(sql.split()), params))
        return 1

    @contextmanager
    def tx(self):
        yield object()


def test_leer_devuelve_lo_guardado_y_sin_tabla_queda_vacio():
    d = _DB([{"clave": "FE96CAF_ABI", "rollos": 7, "rib_kg": 20}])
    with patch.object(borrador, "db", d):
        assert borrador.leer("tamara") == {"FE96CAF_ABI": (7.0, 20.0, 0.0, 0.0)}
        assert borrador.leer("") == {}
    with patch.object(borrador, "db", _DB(falla=True)):
        assert borrador.leer("tamara") == {}


def test_guardar_reemplaza_solo_los_colores_de_la_pantalla():
    d = _DB()
    with patch.object(borrador, "db", d):
        n = borrador.guardar("tamara", ["A_TUB", "B_ABI", "C_TUB"],
                             {"A_TUB": [6, 0], "B_ABI": ["0", "15,5", 0, "40"], "C_TUB": [0, 0], "X": [9, 9]})
        assert borrador.guardar("tamara", [], {}) == 0
    assert n == 2
    (borra, p0), *ins = d.hechos
    assert borra.startswith("DELETE") and p0 == ("tamara", ["A_TUB", "B_ABI", "C_TUB"])
    assert [p for _, p in ins] == [("tamara", "A_TUB", 6.0, 0.0, 0.0, 0.0),
                                   ("tamara", "B_ABI", 0.0, 15.5, 0.0, 40.0)]


def test_borrar_saca_lo_mandado_y_no_rompe_sin_tabla():
    d = _DB()
    with patch.object(borrador, "db", d):
        borrador.borrar("tamara", ["A_TUB"])
        borrador.borrar("tamara", [])
    assert d.hechos == [("DELETE FROM scintela.pedidos_producir_borrador WHERE usuario = %s "
                         "AND clave = ANY(%s)", ("tamara", ["A_TUB"]))]
    with patch.object(borrador, "db", _DB(falla=True)):
        borrador.borrar("tamara", ["A_TUB"])


def test_el_boton_guardar_guarda_por_usuario(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(views.borrador, "guardar", return_value=1) as g:
        r = c.post("/pedidos/producir/borrador",
                   data={"claves": '["A_TUB"]', "valores": '{"A_TUB": [6, 0]}'})
    assert r.get_json() == {"ok": True, "n": 1}
    g.assert_called_once_with("test", ["A_TUB"], {"A_TUB": [6, 0]})
    assert c.post("/pedidos/producir/borrador", data={"valores": "[1]"}).status_code == 400
    with patch.object(views.borrador, "guardar", side_effect=RuntimeError("x")):
        assert c.post("/pedidos/producir/borrador", data={}).status_code == 500


def test_lo_guardado_vuelve_a_los_cuadritos_y_al_mandar_sale(app, fake_db):
    c = _login(app, fake_db)
    clave = service.clave_renglon("FE96CAF", "")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.formulas_memos, "estados", return_value={}), \
         patch.object(views.service, "por_pedido", return_value=([], False)), \
         patch.object(views.formulas_fabricar, "envios", return_value=([], True)), \
         patch.object(views.formulas_fabricar, "disponible", return_value=True), \
         patch.object(views, "_punto_reposicion", return_value={}), \
         patch.object(views.borrador, "leer", return_value={clave: (7.0, 20.0, 0.0, 0.0)}):
        html = c.get("/pedidos?corte=tela&cat=Fleece").get_data(as_text=True)
    assert 'value="7"' in html and 'value="20"' in html and "var guardado = true" in html

    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.formulas_memos, "estados", return_value={}), \
         patch.object(views.service, "por_pedido", return_value=([], False)), \
         patch.object(views.formulas_fabricar, "envios", return_value=([], True)), \
         patch.object(views, "_punto_reposicion", return_value={}), \
         patch.object(views.formulas_fabricar, "mandar", return_value=(True, "enviado", "AF-x")), \
         patch.object(views.borrador, "borrar") as b:
        c.post("/pedidos/mandar-a-hacer", data={"cat": "Fleece", f"c_{clave}": "7"})
    b.assert_called_once_with("test", [clave])


# ── Pique: cuellos y puños (Tamara 16/10/2026) ───────────────────────────

def test_solo_pique_lleva_cuellos_y_punos():
    assert service.lleva_cuellos("Pique") and service.lleva_cuellos(" pique ")
    assert not service.lleva_cuellos("Fleece") and not service.lleva_cuellos(None)


def test_se_mandan_cuellos_y_punos(app, fake_db):
    c = _login(app, fake_db)
    clave = service.clave_renglon("FE96CAF", "")
    mandado = {}

    def fake_mandar(lineas, usuario):
        mandado["lineas"] = lineas
        return True, "enviado", "AF-x"

    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.formulas_memos, "estados", return_value={}), \
         patch.object(views.service, "por_pedido", return_value=([], False)), \
         patch.object(views.formulas_fabricar, "envios", return_value=([], True)), \
         patch.object(views, "_punto_reposicion", return_value={}), \
         patch.object(views.borrador, "borrar"), \
         patch.object(views.formulas_fabricar, "mandar", side_effect=fake_mandar):
        r = c.post("/pedidos/mandar-a-hacer",
                   data={"cat": "Fleece", f"u_{clave}": "120", f"p_{clave}": "80"}, follow_redirects=True)
    (ln,) = mandado["lineas"]
    assert (ln["cantidad"], ln["cuellos"], ln["punos"]) == (0.0, 120.0, 80.0)
    assert "120 cuellos" in r.get_data(as_text=True)
