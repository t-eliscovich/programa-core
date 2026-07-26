"""Tab 'Producción Tejeduría Asinfo' — resumen diario/mensual de kg producidos
por tejedor (Asinfo) y match contra las compras tipo K de Programa Core.

Reemplaza la carga manual del dBase: muestra lo que Asinfo dice que se produjo
(INTELA + tercerizados), lo cruza contra lo cargado en compras, y carga las OFs
tercerizadas que faltan usando las TARIFAS $/kg del panel de arriba (mig 0133).

Rutas:
  GET  /produccion-tejeduria-asinfo            (compras.ver)
  POST /produccion-tejeduria-asinfo/tarifas    (tarifas.editar → sólo wildcard '*'
                                                = Accionistas y Andrés)
  POST /produccion-tejeduria-asinfo/tarifas/<id>/borrar   (tarifas.editar)
  POST /produccion-tejeduria-asinfo/cargar-pendientes     (compras.crear)
"""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso
from error_messages import humanize
from filters import today_ec
from parsers import parse_monto

from . import queries, service

tejeduria_asinfo_bp = Blueprint(
    "tejeduria_asinfo", __name__, template_folder="templates",
)


def _anio_mes():
    hoy = today_ec()
    try:
        anio = int(request.values.get("anio") or hoy.year)
    except (TypeError, ValueError):
        anio = hoy.year
    try:
        mes = int(request.values.get("mes") or hoy.month)
    except (TypeError, ValueError):
        mes = hoy.month
    return anio, max(1, min(mes, 12))


@tejeduria_asinfo_bp.route("/produccion-tejeduria-asinfo")
@requiere_login
@requiere_permiso("compras.ver")
def tab():
    anio, mes = _anio_mes()
    data = service.resumen_mes(anio, mes)
    return render_template(
        "tejeduria_asinfo/tab.html", data=data, anio=anio, mes=mes,
    )


@tejeduria_asinfo_bp.route("/produccion-tejeduria-asinfo/tarifas", methods=["POST"])
@requiere_login
@requiere_permiso("tarifas.editar")
def guardar_tarifas():
    """Guarda la matriz de tarifas. Mismo patrón que /precios/guardar: los
    inputs vienen como t_<i>_<campo> y sólo se escriben las filas con proveedor
    y tarifa. La fila en blanco del final sirve para agregar un patrón nuevo."""
    anio, mes = _anio_mes()
    usuario = (g.user or {}).get("username", "web")
    filas: list[dict] = []
    idxs = sorted({
        k.split("_")[1] for k in request.form
        if k.startswith("t_") and len(k.split("_")) >= 3
    })
    for i in idxs:
        raw = request.form.get(f"t_{i}_tarifa")
        cod = (request.form.get(f"t_{i}_cod_prov") or "").strip()
        if not cod:
            continue
        tarifa = parse_monto(raw)
        if (raw or "").strip() and tarifa is None:
            flash(f"No entendí la tarifa «{raw}» de {cod.upper()}. Usá formato 2,0125.",
                  "error")
            return redirect(url_for("tejeduria_asinfo.tab", anio=anio, mes=mes))
        if tarifa is None:
            continue
        filas.append({
            "cod_prov": cod,
            "patron": request.form.get(f"t_{i}_patron"),
            "tarifa": tarifa,
            "nota": request.form.get(f"t_{i}_nota"),
        })
    try:
        res = queries.guardar_tarifas(filas, usuario=usuario)
        flash(f"Tarifas guardadas ({res['guardadas']}).", "ok")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:  # noqa: BLE001
        flash(humanize(e), "error")
    return redirect(url_for("tejeduria_asinfo.tab", anio=anio, mes=mes))


@tejeduria_asinfo_bp.route(
    "/produccion-tejeduria-asinfo/tarifas/<int:id_tarifa>/borrar", methods=["POST"])
@requiere_login
@requiere_permiso("tarifas.editar")
def borrar_tarifa(id_tarifa: int):
    anio, mes = _anio_mes()
    try:
        queries.borrar_tarifa(id_tarifa)
        flash("Tarifa borrada.", "ok")
    except Exception as e:  # noqa: BLE001
        flash(humanize(e), "error")
    return redirect(url_for("tejeduria_asinfo.tab", anio=anio, mes=mes))


@tejeduria_asinfo_bp.route(
    "/produccion-tejeduria-asinfo/cargar-pendientes", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("compras.crear")
def cargar_pendientes():
    """GET  = PREVISUALIZACIÓN (dry-run, no escribe nada).
    POST = crea las compras.

    TMT 2026-07-26 (dueña: "hacelo vos y fijate de no duplicar"): antes el botón
    tenía un `confirm()` nativo. Ahora es una pantalla con el detalle de qué se
    va a crear y qué se saltea (con el motivo), armada con la MISMA función que
    ejecuta (`dry_run=True`) — el preview no puede mentir. Además evita el
    diálogo del navegador, que congela la automatización.
    """
    anio, mes = _anio_mes()
    usuario = (g.user or {}).get("username", "web")
    if request.method == "GET":
        plan = service.cargar_pendientes(anio, mes, usuario=usuario, dry_run=True)
        return render_template(
            "tejeduria_asinfo/cargar_preview.html", plan=plan, anio=anio, mes=mes,
        )
    clave = (g.user or {}).get("clave") or usuario[:3].upper()
    try:
        res = service.cargar_pendientes(anio, mes, usuario=usuario, clave=clave)
    except Exception as e:  # noqa: BLE001
        flash(humanize(e), "error")
        return redirect(url_for("tejeduria_asinfo.tab", anio=anio, mes=mes))

    if res["creadas"]:
        flash(
            f"Cargué {res['creadas']} compra(s) de tejeduría por "
            f"$ {res['importe']:,.2f}.", "ok",
        )
    if res.get("avisos_dup"):
        flash(
            f"Ojo: {res['avisos_dup']} de esas ya tenían una compra del mismo "
            f"proveedor por el mismo importe en la ventana — revisalas en /compras.",
            "warn",
        )
    if res["salteadas"]:
        motivos = "; ".join(
            f"{d['oft']}: {d['motivo']}" for d in res["detalle"] if not d["ok"]
        )[:400]
        flash(f"Salteé {res['salteadas']} OF — {motivos}", "warn")
    if not res["creadas"] and not res["salteadas"]:
        flash("No hay OFs pendientes de cargar en este mes.", "ok")
    return redirect(url_for("tejeduria_asinfo.tab", anio=anio, mes=mes))
