"""Los dos avisos que faltaban: kilos sin su plata, y partidas que no se unieron.

TMT 2026-08-21, después de MH 68/69/70: *"cómo hacemos por si algo no se unió e
identifica que entraron sin costo o las importaciones no estaban unidas"*.

Los dos números ya se calculaban y no los miraba nadie: `kg_sin_costo` (en cada
foto de la traza y en las estadísticas del health) y `grupo_aviso` (sólo en
/admin/debug-grupos-partidas). Acá se prueban con los números medidos ese día:
47.580 kg afuera del divisor, la tarifa en 3,0013 en vez de 2,9270 y 172.881
US$ de stock apoyados en eso.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from concepto_parser import parse_nota_importacion
from filters import today_ec
from modules.importaciones import service as svc
from modules.importaciones import vigilancia as vig

# La foto de la traza del 21/08: 1,9 M kg de hilado a 3,0013, más crudo y
# terminado. Es lo que hace que el efecto se pueda decir en dólares.
FOTO = {"hilado_kg": 1_900_000.0, "hilado_ukg": 3.0013,
        "tejido_kg": 210_000.0, "terminado_kg": 170_000.0}


def _filas(spec, *, dias_atras=2):
    """`spec` = [(nota, kg, importe), ...] recibidas hace `dias_atras` días."""
    rec = str(today_ec() - timedelta(days=dias_atras))
    rows = []
    for i, (nota, kg, importe) in enumerate(spec, start=1):
        r = {"im_numero": f"IM-000070{i}", "nota": nota, "kg": float(kg),
             "fecha": str(today_ec() - timedelta(days=60)),
             "fecha_recepcion": rec, "prov_cod_asinfo": "MH", "recibida": True,
             "compra": ({"items": [{"id_compra": i, "fecha": rec,
                                    "importe": float(importe)}]}
                        if importe else None),
             "anticipo": None}
        c = parse_nota_importacion(nota)
        r.update(codigo=c.get("codigo"), prov=c.get("prov"),
                 numero=c.get("numero"), numero_hasta=c.get("numero_hasta"))
        r["importe_programa"] = float(importe) or None
        rows.append(r)
    svc.adjuntar_grupo_partidas(rows)
    return rows


MH_68_69_70 = [
    ("INV HY3821-26-1 ( MH 68 )", 24300, 160400.78),
    ("INV HY3821-26-1 ( MH 69)", 23430, 0),
    ("INV HY3821-26-1 ( MH 70)", 24150, 0),
]


def _caso(rows, **kw):
    with patch("modules.informes.traza.foto_stock_buena", return_value=FOTO):
        return vig.kilos_sin_plata(rows=rows, **kw)


# ── Kilos sin plata ─────────────────────────────────────────────────────────
def test_dice_cuantos_kilos_quedaron_afuera_del_divisor():
    c = _caso(_filas(MH_68_69_70))
    assert c is not None
    assert c["kg_sin_costo"] == 47580.0
    assert c["kg_con_costo"] == 24300.0
    assert c["kg_recibidos"] == 71880.0
    assert c["us_cargados"] == 160400.78
    assert [i["codigo"] for i in c["importaciones"]] == ["MH 70", "MH 69"]


def test_dice_cuanta_plata_esta_apoyada_en_la_tarifa_incompleta():
    """El número que hace que uno vaya a mirar. Contra el movimiento real de la
    utilidad ese día (172.881) la estimación da 171.400: 0,9 % abajo."""
    ef = _caso(_filas(MH_68_69_70))["efecto"]
    assert ef["tarifa"] == 3.0013
    assert ef["tarifa_si_entraran"] == 2.9261      # 3,0013 − 0,0752
    assert ef["kg_stock"] == 2_280_000.0
    assert 168_000 <= ef["us"] <= 176_000
    assert abs(ef["us"] - 172_881) / 172_881 < 0.02


def test_el_mismo_dia_que_llegan_no_avisa():
    """Los kilos entran con la recepción y la plata la crea la conversión
    después (8, 27 y 76 minutos el 31/07). Avisar dentro del día es avisar del
    estado normal de una importación recién llegada."""
    assert _caso(_filas(MH_68_69_70, dias_atras=0)) is None


def test_al_dia_siguiente_si():
    assert _caso(_filas(MH_68_69_70, dias_atras=1)) is not None


def test_poquitos_kilos_no_avisan():
    """3.000 kg mueven la tarifa 0,005 y el stock ~10.000: no alcanza para
    gastarle atención a nadie."""
    assert _caso(_filas([
        ("INV X-1 ( MH 80 )", 24300, 80000.0),
        ("INV X-1 ( MH 81)", 3000, 0),
    ])) is None


def test_si_esta_todo_cargado_no_avisa():
    assert _caso(_filas([
        ("INV X-1 ( MH 80 )", 24300, 80000.0),
        ("INV X-1 ( MH 81)", 23430, 77000.0),
    ])) is None


def test_sin_foto_de_la_traza_el_aviso_igual_sale_sin_el_efecto():
    with patch("modules.informes.traza.foto_stock_buena", return_value=None):
        c = vig.kilos_sin_plata(rows=_filas(MH_68_69_70))
    assert c is not None and c["kg_sin_costo"] == 47580.0
    assert c["efecto"] is None


def test_si_la_foto_explota_no_rompe():
    with patch("modules.informes.traza.foto_stock_buena",
               side_effect=RuntimeError("db")):
        assert vig.kilos_sin_plata(rows=_filas(MH_68_69_70))["efecto"] is None


def test_si_no_se_puede_leer_no_inventa():
    with patch("modules.importaciones.service.importaciones_con_cruce",
               side_effect=RuntimeError("asinfo caido")):
        assert vig.kilos_sin_plata() is None


def test_los_umbrales_se_pueden_mover(monkeypatch):
    monkeypatch.delenv("HILADO_SIN_COSTO_DIAS", raising=False)
    monkeypatch.delenv("HILADO_SIN_COSTO_KG", raising=False)
    assert vig._int_env("HILADO_SIN_COSTO_DIAS", vig.DIAS_SIN_COSTO_DEFAULT) == 1
    assert vig._int_env("HILADO_SIN_COSTO_KG", vig.KG_SIN_COSTO_DEFAULT) == 5000
    monkeypatch.setenv("HILADO_SIN_COSTO_KG", "20000")
    assert vig._int_env("HILADO_SIN_COSTO_KG", vig.KG_SIN_COSTO_DEFAULT) == 20000
    monkeypatch.setenv("HILADO_SIN_COSTO_KG", "nada")
    assert vig._int_env("HILADO_SIN_COSTO_KG", vig.KG_SIN_COSTO_DEFAULT) == 5000


# ── Partidas que el programa no pudo unir ───────────────────────────────────
def _partidas_a_destiempo(dias_atras=2):
    """AC 88 real: las dos mitades a 68 días y recibidas en meses distintos —
    el agrupador las descarta y deja el motivo escrito."""
    rec1 = str(today_ec() - timedelta(days=dias_atras))
    rows = []
    for i, (im, kg, rec) in enumerate([
        ("IM-0000801", 19812.48, rec1),
        ("IM-0000802", 19812.48, str(today_ec() - timedelta(days=dias_atras + 40))),
    ], start=1):
        nota = f"ACMT/EXP/2026-27/7682 ( AC 88 )-------{i}"
        r = {"im_numero": im, "nota": nota, "kg": kg,
             "fecha": str(today_ec() - timedelta(days=90)),
             "fecha_recepcion": rec, "prov_cod_asinfo": "AC", "recibida": True,
             "compra": None, "anticipo": None}
        c = parse_nota_importacion(nota)
        r.update(codigo=c.get("codigo"), prov=c.get("prov"),
                 numero=c.get("numero"), numero_hasta=c.get("numero_hasta"))
        rows.append(r)
    svc.adjuntar_grupo_partidas(rows)
    return rows


def test_avisa_que_no_pudo_unir_y_por_que():
    filas = _partidas_a_destiempo()
    assert all(f["grupo_aviso"] for f in filas)      # el agrupador ya lo dijo
    casos = vig.grupos_que_no_se_pudieron_unir(rows=filas)
    assert len(casos) == 1
    c = casos[0]
    assert c["codigo"] == "AC 88"
    assert c["ims"] == ["IM-0000801", "IM-0000802"]
    assert c["kg"] == 39624.96
    assert "meses distintos" in c["motivo"]


def test_un_grupo_que_si_se_unio_no_avisa():
    assert vig.grupos_que_no_se_pudieron_unir(rows=_filas(MH_68_69_70)) == []


def test_lo_viejo_no_entra():
    assert vig.grupos_que_no_se_pudieron_unir(
        rows=_partidas_a_destiempo(dias_atras=200)) == []
    assert len(vig.grupos_que_no_se_pudieron_unir(
        rows=_partidas_a_destiempo(dias_atras=200), techo=0)) == 1


def test_en_transito_no_se_mira():
    filas = _partidas_a_destiempo()
    for f in filas:
        f["recibida"] = False
    assert vig.grupos_que_no_se_pudieron_unir(rows=filas) == []


def test_sin_lectura_no_inventa():
    with patch("modules.importaciones.service.importaciones_con_cruce",
               side_effect=RuntimeError("down")):
        assert vig.grupos_que_no_se_pudieron_unir() == []


# ── Los dos avisos, en la campanita ─────────────────────────────────────────
def test_los_dos_avisos_salen_una_sola_vez():
    vig._ultima_corrida = 0.0
    caso = _caso(_filas(MH_68_69_70))
    nounir = vig.grupos_que_no_se_pudieron_unir(rows=_partidas_a_destiempo())
    vistos = []
    with patch.object(vig, "_leer_importaciones", return_value=[]), \
         patch.object(vig, "importaciones_fuera_de_banda", return_value=[]), \
         patch.object(vig, "facturas_con_plata_en_una_sola", return_value=[]), \
         patch.object(vig, "kilos_sin_plata", return_value=caso), \
         patch.object(vig, "grupos_que_no_se_pudieron_unir", return_value=nounir), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: vistos.append(kw) or True):
        out = vig.revisar_si_toca()
    assert out["avisados"] == 2
    assert out["kg_sin_costo"] == 47580.0 and out["sin_unir"] == 1

    a = next(x for x in vistos if x["clave"].startswith("hilado-sin-costo:"))
    assert a["clave"].endswith("IM-0000702+IM-0000703")
    assert "47,580 kg de hilado" in a["titulo"]
    assert "3.0013" in a["detalle"] and "2.9261" in a["detalle"]
    assert "2,280,000 kg de stock" in a["detalle"]
    assert "MH 70 (IM-0000703, 24,150 kg" in a["detalle"]
    assert a["url"] == "/importaciones"

    b = next(x for x in vistos if x["clave"].startswith("import-no-unidas:"))
    assert b["clave"] == "import-no-unidas:IM-0000801+IM-0000802"
    assert "AC 88" in b["titulo"] and "no pudo unir" in b["titulo"]
    assert "meses distintos" in b["detalle"]
    for x in (a, b):
        assert len(x["titulo"]) <= 200
        assert x["fuente"] == "importaciones" and x["nivel"] == "alerta"


def test_la_pantalla_de_auditoria_muestra_los_cuatro_chequeos():
    import inspect

    from modules.admin_dbase import import_sin_plata_view as v
    src = inspect.getsource(v.run)
    for k in ("casos", "facturas_repartidas", "kilos_sin_plata",
              "no_se_pudieron_unir"):
        assert k in src
