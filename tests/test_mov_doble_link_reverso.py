"""«Todas tienen que tener link a deshacer» — dueña, 29/07/2026.

Un mov_doble en `estado='reversado'` con `id_reverso IS NULL` es un renglón
tachado sin la contrapartida al lado: el chequeo de salud reportaba 30 así.
Ninguno era plata perdida, pero el libro de auditoría no se podía leer
completo.

Había dos formas distintas de llegar ahí, y este test fija las dos:

  1. **UPDATE pelado, sin reverso ninguno.** `dolares/views.cancelar_anticipo`
     y `gastos._clasificar_caja_dentro_tx` marcaban `estado='reversado'` con
     un UPDATE y no creaban la fila del reverso. De ahí salen los dos
     anticipos USD de $18.000 del 08/07 y el `caja_s_to_compra_proveedor`
     del 15/05. Ahora usan `mov_doble.registrar(..., id_original=…)`, que
     crea el reverso Y linkea el original de una sola vez.

  2. **Marcado en bloque sin decir a quién apuntar.** Al reversar un cheque
     o deshacer un neteo, los hijos se marcan en bloque a propósito (el
     reverso es UNO, el del padre) — pero quedaban con `id_reverso NULL`.
     Ahora el bloque apunta al reverso del padre. Los 27 movs de PUE del
     15/05 son de acá.

El test es de CÓDIGO, no de base: mira que ningún UPDATE que ponga
`estado='reversado'` deje de setear `id_reverso`. Es la única forma de que
esto no vuelva a filtrarse en el próximo módulo.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent

# Archivos que tocan mov_doble.estado. mov_doble.py es la implementación
# canónica (ahí sí se setea id_reverso, es su trabajo).
_FUENTES = sorted(
    p for p in (_RAIZ / "modules").rglob("*.py")
    if "estado='reversado'" in p.read_text(encoding="utf-8")
    or "estado = 'reversado'" in p.read_text(encoding="utf-8")
)

# Un UPDATE puede legítimamente NO setear id_reverso en un solo caso: cuando
# lo que marca reversado es un reverso que se está DES-haciendo (des-reverso).
# Ahí la fila tachada es el reverso mismo y no hay "reverso del reverso".
_EXCEPCIONES = {
    # informes/queries._reactivar_factura_anulada: des-reversa la anulación
    # de una factura. Marca el 'reverso_factura_anulada' y reactiva el
    # original — es el camino inverso, no un reverso nuevo.
    ("informes/queries.py", "reverso_factura_anulada"),
}

_RE_UPDATE = re.compile(
    r"UPDATE\s+scintela\.mov_doble\s+(?:\n\s*)?SET\s+estado\s*=\s*'reversado'"
    r"(?P<resto>.{0,120})",
    re.IGNORECASE | re.DOTALL,
)


@pytest.mark.parametrize("ruta", _FUENTES, ids=lambda p: p.name)
def test_todo_update_a_reversado_setea_id_reverso(ruta: Path):
    txt = ruta.read_text(encoding="utf-8")
    rel = str(ruta.relative_to(_RAIZ / "modules"))
    for m in _RE_UPDATE.finditer(txt):
        resto = m.group("resto")
        if "id_reverso" in resto:
            continue
        # ¿Está en la lista de des-reversos legítimos?
        ventana = txt[max(0, m.start() - 600):m.end() + 200]
        if any(rel.endswith(f) and marca in ventana for f, marca in _EXCEPCIONES):
            continue
        linea = txt[:m.start()].count("\n") + 1
        pytest.fail(
            f"{rel}:{linea} marca mov_doble como 'reversado' sin setear "
            f"id_reverso. Eso deja un renglón tachado sin contrapartida y el "
            f"chequeo de salud lo reporta como link roto. Si el reverso es uno "
            f"solo (el del padre), pasale su id: "
            f"SET estado='reversado', id_reverso=%s."
        )


def test_hay_fuentes_para_revisar():
    """Si el regex deja de matchear nada, el test de arriba pasa en vacío."""
    assert _FUENTES, "no encontré ningún módulo que marque mov_doble reversado"


def test_dolares_y_gastos_registran_el_reverso():
    """Los dos que no creaban NINGUNA fila de reverso."""
    for rel, tipo in (("dolares/views.py", "reverso_dolares_anticipo"),
                      ("gastos/queries.py", "reverso_caja_s_to_compra_proveedor")):
        txt = (_RAIZ / "modules" / rel).read_text(encoding="utf-8")
        assert tipo in txt, f"{rel} dejó de registrar el reverso {tipo}"
        assert "id_original=md" in txt.replace(" ", "").replace("\n", "") or \
               "id_original=" in txt, f"{rel}: el reverso no linkea al original"
