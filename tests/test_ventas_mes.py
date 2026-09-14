"""`/informes/ventas` — ranking del mes por cliente + tab "día por día".

TMT 2026-09-14 — Tamara, sobre https://programa.intela.com.ec/informes/ventas:

    "aca los recuadros esta mal. [...] y podria haber otro tab que sea dia
    por dia."

Dos cosas:

1. "los recuadros esta mal" — las 3 tablas de ranking (partidas en columnas)
   se estaban dejando ordenar con click (flechitas ↕ de `sortable-tables.js`
   en TODAS las columnas), cuando en realidad tienen un orden fijo (por
   monto descendente, como TINT.BAT del dBase legacy). Se protege con
   `data-no-sort-table` en el contenedor `.vm-grid` — convención ya usada
   en balance/resultados (ver comentario "TMT 2026-07-06 dueña").

2. Tab nuevo "día por día": Tamara eligió, vía pregunta directa, "Total de
   la empresa por día" — no un ranking por cliente de un solo día. Mismo
   patrón `?tab=` que `/uso`. `ventas_por_dia` usa la MISMA fuente y los
   MISMOS filtros que `ventas_clientes_del_mes` (scintela.factura, excluye
   `stat='X'` y el backfill de asinfo) para que los dos tabs sumen lo
   mismo — eso es lo que protegen los tests de acá.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from modules.informes import queries

# ── ventas_por_dia ───────────────────────────────────────────────────────────


def _fake_fetch_all(monkeypatch, rows):
    import db

    capturado = {}

    def fetch_all(sql, params=None, **kw):
        capturado["sql"] = sql
        capturado["params"] = params
        return rows

    monkeypatch.setattr(db, "fetch_all", fetch_all)
    return capturado


def test_un_renglon_por_dia_del_mes_con_ceros(monkeypatch):
    """Septiembre tiene 30 días: la grilla trae 30 filas, aunque sólo 2
    tengan venta — los demás días van en cero, no se saltean."""
    cap = _fake_fetch_all(
        monkeypatch,
        [
            {"dia": 1, "kg": 100, "monto": 500.0},
            {"dia": 15, "kg": 40, "monto": 200.0},
        ],
    )
    with patch.object(queries, "today_ec", return_value=date(2026, 9, 14)):
        data = queries.ventas_por_dia(2026, 9)

    assert len(data["filas"]) == 30
    assert data["filas"][0] == {"dia": 1, "kg": 100, "monto": 500.0}
    assert data["filas"][1] == {"dia": 2, "kg": 0, "monto": 0.0}
    assert data["filas"][14] == {"dia": 15, "kg": 40, "monto": 200.0}
    assert data["dias_con_venta"] == 2
    assert data["total_kg"] == 140
    assert data["total_monto"] == 700.0
    assert cap["params"] == (2026, 9)


def test_excluye_anuladas_y_backfill_igual_que_el_ranking_por_cliente(monkeypatch):
    """El SQL tiene que llevar los MISMOS filtros que `ventas_clientes_del_mes`
    (stat <> 'X', usuario_crea <> 'asinfo-backfill') para que ambos tabs
    coincidan en el total."""
    cap = _fake_fetch_all(monkeypatch, [])
    with patch.object(queries, "today_ec", return_value=date(2026, 9, 14)):
        queries.ventas_por_dia(2026, 9)
    sql = cap["sql"]
    assert "stat" in sql and "'X'" in sql
    assert "asinfo-backfill" in sql
    assert "scintela.factura" in sql


def test_default_anio_mes_es_hoy(monkeypatch):
    _fake_fetch_all(monkeypatch, [])
    with patch.object(queries, "today_ec", return_value=date(2026, 9, 14)):
        data = queries.ventas_por_dia()
    assert data["anio"] == 2026
    assert data["mes"] == 9
    assert len(data["filas"]) == 30  # setiembre


def test_febrero_bisiesto(monkeypatch):
    _fake_fetch_all(monkeypatch, [])
    with patch.object(queries, "today_ec", return_value=date(2026, 9, 14)):
        data = queries.ventas_por_dia(2024, 2)
    assert len(data["filas"]) == 29


# ── la pantalla: /informes/ventas?tab=... ───────────────────────────────────


def _login(app, fake_db, perms=("informes.ver",)):
    rid = fake_db.add_role("Tester", list(perms))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _data_mes():
    return {
        "anio": 2026, "mes": 9,
        "filas": [{"orden": 1, "codigo_cli": "ABC", "kg": 100, "monto": 500.0, "pct": 100.0}],
        "total_kg": 100, "total_monto": 500.0, "n_clientes": 1,
    }


def _data_dia():
    return {
        "anio": 2026, "mes": 9,
        "filas": [{"dia": d, "kg": 0, "monto": 0.0} for d in range(1, 31)],
        "total_kg": 0, "total_monto": 0.0, "dias_con_venta": 0,
    }


def test_tab_por_defecto_es_mes(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_clientes_del_mes", return_value=_data_mes()) as m_mes, \
         patch.object(queries, "ventas_por_dia", return_value=_data_dia()) as m_dia:
        r = c.get("/informes/ventas?anio=2026&mes=9")
    assert r.status_code == 200
    m_mes.assert_called_once()
    m_dia.assert_not_called()
    html = r.get_data(as_text=True)
    assert "Por cliente" in html
    assert "Día por día" in html


def test_tab_dia_llama_ventas_por_dia(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_clientes_del_mes", return_value=_data_mes()) as m_mes, \
         patch.object(queries, "ventas_por_dia", return_value=_data_dia()) as m_dia:
        r = c.get("/informes/ventas?anio=2026&mes=9&tab=dia")
    assert r.status_code == 200
    m_dia.assert_called_once()
    m_mes.assert_not_called()
    html = r.get_data(as_text=True)
    assert "TOTAL" in html.upper()


def test_tab_invalido_cae_a_mes(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_clientes_del_mes", return_value=_data_mes()) as m_mes, \
         patch.object(queries, "ventas_por_dia", return_value=_data_dia()) as m_dia:
        r = c.get("/informes/ventas?tab=lo-que-sea")
    assert r.status_code == 200
    m_mes.assert_called_once()
    m_dia.assert_not_called()


def test_las_3_tablas_del_ranking_no_se_dejan_ordenar(app, fake_db):
    """'los recuadros esta mal' (Tamara) — eran las flechitas de orden en
    columnas de una tabla con orden fijo. `data-no-sort-table` es el
    opt-out estándar de sortable-tables.js (ver balance/resultados)."""
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_clientes_del_mes", return_value=_data_mes()):
        r = c.get("/informes/ventas?anio=2026&mes=9")
    html = r.get_data(as_text=True)
    assert "data-no-sort-table" in html


def test_tab_dia_tambien_tiene_orden_fijo(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_por_dia", return_value=_data_dia()):
        r = c.get("/informes/ventas?tab=dia")
    html = r.get_data(as_text=True)
    assert "data-no-sort-table" in html


def test_el_filtro_de_anio_mes_preserva_el_tab(app, fake_db):
    """Cambiar de año/mes con el form no te tiene que devolver al tab
    'Por cliente' si estabas en 'Día por día'."""
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_por_dia", return_value=_data_dia()):
        r = c.get("/informes/ventas?tab=dia&anio=2026&mes=9")
    html = r.get_data(as_text=True)
    assert 'name="tab" value="dia"' in html


def test_sin_permiso_no_deja_pasar(app, fake_db):
    c = _login(app, fake_db, perms=())
    r = c.get("/informes/ventas")
    assert r.status_code in (302, 403, 404)
