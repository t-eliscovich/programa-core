"""El reverso de un lote de cheques deja ELEGIR cuáles anular.

TMT 2026-09-11 (dueña, caso VGA del 10/09): Alex cargó dos anticipos de VGA
en el mismo formulario (4.871,54 y 2.162,30), quiso deshacer el de 2.162,30
y el "Reverso" del lote en Historial se llevó los dos. El de 4.871,54 nunca
se repuso y el banco lo tenía pendiente. Ahora la confirmación lista cada
cheque con su tilde y el POST anula sólo los tildados (sus aplicaciones y
su espejo van con cada cheque). Sin el campo `ch` se reversa todo, como
siempre.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.historial import views as hv  # noqa: E402

BATCH = "7791e956-6393-443d-9b7e-b4899313615c"


def _rows():
    return [
        {"id_mov_doble": 1, "tipo": "cheque_creado", "origen_table": "cheque",
         "origen_id": 104294, "destino_table": "cheque", "destino_id": 104294,
         "importe": 2162.30, "concepto": "Dep. Pich. de VGA", "estado": "activo"},
        {"id_mov_doble": 2, "tipo": "cheque_anticipo_espejo", "origen_table": "cheque",
         "origen_id": 104294, "destino_table": "cheque", "destino_id": 104295,
         "importe": -2162.30, "concepto": "Espejo", "estado": "activo"},
        {"id_mov_doble": 3, "tipo": "cheque_creado", "origen_table": "cheque",
         "origen_id": 104296, "destino_table": "cheque", "destino_id": 104296,
         "importe": 4871.54, "concepto": "Dep. Pich. de VGA", "estado": "activo"},
        {"id_mov_doble": 4, "tipo": "cheque_aplicado_a_factura", "origen_table": "cheque",
         "origen_id": 104296, "destino_table": "factura", "destino_id": 555,
         "importe": 4871.54, "concepto": "", "estado": "activo"},
    ]


def _login(app, fake_db):
    rid = fake_db.add_role("Cob", ["historial.ver", "informes.ver",
                                   "cheques.anular", "cheques.aplicar"])
    uid = fake_db.add_user("alex", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_cheques_del_batch_agrupa_por_cheque_con_aplicaciones_y_espejo():
    with patch.object(hv.db, "fetch_all", return_value=[
        {"id_cheque": 104296, "codigo_cli": "VGA", "no_cheque": "", "importe": 4871.54,
         "doc_banco": "56543360", "stat": "B", "cliente_nombre": "TEXTINORT CIA. LTDA."},
    ]):
        ch = hv._cheques_del_batch(_rows())
    assert [c["id_cheque"] for c in ch] == [104294, 104296]
    c1, c2 = ch
    assert c1["es_anticipo"] is True and c1["n_aplicaciones"] == 0
    assert c1["importe"] == 2162.30  # sin fila en la DB cae al mov_doble
    assert c2["codigo_cli"] == "VGA" and c2["doc_banco"] == "56543360"
    assert c2["n_aplicaciones"] == 1 and c2["es_anticipo"] is False


def test_cheques_del_batch_sin_cheques_es_vacio():
    assert hv._cheques_del_batch([{"tipo": "activacion_maquinaria", "origen_id": 1}]) == []


def test_cheques_elegidos():
    from werkzeug.datastructures import MultiDict
    rows = _rows()
    assert hv._cheques_elegidos(rows, MultiDict()) is None          # POST viejo
    assert hv._cheques_elegidos(rows, MultiDict([("ch", "104296"), ("ch", "x")])) == {104296}
    assert hv._cheques_elegidos([{"tipo": "activacion_maquinaria"}],
                                MultiDict([("ch", "1")])) is None   # sin cheques


def test_get_lista_los_cheques_con_tilde(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(hv, "_cheques_del_batch", return_value=[
        {"id_cheque": 104294, "codigo_cli": "VGA", "cliente_nombre": "TEXTINORT CIA. LTDA.",
         "no_cheque": "", "doc_banco": "66985468", "importe": 2162.30, "concepto": "",
         "n_aplicaciones": 0, "es_anticipo": True},
        {"id_cheque": 104296, "codigo_cli": "VGA", "cliente_nombre": "TEXTINORT CIA. LTDA.",
         "no_cheque": "", "doc_banco": "56543360", "importe": 4871.54, "concepto": "",
         "n_aplicaciones": 0, "es_anticipo": True},
    ]), patch("mov_doble.buscar_por_batch", return_value=_rows()):
        r = c.get(f"/historial/batch/{BATCH}/reverso")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'name="ch" value="104294"' in html
    assert 'name="ch" value="104296"' in html
    assert "Anular los tildados" in html
    assert "doc banco 56543360" in html


def test_post_anula_solo_los_tildados(app, fake_db):
    c = _login(app, fake_db)
    anulados: list[int] = []
    desaplicados: list[tuple] = []

    class _Tx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    from modules.cheques import queries as chq
    with patch("mov_doble.buscar_por_batch", return_value=_rows()), \
         patch.object(hv.db, "tx", return_value=_Tx()), \
         patch.object(chq, "anular_por_error_de_carga",
                      side_effect=lambda idc, **k: anulados.append(int(idc))), \
         patch.object(chq, "desaplicar_factura",
                      side_effect=lambda **k: desaplicados.append((k["id_cheque"], k["id_factura"]))):
        r = c.post(f"/historial/batch/{BATCH}/reverso",
                   data={"motivo": "", "ch": ["104294"]})
    assert r.status_code == 302
    assert anulados == [104294]          # el de 4.871,54 NO se toca
    assert desaplicados == []            # su factura tampoco


def test_post_sin_ch_reversa_todo_como_antes(app, fake_db):
    c = _login(app, fake_db)
    anulados: list[int] = []
    desaplicados: list[tuple] = []

    class _Tx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    from modules.cheques import queries as chq
    with patch("mov_doble.buscar_por_batch", return_value=_rows()), \
         patch.object(hv.db, "tx", return_value=_Tx()), \
         patch.object(chq, "anular_por_error_de_carga",
                      side_effect=lambda idc, **k: anulados.append(int(idc))), \
         patch.object(chq, "desaplicar_factura",
                      side_effect=lambda **k: desaplicados.append((k["id_cheque"], k["id_factura"]))):
        r = c.post(f"/historial/batch/{BATCH}/reverso", data={"motivo": ""})
    assert r.status_code == 302
    assert sorted(anulados) == [104294, 104296]
    assert desaplicados == [(104296, 555)]


def test_post_sin_ninguno_tildado_no_anula_nada(app, fake_db):
    c = _login(app, fake_db)
    from modules.cheques import queries as chq
    with patch("mov_doble.buscar_por_batch", return_value=_rows()), \
         patch.object(chq, "anular_por_error_de_carga") as an:
        r = c.post(f"/historial/batch/{BATCH}/reverso", data={"motivo": "", "ch": [""]})
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/historial/batch/{BATCH}/reverso")
    an.assert_not_called()
