"""Memos que cambiaron en Asinfo después de mandarlos (2026-09-04).

David (audio 04/09): *"Irene le modificó un pedido de un memo que ya fue
enviado por el sistema a David, pero a David no le refleja ese cambio…
que si Irene cambia, aumenta, quita o elimina la cantidad o el ítem,
se le actualice a David y le dé un mensaje de alerta: pedido tal
modificado, revisar"*.

El memo es una FOTO del pedido al momento de enviar. Este módulo la
mantiene al día: cada pocos minutos (del hilo de fondo de
`autocarga_facturas`, sin cron del EC2 como todo lo demás) mira
`pedido_cliente.fecha_modificacion` en Asinfo para los memos VIVOS
('pendiente' / 'en_proceso'). Si el pedido se tocó después del envío,
arma la foto nueva con `service.armar_memo`, la compara línea por línea
con la vieja, pisa `detalle` en formulas_app y deja en `cambios` qué
cambió, en castellano. La fábrica lo ve como alerta roja en /memos y en
la campanita hasta que apreta "Visto".

Por qué `fecha_modificacion` y no comparar cantidades a secas: las
cantidades del memo son el SALDO comprometido, que también baja con cada
despacho parcial. Verificado 04/09: el despacho NO toca
`pedido_cliente.fecha_modificacion` (pedidos con despacho y
fecha_modificacion NULL) — sólo la edición desde Ventas la sella. Así una
entrega no dispara "modificado".

⚠ `fecha_modificacion` viene en hora ECUADOR (como todas las columnas
datetime guardadas de Asinfo); `enviado_en` del memo es TIMESTAMPTZ. Se
compara llevando la de Asinfo a UTC (+5 h).

Guard sin tabla: la fecha de modificación ya procesada queda adentro del
`detalle` (`asinfo_modificado`), que es de Programa Core — un memo se
mira una sola vez por cada edición. Un pedido que ya no está entre los
pendientes (se despachó entero) no se puede re-fotografiar: se deja como
está. `MEMOS_SYNC_AUTO=0` lo apaga.
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from datetime import UTC, datetime, timedelta

from modules._lib import formulas_memos, metabase_client

_LOG = logging.getLogger("programa_core.pedidos.memos_sync")

_CHECK_MIN_SECS = 180  # mirar Asinfo a lo sumo cada 3 min
_lock = threading.Lock()
_ultimo_check = 0.0

_SQL_MODIFICADOS = """
SELECT p.numero,
       CONVERT(varchar(19), p.fecha_modificacion, 120) AS modificado,
       ISNULL(p.usuario_modificacion, '') AS usuario
  FROM pedido_cliente p
 WHERE p.numero IN ({in_list})
   AND p.fecha_modificacion IS NOT NULL
"""


def _numero_seguro(numero: str) -> str:
    """Sólo lo que puede ser un número de pedido ('PDCL-30949'): va
    interpolado en el IN de la SQL."""
    return "".join(
        c for c in (numero or "").strip().upper() if c.isalnum() or c == "-"
    )[:20]


def _a_utc(modificado_ec: str) -> datetime | None:
    """'YYYY-MM-DD HH:MM:SS' en hora Ecuador → datetime aware en UTC."""
    try:
        naive = datetime.strptime(modificado_ec[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (naive + timedelta(hours=5)).replace(tzinfo=UTC)


def _num(v) -> str:
    """5.0 → '5', 12.5 → '12,5' (coma decimal, como toda la app)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v or "")
    s = f"{f:.1f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _rotulo(linea: dict) -> str:
    partes = [p for p in (linea.get("tela"), linea.get("color")) if p]
    prod = linea.get("producto") or ""
    return f"{prod} ({' '.join(partes)})" if partes else prod


def diferencias(viejo: dict, nuevo: dict) -> list[str]:
    """Qué cambió entre dos fotos del pedido, en frases para la fábrica.
    Compara por producto: agregado, sacado, cantidad distinta; y la nota
    del pedido. [] si lo que ve la fábrica quedó igual."""
    v_lineas = {ln.get("producto"): ln for ln in (viejo.get("lineas") or []) if ln.get("producto")}
    n_lineas = {ln.get("producto"): ln for ln in (nuevo.get("lineas") or []) if ln.get("producto")}
    out: list[str] = []
    for prod, ln in n_lineas.items():
        if prod not in v_lineas:
            out.append(f"Agregó {_rotulo(ln)}: {_num(ln.get('cantidad'))} {ln.get('unidad') or ''}".rstrip())
    for prod, ln in v_lineas.items():
        if prod not in n_lineas:
            out.append(f"Sacó {_rotulo(ln)}: tenía {_num(ln.get('cantidad'))} {ln.get('unidad') or ''}".rstrip())
    for prod, ln in n_lineas.items():
        vl = v_lineas.get(prod)
        if vl is None:
            continue
        a, b = _num(vl.get("cantidad")), _num(ln.get("cantidad"))
        ua, ub = vl.get("unidad") or "", ln.get("unidad") or ""
        if a != b or ua != ub:
            out.append(f"{_rotulo(ln)}: {a} {ua} → {b} {ub}".replace("  ", " ").rstrip())
    if (viejo.get("descripcion") or "") != (nuevo.get("descripcion") or ""):
        out.append(f"Nota del pedido: «{nuevo.get('descripcion') or '(sin nota)'}»")
    return out


def _modificados_en_asinfo(numeros: list[str]) -> tuple[dict[str, tuple[str, str]], bool]:
    """`{numero: (fecha_modificacion EC 'YYYY-MM-DD HH:MM:SS', usuario)}`
    de los pedidos que Asinfo tiene sellados como modificados."""
    from modules.pedidos.service import ASINFO_DB
    seguros = sorted({_numero_seguro(n) for n in numeros if _numero_seguro(n)})
    if not seguros:
        return {}, True
    in_list = ", ".join(f"'{n}'" for n in seguros)
    filas, ok = metabase_client.fetch_dataset_estado(
        ASINFO_DB, _SQL_MODIFICADOS.format(in_list=in_list))
    if not ok:
        return {}, False
    out = {}
    for r in filas:
        n = str(r.get("numero") or "").strip().upper()
        if n:
            out[n] = (str(r.get("modificado") or "")[:19], str(r.get("usuario") or "").strip())
    return out, True


def sincronizar(usuario: str = "auto-sync-memos") -> dict:
    """Una pasada: mira los memos vivos contra Asinfo y pisa los que
    cambiaron. Devuelve {"revisados", "actualizados": [numeros con
    alerta], "silenciosos": [numeros refrescados sin cambios visibles],
    "disponible": si Asinfo contestó}."""
    from modules.pedidos import service

    res = {"revisados": 0, "actualizados": [], "silenciosos": [], "disponible": True}
    memos = formulas_memos.vivos()
    if not memos:
        return res
    res["revisados"] = len(memos)
    mods, ok = _modificados_en_asinfo([m["numero"] for m in memos])
    if not ok:
        res["disponible"] = False
        return res

    cache_limpio = False
    for m in memos:
        numero = str(m["numero"]).strip().upper()
        sello = mods.get(numero)
        if not sello:
            continue
        mod_ec, quien = sello
        viejo = m.get("detalle") or {}
        if viejo.get("asinfo_modificado") == mod_ec:
            continue  # esta edición ya se procesó
        mod_utc = _a_utc(mod_ec)
        enviado = m.get("enviado_en")
        if mod_utc is None or enviado is None:
            continue
        if enviado.tzinfo is None:
            enviado = enviado.replace(tzinfo=UTC)
        if mod_utc <= enviado:
            # Se editó ANTES de mandar el memo: la foto ya lo trae. Se deja
            # anotado para no volver a mirarlo.
            nuevo = dict(viejo, asinfo_modificado=mod_ec)
            ok_upd, _ = formulas_memos.actualizar(numero, nuevo, None, "")
            if ok_upd:
                res["silenciosos"].append(numero)
            continue
        if not cache_limpio:
            # La foto nueva tiene que salir de Asinfo AHORA, no del cache de
            # 5 min de la pantalla.
            service._CACHE.pop("por_pedido", None)
            cache_limpio = True
        nuevo = service.armar_memo(numero)
        if nuevo is None:
            continue  # ya no está entre los pendientes: no hay foto nueva
        nuevo["asinfo_modificado"] = mod_ec
        lineas = diferencias(viejo, nuevo)
        cambio = None
        if lineas:
            cambio = {
                "en": datetime.now(UTC).isoformat(timespec="seconds"),
                "por": quien,
                "asinfo_modificado": mod_ec,
                "lineas": lineas,
            }
        ok_upd, _ = formulas_memos.actualizar(
            numero, nuevo, cambio, f"Asinfo · {quien}" if quien else "Asinfo")
        if not ok_upd:
            continue
        (res["actualizados"] if cambio else res["silenciosos"]).append(numero)
    return res


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo. Nunca levanta."""
    res = {"corrio": False}
    if os.environ.get("MEMOS_SYNC_AUTO", "1") == "0" or not formulas_memos.disponible():
        return res
    global _ultimo_check
    ahora = _time.monotonic()
    with _lock:
        if _ultimo_check and (ahora - _ultimo_check) < _CHECK_MIN_SECS:
            return res
        _ultimo_check = ahora
    try:
        res["corrio"] = True
        res["reporte"] = sincronizar()
    except Exception as e:  # noqa: BLE001 — el hilo no se cae por esto
        _LOG.warning("sync memos (fondo): %s", e)
    return res
