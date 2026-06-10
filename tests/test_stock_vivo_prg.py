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
