"""«Mandar a hacer» → tabla `a_fabricar` de formulas_app (15/10/2026).

La oficina carga, en /pedidos (Tipo de tela), cuántos rollos mandar a hacer
de cada tela × color × acabado; revisa y aprieta «Mandar a producción». Eso
inserta UN renglón por color en `a_fabricar`, todos con el mismo `envio`.
Jonathan lo ve en la pestaña «Fabricación» de formulas_app y lo marca en
proceso / hecho. Programa Core lee esos estados para:

  - el historial de lo mandado (/pedidos/mandados);
  - el estado "Enviado a producción" de los pedidos que cubre cada renglón
    (no es un memo: el memo es más especial, Tamara 15/10) — esos pedidos
    dejan de contar en "Falta (sin memo)" mientras el renglón esté vivo.

Usa el MISMO pool y el mismo rol que los memos (`programa_core_memos`), que
en formulas_app tiene SELECT + INSERT sobre `a_fabricar` y nada más: el
estado lo cambia sólo la fábrica.

Fail-soft como todo el bridge: sin pool, `mandar()` devuelve
(False, "sin_bridge", "") y `envios()` devuelve ([], False).
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime

from modules._lib import formulas_memos

_log = logging.getLogger("programa_core.formulas_fabricar")

#: Estados en que un renglón todavía "cubre" sus pedidos.
VIVOS = ("pendiente", "en_proceso")


def _ec(dt):
    if not isinstance(dt, datetime):
        return None
    from zoneinfo import ZoneInfo
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo("America/Guayaquil"))


def _dm(dt) -> str:
    d = _ec(dt)
    return d.strftime("%d/%m") if d else ""


def _hm(dt) -> str:
    d = _ec(dt)
    return d.strftime("%d/%m %H:%M") if d else ""


def disponible() -> bool:
    return formulas_memos.disponible()


def nuevo_envio() -> str:
    """Id legible y único del envío: AF-20261015-1123-3f9a."""
    ahora = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return f"AF-{ahora}-{secrets.token_hex(2)}"


def mandar(lineas: list[dict], enviado_por: str) -> tuple[bool, str, str]:
    """Inserta todos los renglones de un envío en UNA transacción.

    Cada línea: {categoria, tela, acabado, color, codigo, cantidad (rollos de
    la tela), unidad, rib_kg (rib en kilos, Tamara 15/10: "Producir: Rollos |
    Rib"), pedidos: [{numero, cliente, cantidad}]}. Devuelve (ok, motivo, envio);
    motivos: "enviado", "vacio", "sin_bridge".
    """
    if not lineas:
        return False, "vacio", ""
    if not formulas_memos.disponible():
        return False, "sin_bridge", ""
    envio = nuevo_envio()
    # color_nombre (15/10/2026): Fabricación muestra "0412 VERDE PINO". Si
    # formulas todavía no tiene la columna, se manda sin ella — el envío no
    # se pierde por un nombre.
    for con_nombre in (True, False):
        try:
            _insertar(envio, enviado_por, lineas, con_nombre)
            return True, "enviado", envio
        except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
            if con_nombre and "color_nombre" in str(e):
                continue
            _log.warning("formulas_fabricar.mandar falló: %s", e)
            return False, "sin_bridge", ""
    return False, "sin_bridge", ""


def _insertar(envio: str, enviado_por: str, lineas: list[dict], con_nombre: bool) -> None:
    cols = ("envio, enviado_por, categoria, tela, acabado, color, codigo, cantidad, "
            "unidad, rib_kg, pedidos" + (", color_nombre" if con_nombre else ""))
    marcas = ", ".join(["%s"] * (12 if con_nombre else 11))
    with formulas_memos._conn() as c:
        with c.cursor() as cur:
            for ln in lineas:
                vals = (envio, enviado_por or "", ln.get("categoria") or "",
                        ln.get("tela") or "", ln.get("acabado") or "",
                        ln.get("color") or "", ln.get("codigo") or "",
                        ln.get("cantidad") or 0, ln.get("unidad") or "roll",
                        ln.get("rib_kg") or 0,
                        json.dumps(ln.get("pedidos") or [], default=str))
                if con_nombre:
                    vals += (ln.get("color_nombre") or "",)
                cur.execute(f"INSERT INTO a_fabricar ({cols}) VALUES ({marcas})", vals)
        c.commit()


def cancelar(usuario: str, envio: str = "", id_: int | None = None) -> tuple[int, str]:
    """Cancela un renglón (`id_`) o todos los de un `envio`, SÓLO los que
    siguen PENDIENTES (Tamara 15/10/2026: "que se pueda cancelar un pedido
    enviado"). Lo que la fábrica ya tomó no se toca. Devuelve
    (cuántos cambió, motivo): "cancelado", "no_pendiente", "sin_bridge".
    """
    if not formulas_memos.disponible():
        return 0, "sin_bridge"
    try:
        with formulas_memos._conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    UPDATE a_fabricar
                       SET estado = 'cancelado', cancelado_por = %s, cancelado_en = NOW()
                     WHERE estado = 'pendiente'
                       AND ((%s <> '' AND envio = %s) OR id = %s)
                 RETURNING id
                    """,
                    (usuario or "", envio or "", envio or "", id_ if id_ is not None else -1),
                )
                n = len(cur.fetchall())
            c.commit()
        return n, ("cancelado" if n else "no_pendiente")
    except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
        _log.warning("formulas_fabricar.cancelar falló: %s", e)
        return 0, "sin_bridge"


def envios(dias: int = 90) -> tuple[list[dict], bool]:
    """Los renglones mandados en los últimos `dias` días (más los que siguen
    vivos aunque sean viejos), más nuevo arriba. `(filas, ok)`."""
    if not formulas_memos.disponible():
        return [], False
    try:
        with formulas_memos._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT id, envio, enviado_por, enviado_en, categoria, tela,
                       acabado, color, codigo, cantidad, unidad, rib_kg, pedidos,
                       estado, en_proceso_por, en_proceso_en,
                       terminado_por, terminado_en, cancelado_por, cancelado_en
                  FROM a_fabricar
                 WHERE estado IN ('pendiente', 'en_proceso')
                    OR enviado_en >= NOW() - (%s || ' days')::interval
                 ORDER BY enviado_en DESC, id
                """,
                (str(int(dias)),),
            )
            cols = [d[0] for d in cur.description]
            filas = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
        for f in filas:
            if not isinstance(f.get("pedidos"), list):
                try:
                    f["pedidos"] = json.loads(f.get("pedidos") or "[]")
                except (TypeError, ValueError):
                    f["pedidos"] = []
            f["cantidad"] = float(f.get("cantidad") or 0)
            f["rib_kg"] = float(f.get("rib_kg") or 0)
            f["fecha_fmt"] = _dm(f.get("enviado_en"))
            f["hora_fmt"] = _hm(f.get("enviado_en"))
            f["en_proceso_fmt"] = _dm(f.get("en_proceso_en"))
            f["terminado_fmt"] = _dm(f.get("terminado_en"))
            f["cancelado_fmt"] = _dm(f.get("cancelado_en"))
        return filas, True
    except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
        _log.warning("formulas_fabricar.envios falló: %s", e)
        return [], False


def vivos_por_clave(filas: list[dict], clave_de) -> dict[str, dict]:
    """`{clave del renglón: el envío vivo más nuevo}` — para la marca
    "Mandado dd/mm · N roll" de la fila del color."""
    out: dict[str, dict] = {}
    for f in filas:  # vienen más nuevo arriba
        if f.get("estado") in VIVOS:
            out.setdefault(clave_de(f.get("codigo") or "", f.get("acabado") or ""), f)
    return out


def pedidos_enviados(filas: list[dict]) -> dict[str, dict]:
    """`{numero de pedido: renglón vivo que lo cubre}` — los pedidos
    "Enviados a producción"."""
    out: dict[str, dict] = {}
    for f in filas:
        if f.get("estado") in VIVOS:
            for p in f.get("pedidos") or []:
                n = str((p or {}).get("numero") or "").strip()
                if n:
                    out.setdefault(n, f)
    return out
