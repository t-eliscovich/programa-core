"""Contract tests — stock por etapa VIVO (fórmula PRG), pedido Andrés 2026-06-10.

En el dBase, tipear el paso de tejeduría/tintorería SUBE la utilidad: los kg
se revalorizan al cambiar de etapa (tejido = hilado+0,50; terminado =
tejido+1,70). INFORMES.PRG L313-315:

    HI = HI0 + KM − KH      (KH = KK/(1−DESK/100), DESK=0,5)
    TJ = TJ0 + KK − KT      (KT = compras_T_ext + KTINT − KSTI)
    PF = PF0 + KR − KV

con HI0/TJ0/PF0 = fila del mes ANTERIOR de iniciales (la fila del mes en
curso es un caché que el dBase reescribe en cada corrida — verificado contra
HISTORIA.DBF: iniciales[jun] = STOCK 09/06, iniciales[may] = STOCK 31/05).

PC tenía hilado/tejido congelados al caché → los pasos no movían la utilidad.
NO confundir con resumen_stock() (intento revertido 78fbff7: doble conteo).
"""
from __future__ import annotations

import inspect


def _src():
    from modules.informes import queries as iq
    return inspect.getsource(iq.informe_balance)


def test_hilado_tejido_usan_formula_viva_no_cache():
    src = _src()
    # HI vivo (del bloque MAT.PR.) alimenta el panel STOCK
    assert "h_hilado = max(0.0, HI)" in src, (
        "h_hilado volvió al caché/snapshot — los pasos de tejeduría dejan "
        "de mover la utilidad (pedido Andrés 2026-06-10)"
    )
    # TJ vivo con la resta de KT (paso a tintura)
    assert "KT_stock" in src and "_tj0_prev + KK - KT_stock" in src
    # y ya NO se lee el caché congelado para el panel
    assert 'h_hilado = float(hist.get("hilado")' not in src


def test_tj0_e_hi0_vienen_del_mes_anterior():
    src = _src()
    assert 'tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "hilado")' in src
    assert 'tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "tejido")' in src


def test_kt_incluye_externos_y_resta_servicios():
    src = _src()
    assert "compras_tipo_t_externos_mes()" in src
    assert "tinto_kg_servicios_mes()" in src


def test_whitelist_tarifa_prev_tiene_columnas_stock():
    from modules.informes import queries as iq
    for col in ("hilado", "tejido", "terminado"):
        assert col in iq._TARIFA_COLS_PREV


def test_sync_tinto_dbase_gana_absorbe_pc_carga():
    """El sync absorbe partidas pc-carga que el DBF trae igual (fecha+cod+kg)
    — permite cargar la planilla a mano sin doble conteo. TMT 2026-06-10."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "import_dbf.py"
    spec = importlib.util.spec_from_file_location("_imp_dbf_tinto_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = inspect.getsource(mod.import_one)
    assert "scintela.tinto" in src and "pc-carga" in src, (
        "el sync perdió el dedupe dBase-gana de tinto — la planilla manual "
        "se duplica con el próximo sync"
    )


def test_sync_tinto_preserva_manual_kg_edit_mes_corriente():
    """'Deberíamos contarlo, para eso lo creé' (dueña 2026-06-10): los
    ajustes manual-kg-edit cuentan y el sync los preserva el mes corriente;
    los absorbe solo cuando el DBF trae filas de la misma fecha."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "import_dbf.py"
    spec = importlib.util.spec_from_file_location("_imp_dbf_kge_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    where_sql, _ = mod.TABLE_MAP["TINTO.DBF"]["delete_where"]
    assert "manual-kg-edit" in where_sql, (
        "el sync volvió a borrar los ajustes manual-kg-edit siempre"
    )
    src = inspect.getsource(mod.import_one)
    assert "manual-kg-edit" in src, "falta el dBase-gana por fecha para los ajustes"


def test_editar_kg_fetch_manda_csrf():
    """El POST de editar-KG debe mandar X-CSRFToken (sin eso: 400 silencioso,
    el ajuste nunca se guarda — bug encontrado 2026-06-10)."""
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / "modules" / "comparativa_tintoreria"
           / "templates" / "comparativa_tintoreria" / "index.html").read_text()
    i = tpl.find("editar_kg_dbase")
    assert i > 0 and "X-CSRFToken" in tpl[i:i+600], (
        "el fetch de editar-kg no manda CSRF token"
    )


def test_vqx_vivo_formula_prg():
    """Stock Quí vivo (PRG L322: VQX = VQ0 + VQQ − ITIN), no el caché del
    snapshot de historia ('stock químicos seguro muy mal' — dueña 2026-06-10)."""
    src = _src()
    assert 'tarifa_iniciales_mes_anterior(mesnum_actual, yy_actual, "vq")' in src
    assert "_vqq_mes" in src and "- ITIN" in src
    from modules.informes import queries as iq
    assert "vq" in iq._TARIFA_COLS_PREV


def test_editar_kg_estima_desperdicio_e_importe():
    """El ajuste manual imita la planilla del dBase: kgn con rinde promedio
    (no kgn=kg) e importe con $/kg promedio (resta químicos de Stock Quí)."""
    import inspect as _i

    from modules.comparativa_tintoreria import views as v
    src = _i.getsource(v.editar_kg_dbase)
    assert "rinde" in src and "imp_est" in src and "kgn_est" in src
    assert "VALUES (%s, 'MAN', 'AJUSTE MANUAL', %s, %s, 0, '', %s)" not in src


def test_dbase_compare_existe_y_es_solo_lectura():
    """Comparador sistemático (pedido dueña 2026-06-10): 13 checks PRG vs PC
    + identidad de utilidad. NUNCA escribe (no INSERT/UPDATE/DELETE/tx)."""
    import inspect as _i

    from modules.admin_dbase import dbase_compare_view as dcv
    src = _i.getsource(dcv)
    for kw in ("INSERT INTO", "UPDATE scintela", "DELETE FROM", "db.tx("):
        assert kw not in src, f"dbase-compare contiene escritura: {kw}"
    for seccion in ("CAJA", "BANCOS", "CHEQUES", "FACTURAS", "ANTICIPOS",
                    "PASIVOS", "ACTIVOS", "RETIROS", "PRODUCCIÓN", "STOCK",
                    "QUÍMICOS", "PATANT", "UTILIDAD", "RESIDUO"):
        assert seccion in src, f"falta la sección {seccion}"


# ─────────────────────────────────────────────────────────────────────────
# dBase-compare 1 a 1 — "tenemos que ver 1 a 1 los MOVIMIENTOS, no los
# totales" (dueña 2026-06-11). Diffs multiset puros + wiring del reporte.
# ─────────────────────────────────────────────────────────────────────────

def test_dbase_compare_caja_linea_por_linea():
    """Caja reusa el patrón de bancos: multiset (fecha, |importe|) +
    saldo fin de día con ► donde el gap cambia."""
    import inspect as _i

    from modules.admin_dbase import dbase_compare_view as dcv
    src = _i.getsource(dcv)
    assert "caja_movs" in src and "_pc_movs_caja" in src
    assert 'saldos_fin_de_dia(d.get("caja_movs")' in src, (
        "el saldo fin de día de CAJA no usa el patrón de bancos"
    )


def test_dbase_compare_diff_cheques_flip_de_stat():
    """Multiset (cliente, importe, grupo VIVO Z123PD / NO-VIVO): un flip de
    stat (vivo en dBase, depositado en PC) aparece en AMBAS listas."""
    from modules.admin_dbase import dbase_compare_view as dcv
    db_rows = [{"cliente": "AAA", "importe": 100.0, "stat": "Z"},
               {"cliente": "BBB", "importe": 50.0, "stat": "B"}]
    pc_rows = [{"cliente": "AAA", "importe": 100.0, "stat": "B"},
               {"cliente": "BBB", "importe": 50.0, "stat": "B"}]
    r = dcv.diff_cheques(db_rows, pc_rows)
    assert [c["cliente"] for c in r["solo_dbase"]] == ["AAA"]
    assert [c["cliente"] for c in r["solo_pc"]] == ["AAA"]
    assert dcv._grupo_stat_cheque("Z") == "VIVO"
    assert dcv._grupo_stat_cheque("B") == "NO-VIVO"
    # match exacto no lista nada
    r2 = dcv.diff_cheques([{"cliente": "C", "importe": 1.0, "stat": "P"}],
                          [{"cliente": "C", "importe": 1.0, "stat": "D"}])
    assert not r2["solo_dbase"] and not r2["solo_pc"]  # P y D = mismo grupo VIVO


def test_dbase_compare_diff_anticipos_cta_importe():
    from modules.admin_dbase import dbase_compare_view as dcv
    r = dcv.diff_anticipos([{"cta": "AC", "importe": 10.0}],
                           [{"cta": "AC", "importe": 10.0}])
    assert not r["solo_dbase"] and not r["solo_pc"]
    r = dcv.diff_anticipos([{"cta": "AC", "importe": 10.0}],
                           [{"cta": "AC", "importe": 11.0}])
    assert len(r["solo_dbase"]) == 1 and len(r["solo_pc"]) == 1


def test_dbase_compare_facturas_pareo_por_numf_sri():
    """Vivas dBase (STAT $ 'ZA', NUMF>0) vs vivas PC que CUENTAN (sin
    asinfo-backfill), apareadas por N° SRI = últimos dígitos de
    numf_completo. Las NUMF=0 del dBase van aparte (no apareables)."""
    from modules.admin_dbase import dbase_compare_view as dcv
    assert dcv._numf_sri_pc({"numf_completo": "001-002-000174007"}) == 174007
    assert dcv._numf_sri_pc({"numf_completo": None, "numf": 55}) == 55
    dbf = [{"numf": 174007, "stat": "Z", "saldo": 100.0},
           {"numf": 0, "stat": "Z", "saldo": 7.0},        # sin numerar
           {"numf": 174010, "stat": "T", "saldo": 0.0}]    # no viva
    pc = [{"numf_completo": "001-002-000174007", "stat": "A",
           "usuario_crea": "asinfo-carga", "saldo": 100.0},
          {"numf_completo": "001-002-000174008", "stat": "Z",
           "usuario_crea": "asinfo-backfill", "saldo": 9.0},   # NO cuenta
          {"numf_completo": "001-002-000174009", "stat": "Z",
           "usuario_crea": "asinfo-carga", "saldo": 5.0}]
    r = dcv.diff_facturas_sri(dbf, pc)
    assert not r["solo_dbase"], "174007 debería aparear dBase↔PC por N° SRI"
    assert [dcv._numf_sri_pc(x) for x in r["solo_pc"]] == [174009]
    assert len(r["dbase_sin_numf"]) == 1


def test_dbase_compare_yy_asof_fecha_tarball():
    """Pasivos: el TOTP del DBF es de anoche y PC ya persistió las cuotas
    YY/RT de hoy → falso +32k. El reporte muestra TAMBIÉN el TOTP de PC
    as-of el mtime de POSDAT.DBF (mismas cuotas que el motor de posdat)."""
    from datetime import date

    from modules.admin_dbase import dbase_compare_view as dcv
    # 2026-06-08 = lunes, 10 = miércoles, 11 = jueves (días hábiles)
    rows = [{"cuota_diaria": 100.0, "baseline_date": date(2026, 6, 11)}]
    assert dcv.ajuste_yy_a_fecha(rows, date(2026, 6, 10)) == -100.0  # PC adelantado
    assert dcv.ajuste_yy_a_fecha(rows, date(2026, 6, 11)) == 0.0
    rows = [{"cuota_diaria": 100.0, "baseline_date": date(2026, 6, 8)}]
    assert dcv.ajuste_yy_a_fecha(rows, date(2026, 6, 10)) == 200.0   # persist atrasado
    import inspect as _i
    src = _i.getsource(dcv)
    assert "posdat_mtime" in src and "_resolver_cuotas" in src, (
        "el as-of tarball debe usar el MISMO motor de cuotas que posdat"
    )


# ─────────────────────────────────────────────────────────────────────────
# COSTOS DE TINTORERÍA multi-mes: los meses previos al mes en curso salen de
# formulas_app (scintela.tinto solo guarda el mes vigente del dBase).
# Pedido dueña 2026-07-07: la tabla tiene que mostrar MÁS de un mes.
# ─────────────────────────────────────────────────────────────────────────

def test_tinto_formulas_bajos_fuertes_por_mes_clasifica_y_agrupa():
    """La query de formulas_app clasifica Bajos/Fuertes con $/kg <= 0.4,
    agrupa por (yy, mm, tipo), suma kgn como kg, e ignora órdenes sin
    terminar (kgn <= 0)."""
    from datetime import date as _date
    from unittest.mock import patch

    from modules.comparativa_tintoreria import queries as q
    from modules.tintura.service import TintoEquivOrden

    fake = [
        # Bajos: 100/1000 crudo = 0.1 <= 0.4
        TintoEquivOrden(numero="1", fecha=_date(2026, 4, 3), fecha_terminado=None,
                        cod="A", color="c", categoria=None,
                        kg=1000.0, kgn=950.0, importe=100.0),
        # Fuertes: 800/1000 = 0.8 > 0.4
        TintoEquivOrden(numero="2", fecha=_date(2026, 4, 10), fecha_terminado=None,
                        cod="B", color="c", categoria=None,
                        kg=1000.0, kgn=900.0, importe=800.0),
        # otra Bajos en mayo
        TintoEquivOrden(numero="3", fecha=_date(2026, 5, 1), fecha_terminado=None,
                        cod="A", color="c", categoria=None,
                        kg=2000.0, kgn=1900.0, importe=200.0),
        # sin terminar → se ignora
        TintoEquivOrden(numero="4", fecha=_date(2026, 5, 2), fecha_terminado=None,
                        cod="A", color="c", categoria=None,
                        kg=500.0, kgn=0.0, importe=50.0),
    ]
    with patch("modules.tintura.service.tinto_equiv_formulas", return_value=fake):
        rows = q.tinto_formulas_bajos_fuertes_por_mes(_date(2026, 1, 1), _date(2026, 5, 31))

    by = {(r["yy"], r["mm"], r["tipo"]): r for r in rows}
    assert by[(2026, 4, "Bajos")]["kg"] == 950.0
    assert by[(2026, 4, "Bajos")]["importe"] == 100.0
    assert by[(2026, 4, "Fuertes")]["kg"] == 900.0
    assert by[(2026, 5, "Bajos")]["kg"] == 1900.0
    # la orden sin terminar (numero 4) no aporta
    assert (2026, 5, "Fuertes") not in by


def test_tinto_formulas_bajos_fuertes_por_mes_fail_soft():
    """Si el bridge rompe o formulas_app no está, devuelve [] sin romper."""
    from datetime import date as _date
    from unittest.mock import patch

    from modules.comparativa_tintoreria import queries as q

    with patch("modules.tintura.service.tinto_equiv_formulas",
               side_effect=RuntimeError("db down")):
        assert q.tinto_formulas_bajos_fuertes_por_mes(_date(2026, 1, 1), _date(2026, 12, 31)) == []


def test_build_tintoreria_mensual_rellena_meses_desde_formulas():
    """_build_tintoreria_mensual usa scintela.tinto donde existe y rellena
    los meses faltantes desde formulas_app, sin doblar el mes del dBase."""
    from unittest.mock import patch

    from modules.comparativa_tintoreria import views as v

    # scintela.tinto solo tiene julio (como en producción)
    tinto_rows = [
        {"yy": 2026, "mm": 7, "tipo": "Bajos", "kg": 100.0, "importe": 10.0},
        {"yy": 2026, "mm": 7, "tipo": "Fuertes", "kg": 200.0, "importe": 250.0},
    ]
    # formulas_app tiene abril, mayo y TAMBIÉN julio (que NO debe pisar al dBase)
    form_rows = [
        {"yy": 2026, "mm": 4, "tipo": "Bajos", "kg": 500.0, "importe": 50.0},
        {"yy": 2026, "mm": 5, "tipo": "Fuertes", "kg": 700.0, "importe": 600.0},
        {"yy": 2026, "mm": 7, "tipo": "Bajos", "kg": 9999.0, "importe": 9999.0},
    ]
    with patch("modules.comparativa_tintoreria.views.queries.tinto_bajos_fuertes_por_mes",
               return_value=tinto_rows), \
         patch("modules.comparativa_tintoreria.views.queries.tinto_formulas_bajos_fuertes_por_mes",
               return_value=form_rows), \
         patch("modules.comparativa_tintoreria.views.queries.gs_produccion_tintoreria_por_mes",
               return_value={}):
        data = v._build_tintoreria_mensual(2026, 7)

    labels = [f["label"] for f in data["filas"]]
    assert labels == ["04/2026", "05/2026", "07/2026"]  # 3 meses, ordenados
    julio = next(f for f in data["filas"] if f["label"] == "07/2026")
    # julio salió del dBase (kg total 300), NO del row falso de formulas (9999)
    assert julio["t_kg"] == 300.0
    abril = next(f for f in data["filas"] if f["label"] == "04/2026")
    assert abril["b_kg"] == 500.0
