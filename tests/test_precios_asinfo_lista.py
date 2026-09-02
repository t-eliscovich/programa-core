"""La lista de precios de Asinfo comparada con /precios, y el aviso de versión.

CONTEXTO (2026-09-02). Tamara: *"si cambian los precios en asinfo tenemos que
cambiarlos en programa core… tener metodo de importar cambios"*. Decisión:
revisar y confirmar (nada se pisa solo) + campanita cuando sale una versión.

Lo que se prueba es lo que puede COBRAR MAL:

1. Con la lista REAL de Asinfo (versión 554, fixture) contra la matriz de
   producción del 02/09, la única diferencia es CUELLOS/FUERTES (14,61 vs
   14,60). Si el mapa subcategoría→columna se desarma, esto lo muestra.
2. La comparación va en la escala del usuario (c/IVA, 2 dec): 7,94 y 7,9391
   son el MISMO 9,13 y no aparecen como diferencia.
3. Gana el precio MODAL, no el promedio ni el último; con poco apoyo se
   muestra pero no se sugiere aplicar.
4. Aplicar escribe el NETO de Asinfo (4 dec) por el mismo camino que la
   pantalla, y el precio NO viene del formulario.
5. Si Asinfo no contesta: nada se toca, y se devuelve el último valor bueno
   marcado como viejo (no un cero que parece un dato).
6. Versión nueva → campanita UNA vez; la primera corrida sólo aprende.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import db
from modules.precios import asinfo_lista as al
from modules.precios import queries

_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "asinfo_lista_554.txt")


def _filas_554():
    out = []
    with open(_FIX, encoding="utf-8") as f:
        for line in f:
            c, s, t, p, n = line.rstrip("\n").split("|")
            out.append({"version": 554, "desde": "2026-04-14T00:00:00Z",
                        "categoria": c, "subcategoria": s, "tono": t,
                        "precio": float(p), "n": int(n)})
    return out


_COLS = ["clase", "descripcio", "jersey", "rib", "pique", "cuellos", "toper",
         "falso", "lycra", "alemania", "kiana", "medical", "micro", "james"]
# scintela.precios en producción, 02/09/2026 (NETO, 4 decimales).
_MATRIZ_PROD = [dict(zip(_COLS, r, strict=True)) for r in [
    [1, "BLANCO", 7.9391, 8.53, 8.03, 10.81, 8.0609, 8.21, 10.39, 7.26, 7.5217, 10.4522, 7.72, 7.72],
    [2, "BAJOS", 8.5739, 9.17, 8.64, 11.45, 8.17, 8.31, 11.0609, 7.26, 7.5217, 10.4522, 7.72, 7.72],
    [3, "MEDIOS", 9.2, 9.84, 9.287, 12.08, 8.53, 8.6783, 11.0609, 7.26, 7.5217, 10.4522, 7.72, 7.72],
    [4, "JASPEADOS", 9.2783, 9.57, 9.3739, 11.85, 8.27, 9.05, 11.0609, 7.26, 7.5217, 10.4522, 7.72, 7.72],
    [5, "FUERTES", 9.82, 10.51, 9.913, 12.7043, 9.11, 9.25, 11.4174, 7.26, 7.5217, 10.4522, 7.72, 7.72],
]]
_PLANOS_PROD = [
    {"id": 1, "tela": "JERSEY 3,5", "precio": None, "ref_col": "jersey"},
    {"id": 3, "tela": "SCUBA", "precio": 9.7826, "ref_col": None},
    {"id": 4, "tela": "SUPLEX", "precio": 8.8783, "ref_col": None},
    {"id": 5, "tela": "BELTIS", "precio": 9.9913, "ref_col": None},
    {"id": 6, "tela": "NATY", "precio": 7.7217, "ref_col": None},
]


def _fila(cat, sub, tono, precio, n, version=554):
    return {"version": version, "desde": "2026-04-14T00:00:00Z", "categoria": cat,
            "subcategoria": sub, "tono": tono, "precio": precio, "n": n}


def _matriz(**jersey):
    filas = []
    for clase, desc in [(1, "BLANCO"), (2, "BAJOS"), (3, "MEDIOS"), (4, "JASPEADOS"), (5, "FUERTES")]:
        f = {col: None for col, _ in queries.TELAS}
        f.update(clase=clase, descripcio=desc, jersey=jersey.get(desc.lower()))
        filas.append(f)
    return filas


@pytest.fixture(autouse=True)
def _sin_cache():
    al.limpiar_cache()
    yield
    al.limpiar_cache()


# ---------------------------------------------------------------------------
# 1 — la lista real contra la matriz real
# ---------------------------------------------------------------------------
def test_la_554_contra_produccion_solo_difiere_cuellos_fuertes():
    r = al.comparar(_filas_554(), _MATRIZ_PROD, _PLANOS_PROD)
    assert (r["version"], r["desde"]) == (554, "2026-04-14")
    assert r["sin_dato"] == []
    assert r["iguales"] == 58  # 11 telas × 5 clases + 4 planas − 1
    (d,) = r["diferencias"]
    assert (d["tela"], d["clase_desc"]) == ("CUELLOS", "FUERTES")
    assert (d["pc_iva"], d["asinfo_iva"]) == (14.61, 14.60)
    assert d["asinfo_neto"] == 12.70 and d["aplicable"] is True


def test_todas_las_telas_activas_tienen_subcategorias_de_asinfo():
    """Una tela nueva en TELAS sin entrada en MAPA quedaría sin comparar."""
    for col, _ in queries.TELAS:
        assert al.MAPA.get(col), col


# ---------------------------------------------------------------------------
# 2 — la escala del usuario
# ---------------------------------------------------------------------------
def test_7_9391_y_7_94_son_el_mismo_precio_con_iva():
    r = al.comparar([_fila("Jersey", "Jersey 105", "BLN", 7.94, 5)],
                    _matriz(blanco=7.9391), [])
    assert r["diferencias"] == [] and r["iguales"] == 1


def test_un_centavo_con_iva_si_es_diferencia():
    r = al.comparar([_fila("Jersey", "Jersey 105", "BLN", 7.95, 5)],
                    _matriz(blanco=7.9391), [])
    (d,) = r["diferencias"]
    assert (d["pc_iva"], d["asinfo_iva"]) == (9.13, 9.14)


def test_celda_vacia_en_programa_es_diferencia():
    r = al.comparar([_fila("Jersey", "Jersey 105", "BLN", 7.94, 5)], _matriz(), [])
    (d,) = r["diferencias"]
    assert d["pc_iva"] is None and d["asinfo_iva"] == 9.13


# ---------------------------------------------------------------------------
# 3 — modal, no promedio; poco apoyo no se sugiere
# ---------------------------------------------------------------------------
def test_gana_el_precio_con_mas_productos_sumando_subcategorias():
    filas = [
        _fila("Jersey", "Jersey 105", "BLN", 7.94, 3),
        _fila("Jersey", "Jersey 1.2", "BLN", 7.94, 3),
        _fila("Jersey", "Jersey 3", "BLN", 9.99, 5),
    ]
    r = al.comparar(filas, _matriz(blanco=1.0), [])
    (d,) = r["diferencias"]
    assert d["asinfo_neto"] == 7.94 and (d["n"], d["total"]) == (6, 11)


def test_empate_gana_el_mas_alto():
    filas = [_fila("Jersey", "Jersey 105", "BLN", 7.94, 3),
             _fila("Jersey", "Jersey 3", "BLN", 7.95, 3)]
    (d,) = al.comparar(filas, _matriz(blanco=1.0), [])["diferencias"]
    assert d["asinfo_neto"] == 7.95


def test_con_poco_apoyo_se_muestra_pero_no_se_sugiere():
    filas = [_fila("Jersey", "Jersey 105", "BLN", 7.94, 2),
             _fila("Jersey", "Jersey 3", "BLN", 7.90, 1),
             _fila("Jersey", "Jersey 1.2", "BLN", 7.80, 1),
             _fila("Jersey", "Jersey 95", "BLN", 7.70, 1)]
    (d,) = al.comparar(filas, _matriz(blanco=1.0), [])["diferencias"]
    assert d["pureza"] == 0.4 and d["aplicable"] is False


def test_subcategoria_que_no_esta_en_el_mapa_no_cuenta():
    """Jersey Boca tiene escalera propia, más barata: no puede pisar JERSEY."""
    filas = [_fila("Jersey", "Jersey Boca", "BLN", 6.35, 500),
             _fila("Jersey", "Jersey 105", "BLN", 7.94, 5)]
    (d,) = al.comparar(filas, _matriz(blanco=1.0), [])["diferencias"]
    assert d["asinfo_neto"] == 7.94


def test_tono_especial_no_entra_en_ninguna_clase():
    r = al.comparar([_fila("Toper", "Toper", "ESP", 12.39, 44)], _matriz(), [])
    assert r["diferencias"] == [] and "TOPER/BLANCO" in r["sin_dato"]


def test_plana_junta_todos_los_tonos_y_salta_las_ref_col():
    filas = [_fila("Poliester", "Beltis", "BJS", 9.99, 34),
             _fila("Poliester", "Beltis", "FRT", 9.99, 187),
             _fila("Poliester", "Beltis", "BLN", 9.77, 1)]
    planos = [{"id": 1, "tela": "JERSEY 3", "precio": None, "ref_col": "jersey"},
              {"id": 5, "tela": "BELTIS", "precio": 11.49, "ref_col": None}]
    (d,) = al.comparar(filas, _matriz(), planos)["diferencias"]
    assert d["tipo"] == "plano" and d["id"] == 5
    assert d["asinfo_neto"] == 9.99 and (d["n"], d["total"]) == (221, 222)


# ---------------------------------------------------------------------------
# 4 — aplicar
# ---------------------------------------------------------------------------
def test_aplicar_escribe_el_neto_de_asinfo_por_el_camino_de_la_pantalla(monkeypatch):
    monkeypatch.setattr(al, "traer_asinfo", lambda: (_filas_554(), True))
    monkeypatch.setattr(queries, "matriz", lambda: _MATRIZ_PROD)
    monkeypatch.setattr(queries, "precio_plano", lambda: _PLANOS_PROD)
    escrito = []
    monkeypatch.setattr(queries, "actualizar_precio",
                        lambda clase, col, valor, usuario: escrito.append((clase, col, valor, usuario)))
    rep = al.aplicar(["m_5_cuellos", "m_1_jersey"], "tamara")
    assert rep["aplicados"] == 1
    assert rep["no_encontrados"] == ["m_1_jersey"]  # no difería: no se toca
    assert escrito == [(5, "cuellos", 12.7, "tamara")]


def test_aplicar_una_plana(monkeypatch):
    monkeypatch.setattr(al, "traer_asinfo", lambda: (
        [_fila("Poliester", "Beltis", "FRT", 9.99, 187)], True))
    monkeypatch.setattr(queries, "matriz", lambda: _matriz())
    monkeypatch.setattr(queries, "precio_plano", lambda: [
        {"id": 5, "tela": "BELTIS", "precio": 11.49, "ref_col": None}])
    escrito = []
    monkeypatch.setattr(queries, "actualizar_precio_plano",
                        lambda id_, valor, usuario: escrito.append((id_, valor)))
    assert al.aplicar(["p_5"], "u")["aplicados"] == 1
    assert escrito == [(5, 9.99)]


# ---------------------------------------------------------------------------
# 5 — Asinfo no contesta
# ---------------------------------------------------------------------------
def test_si_asinfo_no_contesta_no_es_ok_y_no_hay_diferencias(monkeypatch):
    monkeypatch.setattr(al, "traer_asinfo", lambda: ([], False))
    r = al.diferencias()
    assert r["ok"] is False and r["diferencias"] == []


def test_si_asinfo_deja_de_contestar_vuelve_el_ultimo_valor_bueno(monkeypatch):
    monkeypatch.setattr(queries, "matriz", lambda: _matriz(blanco=1.0))
    monkeypatch.setattr(queries, "precio_plano", lambda: [])
    monkeypatch.setattr(al, "traer_asinfo",
                        lambda: ([_fila("Jersey", "Jersey 105", "BLN", 7.94, 5)], True))
    assert al.diferencias()["ok"] is True
    monkeypatch.setattr(al, "traer_asinfo", lambda: ([], False))
    r = al.diferencias(forzar=True)
    assert r["ok"] is True and r["viejo"] is True and len(r["diferencias"]) == 1


def test_el_cache_guarda_solo_el_exito(monkeypatch):
    llamadas = []
    monkeypatch.setattr(al, "traer_asinfo", lambda: (llamadas.append(1), ([], False))[1])
    al.diferencias()
    al.diferencias()
    assert len(llamadas) == 2  # un fracaso no se cachea


def test_health_con_asinfo_caido_es_ok_con_sin_datos(monkeypatch):
    monkeypatch.setattr(al, "traer_asinfo", lambda: ([], False))
    h = al.health()
    assert h["ok"] is True and h["stats"]["sin_datos"] is True


def test_health_alerta_con_una_diferencia(monkeypatch):
    monkeypatch.setattr(al, "traer_asinfo", lambda: (_filas_554(), True))
    monkeypatch.setattr(queries, "matriz", lambda: _MATRIZ_PROD)
    monkeypatch.setattr(queries, "precio_plano", lambda: _PLANOS_PROD)
    h = al.health()
    assert h["ok"] is False
    assert h["alerts"][0]["category"] == "precios_distintos_asinfo"
    assert "CUELLOS/FUERTES 14.61→14.6" in h["alerts"][0]["msg"]


# ---------------------------------------------------------------------------
# 6 — versión nueva → campanita
# ---------------------------------------------------------------------------
def _armar_version(monkeypatch, vista, nueva, n_dif=0):
    cap = {"sql": [], "avisos": []}
    monkeypatch.setattr(al, "version_vista", lambda: vista)
    monkeypatch.setattr(al, "version_asinfo", lambda: nueva)
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: cap["sql"].append(params))
    monkeypatch.setattr(al, "diferencias", lambda forzar=False: {
        "diferencias": [{"x": i} for i in range(n_dif)]})
    monkeypatch.setattr("modules.avisos.queries.avisar",
                        lambda **kw: cap["avisos"].append(kw) or True)
    return cap


def test_la_primera_vez_solo_aprende_la_version(monkeypatch):
    cap = _armar_version(monkeypatch, None, {"version": 554, "desde": "2026-04-14"})
    r = al.chequear_version()
    assert r["cambio"] is False and r["primera_vez"] is True
    assert cap["sql"] == [{"v": 554, "d": "2026-04-14"}]
    assert cap["avisos"] == []


def test_misma_version_no_hace_nada(monkeypatch):
    cap = _armar_version(monkeypatch, {"version": 554}, {"version": 554, "desde": "2026-04-14"})
    assert al.chequear_version()["cambio"] is False
    assert cap["sql"] == [] and cap["avisos"] == []


def test_version_nueva_avisa_con_cuantos_precios_quedaron_distintos(monkeypatch):
    cap = _armar_version(monkeypatch, {"version": 554},
                         {"version": 601, "desde": "2026-09-15"}, n_dif=7)
    r = al.chequear_version()
    assert r["cambio"] is True and r["n_diferencias"] == 7
    (av,) = cap["avisos"]
    assert av["nivel"] == "alerta" and av["cantidad"] == 7
    assert "versión 601" in av["titulo"] and "15/09/2026" in av["detalle"]
    assert av["clave"] == "precios-asinfo-version-601"
    assert av["url"] == "/precios#precios-asinfo"


def test_version_nueva_sin_diferencias_avisa_en_ok(monkeypatch):
    cap = _armar_version(monkeypatch, {"version": 554},
                         {"version": 601, "desde": "2026-09-15"}, n_dif=0)
    al.chequear_version()
    assert cap["avisos"][0]["nivel"] == "ok"


def test_si_asinfo_no_contesta_la_version_no_se_toca_nada(monkeypatch):
    cap = _armar_version(monkeypatch, {"version": 554}, None)
    assert al.chequear_version()["cambio"] is False
    assert cap["sql"] == []


def test_correr_si_toca_a_lo_sumo_una_vez_por_hora(monkeypatch):
    llamadas = []
    monkeypatch.setattr(al, "chequear_version", lambda: llamadas.append(1) or {"cambio": False})
    monkeypatch.setattr(al, "_auto_ultimo", 0.0)
    reloj = {"t": 1000.0}
    monkeypatch.setattr(al._time, "monotonic", lambda: reloj["t"])
    assert al.correr_si_toca()["corrio"] is True
    reloj["t"] += 1800
    assert al.correr_si_toca()["corrio"] is False
    reloj["t"] += 1801
    assert al.correr_si_toca()["corrio"] is True
    assert len(llamadas) == 2


def test_correr_si_toca_se_apaga_por_env(monkeypatch):
    monkeypatch.setenv("PRECIOS_ASINFO_AUTO", "0")
    monkeypatch.setattr(al, "chequear_version", lambda: pytest.fail("no debía correr"))
    assert al.correr_si_toca() == {"corrio": False}


# ---------------------------------------------------------------------------
# Las rutas
# ---------------------------------------------------------------------------
@pytest.fixture
def _base_fake(monkeypatch):
    monkeypatch.setattr(queries, "matriz", lambda: _MATRIZ_PROD)
    monkeypatch.setattr(queries, "precio_plano", lambda: _PLANOS_PROD)


@pytest.fixture
def duenia(app, fake_db, _base_fake):
    rid = fake_db.add_role("Accionista", ["*"])
    uid = fake_db.add_user("tamara", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    return c


@pytest.fixture
def vendedor(app, fake_db, _base_fake):
    rid = fake_db.add_role("Vendedor", [])
    uid = fake_db.add_user("vendedor", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    return c


def test_el_bloque_esta_cerrado_y_solo_lo_ve_quien_edita(duenia, vendedor):
    html = duenia.get("/precios").get_data(as_text=True)
    assert 'id="precios-asinfo"' in html
    marca = html[html.index('<details id="precios-asinfo"'):][:400]
    assert " open" not in marca
    assert 'id="precios-asinfo"' not in vendedor.get("/precios").get_data(as_text=True)


def test_precios_no_le_pregunta_a_asinfo_al_cargar(duenia):
    with patch("modules._lib.metabase_client.fetch_dataset_estado") as fake:
        duenia.get("/precios")
    fake.assert_not_called()


def test_la_ruta_devuelve_las_diferencias_con_su_clave(duenia, monkeypatch):
    monkeypatch.setattr(al, "traer_asinfo", lambda: (_filas_554(), True))
    data = duenia.get("/precios/asinfo-lista").get_json()
    assert data["ok"] is True and data["version"] == 554
    (d,) = data["diferencias"]
    assert d["clave"] == "m_5_cuellos"


def test_el_vendedor_no_ve_la_ruta(vendedor):
    assert vendedor.get("/precios/asinfo-lista").status_code == 404
    assert vendedor.post("/precios/asinfo-aplicar", data={"celda": "m_5_cuellos"}).status_code == 404


def test_aplicar_desde_la_pantalla(duenia, monkeypatch):
    monkeypatch.setattr(al, "traer_asinfo", lambda: (_filas_554(), True))
    escrito = []
    monkeypatch.setattr(queries, "actualizar_precio",
                        lambda clase, col, valor, usuario: escrito.append((clase, col, valor)))
    resp = duenia.post("/precios/asinfo-aplicar", data={"celda": ["m_5_cuellos"]})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#precios-asinfo")
    assert escrito == [(5, "cuellos", 12.7)]


def test_aplicar_sin_tildar_nada_no_escribe(duenia, monkeypatch):
    monkeypatch.setattr(queries, "actualizar_precio", lambda *a: pytest.fail("escribió"))
    assert duenia.post("/precios/asinfo-aplicar", data={}).status_code == 302
