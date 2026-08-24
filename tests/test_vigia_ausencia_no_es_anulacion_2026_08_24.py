"""El vigía no concluye "la anularon" por AUSENCIA. Pregunta.

TMT 2026-08-24. El 21/08 a las 20:46 la card 199 devolvió el período casi
entero pero sin dos documentos, y el vigía anuló `001-099-000182254` (KJG,
$9.421,44) y `001-099-000182327` (VGA, $7.531,62) — las dos VIVAS en Asinfo,
`fc.estado = 4`, sin motivo ni fecha de anulación. Con 503 documentos en la
ventana, dos que faltan no se distinguen de dos que anularon: por conteo el
caso es indecidible.

Los cinco guards que ya había cubren todos "Asinfo mudo". Ninguno cubría
"Asinfo contestó incompleto".

Dueña, sobre qué hacer: *"que frene y avise"*.

Guard 6: antes de anular se le pregunta a Asinfo por el número
(`factura_cliente.estado`). Sólo `estado = 0` habilita a tocar. Vivo, un
estado que no conocemos, la fila que no aparece, o la consulta que falla
mandan la factura a `frenadas` — sin tocarla — y suenan la campanita.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.asinfo import service as asinfo_service
from modules.facturas import vigia_anuladas as vig

VIVA = "001-099-000182254"     # KJG, la que el vigía mató estando viva
ANULADA = "NTEN-10879"         # BED, anulada de verdad ("ERROR PRECIO")


def _pc(numero, cliente="KJG", importe=9421.44):
    return {
        "id_factura": 282170, "numf": 182254, "numf_completo": numero,
        "fecha": None, "codigo_cli": cliente, "importe": importe,
        "abono": 0, "saldo": importe, "stat": "Z",
        "usuario_crea": vig.ORIGEN_AUTOMATICO,
    }


def _correr_guard6(candidatas, respuesta_asinfo, sugeridas=None):
    """Corre sólo el guard 6 sobre un diagnóstico armado a mano."""
    out = {"para_anular": list(candidatas), "sugeridas": list(sugeridas or []),
           "frenadas": []}
    with patch.object(asinfo_service, "estado_de_documentos",
                      return_value=respuesta_asinfo), \
            patch.object(vig, "_avisar_frenadas") as avisar:
        vig._confirmar_contra_asinfo(out)
    return out, avisar


def test_la_que_asinfo_da_por_viva_no_se_anula():
    """El caso 182254 exacto: la card no la trajo, pero el ERP la tiene viva."""
    out, avisar = _correr_guard6([_pc(VIVA)], {VIVA: {"estado": 4, "motivo": ""}})
    assert out["para_anular"] == [], (
        "Asinfo la da por viva (estado 4) y el vigía igual la iba a anular"
    )
    assert len(out["frenadas"]) == 1
    assert "VIVA" in out["frenadas"][0]["freno"].upper()
    assert avisar.called, "una frenada sin campanita no la ve nadie"


def test_la_anulada_de_verdad_se_sigue_anulando():
    """El freno no puede tapar el trabajo que el vigía sí tiene que hacer."""
    out, _ = _correr_guard6(
        [_pc(ANULADA, "BED", 2176.56)],
        {ANULADA: {"estado": 0, "motivo": "ERROR PRECIO"}},
    )
    assert len(out["para_anular"]) == 1
    assert out["frenadas"] == []
    assert out["para_anular"][0]["motivo_asinfo"] == "ERROR PRECIO"


def test_si_asinfo_no_reconoce_el_numero_tampoco_se_anula():
    out, _ = _correr_guard6([_pc(VIVA)], {})
    assert out["para_anular"] == [] and len(out["frenadas"]) == 1


def test_si_no_se_pudo_preguntar_no_se_anula_nada():
    """`None` es "no sé", que no es "no está"."""
    out, _ = _correr_guard6([_pc(VIVA)], None)
    assert out["para_anular"] == [] and len(out["frenadas"]) == 1
    assert "confirmar" in out["frenadas"][0]["freno"]


def test_las_dos_del_21_08_se_frenan_juntas():
    otra = {**_pc("001-099-000182327", "VGA", 7531.62),
            "id_factura": 282246, "numf": 182327}
    out, _ = _correr_guard6(
        [_pc(VIVA), otra],
        {VIVA: {"estado": 4, "motivo": ""},
         "001-099-000182327": {"estado": 4, "motivo": ""}},
    )
    assert out["para_anular"] == []
    assert len(out["frenadas"]) == 2


def test_a_las_sugeridas_se_les_anota_lo_que_dijo_asinfo():
    """Las de otro origen las decide una persona — que las decida INFORMADA."""
    sug = {**_pc(VIVA), "usuario_crea": "tamara"}
    out, _ = _correr_guard6([], {VIVA: {"estado": 4, "motivo": ""}}, sugeridas=[sug])
    assert out["sugeridas"][0]["freno"], "quedó sin decir qué contestó Asinfo"


def test_sin_candidatas_no_se_le_pregunta_nada_a_asinfo():
    out = {"para_anular": [], "sugeridas": [], "frenadas": []}
    with patch.object(asinfo_service, "estado_de_documentos") as pregunta:
        vig._confirmar_contra_asinfo(out)
    assert not pregunta.called


# ---------------------------------------------------------------------------
# La consulta a Asinfo
# ---------------------------------------------------------------------------

def test_la_consulta_pide_el_estado_por_numero_completo():
    llamadas = []

    def fake(db_id, sql, max_results=0):
        llamadas.append((db_id, sql))
        return [{"numero": VIVA, "estado": 4, "id_documento": 7,
                 "motivo": None}]

    with patch.object(asinfo_service.metabase_client, "fetch_dataset",
                      side_effect=fake):
        out = asinfo_service.estado_de_documentos([VIVA, ANULADA])
    db_id, sql = llamadas[0]
    assert db_id == 2, "Asinfo es la base 2 de Metabase"
    assert "factura_cliente" in sql and "fc.estado" in sql
    # Las notas de entrega viven en la MISMA tabla y guardan su número
    # completo en fc.numero — por eso una sola consulta sirve para las dos.
    assert VIVA in sql and ANULADA in sql
    assert out == {VIVA: {"estado": 4, "motivo": "", "id_documento": 7}}


@pytest.mark.parametrize("hostil", [
    "001'--", "NTEN-1; DROP TABLE factura_cliente", "' OR '1'='1", "",
])
def test_un_numero_raro_no_entra_en_la_sql(hostil):
    """La SQL se interpola: el filtro de caracteres es la única puerta."""
    llamadas = []

    def fake(db_id, sql, max_results=0):
        llamadas.append(sql)
        return []

    with patch.object(asinfo_service.metabase_client, "fetch_dataset",
                      side_effect=fake):
        asinfo_service.estado_de_documentos([hostil])
    assert llamadas == [], f"{hostil!r} llegó hasta la consulta"


def test_si_metabase_no_contesta_devuelve_no_se():
    with patch.object(asinfo_service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        assert asinfo_service.estado_de_documentos([VIVA]) is None


def test_metabase_que_contesta_vacio_no_es_lo_mismo_que_caido():
    """Contestó y no encontró el número: eso SÍ es un dato (dict vacío)."""
    with patch.object(asinfo_service.metabase_client, "fetch_dataset_estado",
                      return_value=([], True)):
        assert asinfo_service.estado_de_documentos([VIVA]) == {}


def test_solo_el_estado_cero_es_una_anulacion():
    assert asinfo_service.ESTADO_ANULADO == 0
