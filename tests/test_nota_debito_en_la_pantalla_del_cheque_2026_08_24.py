"""La nota de débito se emite en la MISMA pantalla que el cheque.

TMT 2026-08-24 (dueña): *"cuando emitimos nota de débito, tiene que ser igual
que emitir cheque, misma pantalla. sin numero de cheque"*.

Hasta hoy la ND tenía pantalla propia (`/bancos/nuevo-movimiento?doc=ND`) y ahí
los destinos se pedían tipeando el concepto en formato mágico: "INOP AI 11"
para dejarlo a favor del proveedor, "RR TM" para un retiro, "CAJA…" para la
caja. La de emitir cheque, en cambio, los pide con tarjetas. Es el MISMO acto
—sale plata del banco y va a algún lado—, así que quedó una sola pantalla:
`?doc=ND` cambia el documento y esconde el número de cheque, nada más.

Lo que este archivo protege:
  · la ND sale con documento='ND' y SIN numreferencia (el número lo pone el
    banco, no nosotros);
  · los dos destinos que sólo existían en la pantalla vieja (posdato/INOP y el
    anticipo a proveedor) siguen estando;
  · el cheque a un proveedor sin posdatado ya no se pierde: graba la compra,
    como venía haciendo la ND;
  · los dos frenos de la pantalla vieja (fecha vieja y repetido) valen para los
    dos documentos;
  · el reverso de la ND sigue deshaciendo su contraparte.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

NB = 10          # Pichincha
PROV = "SMK"
USUARIO = "test-nd"


@pytest.fixture
def base(real_db_conn, migrated_db):
    from filters import today_ec
    real_db_conn.autocommit = True
    cur = real_db_conn.cursor()
    # Sin ON CONFLICT: estas tablas vienen del dBase y no tienen unique.
    cur.execute(
        "INSERT INTO scintela.banco (no_banco, nombre) "
        "SELECT %s, 'PICHINCHA' WHERE NOT EXISTS "
        "  (SELECT 1 FROM scintela.banco WHERE no_banco = %s)", (NB, NB))
    cur.execute(
        "INSERT INTO scintela.proveedor (codigo_prov, nombre, tipo, activo) "
        "SELECT %s, 'Proveedor de prueba', 'H', '1' WHERE NOT EXISTS "
        "  (SELECT 1 FROM scintela.proveedor WHERE codigo_prov = %s)",
        (PROV, PROV))
    cur.execute("DELETE FROM scintela.transacciones_bancarias "
                "WHERE usuario_crea = %s", (USUARIO,))
    cur.execute("DELETE FROM scintela.compra WHERE usuario_crea = %s", (USUARIO,))
    cur.execute("DELETE FROM scintela.posdat WHERE usuario_crea = %s", (USUARIO,))
    return real_db_conn, today_ec()


def _emitir(**kw):
    from modules.bancos import queries as bq
    datos = {"tipo": "otro", "no_banco": NB, "importe": 100.0,
             "usuario": USUARIO, "concepto": "PRUEBA ND"}
    datos.update(kw)
    return bq.emitir_cheque(**datos)


def _tx(conn, id_transaccion):
    cur = conn.cursor()
    cur.execute(
        "SELECT documento, numreferencia, importe FROM "
        "scintela.transacciones_bancarias WHERE id_transaccion = %s",
        (id_transaccion,))
    doc, numref, importe = cur.fetchone()
    return (doc or "").strip().upper(), numref, float(importe)


@pytest.mark.db
def test_la_nota_de_debito_sale_sin_numero_de_cheque(base):
    """⭐ EL PEDIDO: mismo camino que el cheque, sin número."""
    conn, hoy = base
    r = _emitir(documento="ND", fecha=hoy, concepto="COMISIONES")

    doc, numref, importe = _tx(conn, r["id_transaccion"])
    assert doc == "ND"
    assert numref is None, "la nota de débito no lleva número: lo pone el banco"
    assert importe == 100.0
    assert r["no_cheque"] == ""


@pytest.mark.db
def test_el_numero_tipeado_no_se_cuela_en_la_nota_de_debito(base):
    """🪤 El form esconde el campo, pero el POST puede traerlo igual."""
    conn, hoy = base
    r = _emitir(documento="ND", fecha=hoy, no_cheque="9999")

    _doc, numref, _importe = _tx(conn, r["id_transaccion"])
    assert numref is None, (
        "quedó con número de cheque: la conciliación matchea por documento y "
        "número, y una ND con número de cheque matchea contra el cheque que no es"
    )


@pytest.mark.db
def test_el_cheque_sigue_saliendo_con_su_numero(base):
    """El camino de siempre no se toca."""
    conn, hoy = base
    r = _emitir(fecha=hoy, no_cheque="5001")

    doc, numref, _importe = _tx(conn, r["id_transaccion"])
    assert doc == "CH"
    assert int(numref) == 5001


@pytest.mark.db
def test_el_posdato_nace_negativo_y_vence_a_120_dias(base):
    """El viejo "INOP AI 11", ahora una tarjeta."""
    conn, hoy = base
    r = _emitir(documento="ND", fecha=hoy, tipo="posdato",
                beneficiario="AI", importe=350.0, concepto="A FAVOR AI")

    cur = conn.cursor()
    cur.execute(
        "SELECT prov, importe, fechad, banc FROM scintela.posdat "
        " WHERE usuario_crea = %s ORDER BY id_posdat DESC LIMIT 1", (USUARIO,))
    prov, importe, fechad, banc = cur.fetchone()
    assert (prov or "").strip() == "AI"
    assert float(importe) == -350.0, "queda A FAVOR del proveedor: va en negativo"
    assert fechad == hoy + timedelta(days=120)
    assert int(banc or 0) == 0, "banc=0 es lo que la hace deuda viva"
    assert "120" in r["side_effect"]


@pytest.mark.db
def test_el_posdato_sin_proveedor_rebota_con_el_motivo(base):
    _conn, hoy = base
    with pytest.raises(ValueError, match="2 letras"):
        _emitir(documento="ND", fecha=hoy, tipo="posdato", beneficiario="")


@pytest.mark.db
def test_proveedor_sin_posdatado_graba_la_compra(base):
    """⭐ Decisión de la dueña al unificar: sin posdatado, se graba la compra.

    Antes la ND lo hacía (réplica de BANCOS.PRG ~195) y el cheque no: la plata
    salía del banco y no quedaba en Compras.
    """
    conn, hoy = base
    r = _emitir(fecha=hoy, tipo="proveedor", beneficiario=PROV, importe=55.0,
                no_cheque="5002", concepto="PAGO DIRECTO")

    cur = conn.cursor()
    cur.execute(
        "SELECT codigo_prov, tipo, importe FROM scintela.compra "
        " WHERE usuario_crea = %s ORDER BY id_compra DESC LIMIT 1", (USUARIO,))
    fila = cur.fetchone()
    assert fila is not None, "el pago no quedó registrado en ningún lado"
    codigo_prov, tipo_compra, importe = fila
    assert (codigo_prov or "").strip() == PROV
    assert tipo_compra == "H", "el tipo sale del proveedor (H hilos)"
    assert float(importe) == 55.0
    assert "Compra" in r["side_effect"]


@pytest.mark.db
def test_un_beneficiario_que_no_es_proveedor_no_inventa_compra(base):
    """El campo acepta texto libre: sin proveedor real, no se graba nada."""
    conn, hoy = base
    r = _emitir(fecha=hoy, tipo="proveedor", beneficiario="ZZZ",
                no_cheque="5003", importe=12.0)

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scintela.compra WHERE usuario_crea = %s",
                (USUARIO,))
    assert cur.fetchone()[0] == 0
    assert "sólo movimiento bancario" in r["side_effect"]


@pytest.mark.db
def test_el_repetido_se_pregunta_y_confirmado_entra(base):
    """Freno traído de la pantalla vieja de ND."""
    from modules.bancos import queries as bq

    conn, hoy = base
    _emitir(documento="ND", fecha=hoy, importe=64.73, concepto="COMISIONES")

    with pytest.raises(bq.MovimientoRepetido):
        _emitir(documento="ND", fecha=hoy, importe=64.73, concepto="COMISIONES")

    _emitir(documento="ND", fecha=hoy, importe=64.73, concepto="COMISIONES",
            permitir_duplicado=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM scintela.transacciones_bancarias "
        " WHERE usuario_crea = %s AND ROUND(importe::numeric,2) = 64.73",
        (USUARIO,))
    assert cur.fetchone()[0] == 2, "confirmado, el segundo tiene que entrar"


@pytest.mark.db
def test_dos_cheques_del_mismo_importe_con_numero_distinto_no_son_repetidos(base):
    """El número ES la firma del cheque: dos del mismo importe pasan derecho."""
    conn, hoy = base
    _emitir(fecha=hoy, importe=90.0, no_cheque="5100", concepto="TANDA")
    _emitir(fecha=hoy, importe=90.0, no_cheque="5101", concepto="TANDA")

    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM scintela.transacciones_bancarias "
        " WHERE usuario_crea = %s AND ROUND(importe::numeric,2) = 90.00",
        (USUARIO,))
    assert cur.fetchone()[0] == 2


@pytest.mark.db
def test_el_reverso_de_la_nota_de_debito_deshace_su_destino(base):
    """La ND sigue reversándose por donde siempre, y se lleva la contraparte."""
    from modules.bancos import queries as bq

    conn, hoy = base
    r = _emitir(documento="ND", fecha=hoy, tipo="posdato", beneficiario="AI",
                importe=200.0, concepto="A FAVOR AI")
    assert r["id_mov_doble"], "sin mov_doble no hay reverso posible"

    bq.reversar_movimiento_simple(id_mov_doble=r["id_mov_doble"],
                                  motivo="prueba", usuario=USUARIO)

    cur = conn.cursor()
    cur.execute("SELECT anulada FROM scintela.posdat WHERE id_posdat = %s",
                (r["id_posdat_inop"],))
    assert cur.fetchone()[0] is True, (
        "el reverso devolvió la plata al banco y dejó viva la posdat: "
        "la deuda queda contada dos veces"
    )


# ── La pantalla ───────────────────────────────────────────────────────────

class _FakeQueries:
    DOCS_EMITIBLES = ("CH", "ND")

    class MovimientoRepetido(Exception):
        pass

    def bancos_operativos(self):
        return [{"no_banco": NB, "nombre": "PICHINCHA"}]

    def lista_bancos(self):
        return self.bancos_operativos()

    def posdat_abiertas_de(self, prov=None):
        return []

    def conceptos_frecuentes_egresos(self, limite=50):
        return []

    def proveedores_activos(self, limite=500):
        return []

    def proveedores_op_saldos(self, limite=500):
        return []


@pytest.fixture
def cliente():
    from modules.bancos import views as bancos_views
    from tests.test_routes_smoke import ALL_PERMS, _make_fake_user, build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_falso():  # pragma: no cover - infraestructura del test
        from flask import g
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    previo = bancos_views.queries
    bancos_views.queries = _FakeQueries()
    try:
        yield app.test_client()
    finally:
        bancos_views.queries = previo
        deshacer()


def test_la_pantalla_de_nd_no_muestra_el_numero_de_cheque(cliente):
    """⭐ EL PEDIDO, del lado de la pantalla."""
    html = cliente.get("/bancos/emitir-cheque?doc=ND").get_data(as_text=True)

    assert 'name="no_cheque"' not in html, "la nota de débito no tiene número"
    assert "Emitir nota de débito" in html
    assert 'name="documento" value="ND"' in html, (
        "sin el hidden, el POST vuelve a grabar un cheque"
    )


def test_la_pantalla_del_cheque_sigue_pidiendo_el_numero(cliente):
    html = cliente.get("/bancos/emitir-cheque").get_data(as_text=True)

    assert 'name="no_cheque"' in html
    assert "Emitir cheque" in html


def test_los_dos_destinos_de_la_pantalla_vieja_estan_en_la_nueva(cliente):
    """Posdato (el viejo INOP) y el anticipo a proveedor, sin conceptos mágicos."""
    html = cliente.get("/bancos/emitir-cheque?doc=ND").get_data(as_text=True)

    assert 'value="posdato"' in html, "se perdió el destino INOP"
    assert 'value="anticipo_usd"' in html, "se perdió el anticipo a proveedor"


def test_el_link_viejo_de_nota_de_debito_lleva_a_la_pantalla_nueva(cliente):
    """Los favoritos y los links viejos no pueden quedar en una pantalla muerta."""
    r = cliente.get("/bancos/nuevo-movimiento?doc=ND")

    assert r.status_code == 302
    assert "/bancos/emitir-cheque" in r.headers["Location"]
    assert "doc=ND" in r.headers["Location"]


def test_el_deposito_y_la_nota_de_credito_siguen_donde_estaban(cliente):
    """Sólo se mudó la nota de débito."""
    for doc in ("DE", "NC"):
        r = cliente.get(f"/bancos/nuevo-movimiento?doc={doc}")
        assert r.status_code == 200, f"{doc} tenía que seguir abriendo acá"
