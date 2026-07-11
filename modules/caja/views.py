"""Caja — libro de caja en efectivo."""

from flask import (
    Blueprint,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from exports import csv_response
from filters import today_ec
from parsers import parse_date, parse_monto

from . import queries

caja_bp = Blueprint("caja", __name__, template_folder="templates")


@caja_bp.route("/caja/_api/preview-concepto")
@requiere_login
@requiere_permiso("caja.ver")
def preview_concepto():
    """JSON: previsualiza el side effect que dispararía un concepto.

    Usado por el form de nuevo movimiento de caja para mostrar al usuario
    qué movimiento doble se va a crear (ej. "+ entrada en Pichincha").
    """
    import concepto_parser
    import db as _db

    concepto = (request.args.get("concepto") or "").strip()
    if not concepto:
        return {"descripcion": ""}
    provs_validos = {
        (r.get("codigo_prov") or "").strip().upper()
        for r in (_db.fetch_all("SELECT codigo_prov FROM scintela.proveedor") or [])
    }
    bancos_map: dict = {}
    for b in _db.fetch_all("SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco") or []:
        n = (b.get("nombre") or "").upper().strip()
        if "PICHINC" in n:
            bancos_map.setdefault("PICHINCHA", int(b["no_banco"]))
        if "INTER" in n:
            bancos_map.setdefault("INTERNACIONAL", int(b["no_banco"]))
    parsed = concepto_parser.parse_concepto(
        concepto,
        {"provs_validos": provs_validos, "bancos": bancos_map},
    )
    return {
        "tipo": parsed.get("tipo"),
        "descripcion": concepto_parser.descripcion_humana(parsed),
    }


@caja_bp.route("/caja/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("caja.crear")
def nuevo():
    errores: list[str] = []
    form: dict = {}

    # Conceptos más usados en el histórico — datalist para autocomplete.
    try:
        conceptos = queries.conceptos_frecuentes(limite=50)
    except Exception:
        conceptos = []

    if request.method == "GET":
        # TMT 2026-06-04: today_ec() (fecha Ecuador), no datetime.now() del
        # server UTC, que de noche fecha la caja con el día de mañana.
        form["fecha"] = today_ec().isoformat()
        # Prefijar el tipo según el botón clickeado: ?tipo=E (entrada) o S (salida).
        # Convención legacy dBase: E=entrada, S=salida (importe siempre positivo).
        # Default 'E' para que el form arranque en ingreso.
        tipo_inicial = (request.args.get("tipo") or "E").upper()
        if tipo_inicial not in ("E", "S"):
            tipo_inicial = "E"
        form["tipo"] = tipo_inicial
        form["saldo_actual"] = queries.saldo_actual()
        return render_template("caja/nuevo.html", form=form, errores=errores, conceptos=conceptos)

    fecha = parse_date(request.form.get("fecha"))
    tipo = (request.form.get("tipo") or "").strip().upper()
    importe = parse_monto(request.form.get("importe"))
    concepto = (request.form.get("concepto") or "").strip()
    # TMT 2026-05-19 v3 — pedido dueña: el form de salida muestra 9 chips
    # V1..V9 que pre-cargan concepto + setean `xgast_num`. Si viene, después
    # de crear la caja S clasificamos automáticamente como xgast con ese num.
    xgast_num_raw = (request.form.get("xgast_num") or "").strip()
    xgast_num: int | None = None
    if xgast_num_raw:
        try:
            v = int(xgast_num_raw)
            if 1 <= v <= 9:
                xgast_num = v
        except (TypeError, ValueError):
            xgast_num = None

    if fecha is None:
        errores.append("Fecha inválida.")
    if tipo not in ("E", "S"):
        errores.append("Tipo debe ser E (entrada/ingreso) o S (salida/egreso).")
    if importe is None or importe <= 0:
        errores.append("Importe debe ser mayor que cero.")
    if not concepto:
        errores.append("Concepto requerido.")
    # xgast_num sólo tiene sentido en salidas (tipo S).
    if xgast_num and tipo != "S":
        xgast_num = None

    form.update(
        {
            "fecha": request.form.get("fecha"),
            "tipo": tipo,
            "importe": request.form.get("importe"),
            "concepto": concepto,
            "xgast_num": xgast_num,
            "saldo_actual": queries.saldo_actual(),
        }
    )

    if errores:
        return render_template("caja/nuevo.html", form=form, errores=errores, conceptos=conceptos), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:3].upper()
        # TMT 2026-05-19 v4 audit: pasamos xgast_num a crear(), que lo
        # clasifica DENTRO de la misma tx. Si falla, rollback total
        # (no quedan cajas huérfanas).
        r = queries.crear(
            fecha=fecha,
            tipo=tipo,
            importe=importe,
            concepto=concepto,
            clave=clave,
            usuario=usuario,
            xgast_num=xgast_num,
        )
        msg = f"Movimiento de caja registrado (id {r.get('id_caja')})."
        if r.get("clasif_gasto"):
            msg += f" Clasificado como gasto V{xgast_num} (atómico)."
        flash(msg, "ok")
        return redirect(url_for("caja.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template("caja/nuevo.html", form=form, errores=errores), 400
    except Exception as e:
        errores.append(f"No pude registrar: {e}")
        return render_template("caja/nuevo.html", form=form, errores=errores), 500


def _reverso_preview_caja(id_caja: int) -> dict | None:
    """Preview del reverso de un movimiento de caja (compartido por la página
    HTML y el modal in-page). None si no existe. TMT 2026-07-11 (dueña)."""
    m = queries.por_id(id_caja)
    if not m:
        return None
    tipo_legible = {"E": "Entrada", "S": "Salida"}.get((m.get("tipo") or "").upper(), m.get("tipo") or "—")
    detalle = {
        "ID caja": m.get("id_caja"),
        "Fecha": (m.get("fecha").strftime("%d/%m/%Y") if m.get("fecha") else "—"),
        "Tipo": tipo_legible,
        "Importe": f"$ {m.get('importe') or 0:,.2f}",
        "Concepto": m.get("concepto") or "—",
    }
    return {
        "titulo": f"Reversar movimiento de caja id={id_caja}",
        "mensaje": (
            f"Vas a reversar el movimiento de caja id={id_caja} "
            f"({tipo_legible} $ {m.get('importe') or 0:,.2f}). "
            "Se crea un movimiento opuesto que compensa este — el original "
            "queda intacto en la historia."
        ),
        "detalle": detalle,
        "confirm_label": "Confirmar reverso",
    }


@caja_bp.route("/caja/<int:id_caja>/reverso-preview", methods=["GET"])
@requiere_login
@requiere_permiso("caja.crear")
def reverso_preview(id_caja: int):
    """JSON para el modal in-page de reverso (mismo shape que historial)."""
    data = _reverso_preview_caja(id_caja)
    if not data:
        return jsonify({"ok": False, "error": "El movimiento de caja no existe."})
    data["ok"] = True
    data["accion_url"] = url_for("caja.reversar", id_caja=id_caja)
    return jsonify(data)


@caja_bp.route("/caja/<int:id_caja>/confirmar-reverso", methods=["GET"])
@requiere_login
@requiere_permiso("caja.crear")
def confirmar_reverso(id_caja: int):
    """Fallback HTML del reverso (links directos / no-JS). El flujo normal
    desde la lista usa el modal in-page (ver reverso_preview). TMT 2026-05-13.
    """
    from flask import abort

    data = _reverso_preview_caja(id_caja)
    if not data:
        abort(404)
    return render_template(
        "_confirmar_accion.html",
        titulo=data["titulo"],
        mensaje=data["mensaje"],
        detalle_registro=data["detalle"],
        accion_url=url_for("caja.reversar", id_caja=id_caja),
        volver_url=url_for("caja.lista"),
        motivo_requerido=True,
        # TMT 2026-07-08 dueña: motivo de reverso NO obligatorio ("se hace largo").
        motivo_obligatorio=False,
        confirm_label=data["confirm_label"],
    )


@caja_bp.route("/caja/<int:id_caja>/reversar", methods=["POST"])
@requiere_login
@requiere_permiso("caja.crear")
def reversar(id_caja: int):
    """Crea un movimiento opuesto que compensa al id_caja indicado.

    No borra la fila original — conserva audit trail. Inserta una nueva
    con tipo invertido (E↔S), mismo importe, concepto "REVERSO id N — <motivo>".
    """
    motivo = (request.form.get("motivo") or "").strip()
    # TMT 2026-05-21 dueña: motivo opcional sin requerir.
    if not motivo:
        motivo = "sin motivo"
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.reversar(id_caja, motivo=motivo, usuario=usuario)
        msg = (
            f"Movimiento id={r['id_caja_original']} reversado. "
            f"Nuevo id={r['id_caja_nuevo']} (tipo {r['tipo_nuevo']}, $ {r['importe']:.2f})."
        )
        se = r.get("side_effect_reversado")
        if se:
            tipo_legible = {
                "transfer_banco": "banco",
                "retiro_socio": "retiro de socio",
                "dolares": "USD",
                "compra_proveedor": "compra a proveedor",
            }.get(se.get("tipo"), se.get("tipo"))
            msg += f" También se reversó el side effect de {tipo_legible}."
        flash(msg, "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash(f"No pude reversar: {e}", "warn")
    # TMT 2026-07-11 (dueña): volver a la pantalla anterior con filtros (`next`).
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("caja.lista"))


@caja_bp.route("/caja")
@requiere_login
@requiere_permiso("caja.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    # TMT 2026-07-11 (dueña): flechita para avanzar. 500/página; pedimos
    # limite+1 para saber si hay página siguiente sin un COUNT extra.
    POR_PAGINA = 500
    try:
        pagina = max(1, int(request.args.get("pagina") or "1"))
    except (TypeError, ValueError):
        pagina = 1
    es_export = request.args.get("export") == "csv"
    limite = 100000 if es_export else POR_PAGINA
    offset = 0 if es_export else (pagina - 1) * POR_PAGINA
    try:
        filas = queries.movimientos(desde, hasta, q, limite=limite + 1, offset=offset)
        hay_mas = (not es_export) and len(filas) > limite
        if hay_mas:
            filas = filas[:limite]
        resumen = queries.resumen()
        # Egresos del mes sin clasificar como gasto V1..V9 — pasamos sólo
        # el SET de ids; el botón "Clasificar" aparece inline en la fila
        # de cada egreso que está en este set (TMT 2026-05-15, opción B).
        from modules.gastos import queries as _gq

        try:
            egresos_sin_clasif = _gq.caja_egresos_sin_clasificar(limite=2000)
        except Exception:
            egresos_sin_clasif = []
        ids_sin_clasif = {int(e["id_caja"]) for e in egresos_sin_clasif}
        error = None
    except Exception as e:
        filas, resumen, error = [], {}, str(e)
        egresos_sin_clasif, ids_sin_clasif = [], set()
        hay_mas = False

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"),
                ("tipo", "Tipo"),
                ("concepto", "Concepto"),
                ("importe", "Importe"),
                ("saldo", "Saldo"),
                ("clave", "Clave"),
                ("no_cheque", "N° cheque"),
                ("cliente", "Cliente"),
            ],
            filename="caja.csv",
        )

    return render_template(
        "caja/lista.html",
        filas=filas,
        resumen=resumen,
        q=q,
        desde=desde,
        hasta=hasta,
        error=error,
        ids_sin_clasif=ids_sin_clasif,
        n_sin_clasif=len(ids_sin_clasif),
        pagina=pagina,
        hay_mas=hay_mas,
    )


# ---------------------------------------------------------------------------
# CARGA MULTI-LÍNEA — alta masiva de movimientos de caja (réplica dBase ADD)
# ---------------------------------------------------------------------------
# TMT 2026-06-13 dueña: "dejame cargar varios movimientos de caja de una".
# Mismo patrón que /informes/tinto-carga: N líneas + un solo botón Cargar,
# all-or-nothing en el backend (si alguna línea con datos falla, no se
# carga NADA). A diferencia de /caja/nuevo, esta pantalla es el ADD SIMPLE
# del dBase: sólo fecha/tipo/importe/concepto, SIN side-effects de concepto
# (PICH/INTER/PR/RR), SIN clasificación V1..V9. Cada línea se inserta con
# caja_helpers.insert_movimiento_caja (saldo running correcto, E=+, S=−) y
# al final se recalculan los saldos desde la fecha más antigua cargada para
# que el running balance quede consistente aunque se carguen fechas mezcladas.


@caja_bp.route("/caja/cargar", methods=["GET"])
@requiere_login
@requiere_permiso("caja.crear")
def cargar():
    """Planilla de alta masiva — N líneas fecha/tipo/importe/concepto."""
    try:
        conceptos = queries.conceptos_frecuentes(limite=50)
    except Exception:
        conceptos = []
    try:
        saldo_actual = queries.saldo_actual()
    except Exception:
        saldo_actual = 0.0
    return render_template(
        "caja/cargar.html",
        hoy=today_ec().isoformat(),
        saldo_actual=saldo_actual,
        conceptos=conceptos,
    )


@caja_bp.route("/caja/cargar/agregar", methods=["POST"])
@requiere_login
@requiere_permiso("caja.crear")
def cargar_agregar():
    """Alta de N movimientos de caja en un solo POST (all-or-nothing).

    Acepta inputs repetidos fecha/tipo/importe/concepto. Las líneas
    totalmente vacías se ignoran. Si CUALQUIER línea con datos tiene un
    error, no se carga NADA (así no quedan cargas a medias).

    Es el ADD simple del dBase: fecha/tipo/importe/concepto, sin
    side-effects de concepto ni clasificación de gasto. El signo lo
    lleva el `tipo` (E=entrada/+, S=salida/−); el importe siempre va
    positivo (lo normaliza caja_helpers).
    """
    import caja_helpers
    import db

    fechas = request.form.getlist("fecha")
    tipos = request.form.getlist("tipo")
    importes = request.form.getlist("importe")
    conceptos = request.form.getlist("concepto")
    n = max(len(fechas), len(tipos), len(importes), len(conceptos))

    def _at(lst, i):
        return lst[i] if i < len(lst) else ""

    filas: list[dict] = []
    errores: list[str] = []
    for i in range(n):
        f_raw = str(_at(fechas, i) or "").strip()
        tipo = (_at(tipos, i) or "").strip().upper()
        imp_raw = str(_at(importes, i) or "").strip()
        concepto = (_at(conceptos, i) or "").strip()
        # Línea vacía (sin importe ni concepto) → ignorar.
        if not imp_raw and not concepto:
            continue
        rotulo = f"línea {len(filas) + len(errores) + 1}"
        fecha = parse_date(f_raw)
        if fecha is None:
            errores.append(f"{rotulo}: fecha inválida")
            continue
        if tipo not in ("E", "S"):
            errores.append(f"{rotulo}: tipo debe ser E (entrada) o S (salida)")
            continue
        importe = parse_monto(imp_raw)
        if importe is None or float(importe) <= 0:
            errores.append(f"{rotulo}: importe debe ser mayor que cero")
            continue
        if not concepto:
            errores.append(f"{rotulo}: concepto requerido")
            continue
        filas.append({
            "fecha": fecha,
            "tipo": tipo,
            "importe": float(importe),
            "concepto": concepto,
        })

    if not filas and not errores:
        flash("No hay líneas para cargar.", "warn")
        return redirect(url_for("caja.cargar"))

    if errores:
        flash("No se cargó nada. " + " | ".join(errores), "warn")
        return redirect(url_for("caja.cargar"))

    usuario = (g.user or {}).get("username", "web")
    clave = (g.user or {}).get("clave") or usuario[:3].upper()

    # Insertar en orden cronológico para que el saldo running encadene
    # bien línea a línea dentro de la misma tx. Al final recomputamos
    # los saldos desde la fecha más antigua cargada por si quedó alguna
    # fila histórica intercalada (fecha < última fila previa).
    filas_ordenadas = sorted(filas, key=lambda f: f["fecha"])
    fecha_min = filas_ordenadas[0]["fecha"]
    tot_e = tot_s = 0.0
    import mov_doble as _md
    try:
        with db.tx() as conn:
            for f in filas_ordenadas:
                r = caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=f["fecha"],
                    tipo=f["tipo"],
                    importe=f["importe"],
                    concepto=f["concepto"],
                    clave=clave,
                    usuario=usuario,
                )
                # TMT 2026-07-07 (dueña): registrar mov_doble por CADA fila de
                # la carga masiva, igual que el alta simple (queries.crear).
                # Sin esto, los movimientos batch salían en /mi-historial con
                # id sintético NEGATIVO y SIN botón de reverso (el template
                # solo ofrece reversar si id_mov_doble > 0) → Alex no podía
                # reversar sus cargas de caja (404). Con el mov_doble self-ref
                # sale con id positivo y despacha a caja.confirmar_reverso
                # (gate caja.crear, que Alex tiene). Sin side-effects.
                id_caja_new = r.get("id_caja") if r else None
                if id_caja_new:
                    _md.registrar(
                        conn=conn,
                        tipo=f"caja_{f['tipo'].lower()}_simple",
                        origen_table="caja",
                        origen_id=id_caja_new,
                        destino_table="caja",
                        destino_id=id_caja_new,
                        importe=f["importe"],
                        fecha=f["fecha"],
                        concepto=f["concepto"],
                        usuario=usuario,
                        metadata={"tipo_caja": f["tipo"],
                                  "tiene_side_effect": False,
                                  "origen": "caja/cargar"},
                    )
                if f["tipo"] == "E":
                    tot_e += f["importe"]
                else:
                    tot_s += f["importe"]
            # Reencadenar saldos desde la fecha más antigua cargada — así el
            # running balance queda consistente aunque se hayan cargado
            # fechas mezcladas o backdated.
            caja_helpers.recompute_saldos_desde(conn, ancla_fecha=fecha_min)
    except ValueError as e:
        flash(f"No se cargó nada: {e}", "warn")
        return redirect(url_for("caja.cargar"))
    except Exception as e:
        flash(f"No pude cargar: {e}", "warn")
        return redirect(url_for("caja.cargar"))

    n_filas = len(filas_ordenadas)
    flash(
        f"Cargada{'s' if n_filas != 1 else ''} {n_filas} línea"
        f"{'s' if n_filas != 1 else ''}: "
        f"entradas $ {tot_e:,.2f} · salidas $ {tot_s:,.2f}.",
        "ok",
    )
    return redirect(url_for("caja.cargar"))
