"""Recepción ANULADA en Asinfo — TMT 2026-09-30 (AI 53, BOD-2393).

10:45 Asinfo recibe la importación y el automático la convierte a compra BAP.
12:14 anulan la recepción: el hilo sale del stock, la compra y los anticipos
consumidos quedan → utilidad −171 mil. 12:33 la reciben de nuevo con otro BOD,
pero el BOD anulado seguía sumando como "compra local" de 23.953 kg a $ 0 y
bajaba el $/kg de todo el hilo.

Se prueban las tres patas:
  1. el BOD anulado no entra al $/kg ni a la tabla de compras del flujo;
  2. la importación con la recepción anulada deja de figurar recibida (SQL);
  3. el automático deshace sola la conversión cuya recepción anularon, después
     de la gracia, y no toca nada si Asinfo no contesta.
"""
import inspect
from unittest.mock import patch

from modules.importaciones import autobap


# ── 1. BOD anulado fuera del $/kg ───────────────────────────────────────────

def _local(**kw):
    base = {"recibida": True, "fecha_recepcion": "2026-09-30", "kg": 1000.0,
            "importe_sugerido": 3450.0, "anulada": False, "prov": "HY",
            "tarifa": 3.0}
    base.update(kw)
    return base


def test_bod_anulado_no_suma_kg_ni_plata_al_hilo_local():
    from modules.compras_locales import service as loc
    filas = [_local(), _local(kg=23953.25, importe_sugerido=None, anulada=True)]
    with patch.object(loc, "compras_locales_con_cruce", return_value=filas):
        r = loc.hilado_local_recibido_mes(2026, 9)
    assert r["kg"] == 1000.0


def test_bod_anulado_no_entra_a_la_tabla_de_compras_del_flujo():
    from modules.compras_locales import service as loc
    from modules.importaciones import service as imp
    filas = [_local(), _local(kg=23953.25, anulada=True)]
    with patch.object(imp, "importaciones_con_cruce", return_value=[]), \
         patch.object(loc, "compras_locales_con_cruce", return_value=filas):
        r = imp.compras_hilado_recibidas_mes(2026, 9)
    assert r["total"]["kg"] == 1000.0


# ── 2. Importación con recepción anulada = no recibida ──────────────────────

def test_importacion_ignora_recepcion_anulada():
    from modules.asinfo import service as asvc
    src = inspect.getsource(asvc.importaciones_asinfo)
    assert "rp.fecha_anulacion IS NULL" in src


# ── 3. El automático deshace la conversión ──────────────────────────────────

_CONV = {"id_autobap_log": 70, "im_numero": "IM-0000629", "codigo_prov": "AI",
         "ref_num": 53, "importe": 85884.99, "id_compra": 821,
         "id_mov_doble": 9001}


def _index(recibida: bool):
    return {("AI", 53): [{"im_numero": "IM-0000629", "recibida": recibida,
                          "grupo_ims": []}]}


def _revertir(index, ahora, conv=None):
    with patch.object(autobap, "_conversiones_vivas",
                      return_value=[conv or _CONV]), \
         patch("modules.dolares.queries.reversar_conversion",
               return_value={"restaurados": 3}) as rev, \
         patch.object(autobap, "_avisar_revertida") as av:
        r = autobap.revertir_recepciones_anuladas(index, ahora=ahora)
    return r, rev, av


def setup_function(_):
    autobap._sin_recepcion_desde.clear()


def test_recibida_no_se_toca():
    r, rev, _ = _revertir(_index(True), ahora=0.0)
    assert r["revertidas"] == 0 and not rev.called


def test_anulada_espera_la_gracia_y_despues_deshace():
    r, rev, _ = _revertir(_index(False), ahora=0.0)
    assert r["revertidas"] == 0 and r["esperando"] == ["AI 53"]
    assert not rev.called
    r, rev, av = _revertir(_index(False),
                           ahora=autobap._GRACIA_ANULADA_SECS + 1)
    assert r["revertidas"] == 1 and r["importe"] == 85884.99
    rev.assert_called_once()
    assert rev.call_args.args[0] == 9001
    av.assert_called_once()


def test_re_recibida_dentro_de_la_gracia_no_se_deshace():
    """El caso real: anulada 12:14, recibida de nuevo 12:33."""
    _revertir(_index(False), ahora=0.0)
    r, rev, _ = _revertir(_index(True), ahora=600.0)
    assert not rev.called
    # y el reloj arranca de cero si la vuelven a anular
    r, rev, _ = _revertir(_index(False), ahora=autobap._GRACIA_ANULADA_SECS)
    assert not rev.called and r["esperando"] == ["AI 53"]


def test_asinfo_mudo_no_deshace_nada():
    r, rev, _ = _revertir({}, ahora=10**9)
    assert r["revertidas"] == 0 and not rev.called


def test_importacion_que_no_aparece_no_se_toca():
    otro = {("AI", 54): [{"im_numero": "IM-0000700", "recibida": False}]}
    _revertir(otro, ahora=0.0)
    r, rev, _ = _revertir(otro, ahora=10**9)
    assert r["revertidas"] == 0 and not rev.called


def test_partida_con_una_mitad_recibida_no_se_deshace():
    idx = {("AI", 15): [
        {"im_numero": "IM-0000571", "recibida": False,
         "grupo_ims": ["IM-0000571", "IM-0000572"]},
        {"im_numero": "IM-0000572", "recibida": True,
         "grupo_ims": ["IM-0000571", "IM-0000572"]},
    ]}
    conv = dict(_CONV, im_numero="IM-0000571", codigo_prov="AI", ref_num=15)
    _revertir(idx, ahora=0.0, conv=conv)
    r, rev, _ = _revertir(idx, ahora=10**9, conv=conv)
    assert not rev.called


def test_dry_run_no_escribe():
    _revertir(_index(False), ahora=0.0)
    with patch.object(autobap, "_conversiones_vivas", return_value=[_CONV]), \
         patch("modules.dolares.queries.reversar_conversion") as rev:
        r = autobap.revertir_recepciones_anuladas(
            _index(False), dry_run=True, ahora=10**9)
    assert not rev.called and r["detalle"][0]["id_compra"] == 821


def test_si_falla_el_reverso_avisa_y_sigue():
    _revertir(_index(False), ahora=0.0)
    with patch.object(autobap, "_conversiones_vivas", return_value=[_CONV]), \
         patch("modules.dolares.queries.reversar_conversion",
               side_effect=ValueError("ya no es BAP")), \
         patch.object(autobap, "avisar") as av:
        r = autobap.revertir_recepciones_anuladas(_index(False), ahora=10**9)
    assert r["revertidas"] == 0
    assert av.call_args.kwargs["tipo"] == "error"


def test_ya_cargada_ignora_conversiones_deshechas():
    """Si la conversión se deshizo, la próxima es la importación entera (con
    sus kg), no "otra parte"."""
    src = inspect.getsource(autobap._ya_cargada)
    assert "JOIN scintela.compra" in src and "'Y'" in src
