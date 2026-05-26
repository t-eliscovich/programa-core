"""Bitácora — visor de auditoría global."""
from flask import Blueprint, g, render_template, request

from auth import requiere_login, requiere_permiso
from exports import csv_response

from . import queries

bitacora_bp = Blueprint("bitacora", __name__, template_folder="templates")


@bitacora_bp.route("/mi-historial")
@requiere_login
def mi_historial():
    """Historial de movimientos visible para el USER LOGUEADO.

    TMT 2026-05-26 dueña update: 'que muestren solo los movimientos de
    las secciones que el puede ver'. O sea: NO filtrar por quién lo
    hizo, sino por las secciones (módulos) donde el user tiene .ver.

    Ej. Alex ve cheques+facturas+caja+bancos (tiene esos *.ver) pero
    NO ve retiros (no tiene retiros.ver).

    Es un redirect a /historial?mis_origenes=1 para que /historial
    resuelva los origen_tables según permisos del user logueado.
    """
    from flask import redirect, url_for
    username = (g.user or {}).get("username") if g.user else None
    if not username:
        return redirect(url_for("auth.login"))
    return redirect(url_for("historial.lista", mis_origenes="1"))


@bitacora_bp.route("/bitacora")
@requiere_login
@requiere_permiso("bitacora.ver")
def lista():
    q = request.args.get("q", "").strip()
    usuario = (request.args.get("usuario") or "").strip() or None
    modulo = (request.args.get("modulo") or "").strip() or None
    entidad = (request.args.get("entidad") or "").strip() or None
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    # request_id acepta un UUID completo o un prefijo (por ej. los 8 chars
    # que logueamos en la consola). La query usa LIKE con el prefijo.
    request_id = (request.args.get("request_id") or "").strip() or None

    try:
        filas = queries.listar(
            q=q, usuario=usuario, modulo=modulo, entidad=entidad,
            desde=desde, hasta=hasta, request_id=request_id,
        )
        modulos = queries.modulos_distintos()
        error = None
    except Exception as e:
        filas, modulos, error = [], [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("ts", "Timestamp"),
                ("usuario", "Usuario"),
                ("rol", "Rol"),
                ("metodo", "Método"),
                ("ruta", "Ruta"),
                ("modulo", "Módulo"),
                ("accion", "Acción"),
                ("entidad", "Entidad"),
                ("id_entidad", "Id"),
                ("status_http", "Status"),
                ("request_id", "Request-Id"),
                ("resumen", "Resumen"),
            ],
            filename="bitacora.csv",
        )

    return render_template(
        "bitacora/lista.html",
        filas=filas, modulos=modulos,
        q=q, usuario=usuario, modulo=modulo, entidad=entidad,
        desde=desde, hasta=hasta, request_id=request_id, error=error,
    )
