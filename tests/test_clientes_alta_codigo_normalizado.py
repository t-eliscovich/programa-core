"""El alta de cliente no puede dejar pasar un código duplicado.

TMT 2026-08-04 (dueña): "hay que prohibir codigos duplicados".

El chequeo de unicidad de `queries.crear` comparaba ``codigo_cli = %s``
**exacto**. Todo el resto del sistema —la factura, el cheque, la retención,
el índice único de la migración 0155— resuelve por ``UPPER(TRIM(codigo_cli))``,
y en la base hay códigos guardados con espacios al final. O sea que el alta
era la única puerta que seguía viendo `'LEC '` y `'LEC'` como dos códigos
distintos: por ahí entraba el duplicado que el resto del sistema iba a contar
dos veces.

Desde la 0155 el INSERT además explota contra el índice. Un `UniqueViolation`
crudo en la cara del usuario no dice QUIÉN ocupa el código, que es lo único
que sirve para elegir otro. Estos tests fijan que el rechazo pase antes, con
el nombre adentro del mensaje.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.clientes import queries as q  # noqa: E402


class _FakeDB:
    """Stub de db. `ocupados` mapea código NORMALIZADO → nombre del dueño."""

    def __init__(self, ocupados: dict[str, str] | None = None):
        # La base guarda lo que guarda; la clave es el código ya normalizado,
        # que es como lo mira el SQL de producción.
        self.ocupados = ocupados or {}
        self.consultas: list[tuple[str, tuple]] = []
        self.inserts: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split())
        self.consultas.append((s, tuple(params or ())))
        cod = (params or ("",))[0]
        if cod in self.ocupados:
            return {"nombre": self.ocupados[cod]}
        return None

    def execute_returning(self, sql, params=None, conn=None):
        self.inserts.append(tuple(params or ()))
        return {"id_cliente": 999, "codigo_cli": (params or ("",))[0]}


@pytest.fixture()
def fake(monkeypatch):
    def _mk(**kw):
        f = _FakeDB(**kw)
        monkeypatch.setattr(q, "db", f)
        return f
    return _mk


def test_codigo_libre_se_da_de_alta(fake):
    f = fake()
    out = q.crear(codigo_cli="zzq", nombre="CLIENTE NUEVO")
    assert out["codigo_cli"] == "ZZQ"
    assert len(f.inserts) == 1
    assert f.inserts[0][0] == "ZZQ", "el código se guarda normalizado"


def test_rechaza_el_codigo_ocupado(fake):
    fake(ocupados={"LEC": "LUIS ERNESTO CANAMAR"})
    with pytest.raises(ValueError) as exc:
        q.crear(codigo_cli="LEC", nombre="OTRO CLIENTE")
    assert "LEC" in str(exc.value)


def test_el_mensaje_dice_quien_ocupa_el_codigo(fake):
    """Sin el nombre, el usuario no sabe si el ocupado es un error suyo."""
    fake(ocupados={"LEC": "LUIS ERNESTO CANAMAR"})
    with pytest.raises(ValueError) as exc:
        q.crear(codigo_cli="lec", nombre="LOLA EMPERATRIZ CISNEROS")
    assert "LUIS ERNESTO CANAMAR" in str(exc.value)


@pytest.mark.parametrize("tipeado", ["lec", "LEC", " lec ", "Lec  "])
def test_el_duplicado_no_entra_escrito_de_ninguna_forma(fake, tipeado):
    """Minúsculas y espacios son EL MISMO código para el resto del sistema."""
    fake(ocupados={"LEC": "LUIS ERNESTO CANAMAR"})
    with pytest.raises(ValueError):
        q.crear(codigo_cli=tipeado, nombre="OTRO")


def test_la_consulta_de_unicidad_normaliza(fake):
    """El contrato con el índice de la 0155: misma expresión de los dos lados."""
    f = fake()
    q.crear(codigo_cli="ZZQ", nombre="CLIENTE NUEVO")
    sql, params = f.consultas[0]
    assert "UPPER(TRIM(codigo_cli)) = %s" in sql
    assert params == ("ZZQ",)


def test_no_llega_al_insert_si_el_codigo_esta_ocupado(fake):
    f = fake(ocupados={"LEC": "LUIS ERNESTO CANAMAR"})
    with pytest.raises(ValueError):
        q.crear(codigo_cli="LEC", nombre="OTRO")
    assert f.inserts == [], "el INSERT no se tiene que intentar"


@pytest.mark.parametrize("vacio", ["", "   "])
def test_codigo_vacio_se_rechaza_antes_de_consultar(fake, vacio):
    f = fake()
    with pytest.raises(ValueError, match="Código requerido"):
        q.crear(codigo_cli=vacio, nombre="CLIENTE")
    assert f.consultas == []


def test_nombre_requerido(fake):
    fake()
    with pytest.raises(ValueError, match="Nombre requerido"):
        q.crear(codigo_cli="ZZQ", nombre="")
