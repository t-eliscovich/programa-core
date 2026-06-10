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
