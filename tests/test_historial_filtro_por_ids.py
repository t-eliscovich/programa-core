"""El renglón de la traza lleva a SUS movimientos, no al día entero.

TMT 2026-08-11 (dueña, clickeando "4 FA · CLR, MHE, MPO"): *"4 facturas pero
acá dicen sólo 3 nombres, y cuando lo clickeo me debería llevar sólo a esas
facturas"*. Iba a `/historial?tipo=factura_emitida&desde=hoy&hasta=hoy`: las 37
del día para explicar cuatro.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.historial import queries as hq  # noqa: E402
from modules.informes import traza as t  # noqa: E402


def _sql_de(**kw):
    visto = {}

    def _fake(sql, params=None, *a, **k):
        visto["sql"], visto["params"] = sql, params
        return []

    with patch.object(hq.db, "fetch_all", side_effect=_fake), \
         patch.object(hq.db, "fetch_one", return_value={"1": 1}):
        hq.listar(**kw)
    return visto


def test_la_consulta_filtra_por_los_ids_pedidos():
    v = _sql_de(ids=[101, 102, 103, 104])
    assert "u.id_mov_doble = ANY(%(ids)s::bigint[])" in v["sql"]
    assert v["params"]["ids"] == [101, 102, 103, 104]


def test_sin_ids_no_cambia_nada():
    """El filtro tiene que ser invisible cuando no se usa: `ids` en NULL deja
    pasar todo, igual que antes."""
    assert _sql_de()["params"]["ids"] is None


def test_los_ids_llegan_como_enteros():
    """Vienen de la query string: si entran como texto, el cast a bigint[]
    revienta la pantalla entera."""
    assert _sql_de(ids=["101", "102"])["params"]["ids"] == [101, 102]


# ── el link que los manda ───────────────────────────────────────────────────

def test_el_renglon_linkea_a_sus_movimientos():
    g = {"hechos": {104, 101, 103, 102}}
    ev = {"tipo": "factura_emitida", "dia": "2026-08-11"}
    assert t._url_del_grupo(g, ev) == "/historial?ids=101,102,103,104"


def test_una_foto_vieja_sin_hechos_cae_al_filtro_por_dia():
    """Las fotos anteriores a la grabadora de detalle no guardaron los ids:
    ahí el link viejo es lo mejor que hay, y es mejor que ninguno."""
    g = {"hechos": set()}
    ev = {"tipo": "factura_emitida", "dia": "2026-08-11"}
    assert t._url_del_grupo(g, ev) == (
        "/historial?tipo=factura_emitida&desde=2026-08-11&hasta=2026-08-11")


def test_banco_varios_sigue_sin_link():
    """No es un tipo de mov_doble: filtrar por él daba una pantalla vacía."""
    assert t._url_del_grupo({"hechos": {1}}, {"tipo": "banco_varios",
                                              "dia": "2026-08-11"}) is None
