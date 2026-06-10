"""Tests de importar_desde_dbf (core de /admin/clientes-import y del hook
post-sync de /admin/dbase-sync). TMT 2026-06-10."""
from contextlib import contextmanager
from pathlib import Path

import pytest

from modules.admin_dbase import clientes_import_view as civ


def _fila(cod, nombre="", direccion1="", telefono="", **kw):
    base = {c: "" for c, _d, _m in civ.FICHA}
    base.update({"codigo_cli": cod, "nombre": nombre,
                 "direccion1": direccion1, "telefono": telefono})
    base.update(kw)
    return base


def _setup(monkeypatch, dbf_rows, pc_rows):
    import db as _db
    executes = []

    monkeypatch.setattr(civ, "_leer_dbf", lambda p: dbf_rows)
    monkeypatch.setattr(
        civ, "_leer_pc",
        lambda: {r["codigo_cli"].upper(): r for r in pc_rows},
    )

    def fake_execute(sql, params=None, conn=None):
        executes.append((sql, params))
        return 1

    @contextmanager
    def fake_tx():
        yield object()

    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setattr(_db, "tx", fake_tx)
    return executes


def _run(aplicar, verbose=True):
    return "".join(civ.importar_desde_dbf(Path("x.DBF"), aplicar, verbose=verbose))


def test_insert_cliente_nuevo_y_rellenar_solo(monkeypatch):
    executes = _setup(
        monkeypatch,
        dbf_rows=[
            _fila("FGJ", nombre="RAMIREZ EDGAR", direccion1="AV X"),  # falta en PC
            _fila("AJO", nombre="OTRO NOMBRE", telefono="099"),       # PC ya tiene nombre
        ],
        pc_rows=[{"codigo_cli": "AJO", "nombre": "AJO REAL", "telefono": "",
                  "ruc": "", "direccion1": "", "direccion2": "",
                  "provincia": "", "canton": "", "parroquia": "", "vend": ""}],
    )
    out = _run(aplicar=True)
    assert "1 UPDATE" in out and "1 INSERT" in out
    assert "APLICADO" in out
    ins = [e for e in executes if e[0].lstrip().startswith("INSERT")]
    ups = [e for e in executes if e[0].lstrip().startswith("UPDATE")]
    assert len(ins) == 1 and ins[0][1][0] == "FGJ"
    # rellenar-solo: AJO gana telefono pero NO se toca el nombre ya cargado
    assert len(ups) == 1 and "telefono" in ups[0][0] and "nombre" not in ups[0][0]


def test_dry_run_no_escribe(monkeypatch):
    executes = _setup(monkeypatch, dbf_rows=[_fila("XX")], pc_rows=[])
    out = _run(aplicar=False)
    assert "DRY-RUN: no se tocó nada" in out
    assert executes == []


def test_verbose_false_resume_sin_listados(monkeypatch):
    _setup(monkeypatch, dbf_rows=[_fila("FGJ", nombre="N")], pc_rows=[])
    out = _run(aplicar=False, verbose=False)
    assert "--- INSERT" not in out
    assert "nuevos: FGJ" in out
    assert "PLAN:" in out


def test_solo_pc_queda_intacto(monkeypatch):
    # cliente solo-PC (ej. auto-creado desde Asinfo) no se borra ni toca
    executes = _setup(
        monkeypatch, dbf_rows=[],
        pc_rows=[{"codigo_cli": "NAI", "nombre": "", "telefono": "", "ruc": "",
                  "direccion1": "", "direccion2": "", "provincia": "",
                  "canton": "", "parroquia": "", "vend": ""}],
    )
    out = _run(aplicar=True)
    assert "solo-PC" in out or "NO están en el DBF" in out
    assert executes == []
