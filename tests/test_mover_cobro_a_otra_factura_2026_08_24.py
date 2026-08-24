"""Mover un cobro de una factura a otra — la pantalla que faltaba (24/08/2026).

Había Desaplicar, pero no había con qué volver a aplicar: `/cheques/<id>/aplicar`
se borró el 20/07 por huérfana. El único camino era recargar el cobro por
Cobranza, y toda carga con DEP.PICH. acuña un movimiento de banco nuevo. Por eso
el 19/08 mudar $500 de RAR de una factura a otra terminó dejando $500 de más en
Pichincha: no fue un descuido de Alex, era la única salida que le dejábamos.

Lo que estos tests protegen: que mover **no toque el banco**, que no cruce
clientes, y que las dos mitades (desaplicar + aplicar) viajen en la MISMA
transacción — si sólo corriera la primera, la factura de origen se abre y el
cobro se pierde en el aire.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques import queries as cq  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
DETALLE = (RAIZ / "modules/cheques/templates/cheques/detalle.html").read_text(encoding="utf-8")
PANTALLA = (RAIZ / "modules/cheques/templates/cheques/mover_aplicacion.html").read_text(encoding="utf-8")
VISTAS = (RAIZ / "modules/cheques/views.py").read_text(encoding="utf-8")


class _DB:
    """Devuelve el cheque y la factura destino que le pidan; registra la tx."""

    def __init__(self, *, cli_cheque="RAR", cli_factura="RAR", numf=179364):
        self.cli_cheque = cli_cheque
        self.cli_factura = cli_factura
        self.numf = numf
        self.txs = 0

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            return {"id_factura": 101, "numf": self.numf, "codigo_cli": self.cli_factura}
        if "from scintela.cheque" in s:
            return {"id_cheque": 133, "no_cheque": "", "codigo_cli": self.cli_cheque}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    @contextlib.contextmanager
    def tx(self):
        self.txs += 1
        yield f"conn-{self.txs}"


@pytest.fixture
def mover(monkeypatch):
    """`mover_aplicacion` con las dos mitades espiadas."""

    def _armar(*, importe_desaplicado=500.0, **kw):
        stub = _DB(**kw)
        monkeypatch.setattr(cq, "db", stub)
        llamadas: list[tuple] = []

        def _fake_des(**k):
            llamadas.append(("desaplicar", k))
            return {
                "importe_desaplicado": importe_desaplicado,
                "saldo_factura_post": 264.99,
                "stat_factura_post": "A",
            }

        def _fake_apl(**k):
            llamadas.append(("aplicar", k))
            return {"total_aplicado": k["aplicaciones"][0]["importe"]}

        monkeypatch.setattr(cq, "desaplicar_factura", _fake_des)
        monkeypatch.setattr(cq, "aplicar_a_factura", _fake_apl)
        return stub, llamadas

    return _armar


# ------------------------------------------------------------------ el camino

def test_mueve_el_importe_a_la_factura_elegida(mover):
    stub, llamadas = mover()
    r = cq.mover_aplicacion(
        id_cheque=133, id_fact_origen=100, id_fact_destino=101,
        motivo="el cliente pidió que vaya a la de julio", usuario="tamara",
    )
    assert r["importe"] == 500.0
    assert r["id_fact_destino"] == 101
    assert [c[0] for c in llamadas] == ["desaplicar", "aplicar"], (
        "primero sale de la vieja, después entra en la nueva"
    )
    _, apl = llamadas[1]
    assert apl["aplicaciones"] == [{"id_fact": 101, "importe": 500.0}]


def test_las_dos_mitades_van_en_la_MISMA_transaccion(mover):
    """Si sólo corriera la primera, la factura de origen se abre y el cobro
    queda en el aire. Una sola `tx`, y las dos llamadas con ESE `conn`."""
    stub, llamadas = mover()
    cq.mover_aplicacion(id_cheque=133, id_fact_origen=100, id_fact_destino=101)
    assert stub.txs == 1
    conns = {k.get("conn") for _, k in llamadas}
    assert conns == {"conn-1"}, f"cada mitad abrió lo suyo: {conns}"


def test_el_cobro_depositado_se_puede_mover(mover):
    """Es EL caso: la plata ya entró al banco y hay que reimputarla.

    `aplicar_a_factura` rechaza los cheques ya depositados salvo que se lo
    pidan; sin esto la pantalla no serviría para lo único que le pedimos.
    """
    stub, llamadas = mover()
    cq.mover_aplicacion(id_cheque=133, id_fact_origen=100, id_fact_destino=101)
    assert llamadas[1][1]["permitir_depositado"] is True


# ------------------------------------------------------------------ los frenos

def test_no_cruza_de_cliente(mover):
    """Un cobro de RAR no puede cancelar la factura de otro: quedarían mal los
    dos estados de cuenta, cada uno por su lado y sin nada que los relacione."""
    stub, llamadas = mover(cli_cheque="RAR", cli_factura="MMQ")
    with pytest.raises(ValueError) as exc:
        cq.mover_aplicacion(id_cheque=133, id_fact_origen=100, id_fact_destino=101)
    assert "otro cliente" in str(exc.value)
    assert llamadas == [], "no se desaplica nada antes de fallar"


def test_no_mueve_a_la_misma_factura(mover):
    stub, llamadas = mover()
    with pytest.raises(ValueError) as exc:
        cq.mover_aplicacion(id_cheque=133, id_fact_origen=101, id_fact_destino=101)
    assert "misma" in str(exc.value)
    assert llamadas == []


def test_una_aplicacion_negativa_no_se_mueve(mover):
    """El −500 del 19/08 es una corrección contable, no un cobro. Moverlo
    dejaría mal las dos facturas."""
    stub, llamadas = mover(importe_desaplicado=-500.0)
    with pytest.raises(ValueError) as exc:
        cq.mover_aplicacion(id_cheque=133, id_fact_origen=100, id_fact_destino=101)
    assert "negativa" in str(exc.value)
    assert [c[0] for c in llamadas] == ["desaplicar"], (
        "falla ANTES de aplicar; la tx se cae entera y el desaplicar no queda"
    )


# --------------------------------------------------- mover NO toca el banco

def test_mover_no_toca_el_banco():
    """El corazón del arreglo: mover NO acuña un movimiento bancario.

    Recargar el cobro por Cobranza sí lo hace, y por eso el 19/08 Pichincha
    quedó $500 arriba. El candado va por AST y no por texto: buscar la cadena
    "insert_movimiento_bancario" se escapa si alguien la llama por un alias.
    """
    arbol = ast.parse(inspect.getsource(cq.mover_aplicacion))
    llamadas = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
        for n in ast.walk(arbol) if isinstance(n, ast.Call)
    }
    for prohibida in ("insert_movimiento_bancario", "insert_movimiento_caja",
                      "compensar_deposito_devuelto", "transicionar_stat"):
        assert prohibida not in llamadas, f"mover no puede llamar a {prohibida}"


def test_tampoco_cambia_el_estado_del_cobro():
    """El cheque sigue depositado, en cartera o donde estuviera."""
    fuente = inspect.getsource(cq.mover_aplicacion)
    assert "UPDATE scintela.cheque" not in fuente
    assert "update scintela.cheque" not in fuente.lower()


# ------------------------------------------------------- el picker de destino

def test_el_picker_saca_la_de_origen_las_anuladas_y_las_sin_saldo(monkeypatch):
    capturado = {}

    class _Cap:
        def fetch_all(self, sql, params=None, conn=None):
            capturado["sql"] = " ".join(sql.split())
            capturado["params"] = params
            return []

    monkeypatch.setattr(cq, "db", _Cap())
    cq.facturas_destino_para_mover(" rar ", 100)
    sql = capturado["sql"]
    assert "id_factura <> %s" in sql
    assert "ABS(COALESCE(saldo, 0)) > 0.005" in sql
    assert "<> 'Y'" in sql, "las anuladas no son destino"
    assert "ORDER BY fecha ASC" in sql, "de la más vieja a la más nueva, como el FIFO"
    assert capturado["params"] == ("RAR", 100), "el código va normalizado"


# ------------------------------------------------------------ dónde se entra

def test_el_boton_esta_al_lado_de_desaplicar_en_la_ficha():
    """Quien mira la aplicación equivocada tiene ahí las DOS salidas."""
    assert "cheques.mover_aplicacion" in DETALLE
    assert DETALLE.index("cheques.mover_aplicacion") < DETALLE.index(
        "cheques.confirmar_desaplicar"
    ), "Mover va primero: es la salida que resuelve, Desaplicar sólo saca"


def test_la_ruta_esta_gateada_por_la_operacion():
    """Gatear por OPERACIÓN, no por rol. Mismo permiso que el botón."""
    assert '@requiere_permiso("cheques.aplicar")\ndef mover_aplicacion' in VISTAS


def test_la_ruta_existe_de_verdad_en_el_url_map(app):
    """El botón de la ficha se arma con `url_for`: si la ruta no estuviera
    registrada, la ficha ENTERA reventaría al renderizar. Esto lo caza acá y
    no en la cara de Alex."""
    reglas = {r.endpoint: str(r) for r in app.url_map.iter_rules()}
    assert "cheques.mover_aplicacion" in reglas
    assert reglas["cheques.mover_aplicacion"] == (
        "/cheques/<int:id_cheque>/mover/<int:id_factura>"
    )


def test_la_pantalla_nombra_el_medio_y_no_el_id_interno():
    """Un depósito se presenta como "Dep. Pich.", no como "Cobro #102758".

    `por_id` devuelve el nombre del banco en `banco_texto` y `etiqueta_cobro`
    lo lee de `banco_nombre`: sin traducir la llave, la etiqueta caía al id
    interno — el mismo pecado que esta sesión fue a corregir.
    """
    assert 'ch.get("banco_texto")' in VISTAS
    assert cq.etiqueta_cobro(
        {"no_cheque": "", "no_banco": 90, "banco_nombre": "DEP.PICH."}
    ) == "Dep. Pich."


def test_la_pantalla_dice_que_la_plata_no_se_mueve():
    """Es lo que la gente teme al leer "mover un cobro"."""
    assert "La plata no se mueve" in PANTALLA


def test_sin_facturas_destino_la_pantalla_no_se_queda_muda():
    """Un formulario vacío se lee como que la pantalla está rota."""
    assert "{% else %}" in PANTALLA
    assert "no tiene otra factura con saldo" in PANTALLA
