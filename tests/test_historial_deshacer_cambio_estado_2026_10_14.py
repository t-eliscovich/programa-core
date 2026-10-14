"""El ↺ del historial deshace un cambio de estado del cheque (X/Z/P/D/T).

Dueña 14/10/2026: *"tuve que entrar al cheque para deshacer. me debería haber
dejado desde el historial"*. La fila "Cheque: cambio de estado" mostraba el ↺
pero el tipo no tenía handler y mandaba a la ficha.
"""
import pytest

from modules.cheques import queries as q
from modules.historial import views as hv


def test_el_tipo_tiene_reverso_inline_con_permiso_de_la_operacion():
    assert hv._PERMISO_REVERSO_INLINE["cheque_stat_cambio"] == "cheques.transicionar"


@pytest.mark.parametrize("meta,concepto,esperado", [
    ({"stat_prev": "Z", "stat_destino": "X"}, "", ("Z", "X")),
    ({}, "X→Z cheque 106487 LEG", ("X", "Z")),   # filas sin metadata
])
def test_lee_de_que_estado_venia(monkeypatch, meta, concepto, esperado):
    fila = {"id_mov_doble": 1, "tipo": "cheque_stat_cambio", "estado": "activo",
            "origen_id": 7, "importe": 1, "concepto": concepto, "metadata": meta}
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: fila)
    _, st = q._mov_stat_cambio(1)
    assert (st["stat_prev"], st["stat_destino"]) == esperado


def test_no_deshace_dos_veces_ni_otro_tipo(monkeypatch):
    fila = {"tipo": "cheque_stat_cambio", "estado": "reversado", "metadata": {}}
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: fila)
    with pytest.raises(ValueError, match="ya se deshizo"):
        q._mov_stat_cambio(1)
    fila.update(tipo="cheque_depositado", estado="activo")
    with pytest.raises(ValueError, match="no un cambio de estado"):
        q._mov_stat_cambio(1)


def test_frena_si_el_cheque_se_movio_despues(monkeypatch):
    filas = iter([
        {"tipo": "cheque_stat_cambio", "estado": "activo", "origen_id": 7,
         "metadata": {"stat_prev": "P", "stat_destino": "Z"}, "concepto": ""},
        {"id_cheque": 7, "no_cheque": "1", "stat": "D", "codigo_cli": "AAA", "importe": 1},
    ])
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: next(filas))
    with pytest.raises(ValueError, match="se movió después"):
        q.plan_deshacer_cambio_estado(1)
