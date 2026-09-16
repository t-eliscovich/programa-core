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
    monkeypatch.setattr(vinculos_totalizar, "asegurar_tabla", lambda *a, **k: True)

    assert vinculos_totalizar.de_la_factura(276593) == [{"id_cheque": 102222}]
    assert "t.id_fact = %s" in espia.sql
    assert espia.params == (276593,)

    vinculos_totalizar.del_cheque(102222)
    assert "t.id_cheque = %s" in espia.sql
    assert espia.params == (102222,)


def test_el_backfill_solo_trae_los_totalizar_con_detalle(monkeypatch):
    """Las 8 corridas previas al 19/08 no guardaron `links` (15 vínculos), y
    sin el filtro de `tipo` entraría cualquier movimiento con esa clave."""
    espia = _Espia()
    monkeypatch.setattr(db, "execute", espia)
    vinculos_totalizar.backfill_desde_metadata()
    assert "m.tipo = 'totalizar_estado_cuenta'" in espia.sql
    assert "m.metadata ? 'links'" in espia.sql
    # Sin esto, el backfill duplicaría en cada arranque del proceso.
    assert "ON CONFLICT DO NOTHING" in espia.sql


def test_un_vinculo_que_volvio_a_aplicarse_no_es_historial(monkeypatch):
    """Si el par cheque↔factura está vivo en `chequesxfact`, ya se ve arriba.

    Mostrarlo también acá sería la MISMA aplicación dos veces en la misma
    ficha, una de ellas diciendo que se soltó.
    """
    espia = _Espia()
    monkeypatch.setattr(db, "fetch_all", espia)
    monkeypatch.setattr(vinculos_totalizar, "asegurar_tabla", lambda *a, **k: True)
    vinculos_totalizar.del_cheque(1)
    assert "NOT EXISTS" in espia.sql
    assert "scintela.chequesxfact x" in espia.sql


def test_si_la_consulta_falla_la_ficha_igual_abre(monkeypatch):
    """Es historial, no plata: un error acá no puede voltear la ficha."""
    def _explota(*a, **k):
        raise RuntimeError("se cayó la base")

    monkeypatch.setattr(db, "fetch_all", _explota)
    monkeypatch.setattr(vinculos_totalizar, "asegurar_tabla", lambda *a, **k: True)
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


# ── El antes/después del totalizar, en el estado de cuenta ──────────────────
# TMT 2026-09-16 (dueña, sobre la tabla que le armé por chat): *"esta tablita
# la quiero ver en el estado de cuenta en algún lado, dónde se ve en el
# programa"*. No se veía en ninguna pantalla.
EC_PAGINA = (RAIZ / "modules/informes/templates/informes/estado_cuenta.html").read_text(
    encoding="utf-8")
EC_IMPRESO = (
    RAIZ / "modules/informes/templates/informes/_estado_cuenta_impreso.html"
).read_text(encoding="utf-8")

_MD_CORRIDA = {
    "codigo_cli": "MTM", "n_facturas": 2, "n_links_borrados": 1,
    "links": [{"id_cheque": 102222, "id_fact": 276593, "importe": 536.30}],
    "antes": [
        {"id": 276593, "numf": "001-099-000177617", "stat": "A",
         "abono": 666.30, "saldo": 1392.98, "importe": 2059.28},
        {"id": 281470, "numf": "001-099-000181773", "stat": "Z",
         "abono": 0.0, "saldo": 173.01, "importe": 173.01},
    ],
    "despues": [
        {"id": 276593, "stat": "T", "abono": 2059.28, "saldo": 0.0},
        {"id": 281470, "stat": "Z", "abono": 0.0, "saldo": 173.01},
    ],
}


def _corrida(monkeypatch, metadata=None):
    espia = _Espia([{
        "id_mov_doble": 34558, "fecha": "2026-09-09", "usuario": "alex",
        "pool": 3988.62,
        "metadata": _MD_CORRIDA if metadata is None else metadata,
    }])
    monkeypatch.setattr(db, "fetch_all", espia)
    return espia


def test_el_antes_y_el_despues_se_mergean_por_factura(monkeypatch):
    """El `despues` sólo trae lo que cambió: sin el merge, la tabla sale sin
    importe ni número y no se entiende qué pasó."""
    _corrida(monkeypatch)
    corridas = vinculos_totalizar.corridas_del_cliente("mtm")
    assert len(corridas) == 1
    f = corridas[0]["facturas"][0]
    assert f["numf"] == "001-099-000177617"
    assert (f["abono_antes"], f["saldo_antes"], f["stat_antes"]) == (666.30, 1392.98, "A")
    assert (f["abono_despues"], f["saldo_despues"], f["stat_despues"]) == (2059.28, 0.0, "T")
    assert f["cambio"] is True
    # La que quedó igual se marca, para poder pintarla en gris.
    assert corridas[0]["facturas"][1]["cambio"] is False


def test_cuenta_los_cheques_que_solto_cada_factura(monkeypatch):
    """La otra mitad de la historia: qué cobro pagaba esa factura."""
    _corrida(monkeypatch)
    facturas = vinculos_totalizar.corridas_del_cliente("MTM")[0]["facturas"]
    assert facturas[0]["cheques_sueltos"] == 1
    assert facturas[1]["cheques_sueltos"] == 0


def test_el_codigo_del_cliente_va_en_mayuscula(monkeypatch):
    """Los códigos se guardan en mayúscula; buscar 'mtm' devolvía vacío."""
    espia = _corrida(monkeypatch)
    vinculos_totalizar.corridas_del_cliente("mtm")
    assert espia.params == ("MTM",)


def test_una_corrida_sin_detalle_igual_aparece(monkeypatch):
    """Las 8 previas al 19/08 no guardaron antes/después. Que la fila exista es
    lo que explica por qué esa cuenta se ve así."""
    _corrida(monkeypatch, metadata={"codigo_cli": "MTM", "n_facturas": 4})
    c = vinculos_totalizar.corridas_del_cliente("MTM")[0]
    assert c["facturas"] == []
    assert c["n_facturas"] == 4
    assert "De esta corrida no quedó guardado el detalle" in EC_PAGINA


def test_un_totalizar_deshecho_no_se_muestra(monkeypatch):
    """Si lo deshicieron con el ↺, la cuenta volvió atrás: mostrar el reparto
    sería contar algo que ya no pasó."""
    espia = _corrida(monkeypatch)
    vinculos_totalizar.corridas_del_cliente("MTM")
    assert "estado <> 'reversado'" in espia.sql


def test_si_falla_el_estado_de_cuenta_igual_abre(monkeypatch):
    def _explota(*a, **k):
        raise RuntimeError("se cayó la base")

    monkeypatch.setattr(db, "fetch_all", _explota)
    assert vinculos_totalizar.corridas_del_cliente("MTM") == []


def test_el_bloque_no_se_mete_en_la_hoja_del_cliente():
    """⚠ El parcial impreso lo comparten la hoja de la oficina, el portal del
    cliente y /mi-cartera: esto es cocina interna y va FUERA, y no-print."""
    assert "totalizares" not in EC_IMPRESO
    assert "Se totalizó el" in EC_PAGINA
    bloque = EC_PAGINA[EC_PAGINA.index("{% if totalizares %}"):]
    bloque = bloque[:bloque.index("{% include")]
    assert 'class="no-print' in bloque


def test_el_estado_de_cuenta_lo_manda_al_template():
    import modules.informes.views as _v
    fuente = inspect.getsource(_v.estado_cuenta)
    assert "corridas_del_cliente" in fuente
    assert "totalizares=" in fuente


# ── El vínculo NO se pierde ────────────────────────────────────────────────
# TMT 2026-09-16 (dueña): *"puede totalizarse, pero no que se pierda qué
# cheque/depósito/transferencia pagó qué factura"*.
import modules.informes.queries as _iq  # noqa: E402


def test_el_totalizar_guarda_los_vinculos_antes_de_borrarlos():
    """⚠ El orden importa: primero se copian, después se borran. Y el id del
    movimiento sale de `registrar`, no de un "último id"."""
    fuente = inspect.getsource(_iq.totalizar_estado_cuenta_ejecutar)
    assert "vinculos_totalizar" in fuente
    assert "_vt.guardar(" in fuente
    assert "id_mov_doble=int(id_mov)" in fuente
    # El SELECT de los links va antes del DELETE (si no, se copia lo que ya no está).
    assert fuente.index("SELECT " + "\" + _COLS_LINK_TOTALIZAR") < fuente.index(
        "DELETE FROM scintela.chequesxfact")


def test_deshacer_el_totalizar_borra_su_historial():
    """El ↺ repone los vínculos vivos: dejarlos también en el historial los
    mostraría dos veces, una diciendo que se soltaron."""
    fuente = inspect.getsource(_iq.totalizar_reverso_ejecutar)
    assert "olvidar_corrida" in fuente


def test_el_historial_vive_en_su_propia_tabla():
    """Los vínculos NO pueden volver a `chequesxfact`: el rewind de TOTF as-of
    y el health de sobre-aplicadas suman esa tabla, y contarían de más."""
    assert vinculos_totalizar.TABLA == "scintela.chequesxfact_totalizado"
    assert "CREATE TABLE IF NOT EXISTS" in vinculos_totalizar._DDL


def test_la_tabla_se_crea_sola(monkeypatch):
    """El deploy no corre migraciones: la tabla se bootstrapea en caliente."""
    hechas = []
    monkeypatch.setattr(vinculos_totalizar, "_listo", False)
    monkeypatch.setattr(db, "execute", lambda sql, *a, **k: hechas.append(sql) or 0)
    assert vinculos_totalizar.asegurar_tabla() is True
    assert any("CREATE TABLE IF NOT EXISTS" in s for s in hechas)
    assert any("jsonb_array_elements" in s for s in hechas), "falta el backfill"


def test_guardar_no_toca_la_base_si_no_hay_vinculos(monkeypatch):
    def _no(*a, **k):
        raise AssertionError("no tendría que escribir")

    monkeypatch.setattr(db, "execute", _no)
    assert vinculos_totalizar.guardar(None, id_mov_doble=1, fecha=None,
                                      codigo_cli="MTM", links=[]) == 0
