"""Pantalla /clientes/sync-asinfo — el sync del maestro de clientes.

Muestra las últimas corridas del sync (las 2 diarias del cron + las manuales),
los CONFLICTOS que quedaron sin importar (RUC ya en PC bajo otro código,
códigos duplicados dentro de Asinfo) y un botón "Sincronizar ahora".

La lógica vive en `modules.clientes.sync_asinfo` — esta vista sólo la llama
por el mismo camino que el cron, para que lo que hace una persona y lo que
hace la máquina sea EXACTAMENTE lo mismo (regla operar-por-la-ui).
"""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, url_for

from auth import requiere_login, requiere_permiso
from modules.clientes import sync_asinfo

bp = Blueprint("clientes_sync_asinfo", __name__,
               url_prefix="/clientes/sync-asinfo", template_folder="templates")


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("clientes.editar")
def pantalla():
    corridas = sync_asinfo.ultimas_corridas(limite=10)
    ultima = corridas[0] if corridas else None
    return render_template(
        "clientes/sync_asinfo.html",
        corridas=corridas,
        ultima=ultima,
        conflictos=(ultima or {}).get("reporte", {}).get("conflictos", []),
        dup_asinfo=(ultima or {}).get("reporte", {}).get("dup_asinfo", []),
    )


@bp.route("/correr", methods=["POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def correr():
    usuario = ((g.user or {}).get("username") or "web")[:50]
    r = sync_asinfo.sincronizar(usuario=f"sync-asinfo:{usuario}"[:60])
    if not r.get("ok"):
        flash(f"El sync no corrió: {r.get('error', 'error desconocido')}", "warn")
    else:
        altas = ", ".join(r.get("altas") or []) or "ninguna"
        flash(
            f"Sync OK — {r.get('actualizados', 0)} fichas actualizadas, "
            f"altas: {altas}, {len(r.get('conflictos') or [])} conflictos.",
            "ok",
        )
    return redirect(url_for("clientes_sync_asinfo.pantalla"))
