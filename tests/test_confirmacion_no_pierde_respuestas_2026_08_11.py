"""Una respuesta que el usuario YA DIO tiene que llegar hasta el guardado.

La cobranza se guarda en dos pasos: el form manda `paso=guardar`, aparece
"Confirmá los cambios", y ese POST manda `paso=ejecutar`. Todo lo que el
usuario contestó en el paso 1 —tildes, motivos— viaja escondido en el form del
paso 2. Cuando un tilde no viaja, el freno que ya contestó vuelve a saltar y el
guardado no termina NUNCA.

Ya pasó dos veces:

· 16/06/2026 — `aprobar_diferencia`: la cobranza con flete/retención rebotaba
  "supera por más de $50" aunque la dueña ya lo había tildado.
· 11/08/2026 — `confirmar_no_duplicado` (freno del 05/08): Alex tildaba «Es
  otro cheque, no es duplicado», llegaba a la confirmación, apretaba Confirmar
  y el aviso de duplicado saltaba otra vez. Medido end-to-end contra una base
  local: el cheque no se guardaba.

Las dos veces el arreglo fue agregar el campo A MANO a un formulario. Esta
pantalla tiene TRES (con facturas, sin facturas, volver a editar), así que
agregarlo a mano es exactamente cómo se pierde el siguiente. Ahora salen de un
macro único, `respuestas_ya_dadas()`, y esto verifica que los tres lo usen.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "modules" / "cheques" / "templates" / "cheques" / "nuevo_confirmar.html"
)

#: Los tildes/valores que el usuario contesta en el paso 1 y el backend vuelve
#: a leer en el paso 2. Si agregás uno nuevo al alta, va en el macro.
#: (`confirmar_no_duplicado` estaba en esta lista hasta el 12/08/2026, cuando
#: la dueña mandó sacar el freno de duplicados. La lección por la que existe
#: este archivo no cambia: el que venga después va acá.)
RESPUESTAS = [
    "es_anticipo",
    "aprobar_diferencia",
    "motivo_diferencia",
    "aplicar_t_used",
]


@pytest.fixture(scope="module")
def fuente() -> str:
    return TEMPLATE.read_text(encoding="utf8")


def test_todos_los_formularios_reenvian_las_respuestas(fuente):
    """Cada <form> con un `paso` llama al macro. Ninguno se salva de memoria."""
    formularios = re.findall(
        r'<input[^>]*name="paso"[^>]*value="(\w+)"[^>]*>(.{0,400})', fuente, re.S
    )
    assert len(formularios) >= 3, (
        f"Se esperaban al menos 3 formularios con `paso`; hay {len(formularios)}. "
        "Si se agregó uno, tiene que llamar a respuestas_ya_dadas()."
    )
    sin_macro = [p for p, cuerpo in formularios if "respuestas_ya_dadas()" not in cuerpo]
    assert not sin_macro, (
        "Estos formularios no reenvían lo que el usuario ya contestó: "
        f"{sin_macro}. Van a hacer saltar de nuevo un freno ya respondido."
    )


@pytest.mark.parametrize("campo", RESPUESTAS)
def test_cada_respuesta_esta_en_el_macro(campo, fuente):
    macro = fuente[fuente.index("{% macro respuestas_ya_dadas"):]
    macro = macro[: macro.index("{% endmacro %}")]
    assert f'name="{campo}"' in macro, (
        f"{campo!r} no viaja del paso 1 al paso 2."
    )


def test_las_respuestas_no_se_repiten_sueltas_fuera_del_macro(fuente):
    """Si además queda una copia suelta, vuelve el problema de mantener dos."""
    cuerpo = fuente[fuente.index("{% endmacro %}"):]
    sueltas = [c for c in RESPUESTAS if f'name="{c}"' in cuerpo]
    assert not sueltas, (
        f"Estos campos quedaron escritos a mano fuera del macro: {sueltas}. "
        "Con dos lugares, el próximo cambio se olvida de uno."
    )
