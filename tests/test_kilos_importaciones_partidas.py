"""Kilos de importación: partidas ---1/---2 y recargos con kg repetido.

TMT 2026-07-31. El `$/kg` del hilado revalúa 1,9 M de kg de stock: 0,1 US$/kg
son ~210.000 de utilidad. `compra.kg` estaba mal por los DOS lados a la vez:

  · de MÁS — los recargos (CAE/flete/seguro) que grabó el dBase repiten los kg
    de la fila principal: AC 15, AC 16 y AC 25 = 70.354 kg contados dos veces;
  · de MENOS — las importaciones partidas llevan la mitad: AC 25 48.988 reales
    contra 24.494 en PC, AC 19 22.354/10.990, MH 66 46.860/23.430 = 70.570 kg.

Se cancelan hasta 215 kg. Arreglar uno solo movía el stock ±210.000 — por eso
el arreglo es UNO (kg_hilado_mes reemplaza el SUM crudo) y no dos.

La regla de agrupación exige LAS TRES cosas: mismo prov_cod_asinfo, misma fecha
exacta y misma nota base. Los tests de AC 58 y AC 5 son los que mataron las dos
reglas anteriores; si alguno se pone en verde aflojando la regla, el $/kg de
julio cae de 3,0271 a 2,3428 y el stock baja 284.772.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from modules.importaciones import service as isvc


def _con_asinfo(imps, kgs):
    """Parcha Asinfo con las importaciones y kilos dados."""
    return (
        patch.object(isvc.asinfo_service, "importaciones_asinfo", return_value=imps),
        patch.object(isvc.asinfo_service, "importaciones_kg", return_value=kgs),
    )


def _grupos(imps, kgs):
    """Corre la agrupación como la corre el código real y devuelve las filas."""
    from concepto_parser import parse_nota_importacion
    filas = [dict(r) for r in imps]
    for r in filas:
        r["kg"] = kgs.get(r["im_numero"])
        c = parse_nota_importacion(r.get("nota"))
        r.setdefault("prov", c.get("prov"))
        r.setdefault("numero", c.get("numero"))
        r.setdefault("numero_hasta", c.get("numero_hasta"))
    isvc.adjuntar_grupo_partidas(filas)
    return filas


# ── La nota: base y nº de partida ────────────────────────────────────────────
def test_partida_de_saca_codigo_y_sufijo():
    # Las seis notaciones reales que usa Asinfo caen todas en la misma base.
    assert isvc._nota_base("AYF02649 ( AI 11) ---1") == "AYF02649"
    assert isvc._nota_base("AYF02653 ( AI 15 ) ----2") == "AYF02653"
    assert isvc._nota_base("ACMT/EXP/2026-27/7682 ( AC 88)-------2") == "ACMT/EXP/2026-27/7682"
    assert isvc._nota_base("ACMT/EXP/2026-27/7914 (--1--)  (AC 19)") == "ACMT/EXP/2026-27/7914"
    assert isvc._nota_base("HY3087-26-1 ( MH 66 -67)--2") == "HY3087-26-1"
    assert isvc._nota_base("MTGE3753--- 2") == "MTGE3753"
    assert isvc._partida_de("AYF02649---1 ( AI 11)") == ("AYF02649", 1)


# ── 5. AI 11 SÍ se agrupa (misma fecha, misma nota base, sufijos 1 y 2) ──────
def test_ai_11_se_agrupa_y_suma_los_dos_kilos():
    imps = [
        {"im_numero": "IM-700", "fecha": "2026-07-02", "fecha_recepcion": "2026-07-08",
         "recibida": True, "prov_cod_asinfo": "AYF", "nota": "AYF02649---1 ( AI 11)"},
        {"im_numero": "IM-701", "fecha": "2026-07-02", "fecha_recepcion": "2026-07-08",
         "recibida": True, "prov_cod_asinfo": "AYF", "nota": "AYF02649---2 ( AI 11)"},
    ]
    filas = _grupos(imps, {"IM-700": 22564.78, "IM-701": 22564.78})
    assert filas[0]["grupo_id"] == filas[1]["grupo_id"] == "IM-700+IM-701"
    assert filas[0]["grupo_kg"] == filas[1]["grupo_kg"] == 45129.56
    assert filas[0]["grupo_ims"] == ["IM-700", "IM-701"]
    assert filas[0]["grupo_completo"] is True


# ── 3. AC 58 NO se agrupa — EL CASO QUE MATÓ LA REGLA ORIGINAL ──────────────
def test_ac_58_no_se_agrupa_mismo_prov_mismo_numero_mismo_anio():
    """Mismo proveedor, mismo nº, mismo año — pero fechas y notas distintas.
    No son mitades de nada: son dos campañas. Agruparlas les sumaba los kilos
    de febrero a la compra de julio."""
    imps = [
        {"im_numero": "IM-622", "fecha": "2026-07-20", "recibida": True,
         "prov_cod_asinfo": "ACMT", "nota": "ACMT/EXP/2026-27/8368 ( AC 58)"},
        {"im_numero": "IM-527", "fecha": "2026-02-11", "recibida": True,
         "prov_cod_asinfo": "ACMT", "nota": "INV HY336-26-1 ( AC 58)"},
    ]
    filas = _grupos(imps, {"IM-622": 24000.0, "IM-527": 20000.0})
    assert filas[0]["grupo_id"] != filas[1]["grupo_id"]
    assert filas[0]["grupo_kg"] == 24000.0
    assert filas[1]["grupo_kg"] == 20000.0


# ── 4. AC 5: dos embarques de la misma campaña, a 10 meses ──────────────────
def test_no_se_agrupa_misma_nota_base_pero_otra_fecha():
    """La nota base coincide y hasta hay sufijos ---1/---2, pero las fechas son
    distintas. La fecha exacta es lo que separa una partida de dos embarques."""
    imps = [
        {"im_numero": "IM-300", "fecha": "2025-09-15", "recibida": True,
         "prov_cod_asinfo": "ACMT", "nota": "ACMT/EXP/2025/1000---1 ( AC 5)"},
        {"im_numero": "IM-480", "fecha": "2026-07-15", "recibida": True,
         "prov_cod_asinfo": "ACMT", "nota": "ACMT/EXP/2025/1000---2 ( AC 5)"},
    ]
    filas = _grupos(imps, {"IM-300": 10000.0, "IM-480": 12000.0})
    assert filas[0]["grupo_id"] != filas[1]["grupo_id"]
    assert filas[0]["grupo_kg"] == 10000.0


def test_no_se_agrupa_otro_proveedor():
    imps = [
        {"im_numero": "IM-800", "fecha": "2026-07-02", "recibida": True,
         "prov_cod_asinfo": "AYF", "nota": "X999---1 ( AI 11)"},
        {"im_numero": "IM-801", "fecha": "2026-07-02", "recibida": True,
         "prov_cod_asinfo": "MHUM", "nota": "X999---2 ( MH 66)"},
    ]
    filas = _grupos(imps, {"IM-800": 100.0, "IM-801": 200.0})
    assert filas[0]["grupo_kg"] == 100.0 and filas[1]["grupo_kg"] == 200.0


# ── AC 19: el ordinal va DENTRO del paréntesis y aun así agrupa ────────────
def test_ac_19_agrupa_aunque_el_ordinal_este_en_el_parentesis():
    """Medido: "ACMT/EXP/2026-27/7914 (--1--)  (AC 19)" y "(--2--) ( AC 19)".
    11.363,72 + 10.990,42 = 22.354,14, que es exactamente lo que dice el dBase.
    Exigir el sufijo ---N (la regla anterior) perdía estos 11.000 kg."""
    imps = [
        {"im_numero": "IM-0000550", "fecha": "2026-04-16", "fecha_recepcion": "2026-07-08",
         "recibida": True, "prov_cod_asinfo": "AC",
         "nota": "ACMT/EXP/2026-27/7914 (--1--)  (AC 19)"},
        {"im_numero": "IM-0000551", "fecha": "2026-04-16", "fecha_recepcion": "2026-07-08",
         "recibida": True, "prov_cod_asinfo": "AC",
         "nota": "ACMT/EXP/2026-27/7914 (--2--) ( AC 19)"},
    ]
    filas = _grupos(imps, {"IM-0000550": 11363.72, "IM-0000551": 10990.42})
    assert filas[0]["grupo_kg"] == 22354.14
    assert filas[0]["grupo_completo"] is True


# ── MH 68/69/70: UNA factura, TRES importaciones distintas ─────────────────
def test_una_factura_con_tres_codigos_distintos_no_es_una_partida():
    """Medido: "INV HY3821-26" cubre MH 68, MH 69 y MH 70, mismo día, mismo
    proveedor. NO son mitades: agruparlas daba 71.880 kg donde van 24.300.
    El código del programa es lo único que las separa."""
    imps = [
        {"im_numero": "IM-0000617", "fecha": "2026-07-20", "recibida": False,
         "prov_cod_asinfo": "EXT0059", "nota": "INV HY3821-26 ( MH 68)"},
        {"im_numero": "IM-0000618", "fecha": "2026-07-20", "recibida": False,
         "prov_cod_asinfo": "EXT0059", "nota": "INV HY3821-26 ( MH 69)"},
        {"im_numero": "IM-0000619", "fecha": "2026-07-20", "recibida": False,
         "prov_cod_asinfo": "EXT0059", "nota": "INV HY3821-26 ( MH 70)"},
    ]
    filas = _grupos(imps, {"IM-0000617": 24300.0, "IM-0000618": 23430.0,
                           "IM-0000619": 24150.0})
    assert [f["grupo_kg"] for f in filas] == [24300.0, 23430.0, 24150.0]


# ── Ventana de fechas: dos campañas que reusan el número de factura ────────
def test_fuera_de_la_ventana_no_agrupa():
    """El único caso a más de 68 días en toda la historia son 366 — y no es una
    partida."""
    imps = [
        {"im_numero": "IM-A", "fecha": "2023-08-20", "fecha_recepcion": "2024-10-07",
         "recibida": True, "prov_cod_asinfo": "EXT0017", "nota": "SG-INV-2024 ( MH 9) --1"},
        {"im_numero": "IM-B", "fecha": "2024-08-20", "fecha_recepcion": "2024-10-08",
         "recibida": True, "prov_cod_asinfo": "EXT0017", "nota": "SG-INV-2024 ( MH 9) --2"},
    ]
    filas = _grupos(imps, {"IM-A": 23430.0, "IM-B": 24300.0})
    assert filas[0]["grupo_kg"] == 23430.0
    assert "días" in filas[0]["grupo_aviso"]


# ── Recibidas en meses distintos: el número que alimentan es MENSUAL ───────
def test_mitades_recibidas_en_meses_distintos_no_agrupan():
    imps = [
        {"im_numero": "IM-C", "fecha": "2026-04-01", "fecha_recepcion": "2026-06-30",
         "recibida": True, "prov_cod_asinfo": "AC", "nota": "ACMT/1 ( AC 40) ---1"},
        {"im_numero": "IM-D", "fecha": "2026-04-02", "fecha_recepcion": "2026-07-01",
         "recibida": True, "prov_cod_asinfo": "AC", "nota": "ACMT/1 ( AC 40) ---2"},
    ]
    filas = _grupos(imps, {"IM-C": 10000.0, "IM-D": 12000.0})
    assert filas[0]["grupo_kg"] == 10000.0
    assert "meses distintos" in filas[0]["grupo_aviso"]


# ── 9. Un miembro sin kg ⇒ grupo incompleto ────────────────────────────────
def test_miembro_sin_kg_no_suma_parcial():
    """Una suma parcial que cambia en cada corrida es PEOR que el número de hoy:
    la mitad podría quedar fuera del tope de filas que devuelve Asinfo."""
    imps = [
        {"im_numero": "IM-920", "fecha": "2026-07-02", "recibida": True,
         "prov_cod_asinfo": "AYF", "nota": "AYF02649---1 ( AI 11)"},
        {"im_numero": "IM-921", "fecha": "2026-07-02", "recibida": True,
         "prov_cod_asinfo": "AYF", "nota": "AYF02649---2 ( AI 11)"},
    ]
    filas = _grupos(imps, {"IM-920": 100.0})  # a la ---2 le falta el kg
    assert filas[0]["grupo_kg"] == 100.0
    assert filas[0]["grupo_completo"] is False
    assert "incompleto" in filas[0]["grupo_aviso"]


# ── AC 25 / MH 66-67: el RANGO es el código compartido, no un peligro ──────
def test_el_rango_en_la_nota_no_descalifica():
    """Medido: AC 25 son IM-0000548 e IM-0000549, las DOS "AC 25-26".
    24.492,24 + 24.494,24 = 48.986,48, contra los 48.988 del dBase.
    Descalificar por rango (la regla anterior) mataba justo el caso bueno."""
    imps = [
        {"im_numero": "IM-0000548", "fecha": "2026-04-15", "fecha_recepcion": "2026-06-18",
         "recibida": True, "prov_cod_asinfo": "AC",
         "nota": "ACMT/EXP/2026-27/7918    (AC 25-26)---1"},
        {"im_numero": "IM-0000549", "fecha": "2026-04-15", "fecha_recepcion": "2026-06-18",
         "recibida": True, "prov_cod_asinfo": "AC",
         "nota": "ACMT/EXP/2026-27/7918    (AC 25-26)---2"},
    ]
    filas = _grupos(imps, {"IM-0000548": 24492.24, "IM-0000549": 24494.24})
    assert filas[0]["grupo_kg"] == 48986.48
    assert filas[0]["grupo_completo"] is True


def test_ac_88_agrupa_a_68_dias():
    """Asinfo fecha los dos documentos en días distintos: 2026-01-15 y
    2026-03-24, mismos 19.812,48 kg cada uno, recibidos los dos el 28/03.
    Exigir "misma fecha exacta" perdía la mitad."""
    imps = [
        {"im_numero": "IM-0000524", "fecha": "2026-01-15", "fecha_recepcion": "2026-03-28",
         "recibida": True, "prov_cod_asinfo": "AC",
         "nota": "ACMT/EXP/2026-27/7682 ( AC 88) ----1"},
        {"im_numero": "IM-0000535", "fecha": "2026-03-24", "fecha_recepcion": "2026-03-28",
         "recibida": True, "prov_cod_asinfo": "AC",
         "nota": "ACMT/EXP/2026-27/7682 ( AC 88)-------2"},
    ]
    filas = _grupos(imps, {"IM-0000524": 19812.48, "IM-0000535": 19812.48})
    assert filas[0]["grupo_kg"] == 39624.96


def test_mas_de_tres_partidas_no_suma():
    imps = [
        {"im_numero": f"IM-{i}", "fecha": "2026-07-02", "fecha_recepcion": "2026-07-08",
         "recibida": True, "prov_cod_asinfo": "AYF", "nota": f"AYF1 ( AI 11) ---{i}"}
        for i in (1, 2, 3, 4)
    ]
    filas = _grupos(imps, {f"IM-{i}": 100.0 for i in (1, 2, 3, 4)})
    assert filas[0]["grupo_kg"] == 100.0
    assert "máx 3" in filas[0]["grupo_aviso"]


# ── 15. Idempotencia sobre filas cacheadas ─────────────────────────────────
def test_agrupar_dos_veces_da_el_mismo_kg():
    """`importaciones_asinfo` devuelve LAS MISMAS filas dentro del TTL de 5
    min. Una pasada que acumulara sobre grupo_kg lo duplicaría en la segunda
    visita — y nadie lo vería hasta que la utilidad se moviera sola."""
    imps = [
        {"im_numero": "IM-940", "fecha": "2026-07-02", "recibida": True,
         "prov_cod_asinfo": "AYF", "nota": "AYF02649---1 ( AI 11)"},
        {"im_numero": "IM-941", "fecha": "2026-07-02", "recibida": True,
         "prov_cod_asinfo": "AYF", "nota": "AYF02649---2 ( AI 11)"},
    ]
    filas = _grupos(imps, {"IM-940": 22564.78, "IM-941": 22564.78})
    isvc.adjuntar_grupo_partidas(filas)
    isvc.adjuntar_grupo_partidas(filas)
    assert filas[0]["grupo_kg"] == 45129.56


# ── kg_hilado_mes: el reemplazo ────────────────────────────────────────────
_IMPS_PARTIDA = [
    {"im_numero": "IM-950", "fecha": "2026-07-02", "fecha_recepcion": "2026-07-08",
     "recibida": True, "prov_cod_asinfo": "ACMT", "nota": "ACMT/1---1 ( AC 25)"},
    {"im_numero": "IM-951", "fecha": "2026-07-02", "fecha_recepcion": "2026-07-08",
     "recibida": True, "prov_cod_asinfo": "ACMT", "nota": "ACMT/1---2 ( AC 25)"},
]
_KG_PARTIDA = {"IM-950": 24494.0, "IM-951": 24494.0}


def test_kg_hilado_mes_usa_los_kilos_del_grupo_no_los_de_la_compra():
    """El caso AC 25: el dBase dice 48.988 y la compra de PC trae 24.494."""
    compras = [{"prov": "AC", "ref": 25, "fecha": date(2026, 7, 9),
                "kg": 24494.0, "importe": 148000.0}]
    p1, p2 = _con_asinfo(_IMPS_PARTIDA, _KG_PARTIDA)
    with p1, p2:
        out = isvc.kg_hilado_mes(compras)
    assert out["disponible"] is True
    assert out["kg"] == 48988.0
    assert out["kg_compra_ignorado"] == 24494.0


def test_kg_hilado_mes_no_cuenta_dos_veces_el_recargo():
    """AC 15/16/25: el recargo de CAE repite los kg de la principal. Con el SUM
    crudo eso eran 70.354 kg de más en julio."""
    compras = [
        {"prov": "AC", "ref": 25, "fecha": date(2026, 7, 9),
         "kg": 24494.0, "importe": 132500.0},
        {"prov": "AC", "ref": 25, "fecha": date(2026, 7, 11),   # recargo CAE
         "kg": 24494.0, "importe": 15441.06},
    ]
    p1, p2 = _con_asinfo(_IMPS_PARTIDA, _KG_PARTIDA)
    with p1, p2:
        out = isvc.kg_hilado_mes(compras)
    assert out["kg"] == 48988.0          # el grupo UNA vez, no 4 × 24.494
    assert out["grupos"] == 1


# ── 11. Dedup por grupo, no por im_numero ─────────────────────────────────
def test_dedup_por_grupo_no_por_documento():
    """El doble conteo que aparece JUSTO cuando el arreglo empieza a funcionar:
    dos compras que caen en mitades distintas del mismo grupo. Con dedup por
    im_numero cada una sumaría los 48.988 del grupo entero."""
    compras = [
        {"prov": "AC", "ref": 25, "fecha": date(2026, 7, 2),
         "kg": 24494.0, "importe": 132500.0},
        {"prov": "AC", "ref": 25, "fecha": date(2026, 7, 3),
         "kg": 24494.0, "importe": 15441.06},
    ]
    p1, p2 = _con_asinfo(_IMPS_PARTIDA, _KG_PARTIDA)
    with p1, p2:
        out = isvc.kg_hilado_mes(compras)
    assert out["kg"] == 48988.0


def test_kg_hilado_mes_respeta_el_kg_de_las_compras_locales():
    """HY / EP cruzan por RUC, no tienen importación: su kg es el bueno."""
    compras = [
        {"prov": "AC", "ref": 25, "fecha": date(2026, 7, 9),
         "kg": 24494.0, "importe": 148000.0},
        {"prov": "HY", "ref": 37711, "fecha": date(2026, 7, 10),
         "kg": 5002.5, "importe": 13231.61},
    ]
    p1, p2 = _con_asinfo(_IMPS_PARTIDA, _KG_PARTIDA)
    with p1, p2:
        out = isvc.kg_hilado_mes(compras)
    assert out["kg"] == 48988.0 + 5002.5


def test_kg_hilado_mes_fail_soft_asinfo_caido():
    """Asinfo caído: el número no cambia respecto de hoy (SUM crudo) y el
    balance ya advierte. Nunca inventar kilos."""
    compras = [{"prov": "AC", "ref": 25, "fecha": date(2026, 7, 9),
                "kg": 24494.0, "importe": 148000.0}]
    with patch.object(isvc.asinfo_service, "importaciones_asinfo",
                      side_effect=RuntimeError("down")):
        out = isvc.kg_hilado_mes(compras)
    assert out["disponible"] is False
    assert out["kg"] == 24494.0


# ── 10. Banda económica: avisa, no rompe ──────────────────────────────────
def test_banda_avisa_cuando_el_usd_kg_se_va_de_rango():
    """MH 66-67 a 2,2385 y las de 4,477/6,546 de julio: el balance tiene que
    decirlo solo. Antes se encontraban ordenando la lista a mano."""
    compras = [{"prov": "AC", "ref": 25, "fecha": date(2026, 7, 9),
                "kg": 24494.0, "importe": 300000.0}]  # 6,12 $/kg sobre 48.988
    p1, p2 = _con_asinfo(_IMPS_PARTIDA, _KG_PARTIDA)
    with p1, p2:
        out = isvc.kg_hilado_mes(compras)
    assert out["kg"] == 48988.0            # la banda NO cambia los kilos
    assert len(out["fuera_de_banda"]) == 1
    assert out["fuera_de_banda"][0]["codigo"] == "AC 25"
    assert out["fuera_de_banda"][0]["usd_kg"] > 6.0


def test_banda_no_avisa_dentro_de_rango():
    compras = [{"prov": "AC", "ref": 25, "fecha": date(2026, 7, 9),
                "kg": 24494.0, "importe": 148000.0}]  # 3,02 $/kg
    p1, p2 = _con_asinfo(_IMPS_PARTIDA, _KG_PARTIDA)
    with p1, p2:
        out = isvc.kg_hilado_mes(compras)
    assert out["fuera_de_banda"] == []


def test_banda_mira_el_grupo_entero_no_la_compra_suelta():
    """El recargo de CAE solo son 0,32 $/kg. Mirado por fila, dispara una
    alarma falsa cada vez que se carga un CAE."""
    compras = [
        {"prov": "AC", "ref": 25, "fecha": date(2026, 7, 9),
         "kg": 24494.0, "importe": 132558.94},
        {"prov": "AC", "ref": 25, "fecha": date(2026, 7, 11),
         "kg": 0, "importe": 15441.06},
    ]
    p1, p2 = _con_asinfo(_IMPS_PARTIDA, _KG_PARTIDA)
    with p1, p2:
        out = isvc.kg_hilado_mes(compras)
    assert out["fuera_de_banda"] == []     # 148.000 / 48.988 = 3,02


# ── 16. Los agregados mensuales NO leen grupo_kg ──────────────────────────
def test_los_agregados_mensuales_siguen_sumando_fila_por_fila():
    """costo_hilado_recibido_mes ya suma bien: recorre las dos mitades y suma
    el kg de cada una. Si además leyera grupo_kg, contaría el doble."""
    p1, p2 = _con_asinfo(_IMPS_PARTIDA, _KG_PARTIDA)
    with p1, p2, patch.object(isvc, "_buscar_compras", return_value=[]), \
            patch.object(isvc, "_buscar_anticipos", return_value=[]), \
            patch("modules.importaciones.pago.estados_por_im", return_value={}), \
            patch("modules.importaciones.pago.movimientos_por_im", return_value={}):
        out = isvc.costo_hilado_recibido_mes(2026, 7)
    assert out["kg"] == 48988.0            # 24.494 + 24.494, no 4 × 24.494


# ── 2 (paso 2). promedio_hilado_usd_kg: la mitad sin plata aporta sus kilos ──
def test_promedio_no_pierde_los_kilos_de_la_mitad_sin_importe():
    """La partida ---1 no tiene importe_programa (el costo vive en la ---2).
    El `continue` salteaba la fila entera: se perdían sus kilos y el $/kg salía
    al doble."""
    rows = [
        {"recibida": True, "kg": 100.0, "importe_programa": None,
         "prov": "AI", "numero": 11, "fecha": "2026-07-02"},
        {"recibida": True, "kg": 100.0, "importe_programa": 600.0,
         "prov": "AI", "numero": 11, "fecha": "2026-07-02"},
    ]
    with patch.object(isvc, "importaciones_con_cruce", return_value=rows):
        assert isvc.promedio_hilado_usd_kg() == 3.0   # 600 / 200, no 600 / 100


# ── 13. autobap: la partida no es ambigüedad ──────────────────────────────
def test_grupos_plausibles_cuenta_grupos_no_documentos():
    cands = [
        {"im_numero": "IM-950", "fecha": "2026-07-02", "grupo_id": "IM-950+IM-951"},
        {"im_numero": "IM-951", "fecha": "2026-07-02", "grupo_id": "IM-950+IM-951"},
    ]
    assert len(isvc.grupos_plausibles(cands, date(2026, 7, 9))) == 1


def test_grupos_plausibles_sigue_viendo_la_ambiguedad_real():
    """AC 58: dos campañas distintas, las dos a tiro. Eso sí es ambiguo y
    tiene que seguir frenando la conversión automática."""
    cands = [
        {"im_numero": "IM-622", "fecha": "2026-07-20", "grupo_id": "IM-622"},
        {"im_numero": "IM-527", "fecha": "2026-02-11", "grupo_id": "IM-527"},
    ]
    assert len(isvc.grupos_plausibles(cands, date(2026, 7, 25))) == 2


# ── El interruptor: sale en dos deploys ───────────────────────────────────
def test_interruptor_legacy_no_mueve_el_numero(monkeypatch):
    """Deploy 1: se calcula el número nuevo y se publica, pero el balance sigue
    usando el de siempre. Así se mira el antes y el después el mismo día sin
    que la utilidad se mueva mientras tanto."""
    from modules.informes import queries as q
    monkeypatch.delenv("HILADO_KG_FUENTE", raising=False)
    assert q._hilado_kg_fuente() == q._HILADO_KG_FUENTE_DEFAULT
    monkeypatch.setenv("HILADO_KG_FUENTE", "grupo")
    assert q._hilado_kg_fuente() == "grupo"
    monkeypatch.setenv("HILADO_KG_FUENTE", "cualquier-cosa")
    assert q._hilado_kg_fuente() == q._HILADO_KG_FUENTE_DEFAULT


# ── La banda NO sale en rojo en Resultados ────────────────────────────────
def test_la_banda_no_ensucia_las_advertencias_del_balance():
    """TMT 2026-07-31: *"me sacás todo esto en rojo de Resultados"*. Tres
    carteles arriba del Informe todos los días tapan la pantalla con la que se
    decide. El dato NO se pierde: queda en diagnostico.hilado_fuera_de_banda."""
    import inspect

    from modules.informes import queries as q
    src = inspect.getsource(q.informe_balance)
    i = src.index("ALARMA de BANDA")
    # Desde el comentario de la banda hasta el fin del bloque no puede haber
    # ningún advertencias.append: si alguien lo vuelve a poner, esto se cae.
    bloque = src[i:src.index('diagnostico["hilado_fuera_de_banda"]', i)]
    assert "advertencias.append" not in bloque
    assert 'diagnostico["hilado_fuera_de_banda"]' in src
