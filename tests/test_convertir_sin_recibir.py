"""No se convierte lo que todavía no llegó (TMT 2026-07-31).

Dueña, después de encontrar la caída: *"¿podemos, la próxima que cargue compra
sin recibir, avisarle a Andrés?"*.

Qué pasó el 31/07 a las 15:19. Se convirtieron a compra los anticipos de AC 22
(IM-0000578) — 53.860,75 + 3.545,52 — cuando esa importación NO figura recibida
en Asinfo: la mercadería está navegando. La conversión:

    · saca el anticipo del activo            → −53.861
    · crea la compra PAGADA                  → no nace ningún pasivo
    · no entra ni un kilo al stock           → nada ocupa el lugar del activo

Patrimonio −53.861 contra nada: una pérdida ficticia. El anticipo de algo en
tránsito es un activo legítimo y tiene que seguir siéndolo hasta que llegue.

La conversión AUTOMÁTICA ya respetaba la regla (sólo mira recepciones). Esto es
la misma regla para el camino manual, que era por donde se coló.
"""
from unittest.mock import patch

import pytest

from modules.dolares import queries as dq


def _recepcion(mapa):
    """Mockea el cruce con Asinfo: {id_dolares: fecha_recepcion o None}."""
    def _adjuntar(filas):
        for f in filas:
            f["im_numero"] = f"IM-{f['id_dolares']}"
            f["ref"] = 22
            f["fecha_recepcion_im"] = mapa.get(f["id_dolares"])
    return patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
                 side_effect=_adjuntar)


ROWS = [{"id_dolares": 1, "importe": 53860.75},
        {"id_dolares": 2, "importe": 3545.52}]


def test_lo_que_no_llego_queda_marcado():
    with _recepcion({1: None, 2: None}):
        faltan = dq._anticipos_sin_recepcion(ROWS, "AC")
    assert faltan == ["AC 22 · IM-1", "AC 22 · IM-2"]


def test_lo_que_ya_llego_no_molesta():
    with _recepcion({1: "2026-07-31", 2: "2026-07-31"}):
        assert dq._anticipos_sin_recepcion(ROWS, "AC") == []


def test_mezcla_solo_marca_lo_que_falta():
    with _recepcion({1: "2026-07-31", 2: None}):
        assert dq._anticipos_sin_recepcion(ROWS, "AC") == ["AC 22 · IM-2"]


# ── Fail-OPEN: un puente caído no puede frenar el trabajo del día ──────────

def test_si_el_cruce_no_engancha_ninguna_NO_bloquea():
    """Cero matches = el cruce no anduvo, no es que no haya llegado."""
    def _mudo(filas):
        for f in filas:
            f["im_numero"] = None
            f["fecha_recepcion_im"] = None

    with patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
               side_effect=_mudo):
        assert dq._anticipos_sin_recepcion(ROWS, "AC") == []


def test_si_asinfo_explota_NO_bloquea():
    with patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
               side_effect=RuntimeError("Metabase caído")):
        assert dq._anticipos_sin_recepcion(ROWS, "AC") == []


def test_sin_anticipos_no_hay_nada_que_mirar():
    with _recepcion({}):
        assert dq._anticipos_sin_recepcion([], "AC") == []


# ── El enganche: la conversión tiene que consultarlo ───────────────────────

def test_convertir_a_compra_consulta_el_guard():
    """Si nadie lo llama, la función no sirve de nada."""
    import inspect
    src = inspect.getsource(dq.convertir_a_compra)
    assert "_anticipos_sin_recepcion" in src
    assert "permitir_sin_recibir" in src


def test_el_mensaje_explica_POR_QUE_y_como_seguir():
    """Un 'no se puede' sin motivo hace que la gente busque la vuelta."""
    import inspect
    src = inspect.getsource(dq.convertir_a_compra)
    assert "todavía no llegó" in src
    assert "baja la utilidad" in src
    assert "convertir aunque no haya llegado" in src


def test_forzado_deja_aviso_en_novedades():
    """Se puede forzar, pero no en silencio."""
    import inspect
    src = inspect.getsource(dq.convertir_a_compra)
    assert "avisar(" in src
    assert "convertido-sin-recibir" in src        # clave idempotente
    assert 'nivel="alerta"' in src


def test_los_quimicos_no_pasan_por_el_guard():
    """Los anticipos de químicos no vienen de una importación con recepción."""
    import inspect
    from modules.dolares import views as dv
    src = inspect.getsource(dv)
    assert "permitir_sin_recibir=True" in src     # el camino de químicos
