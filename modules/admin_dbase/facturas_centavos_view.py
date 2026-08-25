"""Endpoint /admin/facturas-centavos — cerrar las facturas que quedaron por centavos.

TMT 2026-08-25 (dueña): *"perfecto dale"*, sobre las 90 facturas abiertas con
dos centavos o menos de saldo (USD 0,94 en total, 12 de agosto y 42 de julio).

De dónde salen: el importe que carga Programa Core aplica el IVA renglón por
renglón y el de la factura de papel sale de la cabecera. Redondear doce veces y
sumar no da lo mismo que sumar y redondear una, así que en una de cada cuatro
facturas los dos números difieren en un centavo. El cliente paga lo que dice el
papel, la factura queda con el centavo colgado, y sigue en la cartera sumando
días de vencida por nada. Ver `/admin/debug-asinfo-facturas/card-importe`, que
arregla el origen para las que entren de acá en adelante.

Dos frenos, porque esto cierra facturas de verdad:

· Sólo las que YA TIENEN algo cobrado (abono o retención). Una factura de dos
  centavos que nadie pagó nunca es una factura impaga, no un resto de redondeo.
· Sólo Z y A. Una T ya está cerrada y una X está anulada.

Cierra con `factura_cambiar_stat_a_t`, la MISMA función del desplegable del
estado de cuenta: queda en `mov_doble` con su foto del saldo previo y se puede
reabrir desde la pantalla, una por una. No hay UPDATE suelto acá.

GET = dry-run (no toca nada). `?aplicar=1` escribe.
"""
from __future__ import annotations

from flask import Blueprint, Response, g, request, stream_with_context

from auth import requiere_login, requiere_permiso

bp = Blueprint("facturas_centavos", __name__, url_prefix="/admin/facturas-centavos")

#: Hasta acá un saldo es redondeo, no una deuda. Dos centavos: la diferencia
#: medida nunca pasó de uno, y el segundo es para el caso de dos documentos.
UMBRAL = 0.02

_SQL = """
    SELECT id_factura, numf, numf_completo, codigo_cli, fecha,
           importe, abono, retencion, saldo, stat
      FROM scintela.factura
     WHERE saldo > 0
       AND saldo <= %s
       AND COALESCE(stat, '') IN ('Z', 'A')
       AND COALESCE(abono, 0) + COALESCE(retencion, 0) > 0
     ORDER BY fecha, numf
"""


def candidatas(umbral: float = UMBRAL) -> list[dict]:
    """Las facturas que quedaron abiertas por centavos. Sólo lectura."""
    import db

    return db.fetch_all(_SQL, (umbral,)) or []


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def run():
    aplicar = (request.args.get("aplicar") or "").strip() in ("1", "true", "on")
    usuario = getattr(getattr(g, "user", None), "username", None) or "web"
    return Response(stream_with_context(_run(aplicar, usuario)),
                    mimetype="text/plain; charset=utf-8")


def _run(aplicar: bool, usuario: str):
    from modules.informes import queries as iq

    def line(m=""):
        return m.rstrip("\n") + "\n"

    modo = "APLICAR" if aplicar else "DRY-RUN (no toca nada)"
    yield line(f"=== Facturas abiertas por centavos — {modo} ===")
    yield line(f"Umbral: saldo hasta {UMBRAL:.2f}, con algo ya cobrado, en Z o A.")
    yield line("")

    filas = candidatas()
    total = sum(float(f.get("saldo") or 0) for f in filas)
    yield line(f"Son {len(filas)} facturas, {total:.2f} en total.")
    yield line("")
    for f in filas:
        yield line(
            f"  {f.get('codigo_cli') or '???':<4} "
            f"{f.get('numf_completo') or f.get('numf')}  "
            f"{f.get('fecha')}  importe {float(f.get('importe') or 0):>10.2f}  "
            f"saldo {float(f.get('saldo') or 0):.2f}  ({f.get('stat')})"
        )
    yield line("")

    if not aplicar:
        yield line("Dry-run: no se cerró ninguna. Agregá ?aplicar=1 para cerrarlas.")
        return
    if not filas:
        yield line("No hay nada que cerrar.")
        return

    ok = 0
    for f in filas:
        try:
            iq.factura_cambiar_stat_a_t(
                int(f["id_factura"]), f.get("codigo_cli") or "", usuario=usuario)
            ok += 1
        except Exception as exc:  # noqa: BLE001 — una mala no frena a las otras
            yield line(f"  [ERROR] {f.get('numf_completo') or f.get('numf')}: {exc!r}")
    yield line("")
    yield line(f"Cerradas {ok} de {len(filas)}. Cada una queda en el historial "
               "y se puede reabrir desde el estado de cuenta.")
