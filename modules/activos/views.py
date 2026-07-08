"""Vistas de activos fijos — listado + acción de amortización mensual.

TMT 2026-05-17: agregado endpoint /activos/nuevo para dar de alta activos
fijos desde la UI (antes sólo se podían cargar via DBF dump). Cuota mensual
se autocalcula como inicial/vida_util si la dueña no la especifica.
"""
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
from error_messages import flash_exc
from exports import csv_response
from filters import today_ec
from parsers import parse_date, parse_int, parse_monto

from . import queries

activos_bp = Blueprint("activos", __name__, template_folder="templates")


@activos_bp.route("/activos")
@requiere_login
@requiere_permiso("activos.ver")
def lista():
    q = request.args.get("q", "").strip()
    tipo = (request.args.get("tipo") or "").strip().upper() or None
    solo_activos = request.args.get("solo_activos") == "1"

    try:
        filas = queries.buscar(q=q, tipo=tipo, solo_activos=solo_activos)
        resumen = queries.resumen()
        tipos = queries.tipos_disponibles()
        error = None
    except Exception as e:
        filas, resumen, tipos, error = [], {}, [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha",            "Fecha"),
                ("concepto",         "Concepto"),
                ("tipo",             "Tipo"),
                ("proveedor",        "Proveedor"),
                ("inicial",          "Valor inicial"),
                ("amortizac",        "Amort. acum."),
                ("amortimes",        "Cuota mensual"),
                ("valor_libros",     "Valor en libros"),
                ("pct_depreciado",   "% depreciado"),
                ("vida_util",        "Vida útil (m)"),
                ("ult_mes_amortizado", "Últ. mes amort."),
            ],
            filename="activos.csv",
        )

    # TMT 2026-05-20 v4 — subtotales por subcategoría re-agregados
    # (pedido dueña: "ahora si agreguemos subtotales"). Iteración:
    # v2 los puso, v3 los sacó, v4 vuelven con las rows más compactas.
    subtotales: dict[int, dict] = {}
    for f in filas:
        cat = int(f.get("categoria_orden") or 99)
        s = subtotales.setdefault(cat, {
            "n": 0, "inicial": 0.0, "amortizac": 0.0,
            "valor_libros": 0.0, "amortimes": 0.0,
            "deprec_dia": 0.0, "deprec_mes": 0.0,
        })
        s["n"]            += 1
        s["inicial"]      += float(f.get("inicial")      or 0)
        s["amortizac"]    += float(f.get("amortizac")    or 0)
        s["valor_libros"] += float(f.get("valor_libros") or 0)
        s["amortimes"]    += float(f.get("amortimes")    or 0)
        # TMT 2026-05-27 dueña: total diario (1/30 cuota) + total mensual (cuota completa).
        s["deprec_dia"]   += float(f.get("deprec_dia")   or 0)
        s["deprec_mes"]   += float(f.get("cuota")        or 0)

    return render_template(
        "activos/lista.html",
        filas=filas, q=q, tipo=tipo, solo_activos=solo_activos,
        resumen=resumen, tipos=tipos,
        error=error,
        # Códigos canónicos para el dropdown de tipo inline.
        tipos_canonicos=queries.TIPOS_CANONICOS,
        subtotales=subtotales,
    )


@activos_bp.route("/activos/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("activos.crear")
def nuevo():
    """Alta de un activo fijo con cuota de depreciación.

    GET: muestra el form (con autocomplete de proveedores).
    POST: crea el activo en scintela.activos. La cuota se autocalcula como
    inicial/vida_util si no se especifica. La proc actualizar_amortizacion()
    aplica la cuota mes a mes a partir del próximo cierre.
    """
    form = {
        "fecha":           today_ec().isoformat(),
        "concepto":        "",
        "tipo":            "MAQ",
        "inicial":         "",
        "vida_util_meses": "60",  # 5 años default razonable
        "cuota":           "",
        "id_proveedor":    "",
    }
    if request.method == "POST":
        fecha           = parse_date(request.form.get("fecha")) or today_ec()
        concepto        = (request.form.get("concepto") or "").strip()
        tipo            = (request.form.get("tipo") or "").strip().upper()
        inicial         = parse_monto(request.form.get("inicial"))
        vida_util_meses = parse_int(request.form.get("vida_util_meses")) or 0
        cuota           = parse_monto(request.form.get("cuota"))
        id_proveedor    = parse_int(request.form.get("id_proveedor"))

        form.update({
            "fecha":           fecha.isoformat(),
            "concepto":        concepto,
            "tipo":            tipo,
            "inicial":         request.form.get("inicial") or "",
            "vida_util_meses": str(vida_util_meses or 60),
            "cuota":           request.form.get("cuota") or "",
            "id_proveedor":    str(id_proveedor or ""),
        })

        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.crear(
                fecha=fecha, concepto=concepto, tipo=tipo,
                inicial=inicial, vida_util_meses=vida_util_meses,
                cuota=cuota, id_proveedor=id_proveedor,
                usuario=usuario,
            )
            flash(
                f"Activo \"{r['concepto']}\" creado · valor ${r['inicial']:.2f} · "
                f"cuota mensual ${r['cuota']:.2f} ({r['vida_util']} meses).",
                "ok",
            )
            return redirect(url_for("activos.lista"))
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude crear el activo", e)

    # Lista de proveedores para el autocomplete (defensivo si la tabla no existe).
    proveedores = []
    try:
        import db as _db
        proveedores = _db.fetch_all(
            "SELECT id_proveedor, codigo_prov, nombre "
            "FROM scintela.proveedor "
            "WHERE COALESCE(activo, '1') NOT IN ('0', 'N') "
            "ORDER BY nombre LIMIT 500"
        ) or []
    except Exception:
        proveedores = []

    return render_template(
        "activos/nuevo.html",
        form=form,
        proveedores=proveedores,
        hoy=today_ec().isoformat(),
    )


@activos_bp.route("/activos/_api/<int:id_activos>/editar-tipo", methods=["POST"])
@requiere_login
@requiere_permiso("activos.crear")
def api_editar_tipo(id_activos: int):
    """Inline edit del campo `tipo` desde /activos.

    TMT 2026-05-20 — JSON `{tipo: 'T'|'I'|'M'|'K'|'C'}`. Devuelve
    `{ok, tipo, categoria_orden, categoria_label}` para que la fila se
    repinte sin recargar.
    """
    data = request.get_json(silent=True) or request.form
    tipo_nuevo = (data.get("tipo") or "").strip().upper()
    if not tipo_nuevo:
        return jsonify({"ok": False, "error": "Tipo requerido."}), 400
    try:
        r = queries.editar_tipo(
            id_activos, tipo_nuevo,
            usuario=(g.user or {}).get("username", "web"),
        )
        return jsonify({"ok": True, **r})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No pude guardar: {e}"}), 500


@activos_bp.route("/activos/activar-maquinaria", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("activos.crear")
def activar_maquinaria():
    """Wizard de activación de maquinaria.

    TMT 2026-05-20 — pedido dueña. Inputs: proveedor + anticipos vivos a
    consumir + valor total + vida útil + deuda + nº cuotas + meses entre +
    fecha primera cuota. Side effects atómicos: consume anticipos +
    INSERT activo + N posdats + mov_doble (ver activos.queries.activar_maquinaria).
    """
    from datetime import timedelta

    import db as _db

    if request.method == "POST":
        codigo_prov = (request.form.get("codigo_prov") or "").strip().upper()
        ids_raw     = request.form.getlist("id_dolares")
        try:
            ids = [int(x) for x in ids_raw if x and str(x).strip()]
        except ValueError:
            flash("IDs de anticipos inválidos.", "warn")
            return redirect(url_for("activos.activar_maquinaria",
                                    prov=codigo_prov))
        concepto         = (request.form.get("concepto") or "").strip()
        tipo             = (request.form.get("tipo") or "").strip().upper()
        valor_total      = parse_monto(request.form.get("valor_total")) or 0
        vida_util_meses  = parse_int(request.form.get("vida_util_meses")) or 60
        n_cuotas         = parse_int(request.form.get("n_cuotas")) or 0
        meses_entre      = parse_int(request.form.get("meses_entre_cuotas")) or 0
        fecha_primera    = parse_date(request.form.get("fecha_primera_cuota"))
        try:
            r = queries.activar_maquinaria(
                codigo_prov=codigo_prov,
                ids_anticipos=ids,
                concepto=concepto,
                tipo=tipo,
                valor_total=valor_total,
                vida_util_meses=vida_util_meses,
                n_cuotas=n_cuotas,
                meses_entre_cuotas=meses_entre,
                fecha_primera_cuota=fecha_primera,
                usuario=(g.user or {}).get("username", "web"),
            )
            cuotas_msg = (f" + {r['n_cuotas']} posdat(s) cuota mensual "
                          f"${r['cuota_mensual']:.2f}") if r["deuda_total"] > 0 else ""
            flash(
                f"Máquina activada: {concepto} (id {r['id_activos']}) · "
                f"valor ${r['valor_total']:.2f} · "
                f"{r['n_anticipos_consumidos']} anticipo(s) consumido(s)"
                f"{cuotas_msg}.",
                "ok",
            )
            return redirect(url_for("activos.lista"))
        except ValueError as e:
            flash(str(e), "warn")
            return redirect(url_for("activos.activar_maquinaria",
                                    prov=codigo_prov))
        except Exception as e:  # noqa: BLE001
            flash_exc("No pude activar la máquina", e)
            return redirect(url_for("activos.activar_maquinaria",
                                    prov=codigo_prov))

    # GET — lista proveedores con anticipos vivos + anticipos del prov elegido
    prov_sel = (request.args.get("prov") or "").strip().upper() or None
    # Defensivo si el módulo dolares falla por alguna razón.
    grupos = []
    anticipos = []
    nombres: dict[str, str] = {}
    try:
        from modules.dolares import queries as _doq
        # TMT 2026-05-20 — pedido dueña: "si cargo maquinaria solo me
        # debería aparecer los U". Filtramos proveedores tipo='U'.
        grupos = _doq.anticipos_pendientes_por_proveedor(tipos_filter=["U"]) or []
        if prov_sel:
            anticipos = _doq.anticipos_pendientes_de_proveedor(prov_sel) or []
        # Mapeo de nombres por código.
        rows = _db.fetch_all(
            "SELECT codigo_prov, COALESCE(nombre,'') AS nombre "
            "FROM scintela.proveedor"
        ) or []
        nombres = {r["codigo_prov"]: r["nombre"] for r in rows}
        for g_row in grupos:
            g_row["nombre"] = nombres.get(g_row["codigo_prov"]) or ""
    except Exception as e:  # noqa: BLE001
        flash(f"No pude cargar anticipos: {e}", "warn")

    primera_cuota_default = (today_ec() + timedelta(days=90)).isoformat()
    return render_template(
        "activos/activar_maquinaria.html",
        grupos=grupos,
        prov_sel=prov_sel,
        anticipos=anticipos,
        nombres=nombres,
        hoy=today_ec().isoformat(),
        primera_cuota_default=primera_cuota_default,
        tipos_canonicos=queries.TIPOS_CANONICOS,
    )


@activos_bp.route("/activos/reversar-activacion/<int:id_mov_doble>",
                  methods=["GET", "POST"])
@requiere_login
@requiere_permiso("activos.crear")
def reversar_activacion(id_mov_doble: int):
    """Deshace una activación de maquinaria desde /historial (pedido dueña
    2026-07-08). GET: confirmación. POST: ejecuta queries.reversar_activacion
    (restaura anticipos + elimina cuotas + elimina la máquina, atómico)."""
    if request.method == "GET":
        return render_template(
            "_confirmar_accion.html",
            titulo="Deshacer activación de maquinaria",
            mensaje=(
                "Vas a deshacer esta activación: se ELIMINA la máquina, se "
                "borran las cuotas (posdatados) creadas y los anticipos "
                "vuelven a estar vivos (sin consumir). Queda registrado en "
                "/historial. No se puede si la máquina ya amortizó un mes o "
                "alguna cuota ya fue registrada al banco."
            ),
            accion_url=url_for("activos.reversar_activacion",
                               id_mov_doble=id_mov_doble),
            volver_url=url_for("historial.lista"),
            motivo_requerido=False,
            motivo_obligatorio=False,
            confirm_label="Deshacer activación",
        )
    motivo = (request.form.get("motivo") or "").strip()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.reversar_activacion(id_mov_doble, motivo=motivo, usuario=usuario)
        flash(
            f"Activación deshecha: máquina #{r['id_activos']} eliminada, "
            f"{r['restaurados']} anticipo(s) restaurados, "
            f"{r['cuotas_eliminadas']} cuota(s) eliminada(s).",
            "ok",
        )
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude deshacer la activación", e)
    return redirect(url_for("historial.lista"))


@activos_bp.route("/activos/_api/reordenar", methods=["POST"])
@requiere_login
@requiere_permiso("activos.crear")  # mismo permiso que crear, agrupado
def api_reordenar():
    """Persiste el nuevo orden manual desde el drag-and-drop.

    TMT 2026-05-20 — JSON `{ids: [int]}`. La UI manda los ids EN EL
    NUEVO ORDEN VISIBLE; el endpoint le asigna orden_manual = 1..N.
    """
    data = request.get_json(silent=True) or request.form
    ids_raw = data.get("ids") or []
    if isinstance(ids_raw, str):
        ids_raw = [x for x in ids_raw.split(",") if x.strip()]
    try:
        ids = [int(x) for x in ids_raw]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "IDs inválidos."}), 400
    try:
        n = queries.reordenar(
            ids, usuario=(g.user or {}).get("username", "web"),
        )
        return jsonify({"ok": True, "n_actualizados": n})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No pude guardar el orden: {e}"}), 500


@activos_bp.route("/activos/amortizar", methods=["POST"])
@requiere_login
@requiere_permiso("activos.amortizar")
def amortizar():
    """Corre `scintela.actualizar_amortizacion()` del mes actual.

    Idempotente: si ya corrió este mes, sale sin tocar nada.
    """
    try:
        usuario = (g.user or {}).get("username", "web")
        result = queries.correr_amortizacion(usuario=usuario)
        if result.get("ya_estaba"):
            flash(
                f"La amortización del mes {result['mes']} ya estaba aplicada — "
                "no se tocó ningún activo.",
                "info",
            )
        else:
            flash(
                f"Amortización del mes {result['mes']} ejecutada. "
                f"{result['filas_tocadas']} activo(s) actualizado(s).",
                "ok",
            )
    except Exception as e:
        flash_exc("No pude correr la amortización", e)
    return redirect(url_for("activos.lista"))
