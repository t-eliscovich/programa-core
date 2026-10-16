"""El borrador de «Producir» en /pedidos?corte=tela (Tamara 16/10/2026).

"Cuando hice la mitad y quiero seguir más tarde, un botón de guardar y que
se mantenga hasta que mande." Vive en el servidor (tabla
`scintela.pedidos_producir_borrador`, migración 0265) y no en el navegador:
así sigue estando aunque se abra desde otra compu. Es DE LA OFICINA, uno
solo para todos (Tamara 16/10: "Irene no ve lo que guardé"): lo que guarda
una lo sigue la otra. `usuario` queda como quién guardó cada color.

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


def leer() -> dict[str, tuple]:
    """{clave: (rollos, rib_kg, cuellos, puños)} de lo guardado por la oficina
    (si dos guardaron el mismo color, vale lo último)."""
    try:
        filas = db.fetch_all(
            "SELECT DISTINCT ON (clave) clave, rollos, rib_kg, cuellos, punos "
            "FROM scintela.pedidos_producir_borrador "
            "ORDER BY clave, actualizado DESC")
    except Exception as e:  # noqa: BLE001
        _log.warning("borrador de Producir: no se pudo leer (%s)", e)
        return {}
    return {r["clave"]: (float(r["rollos"] or 0), float(r["rib_kg"] or 0),
                         float(r.get("cuellos") or 0), float(r.get("punos") or 0))
            for r in (filas or [])}


def quien() -> tuple[str, str]:
    """(usuario, 'dd/mm hh:mm') del último que guardó — para la barra."""
    try:
        r = db.fetch_one(
            "SELECT usuario, to_char(actualizado AT TIME ZONE 'America/Guayaquil', "
            "'DD/MM HH24:MI') AS cuando FROM scintela.pedidos_producir_borrador "
            "ORDER BY actualizado DESC LIMIT 1")
    except Exception:  # noqa: BLE001
        return "", ""
    return ((r or {}).get("usuario") or "", (r or {}).get("cuando") or "")


def guardar(usuario: str, claves: list[str], valores: dict) -> int:
    """Reemplaza lo guardado de las `claves` de la pantalla por `valores`
    ({clave: [rollos, rib, cuellos, puños]}), sea quien sea que lo había
    guardado. Lo de otras familias no se toca. Devuelve cuántos colores
    quedaron guardados de esta pantalla."""
    if not usuario or not claves:
        return 0
    filas = []
    for clave in claves:
        v = [_num(x) for x in (list(valores.get(clave) or []) + [0, 0, 0, 0])[:4]]
        if any(x > 0 for x in v):
            filas.append((usuario, clave, *v))
    with db.tx() as conn:
        db.execute("DELETE FROM scintela.pedidos_producir_borrador "
                   "WHERE clave = ANY(%s)", (list(claves),), conn=conn)
        for f in filas:
            db.execute("INSERT INTO scintela.pedidos_producir_borrador "
                       "(usuario, clave, rollos, rib_kg, cuellos, punos) "
                       "VALUES (%s, %s, %s, %s, %s, %s)",
                       f, conn=conn)
    return len(filas)


def borrar(claves: list[str]) -> None:
    """Saca del borrador los colores que ya se mandaron. Fail-soft."""
    if not claves:
        return
    try:
        db.execute("DELETE FROM scintela.pedidos_producir_borrador "
                   "WHERE clave = ANY(%s)", (list(claves),))
    except Exception as e:  # noqa: BLE001
        _log.warning("borrador de Producir: no se pudo borrar (%s)", e)
