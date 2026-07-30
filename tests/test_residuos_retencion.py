"""Residuos de retención — los saldos a favor de monedas que dejó PC.

TMT 2026-07-30 (dueña): *"los residuos de retenciones deberían eliminarse"*.

Cuando el cliente retenía impuesto y su cheque no cerraba exacto contra las
facturas, la cobranza dejaba el sobrante como espejo NB=98 negativo. El dBase
no los tiene —ahí la diferencia se olvida—, así que PC arrastraba decenas de
"saldos a favor" de $2,86 que nadie iba a usar: −100,45 repartidos en 6
clientes en el control del 29/07.

Lo que se testea es la GUARDA, que es lo único peligroso: la lista se arma
sola, pero el borrado recibe ids de un form y no puede anular un saldo a favor
de verdad porque alguien mandó el id equivocado.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _fila(id_, importe, cli="DII"):
    return {"id_cheque": id_, "codigo_cli": cli, "importe": importe,
            "stat": "Z", "usuario_crea": "alex", "id_cheque_padre": None,
            "nombre": "", "fecha": None, "fechad": None,
            "importe_padre": None, "fecha_padre": None}


def test_la_consulta_pide_espejos_vivos_y_negativos():
    """Si el filtro se afloja, la pantalla empieza a ofrecer para anular
    saldos a favor legítimos o cheques que no son espejos."""
    import inspect

    from modules.cheques import queries as q
    src = inspect.getsource(q.residuos_retencion)
    assert "no_banco = 98" in src
    assert "COALESCE(c.importe, 0) < 0" in src, "un espejo es negativo"
    assert "IN ('Z', '1', '2', '3', 'P', 'D')" in src, "sólo los vivos"
    assert "ABS(COALESCE(c.importe, 0)) <= %s" in src, "y sólo los chicos"


def test_solo_anula_lo_que_la_consulta_considera_residuo(monkeypatch):
    from modules.cheques import queries as q
    anulados = []
    monkeypatch.setattr(q, "residuos_retencion",
                        lambda tope=100.0: [_fila(11, -2.86), _fila(12, -8.96)])
    monkeypatch.setattr(q, "anular_por_error_de_carga",
                        lambda i, **kw: anulados.append(i))
    # El 999 no está en la lista: es un saldo a favor grande que alguien mandó
    # de más. Se ignora en silencio, no se anula.
    res = q.eliminar_residuos_retencion([11, 999, 12], usuario="t")
    assert anulados == [11, 12], "el 999 no se tocó"
    assert res["n"] == 2
    assert res["total"] == pytest.approx(11.82)


def test_sin_ids_no_hace_nada(monkeypatch):
    from modules.cheques import queries as q
    monkeypatch.setattr(q, "residuos_retencion", lambda tope=100.0: [_fila(11, -2.86)])
    monkeypatch.setattr(q, "anular_por_error_de_carga",
                        lambda i, **kw: pytest.fail("no debería anular nada"))
    assert q.eliminar_residuos_retencion([], usuario="t")["n"] == 0


def test_el_motivo_dice_por_que(monkeypatch):
    """El cheque anulado tiene que explicarse solo dentro de seis meses."""
    from modules.cheques import queries as q
    vistos = {}
    monkeypatch.setattr(q, "residuos_retencion", lambda tope=100.0: [_fila(11, -2.86)])
    monkeypatch.setattr(q, "anular_por_error_de_carga",
                        lambda i, **kw: vistos.update(kw))
    q.eliminar_residuos_retencion([11], usuario="tamara")
    assert "retencion" in vistos["motivo"].lower()
    assert vistos["usuario"] == "tamara"
