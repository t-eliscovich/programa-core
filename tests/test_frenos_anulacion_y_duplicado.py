"""Los tres frenos del 05/08/2026 — nacidos de los 6 pendientes de conciliación.

Ese día la pantalla de conciliación mostraba 6 movimientos "según PC" que
nadie sabía con qué conciliar. La investigación (SQL sobre la base viva) mostró
que eran las cicatrices de tres agujeros del código:

1. **BAN +730**: el mismo cheque cargado dos veces con DOS SEMANAS de
   diferencia. El detector de duplicados sólo miraba cargas del mismo día.
   → FRENO 1: el alta avisa (mismo cliente + importe activo, ±30 días) y pide
   tildar «Es otro cheque» para guardar igual.
2. **MSS −1.100,93**: se anuló por "error de carga" un cheque cuyo depósito ya
   estaba conciliado contra un crédito REAL del extracto (28/07). El crédito
   del banco quedó explicado por plata que según PC no existe.
   → FRENO 2: anular por error de carga se bloquea si hay match vivo.
3. **CG3 +1.136,48 / ELF −301,96**: la ND compensatoria de una anulación y su
   depósito original quedaban SUELTOS en la conciliación — dos mitades que
   netean a cero y jamás van a estar en el extracto.
   → FRENO 3: si el depósito era de ese cheque solo, la anulación los deja
   emparejados como interno (reversible desde 'Deshacer conciliados').

Sin Postgres en CI, la lógica SQL se verifica inspeccionando el fuente
(patrón test_auditar_agrupa_por_batch.py).
"""
from __future__ import annotations

import inspect
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques import duplicados  # noqa: E402
from modules.cheques import queries as q  # noqa: E402
from modules.cheques import views as v  # noqa: E402
from modules.conciliacion import queries as cq  # noqa: E402

_TPL_NUEVO = os.path.join(
    _REPO_ROOT, "modules", "cheques", "templates", "cheques", "nuevo.html"
)


# ─── FRENO 1: aviso de duplicado en el alta ──────────────────────────────


def test_similares_activos_excluye_los_terminales():
    """Un cheque anulado (X) o terminado (T/R) no es un duplicado posible."""
    src = inspect.getsource(duplicados.similares_activos)
    assert "NOT IN ('X', 'T', 'R')" in src


def test_similares_activos_incluye_al_dbase():
    """BAN 730: la primera carga era del import del dBase. Si el filtro
    excluyera usuario_crea='dbf-import' (como hace el diagnóstico, con razón),
    el caso que motivó todo esto pasaría de largo otra vez."""
    src = inspect.getsource(duplicados.similares_activos)
    assert "dbf-import" not in src
    assert "usuario_crea" not in src.split("WHERE")[1].split("ORDER BY")[0]


def test_similares_activos_ventana_por_la_fecha_mas_reciente():
    """La ventana compara la fecha MÁS RECIENTE de la fila (GREATEST de
    fechad / fecha_recibido / fecha): un posdatado a futuro también avisa."""
    src = inspect.getsource(duplicados.similares_activos)
    assert "GREATEST(" in src
    assert "timedelta(days=dias)" in src


def test_similares_activos_sin_cliente_o_importe_no_consulta():
    capturado = {}

    def _fake_fetch_all(sql, params=None, conn=None):  # pragma: no cover
        capturado["sql"] = sql
        return []

    orig = duplicados.db.fetch_all
    duplicados.db.fetch_all = _fake_fetch_all
    try:
        assert duplicados.similares_activos("", 100.0) == []
        assert duplicados.similares_activos("BAN", None) == []
        assert "sql" not in capturado, "sin datos no debe tocar la base"
    finally:
        duplicados.db.fetch_all = orig


def test_similares_activos_normaliza_el_codigo():
    capturado = {}

    def _fake_fetch_all(sql, params=None, conn=None):
        capturado["params"] = params
        return [{"id_cheque": 1}]

    orig = duplicados.db.fetch_all
    duplicados.db.fetch_all = _fake_fetch_all
    try:
        filas = duplicados.similares_activos("  ban ", 730.0)
        assert filas == [{"id_cheque": 1}]
        assert capturado["params"][0] == "BAN"
        assert capturado["params"][1] == 730.0
    finally:
        duplicados.db.fetch_all = orig


def _src_nuevo() -> str:
    return inspect.getsource(v.nuevo)


def test_alta_pide_confirmacion_ante_posible_duplicado():
    src = _src_nuevo()
    assert "confirmar_no_duplicado" in src
    assert "similares_activos" in src
    assert "pedir_confirmar_duplicado" in src


def test_alta_solo_compara_contra_la_base_no_entre_bloques():
    """Dueña 04/08: dos cheques iguales en la MISMA cobranza son legítimos.
    El freno compara contra lo guardado; los importes repetidos dentro del
    propio form se saltean (un aviso por importe alcanza)."""
    src = _src_nuevo()
    i = src.index("FRENO 1")
    bloque = src[i : i + 3000]
    assert "_imps_vistos" in bloque
    assert "continue  # mismo guardado" in bloque


def test_alta_ignora_negativos_y_banco_95():
    src = _src_nuevo()
    i = src.index("FRENO 1")
    bloque = src[i : i + 3000]
    assert 'imp <= 0.005 or ch.get("no_banco") == 95' in bloque


def test_alta_el_aviso_no_puede_tumbar_la_cobranza():
    """Si la consulta falla, la carga sigue: el freno es un aviso, no una
    dependencia dura."""
    src = _src_nuevo()
    i = src.index("FRENO 1")
    bloque = src[i : i + 3000]
    assert "_sim = []  # el aviso nunca puede tumbar una cobranza" in bloque


def test_template_tiene_el_tilde_dentro_del_form():
    """El checkbox tiene que viajar en el POST: si quedara en el recuadro de
    errores (que está FUERA del <form>), no se enviaría nunca."""
    html = open(_TPL_NUEVO).read()
    assert 'name="confirmar_no_duplicado"' in html
    i_form = html.index('<form method="post" id="cheque-form"')
    i_chk = html.index('name="confirmar_no_duplicado"')
    assert i_chk > i_form
    i_endform = html.index("</form>", i_form)
    assert i_chk < i_endform
    assert "pedir_confirmar_duplicado" in html


# ─── FRENO 2: no se anula un cheque ya conciliado ────────────────────────


def _src_anular() -> str:
    return inspect.getsource(q.anular_por_error_de_carga)


def test_anular_bloquea_si_hay_match_vivo():
    src = _src_anular()
    i = src.index("FRENO 2")
    bloque = src[i : src.index("FRENO 3")]
    assert "estado = 'matched'" in bloque
    assert "deshecho_en IS NULL" in bloque, (
        "un match deshecho NO debe bloquear — la definición de vivo es la de banco_v2"
    )
    assert "raise ValueError" in bloque


def test_anular_chequea_antes_de_tocar_nada():
    """El bloqueo va ANTES de revertir aplicaciones y de compensar: si
    saltara después, el rollback dependería de que nadie haya commiteado."""
    src = _src_anular()
    assert src.index("FRENO 2") < src.index("chequesxfact WHERE id_cheque")
    assert src.index("FRENO 2") < src.index("insert_movimiento_bancario")


def test_anular_mira_las_transacciones_del_cheque():
    src = _src_anular()
    i = src.index("FRENO 2")
    bloque = src[i : src.index("FRENO 3")]
    assert "chequextransaccion" in bloque


# ─── FRENO 3: la anulación deja el par conciliado interno ────────────────


def test_anulacion_empareja_solo_depositos_de_un_cheque():
    """dep.N ch. consolidados quedan afuera: la ND compensa una PARTE."""
    src = _src_anular()
    i = src.index("FRENO 3")
    bloque = src[i : i + 3000]
    assert "len(_dep_solo) == 1" in bloque
    assert 'abs(float(x.get("importe") or 0) - abs(importe)) <= 0.01' in bloque
    assert 'int(x.get("no_banco") or 0) == int(banco)' in bloque
    assert '== "DE"' in bloque


def test_anulacion_empareja_en_la_misma_transaccion():
    src = _src_anular()
    i = src.index("FRENO 3")
    bloque = src[i : i + 3000]
    assert "emparejar_interno" in bloque
    assert "conn=conn" in bloque


def test_anulacion_el_pareo_no_es_requisito():
    """Si el pareo falla, la anulación tiene que salir igual."""
    src = _src_anular()
    i = src.index("FRENO 3")
    bloque = src[i : i + 3000]
    i_try = bloque.index("try:")
    assert "except Exception" in bloque[i_try:]
    assert "pass" in bloque[i_try:]


def test_emparejar_interno_un_solo_batch_para_todos():
    """Lección del 03/08: agrupar por confirm_batch_id, no por transacción.
    El batch se genera UNA vez, fuera del loop — deshacer los revierte juntos."""
    src = inspect.getsource(cq.emparejar_interno)
    i_batch = src.index("batch_id = uuid.uuid4().hex")
    i_loop = src.index("for pc_id in ids:")
    assert i_batch < i_loop
    assert "confirm_batch_id" in src
    assert "SET stat = '*'" in src
    assert '"interno:{motivo}"' in src or "f\"interno:{motivo}\"" in src


def test_emparejar_interno_es_reversible_como_cualquier_match():
    """estado='matched' — así 'Deshacer conciliados' lo encuentra."""
    src = inspect.getsource(cq.emparejar_interno)
    assert "'matched'" in src
    assert "compute_tx_firma" in src
