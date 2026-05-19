"""Listado y alta de gastos generales (xgast)."""
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

import db
from auth import requiere_login, requiere_permiso
from error_messages import flash_exc, humanize
from exports import csv_response
from parsers import parse_bool, parse_date, parse_monto

from . import queries

gastos_bp = Blueprint("gastos", __name__, template_folder="templates")


def _proveedores_datalist() -> list[dict]:
    try:
        from modules.autocomplete.queries import proveedores_para_datalist
        return proveedores_para_datalist()
    except Exception:
        return []


@gastos_bp.route("/gastos/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("gastos.crear")
def nuevo():
    """Alta de gasto. Pagado contado por default; postdatado opcional.

    Categorías canónicas (queries.CATEGORIAS): SER/SUE/HON/IMP/ALQ/MAN/OTR.
    """
    errores: list[str] = []
    form: dict = {}

    if request.method == "GET":
        form["fecha"] = datetime.now().date().strftime("%d/%m/%Y")
        form["doc"] = "SER"
        form["pagado"] = True
        # Restaurar campos via query string — si veníamos de crear un
        # proveedor nuevo, /proveedores/nuevo nos redirige acá con los
        # datos del form anterior. TMT 2026-05-13.
        for k in ("fecha", "fechad", "concepto", "importe", "doc", "prov"):
            if request.args.get(k):
                form[k] = request.args.get(k)
        if request.args.get("pagado"):
            form["pagado"] = request.args.get("pagado") in (
                "1", "true", "True", "on"
            )
        return render_template(
            "gastos/nuevo.html",
            form=form,
            errores=errores,
            categorias=queries.CATEGORIAS,
            proveedores_datalist=_proveedores_datalist(),
        )

    fecha = parse_date(request.form.get("fecha"))
    fechad = parse_date(request.form.get("fechad"))
    concepto = (request.form.get("concepto") or "").strip()
    importe = parse_monto(request.form.get("importe"))
    doc = (request.form.get("doc") or "").strip().upper()
    prov = (request.form.get("prov") or "").strip().upper() or None
    pagado = parse_bool(request.form.get("pagado"))

    if fecha is None:
        errores.append("Fecha inválida.")
    if not concepto:
        errores.append("Concepto requerido.")
    if importe is None or importe <= 0:
        errores.append("Importe debe ser mayor que cero.")
    if doc and doc not in queries.CATEGORIAS_SET:
        errores.append(f"Categoría inválida: {doc}.")
    if prov and not db.fetch_one(
        "SELECT 1 AS x FROM scintela.proveedor WHERE codigo_prov = %s",
        (prov,),
    ):
        # Proveedor no existe → flujo guiado a /proveedores/nuevo, mismo
        # patrón que compras.nueva. TMT 2026-05-13.
        _permisos = getattr(g, "permisos", set()) or set()
        if "proveedores.crear" in _permisos or "*" in _permisos:
            from urllib.parse import urlencode
            restore_args = {
                "fecha":    request.form.get("fecha") or "",
                "fechad":   request.form.get("fechad") or "",
                "concepto": concepto or "",
                "importe":  request.form.get("importe") or "",
                "doc":      doc or "",
                "prov":     prov or "",
                "pagado":   "1" if pagado else "",
            }
            restore_args = {k: v for k, v in restore_args.items() if v}
            next_url = url_for("gastos.nuevo") + "?" + urlencode(restore_args)
            flash(
                f"El proveedor {prov} no existe — completá los datos para "
                "crearlo y después seguís con el gasto.",
                "warning",
            )
            return redirect(
                url_for("proveedores.nuevo", codigo=prov, next=next_url)
            )
        errores.append(f"El proveedor {prov!r} no existe.")
    if not pagado and fechad is None:
        errores.append("Si el gasto NO es pagado, indicá fecha de pago.")

    form.update({
        "fecha":    request.form.get("fecha"),
        "fechad":   request.form.get("fechad"),
        "concepto": concepto,
        "importe":  request.form.get("importe"),
        "doc":      doc or "OTR",
        "prov":     prov or "",
        "pagado":   pagado,
    })

    if errores:
        return render_template(
            "gastos/nuevo.html",
            form=form,
            errores=errores,
            categorias=queries.CATEGORIAS,
            proveedores_datalist=_proveedores_datalist(),
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:3].upper()
        r = queries.crear(
            fecha=fecha, fechad=fechad, concepto=concepto,
            importe=importe, doc=doc, prov=prov, pagado=pagado,
            clave=clave, usuario=usuario,
        )
        flash(f"Gasto N° {r.get('num')} cargado.", "ok")
        return redirect(url_for("gastos.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "gastos/nuevo.html",
            form=form,
            errores=errores,
            categorias=queries.CATEGORIAS,
            proveedores_datalist=_proveedores_datalist(),
        ), 400
    except Exception as e:
        flash_exc("No pude cargar el gasto", e)
        return redirect(url_for("gastos.lista"))


@gastos_bp.route("/gastos")
@requiere_login
@requiere_permiso("gastos.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    try:
        filas = queries.buscar(q, desde, hasta)
        resumen = queries.resumen(desde, hasta)
        error = None
    except Exception as e:
        # TMT 2026-05-14 (#37): no exponer detalle crudo al template.
        import logging as _logging
        _logging.getLogger("programa_core.gastos").exception(
            "gastos.lista falló"
        )
        filas, resumen, error = [], {}, humanize(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha",     "Fecha"),
                ("doc",       "Doc"),
                ("prov",      "Cód. prov"),
                ("proveedor", "Proveedor"),
                ("num",       "Número"),
                ("concepto",  "Concepto"),
                ("importe",   "Importe"),
                ("saldo",     "Saldo"),
                ("stat",      "Estado"),
                ("fechad",    "F. dep."),
            ],
            filename="gastos.csv",
        )

    total_importe = sum(float(r["importe"] or 0) for r in filas)
    total_saldo   = sum(float(r["saldo"]   or 0) for r in filas)

    # Matriz V1..V9 (xgast.NUM) calculada desde las filas filtradas, para que
    # respete los mismos filtros desde/hasta/q que la tabla de abajo.
    # Layout 3×3:
    #   filas (1=personal, 2=servicios, 3=otros) × cols (tej, tinto, admin)
    # Mapping:
    #   NUM=1 → tej/personal     NUM=4 → tin/personal     NUM=7 → adm/personal
    #   NUM=2 → tej/servicios    NUM=5 → tin/servicios    NUM=8 → adm/servicios
    #   NUM=3 → tej/otros        NUM=6 → tin/otros        NUM=9 → adm/otros
    _NUM_TO_CELL = {
        1: ("personal",  "tej"), 4: ("personal",  "tin"), 7: ("personal",  "adm"),
        2: ("servicios", "tej"), 5: ("servicios", "tin"), 8: ("servicios", "adm"),
        3: ("otros",     "tej"), 6: ("otros",     "tin"), 9: ("otros",     "adm"),
    }
    matriz = {
        "personal":  {"tej": 0.0, "tin": 0.0, "adm": 0.0},
        "servicios": {"tej": 0.0, "tin": 0.0, "adm": 0.0},
        "otros":     {"tej": 0.0, "tin": 0.0, "adm": 0.0},
    }
    sin_categoria = 0.0
    for r in filas:
        try:
            num = int(r.get("num") or 0)
        except (TypeError, ValueError):
            num = 0
        importe = float(r.get("importe") or 0)
        cell = _NUM_TO_CELL.get(num)
        if cell:
            matriz[cell[0]][cell[1]] += importe
        else:
            sin_categoria += importe

    col_v = {
        "tej": matriz["personal"]["tej"] + matriz["servicios"]["tej"] + matriz["otros"]["tej"],
        "tin": matriz["personal"]["tin"] + matriz["servicios"]["tin"] + matriz["otros"]["tin"],
        "adm": matriz["personal"]["adm"] + matriz["servicios"]["adm"] + matriz["otros"]["adm"],
    }
    fil_total = {
        "personal":  matriz["personal"]["tej"]  + matriz["personal"]["tin"]  + matriz["personal"]["adm"],
        "servicios": matriz["servicios"]["tej"] + matriz["servicios"]["tin"] + matriz["servicios"]["adm"],
        "otros":     matriz["otros"]["tej"]     + matriz["otros"]["tin"]     + matriz["otros"]["adm"],
    }
    suma_v_total = sum(col_v.values())

    return render_template(
        "gastos/lista.html",
        filas=filas, q=q, desde=desde, hasta=hasta,
        total_importe=total_importe, total_saldo=total_saldo,
        resumen=resumen, error=error,
        matriz=matriz, col_v=col_v, fil_total=fil_total,
        suma_v_total=suma_v_total, sin_categoria=sin_categoria,
    )


@gastos_bp.route("/gastos/<int:id_xgast>/confirmar-anulacion", methods=["GET"])
@requiere_login
@requiere_permiso("gastos.anular")
def confirmar_anulacion(id_xgast: int):
    """Wizard 2 pasos para anular un gasto.

    El gasto se marca stat='Y'. Si era pagado al contado, NO se compensa
    automáticamente el movimiento de caja/banco — la dueña tiene que
    reversar ese movimiento aparte. TMT 2026-05-13.
    """
    gx = queries.por_id(id_xgast)
    if not gx:
        abort(404)
    if (gx.get("stat") or "").upper() == "Y":
        flash(f"Gasto #{gx.get('num') or id_xgast} ya está anulado.", "warn")
        return redirect(url_for("gastos.lista"))
    detalle = {
        "N° gasto": gx.get("num") or id_xgast,
        "Fecha": (gx.get("fecha").strftime("%d/%m/%Y") if gx.get("fecha") else "—"),
        "Categoría (doc)": gx.get("doc") or "—",
        "Proveedor": f"{gx.get('prov') or '—'} {gx.get('proveedor') or ''}".strip(),
        "Concepto": gx.get("concepto") or "—",
        "Importe": f"$ {gx.get('importe') or 0:,.2f}",
        "Stat actual": gx.get("stat") or "—",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Anular gasto N° {gx.get('num') or id_xgast}",
        mensaje=(
            f"Vas a anular el gasto N° {gx.get('num') or id_xgast} "
            f"por $ {gx.get('importe') or 0:,.2f}. "
            "Queda con stat='Y' y desaparece de los totales."
            + (" Si fue pagado al contado, reversá el movimiento de caja/banco aparte."
               if (gx.get("stat") or "").upper() == "A" else "")
        ),
        detalle_registro=detalle,
        accion_url=url_for("gastos.anular", id_xgast=id_xgast),
        volver_url=url_for("gastos.lista"),
        motivo_requerido=True,
        motivo_obligatorio=False,
        confirm_label="Confirmar anulación",
    )


@gastos_bp.route("/gastos/<int:id_xgast>/anular", methods=["POST"])
@requiere_login
@requiere_permiso("gastos.anular")
def anular(id_xgast: int):
    motivo = (request.form.get("motivo") or "").strip()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.anular(id_xgast, motivo=motivo, usuario=usuario)
        flash(f"Gasto id={id_xgast} anulado (stat='{r['stat_nuevo']}').", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude anular el gasto", e)
    return redirect(url_for("gastos.lista"))


# ─────────────────────────────────────────────────────────────────────────
# Desclasificar — reverso del flow caja_s_to_xgast. TMT 2026-05-16.
# Usado desde el dispatcher de /historial cuando la dueña clickea reversar
# sobre una fila de tipo='caja_s_to_xgast' (asignación errónea de V1..V9).
# El xgast se anula y la caja S vuelve a aparecer en /gastos/clasificar
# como huérfana lista para re-asignar.
# ─────────────────────────────────────────────────────────────────────────

@gastos_bp.route(
    "/gastos/<int:id_xgast>/confirmar-desclasificar", methods=["GET"]
)
@requiere_login
@requiere_permiso("gastos.anular")
def confirmar_desclasificar(id_xgast: int):
    gx = queries.por_id(id_xgast)
    if not gx:
        abort(404)
    if (gx.get("stat") or "").upper() == "Y":
        flash(
            f"Gasto #{gx.get('num') or id_xgast} ya está anulado.",
            "warn",
        )
        return redirect(url_for("historial.lista"))
    # Verificar que la xgast viene de una clasificación de caja
    md = db.fetch_one(
        """
        SELECT origen_id FROM scintela.mov_doble
         WHERE destino_table='xgast'
           AND destino_id=%s
           AND tipo='caja_s_to_xgast'
           AND estado='activo'
         LIMIT 1
        """,
        (id_xgast,),
    )
    if not md:
        flash(
            "Este gasto no viene de una clasificación de caja — "
            "usá Anular en /gastos en lugar de Desclasificar.",
            "warn",
        )
        return redirect(
            url_for("gastos.confirmar_anulacion", id_xgast=id_xgast)
        )
    detalle = {
        "Gasto #":     gx.get("num") or id_xgast,
        "Fecha":       gx.get("fecha").strftime("%d/%m/%Y") if gx.get("fecha") else "—",
        "Categoría":   f"V{gx.get('num')}" if gx.get('num') else "—",
        "Concepto":    gx.get("concepto") or "—",
        "Importe":     f"$ {gx.get('importe') or 0:,.2f}",
        "Caja origen": f"#{md['origen_id']}",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Desclasificar gasto V{gx.get('num') or '?'}",
        mensaje=(
            f"Vas a deshacer la clasificación de este gasto. "
            f"El xgast queda anulado; la caja S #{md['origen_id']} vuelve "
            f"a aparecer en /gastos/clasificar para asignarle otra V "
            f"(o dejarla sin categoría). NO se toca la fila de caja — "
            f"el egreso de plata sigue ahí."
        ),
        detalle_registro=detalle,
        accion_url=url_for("gastos.desclasificar", id_xgast=id_xgast),
        volver_url=url_for("historial.lista"),
        motivo_requerido=True,
        motivo_obligatorio=False,
        confirm_label="Confirmar desclasificación",
    )


@gastos_bp.route("/gastos/<int:id_xgast>/desclasificar", methods=["POST"])
@requiere_login
@requiere_permiso("gastos.anular")
def desclasificar(id_xgast: int):
    motivo = (request.form.get("motivo") or "").strip()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.desclasificar(id_xgast, motivo=motivo, usuario=usuario)
        flash(
            f"Gasto desclasificado. Caja #{r['id_caja']} lista para "
            f"re-asignar a otra categoría.",
            "ok",
        )
        return redirect(url_for("gastos.clasificar", id_caja=r["id_caja"]))
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude desclasificar el gasto", e)
    return redirect(url_for("historial.lista"))


# ─────────────────────────────────────────────────────────────────────────
# Clasificar egreso de caja como gasto (V1..V9) — TMT 2026-05-15.
# Opción B: el egreso ya está en scintela.caja; este endpoint le agrega
# una fila xgast linkeada vía mov_doble para que entre al balance.
# ─────────────────────────────────────────────────────────────────────────

@gastos_bp.route('/gastos/clasificar/<int:id_caja>', methods=['GET', 'POST'])
@requiere_login
@requiere_permiso('gastos.crear')
def clasificar(id_caja: int):
    caja = db.fetch_one(
        'SELECT id_caja, fecha, tipo, importe, concepto, clave '
        'FROM scintela.caja WHERE id_caja = %s',
        (id_caja,),
    )
    if not caja:
        abort(404)
    if (caja.get('tipo') or '').upper() != 'S':
        flash('Sólo los egresos de caja se clasifican como gasto.', 'warn')
        return redirect(url_for('caja.lista'))

    # Idempotencia: si ya está clasificado, ir directo al detalle.
    ya = db.fetch_one(
        """SELECT destino_id FROM scintela.mov_doble
           WHERE origen_table='caja' AND origen_id=%s
             AND destino_table='xgast' AND estado='activo' LIMIT 1""",
        (id_caja,),
    )
    if ya:
        flash(f'Este egreso ya estaba clasificado como gasto #{ya["destino_id"]}.',
              'info')
        return redirect(url_for('gastos.lista'))

    if request.method == 'GET':
        sugerido = queries.sugerir_categoria(caja.get('concepto') or '')
        return render_template(
            'gastos/clasificar.html',
            caja=caja,
            categorias=queries.CATEGORIAS_V19,
            sugerido=sugerido,
        )

    # POST
    try:
        num = int(request.form.get('num') or 0)
    except ValueError:
        flash('Categoría inválida.', 'warn')
        return redirect(url_for('gastos.clasificar', id_caja=id_caja))
    try:
        usuario = (g.user or {}).get('username', 'web')
        r = queries.clasificar_desde_caja(
            id_caja=id_caja, num=num, usuario=usuario,
        )
        msg = (f'Egreso clasificado como V{r["num"]} '
               f'(xgast #{r["id_xgast"]}). '
               'Se va a reflejar en el balance.')
        if r.get('compra_anulada'):
            msg += (f' Anulé la compra falsa #{r["compra_anulada"]} '
                    'que el parser había creado por error '
                    '(2 letras iniciales = código de proveedor existente).')
        flash(msg, 'ok')
        return redirect(url_for('gastos.lista'))
    except ValueError as e:
        flash(str(e), 'warn')
        return redirect(url_for('gastos.clasificar', id_caja=id_caja))
    except Exception as e:
        flash_exc('No pude clasificar el egreso', e)
        return redirect(url_for('caja.lista'))


# ─────────────────────────────────────────────────────────────────────────
# Wizard de reclasificación masiva — TMT 2026-05-19 v5 (pedido dueña).
# Lista todos los xgast sin num agrupados por concepto único. La dueña
# elige V para cada concepto (1 click) y se aplica masivamente a todos
# los xgast con ese concepto. Reemplaza ampliación hardcoded de keywords.
# ─────────────────────────────────────────────────────────────────────────

@gastos_bp.route("/informes/gastos/reclasificar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("gastos.crear")
def reclasificar():
    """Wizard que muestra conceptos sin num y permite asignar V masivamente.

    TMT 2026-05-19 v6 re-audit hardening:
    - El batch corre en una SOLA tx (atómico): si una fila falla, rollback total.
    - num inválido server-side → flash warn explícito (antes silent skip).
    - Conteo de inputs inválidos para reportar al usuario.
    """
    if request.method == "POST":
        usuario = (g.user or {}).get("username", "web")

        # Pre-validar TODOS los pares ANTES de tocar DB.
        # TMT 2026-05-19 v6 — concepto[] y num[] paralelos por índice
        # (antes era num_<concepto> que rompía con &/=).
        conceptos = request.form.getlist("concepto[]")
        nums_raw = request.form.getlist("num[]")
        pares: list[tuple[str, int]] = []
        invalidos = 0
        for concepto, v in zip(conceptos, nums_raw, strict=False):
            v_str = (v or "").strip()
            concepto_clean = (concepto or "").strip()
            if not v_str:
                # "— Saltar —" → ignorar.
                continue
            if not concepto_clean:
                invalidos += 1
                continue
            try:
                num = int(v_str)
            except (TypeError, ValueError):
                invalidos += 1
                continue
            if num < 1 or num > 9:
                invalidos += 1
                continue
            pares.append((concepto_clean, num))

        if invalidos:
            flash(
                f"{invalidos} selección(es) tenía(n) un valor de V fuera de 1..9 "
                "y fueron ignoradas. Si pasó por DevTools, no juegues con eso.",
                "warn",
            )

        # Aplicar en una sola transacción atómica. Si una fila falla,
        # rollback total y la dueña ve el error sin estado intermedio.
        aplicados = 0
        filas_afectadas_total = 0
        importe_total = 0.0
        try:
            with db.tx() as conn:
                for concepto, num in pares:
                    r = queries.reclasificar_concepto_bulk(
                        concepto=concepto, num=num, usuario=usuario,
                        conn=conn,
                    )
                    if r["filas_afectadas"] > 0:
                        aplicados += 1
                        filas_afectadas_total += r["filas_afectadas"]
                        importe_total += r["importe_total"]
        except Exception as e:
            flash_exc("Reclasificación falló — rollback completo aplicado", e)
            return redirect(url_for("gastos.reclasificar"))

        if aplicados:
            flash(
                f"Reclasificación OK: {aplicados} conceptos · "
                f"{filas_afectadas_total} filas · $ {importe_total:,.2f}.",
                "ok",
            )
        else:
            flash("Ningún concepto fue reclasificado (revisá las selecciones).",
                  "warn")
        return redirect(url_for("gastos.reclasificar"))

    # GET: mostrar la tabla.
    resumen = queries.xgast_sin_num_resumen()
    # TMT 2026-05-19 v6 — limite + n_total para warn si truncamos.
    LIMITE_VISIBLE = 500
    filas = queries.xgast_sin_num_por_concepto(limite=LIMITE_VISIBLE)
    return render_template(
        "gastos/reclasificar.html",
        resumen=resumen,
        filas=filas,
        limite_visible=LIMITE_VISIBLE,
    )
