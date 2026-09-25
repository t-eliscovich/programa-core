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
    # Y el cache de 120 s de `despachos_sin_of` (TMT 24/08) es de proceso
    # igual: sin limpiarlo, el test que sigue lee la respuesta del anterior.
    hs.reset_cache()


def _correr(casos, vivos=(), ofts=None, situacion="auto"):
    """`vivos` son filas de `scintela.aviso` TAL COMO LAS DEVUELVE LA BASE.

    `ofts` es el atajo de siempre: {OSM: OFT} → situación "tiene orden".
    `situacion` pisa todo: el dict que devolvería `situacion_de`, o None
    para simular que Asinfo no contestó.

    🚨 A propósito se mockea `db.fetch_all` y no `avisos.listar()`: la primera
    versión leía por `listar`, que NO devuelve la columna `clave`, y por eso no
    resolvía nunca. Un fake con la forma de la tabla lo habría cazado.
    """
    import db as _db
    puestos, resueltos = [], []
    if situacion == "auto":
        situacion = {osm: {"oft": oft, "viva": True}
                     for osm, oft in (ofts or {}).items()}
    with patch.object(hs, "despachos_sin_of", return_value=casos), \
         patch.object(hs, "situacion_de", return_value=situacion), \
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
    assert a["titulo"] == ("Salieron 4.860 kg de hilo a Ponce — "
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
            "cantidad": 3240,
            "titulo": "Salieron 3.240 kg de hilo a Ponce — falta cargar orden de fabricación"}
    res, _p, resueltos = _correr([_caso(numero="OSM-000010462")],
                                 vivos=[vivo],
                                 ofts={"OSM-000010460": "OFT-000040516"})
    assert res["resueltos"] == 1
    _id, kw = resueltos[0]
    assert _id == 77
    # qué y de quién se conserva del aviso original
    assert kw["titulo"] == "3.240 kg de hilo a Ponce — orden cargada"
    assert kw["detalle"] == "OSM-000010460 → OFT-000040516"


VIVO_PONCE = {"id_aviso": 77, "clave": "hilo-sin-of:OSM-000010460",
              "cantidad": 3240,
              "titulo": "Salieron 3.240 kg de hilo a Ponce — falta cargar orden de fabricación"}


def test_si_anulan_el_despacho_el_aviso_dice_eso_y_no_orden_cargada():
    """TMT 2026-09-21: OSM-000010926 ("MQ11 OF41886 DUPLICADO") era una copia
    exacta de la OSM-000010925 —los mismos 60 lotes, los mismos kilos— y dejó
    60 lotes de hilo en negativo. Se arregla ANULÁNDOLA en Asinfo, no
    colgándole una orden; y el aviso tiene que contar eso, no inventar una
    "orden cargada" que no existe."""
    _, _p, resueltos = _correr(
        [], vivos=[VIVO_PONCE],
        situacion={"OSM-000010460": {"oft": "", "viva": False}})
    assert len(resueltos) == 1
    kw = resueltos[0][1]
    assert kw["titulo"] == "3.240 kg de hilo a Ponce — se anuló el despacho"
    assert "OSM-000010460" in kw["detalle"]
    assert "orden cargada" not in kw["titulo"]


def test_un_despacho_que_asinfo_ya_no_tiene_cuenta_como_anulado():
    """Se preguntó por él y no vino en la respuesta: lo borraron."""
    _, _p, resueltos = _correr([], vivos=[VIVO_PONCE], situacion={})
    assert resueltos[0][1]["titulo"].endswith("— se anuló el despacho")


def test_si_sigue_vivo_y_sin_orden_el_aviso_NO_se_cierra():
    """El despacho salió de `despachos_sin_of` por la ventana de días, no
    porque alguien lo haya arreglado. Cerrarlo como "orden cargada" era la
    forma de que un ⚠ de más de una semana se resolviera solo, mintiendo."""
    _, _p, resueltos = _correr(
        [], vivos=[VIVO_PONCE],
        situacion={"OSM-000010460": {"oft": "", "viva": True}})
    assert resueltos == []


def test_si_asinfo_no_contesta_NO_se_resuelve_nada():
    """Metabase caído hacía que `despachos_sin_of` devolviera [] y TODOS los
    avisos abiertos pasaran a ✅ "orden cargada" de una vez. Sin respuesta de
    Asinfo no hay evidencia, y sin evidencia los ⚠ se quedan como están."""
    res, _p, resueltos = _correr([], vivos=[VIVO_PONCE], situacion=None)
    assert resueltos == []
    assert res["resueltos"] == 0


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


# ── el destino sale de la glosa, y sólo de ahí ──────────────────────────────

def test_el_destino_sale_de_la_glosa():
    """Dueña 2026-08-11: *"si decía Ponce, ponéselo; si no, vacío"*."""
    assert hs.destino_de("A PONCE PENDIENTE 180/C KW22") == "Ponce"
    assert hs.destino_de("A RIVADENEIRA 90/C") == "Rivadeneira"


def test_sin_glosa_no_se_inventa_destino():
    """Asinfo no guarda a quién se despachó: no hay destino, ni transportista,
    ni centro de costo. Fuera de la glosa no hay de dónde sacarlo, y dos de los
    seis despachos del 11/08 la tenían vacía."""
    assert hs.destino_de("") == ""
    assert hs.destino_de("MQ02 OF40515") == ""
    assert hs.destino_de("A 40517") == ""          # un número no es un nombre
    assert hs.destino_de("APONCE") == ""           # sin la "A" suelta, no


def test_sin_destino_el_titulo_queda_como_estaba():
    _, puestos, _ = _correr([_caso(numero="OSM-000010465", kg=910.0,
                                   descripcion="")])
    assert puestos[0]["titulo"] == ("Salieron 910 kg de hilo — "
                                    "falta cargar orden de fabricación")


# ── la situación se pregunta con evidencia, no se adivina ──────────────────

def _situacion_con(rows, ok=True):
    visto = {}

    def _fake(db_id, sql, *a, **k):
        visto["db"] = db_id
        visto["sql"] = sql
        return list(rows), ok

    with patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=_fake):
        out = hs.situacion_de({"OSM-000010925", "OSM-000010926"})
    return out, visto


def test_situacion_distingue_orden_anulado_y_vivo():
    out, visto = _situacion_con([
        {"osm": "OSM-000010925", "estado": 1, "kg": 1647.0,
         "oft": "OFT-000041886"},
        {"osm": "OSM-000010926", "estado": 0, "kg": None, "oft": None},
    ])
    assert visto["db"] == 2
    assert out["OSM-000010925"] == {"oft": "OFT-000041886", "viva": True}
    assert out["OSM-000010926"] == {"oft": "", "viva": False}


def test_situacion_un_despacho_con_renglones_pero_sin_orden_esta_vivo():
    out, _ = _situacion_con([
        {"osm": "OSM-000010926", "estado": 1, "kg": 1620.0, "oft": None}])
    assert out["OSM-000010926"] == {"oft": "", "viva": True}
    # el que se preguntó y no vino: borrado
    assert out["OSM-000010925"] == {"oft": "", "viva": False}


def test_situacion_devuelve_None_si_asinfo_no_contesto():
    """`None` y no `{}`: `{}` diría "los borraron a todos"."""
    out, _ = _situacion_con([], ok=False)
    assert out is None


def test_situacion_busca_la_oft_en_las_dos_junctions():
    """Igual que `despachos_sin_of`: si el despacho cuelga de cualquiera de
    las dos, dejó de estar sin orden por eso y hay que poder nombrarla."""
    _, visto = _situacion_con([])
    sql = visto["sql"]
    assert "orden_fabricacion_orden_salida_material" in sql
    assert "detalle_orden_salida_material_orden_fabricacion" in sql
    assert "'OSM-000010925'" in sql and "'OSM-000010926'" in sql


def test_situacion_sin_numeros_no_pregunta():
    with patch("modules._lib.metabase_client.fetch_dataset_estado") as f:
        assert hs.situacion_de(set()) == {}
    f.assert_not_called()


def test_un_dia_sin_despachos_TAMBIEN_se_guarda(monkeypatch):
    """TMT 2026-09-25: con `if rows:` el caso normal —cero despachos
    esperando— no se cacheaba nunca y cada pantalla pagaba ~540 ms de Asinfo."""
    from modules._lib import metabase_client

    hs.reset_cache()
    llamadas = []

    def _fake(*a, **k):
        llamadas.append(1)
        return []

    monkeypatch.setattr(metabase_client, "fetch_dataset", _fake)
    assert hs.despachos_sin_of(dias=0) == []
    assert hs.despachos_sin_of(dias=0) == []
    assert len(llamadas) == 1
    hs.reset_cache()


def test_si_asinfo_no_contesta_no_se_guarda_el_vacio(monkeypatch):
    from modules._lib import metabase_client

    hs.reset_cache()
    monkeypatch.setattr(metabase_client, "fetch_dataset_estado",
                        lambda *a, **k: ([], False))
    assert hs.despachos_sin_of(dias=0) == []
    assert not hs._CACHE
