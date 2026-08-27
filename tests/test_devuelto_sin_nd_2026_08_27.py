"""Vigia: un cheque devuelto tiene que dejar su ND en el banco (27/08/2026).

El caso GUG: dos cheques de $1.000 del mismo cliente rebotaron en el banco
(debitos reales del 04/08 y del 11/08) y en libros no aparecio NINGUNA nota
de debito — la devolucion de un cheque del dBase, sin deposito vinculado,
no la genera (`if not links: return 0.0`). La conciliacion quedo $1.000
arriba y se encontro a mano.

Lo que se protege aca:

  · el conteo es POR CANTIDAD por (cliente, importe): dos devoluciones del
    mismo importe necesitan DOS notas de debito — con un "¿existe alguna?"
    el segundo rebote se esconde detras de la ND del primero;
  · el vigia entra al /admin/health/all del cron;
  · deshacer un protesto SUELTA el match de conciliacion de su ND (caso
    ch246 CG3: la NC neteaba la plata pero el match vivo dejaba el debito
    real del extracto "explicado" por una ND anulada).
"""
from __future__ import annotations

import inspect
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.admin_dbase.health_audit_view import (  # noqa: E402
    _evaluar_devuelto_sin_nd,
    health_all,
)


def _fila(**kw):
    base = {"codigo_cli": "GUG", "importe": 1000.0,
            "n_devueltos": 1, "n_nds": 1, "ids_cheques": [100192]}
    base.update(kw)
    return base


def test_con_su_nd_no_alerta():
    alerts, stats = _evaluar_devuelto_sin_nd([_fila()])
    assert alerts == []
    assert stats["n_nds_faltantes"] == 0


def test_devuelto_sin_nd_alerta_high():
    alerts, stats = _evaluar_devuelto_sin_nd([_fila(n_nds=0)])
    assert len(alerts) == 1
    assert alerts[0]["nivel"] == "HIGH"
    assert "GUG" in alerts[0]["que"]
    assert stats["n_nds_faltantes"] == 1


def test_dos_devueltos_necesitan_dos_nds():
    """El caso GUG exacto: 2 rebotes de 1.000, UNA sola ND ⇒ falta una."""
    alerts, stats = _evaluar_devuelto_sin_nd(
        [_fila(n_devueltos=2, n_nds=1, ids_cheques=[100192, 101262])])
    assert len(alerts) == 1
    assert stats["n_nds_faltantes"] == 1
    # y las fichas de los dos cheques estan en donde_mirar
    assert "/cheques/100192" in alerts[0]["donde_mirar"]
    assert "/cheques/101262" in alerts[0]["donde_mirar"]


def test_nds_de_sobra_no_alertan():
    """Mas NDs que devoluciones no es problema de ESTE vigia."""
    alerts, _ = _evaluar_devuelto_sin_nd([_fila(n_devueltos=1, n_nds=2)])
    assert alerts == []


def test_el_vigia_entra_al_health_all():
    src = inspect.getsource(health_all)
    assert "devuelto_sin_nd()" in src
    assert '"devuelto_sin_nd"' in src
    # y participa del ok general
    assert 'data17["ok"]' in src


def test_deshacer_protesto_suelta_el_match_de_la_nd():
    """El deshacer consulta los matches vivos de la ND y los rompe."""
    from modules.cheques.queries import deshacer_devuelto
    src = inspect.getsource(deshacer_devuelto)
    assert "banco_conciliacion_match" in src
    assert "deshecho_en IS NULL" in src
    assert "romper_match" in src
    # va DESPUES de la transaccion del deshacer (romper_match abre la suya)
    assert src.index("with db.tx()") < src.index("romper_match")


def test_reclasificar_un_devuelto_viejo_no_cuenta():
    """MTV (D→1): re-clasificar un devuelto del dBase no mueve el banco.

    El vigia filtra por el stat del que SALIO el cheque (metadata del
    protesto): si ya estaba devuelto (D/1/2/9/R) o nunca se deposito (Z),
    no le corresponde ND. El filtro vive en el SQL del endpoint.
    """
    from modules.admin_dbase.health_audit_view import devuelto_sin_nd
    src = inspect.getsource(devuelto_sin_nd)
    assert "stat_prev" in src
    assert "NOT IN ('D', '1', '2', '9', 'R', 'Z')" in src
