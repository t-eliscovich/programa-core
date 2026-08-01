"""/admin/debug-maduracion-importacion — cuánto tarda una importación en tener
TODA su plata cargada, contado desde el día que llegó la mercadería.

TMT 2026-07-31, sobre la alarma de banda: *"pero creo que lo cargará Andrés
cuando llegue"*. Tiene razón, y eso es lo que vuelve inútil la alarma tal como
estaba: los kilos entran a Asinfo el día que llega el contenedor, y la factura,
el CAE y el flete se cargan después, cuando llegan los papeles. O sea que TODA
importación pasa por una ventana en la que tiene todos los kilos y sólo parte de
la plata, y en esa ventana el US$/kg da bajo. La alarma no estaba encontrando un
error: estaba encontrando el estado normal de una importación reciente.

La versión que sirve es *"está fuera de banda **y ya pasaron X días** desde que
llegó"*. Este endpoint existe para sacar la X del dato y no inventarla.

Qué hace: por cada GRUPO de importación recibido, ordena sus compras/anticipos
por fecha, acumula la plata y busca el primer día en que el US$/kg acumulado
entra en la banda. `dias_hasta_banda` = ese día menos la fecha de recepción.

Los grupos que todavía NO llegaron a la banda salen aparte con su edad: son los
que hoy dispararían la alarma, y hay que mirarlos contra la distribución para
saber cuáles están "en proceso" y cuáles "se quedaron así".

SOLO LECTURA.

    ?meses=12   ventana hacia atrás (default 12)
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso
from filters import today_ec

bp = Blueprint(
    "admin_debug_maduracion_import",
    __name__,
    url_prefix="/admin/debug-maduracion-importacion",
)


def _json(payload, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _pct(vals: list[int], p: float):
    if not vals:
        return None
    xs = sorted(vals)
    i = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))
    return xs[i]


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    from modules.importaciones import service as svc

    try:
        meses = int(request.args.get("meses") or 12)
    except (TypeError, ValueError):
        return _json({"ok": False, "error": "meses invalido"}, 400)
    meses = max(1, min(60, meses))

    hoy = today_ec()
    desde = hoy - timedelta(days=31 * meses)

    try:
        rows = svc.importaciones_con_cruce(limite=1000)
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": f"cruce falló: {e}"}, 502)

    lo, hi = svc.BANDA_USD_KG

    # Juntar por GRUPO: las dos mitades de una partida son una sola mercadería,
    # y sus compras pueden estar colgadas de cualquiera de las dos.
    grupos: dict[str, dict] = {}
    for r in rows:
        if not r.get("recibida"):
            continue
        frec = _d(r.get("fecha_recepcion"))
        if not frec or frec < desde:
            continue
        gid = str(r.get("grupo_id") or r.get("im_numero") or "")
        if not gid:
            continue
        g = grupos.setdefault(gid, {
            "codigo": f"{r.get('prov')} {r.get('numero')}"
                      + (f"-{r.get('numero_hasta')}" if r.get("numero_hasta") else ""),
            "ims": r.get("grupo_ims") or [r.get("im_numero")],
            "kg": float(r.get("grupo_kg") or r.get("kg") or 0),
            "recepcion": frec,
            "items": {},          # id → (fecha, importe) — dedup entre mitades
            "anticipo_items": [],
        })
        # La recepción del grupo es la más TARDÍA: la plata no puede estar
        # completa antes de que haya llegado todo.
        if frec > g["recepcion"]:
            g["recepcion"] = frec
        for it in ((r.get("compra") or {}).get("items") or []):
            g["items"][it.get("id_compra")] = (it.get("fecha"), it.get("importe"))
        for it in ((r.get("anticipo") or {}).get("items") or []):
            g["anticipo_items"].append((it.get("fecha"), it.get("importe")))

    maduros, verdes, sin_datos = [], [], []
    for gid, g in grupos.items():
        kg = g["kg"]
        if kg <= 0:
            sin_datos.append({"grupo": gid, "codigo": g["codigo"], "motivo": "sin kg"})
            continue
        movs = list(g["items"].values()) or g["anticipo_items"]
        movs = [(_d(f), float(i or 0)) for f, i in movs if _d(f)]
        if not movs:
            sin_datos.append({"grupo": gid, "codigo": g["codigo"],
                              "motivo": "sin compras ni anticipos fechados"})
            continue
        movs.sort(key=lambda x: x[0])
        acum = 0.0
        fecha_banda = None
        for f, imp in movs:
            acum += imp
            if acum / kg >= lo:
                fecha_banda = f
                break
        total_ukg = round(sum(i for _f, i in movs) / kg, 4)
        base = {
            "grupo": gid, "codigo": g["codigo"], "ims": g["ims"],
            "kg": round(kg, 2), "recepcion": str(g["recepcion"]),
            "usd_kg_hoy": total_ukg, "n_movimientos": len(movs),
            "primer_mov": str(movs[0][0]), "ultimo_mov": str(movs[-1][0]),
        }
        if fecha_banda is None:
            base["dias_desde_recepcion"] = (hoy - g["recepcion"]).days
            verdes.append(base)
        else:
            base["fecha_entra_en_banda"] = str(fecha_banda)
            # Puede ser negativo: la plata a veces se carga ANTES de que llegue
            # el contenedor (anticipos). Eso también es información.
            base["dias_hasta_banda"] = (fecha_banda - g["recepcion"]).days
            maduros.append(base)

    maduros.sort(key=lambda x: -x["dias_hasta_banda"])
    verdes.sort(key=lambda x: -x["dias_desde_recepcion"])

    dias = [m["dias_hasta_banda"] for m in maduros]
    positivos = [d for d in dias if d > 0]
    resumen = {
        "n_grupos_recibidos": len(grupos),
        "n_llegaron_a_banda": len(maduros),
        "n_todavia_abajo": len(verdes),
        "n_sin_datos": len(sin_datos),
        "dias_hasta_banda": {
            "min": min(dias) if dias else None,
            "p50": _pct(dias, 0.50), "p75": _pct(dias, 0.75),
            "p90": _pct(dias, 0.90), "p95": _pct(dias, 0.95),
            "max": max(dias) if dias else None,
            "n_negativos_o_cero": len(dias) - len(positivos),
        },
        "solo_los_que_tardaron": {
            "n": len(positivos),
            "p50": _pct(positivos, 0.50), "p90": _pct(positivos, 0.90),
            "p95": _pct(positivos, 0.95),
            "max": max(positivos) if positivos else None,
        },
        "lectura": ("El umbral de la alarma tiene que dejar pasar la maduración "
                    "normal. Un buen candidato es el p90/p95 de 'solo_los_que_"
                    "tardaron': por debajo de eso, 'esta bajo' significa 'se esta "
                    "cargando', no 'esta mal'."),
    }
    return _json({
        "ok": True, "meses": meses, "desde": str(desde),
        "banda": [lo, hi], "resumen": resumen,
        "todavia_abajo_de_la_banda": verdes,
        "maduros": maduros[:80],
        "sin_datos": sin_datos[:30],
    })
