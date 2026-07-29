"""Todo mov_doble activo tiene vuelta atrás — y la app la dice bien.

TMT 2026-07-29. El chequeo de salud reportaba 17 tipos "sin reverso
automatizado" (1.776 movs activos). Yendo al código uno por uno, NINGUNO
necesitaba un handler escrito de cero:

  - `cheque_depositado` (913 movs, el tipo más grande del sistema) y
    `banco_clasificado_gasto` ya tenían wizard de confirmación propio
    (`cheques.deshacer_deposito`, `gastos.confirmar_desclasificar`) — sólo
    faltaba enchufarlos al dispatcher.
  - 5 tipos más se deshacen desde otra pantalla, con un POST por período o
    por lote que el dispatcher no puede redirigir. Para ésos existe
    `_REVERSO_BLOQUEADO`, que estaba VACÍO desde el audit de mayo: su
    mensaje nunca se mostró y la app le decía a la dueña "no tiene reverso
    automatizado" sobre 521 movimientos que sí se podían deshacer.

Estos tests fijan las dos mitades: que los dos wizards estén enchufados, y
que ningún tipo con reverso en otra pantalla quede sin ruta.
"""
from __future__ import annotations

from modules.historial.views import (
    _PERMISO_REVERSO_INLINE,
    _REVERSO_BLOQUEADO,
    _REVERSO_DISPATCH,
)


def test_cheque_depositado_va_al_wizard_de_deshacer_deposito():
    """913 movs activos. `deshacer_deposito` devuelve el cheque a cartera Y
    baja el depósito del banco por su importe — reverso completo."""
    assert "cheque_depositado" in _REVERSO_DISPATCH
    endpoint, kwargs_fn = _REVERSO_DISPATCH["cheque_depositado"]
    assert endpoint == "cheques.deshacer_deposito"
    # El wizard toma id_cheque, y en este mov el cheque es el ORIGEN.
    assert kwargs_fn({"origen_id": 77, "destino_id": 5}) == {"id_cheque": 77}


def test_banco_clasificado_gasto_desclasifica_por_el_xgast():
    """Misma forma que `caja_s_to_xgast`: el gasto es el DESTINO, no el
    origen. Pasar origen_id mandaría a desclasificar el xgast equivocado —
    el origen es la transacción bancaria."""
    assert "banco_clasificado_gasto" in _REVERSO_DISPATCH
    endpoint, kwargs_fn = _REVERSO_DISPATCH["banco_clasificado_gasto"]
    assert endpoint == "gastos.confirmar_desclasificar"
    assert kwargs_fn({"origen_id": 44053, "destino_id": 912}) == {"id_xgast": 912}
    # Y exactamente el mismo endpoint que el hermano de caja.
    assert _REVERSO_DISPATCH["caja_s_to_xgast"][0] == endpoint


def test_los_tipos_sin_wizard_tienen_ruta_escrita_no_el_mensaje_generico():
    """`_REVERSO_BLOQUEADO` existía vacío: el `flash` de "Reverso no
    automatizado para este tipo. {mensaje}" nunca se disparaba."""
    assert _REVERSO_BLOQUEADO, (
        "el dict está vacío otra vez — los tipos que se deshacen en otra "
        "pantalla vuelven a caer en el mensaje genérico, que dice que no hay "
        "reverso cuando sí lo hay"
    )
    for tipo, msg in _REVERSO_BLOQUEADO.items():
        assert len(msg) > 40, f"{tipo}: el mensaje no alcanza para guiar a nadie"
        # Tiene que decir DÓNDE, no sólo que no se puede acá.
        assert any(p in msg.lower() for p in ("desde", "→")), (
            f"{tipo}: el mensaje no indica en qué pantalla se deshace"
        )


def test_todos_los_endpoints_del_dispatcher_existen(app):
    """Un nombre de endpoint mal escrito no falla al importar: falla en
    `url_for` cuando la dueña aprieta «reversar», con un error genérico. Que
    lo agarre el test."""
    registrados = {r.endpoint for r in app.url_map.iter_rules()}
    faltan = sorted(
        {ep for ep, _ in _REVERSO_DISPATCH.values()} - registrados
    )
    assert not faltan, f"el dispatcher apunta a endpoints que no existen: {faltan}"


def test_ningun_tipo_esta_en_dos_tablas_a_la_vez():
    """Si un tipo está en el dispatcher Y en bloqueados, gana el bloqueado
    (el `if tipo in _REVERSO_BLOQUEADO` va primero) y el wizard queda
    muerto sin que nadie se entere."""
    dobles = set(_REVERSO_DISPATCH) & set(_REVERSO_BLOQUEADO)
    assert not dobles, (
        f"{sorted(dobles)} están en dispatch y en bloqueado: el mensaje "
        "gana y el wizard nunca se ejecuta"
    )
    dobles_inline = set(_REVERSO_BLOQUEADO) & set(_PERMISO_REVERSO_INLINE)
    assert not dobles_inline, f"{sorted(dobles_inline)} bloqueados pero con reverso inline"
