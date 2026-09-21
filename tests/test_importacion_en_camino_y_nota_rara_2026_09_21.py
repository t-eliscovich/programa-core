"""Agarrar la Nota mal escrita ANTES de que alguien le cargue plata.

Tamara 2026-09-21, sobre AC 83A: la IM-0000663 se creó en Asinfo el 09/09 con
la Nota "ACMT/EXP/2026-27/8586 AC 83A)" (sin paréntesis, letra pegada) y
estuvo 12 días muda — el vigía sólo miraba las RECIBIDAS. *"¿Y cómo
podríamos agarrar estas cosas más temprano?"*:

  1. la importación SIN código avisa apenas aparece, en tránsito;
  2. la Nota que cruza por el camino de emergencia (sin paréntesis o con
     letra pegada) avisa para que la corrijan en Asinfo.
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
        "sin_parentesis": bool(code.get("sin_parentesis")),
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


# ── 2 · la Nota mal escrita ─────────────────────────────────────────────────

def test_nota_sin_parentesis_y_con_letra_es_una_nota_rara():
    rows = [
        _fila("ACMT/EXP/2026-27/8586 AC 83A)", dias_atras=12),
        _fila("ACMT/EXP/2026-27/8549 ( AC 69)", im="IM-0000668"),      # bien
        _fila("INVHY5464-26-2 ( MH 74-75 )", im="IM-0000658"),         # rango: bien
        _fila("MTG3756", im="IM-0000654"),                             # sin código: es del otro
    ]
    raras = vig.notas_mal_escritas(rows=rows)
    assert [r["im_numero"] for r in raras] == ["IM-0000663"]
    r = raras[0]
    assert r["codigo"] == "AC 83A"
    assert r["motivos"] == ["sin paréntesis", "letra pegada al número (A)"]

    out, vistos = _correr(rows)
    assert out["notas_raras"] == 1
    a = next(v for v in vistos if v["clave"] == "import-nota-rara:IM-0000663")
    assert a["nivel"] == "alerta"       # 'ok' nacería resuelto: no se podría dar vuelta
    assert a["titulo"] == ("AC 83A · la Nota en Asinfo está mal escrita "
                           "(sin paréntesis, letra pegada al número (A)). "
                           "Corregila.")
    assert "( AC 36 )" in a["detalle"]
    assert a["url"] == "/importaciones?anio=todos&q=IM-0000663"


def test_nota_rara_vieja_no_avisa():
    assert vig.notas_mal_escritas(
        rows=[_fila("X AC 83A)", dias_atras=200)]) == []


def test_la_nota_rara_se_resuelve_cuando_la_escriben_bien():
    rows = [_fila("ACMT/EXP/2026-27/8586 ( AC 83 )")]      # ya corregida
    resueltos = []
    with patch.object(vig, "_leer_costos", return_value={}), \
         patch("modules.avisos.queries.abiertos_por_clave",
               side_effect=lambda pref: (
                   [{"id_aviso": 9, "clave": "import-nota-rara:IM-0000663"}]
                   if pref == "import-nota-rara:" else [])), \
         patch("modules.avisos.queries.resolver",
               side_effect=lambda id_aviso, **kw: resueltos.append(
                   (id_aviso, kw)) or True):
        vig._resolver_los_arreglados(rows)
    assert resueltos == [(9, {"titulo": "IM-0000663 · listo, la Nota ya está bien escrita",
                              "detalle": "Cruza por el camino normal."})]


def test_la_nota_rara_no_se_resuelve_mientras_siga_igual():
    rows = [_fila("ACMT/EXP/2026-27/8586 AC 83A)")]
    resueltos = []
    with patch.object(vig, "_leer_costos", return_value={}), \
         patch("modules.avisos.queries.abiertos_por_clave",
               side_effect=lambda pref: (
                   [{"id_aviso": 9, "clave": "import-nota-rara:IM-0000663"}]
                   if pref == "import-nota-rara:" else [])), \
         patch("modules.avisos.queries.resolver",
               side_effect=lambda id_aviso, **kw: resueltos.append(id_aviso)):
        vig._resolver_los_arreglados(rows)
    assert resueltos == []


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
