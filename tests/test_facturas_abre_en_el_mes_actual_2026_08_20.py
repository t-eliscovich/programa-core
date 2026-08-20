"""/facturas abre en el MES ACTUAL (Federico 2026-08-20).

*"cuando entro en Facturas traeme la informacion del mes actual (en los
cuadrados de kg, precio, valor $)"*.

Entrar pelado —el link del menú, sin un solo parámetro— equivale a apretar el
botón "Mes actual": vista `facturado` (todo lo emitido, cobrado + pendiente +
cancelado, sin eliminadas ni backfill — el mismo universo que la Venta de
Resultados) del día 1 al último del mes.

Lo que estos tests fijan:
  1. sin args → vista/desde/hasta del mes en curso;
  2. con CUALQUIER arg → el default NO se re-aplica (si no, los tabs, el
     Filtrar y la paginación no podrían salir nunca del mes);
  3. el mes se calcula en hora Ecuador (UTC-5), no en la del server (que corre
     en UTC): el 31 a las 20:00 EC ya son las 01:00 UTC del día 1 siguiente.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from modules.facturas import queries as fq
from modules.facturas import views as fv


@pytest.fixture
def cliente(app, fake_db):
    rid = fake_db.add_role("Tester", ["facturas.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _pantalla(cliente, url, ahora_utc):
    """GET a /facturas con la capa de datos y el reloj mockeados.

    Devuelve {vista, desde, hasta} tal como la vista se los pasó a
    `contar_filtrado()` — que es la query de los cuadrados de arriba (kg /
    precio / facturado). Ojo: `desde` y `hasta` viajan POSICIONALES
    (q, desde, hasta, solo_abiertas, ...), sólo `vista` va por nombre.
    """
    capturado = {}

    def _contar(*a, **kw):
        capturado["llamada"] = {
            "vista": kw.get("vista"),
            "desde": a[1] if len(a) > 1 else kw.get("desde"),
            "hasta": a[2] if len(a) > 2 else kw.get("hasta"),
        }
        return {"n": 0, "total_importe": 0.0, "total_saldo": 0.0, "total_kg": 0.0}

    class _Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return ahora_utc if tz else ahora_utc.replace(tzinfo=None)

    with patch.object(fq, "buscar", return_value=[]), \
         patch.object(fq, "conteos_por_vista", return_value={}), \
         patch.object(fq, "contar_filtrado", _contar), \
         patch.object(fv, "_auto_cargar_facturas_hoy", return_value={}), \
         patch("datetime.datetime", _Reloj):
        r = cliente.get(url)
    assert r.status_code == 200, r.status_code
    return capturado.get("llamada", {}), r.get_data(as_text=True)


def test_entrar_pelado_trae_el_mes_en_curso(cliente):
    """Sin un solo parámetro: vista 'facturado', del 1 al último del mes."""
    kw, _ = _pantalla(cliente, "/facturas", datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc))
    assert kw["vista"] == "facturado"
    assert kw["desde"] == "2026-08-01"
    assert kw["hasta"] == "2026-08-31"


def test_febrero_no_se_pasa_de_largo(cliente):
    """El último día sale de calendar.monthrange, no de un 30/31 fijo."""
    kw, _ = _pantalla(cliente, "/facturas", datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc))
    assert kw["desde"] == "2026-02-01"
    assert kw["hasta"] == "2026-02-28"


def test_el_mes_es_el_de_ecuador_y_no_el_del_server(cliente):
    """31/08 20:00 EC = 01/09 01:00 UTC. El server está en UTC.

    Con `now()` pelado la pantalla saltaría a septiembre a las 19:00 de un día
    de trabajo normal, y la dueña vería el mes vacío sin haber cambiado nada.
    """
    kw, _ = _pantalla(cliente, "/facturas", datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc))
    assert kw["desde"] == "2026-08-01"
    assert kw["hasta"] == "2026-08-31"


@pytest.mark.parametrize("url", [
    "/facturas?vista=cartera",
    "/facturas?page=2",
    "/facturas?cliente=BED",
])
def test_con_cualquier_parametro_el_default_no_pisa_nada(cliente, url):
    """Los tabs, el Filtrar y la paginación tienen que poder salir del mes."""
    kw, _ = _pantalla(cliente, url, datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc))
    assert not (kw["desde"] == "2026-08-01" and kw["hasta"] == "2026-08-31"), kw
