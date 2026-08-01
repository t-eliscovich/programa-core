"""/admin/importaciones-sin-plata — qué importaciones llegaron y quedaron sin
toda su plata cargada, y forzar la revisión sin esperar el ciclo de fondo.

TMT 2026-07-31. La vigilancia corre sola cada 6 h colgada del ciclo de fondo,
pero el freno vive en memoria: después de borrar avisos (o de cambiar el umbral
con IMPORT_SIN_PLATA_DIAS) hay que esperar el próximo turno para verla actuar.
Este endpoint deja mirar la lista cuando uno quiere, y con `?correr=1` dispara
la revisión y deja los avisos en la campanita.

    ?dias=30     umbral (default: el de la vigilancia)
    ?techo=0     sin techo de antigüedad — muestra TODO el histórico
    ?correr=1    además de listar, deja los avisos

SOLO LECTURA salvo `?correr=1`, que sólo escribe avisos (nunca toca plata).
"""
from __future__ import annotations

import json

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_import_sin_plata",
    __name__,
    url_prefix="/admin/importaciones-sin-plata",
)


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    from modules.importaciones import service as svc
    from modules.importaciones import vigilancia as vig

    dias = request.args.get("dias")
    try:
        dias = int(dias) if dias else None
    except (TypeError, ValueError):
        return Response(json.dumps({"ok": False, "error": "dias invalido"}),
                        status=400, mimetype="application/json")
    if dias is not None and not (1 <= dias <= 3650):
        return Response(json.dumps({"ok": False, "error": "dias fuera de rango"}),
                        status=400, mimetype="application/json")

    techo = request.args.get("techo")
    try:
        techo = int(techo) if techo is not None else None
    except (TypeError, ValueError):
        return Response(json.dumps({"ok": False, "error": "techo invalido"}),
                        status=400, mimetype="application/json")
    # `?techo=0` muestra TODO el histórico. La alarma nunca lo hace — es para
    # mirar a mano lo que quedó viejo, que sigue existiendo aunque no avise.
    casos = vig.importaciones_fuera_de_banda(dias, techo=techo)
    corrida = None
    if (request.args.get("correr") or "").strip() == "1":
        vig._ultima_corrida = 0.0        # saltear el freno: es un pedido humano
        corrida = vig.revisar_si_toca()

    lo, hi = svc.BANDA_USD_KG
    return Response(
        json.dumps({
            "ok": True,
            "umbral_dias": dias or vig._dias_umbral(),
            "techo_dias": (vig._techo_dias() if techo is None else techo),
            "banda": [lo, hi],
            "n": len(casos),
            "nota": ("Sólo entran las recibidas hace más del umbral, con al menos "
                     "UN movimiento atribuido y dentro del techo de antigüedad. "
                     "Sin esos dos filtros la alarma prende sobre toda la "
                     "historia — pasó el 31/07: 200+ avisos."),
            "casos": casos,
            "corrida": corrida,
        }, indent=2, default=str, ensure_ascii=False),
        mimetype="application/json",
    )
