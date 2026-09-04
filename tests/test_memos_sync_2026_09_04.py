"""Sync de memos MODIFICADOS en Asinfo (David, audio 04/09/2026): si Irene
toca el pedido después de mandar el memo, la foto en formulas_app se pisa
y queda la alerta "modificado — revisar" con qué cambió.

`fetch_dataset_estado` se pisa con un fake que contesta según la SQL
(la de `pedido_cliente.fecha_modificacion` o la de `por_pedido`);
`formulas_memos.vivos/actualizar` se pisan directo.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from modules._lib import formulas_memos
from modules.pedidos import memos_sync, service


@pytest.fixture(autouse=True)
def _sin_cache():
    service.reset_cache()
    yield
    service.reset_cache()


_VENDEDORES = {
    frozenset({"PATRICIO", "PROANO"}): {"codigo": "PPR", "nombre": "Patricio Proano"},
}


def _fila(**kw):
    base = {
        "numero": "PDCL-30949", "fecha": "2026-09-01",
        "cliente": "CONFECCIONES NOVA", "codigo_cliente": "CNV",
        "agente_id": 751, "agente_nombre": "Proaño Patricio",
        "descripcion": "", "codigo": "FE96HAB", "color": "HAB",
        "tela": "Fleece 96 Perchado", "cantidad": 17.0, "unidad": 51,
    }
    base.update(kw)
    return base


def _foto_vieja():
    return {
        "numero": "PDCL-30949", "fecha": "2026-09-01",
        "cliente": {"codigo": "CNV", "nombre": "CONFECCIONES NOVA"},
        "vendedor": {"codigo": "PPR", "nombre": "Patricio Proano"},
        "descripcion": "",
        "lineas": [
            {"producto": "FE96HAB", "tela": "Fleece 96 Perchado", "color": "HAB",
             "cantidad": 12.0, "unidad": "roll", "kg": 282},
            {"producto": "RB96NEG", "tela": "Rib 96", "color": "NEG",
             "cantidad": 3.0, "unidad": "roll", "kg": 71},
        ],
        "total_kg": 353,
    }


def _fake_asinfo(mod_ec="2026-09-04 14:51:44", quien="Ventas", filas_pedido=None):
    """Contesta la SQL de modificados o la de por_pedido según cuál sea."""
    if filas_pedido is None:
        filas_pedido = [_fila(), _fila(codigo="PO24AZU", color="AZU", tela="Polar 24",
                                       cantidad=5.0)]

    def _fetch(_db, sql, *a, **k):
        if "fecha_modificacion" in sql:
            return ([{"numero": "PDCL-30949", "modificado": mod_ec, "usuario": quien}], True)
        return (filas_pedido, True)
    return _fetch


# ── el diff ──────────────────────────────────────────────────────────────────

def test_diferencias_cuenta_agregados_sacados_y_cantidades():
    viejo = _foto_vieja()
    nuevo = {
        "descripcion": "urgente",
        "lineas": [
            {"producto": "FE96HAB", "tela": "Fleece 96 Perchado", "color": "HAB",
             "cantidad": 17.0, "unidad": "roll"},
            {"producto": "PO24AZU", "tela": "Polar 24", "color": "AZU",
             "cantidad": 5.0, "unidad": "roll"},
        ],
    }
    d = memos_sync.diferencias(viejo, nuevo)
    assert d == [
        "Agregó PO24AZU (Polar 24 AZU): 5 roll",
        "Sacó RB96NEG (Rib 96 NEG): tenía 3 roll",
        "FE96HAB (Fleece 96 Perchado HAB): 12 roll → 17 roll",
        "Nota del pedido: «urgente»",
    ]


def test_diferencias_vacias_si_la_fabrica_ve_lo_mismo():
    assert memos_sync.diferencias(_foto_vieja(), _foto_vieja()) == []


def test_la_fecha_de_asinfo_es_hora_ecuador_y_se_lleva_a_utc():
    assert memos_sync._a_utc("2026-09-04 14:51:44") == datetime(2026, 9, 4, 19, 51, 44, tzinfo=UTC)
    assert memos_sync._a_utc("basura") is None


# ── la pasada ────────────────────────────────────────────────────────────────

def _memo_vivo(enviado, detalle=None):
    return {"numero": "PDCL-30949", "estado": "pendiente", "enviado_en": enviado,
            "detalle": detalle if detalle is not None else _foto_vieja()}


def test_pedido_editado_despues_del_envio_pisa_la_foto_y_deja_la_alerta():
    enviado = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    llamadas = []

    def _actualizar(numero, detalle, cambio, por):
        llamadas.append((numero, detalle, cambio, por))
        return True, "actualizado"

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=_fake_asinfo()), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "vivos", return_value=[_memo_vivo(enviado)]), \
         patch.object(formulas_memos, "actualizar", side_effect=_actualizar):
        res = memos_sync.sincronizar()

    assert res["actualizados"] == ["PDCL-30949"]
    numero, detalle, cambio, por = llamadas[0]
    assert detalle["asinfo_modificado"] == "2026-09-04 14:51:44"
    assert [ln["producto"] for ln in detalle["lineas"]] == ["FE96HAB", "PO24AZU"]
    assert cambio["por"] == "Ventas"
    assert "Agregó PO24AZU (Polar 24 AZU): 5 roll" in cambio["lineas"]
    assert "FE96HAB (Fleece 96 Perchado HAB): 12 roll → 17 roll" in cambio["lineas"]
    assert por == "Asinfo · Ventas"


def test_pedido_editado_antes_del_envio_no_alerta_pero_se_anota():
    """La foto ya traía esa edición: se sella `asinfo_modificado` en
    silencio para no volver a mirarlo."""
    enviado = datetime(2026, 9, 4, 23, 0, tzinfo=UTC)  # después de las 14:51 EC
    llamadas = []
    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=_fake_asinfo()), \
         patch.object(formulas_memos, "vivos", return_value=[_memo_vivo(enviado)]), \
         patch.object(formulas_memos, "actualizar",
                      side_effect=lambda *a: (llamadas.append(a), (True, "actualizado"))[1]):
        res = memos_sync.sincronizar()
    assert res["actualizados"] == [] and res["silenciosos"] == ["PDCL-30949"]
    assert llamadas[0][2] is None
    assert llamadas[0][1]["asinfo_modificado"] == "2026-09-04 14:51:44"
    assert llamadas[0][1]["lineas"] == _foto_vieja()["lineas"]


def test_una_edicion_ya_procesada_no_se_vuelve_a_mirar():
    enviado = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    detalle = dict(_foto_vieja(), asinfo_modificado="2026-09-04 14:51:44")
    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=_fake_asinfo()), \
         patch.object(formulas_memos, "vivos", return_value=[_memo_vivo(enviado, detalle)]), \
         patch.object(formulas_memos, "actualizar") as act:
        res = memos_sync.sincronizar()
    assert res["actualizados"] == [] and res["silenciosos"] == []
    act.assert_not_called()


def test_edicion_sin_cambios_visibles_refresca_sin_alerta():
    """Asinfo selló fecha_modificacion pero lo que ve la fábrica es igual
    (p. ej. tocaron el precio): se pisa la foto sin prender la alerta."""
    enviado = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    viejo = _foto_vieja()
    filas = [_fila(codigo=ln["producto"], color=ln["color"], tela=ln["tela"],
                   cantidad=ln["cantidad"]) for ln in viejo["lineas"]]
    llamadas = []
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo(filas_pedido=filas)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "vivos", return_value=[_memo_vivo(enviado, viejo)]), \
         patch.object(formulas_memos, "actualizar",
                      side_effect=lambda *a: (llamadas.append(a), (True, "actualizado"))[1]):
        res = memos_sync.sincronizar()
    assert res["silenciosos"] == ["PDCL-30949"] and res["actualizados"] == []
    assert llamadas[0][2] is None


def test_pedido_que_ya_no_esta_pendiente_se_deja_como_esta():
    enviado = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo(filas_pedido=[])), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "vivos", return_value=[_memo_vivo(enviado)]), \
         patch.object(formulas_memos, "actualizar") as act:
        res = memos_sync.sincronizar()
    assert res["actualizados"] == []
    act.assert_not_called()


def test_sin_asinfo_no_se_toca_nada():
    enviado = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    with patch.object(service.metabase_client, "fetch_dataset_estado", return_value=([], False)), \
         patch.object(formulas_memos, "vivos", return_value=[_memo_vivo(enviado)]), \
         patch.object(formulas_memos, "actualizar") as act:
        res = memos_sync.sincronizar()
    assert res["disponible"] is False
    act.assert_not_called()


def test_el_numero_va_saneado_al_in_de_la_sql():
    assert memos_sync._numero_seguro(" pdcl-30949'; drop ") == "PDCL-30949DROP"


# ── el bridge ────────────────────────────────────────────────────────────────

@pytest.fixture
def _pool_formulas_memos():
    original = formulas_memos._pool
    yield
    formulas_memos._pool = original


def _wire_fake_pool_memos(fila_returned):
    from unittest.mock import MagicMock

    fake_cur = MagicMock()
    fake_cur.fetchone.return_value = fila_returned
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    fake_conn.cursor.return_value.__exit__.return_value = False
    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn
    formulas_memos._pool = fake_pool
    return fake_cur


def test_actualizar_con_cambio_sella_modificado_y_apila_el_cambio(_pool_formulas_memos):
    cur = _wire_fake_pool_memos((5,))
    ok, motivo = formulas_memos.actualizar("PDCL-1", {"x": 1}, {"lineas": ["a"]}, "Asinfo · Ventas")
    assert (ok, motivo) == (True, "actualizado")
    sql = cur.execute.call_args[0][0]
    assert "modificado_en = NOW()" in sql and "cambios = COALESCE(cambios" in sql
    assert "estado IN ('pendiente', 'en_proceso')" in sql


def test_actualizar_sin_cambio_solo_refresca_la_foto(_pool_formulas_memos):
    cur = _wire_fake_pool_memos((5,))
    ok, _ = formulas_memos.actualizar("PDCL-1", {"x": 1}, None, "")
    assert ok
    sql = cur.execute.call_args[0][0]
    assert "modificado_en" not in sql


def test_actualizar_no_pisa_un_memo_que_ya_no_esta_vivo(_pool_formulas_memos):
    _wire_fake_pool_memos(None)
    assert formulas_memos.actualizar("PDCL-1", {}, None, "") == (False, "no_vivo")


def test_sin_bridge_vivos_y_actualizar_no_rompen(_pool_formulas_memos):
    formulas_memos._pool = None
    assert formulas_memos.vivos() == []
    assert formulas_memos.actualizar("PDCL-1", {}, None, "") == (False, "sin_bridge")
    assert memos_sync.correr_si_toca() == {"corrio": False}
