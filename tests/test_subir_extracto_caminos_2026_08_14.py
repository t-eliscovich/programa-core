"""Los caminos de SUBIR UN EXTRACTO, que es por donde entra todo.

TMT 2026-08-14. `banco_v2_view.py` está al 21% y este endpoint es la puerta
de entrada de la conciliación: es el que Alex usa todos los días y el que en
dos días produjo el 500 de la sesión huérfana y el "no trajo movimientos"
cuando subió el archivo equivocado. Ninguno de sus caminos tenía test.

Se prueban los seis que puede tomar, y a propósito los de ERROR primero: son
los que el operador ve cuando algo sale mal, y hasta hoy nadie los había
ejercitado nunca.
"""
from __future__ import annotations

import io
import os
import sys
from datetime import date

import pytest

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)


@pytest.fixture
def cliente(app, monkeypatch):
    from modules.conciliacion import banco_v2_view as v

    @app.before_request
    def _login():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "alex", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    monkeypatch.setattr(v, "_migracion_lista_o_redirect", lambda: None)
    monkeypatch.setattr(v._db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(v._sesion, "completar_oficinas_pendientes", lambda nb: 0)
    return app.test_client()


def _avisos(cliente) -> str:
    with cliente.session_transaction() as s:
        return " ".join(m for _c, m in s.get("_flashes", [])).lower()


def _subir(cliente, contenido=b"xlsx", nombre="mov.xlsx"):
    return cliente.post(
        "/conciliacion/banco-v2/crear-sesion",
        data={"archivo": (io.BytesIO(contenido), nombre)},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def _mov(doc="1", monto=100.0):
    from modules.conciliacion.parser_banco import MovBanco
    return MovBanco(fecha=date(2026, 8, 12), concepto="TRANSFERENCIA",
                    documento=doc, monto=monto, saldo=1000, codigo="001045",
                    tipo="C", oficina="AG. NORTE")


# ── los caminos de error, que son los que ve el operador ──────────────

def test_sin_archivo_avisa_y_no_abre_nada(cliente, monkeypatch):
    from modules.conciliacion import banco_v2_view as v

    def _no_llamar(**kw):  # pragma: no cover
        raise AssertionError("no debe tocar la base sin archivo")
    monkeypatch.setattr(v._sesion, "crear_sesion", _no_llamar)

    rv = cliente.post("/conciliacion/banco-v2/crear-sesion",
                      data={}, content_type="multipart/form-data")
    assert rv.status_code == 302
    assert "banco-v2" not in rv.headers["Location"]
    assert "falta el archivo" in _avisos(cliente), (
        "tiene que decir que falta el archivo, no cualquier otra cosa"
    )


def test_archivo_vacio_avisa(cliente, monkeypatch):
    from modules.conciliacion import banco_v2_view as v

    def _no_llamar(**kw):  # pragma: no cover
        raise AssertionError("no debe tocar la base con un archivo vacío")
    monkeypatch.setattr(v._sesion, "crear_sesion", _no_llamar)

    rv = _subir(cliente, contenido=b"")
    assert rv.status_code == 302
    assert "banco-v2" not in rv.headers["Location"]
    # Sin este assert el test pasa igual con el guard sacado: el parser
    # devuelve [] para un archivo vacío y el aviso termina siendo otro.
    assert "vacío" in _avisos(cliente), (
        "tiene que decir que el archivo vino vacío, no 'no trajo movimientos'"
    )


def test_si_el_parser_explota_lo_dice_y_no_rompe_la_pantalla(cliente, monkeypatch):
    """Un xlsx corrupto no puede dejar un 500 en la cara del operador."""
    from modules.conciliacion import banco_v2_view as v

    def _explota(raw):
        raise ValueError("File is not a zip file")
    monkeypatch.setattr(v, "parse_banco_xlsx", _explota)

    rv = _subir(cliente)
    assert rv.status_code == 302, "tiene que avisar, no reventar"
    assert "no pude parsear" in _avisos(cliente)


def test_archivo_que_no_es_un_extracto_dice_cual_va(cliente, monkeypatch):
    """El caso de Alex el 13/08: subió la exportación de pendientes del
    propio programa. El parser devuelve [] y el aviso tiene que nombrar el
    archivo que sí va, o el operador vuelve a subir el mismo."""
    from modules.conciliacion import banco_v2_view as v
    monkeypatch.setattr(v, "parse_banco_xlsx", lambda raw: [])

    rv = _subir(cliente, nombre="conciliacion_pendientes_20260812.xlsx")
    assert rv.status_code == 302
    texto = _avisos(cliente)
    assert "pichincha" in texto and "pendientes del programa" in texto, (
        f"el aviso no dice qué archivo va: {texto}"
    )


# ── los caminos buenos ────────────────────────────────────────────────

def test_filas_nuevas_abren_la_conciliacion(cliente, monkeypatch):
    from modules.conciliacion import banco_v2_view as v

    monkeypatch.setattr(v, "parse_banco_xlsx", lambda raw: [_mov("A1"), _mov("A2")])
    monkeypatch.setattr(v._sesion, "crear_sesion", lambda **kw: (7, 2, 0))

    rv = _subir(cliente)
    assert rv.status_code == 302
    assert "banco-v2" in rv.headers["Location"], "va derecho a conciliar"


def test_todo_ya_cargado_abre_igual_con_los_pendientes(cliente, monkeypatch):
    """El caso que el 13/08 tiraba 500. Ahora la sesión se abre igual y el
    aviso dice que se arrancó con lo que ya estaba."""
    from modules.conciliacion import banco_v2_view as v

    monkeypatch.setattr(v, "parse_banco_xlsx", lambda raw: [_mov("A1")])
    monkeypatch.setattr(v._sesion, "crear_sesion", lambda **kw: (7, 0, 28))

    rv = _subir(cliente)
    assert rv.status_code == 302
    assert "banco-v2" in rv.headers["Location"]
    avisos = _avisos(cliente)
    assert "28" in avisos and "pendientes" in avisos


def test_al_subir_se_completan_las_oficinas(cliente, monkeypatch):
    """La dueña: "no quiero botón, siempre deberían venir puestas". El
    barrido tiene que dispararse solo con cada upload."""
    from modules.conciliacion import banco_v2_view as v

    monkeypatch.setattr(v, "parse_banco_xlsx", lambda raw: [_mov("A1")])
    monkeypatch.setattr(v._sesion, "crear_sesion", lambda **kw: (7, 1, 0))
    llamado = {"n": 0}

    def _completar(no_banco):
        llamado["n"] += 1
        return 3
    monkeypatch.setattr(v._sesion, "completar_oficinas_pendientes", _completar)

    _subir(cliente)
    assert llamado["n"] == 1, "el completado de oficinas no se disparó"
