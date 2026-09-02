"""`/admin/regenerar-snapshot` ("REGENERAR SNAPSHOT") tiene que borrar SÓLO
la fila del cierre (fecha exacta = último día del mes), nunca el mes
entero.

Incidente 2026-09-01: el DELETE viejo filtraba por
`EXTRACT(YEAR FROM fecha)=... AND EXTRACT(MONTH FROM fecha)=...` — un click
sobre "REGENERAR SNAPSHOT 2026-08" borró las 31 fotos diarias de agosto de
un saque, no sólo la fila de cierre que el botón dice regenerar.
`crear_snapshot_historia` sólo escribe UNA fila (el último día) de todos
modos, así que borrar cualquier otra cosa nunca hizo falta.

Tamara 2026-09-02 — segunda vuelta. La vista YA NO borra: le pasa `forzar=True`
a `crear_snapshot_historia`, que hace el DELETE y el INSERT dentro de UNA sola
transacción con advisory lock. Borrar acá dejaba a `scintela.historia` sin el
cierre entre el commit del DELETE y el INSERT de la recreación, y en esa ventana
`PATANT` caía a un cierre mucho más viejo: el 02/09 el balance mostró
+1.971.282 de utilidad durante 18 minutos.

La garantía original NO se afloja — se sigue exigiendo acá que la vista no emita
ningún DELETE por año/mes. Ahora la cumple no emitiendo ninguno.
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
    forzados: list[bool] = []

    def fake_crear(anio, mes, usuario=None, forzar=False, dry_run=False):
        forzados.append(forzar)
        return {"aplicado": False, "razon": "stub"}

    monkeypatch.setattr(iq, "crear_snapshot_historia", fake_crear)

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

    # La vista no borra nada por su cuenta: el borrado va DENTRO de la misma
    # transacción que la recreación, para no dejar a historia sin cierre.
    assert deletes == [], (
        "la vista volvió a borrar por su cuenta. Eso deja a scintela.historia "
        "sin la fila de cierre hasta que termine la recreación, y en esa "
        f"ventana PATANT cae a un cierre más viejo. DELETEs: {deletes}"
    )
    assert forzados == [True], (
        "sin forzar=True, crear_snapshot_historia no pisa la fila que ya está "
        f"y la regeneración no hace nada. forzar recibido: {forzados}"
    )

    # La garantía original: nada que se emita puede filtrar por año/mes.
    for sql, _ in deletes:
        assert "EXTRACT(YEAR" not in sql.upper()
        assert "EXTRACT(MONTH" not in sql.upper()

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
