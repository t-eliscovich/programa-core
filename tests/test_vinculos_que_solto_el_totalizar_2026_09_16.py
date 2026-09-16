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


def test_las_totalizadas_van_EN_la_tabla_de_cheques_con_su_estado():
    """TMT 2026-09-16 (dueña, sobre la 177617): *"pone como estado totalizada y
    ponelo debajo de cheques"*. No es otra tabla: son los cheques que pagaron
    esa factura, abajo de los vivos y marcados."""
    for ficha in (FICHA_FACT, FICHA_CH):
        assert "{% for v in vinculos_viejos | default([]) %}" in ficha
        assert ">Totalizada</span>" in ficha
    # Y ya no hay tabla aparte.
    assert "Cheques que soltó el totalizar" not in FICHA_FACT
    assert "Facturas que soltó el totalizar" not in FICHA_CH


def test_las_totalizadas_cuentan_en_el_titulo_y_en_el_total():
    """La pregunta que contesta ese número es "¿quién pagó esta factura?", no
    "¿cuántos vínculos quedan vivos?"."""
    assert ("aplicaciones | length + (vinculos_viejos | default([])) | length") in FICHA_FACT
    assert ("total_aplicado + (total_totalizado | default(0))") in FICHA_FACT
    assert ("aplicaciones | length + (vinculos_viejos | default([])) | length") in FICHA_CH
    assert ("total_aplicado + (total_totalizado | default(0))") in FICHA_CH


def test_el_vacio_solo_si_no_hay_ninguna():
    """"Sin cheques aplicados" con dos filas totalizadas abajo era mentira."""
    assert "{% if not aplicaciones and not vinculos_viejos | default([]) %}" in FICHA_FACT
    assert "{% if aplicaciones or vinculos_viejos | default([]) %}" in FICHA_CH


def test_la_fila_explica_por_que_esta_ahi():
    """Sin la explicación, una fila gris "Totalizada" se lee como un cobro que
    falta aplicar. Va en el title de la fila, sin ocupar pantalla."""
    assert "La plata sigue contada en el abono." in FICHA_FACT
    assert "La plata sigue contada en el abono de la factura." in FICHA_CH


def test_el_cheque_no_dice_que_no_se_aplico_cuando_el_totalizar_lo_solto():
    """Decirle "todavía no se aplicó" a un cheque que SÍ pagó una factura es
    lo que mandó a la dueña a buscar dónde estaba el cobro."""
    assert "El totalizar de la cuenta soltó su factura" in FICHA_CH


def test_un_deposito_no_muestra_el_id_interno_como_numero_de_cheque():
    """16/09: buscó el 102222 en el campo "Cheque" y no aparecía — ese número
    es la PK interna y el depósito no tiene número de papel."""
    bloque = FICHA_FACT[FICHA_FACT.index("{% for v in vinculos_viejos | default([]) %}"):]
    bloque = bloque[:bloque.index("</tr>")]
    assert "v.no_cheque or v.doc_banco or '—'" in bloque
    assert "sin número" not in bloque, "un depósito tiene papeleta, no 'sin número'"


def test_la_fila_totalizada_linkea_a_la_otra_punta():
    """Dueña 16/09: *"debería haber el link de factura y cheque"*."""
    bloque = FICHA_FACT[FICHA_FACT.index("{% for v in vinculos_viejos | default([]) %}"):]
    assert "url_for('cheques.detalle', id_cheque=v.id_cheque)" in bloque[:bloque.index("</tr>")]
    bloque_ch = FICHA_CH[FICHA_CH.index("{% for v in vinculos_viejos | default([]) %}"):]
    assert "url_for('facturas.detalle'" in bloque_ch[:bloque_ch.index("</tr>")]


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


# ── "No debería volver a pasar": el totalizar re-aplica ─────────────────────
# TMT 2026-09-16 (dueña): *"no entiendo por qué pasó, arreglalo esta vez como
# puedas pero no debería volver a pasar"*.

def test_reparte_el_cobro_a_la_factura_mas_vieja_primero():
    """Mismo criterio que el totalizar: la plata vieja paga la deuda vieja."""
    plan = vinculos_totalizar.repartir(
        facturas=[{"id_fact": 1, "abono": 800.0}, {"id_fact": 2, "abono": 0.0}],
        cobros=[{"id_cheque": 10, "importe": 200.0},
                {"id_cheque": 11, "importe": 600.0}],
    )
    assert [(p["id_fact"], p["id_cheque"], p["importe"]) for p in plan] == [
        (1, 10, 200.0), (1, 11, 600.0)]


def test_un_cobro_se_parte_entre_dos_facturas():
    """No es un invento nuevo: `aplicar_a_factura` ya guarda una fila por
    aplicación, así que un cheque puede pagar dos facturas."""
    plan = vinculos_totalizar.repartir(
        facturas=[{"id_fact": 1, "abono": 100.0}, {"id_fact": 2, "abono": 400.0}],
        cobros=[{"id_cheque": 10, "importe": 500.0}],
    )
    assert [(p["id_fact"], p["importe"]) for p in plan] == [(1, 100.0), (2, 400.0)]


def test_nunca_aplica_mas_que_el_abono_ni_mas_que_el_cobro():
    """El health de facturas sobre-aplicadas suma esta tabla: pasarse de
    abono es prender una alarma real por un arreglo cosmético."""
    plan = vinculos_totalizar.repartir(
        facturas=[{"id_fact": 1, "abono": 50.0}],
        cobros=[{"id_cheque": 10, "importe": 500.0},
                {"id_cheque": 11, "importe": 300.0}],
    )
    assert sum(p["importe"] for p in plan) == 50.0
    assert len(plan) == 1, "el segundo cobro no tenía dónde entrar"


def test_el_abono_sin_cobro_queda_sin_vinculo():
    """Abono del backfill histórico / dbf-import: inventarle un cheque sería
    peor que dejarlo sin uno."""
    plan = vinculos_totalizar.repartir(
        facturas=[{"id_fact": 1, "abono": 900.0}],
        cobros=[{"id_cheque": 10, "importe": 200.0}],
    )
    assert sum(p["importe"] for p in plan) == 200.0


def test_el_totalizar_vuelve_a_aplicar_los_cobros():
    fuente = inspect.getsource(_iq.totalizar_estado_cuenta_ejecutar)
    assert "_vt.reaplicar(" in fuente
    # Los cobros entran por fecha: el de julio paga la factura de mayo.
    assert "fechaing" in fuente
    assert "n_links_reaplicados" in fuente


def test_no_le_repone_el_vinculo_a_un_cheque_anulado():
    """La aplicación fantasma que describe el reverso del totalizar: el abono
    deja de cuadrar y bloquea futuras anulaciones."""
    fuente = inspect.getsource(vinculos_totalizar.reaplicar)
    assert "COALESCE(stat, '') <> 'X'" in fuente


def test_el_boton_de_reponer_solo_sale_si_hay_algo_que_reponer():
    assert "{% if vinculos_por_reponer and tiene_permiso('estado_cuenta.totalizar') %}" in EC_PAGINA
    assert "Volver a ponerlos en sus facturas" in EC_PAGINA
    assert "no cambia ningún abono ni saldo" in EC_PAGINA


def test_reponer_pide_el_permiso_de_totalizar():
    """La ruta la ve quien ve la cuenta; escribir en chequesxfact no."""
    import modules.informes.views as _v
    fuente = inspect.getsource(_v.estado_cuenta_reponer_vinculos)
    assert 'tiene_permiso("estado_cuenta.totalizar")' in fuente
    assert "methods=[\"POST\"]" in inspect.getsource(_v).split(
        "def estado_cuenta_reponer_vinculos")[0][-400:]


def test_reponer_solo_mira_las_facturas_de_la_corrida():
    """⚠ Repartir sobre TODAS las facturas del cliente mandaría los cobros de
    agosto a las de 2022 del backfill de Asinfo —que arrastran abono sin
    vínculo desde siempre— y la factura que perdió su cheque seguiría sin él."""
    fuente = inspect.getsource(vinculos_totalizar.reponer_cliente)
    assert "SELECT DISTINCT t.id_fact" in fuente
    # Y sólo el abono que todavía no tiene vínculo vivo.
    assert "SUM(x.importe) FROM scintela.chequesxfact x" in fuente


def test_un_link_sin_fecha_no_voltea_la_reposicion():
    """`chequesxfact.fechaing` es NOT NULL y hay links viejos sin fecha: sin el
    respaldo, la reposición entera moría con un NotNullViolation (16/09)."""
    fuente = inspect.getsource(vinculos_totalizar.reaplicar)
    assert 'p.get("fechaing") or vivo.get("fechaing") or vivo.get("fecha")' in fuente


def test_el_form_de_reponer_lleva_su_csrf():
    """Sin el token, el POST lo come el CSRF y no pasa NADA —ni error ni
    aviso—: el botón parece roto. Apretado en producción el 16/09."""
    bloque = EC_PAGINA[EC_PAGINA.index("estado_cuenta_reponer_vinculos"):]
    bloque = bloque[:bloque.index("</form>")]
    assert 'name="csrf_token"' in bloque
