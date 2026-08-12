"""Los tres frenos del 05/08/2026 — nacidos de los 6 pendientes de conciliación.

Ese día la pantalla de conciliación mostraba 6 movimientos "según PC" que
nadie sabía con qué conciliar. La investigación (SQL sobre la base viva) mostró
que eran las cicatrices de tres agujeros del código:

1. **BAN +730**: el mismo cheque cargado dos veces con DOS SEMANAS de
   diferencia. El detector de duplicados sólo miraba cargas del mismo día.
   → FRENO 1: el alta avisaba (mismo cliente + importe activo, ±30 días) y
   pedía tildar «Es otro cheque». **SACADO el 12/08/2026 por decisión de la
   dueña**: molestaba en la carga diaria más de lo que atajaba. Queda el
   diagnóstico `/cheques/diag/cobros-duplicados` para buscarlos después, y
   los tests de acá abajo protegen que ESE siga andando.
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


# ─── FRENO 1: SACADO el 12/08/2026 ───────────────────────────────────────
# El alta ya no mira duplicados. Lo único que se protege es que no vuelva por
# accidente y que el diagnóstico posterior siga en pie.


def test_el_alta_no_frena_por_duplicado():
    """Dueña 12/08/2026: "eliminar lo de duplicados"."""
    src = inspect.getsource(v.nuevo)
    for rastro in ("confirmar_no_duplicado", "similares_activos",
                   "pedir_confirmar_duplicado"):
        assert rastro not in src, f"volvió el freno de duplicados ({rastro})"


def test_el_diagnostico_de_duplicados_sigue_existiendo():
    """Buscar duplicados DESPUÉS sigue estando bien — es el alta la que no frena."""
    assert hasattr(duplicados, "revisar")
    assert not hasattr(duplicados, "similares_activos"), (
        "similares_activos era sólo para el freno del alta; si vuelve, alguien "
        "está reponiendo el freno."
    )


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
