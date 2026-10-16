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

    def fetch_one(self, sql, params=None, conn=None):
        if self.falla:
            raise RuntimeError("no existe la tabla")
        return {"usuario": "irene", "cuando": "16/10 17:05"}

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
        assert borrador.leer() == {"FE96CAF_ABI": (7.0, 20.0, 0.0, 0.0)}
        assert borrador.quien() == ("irene", "16/10 17:05")
    with patch.object(borrador, "db", _DB(falla=True)):
        assert borrador.leer() == {}
        assert borrador.quien() == ("", "")


def test_guardar_reemplaza_solo_los_colores_de_la_pantalla():
    d = _DB()
    with patch.object(borrador, "db", d):
        n = borrador.guardar("tamara", ["A_TUB", "B_ABI", "C_TUB"],
                             {"A_TUB": [6, 0], "B_ABI": ["0", "15,5", 0, "40"], "C_TUB": [0, 0], "X": [9, 9]})
        assert borrador.guardar("tamara", [], {}) == 0
    assert n == 2
    (borra, p0), *ins = d.hechos
    # Es de la oficina: borra lo de esos colores lo haya guardado quien sea.
    assert borra.startswith("DELETE") and "usuario" not in borra and p0 == (["A_TUB", "B_ABI", "C_TUB"],)
    assert [p for _, p in ins] == [("tamara", "A_TUB", 6.0, 0.0, 0.0, 0.0),
                                   ("tamara", "B_ABI", 0.0, 15.5, 0.0, 40.0)]


def test_borrar_saca_lo_mandado_y_no_rompe_sin_tabla():
    d = _DB()
    with patch.object(borrador, "db", d):
        borrador.borrar(["A_TUB"])
        borrador.borrar([])
    assert d.hechos == [("DELETE FROM scintela.pedidos_producir_borrador WHERE clave = ANY(%s)",
                         (["A_TUB"],))]
    with patch.object(borrador, "db", _DB(falla=True)):
        borrador.borrar(["A_TUB"])


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
         patch.object(views.borrador, "leer", return_value={clave: (7.0, 20.0, 0.0, 0.0)}), \
         patch.object(views.borrador, "quien", return_value=("irene", "16/10 17:05")):
        html = c.get("/pedidos?corte=tela&cat=Fleece").get_data(as_text=True)
    assert 'value="7"' in html and 'value="20"' in html and "var guardado = true" in html
    assert "Guardado por irene" in html and "16/10 17:05" in html

    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.formulas_memos, "estados", return_value={}), \
         patch.object(views.service, "por_pedido", return_value=([], False)), \
         patch.object(views.formulas_fabricar, "envios", return_value=([], True)), \
         patch.object(views, "_punto_reposicion", return_value={}), \
         patch.object(views.formulas_fabricar, "mandar", return_value=(True, "enviado", "AF-x")), \
         patch.object(views.borrador, "borrar") as b:
        c.post("/pedidos/mandar-a-hacer", data={"cat": "Fleece", f"c_{clave}": "7"})
    b.assert_called_once_with([clave])


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


# ── «+ Agregar color»: colores de la tela que no están en los pedidos ─────

def test_el_prefijo_y_el_renglon_nuevo_salen_de_otro_color_de_la_tela():
    base = {"tela": "Pique Especial", "categoria": "Pique", "codigo": "PEAZN", "color": "AZN", "u": "roll"}
    assert views._prefijo(base) == "PE" and views._prefijo({"codigo": "X", "color": "AZN"}) == ""
    nombres = {"CAR": "CARDENILLO"}
    f = views._renglon_nuevo("PECAR_TUB", "Pique Especial", [base], nombres)
    assert (f["codigo"], f["color"], f["acabado"], f["categoria"], f["agregado"]) == \
        ("PECAR", "CAR", "TUB", "Pique", True)
    assert views._renglon_nuevo("PEZZZ_TUB", "Pique Especial", [base], nombres) is None
    assert views._renglon_nuevo("PECAR_XX", "Pique Especial", [base], nombres) is None
    assert views._renglon_nuevo("QQCAR_TUB", "Pique Especial", [base], nombres) is None
    assert views._renglon_nuevo("PECAR_TUB", "Otra", [base], nombres) is None


def test_los_agregados_guardados_vuelven_a_su_tela():
    telas = [{"tela": "Pique Especial", "filas": [{"clave": "PEAZN_TUB", "codigo": "PEAZN", "color": "AZN"}]}]
    views._colores_agregados(telas, {"PECAR_TUB": (8, 0, 400, 300), "PEAZN_TUB": (1, 0, 0, 0),
                                     "FL102GRI_ABI": (6, 0, 0, 0), "PEMALO": (1, 0, 0, 0)})
    assert telas[0]["prefijo"] == "PE"
    assert telas[0]["agregados"] == [{"clave": "PECAR_TUB", "color": "CAR", "acabado": "TUB",
                                      "vals": (8, 0, 400, 300)}]
    sin = [{"tela": "X", "filas": [{"clave": "A", "codigo": "", "color": "A"}]}]
    views._colores_agregados(sin, {})
    assert sin[0]["agregados"] == [] and sin[0]["prefijo"] == ""


def test_productos_existentes_pregunta_a_asinfo():
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([{"codigo": "PECAR"}], True)) as m:
        assert service.productos_existentes(["pecar", "PE'X"]) == {"PECAR"}
    assert "'PECAR'" in m.call_args[0][1] and "'" + "PEX" + "'" in m.call_args[0][1]
    assert service.productos_existentes([]) == set()
    with patch.object(service.metabase_client, "fetch_dataset_estado", return_value=([], False)):
        assert service.productos_existentes(["PECAR"]) is None


def test_un_color_agregado_se_manda_si_existe(app, fake_db):
    c = _login(app, fake_db)
    mandado = {}

    def fake_mandar(lineas, usuario):
        mandado["lineas"] = lineas
        return True, "enviado", "AF-x"

    comunes = dict(
        estados=patch.object(views.formulas_memos, "estados", return_value={}),
        por_pedido=patch.object(views.service, "por_pedido", return_value=([], False)),
        envios=patch.object(views.formulas_fabricar, "envios", return_value=([], True)),
        punto=patch.object(views, "_punto_reposicion", return_value={}),
        borrar=patch.object(views.borrador, "borrar"),
        nombres=patch.object(views.service, "nombres_color", return_value={"CAR": "CARDENILLO", "CAF": "CAFE"}),
        mandar=patch.object(views.formulas_fabricar, "mandar", side_effect=fake_mandar),
    )
    datos = {"cat": "Fleece", "c_FE96CAR_TUB": "8", "u_FE96CAR_TUB": "400",
             "n_FE96CAR_TUB": "Fleece 96 Perchado", "c_FE96ZZZ_TUB": "3", "n_FE96ZZZ_TUB": "Fleece 96 Perchado"}
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.service, "productos_existentes", return_value={"FE96CAR"}), \
         comunes["estados"], comunes["por_pedido"], comunes["envios"], comunes["punto"], \
         comunes["borrar"], comunes["nombres"], comunes["mandar"]:
        r = c.post("/pedidos/mandar-a-hacer", data=datos, follow_redirects=True)
    (ln,) = mandado["lineas"]
    assert (ln["codigo"], ln["color"], ln["acabado"], ln["cantidad"], ln["cuellos"], ln["pedidos"]) == \
        ("FE96CAR", "CAR", "TUB", 8.0, 400.0, [])
    assert "FE96ZZZ" in r.get_data(as_text=True)   # no existe: se avisa que no se mandó

    mandado.clear()
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.service, "productos_existentes", return_value=None), \
         comunes["estados"], comunes["por_pedido"], comunes["envios"], comunes["punto"], \
         comunes["borrar"], comunes["nombres"], comunes["mandar"]:
        r = c.post("/pedidos/mandar-a-hacer", data=datos, follow_redirects=True)
    assert "No pude confirmar en Asinfo" in r.get_data(as_text=True)
