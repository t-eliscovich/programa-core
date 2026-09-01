"""`/admin/regenerar-snapshot` ("REGENERAR SNAPSHOT") tiene que borrar SÓLO
la fila del cierre (fecha exacta = último día del mes), nunca el mes
entero.

Incidente 2026-09-01: el DELETE viejo filtraba por
`EXTRACT(YEAR FROM fecha)=... AND EXTRACT(MONTH FROM fecha)=...` — un click
sobre "REGENERAR SNAPSHOT 2026-08" borró las 31 fotos diarias de agosto de
un saque, no sólo la fila de cierre que el botón dice regenerar.
`crear_snapshot_historia` sólo escribe UNA fila (el último día) de todos
modos, así que borrar cualquier otra cosa nunca hizo falta.
"""
from __future__ import annotations

from unittest.mock import patch

import db
from modules.admin_dbase import regen_snapshot_view
from modules.informes import queries as iq


def test_aplicar_borra_por_fecha_exacta_no_por_mes(app, monkeypatch):
    deletes: list[tuple[str, tuple]] = []
    selects: list[tuple[str, tuple]] = []

    def fake_fetch_all(sql, params=None, conn=None):
        selects.append((" ".join(sql.split()), params))
        return [{"id_historia": 999}]  # simula que SÍ hay algo para borrar

    def fake_execute(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if s.strip().upper().startswith("DELETE"):
            deletes.append((s, params))
        return 0

    class _DummyTx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(db, "tx", lambda: _DummyTx())
    monkeypatch.setattr(
        iq, "crear_snapshot_historia",
        lambda anio, mes, usuario=None: {"aplicado": False, "razon": "stub"},
    )

    @app.before_request
    def _login():
        from flask import g
        g.user = {"id_usuario": 0, "username": "test", "id_rol": 0,
                   "nombre_rol": "Accionista", "activo": True, "vend": None}
        g.permisos = {"*"}

    client = app.test_client()
    client.post(
        "/admin/regenerar-snapshot/",
        data={"anio": "2026", "mes": "8", "aplicar": "1"},
    )

    # No hubo NINGÚN DELETE que filtre por año/mes en vez de por fecha.
    assert deletes, "el POST tendría que haber intentado el DELETE."
    for sql, params in deletes:
        assert "EXTRACT(YEAR" not in sql.upper()
        assert "EXTRACT(MONTH" not in sql.upper()
        assert "fecha = %s" in sql
        assert params == (regen_snapshot_view._fecha_cierre_de(2026, 8),)

    # La consulta que CUENTA cuánto se va a borrar (para el mensaje
    # "Borradas N filas") tiene que mirar la MISMA fecha exacta que el
    # DELETE — si no, el conteo mentiría.
    n_borrados_selects = [
        (sql, params) for sql, params in selects
        if "id_historia" in sql and "scintela.historia" in sql
        and "order by fecha desc" not in sql.lower()
    ]
    assert n_borrados_selects, "no encontré la consulta que cuenta n_borrados"
    for sql, params in n_borrados_selects:
        assert "EXTRACT(YEAR" not in sql.upper()
        assert "EXTRACT(MONTH" not in sql.upper()
        assert "fecha = %s" in sql
