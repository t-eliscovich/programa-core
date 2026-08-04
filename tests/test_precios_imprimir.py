"""La hoja imprimible de la lista de precios (al pie de /precios).

Pedido de la dueña 2026-08-04, literal: "Precios tenemos que poder imprimir,
tambien no es Falso es fleece" + "una impresion asi masomenos, hagamosla
elegante y simple" (con el Excel "LISTA DE PRECIOS IVA 15%" como modelo).

Dos cosas se prueban acá:

1. FLEECE, no FALSO. `falso` es el NOMBRE DE COLUMNA que viene de
   PRECIOS.DBF; la tela se llama FLEECE. Proformas ya lo tenía bien
   (`("falso", "FLEECE")` en modules/proformas/queries.py) y /precios lo
   mostraba mal. El test es sobre TELAS, no sobre el HTML, así que también
   cubre la matriz editable, el <select> de descuentos y la hoja impresa de
   una sola vez.

2. La hoja: matriz clases x telas al pie de /precios ("puedes poner el
   imprimir abajo y asi no hay tantas pantallas"), SIEMPRE con IVA 15%
   ("nadie imprime sin iva") y con el tramo de descuento por querystring.
   El riesgo real es que el descuento o el IVA se apliquen mal, así que los
   tests miran el NÚMERO, no el layout.
"""
from __future__ import annotations

import pytest

from modules.precios import queries

# ---------------------------------------------------------------------------
# 1. La tela es FLEECE
# ---------------------------------------------------------------------------


def test_la_columna_falso_se_muestra_como_fleece():
    """`falso` es el nombre de columna del dBase; la tela es FLEECE."""
    etiquetas = dict(queries.TELAS)
    assert etiquetas["falso"] == "FLEECE"


def test_ninguna_tela_se_llama_falso():
    """Ninguna etiqueta visible puede decir FALSO (era la queja de la dueña)."""
    assert "FALSO" not in {label.upper() for _col, label in queries.TELAS}


def test_las_etiquetas_coinciden_con_proformas():
    """Proformas y Precios leen la MISMA columna: no pueden llamarla distinto.

    Si mañana alguien renombra la tela en un lado y no en el otro, la dueña
    ve dos nombres para el mismo precio. Este test ata los dos módulos.
    """
    from modules.proformas import queries as proformas_queries

    precios = dict(queries.TELAS)
    for col, label in proformas_queries.TELAS:
        if col in precios:
            assert precios[col] == label, f"{col}: precios={precios[col]!r} proformas={label!r}"


# ---------------------------------------------------------------------------
# 2. tabla_impresion: el número
# ---------------------------------------------------------------------------


def _fila(**kw):
    base = {col: None for col, _ in queries.TELAS}
    base.update({"clase": 1, "descripcio": "BLANCO"})
    base.update(kw)
    return base


def test_tramo_basico_sin_iva_es_el_precio_de_lista():
    out = queries.tabla_impresion([_fila(jersey=8.87)], 0, con_iva=False)
    assert out[0]["valores"][0] == 8.87
    assert out[0]["descripcio"] == "BLANCO"


def test_tramo_basico_con_iva_multiplica_por_115():
    """El Excel de la dueña se titula "IVA 15%" y hace exactamente esto."""
    out = queries.tabla_impresion([_fila(jersey=8.87)], 0, con_iva=True)
    assert out[0]["valores"][0] == round(8.87 * 1.15, 2) == 10.20


def test_descuento_en_cascada_se_aplica_sobre_la_lista():
    """5%+9% = lista x 0,95 x 0,91 (sucesivos, no 14% de una)."""
    idx = [i for i, (lbl, _f) in enumerate(queries.TRAMOS_DESCUENTO) if lbl == "5%+9%"][0]
    out = queries.tabla_impresion([_fila(jersey=10.00)], idx, con_iva=False)
    assert out[0]["valores"][0] == round(10.00 * 0.95 * 0.91, 2) == 8.64


def test_descuento_y_iva_se_componen():
    """El IVA va sobre el NETO ya descontado, no sobre la lista."""
    idx = [i for i, (lbl, _f) in enumerate(queries.TRAMOS_DESCUENTO) if lbl == "5%"][0]
    out = queries.tabla_impresion([_fila(jersey=10.00)], idx, con_iva=True)
    assert out[0]["valores"][0] == round(10.00 * 0.95 * 1.15, 2)


def test_celda_vacia_queda_vacia_no_cero():
    """NULL en la tabla = raya en la hoja. Un 0,00 sería un precio inventado."""
    out = queries.tabla_impresion([_fila(jersey=8.87)], 0, con_iva=False)
    valores = dict(zip([c for c, _ in queries.TELAS], out[0]["valores"], strict=False))
    assert valores["jersey"] == 8.87
    assert valores["rib"] is None


def test_valores_alineados_al_orden_de_telas():
    """La hoja imprime `valores` en paralelo al header `telas`: mismo largo."""
    out = queries.tabla_impresion([_fila(jersey=1.0), _fila(clase=2, rib=2.0)], 0, False)
    for fila in out:
        assert len(fila["valores"]) == len(queries.TELAS)


def test_tramo_invalido_es_error_no_silencio():
    with pytest.raises(ValueError):
        queries.tabla_impresion([_fila()], 99, con_iva=False)


# ---------------------------------------------------------------------------
# 3. La ruta
# ---------------------------------------------------------------------------


@pytest.fixture
def cliente_logueado(app, fake_db, monkeypatch):
    """Usuario SIN 'precios.editar': la hoja la ven todos (sólo login)."""
    rid = fake_db.add_role("Vendedor", [])
    uid = fake_db.add_user("vendedor", b"$2b$12$fakehash", rid)
    monkeypatch.setattr(
        queries,
        "matriz",
        lambda: [_fila(jersey=8.87, falso=9.19), _fila(clase=5, descripcio="FUERTES", jersey=11.04)],
    )
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    return client


def test_la_hoja_requiere_login(client):
    resp = client.get("/precios")
    assert resp.status_code in (302, 401)


def test_la_hoja_esta_al_pie_de_precios(cliente_logueado):
    """Una sola pantalla: la hoja va abajo de /precios, no aparte."""
    html = cliente_logueado.get("/precios").get_data(as_text=True)
    assert "LISTA DE PRECIOS" in html
    assert 'id="hoja"' in html
    assert "FLEECE" in html and "FALSO" not in html
    assert "BLANCO" in html and "FUERTES" in html


def test_la_hoja_sale_siempre_con_iva(cliente_logueado):
    """Dueña: "deja solo la opcion con IVA para imprimir, nadie imprime sin
    iva". No hay switch: 8,87 x 1,15 = 10,20."""
    html = cliente_logueado.get("/precios").get_data(as_text=True)
    assert "IVA 15%" in html
    assert "10,20" in html


def test_la_hoja_aplica_el_tramo_elegido(cliente_logueado):
    idx = [i for i, (lbl, _f) in enumerate(queries.TRAMOS_DESCUENTO) if lbl == "5%"][0]
    html = cliente_logueado.get(f"/precios?tramo={idx}").get_data(as_text=True)
    # 8,87 x 0,95 x 1,15 = 9,69
    assert "9,69" in html
    assert "Descuento 5%" in html


def test_tramo_basura_no_rompe(cliente_logueado):
    resp = cliente_logueado.get("/precios?tramo=99")
    assert resp.status_code == 200
    assert "10,20" in resp.get_data(as_text=True)


def test_la_ruta_vieja_de_imprimir_redirige(cliente_logueado):
    """Los links de este sistema son strings hardcodeados: la ruta vieja no
    puede empezar a dar 404."""
    resp = cliente_logueado.get("/precios/imprimir")
    assert resp.status_code == 302
    assert "/precios" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# 4. Telas de PRECIO ÚNICO (mig. 0159) — la tablita que mandó la dueña por foto
#    el 2026-08-04: "a proforma y precios agregar esto, tiene iva incluido".
# ---------------------------------------------------------------------------

# Lo que se ve en la foto, tal cual. Son NETOS (sin IVA), igual que la matriz.
FOTO = {"SCUBA": 11.25, "SUPLEX": 10.21, "BELTIS": 11.49, "NATY": 8.88}


def test_el_seed_guarda_el_numero_de_la_foto_tal_cual():
    """Las cifras de la foto ya son netas: van SIN dividir por 1,15.

    El primer intento las dividió (leyó "tiene iva incluido" como que venían
    con IVA) y la dueña lo corrigió: "les sacamos doblemente IVA, ya estaban
    sin IVA". Se ve mirando la matriz: KIANA vale 8,88 y NATY también 8,88,
    o sea están en la misma escala. Este test es el que impide que vuelva.
    """
    seed = {tela: precio for _o, tela, precio, ref, _n in queries._PLANO_SEED if ref is None}
    assert seed == FOTO


def test_jersey_3_y_3_5_no_tienen_precio_propio():
    """Cobran lo que el JERSEY: guardar una copia los desincronizaría."""
    refs = {tela: ref for _o, tela, _p, ref, _n in queries._PLANO_SEED if ref}
    assert refs == {"JERSEY 3,5": "jersey", "JERSEY 3": "jersey"}
    for _o, tela, precio, ref, _n in queries._PLANO_SEED:
        assert (precio is None) == bool(ref), tela


PLANOS = [
    {"id": 1, "orden": 1, "tela": "JERSEY 3,5", "precio": None, "ref_col": "jersey",
     "nota": "Precio de JERSEY"},
    {"id": 3, "orden": 3, "tela": "SCUBA", "precio": 11.25, "ref_col": None,
     "nota": "Todos los colores"},
]





def test_resolver_plano_muestra_el_rango_del_jersey():
    """En pantalla, JERSEY 3,5 muestra de cuánto a cuánto va el jersey."""
    filas = [_fila(jersey=8.64), _fila(clase=5, descripcio="FUERTES", jersey=10.75)]
    out = queries.resolver_plano(PLANOS, filas)
    fila = [p for p in out if p["tela"] == "JERSEY 3,5"][0]
    assert fila["rango"] == (8.64, 10.75)


def test_proformas_ofrece_las_telas_de_precio_unico(monkeypatch):
    """La proforma tiene que poder cotizar SCUBA y JERSEY 3,5."""
    from modules.proformas import queries as pq

    monkeypatch.setattr(queries, "precio_plano", lambda: PLANOS)
    planas = pq.telas_planas()
    por_label = {p["label"]: p for p in planas}
    assert por_label["SCUBA"]["precio"] == 11.25
    assert por_label["SCUBA"]["col"] is None
    # JERSEY 3,5 no lleva cifra: la saca de la columna jersey de la matriz.
    assert por_label["JERSEY 3,5"]["precio"] is None
    assert por_label["JERSEY 3,5"]["col"] == "jersey"


@pytest.fixture
def cliente_con_planos(app, fake_db, monkeypatch):
    rid = fake_db.add_role("Vendedor", [])
    uid = fake_db.add_user("vendedor2", b"$2b$12$fakehash", rid)
    monkeypatch.setattr(queries, "matriz", lambda: [_fila(jersey=8.87)])
    monkeypatch.setattr(queries, "precio_plano", lambda: PLANOS)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    return client


def test_la_hoja_impresa_incluye_las_telas_de_precio_unico(cliente_con_planos):
    """Van SUMADAS como columnas de la misma tabla, no en un bloque aparte
    (corrección de la dueña: "sumar a como está, no una foto así")."""
    html = cliente_con_planos.get("/precios").get_data(as_text=True)
    assert "SCUBA" in html
    assert "12,94" in html          # 11,25 neto x 1,15
    assert "JERSEY 3,5" in html
    # JERSEY 3,5 cobra lo del jersey: 8,87 x 1,15 = 10,20, y sale DOS veces
    # (columna JERSEY y columna JERSEY 3,5) en la misma fila.
    assert html.count("10,20") >= 2
    # Y no queda una tablita suelta con la nota de la foto.
    assert "Precio de JERSEY" not in html


def test_la_pantalla_de_precios_incluye_las_telas_de_precio_unico(cliente_con_planos):
    html = cliente_con_planos.get("/precios").get_data(as_text=True)
    assert "SCUBA" in html and "JERSEY 3,5" in html


# ---------------------------------------------------------------------------
# 5. columnas_hoja: UNA sola tabla (dueña: "sumar a como está, no una foto")
# ---------------------------------------------------------------------------


def test_columnas_hoja_pega_la_ref_al_lado_de_su_tela():
    """JERSEY 3,5 y JERSEY 3 van JUNTO a JERSEY, no al final."""
    cols = queries.columnas_hoja([_fila(jersey=8.87)], PLANOS)
    labels = [c["label"] for c in cols]
    assert labels[:2] == ["JERSEY", "JERSEY 3,5"]
    assert labels[-1] == "SCUBA"  # las de cifra fija, al final


def test_columnas_hoja_sin_planos_es_la_matriz_de_siempre():
    cols = queries.columnas_hoja([_fila(jersey=8.87)], [])
    assert [c["label"] for c in cols] == ["JERSEY"]


def test_la_columna_fija_repite_el_mismo_numero_en_todas_las_clases():
    filas = [_fila(jersey=8.64), _fila(clase=5, descripcio="FUERTES", jersey=10.75)]
    cols = queries.columnas_hoja(filas, PLANOS)
    i = [c["label"] for c in cols].index("SCUBA")
    out = queries.tabla_impresion(filas, 0, False, cols)
    assert {f["valores"][i] for f in out} == {FOTO["SCUBA"]}


def test_la_columna_ref_sigue_al_jersey_clase_por_clase():
    """No es una cifra fija: JERSEY 3,5 vale lo que el jersey de ESA clase."""
    filas = [_fila(jersey=8.64), _fila(clase=5, descripcio="FUERTES", jersey=10.75)]
    cols = queries.columnas_hoja(filas, PLANOS)
    labels = [c["label"] for c in cols]
    ij, i35 = labels.index("JERSEY"), labels.index("JERSEY 3,5")
    out = queries.tabla_impresion(filas, 0, False, cols)
    assert [f["valores"][i35] for f in out] == [f["valores"][ij] for f in out]
    assert out[0]["valores"][i35] != out[1]["valores"][i35]


def test_los_tramos_van_de_5_mas_4_a_5_mas_14_sin_saltos():
    """Dueña: "dame la opcion de calcular descuentos desde 5%+4% y 5%+5%
    hasta 5%+14% todas". Y son EN CASCADA: 5%+9% = x0,95 x0,91, no x0,86."""
    labels = [lbl for lbl, _f in queries.TRAMOS_DESCUENTO]
    assert labels[:2] == ["Basico", "5%"]
    assert labels[2:] == [f"5%+{n}%" for n in range(4, 15)]
    por_label = dict(queries.TRAMOS_DESCUENTO)
    for n in range(4, 15):
        assert por_label[f"5%+{n}%"] == 0.95 * (1 - n / 100)
    assert round(por_label["5%+9%"], 6) == 0.8645  # NO 0,86 (= 14% de una)


def test_el_selector_de_la_pantalla_ofrece_las_telas_de_precio_unico(cliente_con_planos):
    html = cliente_con_planos.get("/precios").get_data(as_text=True)
    assert "SCUBA" in html and "JERSEY 3,5" in html


def test_descuentos_de_una_tela_de_precio_unico(cliente_con_planos):
    """Elegir SCUBA en el selector muestra sus 5 tramos, igual que una tela
    de la matriz."""
    cols = queries.columnas_hoja([_fila(jersey=8.87)], PLANOS)
    scuba = [c for c in cols if c["label"] == "SCUBA"][0]
    filas = [_fila(jersey=8.87)]
    out = queries.tabla_descuentos_columna(filas, scuba)
    assert out[0]["lista"] == 11.25
    assert out[0]["netos"][1] == round(11.25 * 0.95, 2)


# ---------------------------------------------------------------------------
# 6. MEDICAL fuera de la lista (dueña 2026-08-04: "borrar medical")
# ---------------------------------------------------------------------------


def test_medical_no_esta_en_la_lista_de_precios():
    """Salió de la pantalla, de la hoja impresa y de la proforma."""
    from modules.proformas import queries as proformas_queries

    assert "medical" not in queries.COLUMNAS_TELA
    assert "MEDICAL" not in {lab.upper() for _c, lab in queries.TELAS}
    assert "MEDICAL" not in {lab.upper() for _c, lab in proformas_queries.TELAS}


def test_la_matriz_no_pide_la_columna_medical():
    """`matriz()` arma el SELECT con TELAS: si la tela no está, la columna no
    se lee. Los datos siguen en la tabla — esto es sólo dejar de mostrarla."""
    capturado = {}

    def fake_fetch_all(sql, *a, **kw):
        capturado["sql"] = sql
        return []

    import db

    original = db.fetch_all
    db.fetch_all = fake_fetch_all
    try:
        queries.matriz()
    finally:
        db.fetch_all = original
    assert "medical" not in capturado["sql"]
    assert "jersey" in capturado["sql"]


def test_la_proforma_no_ofrece_medical_como_tipo():
    """El <datalist> de tipos sale de la constante TIPOS del template."""
    from pathlib import Path

    tpl = Path("modules/proformas/templates/proformas/nueva.html").read_text()
    assert "label:'MEDICAL'" not in tpl.replace(" ", "")
