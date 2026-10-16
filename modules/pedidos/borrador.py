"""El borrador de «Producir» en /pedidos?corte=tela (Tamara 16/10/2026).

"Cuando hice la mitad y quiero seguir más tarde, un botón de guardar y que
se mantenga hasta que mande." Vive en el servidor (tabla
`scintela.pedidos_producir_borrador`, migración 0265) y no en el navegador:
así sigue estando aunque se abra desde otra compu. Es POR USUARIO: lo que
carga Andrés no le aparece a Tamara.

Fail-soft: si la tabla no está o la base no contesta, el borrador queda vacío
y la pantalla anda igual — sólo no se guarda.
"""
from __future__ import annotations

import logging

import db

_log = logging.getLogger(__name__)


def _num(v) -> float:
    try:
        return max(0.0, float(str(v if v is not None else "0").replace(",", ".")))
    except ValueError:
        return 0.0


def leer(usuario: str) -> dict[str, tuple[float, float]]:
    """{clave: (rollos, rib_kg)} de lo guardado por este usuario."""
    if not usuario:
        return {}
    try:
        filas = db.fetch_all(
            "SELECT clave, rollos, rib_kg FROM scintela.pedidos_producir_borrador "
            "WHERE usuario = %s", (usuario,))
    except Exception as e:  # noqa: BLE001
        _log.warning("borrador de Producir: no se pudo leer (%s)", e)
        return {}
    return {r["clave"]: (float(r["rollos"] or 0), float(r["rib_kg"] or 0))
            for r in (filas or [])}


def guardar(usuario: str, claves: list[str], valores: dict) -> int:
    """Reemplaza lo guardado de las `claves` de la pantalla por `valores`
    ({clave: [rollos, rib]}). Lo de otras familias no se toca. Devuelve
    cuántos colores quedaron guardados de esta pantalla."""
    if not usuario or not claves:
        return 0
    filas = []
    for clave in claves:
        v = list(valores.get(clave) or []) + [0, 0]
        r, k = _num(v[0]), _num(v[1])
        if r > 0 or k > 0:
            filas.append((usuario, clave, r, k))
    with db.tx() as conn:
        db.execute("DELETE FROM scintela.pedidos_producir_borrador "
                   "WHERE usuario = %s AND clave = ANY(%s)", (usuario, list(claves)),
                   conn=conn)
        for f in filas:
            db.execute("INSERT INTO scintela.pedidos_producir_borrador "
                       "(usuario, clave, rollos, rib_kg) VALUES (%s, %s, %s, %s)",
                       f, conn=conn)
    return len(filas)


def borrar(usuario: str, claves: list[str]) -> None:
    """Saca del borrador los colores que ya se mandaron. Fail-soft."""
    if not usuario or not claves:
        return
    try:
        db.execute("DELETE FROM scintela.pedidos_producir_borrador "
                   "WHERE usuario = %s AND clave = ANY(%s)", (usuario, list(claves)))
    except Exception as e:  # noqa: BLE001
        _log.warning("borrador de Producir: no se pudo borrar (%s)", e)
