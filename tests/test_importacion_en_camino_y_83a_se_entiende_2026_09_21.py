"""Agarrar la Nota mal escrita ANTES de que alguien le cargue plata.

Tamara 2026-09-21, sobre AC 83A: la IM-0000663 se creó en Asinfo el 09/09 con
la Nota "ACMT/EXP/2026-27/8586 AC 83A)" (sin paréntesis, letra pegada) y
estuvo 12 días muda — el vigía sólo miraba las RECIBIDAS. *"¿Y cómo
podríamos agarrar estas cosas más temprano?"*:

  1. la importación SIN código avisa apenas aparece, en tránsito;
  2. la Nota "AC 83A)" (sin paréntesis, letra pegada) se ENTIENDE, sin
     avisar nada — Tamara: "quiero que también entienda 83A)".
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from concepto_parser import parse_nota_importacion  # noqa: E402
from modules.importaciones import vigilancia as vig  # noqa: E402


def _fila(nota, *, dias_atras=1, kg=24072.4, im="IM-0000663",
          recibida=False, proveedor="AC"):
    from filters import today_ec
    f = today_ec() - timedelta(days=dias_atras)
    code = parse_nota_importacion(nota)
    return {
        "im_numero": im, "recibida": recibida,
        "fecha_recepcion": str(f) if recibida else None,
        "fecha": str(f),
        "nota": nota, "prov": code.get("prov"), "numero": code.get("numero"),
        "numero_hasta": code.get("numero_hasta"),
        "sufijo": code.get("sufijo"), "codigo": code.get("codigo"),
        "proveedor": proveedor,
        "grupo_id": im, "grupo_ims": [im], "grupo_kg": kg, "kg": kg,
        "compra": None, "anticipo": None,
    }


def _correr(rows):
    vig._ultima_corrida = 0.0
    vistos = []
    with patch.object(vig, "_leer_importaciones", return_value=rows), \
         patch.object(vig, "importaciones_fuera_de_banda", return_value=[]), \
         patch.object(vig, "facturas_con_plata_en_una_sola", return_value=[]), \
         patch.object(vig, "_resolver_los_arreglados"), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: vistos.append(kw) or True):
        out = vig.revisar_si_toca()
    return out, vistos


# ── 1 · en camino, sin código ───────────────────────────────────────────────

def test_en_transito_sin_codigo_avisa_con_su_propio_texto():
    rows = [_fila("ACMT/EXP/2026-27/8586", dias_atras=12)]
    casos = vig.importaciones_sin_codigo(rows=rows)
    assert len(casos) == 1 and casos[0]["recibida"] is False
    assert casos[0]["dias"] == 12                 # desde la fecha de la factura

    out, vistos = _correr(rows)
    assert out["sin_codigo"] == 1
    a = next(v for v in vistos if v["clave"].startswith("import-sin-codigo:"))
    assert a["nivel"] == "alerta"
    assert a["titulo"] == ("ACMT/EXP/2026-27/8586 · 24.072 kg vienen en camino "
                           "sin código del programa en la Nota. "
                           "¿Qué importación es?")
    assert "anticipo no la va a encontrar" in a["detalle"]
    assert "IM-" not in a["titulo"]


def test_en_transito_vieja_no_estrena_la_alarma_con_historia():
    rows = [_fila("INV 25-26/426", dias_atras=200)]
    assert vig.importaciones_sin_codigo(rows=rows) == []


def test_recibida_sin_codigo_sigue_diciendo_que_llego():
    out, vistos = _correr([_fila("MTG3756", recibida=True, kg=16113.6)])
    a = next(v for v in vistos if v["clave"].startswith("import-sin-codigo:"))
    assert a["titulo"].startswith("MTG3756 · 16.114 kg llegaron sin código")


# ── 2 · la Nota "AC 83A)" NO es un caso raro ─────────────────────────────────
# Hubo unas horas un aviso "la Nota en Asinfo está mal escrita" (sin paréntesis
# / letra pegada). Tamara lo bajó el mismo día: *"no quiero que me diga que
# está mal, quiero que también entienda 83A)"*. El formato se entiende y punto.

def test_la_nota_con_letra_y_sin_parentesis_no_genera_ningun_aviso():
    rows = [_fila("ACMT/EXP/2026-27/8586 AC 83A)", dias_atras=12)]
    assert rows[0]["codigo"] == "AC 83A"
    out, vistos = _correr(rows)
    assert out["sin_codigo"] == 0 and out["avisados"] == 0
    assert vistos == []
    assert not hasattr(vig, "notas_mal_escritas")


def test_el_aviso_de_nota_rara_que_quedo_abierto_se_da_vuelta_solo():
    resueltos = []
    with patch.object(vig, "_leer_costos", return_value={}), \
         patch("modules.avisos.queries.abiertos_por_clave",
               side_effect=lambda pref: (
                   [{"id_aviso": 11089, "clave": "import-nota-rara:IM-0000663"}]
                   if pref == "import-nota-rara:" else [])), \
         patch("modules.avisos.queries.resolver",
               side_effect=lambda id_aviso, **kw: resueltos.append(
                   (id_aviso, kw)) or True):
        vig._resolver_los_arreglados([_fila("ACMT/EXP/2026-27/8586 AC 83A)")])
    assert resueltos == [(11089, {
        "titulo": "IM-0000663 · listo, la Nota se entiende así como está",
        "detalle": "El programa ya lee el código con la letra pegada."})]


# ── el nombre: 83A ──────────────────────────────────────────────────────────

def test_el_picker_manda_la_letra_al_concepto():
    """/importaciones/_api/abiertas expone `sufijo` y el JS de /dolares arma
    `83A/26` (Tamara: "se tiene que llamar 83A")."""
    import inspect

    from modules.importaciones import views
    src = inspect.getsource(views.api_importaciones_abiertas)
    assert '"sufijo": r.get("sufijo")' in src
    from pathlib import Path
    html = (Path(_REPO_ROOT) / "modules" / "dolares" / "templates" / "dolares"
            / "lista.html").read_text(encoding="utf-8")
    assert "+ (im.sufijo || '')" in html
    html2 = (Path(_REPO_ROOT) / "modules" / "compras" / "templates" / "compras"
             / "nueva.html").read_text(encoding="utf-8")
    assert "(im.sufijo || '')" in html2


def test_el_casillero_a_mano_acepta_la_letra():
    import re
    m = re.match(r"^([A-Z]{2,3})\s*(\d+[A-Z]?(?:-\d+)?)$", "AC 83A")
    assert m and m.group(2) == "83A"
    import inspect

    from modules.importaciones import views
    assert r"(\d+[A-Z]?(?:-\d+)?)" in inspect.getsource(views)


def test_el_vigia_reconoce_el_codigo_con_letra_para_el_link():
    assert vig._q_del_caso({"codigo": "AC 83A", "ims": ["IM-0000663"]}) == "AC 83A"
