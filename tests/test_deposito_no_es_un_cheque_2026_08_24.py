"""Un depósito directo NO es un cheque — TMT 2026-08-24.

Tamara, mirando la ficha de MMQ llena de filas DEP.PICH.: *"cuando son
depósitos debería ir directo al banco, no pasar por cheques de clientes"*.

La plata SÍ va directo al banco: el depósito arma su movimiento en Pichincha
en el mismo momento de la cobranza. Lo que no era cierto era el RÓTULO —1.577
de las 4.584 filas de `scintela.cheque` no son cheques— y de ese rótulo
colgaban tres cosas que sí estaban rotas:

1. La columna N° del listado ofrecía escribir el número de un cheque que no
   existe, con el ID INTERNO de placeholder.
2. Se podía postergar un depósito y devolverlo a cartera. Así quedó vivo el
   cheque 102080 (MTM 536,30), que cobró una factura de mayo por segunda vez.
3. Un cobro NEGATIVO no dejaba asiento en el banco (ver
   `test_cobranza_dbase_paridad`), así que un −500 y un +500 se compensaban en
   la factura y dejaban +500 en Pichincha.
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques import queries as cq  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
LISTA = (RAIZ / "modules/cheques/templates/cheques/lista.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- los frenos

class _DBUno:
    """Devuelve UNA fila fija para el `SELECT ... FROM scintela.cheque`."""

    def __init__(self, fila):
        self.fila = fila
        self.executes: list[str] = []

    def fetch_one(self, sql, params=None, conn=None):
        return dict(self.fila)

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append(" ".join(sql.split()).lower())
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.executes.append(" ".join(sql.split()).lower())
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.mark.parametrize("no_banco", [90, 91])
def test_no_se_posterga_un_deposito(monkeypatch, no_banco):
    """Andrés postergó dos: MTM 536,30 (21 y 24/08) y RAR −500 (20/08).

    Postergar es "el cliente pide más tiempo antes de que llevemos el papel al
    banco". Un depósito no tiene papel ni fecha que correr.
    """
    stub = _DBUno({
        "id_cheque": 102080, "no_cheque": "", "stat": "P", "codigo_cli": "MTM",
        "fechad": date(2026, 8, 7), "importe": 536.30, "no_banco": no_banco,
    })
    monkeypatch.setattr(cq, "db", stub)
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **kw: None)
    with pytest.raises(ValueError) as exc:
        cq.postergar(id_cheque=102080, nueva_fechad=date(2026, 8, 25))
    assert "depósito directo" in str(exc.value)
    assert not any("update scintela.cheque" in s for s in stub.executes), (
        "el freno tiene que cortar ANTES de tocar la fila"
    )


@pytest.mark.parametrize("no_banco", [90, 91])
def test_no_se_vuelve_a_cartera_un_deposito(monkeypatch, no_banco):
    """La plata ya está en el banco: no hay cheque que volver al cajón."""
    stub = _DBUno({
        "id_cheque": 102222, "no_cheque": "", "stat": "B", "codigo_cli": "MTM",
        "importe": 536.30, "no_banco": no_banco,
    })
    monkeypatch.setattr(cq, "db", stub)
    with pytest.raises(ValueError) as exc:
        cq.deshacer_deposito_cheque(id_cheque=102222, usuario="tamara")
    assert "depósito directo" in str(exc.value)
    assert "error de carga" in str(exc.value), "el mensaje dice por dónde SÍ se hace"


def test_un_cheque_de_verdad_se_sigue_pudiendo_volver_a_cartera(monkeypatch):
    """El freno es ANGOSTO: mira el medio, no el estado.

    Un cheque de papel depositado por error vuelve a cartera como siempre —
    si el freno se comiera este caso, "no lo depositamos al final" dejaría de
    tener pantalla.
    """
    stub = _DBUno({
        "id_cheque": 900, "no_cheque": "4839", "stat": "B", "codigo_cli": "IIA",
        "importe": 100.0, "no_banco": 10,
    })
    monkeypatch.setattr(cq, "db", stub)
    # Llega más lejos que el freno del medio (después se cae por otra cosa del
    # stub, que es justo lo que prueba que pasó de largo).
    with pytest.raises(Exception) as exc:  # noqa: B017 — cualquier cosa MENOS el freno
        cq.deshacer_deposito_cheque(id_cheque=900, usuario="tamara")
    assert "depósito directo" not in str(exc.value)


# ------------------------------------------------------- la fila dice qué es

def test_los_medios_sin_numero_son_los_que_no_tienen_papel():
    """90/91 depósito, 99 efectivo, 98 espejo del saldo a favor.

    El 97 (anticipo) queda AFUERA a propósito: puede tener un cheque detrás.
    """
    assert set(cq.MEDIOS_SIN_NUMERO) == {90, 91, 98, 99}
    assert 97 not in cq.MEDIOS_SIN_NUMERO


@pytest.mark.parametrize(
    "no_banco, banco, esperado",
    [
        (90, "DEP.PICH.", "Dep. Pich."),
        (91, "DEP. INTER.", "Dep. Inter."),
        (99, "EFECTIVO", "Efectivo"),
        (98, "UKN", "Saldo a favor"),
    ],
)
def test_la_fila_sin_papel_dice_el_medio(no_banco, banco, esperado):
    """El rótulo sale de `etiqueta_cobro` — la MISMA que nombra el Historial."""
    fila = {"no_cheque": "", "no_banco": no_banco, "banco_nombre": banco}
    assert cq.etiqueta_cobro(fila) == esperado


def test_la_columna_numero_muestra_el_medio_y_no_el_id_interno():
    """El `placeholder="{{ c.id_cheque }}"` sólo puede vivir dentro del `else`.

    Antes la celda mostraba "99600" —el id interno— en un campo editable, o
    sea invitaba a escribir el número de un papel que no existe. Es el mismo
    arreglo que se hizo en el Historial el 09/08.
    """
    assert "{% if c.medio %}" in LISTA
    pos_medio = LISTA.index("{% if c.medio %}")
    pos_placeholder = LISTA.index('placeholder="{{ c.id_cheque }}"')
    assert pos_medio < pos_placeholder, (
        "la rama del medio tiene que ir ANTES del input con el id de placeholder"
    )


# ------------------------------------------------------------ filtro de medio

def test_el_filtro_de_medio_usa_la_misma_particion_que_el_resumen_del_dia():
    """Una sola definición de "esto es un cheque".

    Si el dropdown y el bucket CHEQUES del resumen de cobranza se escriben por
    separado, tarde o temprano contestan cosas distintas para el mismo día.
    """
    assert cq.SQL_POR_MEDIO["cheques"] == cq.SQL_ES_CHEQUE
    assert "90, 91" in cq.SQL_POR_MEDIO["depositos"]
    assert "= 99" in cq.SQL_POR_MEDIO["efectivo"]


@pytest.mark.parametrize(
    "basura",
    ["'; DROP TABLE scintela.cheque; --", "todos", "", "1 OR 1=1", "no_banco = 90"],
)
def test_el_medio_es_un_enum_y_no_entra_texto_al_sql(basura):
    """La condición sale del diccionario o no se filtra — nunca se interpola.

    El `__COND_MEDIO__` del SQL se reemplaza por una condición ELEGIDA de
    `SQL_POR_MEDIO`; si la clave no está, el marcador queda en blanco. Nada de
    lo que llega por la URL toca la consulta.
    """
    assert cq.SQL_POR_MEDIO.get(basura.strip().lower()) is None


def test_solo_hay_tres_medios():
    assert set(cq.SQL_POR_MEDIO) == {"cheques", "depositos", "efectivo"}


VISTAS = (RAIZ / "modules/cheques/views.py").read_text(encoding="utf-8")


def test_la_pantalla_lista_solo_cheques_y_no_es_negociable():
    """*"dep pichincha no debería aparecer en cheques. solo en el banco"*.

    El medio va HARDCODEADO, no sale de `request.args`: no es un filtro con
    default, es lo que la pantalla ES. Un `?medio=depositos` que funcione
    vuelve a poner en Cheques lo que se decidió sacar.
    """
    assert 'medio = "cheques"' in VISTAS
    assert 'request.args.get("medio"' not in VISTAS
    assert 'name="medio"' not in LISTA, "tampoco hay dropdown que lo ofrezca"


def test_los_badges_de_las_pestanas_cuentan_lo_mismo_que_el_listado():
    """Si el conteo suma lo que el listado esconde, la pestaña miente.

    "Depositados" diría 3.000 y mostraría 1.400. Los cuatro conteos —el CASE
    por bucket y los tres sub-buckets— llevan la MISMA condición, importada de
    `queries.SQL_ES_CHEQUE`, no copiada.
    """
    assert VISTAS.count("queries.SQL_ES_CHEQUE") == 4


def test_el_total_de_arriba_sale_del_mismo_universo_que_las_filas():
    """`total_buscar` alimenta el número grande del encabezado."""
    assert "medio=medio," in VISTAS
    fuente = (RAIZ / "modules/cheques/queries.py").read_text(encoding="utf-8")
    _, _, cola = fuente.partition("def total_buscar(")
    assert "__COND_MEDIO__" in cola.split("\ndef ")[0]


def test_hay_un_renglon_que_dice_donde_estan_los_que_no_se_listan():
    """Esconder sin decir dónde fueron es cómo se lee "se perdieron"."""
    assert "su plata está en el banco y en la caja" in LISTA
    assert '/bancos/10' in LISTA and '/caja' in LISTA


def test_el_banco_no_ofrece_volver_a_cartera_un_deposito():
    """Al cerrar una operación se cierra también el link que lleva a ella.

    `deshacer_deposito_cheque` frena los 90/91; si el botón siguiera visible
    en /bancos, llevaría derecho a un error.
    """
    movs = (RAIZ / "modules/bancos/templates/bancos/movimientos.html").read_text(encoding="utf-8")
    assert "_es_dep_directo" in movs
    assert "not _es_dep_directo" in movs
    bancos_q = (RAIZ / "modules/bancos/queries.py").read_text(encoding="utf-8")
    assert "c.stat, c.no_banco," in bancos_q, (
        "sin traer no_banco el template no puede distinguir el medio"
    )
