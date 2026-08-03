"""El auditor de conciliación agrupa por el BATCH, no por la tx de PC.

QUÉ PASÓ (03/08/2026). `/conciliacion/banco-v2/auditar`, check 2, mostraba
**36 matches con monto distinto · suma +754.963,54** mientras la conciliación
cerraba en **+2,00**. O sea 755k de falso positivo.

Lo cazó la dueña: *"para mí esos de match distinto es porque no está contando
agrupados… o sea distinto número de movimientos de cada lado"*. Tenía razón.

La migración 0071 (2026-06-03) dice textualmente: *"Hoy cada match es 1:1 en
DB. Una conciliación **2 vs 14** crea 14 matches. 'Deshacer' libera de a 1 —
si tenés **N:M**, la math se rompe. Fix: `confirm_batch_id` agrupa los matches
creados juntos en una sola operación."*

El check agrupaba por `tb.id_transaccion` — el lado del PROGRAMA — teniendo
`confirm_batch_id` ya disponible desde el día anterior. Así, una conciliación
de 2 del banco contra 14 del programa se partía en 14 pedazos y se comparaba
cada pedazo contra lo que le tocó del banco: ninguno cerraba, aunque el grupo
entero cerrara perfecto.

Este test mira el FUENTE (mismo patrón que `test_yy_persist_motor_unico` y
`test_conciliacion_xlsx_ida_y_vuelta`) porque la lógica vive en SQL y no hay
Postgres en el CI.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sql_del_check2() -> str:
    from modules.conciliacion.banco_v2_view import banco_auditar

    src = inspect.getsource(banco_auditar)
    i = src.index("Check 2")
    j = src.index("Check 3")
    return src[i:j]


def test_agrupa_por_confirm_batch_id():
    sql = _sql_del_check2()
    assert "confirm_batch_id" in sql, (
        "el auditor volvió a agrupar sin `confirm_batch_id`: una conciliación "
        "N:M se parte en pedazos y ninguno cierra → falso positivo gigante "
        "(36 matches / +754.963,54 el 03/08/2026)"
    )
    assert re.search(r"COALESCE\(\s*m\.confirm_batch_id", sql), (
        "el batch tiene que caer de vuelta a la tx de PC cuando el match es "
        "viejo y no tiene confirm_batch_id"
    )


def test_NO_agrupa_por_la_transaccion_de_pc():
    sql = _sql_del_check2()
    assert "GROUP BY tb.id_transaccion, tb.importe, tb.documento, tb.concepto" not in sql, (
        "volvió el GROUP BY por el lado del PROGRAMA"
    )


def test_dedupea_los_dos_lados_antes_de_sumar():
    """Dentro de un batch, el mismo movimiento aparece en varias filas.

    Si se suma `real_monto` tal cual, el lado del banco se cuenta de más; lo
    mismo del lado de PC con `importe`. Los dos se agrupan por su identidad
    antes de sumar.
    """
    sql = _sql_del_check2()
    assert re.search(
        r"GROUP BY batch,\s*real_fecha,\s*real_documento,\s*real_monto,\s*real_tipo",
        sql,
    ), "el lado del BANCO no se dedupea por firma antes de sumar"
    assert re.search(r"GROUP BY a\.batch,\s*tb\.id_transaccion", sql), (
        "el lado del PROGRAMA no se dedupea por id_transaccion antes de sumar"
    )


def test_muestra_cuantos_movimientos_hay_de_cada_lado():
    """La dueña tiene que poder ver de una que el grupo era N:M."""
    sql = _sql_del_check2()
    assert "n_banco" in sql and "n_pc" in sql
    assert "banco / " in sql, "falta la etiqueta «N banco / M prog.» en la columna Doc"


def test_sigue_filtrando_los_deshechos():
    """Un match deshecho no puede contar como descuadre."""
    sql = _sql_del_check2()
    assert "m.deshecho_en IS NULL" in sql
    assert "m.estado = 'matched'" in sql


def test_la_pantalla_dice_cuantos_eran_falso_positivo():
    """Se muestra el conteo con la agrupación vieja al lado del nuevo.

    Sin eso, el número baja de 36 a un puñado y nadie sabe si se arregló o si
    se rompió el chequeo.
    """
    from modules.conciliacion.banco_v2_view import banco_auditar

    src = inspect.getsource(banco_auditar)
    assert "diff_matches_viejo" in src
    tpl = (ROOT / "modules/conciliacion/templates/conciliacion/auditar.html").read_text(
        encoding="utf-8")
    assert "diff_matches_viejo" in tpl
