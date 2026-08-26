"""/admin/debug-costo-importacion — lo cargado contra lo que el hilo VALE.

TMT 2026-08-26. La alarma de importaciones comparaba el US$/kg contra una banda
única (2,7–3,4) y saltó con MH 66-67. Andrés cerró el caso: *"No falta nada por
pasar. Ese hilo es de poliéster, que tiene un precio menor al polialgodón. Un
hilo de polialgodón está en este momento a 2,75, mientras que uno de poliéster
está a 1,70"*. O sea que la banda no era una banda: era el precio de UN tipo de
hilo, y la alarma no encontró un error sino hilo más barato.

`asinfo.service.importaciones_costo_estimado()` ya arma el promedio US$/kg por
PRODUCTO (= tipo de hilado) con ventana de 3 meses y caída a 6, y marca aparte
los kilos cuyo hilado no tiene histórico. Está escrito desde el 29/06 y no lo
usa nadie. Esta pantalla lo pone al lado de lo que el programa tiene cargado,
para fijar el corte de la alarma CON EL DATO — la misma razón por la que existe
/admin/debug-maduracion-importacion.

Qué mirar: la columna `ratio` (cargado ÷ esperado). El esperado sale del precio
de la factura del proveedor; lo cargado en el programa incluye además CAE, flete
y seguro, así que lo normal es un ratio arriba de 1. El piso de esa nube es el
corte de "le falta plata", y el techo, el de "le faltan kilos".

SOLO LECTURA.

    ?meses=12   ventana hacia atrás (default 12)
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso
from filters import today_ec

bp = Blueprint(
    "admin_debug_costo_importacion",
    __name__,
    url_prefix="/admin/debug-costo-importacion",
)


def _json(payload, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


def _pct(vals: list[float], p: float):
    if not vals:
        return None
    xs = sorted(vals)
    i = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))
    return round(xs[i], 3)


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    from modules.asinfo import service as asinfo_service
    from modules.importaciones import service as svc
    from modules.importaciones import vigilancia as vig

    try:
        meses = int(request.args.get("meses") or 12)
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "meses invalido"}, 400)
    meses = max(1, min(60, meses))
    desde = today_ec() - timedelta(days=31 * meses)

    try:
        rows = svc.importaciones_con_cruce(limite=1000)
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": f"cruce falló: {e}"}, 502)
    try:
        costos = asinfo_service.importaciones_costo_estimado(limite=1000)
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": f"costo estimado falló: {e}"}, 502)
    if not costos:
        return _json({"ok": False, "error": "Asinfo no devolvió costos"}, 502)

    # El MISMO agrupado que usa la alarma: las dos mitades de una partida son
    # una sola mercadería y su plata puede estar colgada de cualquiera.
    grupos = vig._grupos_recibidos(rows)

    filas, sin_precio, ratios = [], [], []
    for gid, g in grupos.items():
        if not isinstance(g.get("recepcion"), date) or g["recepcion"] < desde:
            continue
        kg = float(g.get("kg") or 0)
        if kg <= 0:
            continue
        esperado, falta_historico = 0.0, False
        ventanas = set()
        for im in (g.get("ims") or []):
            c = costos.get(str(im).strip())
            if not c or float(c.get("kg_sin_precio") or 0) > 0:
                falta_historico = True
                continue
            esperado += float(c.get("costo") or 0)
            ventanas.add(c.get("ventana"))
        base = {
            "grupo": gid,
            "codigo": g.get("codigo"),
            "recepcion": str(g.get("recepcion")),
            "kg": round(kg, 2),
            "cargado": round(float(g.get("importe") or 0), 2),
            "cargado_kg": round(float(g.get("importe") or 0) / kg, 4),
        }
        if falta_historico or esperado <= 0:
            sin_precio.append({**base, "motivo": "ese hilado no tiene histórico"})
            continue
        ratio = base["cargado"] / esperado
        ratios.append(ratio)
        filas.append({
            **base,
            "esperado": round(esperado, 2),
            "esperado_kg": round(esperado / kg, 4),
            "ratio": round(ratio, 3),
            "ventana": "6m" if "6m" in ventanas else "3m",
        })

    filas.sort(key=lambda f: f["ratio"])
    return _json({
        "ok": True,
        "meses": meses,
        "grupos": len(filas),
        "ratio": {
            "p05": _pct(ratios, 0.05), "p10": _pct(ratios, 0.10),
            "p50": _pct(ratios, 0.50), "p90": _pct(ratios, 0.90),
            "p95": _pct(ratios, 0.95),
        },
        "filas": filas,
        "sin_precio": sin_precio,
    })
