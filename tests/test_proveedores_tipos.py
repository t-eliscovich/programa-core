"""Los tipos que ofrece la pantalla de proveedores (TMT 2026-08-20).

Dueña: *"cuando creo un proveedor, puedo elegir tipo. me propone opciones que
no tengo, ejemplo U maquinaria, no está"*.

El desplegable de Tipo del alta estaba hardcodeado en `['B','S','M','I']`, y de
esas cuatro letras TRES (B, S, I) no las tenía ningún proveedor de la base,
mientras faltaban las que el sistema sí usa: U (16 proveedores), H (11),
Q (52), K (14), Y (1). Encima había otras dos listas distintas: el filtro de
arriba de /proveedores ofrecía U/H/Q/B/Y/K y el lapicito de la columna Tipo
U/H/Q/B/Y (sin K).

No es cosmético: el tipo decide en qué caja cae el proveedor — los tipo 'H'
entran como MATERIA PRIMA en el flujo de fondos, separada de gastos
(modules/informes/queries.py), 'U' se trata como maquinaria y 'Q' como químicos
en dólares. Un proveedor nuevo con la letra equivocada va a la caja equivocada.

Contrato que fijan estos tests:
  1. El alta ofrece las SEIS letras, cada una con su nombre en castellano.
  2. Ya no ofrece letras que no usa nadie (S, I).
  3. Las tres pantallas leen la MISMA lista — ninguna la repite hardcodeada.
  4. Un tipo viejo ya cargado (M, 37 proveedores) se muestra tal cual y se
     puede volver a guardar: editarle el teléfono no le cambia la clasificación.
  5. El alta rechaza una letra que no está en la lista (el select la limita,
     pero un POST a mano no).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.proveedores import tipos  # noqa: E402
from modules.proveedores import views as prov_views  # noqa: E402
from tests.test_routes_smoke import ALL_PERMS, _make_fake_user, build_app  # noqa: E402

TPL = Path("modules/proveedores/templates/proveedores")

PROVEEDOR_M = {
    "id_proveedor": 77,
    "codigo_prov": "COLH",
    "nombre": "COLHILAZA",
    "ruc": None,
    "telefono": None,
    "representante": None,
    "tipo": "M",          # letra vieja, fuera de la lista de la pantalla
    "plazo": 30,
    "retbase": None,
    "retiva": None,
    "direccion": None,
    "correo": None,
    "activo": "1",
}


class _FakeQueries:
    """Stub de modules.proveedores.queries."""

    def __init__(self):
        self.creado = None
        self.editado = None

    def por_codigo(self, codigo_prov):
        return dict(PROVEEDOR_M) if codigo_prov == PROVEEDOR_M["codigo_prov"] else None

    def crear(self, **kwargs):
        self.creado = kwargs
        return 1

    def editar(self, codigo_prov, **kwargs):
        self.editado = kwargs
        return 1


@pytest.fixture
def cliente_y_queries():
    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_falso():  # pragma: no cover - infraestructura del test
        from flask import g
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    fake = _FakeQueries()
    previo = prov_views.queries
    prov_views.queries = fake
    try:
        yield app.test_client(), fake
    finally:
        prov_views.queries = previo
        deshacer()


def _opciones(html: str) -> list[tuple[str, str]]:
    """(value, texto) de cada <option> del select de tipo."""
    select = re.search(r'<select name="tipo".*?</select>', html, re.S)
    assert select, "la pantalla no tiene el desplegable de tipo"
    return re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', select.group(0))


def test_el_alta_ofrece_los_seis_tipos_con_su_nombre(cliente_y_queries):
    """U · Máquina, H · Hilado, Q · Químicos, B · Bancos, Y · Otro, K · Tejido."""
    cliente, _ = cliente_y_queries
    html = cliente.get("/proveedores/nuevo").get_data(as_text=True)
    ofrecidos = dict(_opciones(html))
    for codigo, nombre in tipos.TIPOS:
        assert ofrecidos.get(codigo) == f"{codigo} · {nombre}", (
            f"falta {codigo} ({nombre}) en el alta: {ofrecidos}"
        )


def test_el_alta_ya_no_ofrece_letras_que_no_usa_nadie(cliente_y_queries):
    """S, M e I eran tres de las cuatro opciones y no las tenía ningún proveedor.

    (B sí es válida — bancos — y por eso queda.)
    """
    cliente, _ = cliente_y_queries
    html = cliente.get("/proveedores/nuevo").get_data(as_text=True)
    valores = {v for v, _ in _opciones(html)}
    assert valores == {"", *tipos.CODIGOS}, valores


def test_las_tres_pantallas_leen_la_misma_lista():
    """Nadie vuelve a escribir las letras a mano en un template.

    Es lo que dejó las tres listas desincronizadas: el alta con B/S/M/I, el
    filtro con U/H/Q/B/Y/K y el lapicito con U/H/Q/B/Y.
    """
    for archivo in ("form.html", "lista.html"):
        texto = (TPL / archivo).read_text(encoding="utf-8")
        sueltas = re.findall(r"\[\s*\(?'[UHQBYKSMI]'", texto)
        assert not sueltas, f"{archivo} tiene una lista de tipos propia: {sueltas}"
        assert "tipos_opciones" in texto, f"{archivo} no lee la lista compartida"


def test_la_ayuda_de_la_columna_nombra_las_seis(cliente_y_queries):
    """El globito de la columna Tipo decía cinco y se comía K=Tejido."""
    assert tipos.AYUDA == (
        "U=Máquina · H=Hilado · Q=Químicos · B=Bancos · Y=Otro · K=Tejido"
    )
    for archivo in ("form.html", "lista.html"):
        texto = (TPL / archivo).read_text(encoding="utf-8")
        assert "U=Máquina · H=Hilado" not in texto, f"{archivo} repite la ayuda a mano"


def test_editar_un_proveedor_con_tipo_viejo_lo_deja_elegido(cliente_y_queries):
    """COLHILAZA es tipo M. Abrir su ficha no puede perder esa letra."""
    cliente, _ = cliente_y_queries
    html = cliente.get("/proveedores/COLH/editar").get_data(as_text=True)
    select = re.search(r'<select name="tipo".*?</select>', html, re.S).group(0)
    assert re.search(r'<option value="M"\s+selected>', select), select


def test_guardar_ese_proveedor_no_le_cambia_el_tipo(cliente_y_queries):
    """Editarle el teléfono a un tipo M no lo reclasifica ni lo rechaza."""
    cliente, fake = cliente_y_queries
    r = cliente.post(
        "/proveedores/COLH/editar",
        data={"nombre": "COLHILAZA", "tipo": "M", "telefono": "099"},
    )
    assert r.status_code == 302, r.get_data(as_text=True)[:400]
    assert fake.editado["tipo"] == "M"


def test_el_alta_rechaza_una_letra_que_no_esta_en_la_lista(cliente_y_queries):
    """El select la limita, pero un POST a mano no: el servidor también corta."""
    cliente, fake = cliente_y_queries
    r = cliente.post(
        "/proveedores/nuevo", data={"codigo_prov": "ZZZ", "nombre": "PRUEBA", "tipo": "S"}
    )
    assert r.status_code == 400
    assert fake.creado is None
    assert "no está en la lista" in r.get_data(as_text=True)


def test_el_alta_acepta_las_seis(cliente_y_queries):
    cliente, fake = cliente_y_queries
    for codigo in tipos.CODIGOS:
        r = cliente.post(
            "/proveedores/nuevo",
            data={"codigo_prov": "ZZZ", "nombre": "PRUEBA", "tipo": codigo},
        )
        assert r.status_code == 302, codigo
        assert fake.creado["tipo"] == codigo


def test_es_valido():
    assert tipos.es_valido("") and tipos.es_valido(None)
    assert tipos.es_valido("u") and tipos.es_valido("K")
    assert not tipos.es_valido("M")
    assert not tipos.es_valido("S")
