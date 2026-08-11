"""Hilo despachado SIN orden de fabricación → aviso (TMT 2026-08-11).

El 11/08 salieron 8.100 kg a Ponce con el despacho marcado `#OF:NR` y la
utilidad cayó $ 24.327: el material sin orden no entra a "en proceso", así que
sale de la bodega y no entra a ningún lado. La dueña pidió enterarse por la
campanita y no por la utilidad, con este texto:
*"salieron 4.860 kg de hilo falta cargar orden de fabricación — OSM-000010458"*.
"""
from unittest.mock import patch

import pytest

from modules.asinfo import hilo_sin_of as hs

# La fila tal como la devuelve Metabase para OSM-000010458 (caso real).
FILA_PONCE = {
    "numero": "OSM-000010458",
    "id_bodega": 51,
    "kg": 4860.0,
    "usuario": "mprima",
    "descripcion": "[#OF:NR] |Matriz| A PONCE PENDIENTE 180/C KW22",
    "creado": "2026-08-11 07:33",
}


@pytest.fixture(autouse=True)
def _sin_freno(monkeypatch):
    """El freno de 15 min es de proceso: sin resetearlo el 2º test no corre."""
    monkeypatch.setattr(hs, "_ultima_corrida", 0.0)
    monkeypatch.delenv("HILO_SIN_OF", raising=False)
    monkeypatch.delenv("HILO_SIN_OF_BODEGAS", raising=False)
    monkeypatch.delenv("HILO_SIN_OF_MIN_KG", raising=False)


def _correr(casos, vivos=(), ofts=None):
    """`vivos` son filas de `scintela.aviso` TAL COMO LAS DEVUELVE LA BASE.

    🚨 A propósito se mockea `db.fetch_all` y no `avisos.listar()`: la primera
    versión leía por `listar`, que NO devuelve la columna `clave`, y por eso no
    resolvía nunca. Un fake con la forma de la tabla lo habría cazado.
    """
    import db as _db
    puestos, resueltos = [], []
    with patch.object(hs, "despachos_sin_of", return_value=casos), \
         patch.object(hs, "ordenes_de", return_value=dict(ofts or {})), \
         patch.object(_db, "fetch_all", return_value=list(vivos)), \
         patch("modules.avisos.queries.resolver",
               side_effect=lambda i, **kw: resueltos.append((i, kw)) or True), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        res = hs.revisar_si_toca()
    return res, puestos, resueltos


def _caso(**kw):
    base = {"numero": "OSM-000010458", "id_bodega": 51, "material": "hilo",
            "kg": 4860.0, "usuario": "mprima",
            "descripcion": "A PONCE PENDIENTE 180/C KW22",
            "creado": "2026-08-11 07:33"}
    base.update(kw)
    return base


def test_el_titulo_es_el_texto_que_pidio_la_duena():
    res, puestos, _arch = _correr([_caso()])
    assert res["avisados"] == 1
    a = puestos[0]
    assert a["titulo"] == ("Salieron 4.860 kg de hilo — "
                           "falta cargar orden de fabricación")
    assert a["clave"] == "hilo-sin-of:OSM-000010458"
    assert a["nivel"] == "alerta"


def test_el_detalle_lleva_el_numero_de_despacho_y_la_glosa():
    """Sin el número y la glosa el aviso no se puede accionar: hay que poder
    ir a Asinfo y encontrar EXACTAMENTE ese despacho."""
    _, puestos, _arch = _correr([_caso()])
    d = puestos[0]["detalle"]
    assert d == ("OSM-000010458 · A PONCE PENDIENTE 180/C KW22 · 07:33\n"
                 "Cargale la orden en Asinfo y vuelven.")
    assert len(d) < 120      # dueña: "está muy largo el mensaje"


def test_habla_en_KILOS_y_de_la_BODEGA_nunca_de_la_utilidad():
    """Dueña 2026-08-11: *"no digas la utilidad, decí la bodega baja x kg"*.
    Quien recibe el aviso es quien despacha: lo que puede arreglar son kilos."""
    _, puestos, _arch = _correr([_caso()])
    a = puestos[0]
    assert "utilidad" not in (a["titulo"] + a["detalle"]).lower()
    assert "$" not in a["detalle"]
    assert a.get("importe") is None
    assert a["cantidad"] == 4860


def test_un_aviso_por_despacho_no_uno_por_dia():
    res, puestos, _arch = _correr([_caso(),
                            _caso(numero="OSM-000010460", kg=3240.0,
                                  descripcion="A PONCE PENDIENTE 120/C KW20")])
    assert res["avisados"] == 2
    assert {p["clave"] for p in puestos} == {
        "hilo-sin-of:OSM-000010458", "hilo-sin-of:OSM-000010460"}


def test_apagado_por_env_no_avisa(monkeypatch):
    monkeypatch.setenv("HILO_SIN_OF", "0")
    res, puestos, _arch = _correr([_caso()])
    assert res == {"corrio": False, "motivo": "apagado"}
    assert puestos == []


def test_el_freno_no_deja_correr_dos_veces_seguidas():
    _correr([_caso()])
    res2, puestos2, _ = _correr([_caso()])
    assert res2 == {"corrio": False, "motivo": "freno"}
    assert puestos2 == []


def test_si_asinfo_no_contesta_no_avisa_nada():
    """Fail-soft: una alarma que no puede leer no inventa."""
    puestos = []
    with patch("modules._lib.metabase_client.fetch_dataset", return_value=[]), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        res = hs.revisar_si_toca()
    assert res["avisados"] == 0
    assert puestos == []


# ── la consulta ────────────────────────────────────────────────────────────

def _sql_de(**kw):
    visto = {}

    def _fake(db_id, sql, *a, **k):
        visto["db"] = db_id
        visto["sql"] = sql
        return []

    with patch("modules._lib.metabase_client.fetch_dataset", side_effect=_fake):
        hs.despachos_sin_of(**kw)
    return visto


def test_pregunta_por_las_DOS_junctions():
    """El balance cuelga de la junction de cabecera y stock_en_proceso de la de
    detalle. Mirar una sola daría un falso positivo el día que Asinfo cambie."""
    sql = _sql_de()["sql"]
    assert "orden_fabricacion_orden_salida_material" in sql
    assert "detalle_orden_salida_material_orden_fabricacion" in sql
    assert sql.count("NOT EXISTS") == 2


def test_va_contra_asinfo_y_solo_mira_la_bodega_de_hilo():
    """La tela cruda sale sin orden casi todos los días — meterla acá sería un
    ⚠ diario. Se vigila 51 y nada más, salvo que se encienda por env."""
    visto = _sql_de()
    assert visto["db"] == 2
    assert "d.id_bodega IN (51)" in visto["sql"]


def test_la_tela_cruda_se_enciende_por_env_sin_deploy(monkeypatch):
    monkeypatch.setenv("HILO_SIN_OF_BODEGAS", "51,52")
    assert "d.id_bodega IN (51, 52)" in _sql_de()["sql"]


def test_el_piso_de_kilos_deja_afuera_los_despachos_chicos():
    """Con piso 200, los sin-orden de 21, 37 y 97 kg de 2026 no encienden nada."""
    with patch("modules._lib.metabase_client.fetch_dataset",
               return_value=[dict(FILA_PONCE, kg=37.55, numero="OSM-000010454")]):
        assert hs.despachos_sin_of() == []


def test_la_glosa_sale_sin_el_OFNR_ni_las_barras():
    """El `[#OF:NR] |Matriz|` es plomería de Asinfo: en la campanita estorba."""
    with patch("modules._lib.metabase_client.fetch_dataset",
               return_value=[FILA_PONCE]):
        casos = hs.despachos_sin_of()
    assert casos[0]["descripcion"] == "A PONCE PENDIENTE 180/C KW22"
    assert casos[0]["material"] == "hilo"
    assert casos[0]["kg"] == 4860.0


# ── el aviso se da vuelta cuando cargan la orden ───────────────────────────

def test_si_cargan_la_orden_el_aviso_pasa_a_resuelto():
    """Dueña 2026-08-11: *"y si cargan la oft, también saldría en campanita
    no?"* → *"claro, campanita"*. Archivarlo lo apagaba en silencio y ella veía
    el anuncio bajar de 4 a 3 sin que nadie se lo dijera."""
    vivo = {"id_aviso": 77, "clave": "hilo-sin-of:OSM-000010460",
            "cantidad": 3240}
    res, _p, resueltos = _correr([_caso(numero="OSM-000010462")],
                                 vivos=[vivo],
                                 ofts={"OSM-000010460": "OFT-000040516"})
    assert res["resueltos"] == 1
    _id, kw = resueltos[0]
    assert _id == 77
    assert kw["titulo"] == "3.240 kg de hilo — orden cargada"
    assert kw["detalle"] == "OSM-000010460 → OFT-000040516"


def test_sin_saber_la_orden_igual_se_cierra():
    """Si Asinfo no contesta el número de la OFT, el aviso se cierra lo mismo:
    dejarlo en rojo diciendo 'falta cargar' sería peor que no nombrarla."""
    vivo = {"id_aviso": 77, "clave": "hilo-sin-of:OSM-000010460",
            "cantidad": 3240}
    _, _p, resueltos = _correr([], vivos=[vivo], ofts={})
    assert resueltos[0][1]["detalle"] == "OSM-000010460"


def test_el_que_sigue_sin_orden_no_se_toca():
    vivo = {"id_aviso": 77, "clave": "hilo-sin-of:OSM-000010458",
            "cantidad": 4860}
    _, _p, resueltos = _correr([_caso()], vivos=[vivo])
    assert resueltos == []


def test_no_toca_avisos_de_otra_cosa():
    """`fuente="stock"` trae más que esto: sólo son nuestros los de la clave."""
    ajeno = {"id_aviso": 9, "clave": "otra-cosa:123", "cantidad": 1}
    _, _p, resueltos = _correr([_caso()], vivos=[ajeno])
    assert resueltos == []


def test_la_consulta_pide_la_clave_que_listar_NO_devuelve():
    """El bug del 11/08: se leía por `avisos.listar()`, que no trae `clave`, y
    la comparación era siempre contra un string vacío — no resolvía nunca y no
    dejaba ni un error. El SELECT propio tiene que pedir clave y cantidad, y
    saltear los ya resueltos."""
    import db as _db
    visto = {}
    with patch.object(hs, "despachos_sin_of", return_value=[]), \
         patch.object(_db, "fetch_all",
                      side_effect=lambda sql, *a, **k: visto.update(sql=sql) or []):
        hs.revisar_si_toca()
    sql = visto["sql"]
    # La PROYECCIÓN, no cualquier mención: el bug era que `clave` no venía en
    # el SELECT (aunque estuviera en el WHERE), y el dict llegaba sin la key.
    assert "SELECT id_aviso, clave, cantidad" in sql
    assert "hilo-sin-of:" in sql
    assert "nivel <> 'ok'" in sql


def test_una_fila_sin_clave_no_resuelve_nada():
    """La firma exacta del bug: así llegaban las filas de `avisos.listar()`.
    Sin `clave` no se puede saber de qué despacho habla — y resolver a ciegas
    sería peor que no resolver."""
    _, _p, resueltos = _correr([], vivos=[{"id_aviso": 77, "cantidad": 3240}])
    assert resueltos == []
