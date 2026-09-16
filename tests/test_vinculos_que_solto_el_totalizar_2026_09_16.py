"""El vínculo que borra el TOTALIZAR se tiene que poder encontrar.

TMT 2026-09-16. La dueña abrió la factura 177617 de MTM —cancelada, abono
2.059,28— y no vio UN solo cheque aplicado: *"ya no hay nada aplicado"*. No
era un bug: el TOTALIZAR del 09/09 re-liquidó la cuenta y borró los vínculos
de `chequesxfact`, que es lo que hace desde el 06/07 (después del reparto el
vínculo viejo apunta a una factura que ese cheque ya no paga).

Su respuesta: *"no borres vínculos!!"* y, cuando se le mostró la tensión,
**"o sea totalizamos pero si quiero puedo encontrar el vínculo"**. Así que el
totalizar sigue igual y el vínculo se muestra como HISTORIAL en las dos
fichas, leído de lo que el propio totalizar dejó dormido en
`mov_doble.metadata->'links'`.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import db
from modules._lib import vinculos_totalizar

RAIZ = Path(__file__).resolve().parent.parent
FICHA_FACT = (RAIZ / "modules/facturas/templates/facturas/detalle.html").read_text(
    encoding="utf-8")
FICHA_CH = (RAIZ / "modules/cheques/templates/cheques/detalle.html").read_text(
    encoding="utf-8")


class _Espia:
    def __init__(self, filas=()):
        self.sql = None
        self.params = None
        self.filas = list(filas)

    def __call__(self, sql, params=None, conn=None):
        self.sql = " ".join(sql.split())
        self.params = params
        return self.filas


def test_la_factura_pregunta_por_su_id_y_el_cheque_por_el_suyo(monkeypatch):
    """Cada ficha filtra por SU lado del vínculo — cruzarlos trae otro caso."""
    espia = _Espia([{"id_cheque": 102222}])
    monkeypatch.setattr(db, "fetch_all", espia)

    assert vinculos_totalizar.de_la_factura(276593) == [{"id_cheque": 102222}]
    assert "(l->>'id_fact')::bigint = %s" in espia.sql
    assert espia.params == (276593,)

    vinculos_totalizar.del_cheque(102222)
    assert "(l->>'id_cheque')::bigint = %s" in espia.sql
    assert espia.params == (102222,)


def test_solo_mira_los_totalizar_que_guardaron_el_detalle(monkeypatch):
    """Las 8 corridas previas al 19/08 no guardaron `links` (15 vínculos).

    Sin el `metadata ? 'links'`, el LATERAL sobre un jsonb inexistente no
    devuelve fila —pero tampoco hay que salir a buscarlas—, y sin el filtro de
    `tipo` entraría cualquier movimiento que tenga una clave `links`.
    """
    espia = _Espia()
    monkeypatch.setattr(db, "fetch_all", espia)
    vinculos_totalizar.de_la_factura(1)
    assert "m.tipo = 'totalizar_estado_cuenta'" in espia.sql
    assert "m.metadata ? 'links'" in espia.sql


def test_un_vinculo_que_volvio_a_aplicarse_no_es_historial(monkeypatch):
    """Si el par cheque↔factura está vivo en `chequesxfact`, ya se ve arriba.

    Mostrarlo también acá sería la MISMA aplicación dos veces en la misma
    ficha, una de ellas diciendo que se soltó.
    """
    espia = _Espia()
    monkeypatch.setattr(db, "fetch_all", espia)
    vinculos_totalizar.del_cheque(1)
    assert "NOT EXISTS" in espia.sql
    assert "scintela.chequesxfact x" in espia.sql


def test_si_la_consulta_falla_la_ficha_igual_abre(monkeypatch):
    """Es historial, no plata: un error acá no puede voltear la ficha."""
    def _explota(*a, **k):
        raise RuntimeError("se cayó la base")

    monkeypatch.setattr(db, "fetch_all", _explota)
    assert vinculos_totalizar.de_la_factura(1) == []
    assert vinculos_totalizar.del_cheque(1) == []


@pytest.mark.parametrize("modulo,funcion", [
    ("modules.facturas.views", "detalle"),
    ("modules.cheques.views", "detalle"),
])
def test_las_dos_fichas_lo_mandan_al_template(modulo, funcion):
    """⚠ El template lee `vinculos_viejos`: si la vista no lo manda, Jinja no
    avisa —renderiza el bloque vacío— y el historial no aparece nunca."""
    mod = __import__(modulo, fromlist=[funcion])
    fuente = inspect.getsource(getattr(mod, funcion))
    assert "vinculos_totalizar" in fuente
    assert "vinculos_viejos=vinculos_viejos" in fuente


def test_el_bloque_solo_sale_cuando_hay_algo_que_mostrar():
    """Una ficha no se llena de bloques vacíos (misma regla que las tablas)."""
    assert "{% if vinculos_viejos %}" in FICHA_FACT
    assert "{% if vinculos_viejos %}" in FICHA_CH
    assert "Cheques que soltó el totalizar" in FICHA_FACT
    assert "Facturas que soltó el totalizar" in FICHA_CH


def test_el_bloque_aclara_que_la_plata_ya_esta_contada():
    """Sin esa frase, el bloque se lee como un cobro que falta aplicar."""
    assert "la plata sigue contada en el abono" in FICHA_FACT
    assert "la plata sigue contada en el abono de cada factura" in FICHA_CH


def test_el_cheque_no_dice_que_no_se_aplico_cuando_el_totalizar_lo_solto():
    """Decirle "todavía no se aplicó" a un cheque que SÍ pagó una factura es
    lo que mandó a la dueña a buscar dónde estaba el cobro."""
    assert "El totalizar de la cuenta soltó su factura" in FICHA_CH


def test_un_deposito_no_muestra_el_id_interno_como_numero_de_cheque():
    """16/09: buscó el 102222 en el campo "Cheque" y no aparecía — ese número
    es la PK interna y el depósito no tiene número de papel."""
    bloque = FICHA_FACT[FICHA_FACT.index("Cheques que soltó el totalizar"):]
    bloque = bloque[:bloque.index("</table>")]
    assert "v.no_cheque or 'sin número'" in bloque
    assert "v.no_cheque or v.id_cheque" not in bloque


def test_las_dos_puntas_del_vinculo_son_link():
    """Dueña 16/09: *"debería haber el link de factura y cheque"*. El vínculo
    tiene dos puntas y desde cualquiera de las dos fichas se salta a la otra."""
    for ficha, titulo in ((FICHA_FACT, "Cheques que soltó el totalizar"),
                          (FICHA_CH, "Facturas que soltó el totalizar")):
        bloque = ficha[ficha.index(titulo):]
        bloque = bloque[:bloque.index("</table>")]
        assert "url_for('cheques.detalle', id_cheque=v.id_cheque)" in bloque
        assert "url_for('facturas.detalle'" in bloque
        # Un link que no se ve azul no se clickea.
        assert bloque.count('class="text-blue-600 hover:underline"') >= 2
