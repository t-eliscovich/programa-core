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


def _correr(casos, vivos=()):
    puestos, archivados = [], []
    with patch.object(hs, "despachos_sin_of", return_value=casos), \
         patch("modules.avisos.queries.listar", return_value=list(vivos)), \
         patch("modules.avisos.queries.archivar",
               side_effect=lambda i, **kw: archivados.append(i) or True), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        res = hs.revisar_si_toca()
    return res, puestos, archivados


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
    assert "OSM-000010458" in d
    assert "A PONCE PENDIENTE 180/C KW22" in d
    assert "2026-08-11 07:33" in d


def test_habla_en_KILOS_y_de_la_BODEGA_nunca_de_la_utilidad():
    """Dueña 2026-08-11: *"no digas la utilidad, decí la bodega baja x kg"*.
    Quien recibe el aviso es quien despacha: lo que puede arreglar son kilos."""
    _, puestos, _arch = _correr([_caso()])
    a = puestos[0]
    assert "la bodega baja 4.860 kg" in a["detalle"]
    assert "utilidad" not in a["detalle"].lower()
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


# ── el aviso se cae solo cuando cargan la orden ────────────────────────────

def test_si_cargan_la_orden_el_aviso_se_archiva():
    """Dueña 2026-08-11: *"y si cargan la oft, también saldría en campanita
    no?"*. Sí — y quedaría colgado diciendo "falta cargar" sobre algo ya
    cargado, que es como se enseña a no creerle a la campanita."""
    vivo = {"id_aviso": 77, "clave": "hilo-sin-of:OSM-000010458"}
    res, _puestos, archivados = _correr([_caso(numero="OSM-000010462")],
                                        vivos=[vivo])
    assert archivados == [77]
    assert res["archivados"] == 1


def test_el_que_sigue_sin_orden_no_se_archiva():
    vivo = {"id_aviso": 77, "clave": "hilo-sin-of:OSM-000010458"}
    _, _p, archivados = _correr([_caso()], vivos=[vivo])
    assert archivados == []


def test_no_toca_avisos_de_otra_cosa():
    """`fuente="stock"` trae más que esto: sólo son nuestros los de la clave."""
    ajeno = {"id_aviso": 9, "clave": "otra-cosa:123"}
    _, _p, archivados = _correr([_caso()], vivos=[ajeno])
    assert archivados == []
