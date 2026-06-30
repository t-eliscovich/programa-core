"""Listado y detalle de facturas."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import db
from auth import requiere_login, requiere_permiso
from error_messages import flash_exc, humanize
from exports import csv_response
from filters import today_ec

from . import queries

facturas_bp = Blueprint("facturas", __name__, template_folder="templates")


def _parse_date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_monto(s: str):
    s = (s or "").strip()
    if s == "":
        return None
    if "," in s and s.count(",") == 1:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


@facturas_bp.route("/facturas/nueva", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def nueva():
    """Crear una factura nueva. Preserva reglas de ALTAS.PRG:

    - stat inicial 'A', saldo = importe, abono = 0
    - vencimiento = fecha + cliente.pago días (default 30) si no se indica
    - numf auto = MAX(numf)+1 si se deja vacío
    """
    errores: list[str] = []
    form: dict = {}

    # Datalist de clientes para autocomplete — bajo costo, <2000 items.
    try:
        from modules.autocomplete.queries import clientes_para_datalist
        clientes_datalist = clientes_para_datalist()
    except Exception:
        clientes_datalist = []

    # Sugerir siguiente numf + fecha hoy para GET
    if request.method == "GET":
        try:
            form["numf_sugerido"] = queries.proximo_numf()
        except Exception:
            form["numf_sugerido"] = ""
        # Fecha default en DD/MM/YYYY — formato que la contadora espera tipear.
        # TMT 2026-06-04: today_ec(), no datetime.now() UTC (de noche fechaba mañana).
        form["fecha"] = today_ec().strftime("%d/%m/%Y")
        # Restaurar campos pre-cargados via query string (ej. cuando se
        # redirige de /clientes/nuevo después de crear un cliente nuevo
        # que disparó este form). Cualquier campo del form aparece en
        # request.args sobrescribe el default.
        for k in ("fecha", "codigo_cli", "kg", "importe", "numf",
                  "vencimiento", "condic", "tipo", "numf_completo"):
            if request.args.get(k):
                form[k] = request.args.get(k)
        # Si veníamos de crear un cliente, el flash "Cliente XYZ creado"
        # ya lo manda clientes.nuevo. Acá NO duplicamos — sino aparecen
        # dos toasts en pantalla. La banner azul en el form alcanza para
        # comunicar el contexto de "veníamos de otra pantalla".
        return render_template("facturas/nueva.html", form=form, errores=errores,
                               clientes_datalist=clientes_datalist)

    # POST — validar
    fecha = _parse_date(request.form.get("fecha"))
    codigo_cli = (request.form.get("codigo_cli") or "").strip().upper()
    kg = _parse_monto(request.form.get("kg"))
    importe = _parse_monto(request.form.get("importe"))
    numf_raw = (request.form.get("numf") or "").strip()
    numf = int(numf_raw) if numf_raw.isdigit() else None
    venci = _parse_date(request.form.get("vencimiento"))
    condic = (request.form.get("condic") or "").strip()[:2] or None
    tipo = (request.form.get("tipo") or "").strip()[:2] or None
    numf_completo = (request.form.get("numf_completo") or "").strip() or None
    # Devolución: el dBase la trata como factura con kg/importe negativos
    # (MODIFICA.PRG:1195 — NT.DEVOL = NUMF>0 AND IMPORTE<0). Si el usuario
    # tipea valores positivos, le ponemos el signo menos automáticamente.
    # Si los tipea ya negativos, los respetamos tal cual.
    devolucion = bool(request.form.get("devolucion"))
    if devolucion:
        if kg is not None and kg > 0:
            kg = -kg
        if importe is not None and importe > 0:
            importe = -importe

    if fecha is None:
        errores.append("Fecha inválida.")
    if not codigo_cli:
        errores.append("Código de cliente requerido.")
    elif not db.fetch_one(
        "SELECT 1 AS x FROM scintela.cliente WHERE codigo_cli = %s",
        (codigo_cli,),
    ):
        # Cliente no existe → flujo guiado: mandamos al usuario a
        # /clientes/nuevo con el código pre-cargado, y guardamos los datos
        # ya tipeados del form en el `next` URL para restaurar la factura
        # cuando el cliente se cree. TMT 2026-05-11: pidió que el flujo
        # sea automático en vez de tener que cargar manualmente el cliente.
        # Permisos viven en g.permisos (top-level), NO en g.user["permisos"]
        # — convención canónica del skill programa-core.
        _permisos = getattr(g, "permisos", set()) or set()
        if "clientes.crear" in _permisos or "*" in _permisos:
            from urllib.parse import urlencode
            restore_args = {
                "fecha": request.form.get("fecha") or "",
                "codigo_cli": codigo_cli,
                "kg": request.form.get("kg") or "",
                "importe": request.form.get("importe") or "",
                "numf": numf_raw,
                "vencimiento": request.form.get("vencimiento") or "",
                "condic": condic or "",
                "tipo": tipo or "",
                "numf_completo": numf_completo or "",
                "vuelta": "1",
            }
            restore_args = {k: v for k, v in restore_args.items() if v}
            next_url = url_for("facturas.nueva") + "?" + urlencode(restore_args)
            flash(
                f"El cliente {codigo_cli} no existe — completá los datos "
                "para crearlo y después seguís con la factura.",
                "warning",
            )
            return redirect(
                url_for("clientes.nuevo", codigo=codigo_cli, next=next_url)
            )
        # Sin permiso clientes.crear, mantener el error clásico.
        errores.append(f"El cliente {codigo_cli!r} no existe.")
    # Validación de signo: venta normal pide positivos, devolución pide
    # negativos (= la mercadería vuelve, los $ regresan al cliente).
    if devolucion:
        if importe is None or importe >= 0:
            errores.append("Devolución: importe debe ser distinto de cero.")
        if kg is None or kg > 0:
            errores.append("Devolución: kg debe ser distinto de cero.")
    else:
        if importe is None or importe <= 0:
            errores.append("Importe requerido (mayor que cero).")
        if kg is None or kg < 0:
            errores.append("Kg requerido (no puede ser negativo).")

    # Preservar lo que cargó el usuario para re-renderizar el form
    form.update({
        "fecha": request.form.get("fecha"),
        "codigo_cli": codigo_cli,
        "kg": request.form.get("kg"),
        "importe": request.form.get("importe"),
        "numf": numf_raw,
        "vencimiento": request.form.get("vencimiento"),
        "condic": condic or "",
        "tipo": tipo or "",
        "numf_completo": numf_completo or "",
        "devolucion": devolucion,
    })

    if errores:
        return render_template("facturas/nueva.html", form=form, errores=errores,
                               clientes_datalist=clientes_datalist), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:2].upper()
        creada = queries.crear(
            fecha=fecha,
            codigo_cli=codigo_cli,
            kg=kg, importe=importe,
            numf=numf,
            vencimiento=venci,
            condic=condic, tipo=tipo,
            numf_completo=numf_completo,
            clave=clave,
            usuario=usuario,
        )
        etiqueta = "Devolución" if devolucion else "Factura"
        flash(f"{etiqueta} N° {creada.get('numf')} creada.", "ok")
        return redirect(url_for(
            "facturas.detalle",
            id_factura=(creada.get("numf") or creada["id_factura"]),
        ))
    except Exception as e:
        # TMT 2026-05-14 (#37): humanizar antes de mostrar.
        import logging as _logging
        _logging.getLogger("programa_core.facturas").exception(
            "facturas.nueva falló"
        )
        errores.append(f"No pude crear la factura: {humanize(e)}")
        return render_template("facturas/nueva.html", form=form, errores=errores,
                               clientes_datalist=clientes_datalist), 500


@facturas_bp.route("/facturas/<int:id_factura>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def editar(id_factura: int):
    """Edición *blanda* de una factura emitida.

    Sólo se puede tocar abono / condic / observacion. Para corregir importe,
    cliente, fecha, kg, numf → anular y reemitir (regla Ecuador).
    """
    fact = queries.por_id_interno(id_factura)
    if not fact:
        abort(404)
    _numf_url = fact.get("numf") or id_factura
    if (fact.get("stat") or "").upper() in queries.STATS_ANULADAS:
        flash("La factura está anulada/eliminada — no se puede editar.", "warn")
        return redirect(url_for("facturas.detalle", id_factura=_numf_url))

    errores: list[str] = []
    form: dict = {
        "abono": str(fact.get("abono") or 0),
        "condic": fact.get("condic") or "",
        "observacion": "",
    }

    if request.method == "POST":
        abono_str = request.form.get("abono")
        abono = _parse_monto(abono_str)
        condic = (request.form.get("condic") or "").strip().upper()[:2] or None
        observacion = (request.form.get("observacion") or "").strip() or None

        if abono is None:
            errores.append("Abono inválido.")
        elif float(abono) < 0:
            errores.append("El abono no puede ser negativo.")

        form.update({
            "abono": abono_str,
            "condic": condic or "",
            "observacion": observacion or "",
        })

        if errores:
            return render_template(
                "facturas/editar.html",
                fact=fact, form=form, errores=errores,
            ), 400
        try:
            usuario = (g.user or {}).get("username", "web")
            res = queries.editar(
                id_factura,
                abono=abono,
                condic=condic,
                observacion=observacion,
                usuario=usuario,
            )
            flash(
                f"Factura editada — saldo nuevo: $ {res['saldo']:,.2f} (stat: {res['stat_nuevo']}).",
                "ok",
            )
            return redirect(url_for("facturas.detalle", id_factura=_numf_url))
        except ValueError as e:
            errores.append(str(e))
            return render_template(
                "facturas/editar.html",
                fact=fact, form=form, errores=errores,
            ), 400
        except Exception as e:
            # TMT 2026-05-14 (#37): humanizar antes de mostrar.
            import logging as _logging
            _logging.getLogger("programa_core.facturas").exception(
                "facturas.editar falló id=%s", id_factura
            )
            errores.append(f"Error al editar: {humanize(e)}")
            return render_template(
                "facturas/editar.html",
                fact=fact, form=form, errores=errores,
            ), 500

    return render_template("facturas/editar.html", fact=fact, form=form, errores=errores)


@facturas_bp.route("/facturas/<int:id_factura>/campo", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def editar_campo(id_factura: int):
    """Edición inline genérica de un campo (importe/kg/fecha) desde /facturas.
    Dueña 2026-05-28: 'dejame editar los montos en facturas'.

    Body: ``campo=<importe|kg|fecha|numf>``, ``valor=<str>``.
    Devuelve JSON con el campo actualizado.
    """
    campo = (request.form.get("campo") or "").strip().lower()
    valor = (request.form.get("valor") or "").strip()
    if campo not in ("importe", "kg", "fecha", "numf"):
        return jsonify(ok=False, error=f"Campo no soportado: {campo}"), 400
    if not valor:
        return jsonify(ok=False, error="Valor vacío."), 400

    # Para campos numéricos, normalizar coma decimal ES → punto.
    if campo in ("importe", "kg"):
        valor_norm = valor.replace(".", "").replace(",", ".") if "," in valor and valor.count(",") == 1 else valor
    else:
        valor_norm = valor

    try:
        usuario = (g.user or {}).get("username", "web")
        res = queries.editar_campo(id_factura, campo, valor_norm, usuario=usuario)
        return jsonify(ok=True, **{k: v for k, v in res.items()
                                    if k != "id_factura"})
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        import logging as _logging
        _logging.getLogger("programa_core.facturas").exception(
            "facturas.editar_campo falló id=%s campo=%s", id_factura, campo
        )
        return jsonify(ok=False, error=humanize(e)), 500


@facturas_bp.route("/facturas/<int:id_factura>/numf", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def editar_numf(id_factura: int):
    """Corrige el N° de factura (typo al cargar). JSON endpoint para edit
    inline desde /facturas. Dueña 2026-05-28: 'dejame editar numero de
    facturas'.

    Body: ``numf=<int>`` (required), ``numf_completo=<str>`` (opcional).
    Devuelve JSON: ``{ok: bool, numf, numf_completo, error?: str}``.
    """
    numf_raw = (request.form.get("numf") or "").strip()
    if not numf_raw.isdigit():
        return jsonify(ok=False, error="El N° debe ser un entero positivo."), 400

    numf_completo_raw = (request.form.get("numf_completo") or "").strip() or None
    try:
        usuario = (g.user or {}).get("username", "web")
        res = queries.editar_numf(
            id_factura,
            int(numf_raw),
            nuevo_numf_completo=numf_completo_raw,
            usuario=usuario,
        )
        return jsonify(
            ok=True,
            numf=res["numf_nuevo"],
            numf_previo=res["numf_previo"],
            numf_completo=res["numf_completo_nuevo"],
        )
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        import logging as _logging
        _logging.getLogger("programa_core.facturas").exception(
            "facturas.editar_numf falló id=%s", id_factura
        )
        return jsonify(ok=False, error=humanize(e)), 500


@facturas_bp.route("/facturas/<int:id_factura>/confirmar-anulacion", methods=["GET"])
@requiere_login
@requiere_permiso("facturas.anular")
def confirmar_anulacion(id_factura: int):
    """Paso 1 del 2-step undo: muestra el resumen + pide motivo antes de anular."""
    fact = queries.por_id_interno(id_factura)
    if not fact:
        abort(404)
    _numf_url = fact.get("numf") or id_factura
    if fact.get("stat") == "Y":
        flash("La factura ya está anulada.", "warn")
        return redirect(url_for("facturas.detalle", id_factura=_numf_url))
    detalle = {
        "N° factura": fact.get("numf_completo") or fact.get("numf"),
        "Fecha": (fact.get("fecha").strftime("%d/%m/%Y") if fact.get("fecha") else "—"),
        "Cliente": f"{fact.get('codigo_cli', '')} — {fact.get('cliente') or ''}",
        "Importe": f"$ {fact.get('importe') or 0}",
        "Saldo actual": f"$ {fact.get('saldo') or 0}",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Anular factura {fact.get('numf_completo') or fact.get('numf')}",
        mensaje=(
            f"Vas a anular la factura {fact.get('numf_completo') or fact.get('numf')} "
            f"del cliente {fact.get('codigo_cli')} por $ {fact.get('importe') or 0}."
        ),
        detalle_registro=detalle,
        accion_url=url_for("facturas.anular", id_factura=id_factura),
        volver_url=url_for("facturas.detalle", id_factura=_numf_url),
        motivo_requerido=True,
        confirm_label="Confirmar anulación",
    )


@facturas_bp.route("/facturas/<int:id_factura>/reversar-carga", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.anular")
def reversar_carga(id_factura: int):
    """Deshace una carga errónea (DELETE) — solo si no tiene movimientos.

    Distinto de anular (que setea stat='X' y deja histórico). Esto
    elimina la fila por completo. Útil cuando cargaste una factura
    por error y querés deshacer la carga.
    """
    try:
        usuario = (g.user or {}).get("username", "web")
        res = queries.borrar_carga_erronea(id_factura, usuario=usuario)
        flash(f"Factura {res['numf_completo'] or '#'+str(res['numf'])} borrada (la carga quedó deshecha).", "ok")
        return redirect(url_for("facturas.lista"))
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude reversar la carga", e)
    _f = queries.por_id_interno(id_factura)
    _numf_url = (_f or {}).get("numf") or id_factura
    return redirect(url_for("facturas.detalle", id_factura=_numf_url))


@facturas_bp.route("/facturas/<int:id_factura>/anular", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.anular")
def anular(id_factura: int):
    motivo = (request.form.get("motivo") or "").strip()
    # Motivo opcional — la dueña puede dejarlo vacío (ej. "error de carga"
    # implícito). TMT 2026-05-13.
    # numf para el flash/redirect (el único número visible de la factura).
    _f = queries.por_id_interno(id_factura)
    _numf_url = (_f or {}).get("numf") or id_factura
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.anular(id_factura, motivo=motivo, usuario=usuario)
        flash(f"Factura {_numf_url} anulada.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude anular", e)
    return redirect(url_for("facturas.detalle", id_factura=_numf_url))


@facturas_bp.route("/facturas/<int:id_factura>")
@requiere_login
@requiere_permiso("facturas.ver")
def detalle(id_factura: int):
    fact = queries.por_id(id_factura)
    if not fact:
        abort(404)
    # TMT 2026-06-07 dueña: "la factura tiene UN solo número, el del dBase
    # (numf). No quiero un número 'programa'." Canonalizamos la URL al numf:
    # si entraron por el id interno, redirigimos para que la barra de
    # direcciones muestre el número real de la factura, no el id.
    if fact.get("numf") and int(id_factura) != int(fact["numf"]):
        return redirect(url_for("facturas.detalle", id_factura=fact["numf"]))
    # TMT 2026-06-07: el param de la URL puede venir como numf (el número del
    # dBase, el ÚNICO visible) y `por_id` lo resuelve a la fila real. Las
    # aplicaciones de cheques se buscan por el id_factura INTERNO resuelto
    # (`fact["id_factura"]`), NO por el numf — si no, una factura cuyo
    # numf != id_factura mostraba "Sin cheques aplicados" aunque tuviera.
    _id_real = fact["id_factura"]
    aplicaciones = queries.cheques_aplicados(_id_real)
    retenciones = queries.retenciones_aplicadas(fact["codigo_cli"], fact["numf"])
    total_aplicado = sum(float(a["aplicado"] or 0) for a in aplicaciones)
    total_retenido = sum(float(r["rete"] or 0) for r in retenciones)
    # Recientes — best-effort, no rompe el detalle si falla.
    try:
        from modules.recientes import queries as rec
        rec.registrar(
            "factura", id_factura,
            etiqueta=f"Factura {fact.get('numf_completo') or fact.get('numf')} · {fact.get('cliente') or fact.get('codigo_cli','')}",
        )
    except Exception:
        pass
    return render_template(
        "facturas/detalle.html",
        fact=fact,
        aplicaciones=aplicaciones,
        retenciones=retenciones,
        total_aplicado=total_aplicado,
        total_retenido=total_retenido,
    )


@facturas_bp.route("/facturas/diag-cartera")
@requiere_login
@requiere_permiso("facturas.ver")
def diag_cartera():
    """Diagnóstico: quién/cuándo cargó las facturas de cartera reciente."""
    rows = db.fetch_all(
        """
        SELECT fecha, codigo_cli, numf, numf_completo, importe, stat,
               usuario_crea
          FROM scintela.factura
         WHERE fecha >= CURRENT_DATE - INTERVAL '7 days'
         ORDER BY fecha DESC, id_factura DESC
         LIMIT 30
        """
    ) or []
    # Conteos por usuario_crea
    por_usuario = db.fetch_all(
        """
        SELECT COALESCE(usuario_crea, '<sin usuario>') AS u, COUNT(*) AS n
          FROM scintela.factura
         WHERE fecha >= CURRENT_DATE - INTERVAL '30 days'
         GROUP BY usuario_crea
         ORDER BY n DESC
        """
    ) or []
    return {
        "ultimas_7d": [
            {"fecha": str(r["fecha"]), "cli": r.get("codigo_cli"),
             "numf": r.get("numf"), "numf_completo": r.get("numf_completo"),
             "importe": float(r.get("importe") or 0), "stat": r.get("stat"),
             "usuario_crea": r.get("usuario_crea")}
            for r in rows
        ],
        "conteo_30d_por_usuario": [{"usuario": r["u"], "n": r["n"]} for r in por_usuario],
    }


@facturas_bp.route("/facturas/debug-match/<int:numf>")
@requiere_login
@requiere_permiso("facturas.ver")
def debug_match_factura(numf: int):
    """Debug rápido: para un numf específico, mostrar qué hay en PC
    y qué match-keys produce, y qué dice Asinfo para esa misma factura.
    """
    from datetime import timedelta as _td

    from modules.asinfo import aliases as _aliases
    from modules.asinfo import service as _asinfo_service
    pc = db.fetch_all(
        """SELECT id_factura, fecha, codigo_cli, kg, importe, abono, saldo,
                  stat, tipo, numf, numf_completo
             FROM scintela.factura
            WHERE numf = %s
            ORDER BY fecha DESC""",
        (numf,),
    ) or []
    pc_norm = []
    for r in pc:
        f = r.get("fecha")
        f_iso = f.isoformat() if hasattr(f, "isoformat") else str(f)[:10]
        imp = float(r.get("importe") or 0)
        cli = (r.get("codigo_cli") or "").strip().upper()
        pc_norm.append({
            "id_factura": r.get("id_factura"),
            "fecha": f_iso,
            "codigo_cli_raw": r.get("codigo_cli"),
            "codigo_cli_norm": cli,
            "importe_raw": float(r.get("importe") or 0),
            "importe_cents": int(round(imp * 100)),
            "stat": r.get("stat"),
            "tipo": r.get("tipo"),
            "numf": r.get("numf"),
            "numf_completo": r.get("numf_completo"),
            "match2_key": [r.get("numf"), cli],
            "match3_key": [cli, f_iso, int(round(imp * 100))],
        })
    # Buscar en Asinfo (últimos 60 días, esa factura).
    try:
        hoy = today_ec()
        asinfo_rows = _asinfo_service.facturas_periodo(hoy - _td(days=60), hoy) or []
    except Exception as e:
        asinfo_rows = [{"error": str(e)}]
    ai_match = [r for r in asinfo_rows if str(r.get("numero", "")).endswith(str(numf))]
    ai_norm = []
    for r in ai_match:
        usd_a = float(r.get("usd") or 0)
        cli_a = (r.get("cliente_codigo") or "").strip().upper()
        cli_p = _aliases.to_pc(cli_a)
        ai_norm.append({
            "numero": r.get("numero"),
            "tipo": r.get("tipo"),
            "fecha": str(r.get("fecha"))[:10] if r.get("fecha") else None,
            "cli_asinfo": cli_a,
            "cli_pc_esperado": cli_p,
            "usd": usd_a,
            "usd_cents": int(round(usd_a * 100)),
            "match2_key": [numf, cli_p],
            "match3_key": [cli_p, str(r.get("fecha"))[:10] if r.get("fecha") else None, int(round(usd_a * 100))],
        })
    return {"pc": pc_norm, "asinfo": ai_norm, "aliases": _aliases.todos()}


@facturas_bp.route("/facturas/desde-asinfo")
@requiere_login
@requiere_permiso("facturas.ver")
def desde_asinfo():
    """Lista facturas que están en Asinfo pero NO en PC.

    Pedido Tamara 2026-05-23: match inverso PC ← Asinfo. Permite cargar
    a PC con un click las que falten.

    TMT 2026-05-26 dueña: ahora aplica `scintela.cliente_alias` (CL2↔CLR,
    AJ2↔AJO, J3C↔VGA) — si Asinfo viene como CL2 y PC tiene la misma
    factura bajo CLR, NO se reporta como missing. Además detecta y
    sugiere nuevos aliases: si todas las missing de un cliente_asinfo X
    tienen sus `numf` en PC pero bajo otro cliente Y, propone X↔Y.

    Filtros: ?desde=YYYY-MM-DD&hasta=YYYY-MM-DD (default: últimos 30 días).
    """
    import re as _re
    from datetime import date as _date
    from datetime import timedelta as _td

    from modules.asinfo import aliases as _aliases

    hoy = today_ec()
    # TMT 2026-05-26 dueña: cutoff para no marcar como missing facturas
    # del día que aún no llegaron por sync DBF (gap de carga).
    # TMT 2026-05-28 v1: bajamos de 3d a 1d (ocultaba misses legítimos
    #                    del 27/05).
    # TMT 2026-05-28 v2: pasamos a OPT-IN (por default NO ocultamos
    # nada). Si el sync DBF está al día las del 28 deben aparecer; si
    # la dueña detecta falsos missing del día puede activar el toggle
    # "Excluir las de hoy" en la UI. URL: ?excluir_hoy=1.
    # Back-compat: el toggle viejo "?incluir_recientes=1" sigue
    # haciendo lo MISMO (mostrar todo) — el cambio de default es lo
    # único que se invierte.
    cutoff_reciente = hoy
    cutoff_reciente_str = cutoff_reciente.isoformat()
    desde_s = request.args.get("desde") or (hoy - _td(days=30)).isoformat()
    hasta_s = request.args.get("hasta") or hoy.isoformat()
    # Default: mostrar todo (incluir_recientes=True). El toggle nuevo
    # ?excluir_hoy=1 oculta las de hoy cuando el sync no llegó todavía.
    excluir_hoy = request.args.get("excluir_hoy") == "1"
    incluir_recientes = not excluir_hoy
    desde = _parse_date(desde_s) or (hoy - _td(days=30))
    hasta = _parse_date(hasta_s) or hoy
    # TMT 2026-06-30 dueña: "ya no importan las de mayo, dejá de hacer el match
    # para atrás de junio". Piso fijo: nunca comparamos antes del 2026-06-01
    # (el backlog abril-mayo se deja como está, no es match-able). Si el usuario
    # pide una fecha anterior, se sube al piso.
    _piso_match = _date(2026, 6, 1)
    if desde < _piso_match:
        desde = _piso_match
        desde_s = _piso_match.isoformat()

    def _fecha_a_date(x):
        """Asinfo a veces devuelve la fecha como ISO string. Normalizar."""
        if x is None or x == "":
            return None
        if hasattr(x, "isoformat"):
            return x  # ya es date/datetime
        try:
            return _date.fromisoformat(str(x)[:10])
        except Exception:
            return None

    # Asinfo: traer facturas del rango
    try:
        from modules.asinfo import service as asinfo_service
        asinfo_rows = asinfo_service.facturas_periodo(desde, hasta) or []
    except Exception as e:
        flash_exc("No se pudo conectar con Asinfo", e)
        asinfo_rows = []

    # PC: traer numf_completo + (numf, codigo_cli) del rango.
    # numf=sufijo numérico, codigo_cli=código en PC. La conjunción es la PK
    # lógica de la factura — match más fuerte que solo numf_completo string.
    pc_rows = db.fetch_all(
        """
        SELECT numf_completo, numf, codigo_cli, importe, fecha, kg
          FROM scintela.factura
         WHERE fecha BETWEEN %s AND %s
           -- TMT 2026-06-10 decisión dueña: las backfill automáticas NO
           -- cuentan como "cargadas" → siguen apareciendo acá como
           -- pendientes; al apretar Cargar, el guard las convierte en
           -- 'asinfo-carga' (cuentan) en vez de duplicar.
           AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
        (desde, hasta),
    ) or []
    # TMT 2026-05-29 dueña 'tenes que arreglar todo': antes excluíamos
    # stat='X' (anuladas). Pero si Asinfo aún muestra la factura como viva
    # y PC la cargó y después la anuló, NO es "falta cargar" — ya está
    # cargada (anulada). Incluir todas. La divergencia "Asinfo dice viva,
    # PC dice anulada" se ve en otro lado (audit de discrepancias).
    pc_numfs_str = {(r.get("numf_completo") or "").strip()
                    for r in pc_rows if r.get("numf_completo")}
    # TMT 2026-05-29 dueña: 'ya agregue siguen siendo muchas'. Las 52 que
    # sobran NO son problema de alias — la 174177 AJO $723.24 29/04 ya
    # está en PC con mismo numf/cli/fecha/importe pero igual aparece como
    # 'faltan cargar'. Causa probable: numf_completo en PC se guarda sin
    # guiones (o sólo el sufijo) y Match 1 compara contra '001-099-000174177'
    # de Asinfo — falla. Match 2 también falla si numf en PC quedó como
    # MAX+1 (cuando se cargó manual sin parsear el numf_completo).
    # Fix: indexar también el numf extraído del numf_completo y agregar
    # Match 3 por (cli_pc, fecha, importe) con tolerancia de 1 centavo.
    def _extract_numf_local(s: str) -> int | None:
        if not s:
            return None
        m = _re.findall(r"\d+", str(s))
        if not m:
            return None
        try:
            return int(m[-1])
        except (ValueError, TypeError):
            return None

    # Map (numf, codigo_cli_PC) → True, para alias-aware matching.
    pc_by_numf_cli: set[tuple[int, str]] = set()
    pc_by_numf: dict[int, set[str]] = {}
    # Map (cli_pc, fecha_iso, importe_cents) → True para Match 3 fallback.
    pc_by_cli_fecha_importe: set[tuple[str, str, int]] = set()
    # Match 4 (date-agnostic): cli + importe + kg. Cubre facturas cargadas en
    # PC sin numero y con fecha corrida vs Asinfo (ej. BED: misma venta, mismo
    # kg e importe, pero 2 dias de diferencia y sin N) — el match por fecha
    # exacta fallaba. importe+kg identicos para el mismo cliente = misma venta.
    pc_by_cli_importe_kg: set[tuple[str, int, int]] = set()
    for r in pc_rows:
        cli_raw = (r.get("codigo_cli") or "").strip().upper()
        # TMT 2026-05-29 dueña 'sigue habiendo facturas sin match': PC tiene
        # facturas cargadas con codigo_cli="AJ2" (alias) cuando el cliente real
        # canonical es "AJO". El alias AJ2→AJO normaliza al canonical. Mismo
        # del lado Asinfo: cli_pc_esperado = to_pc(cli_asinfo). Si AMBOS se
        # canonicalizan con to_pc(), matchean aunque PC haya cargado bajo el
        # código alias.
        cli_pc_canonical = _aliases.to_pc(cli_raw) if cli_raw else ""
        # Indexar bajo el canonical Y bajo el raw — algunos aliases podrían
        # ir solo en una dirección y queremos cubrir ambos casos.
        cli_keys = {cli_raw, cli_pc_canonical} if cli_pc_canonical else {cli_raw}
        cli_keys.discard("")
        numf = r.get("numf")
        n = None
        if numf:
            try:
                n = int(numf)
            except (TypeError, ValueError):
                n = None
        # También indexar numf extraído del numf_completo — cubre el caso
        # donde numf quedó como MAX+1 pero numf_completo trae el real.
        n_completo = _extract_numf_local(r.get("numf_completo") or "")
        for cand in (n, n_completo):
            if cand is not None:
                for cli_k in cli_keys:
                    pc_by_numf_cli.add((cand, cli_k))
                    pc_by_numf.setdefault(cand, set()).add(cli_k)
        # Match 3 fallback: cli + fecha + importe (en centavos).
        try:
            f_ = r.get("fecha")
            f_iso = f_.isoformat() if hasattr(f_, "isoformat") else (str(f_)[:10] if f_ else None)
            imp = float(r.get("importe") or 0)
            if f_iso:
                for cli_k in cli_keys:
                    pc_by_cli_fecha_importe.add((cli_k, f_iso, int(round(imp * 100))))
        except Exception:
            pass
        # Match 4: indexar por (cli, importe_cents, kg_cents) — sin fecha.
        try:
            kg_pc = float(r.get("kg") or 0)
            imp_pc = float(r.get("importe") or 0)
            if kg_pc and imp_pc:
                for cli_k in cli_keys:
                    pc_by_cli_importe_kg.add(
                        (cli_k, int(round(imp_pc * 100)), int(round(kg_pc * 100)))
                    )
        except Exception:
            pass

    # ── TMT 2026-06-13 dueña "no puede haber 151, no tiene sentido": las
    # ~150 que sobraban son facturas viejas (mayo) que SÍ existen en PC pero
    # como fantasmas usuario_crea='asinfo-backfill' (el query de arriba las
    # excluye a propósito porque NO cuentan en cartera). Marcarlas como
    # "faltan cargar" (rojo) es engañoso: físicamente YA están. Acá traemos
    # un segundo lookup que INCLUYE backfill, con las MISMAS claves de match,
    # para poder separar "ya en PC como backfill" de "falta de verdad".
    pc_bf_rows = db.fetch_all(
        """
        SELECT numf_completo, numf, codigo_cli, importe, fecha, kg
          FROM scintela.factura
         WHERE fecha BETWEEN %s AND %s
           AND COALESCE(usuario_crea, '') = 'asinfo-backfill'
        """,
        (desde, hasta),
    ) or []
    # Mismas estructuras de match que las non-backfill, pero solo backfill.
    pc_bf_numfs_str: set[str] = {(r.get("numf_completo") or "").strip()
                                 for r in pc_bf_rows if r.get("numf_completo")}
    pc_bf_by_numf_cli: set[tuple[int, str]] = set()
    pc_bf_by_cli_fecha_importe: set[tuple[str, str, int]] = set()
    pc_bf_by_cli_importe_kg: set[tuple[str, int, int]] = set()
    for r in pc_bf_rows:
        cli_raw = (r.get("codigo_cli") or "").strip().upper()
        cli_pc_canonical = _aliases.to_pc(cli_raw) if cli_raw else ""
        cli_keys = {cli_raw, cli_pc_canonical} if cli_pc_canonical else {cli_raw}
        cli_keys.discard("")
        numf = r.get("numf")
        n = None
        if numf:
            try:
                n = int(numf)
            except (TypeError, ValueError):
                n = None
        n_completo = _extract_numf_local(r.get("numf_completo") or "")
        for cand in (n, n_completo):
            if cand is not None:
                for cli_k in cli_keys:
                    pc_bf_by_numf_cli.add((cand, cli_k))
        try:
            f_ = r.get("fecha")
            f_iso = f_.isoformat() if hasattr(f_, "isoformat") else (str(f_)[:10] if f_ else None)
            imp = float(r.get("importe") or 0)
            if f_iso:
                for cli_k in cli_keys:
                    pc_bf_by_cli_fecha_importe.add((cli_k, f_iso, int(round(imp * 100))))
        except Exception:
            pass
        try:
            kg_pc = float(r.get("kg") or 0)
            imp_pc = float(r.get("importe") or 0)
            if kg_pc and imp_pc:
                for cli_k in cli_keys:
                    pc_bf_by_cli_importe_kg.add(
                        (cli_k, int(round(imp_pc * 100)), int(round(kg_pc * 100)))
                    )
        except Exception:
            pass

    def _en_pc_backfill(numero, numf, cli_pc_esperado, fecha, usd_a, kg_a=0.0):
        """¿Esta factura Asinfo existe en PC SOLO como backfill?
        Mismo orden de matches que el filtro principal (1/2/3/4)."""
        if numero and numero in pc_bf_numfs_str:
            return True
        if numf and (numf, cli_pc_esperado) in pc_bf_by_numf_cli:
            return True
        if fecha and cli_pc_esperado and usd_a:
            f_iso = fecha.isoformat()
            cents = int(round(usd_a * 100))
            if ((cli_pc_esperado, f_iso, cents) in pc_bf_by_cli_fecha_importe
                or (cli_pc_esperado, f_iso, cents - 1) in pc_bf_by_cli_fecha_importe
                or (cli_pc_esperado, f_iso, cents + 1) in pc_bf_by_cli_fecha_importe):
                return True
        # Match 4 (sin fecha): cli + importe + kg.
        if cli_pc_esperado and usd_a and kg_a:
            cents = int(round(usd_a * 100))
            kgc = int(round(kg_a * 100))
            if any((cli_pc_esperado, cents + d, kgc) in pc_bf_by_cli_importe_kg
                   for d in (-1, 0, 1)):
                return True
        return False

    def _extract_numf(numero: str) -> int | None:
        if not numero:
            return None
        m = _re.findall(r"\d+", str(numero))
        if not m:
            return None
        try:
            return int(m[-1])
        except (ValueError, TypeError):
            return None

    # Filtrar Asinfo que NO está en PC (huérfanas-asinfo) — alias-aware.
    # Solo FACTURA y NTEN — las DEVOLUCION/NC son negativas y rara vez se cargan
    huerfanas = []
    # TMT 2026-06-13: facturas que YA existen en PC pero solo como
    # backfill (no cuentan en cartera). NO son "faltan cargar" — van a
    # un grupo aparte, colapsado.
    en_pc_backfill = []
    for r in asinfo_rows:
        tipo = (r.get("tipo") or "").upper()
        if tipo not in ("FACTURA", "NTEN"):
            continue
        numero = (r.get("numero") or "").strip()
        fecha = _fecha_a_date(r.get("fecha"))
        # Exclusión por fecha reciente (gap normal del sync DBF).
        if not incluir_recientes and fecha and fecha >= cutoff_reciente:
            continue
        # Match 1 (más fuerte): numero == numf_completo en PC.
        if numero and numero in pc_numfs_str:
            continue
        # Match 2 (alias-aware): (numf, cli_pc) — donde cli_pc = alias(cli_asinfo).
        numf = _extract_numf(numero)
        cli_asinfo = (r.get("cliente_codigo") or "").strip().upper()
        cli_pc_esperado = _aliases.to_pc(cli_asinfo)
        if numf and (numf, cli_pc_esperado) in pc_by_numf_cli:
            continue
        # Match 3 (fallback): (cli_pc, fecha, importe). Cubre cuando numf
        # quedó mal guardado pero la factura SÍ está cargada con misma
        # fecha y mismo importe. Tolerancia de 1 centavo (redondeo).
        try:
            usd_a = float(r.get("usd") or 0)
        except (TypeError, ValueError):
            usd_a = 0.0
        if fecha and cli_pc_esperado and usd_a:
            f_iso = fecha.isoformat()
            cents = int(round(usd_a * 100))
            if ((cli_pc_esperado, f_iso, cents) in pc_by_cli_fecha_importe
                or (cli_pc_esperado, f_iso, cents - 1) in pc_by_cli_fecha_importe
                or (cli_pc_esperado, f_iso, cents + 1) in pc_by_cli_fecha_importe):
                continue
        # Match 4 (sin fecha): cli + importe + kg. Para facturas cargadas en PC
        # sin N° y con la fecha corrida vs Asinfo (mismo cliente, mismo kg y
        # mismo importe = misma venta, aunque el día y el número no coincidan).
        try:
            kg_a = float(r.get("kg") or 0)
        except (TypeError, ValueError):
            kg_a = 0.0
        if cli_pc_esperado and usd_a and kg_a:
            cents = int(round(usd_a * 100))
            kgc = int(round(kg_a * 100))
            if any((cli_pc_esperado, cents + d, kgc) in pc_by_cli_importe_kg
                   for d in (-1, 0, 1)):
                continue
        # Calcular hint de "está en PC bajo OTRO cli" — sugerencia de alias.
        otros_clis_pc = []
        if numf:
            otros_clis_pc = sorted(c for c in pc_by_numf.get(numf, set())
                                   if c and c != cli_pc_esperado)
        item = {
            "numero": numero,
            "numf": numf,
            "fecha": fecha,
            "tipo": tipo,
            "cliente_codigo": cli_asinfo,
            "vendedor": r.get("vendedor") or "",
            "kg": float(r.get("kg") or 0),
            "usd": float(r.get("usd") or 0),
            "pc_existe_bajo_cli": otros_clis_pc,  # sugerencia alias
        }
        # ¿Existe físicamente en PC, pero solo como fantasma backfill? Entonces
        # NO falta cargar — va al grupo separado. Cargar la convierte en
        # 'asinfo-carga' (cuenta en cartera) sin duplicar.
        if _en_pc_backfill(numero, numf, cli_pc_esperado, fecha, usd_a, kg_a):
            en_pc_backfill.append(item)
        else:
            huerfanas.append(item)
    huerfanas.sort(key=lambda r: (r["fecha"] or _date.min, r["numero"]), reverse=True)
    en_pc_backfill.sort(key=lambda r: (r["fecha"] or _date.min, r["numero"]), reverse=True)

    # ─── Agregado por cliente Asinfo + detección de alias candidatos ──
    # Para cada cliente_codigo de Asinfo con huerfanas, contar cuántas
    # tienen su numf en PC bajo OTRO cli. Si la mayoría apunta al mismo,
    # ese es candidato a alias.
    grupos_cli: dict[str, dict] = {}
    for h in huerfanas:
        cli = h["cliente_codigo"] or "?"
        g = grupos_cli.setdefault(cli, {
            "cliente_codigo": cli,
            "n": 0,
            "sum_usd": 0.0,
            "sum_kg": 0.0,
            "votos_alias": {},  # codigo_pc → count
            "muestra": [],
        })
        g["n"] += 1
        g["sum_usd"] += h["usd"]
        g["sum_kg"] += h["kg"]
        for c_pc in h["pc_existe_bajo_cli"]:
            g["votos_alias"][c_pc] = g["votos_alias"].get(c_pc, 0) + 1
        if len(g["muestra"]) < 3:
            g["muestra"].append(h)

    # Sugerencia de alias por grupo.
    aliases_existentes = _aliases.todos()
    alias_existe_para = {
        a["codigo_asinfo"]: a["codigo_pc"] for a in aliases_existentes
    }
    sugerencias_alias = []
    for cli, g in grupos_cli.items():
        if not g["votos_alias"]:
            continue
        # El alias-candidato con más votos. Si la mayoría (>=50%) apunta al
        # mismo cli_pc, lo sugerimos.
        ganador = max(g["votos_alias"].items(), key=lambda kv: kv[1])
        c_pc, votos = ganador
        if votos * 2 < g["n"]:  # menos del 50%
            continue
        if alias_existe_para.get(cli) == c_pc:
            continue  # ya existe el alias
        sugerencias_alias.append({
            "codigo_asinfo": cli,
            "codigo_pc": c_pc,
            "votos": votos,
            "total_huerf_cli": g["n"],
            "porcentaje": round(100 * votos / g["n"], 1),
        })
    sugerencias_alias.sort(key=lambda s: s["votos"], reverse=True)
    grupos_ordenados = sorted(
        grupos_cli.values(), key=lambda g: g["n"], reverse=True
    )

    return render_template(
        "facturas/desde_asinfo.html",
        desde=desde_s, hasta=hasta_s,
        n_total_asinfo=len(asinfo_rows),
        n_pc=len(pc_rows),
        huerfanas=huerfanas,
        suma_kg=sum(h["kg"] for h in huerfanas),
        suma_usd=sum(h["usd"] for h in huerfanas),
        en_pc_backfill=en_pc_backfill,
        suma_kg_bf=sum(h["kg"] for h in en_pc_backfill),
        suma_usd_bf=sum(h["usd"] for h in en_pc_backfill),
        # Nuevo TMT 2026-05-26
        cutoff_reciente=cutoff_reciente.isoformat(),
        incluir_recientes=incluir_recientes,
        aliases_existentes=aliases_existentes,
        sugerencias_alias=sugerencias_alias,
        grupos_cli=grupos_ordenados,
    )


@facturas_bp.route("/facturas/aliases/agregar", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def alias_agregar():
    """Agrega un alias cliente Asinfo↔PC. Form: codigo_asinfo, codigo_pc, nota."""
    from modules.asinfo import aliases as _aliases
    codigo_asinfo = (request.form.get("codigo_asinfo") or "").strip().upper()
    codigo_pc = (request.form.get("codigo_pc") or "").strip().upper()
    nota = (request.form.get("nota") or "").strip()
    if not codigo_asinfo or not codigo_pc:
        flash("Faltan codigo_asinfo y codigo_pc.", "warn")
        return redirect(url_for("facturas.desde_asinfo", **request.args))
    try:
        creado = _aliases.agregar(
            codigo_asinfo, codigo_pc,
            nota=nota,
            usuario=(g.user or {}).get("username", "web"),
        )
    except Exception as e:
        flash_exc("No se pudo agregar el alias", e)
        return redirect(url_for("facturas.desde_asinfo"))
    if creado:
        flash(f"Alias agregado: {codigo_asinfo} ↔ {codigo_pc}.", "ok")
    else:
        flash(f"Alias {codigo_asinfo} ↔ {codigo_pc} ya existía.", "info")
    # Mantener filtros de la URL anterior.
    return redirect(request.referrer or url_for("facturas.desde_asinfo"))


@facturas_bp.route("/facturas/aliases/borrar", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def alias_borrar():
    """Borra un alias. Form: codigo_asinfo, codigo_pc."""
    from modules.asinfo import aliases as _aliases
    codigo_asinfo = (request.form.get("codigo_asinfo") or "").strip().upper()
    codigo_pc = (request.form.get("codigo_pc") or "").strip().upper()
    try:
        n = _aliases.borrar(codigo_asinfo, codigo_pc)
    except Exception as e:
        flash_exc("No se pudo borrar el alias", e)
        return redirect(url_for("facturas.desde_asinfo"))
    if n:
        flash(f"Alias borrado: {codigo_asinfo} ↔ {codigo_pc}.", "ok")
    else:
        flash("El alias no existía.", "info")
    return redirect(request.referrer or url_for("facturas.desde_asinfo"))



class _CargaAsinfoSkip(Exception):
    """La fila NO se carga (y no se crea cliente). El mensaje explica por qué
    y queda en el flash de errores del caller."""


def _resolver_cliente_asinfo(
    codigo_cli: str,
    usuario: str,
    numero: str | None = None,
    importe=None,
) -> tuple[str, bool]:
    """Resuelve el codigo de cliente Asinfo -> PC para cargar facturas.

    TMT 2026-06-10 (3 facturas de ayer rebotaron con "cliente no existe en
    PC" aunque el dBase SI tiene los clientes). Dos causas:
      1. La carga usaba el codigo Asinfo CRUDO, sin pasar por el alias map
         (AJ2->AJO etc.) que la vista de huerfanas SI usa.
      2. CLIENTES.DBF no entra en el sync normal (no tiene mapper), asi que
         un cliente dado de alta en el dBase despues del ultimo
         /admin/clientes-import no existe en PC todavia.

    Orden de resolucion:
      1. alias canonical (to_pc) existe en PC -> usarlo.
      2. codigo crudo existe en PC -> usarlo (facturas viejas bajo alias).
      3. GUARD anti-duplicados (TMT 2026-06-10 'fijate que no se repitan
         clientes'): si la MISMA factura (numf) ya esta cargada en PC bajo
         OTRO codigo de cliente:
           - mismo importe (+-1 centavo) y UN solo dueno -> es el mismo
             cliente con otro codigo: se AUTO-REGISTRA el alias
             codigo_asinfo->dueno y la fila se saltea (ya estaba cargada).
             NO se crea cliente duplicado.
           - si no, se saltea con sugerencia de alias (caso ambiguo, lo
             resuelve la duena en /facturas/desde-asinfo).
      4. ninguno -> AUTO-CREAR cliente con ficha minima. La ficha real
         (nombre/RUC/direccion) la completa /admin/clientes-import despues,
         que es rellenar-solo e INSERT-si-falta.

    Raises:
        _CargaAsinfoSkip: la fila no debe cargarse (ya existe / ambigua).

    Returns:
        (codigo_pc_a_usar, cliente_creado)
    """
    import re as _re
    from modules.asinfo import aliases as _aliases
    codigo_cli = (codigo_cli or "").strip().upper()
    cli_pc = _aliases.to_pc(codigo_cli)
    for cand in dict.fromkeys((cli_pc, codigo_cli)):
        if cand and db.fetch_one(
            "SELECT 1 FROM scintela.cliente WHERE codigo_cli = %s", (cand,)
        ):
            return cand, False

    # ── Guard anti-duplicados: ¿esta factura YA esta en PC bajo otro cli? ──
    numero = (numero or "").strip()
    m = _re.findall(r"\d+", numero)
    numf = int(m[-1]) if m else None
    if numf is not None:
        rows = db.fetch_all(
            """
            SELECT codigo_cli, importe FROM scintela.factura
             WHERE numf = %s OR (%s <> '' AND numf_completo = %s)
            """,
            (numf, numero, numero),
        ) or []
        if rows:
            try:
                imp = float(importe) if importe is not None else None
            except (TypeError, ValueError):
                imp = None
            duenos_mismo_importe = sorted({
                (r.get("codigo_cli") or "").strip().upper()
                for r in rows
                if imp is not None
                and abs(float(r.get("importe") or 0) - imp) <= 0.01
            } - {""})
            if duenos_mismo_importe == [codigo_cli]:
                # Ya esta en PC bajo este mismo codigo. Si la copia es
                # backfill automatico (NO cuenta en cartera), apretarle
                # Cargar la CONVIERTE en 'asinfo-carga' (cuenta) — decision
                # duena 2026-06-10: "solo si alguien aprieta cargar cuentan".
                flipped = db.execute(
                    """
                    UPDATE scintela.factura
                       SET usuario_crea = 'asinfo-carga'
                     WHERE usuario_crea = 'asinfo-backfill'
                       AND (numf = %s OR (%s <> '' AND numf_completo = %s))
                    """,
                    (numf, numero, numero),
                )
                if flipped:
                    raise _CargaAsinfoSkip(
                        f"estaba como backfill bajo '{codigo_cli}' — ahora "
                        f"CARGADA (cuenta en cartera), no se duplicó"
                    )
                raise _CargaAsinfoSkip(
                    f"ya está cargada en PC bajo '{codigo_cli}' — no se duplicó"
                )
            if len(duenos_mismo_importe) == 1:
                dueno = duenos_mismo_importe[0]
                # Mismo numf + mismo importe + un solo dueno => mismo
                # cliente con otro codigo. Mapear: alias automatico.
                try:
                    _aliases.agregar(
                        codigo_cli, dueno,
                        nota=f"auto: factura {numero or numf} ya cargada bajo {dueno}",
                        usuario=(usuario or "asinfo"),
                    )
                except Exception:
                    pass  # alias ya existia o fallo: el skip aplica igual
                raise _CargaAsinfoSkip(
                    f"ya está cargada bajo '{dueno}' — alias "
                    f"{codigo_cli}→{dueno} registrado, no se duplicó"
                )
            otros = sorted({
                (r.get("codigo_cli") or "").strip().upper() for r in rows
            } - {"", codigo_cli, cli_pc})
            if otros:
                raise _CargaAsinfoSkip(
                    f"numf {numf} ya existe en PC bajo {', '.join(otros)} "
                    f"(importe distinto) — revisá si {codigo_cli} es alias "
                    "antes de cargar"
                )

    nuevo = (cli_pc or codigo_cli)[:5]
    # OJO: scintela.cliente en PROD no tiene UNIQUE(codigo_cli) (el fixture
    # de tests si — drift), asi que ON CONFLICT explota con "no unique or
    # exclusion constraint". WHERE NOT EXISTS logra el mismo no-duplicar
    # sin depender del constraint. (probado en prod 2026-06-10)
    # TMT 2026-06-10: traer nombre/RUC de Asinfo al crear (la razón social
    # SIEMPRE está en el ERP — factura electrónica SRI). Fail-soft: si
    # Metabase no responde, el cliente nace sin ficha y la completa después
    # /admin/clientes-ficha-asinfo o el próximo CLIENTES.DBF.
    nombre = ruc = None
    try:
        from modules.asinfo import service as _asinfo_svc
        ficha = _asinfo_svc.cliente_ficha([codigo_cli, nuevo]) or {}
        f = ficha.get(codigo_cli) or ficha.get(nuevo) or {}
        nombre = (f.get("nombre") or "").strip() or None
        ruc = (f.get("ruc") or "").strip() or None
    except Exception:  # noqa: BLE001
        pass
    db.execute(
        """
        INSERT INTO scintela.cliente (codigo_cli, nombre, ruc, usuario_crea)
        SELECT %s, %s, %s, %s
         WHERE NOT EXISTS (
               SELECT 1 FROM scintela.cliente WHERE codigo_cli = %s
         )
        """,
        (nuevo, nombre, ruc, (usuario or "asinfo")[:50], nuevo),
    )
    return nuevo, True




def _numf_de_numero(numero: str | None) -> int | None:
    """Extrae el numero de factura (sufijo entero) de un numf_completo de
    Asinfo (ej '001-099-000177335' -> 177335). Convencion del proyecto:
    numf = sufijo numerico de numf_completo. Asi la copia asinfo y la del
    DBF quedan con el MISMO numero y el sync las dedupea. Sin esto, crear()
    autoasigna numf=MAX+1 e infla cartera con duplicados (TMT 2026-06-15).
    """
    if not numero:
        return None
    import re as _re_local
    m = _re_local.findall(r"\d+", str(numero))
    if not m:
        return None
    try:
        return int(m[-1])
    except (ValueError, TypeError):
        return None


def _flip_backfill_si_existe(numf_completo: str) -> str | None:
    """Si la factura ya esta en PC por numf_completo: 'flip' si era
    asinfo-backfill y la convirtio a asinfo-carga (ahora CUENTA — decision
    duena 2026-06-10: apretar Cargar sobre una oculta la activa), 'ya' si
    ya estaba cargada/contando. None si no existe.

    TMT 2026-06-11: el flip del guard de _resolver_cliente_asinfo casi
    nunca corria (el resolver retorna apenas el cliente existe) → el bulk
    tiro 296 'duplicate key uq_factura_numf_completo'. El chequeo va aca,
    ANTES del INSERT."""
    if not numf_completo:
        return None
    row = db.fetch_one(
        "SELECT id_factura, COALESCE(usuario_crea,'') AS uc "
        "FROM scintela.factura WHERE numf_completo = %s LIMIT 1",
        (numf_completo,),
    )
    if not row:
        return None
    if row["uc"] == "asinfo-backfill":
        db.execute(
            "UPDATE scintela.factura SET usuario_crea = 'asinfo-carga' "
            "WHERE id_factura = %s",
            (row["id_factura"],),
        )
        return "flip"
    return "ya"

@facturas_bp.route("/facturas/cargar-desde-asinfo-bulk", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def cargar_desde_asinfo_bulk():
    """Carga N facturas de Asinfo en bloque (JSON en hidden field).

    Espera form-encoded:
        rows_json: JSON con [{fecha, numero, tipo, codigo_cli, kg, usd}, ...]
    """
    import json as _json
    raw = request.form.get("rows_json") or "[]"
    try:
        rows = _json.loads(raw)
    except Exception:
        rows = []
    if not rows:
        flash("No seleccionaste ninguna factura.", "warn")
        return redirect(url_for("facturas.desde_asinfo"))

    ok, errs = 0, []
    flipped = 0
    clientes_creados: list[str] = []
    usuario = (
        getattr(g, "user", {}).get("username") if hasattr(g, "user") and isinstance(g.user, dict)
        else "asinfo"
    )
    for r in rows:
        try:
            fecha = _parse_date(r.get("fecha") or "")
            codigo_cli = (r.get("codigo_cli") or "").strip().upper()
            kg = Decimal(str(r.get("kg") or "0"))
            importe = Decimal(str(r.get("usd") or "0"))
            numf_completo = (r.get("numero") or "").strip()
            tipo_asinfo = (r.get("tipo") or "FACTURA").upper()
            if not fecha or not codigo_cli or importe == 0:
                errs.append(f"{numf_completo}: faltan datos")
                continue
            # Resolver alias Asinfo->PC; si el cliente no existe en PC
            # (alta nueva en dBase aun no importada), se auto-crea minimo.
            estado = _flip_backfill_si_existe(numf_completo)
            if estado == "flip":
                flipped += 1
                ok += 1
                continue
            if estado == "ya":
                errs.append(f"{numf_completo}: ya estaba cargada")
                continue
            cli_uso, creado = _resolver_cliente_asinfo(
                codigo_cli, usuario, numero=numf_completo, importe=importe,
            )
            if creado:
                clientes_creados.append(cli_uso)
            queries.crear(
                fecha=fecha,
                codigo_cli=cli_uso,
                kg=kg,
                importe=importe,
                numf=_numf_de_numero(numf_completo),  # numero REAL, no MAX+1
                numf_completo=numf_completo or None,
                tipo=tipo_asinfo[:2],
                usuario='asinfo-carga',  # botón Cargar = a propósito → CUENTA (mig 0087)
            )
            ok += 1
        except Exception as e:
            errs.append(f"{r.get('numero','?')}: {e}")

    if ok:
        msg = f"Cargadas {ok} facturas desde Asinfo."
        if flipped:
            msg += f" ({flipped} estaban ocultas como backfill — ahora CUENTAN)"
        flash(msg, "ok")
    if clientes_creados:
        flash(
            "Clientes creados automáticamente con ficha mínima: "
            + ", ".join(sorted(set(clientes_creados)))
            + ". Corré /admin/clientes-import para traer la ficha del dBase.",
            "info",
        )
    if errs:
        msg = f"{len(errs)} con error: " + "; ".join(errs[:5])
        if len(errs) > 5:
            msg += f"; (+{len(errs)-5} más)"
        flash(msg, "warn")
    return redirect(url_for("facturas.desde_asinfo"))


@facturas_bp.route("/facturas/cargar-desde-asinfo", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def cargar_desde_asinfo():
    """Crea una factura en PC con los datos provenientes de Asinfo."""
    try:
        fecha = _parse_date(request.form.get("fecha") or "")
        codigo_cli = (request.form.get("codigo_cli") or "").strip().upper()
        kg = Decimal(str(request.form.get("kg") or "0"))
        importe = Decimal(str(request.form.get("usd") or "0"))
        numf_completo = (request.form.get("numero") or "").strip()
        tipo_asinfo = (request.form.get("tipo") or "FACTURA").upper()
        if not fecha or not codigo_cli or importe == 0:
            flash("Faltan datos (fecha/cliente/importe).", "warn")
            return redirect(url_for("facturas.desde_asinfo"))
        # Resolver alias Asinfo->PC; auto-crear si falta (alta nueva en
        # dBase que todavia no paso por /admin/clientes-import).
        usuario = (
            getattr(g, "user", {}).get("username")
            if hasattr(g, "user") and isinstance(g.user, dict) else "asinfo"
        )
        estado = _flip_backfill_si_existe(numf_completo)
        if estado == "flip":
            flash(f"Factura {numf_completo} estaba como backfill — ahora CUENTA en cartera.", "ok")
            return redirect(url_for("facturas.desde_asinfo"))
        if estado == "ya":
            flash(f"Factura {numf_completo} ya estaba cargada.", "warn")
            return redirect(url_for("facturas.desde_asinfo"))
        cli_uso, creado = _resolver_cliente_asinfo(
            codigo_cli, usuario, numero=numf_completo, importe=importe,
        )
        if creado:
            flash(
                f"Cliente '{cli_uso}' no existía en PC — se creó automáticamente "
                "con ficha mínima. Corré /admin/clientes-import para traer la "
                "ficha del dBase.",
                "info",
            )
        res = queries.crear(
            fecha=fecha,
            codigo_cli=cli_uso,
            kg=kg,
            importe=importe,
            numf=_numf_de_numero(numf_completo),  # numero REAL, no MAX+1
            numf_completo=numf_completo or None,
            tipo=tipo_asinfo[:2],  # 'FA', 'NT'
            usuario='asinfo-carga',  # botón Cargar = a propósito → CUENTA (mig 0087)
        )
        flash(f"Factura {numf_completo or '#'+str(res.get('numf'))} cargada desde Asinfo.", "ok")
    except _CargaAsinfoSkip as e:
        flash(f"No se cargó: {e}", "warn")
    except Exception as e:
        flash_exc("No se pudo cargar la factura", e)
    return redirect(url_for("facturas.desde_asinfo"))


@facturas_bp.route("/facturas")
@requiere_login
@requiere_permiso("facturas.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    cliente = request.args.get("cliente", "").strip()
    def _parse_num(s: str | None) -> float | None:
        # TMT 2026-06-23: usar el parser de montos compartido (EU/EC: punto miles,
        # coma decimal) en vez de replace(",","."), que rompía "1.000" (→1,0).
        from parsers import parse_monto
        m = parse_monto(s)
        return float(m) if m is not None else None
    monto_min = _parse_num(request.args.get("monto_min"))
    monto_max = _parse_num(request.args.get("monto_max"))
    solo_abiertas = request.args.get("abiertas") == "1"
    # TMT 2026-05-22 — modo auditoría: muestra solo las facturas que
    # PC tiene pero Asinfo NO matchea (excluyendo legacy y NC kg=0).
    # Sirve para encontrar discrepancias reales entre los sistemas.
    solo_huerfanas = request.args.get("solo_huerfanas") == "1"
    # TMT 2026-05-22 — filtro por tipo Asinfo (F=FACTURA, D=DEVOLUCION,
    # N=NTEN, NC=NC_FINANCIERA, NCNT). Vacío = todos.
    tipo_ai_filtro = (request.args.get("tipo_ai") or "").strip().upper()
    # Vista canónica:
    #  - cartera (Z+A vivas)
    #  - estado (= antes "todas"; muestra todo el universo, filtrable por ?estado=)
    #  - canceladas (T)
    #  - eliminadas (X)  -- 'Y' borrado 2026-05-19, nunca existió en la base.
    # TMT 2026-05-19 (pedido dueña): default ahora es 'cartera' (no 'todas').
    # 'todas' renombrado a 'estado' con un filtro dropdown adentro.
    vista = (request.args.get("vista") or "cartera").lower()
    # Back-compat: si todavía llega ?vista=todas, lo mapeamos a 'estado'.
    if vista == "todas":
        vista = "estado"
    if vista not in ("cartera", "estado", "canceladas", "eliminadas"):
        vista = "cartera"
    # Filtro de estado (solo aplica en vista='estado'). Acepta los stats
    # canónicos: Z (cartera), A (parcial), T (cancelada), X (eliminada).
    # TMT 2026-05-19 v8 — multi-checkbox. Stat 'Y' fue retirado (la dueña:
    # "factura Y no existe").
    estados_raw = request.args.getlist("estado")
    estados_filtro = [
        s.upper().strip() for s in estados_raw
        if s and s.upper().strip() in ("Z", "A", "T", "X", "N")
    ]
    # De-dup preservando orden — útil si el form reenvía duplicados.
    seen: set[str] = set()
    estados_filtro = [s for s in estados_filtro if not (s in seen or seen.add(s))]
    # Compat con el flag scalar viejo (templates / código externo que
    # consume `estado`).
    estado_filtro = estados_filtro[0] if len(estados_filtro) == 1 else ""
    # TMT 2026-05-22 — paginación server-side. Default 500/página para que
    # el render sea rápido. ?por_pagina=N (max 5000) y ?page=N. Con paginación
    # ACUM solo refleja la página visible — el header sigue mostrando el total
    # del UNIVERSO filtrado, calculado por contar_filtrado().
    # Casos especiales: si export=csv, traemos TODO (sin paginar) para que
    # el CSV sea completo; si solo_huerfanas, también traemos todo porque el
    # filtro post-enriquecimiento corta filas y la paginación SQL no aplica.
    is_export = request.args.get("export") == "csv"
    try:
        por_pagina = max(1, min(5000, int(request.args.get("por_pagina") or 500)))
    except (TypeError, ValueError):
        por_pagina = 500
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    # Si exporta CSV o pidió huérfanas → sin paginar (vamos por TODO).
    if is_export or solo_huerfanas:
        limite_efectivo = 100000
        offset_efectivo = 0
    else:
        limite_efectivo = por_pagina
        offset_efectivo = (page - 1) * por_pagina
    try:
        filas = queries.buscar(
            q, desde, hasta, solo_abiertas,
            vista=vista, limite=limite_efectivo, offset=offset_efectivo,
            cliente=cliente, monto_min=monto_min, monto_max=monto_max,
            estado=estado_filtro,
            estados=estados_filtro,
        )
        conteos = queries.conteos_por_vista()
        # Total del universo filtrado (sin LIMIT/OFFSET) — para el contador.
        if is_export or solo_huerfanas:
            # No tiene sentido el conteo SQL: con solo_huerfanas el conteo
            # real lo da el filtro post-enriquecimiento.
            total_filtrado = {"n": len(filas), "total_importe": 0.0, "total_saldo": 0.0}
        else:
            total_filtrado = queries.contar_filtrado(
                q, desde, hasta, solo_abiertas,
                vista=vista, cliente=cliente,
                monto_min=monto_min, monto_max=monto_max,
                estado=estado_filtro, estados=estados_filtro,
                )
        error = None
    except Exception as e:
        filas, conteos, total_filtrado, error = [], {}, {"n": 0}, str(e)

    # ===== Enriquecimiento con Asinfo (read-only, fail-soft) =====
    # Para cada factura de PC, intentamos matchear contra la card 199 de
    # Asinfo via numf_completo == numero. Agregamos asinfo_kg/asinfo_usd
    # y los deltas vs PC. Si no hay match → None (la columna queda "—").
    #
    # Performance: solo enriquecemos si <= 6000 filas Y hay fechas válidas
    # en el rango. La cartera viva ronda las 4500, así que con 6000 cubrimos
    # comodamente. Para vistas históricas más amplias saltamos — pero igual
    # inicializamos las 4 claves a None para que el template no rompa.
    _asinfo_intentado = False
    from datetime import date as _date_inic
    _ASINFO_CUTOFF = _date_inic(2025, 1, 1)
    for f in filas:
        f["asinfo_kg"] = None
        f["asinfo_usd"] = None
        f["asinfo_diff_kg"] = None
        f["asinfo_diff_usd"] = None
        f["asinfo_tipo"] = None  # FACTURA / DEVOLUCION cuando matchea
        # TMT 2026-05-22 — flag para PC kg<0 que matchea con FACTURA/NTEN
        # positiva en Asinfo (mismo |kg| y |usd|). Indica bug de carga: PC
        # cargó como devolución algo que en Asinfo es factura.
        f["asinfo_signo_invertido"] = False
        # True si la factura es de ANTES de cuando arrancó Asinfo "limpio"
        # → sabemos que no va a tener match, no es un error.
        f["asinfo_pre_cutoff"] = bool(f.get("fecha") and f["fecha"] < _ASINFO_CUTOFF)
    # Skipping Asinfo en vistas históricas (estado/canceladas/eliminadas)
    # — no aporta info para legacy. Cartera SÍ llama Asinfo para mostrar
    # las columnas KG AI / USD AI (la dueña las usa para detectar drift
    # entre PC y Asinfo). La cache TTL 5min hace que la primera llamada
    # del día sea lenta y las siguientes instantáneas.
    # Triggers para SALTEAR Asinfo:
    #   - vista != cartera (histórico, legacy)
    # Triggers para FORZAR aunque vista sea histórica:
    #   - ?asinfo=1, ?solo_huerfanas=1, export=csv
    _necesita_asinfo = (
        vista == 'cartera'
        or solo_huerfanas
        or request.args.get("asinfo") == "1"
        or is_export
    )
    if 0 < len(filas) <= 6000 and _necesita_asinfo:
        from datetime import date as _date

        # Asinfo solo tiene data limpia desde 2025-01-01. Recortamos el rango
        # mínimo para no pedirle 5 años de facturas históricas que sabemos
        # que NO van a matchear (cartera legacy 2021-2024). Esto pasa de
        # ~50k filas a ~5k y la query baja de 20s a 2-3s.
        ASINFO_DESDE_EFECTIVO = _date(2025, 1, 1)
        fechas = [f["fecha"] for f in filas if f.get("fecha")]
        # Solo pedimos a Asinfo si hay AL MENOS una factura con fecha >= 2025.
        fechas_2025_plus = [f for f in fechas if f >= ASINFO_DESDE_EFECTIVO]
        if fechas_2025_plus:
            try:
                from modules.asinfo import service as asinfo_service

                _asinfo_intentado = True
                mn = max(min(fechas_2025_plus), ASINFO_DESDE_EFECTIVO)
                mx = max(fechas_2025_plus)
                asinfo_rows = asinfo_service.facturas_periodo(mn, mx)
                # TMT 2026-05-22 — extendido: muchos clientes (BED, EDU, BAN…)
                # facturan via NTEN (nota de entrega) en lugar de FACTURA común.
                # Hasta ahora el matcher solo veía FACTURA/DEVOLUCION y dejaba
                # cientos de facturas PC con kg>0 sin match.
                #   - FACTURA + NTEN + NC_FINANCIERA  → contra PC kg > 0
                #     (NTEN tiene kg positivos como FACTURA. NC_FINANCIERA va
                #     acá también para que kg=0 pueda matchearlas si tienen
                #     número completo coincidente.)
                #   - DEVOLUCION + NCNT              → contra PC kg < 0
                #
                # Indexamos por DOS claves dentro de cada universo:
                #   1) `numero` completo ("001-099-000010588" o "NTEN-10309") → match directo
                #   2) sufijo numérico (int 10588 / 10309) → contra el numf chico de PC
                idx_factura_completo: dict[str, dict] = {}
                idx_factura_numf: dict[int, dict] = {}
                idx_devolucion_completo: dict[str, dict] = {}
                idx_devolucion_numf: dict[int, dict] = {}
                _TIPOS_POSITIVOS = ("FACTURA", "NTEN", "NC_FINANCIERA")
                _TIPOS_NEGATIVOS = ("DEVOLUCION", "NCNT")
                for r in asinfo_rows:
                    tipo = r.get("tipo")
                    numero = r.get("numero")
                    if not numero:
                        continue
                    if tipo in _TIPOS_POSITIVOS:
                        c_idx, n_idx = idx_factura_completo, idx_factura_numf
                    elif tipo in _TIPOS_NEGATIVOS:
                        c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
                    else:
                        continue
                    # No pisar si ya hay match con FACTURA (más confiable que NTEN).
                    if numero not in c_idx:
                        c_idx[numero] = r
                    sufijo = numero.split("-")[-1] if "-" in numero else numero
                    try:
                        sufijo_int = int(sufijo)
                        if sufijo_int not in n_idx:
                            n_idx[sufijo_int] = r
                    except (ValueError, TypeError):
                        pass
                # TMT 2026-05-22 — índice por (cliente, fecha, kg redondeado).
                # Muchas filas PC tienen numf=0 (sin número Asinfo cargado) y
                # el match por número no funciona. Pero los importes USD coinciden
                # exactamente con la card 199 (que ya viene sin IVA). Hacemos
                # un índice compuesto para el fallback heurístico.
                from collections import defaultdict as _dd
                idx_compuesto: dict[tuple, list[dict]] = _dd(list)
                # TMT 2026-05-22 — índice ABS (sin signo). Para detectar
                # misregistros: PC cargada como devolución (kg<0) cuando en
                # Asinfo es FACTURA con kg>0 (mismo |kg|, mismo |usd|).
                idx_compuesto_abs: dict[tuple, list[dict]] = _dd(list)
                for r in asinfo_rows:
                    tipo = r.get("tipo")
                    if tipo not in (_TIPOS_POSITIVOS + _TIPOS_NEGATIVOS):
                        continue
                    cli = (r.get("cliente_codigo") or "").strip().upper()
                    fecha_ai = r.get("fecha")
                    kg_ai = float(r.get("kg") or 0)
                    if not (cli and fecha_ai):
                        continue
                    # Redondeamos kg a 2 decimales para tolerar drift mínimo de
                    # formato. usd queda en la fila para validación posterior.
                    key = (cli, str(fecha_ai)[:10], round(kg_ai, 2))
                    idx_compuesto[key].append(r)
                    key_abs = (cli, str(fecha_ai)[:10], round(abs(kg_ai), 2))
                    idx_compuesto_abs[key_abs].append(r)

                # Mergear: elegir índice según signo del kg de PC.
                #   kg > 0  → buscar en FACTURA+NTEN+NC_FINANCIERA
                #   kg < 0  → buscar en DEVOLUCION+NCNT
                #   kg == 0 → no matchear (NC financiera, ajustes)
                for f in filas:
                    pc_kg = float(f.get("kg") or 0)
                    # TMT 2026-05-26 — facturas MARCADAS (#DUP, #SIN_ASINFO, etc.)
                    # se excluyen de match Asinfo: representan filas explícitamente
                    # marcadas por humano/script como "no requiere match" o "dup
                    # conocido". El prefijo '#' nunca aparece en numeros Asinfo
                    # reales (que son '001-099-...' / 'NTEN-...' / 'NCNT-...').
                    if (f.get("numf_completo") or "").startswith("#"):
                        f["asinfo_marcada"] = f["numf_completo"]
                        f["asinfo_tipo"] = "MARCADA"
                        continue
                    # TMT 2026-05-22 — antes kg=0 se saltaba. Ahora también
                    # intentamos matchear NC financieras (kg=0, importe negativo)
                    # por el universo "positivo" (que ya incluye NC_FINANCIERA).
                    if pc_kg > 0:
                        c_idx, n_idx = idx_factura_completo, idx_factura_numf
                    elif pc_kg < 0:
                        c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
                    else:
                        # kg=0 → intentar contra ambos índices, prefiriendo el negativo
                        # si el importe PC es negativo.
                        pc_imp_signo = float(f.get("importe") or 0)
                        if pc_imp_signo < 0:
                            c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
                        else:
                            c_idx, n_idx = idx_factura_completo, idx_factura_numf
                    r_ai = None
                    numero = (f.get("numf_completo") or "").strip()
                    if numero:
                        r_ai = c_idx.get(numero)
                    if r_ai is None and f.get("numf"):
                        try:
                            r_ai = n_idx.get(int(f["numf"]))
                        except (ValueError, TypeError):
                            pass
                    # TMT 2026-05-22 — Fallback heurístico para PC sin numf:
                    # match por (codigo_cli + fecha + kg exacto) y validar
                    # que los importes coincidan.
                    #
                    # TMT 2026-05-29 dueña: 'asinfo siempre tiene numero, PC no'.
                    # Ampliado con 3 estrategias en cascada:
                    #   (a) USD exacto: |pc - ai| < 0.5  (PC sin IVA = card 199)
                    #   (b) USD con IVA 12%: |pc - ai * 1.12| < 0.5
                    #   (c) Tolerancia 15% sobre el importe: cubre IVA + redondeos
                    # Si UNA sola candidata cuadra en CUALQUIERA de las 3, match.
                    if r_ai is None:
                        cli_pc = (f.get("codigo_cli") or "").strip().upper()
                        fecha_pc = f.get("fecha")
                        if cli_pc and fecha_pc:
                            key = (cli_pc, str(fecha_pc)[:10], round(pc_kg, 2))
                            candidatos = idx_compuesto.get(key, [])
                            pc_imp = float(f.get("importe") or 0)

                            def _coincide_usd(ai_usd: float) -> bool:
                                # Estrategia (a) USD exacto.
                                if abs(ai_usd - pc_imp) < 0.5:
                                    return True
                                # Estrategia (b) PC trae IVA 12%, Asinfo no.
                                if abs(pc_imp - ai_usd * 1.12) < 0.5:
                                    return True
                                # Estrategia (c) tolerancia 15% (cubre IVA 12-14%
                                # + redondeos + ajustes chicos). Solo si el monto
                                # no es trivial (>= $1) para evitar matchear
                                # comisiones de centavos al voleo.
                                base = max(abs(ai_usd), abs(pc_imp), 1.0)
                                if base >= 1.0 and abs(ai_usd - pc_imp) / base < 0.15:
                                    return True
                                return False

                            ok = [c for c in candidatos
                                  if _coincide_usd(float(c.get("usd") or 0))]
                            if len(ok) == 1:
                                r_ai = ok[0]
                            elif len(ok) > 1:
                                # TMT 2026-05-28 — dueña: muchas operaciones tienen
                                # FACTURA + NTEN simultáneas con mismo cli/kg/usd
                                # (la NTEN es nota de entrega, la FACTURA es el
                                # comprobante fiscal). Preferimos FACTURA > NTEN.
                                facts = [c for c in ok if c.get("tipo") == "FACTURA"]
                                ntens = [c for c in ok if c.get("tipo") == "NTEN"]
                                if len(facts) == 1:
                                    r_ai = facts[0]
                                elif len(ntens) == 1:
                                    r_ai = ntens[0]
                                # Si hay >1 FACTURA o >1 NTEN, sigue siendo ambiguo
                                # (dejar huérfana — requiere análisis manual).
                    # TMT 2026-05-22 — Detección de signo invertido. Cuando PC
                    # tiene kg<0 (cargada como devolución) y Asinfo tiene una
                    # FACTURA/NTEN positiva con mismo |kg| y mismo |usd|,
                    # asumimos que PC se cargó con signo invertido por error.
                    # Match con tolerancia más amplia porque el USD de PC
                    # podría tener IVA (la card 199 lo trae sin IVA).
                    signo_invertido = False
                    if r_ai is None and pc_kg < 0:
                        cli_pc = (f.get("codigo_cli") or "").strip().upper()
                        fecha_pc = f.get("fecha")
                        if cli_pc and fecha_pc:
                            key_abs = (cli_pc, str(fecha_pc)[:10], round(abs(pc_kg), 2))
                            candidatos = idx_compuesto_abs.get(key_abs, [])
                            pc_imp_abs = abs(float(f.get("importe") or 0))
                            # Tolerancia 15% para absorber IVA (USA: típico 12-14%
                            # ya neteado o no). Filtramos a tipos POSITIVOS y
                            # validamos que el kg sea positivo en Asinfo.
                            ok = []
                            for c in candidatos:
                                if c.get("tipo") not in _TIPOS_POSITIVOS:
                                    continue
                                if float(c.get("kg") or 0) <= 0:
                                    continue
                                ai_usd_abs = abs(float(c.get("usd") or 0))
                                # Aceptamos si los USD coinciden ± 15% (IVA tolerancia)
                                # PERO no más de $5 absoluto en cifras chicas.
                                margen = max(pc_imp_abs * 0.15, 5.0)
                                if abs(ai_usd_abs - pc_imp_abs) <= margen:
                                    ok.append(c)
                            if len(ok) == 1:
                                r_ai = ok[0]
                                signo_invertido = True
                    if r_ai is not None:
                        f["asinfo_kg"] = float(r_ai.get("kg") or 0)
                        f["asinfo_usd"] = float(r_ai.get("usd") or 0)
                        f["asinfo_diff_kg"] = round(f["asinfo_kg"] - pc_kg, 3)
                        f["asinfo_diff_usd"] = round(f["asinfo_usd"] - float(f.get("importe") or 0), 2)
                        f["asinfo_tipo"] = r_ai.get("tipo")
                        f["asinfo_signo_invertido"] = signo_invertido
            except Exception as _e:
                # Cualquier falla del bridge no debe romper la lista de facturas.
                _LOG_ENRICH = __import__("logging").getLogger("programa_core.facturas")
                _LOG_ENRICH.warning("Enriquecimiento Asinfo falló: %s", _e)

    # TMT 2026-05-22 — filtro de auditoría: solo mostrar facturas que PC tiene
    # pero Asinfo NO matcheó, excluyendo legacy (<2025-01-01) y NC kg=0.
    # Esas son las "huérfanas" reales que ameritan investigar.
    if solo_huerfanas and _asinfo_intentado:
        filas = [
            f for f in filas
            if f.get("asinfo_tipo") is None
            and not f.get("asinfo_pre_cutoff")
            and float(f.get("kg") or 0) != 0
            and not (f.get("numf_completo") or "").startswith("#")
        ]

    # TMT 2026-05-22 — filtro por tipo Asinfo (post-enriquecimiento).
    if tipo_ai_filtro:
        _MAP_TIPO = {
            "F": "FACTURA",
            "D": "DEVOLUCION",
            "N": "NTEN",
            "NC": "NC_FINANCIERA",
            "NCNT": "NCNT",
        }
        tipo_buscado = _MAP_TIPO.get(tipo_ai_filtro, tipo_ai_filtro)
        filas = [f for f in filas if f.get("asinfo_tipo") == tipo_buscado]

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("numf", "N° Factura"),
                ("fecha", "Fecha"),
                ("codigo_cli", "Cliente"),
                ("cliente", "Nombre"),
                ("kg", "Kg"),
                ("importe", "Importe"),
                ("abono", "Abono"),
                ("saldo", "Saldo"),
                ("stat", "Stat"),
            ],
            filename=f"facturas_{vista}.csv",
        )

    total_importe = sum(float(r["importe"] or 0) for r in filas)
    total_saldo   = sum(float(r["saldo"]   or 0) for r in filas)
    return render_template(
        "facturas/lista.html",
        filas=filas, q=q, desde=desde, hasta=hasta,
        cliente=cliente, monto_min=monto_min, monto_max=monto_max,
        solo_abiertas=solo_abiertas,
        vista=vista, conteos=conteos,
        estado=estado_filtro,
        estados=estados_filtro,
        total_importe=total_importe, total_saldo=total_saldo,
        error=error,
        asinfo_intentado=_asinfo_intentado,
        solo_huerfanas=solo_huerfanas,
        tipo_ai_filtro=tipo_ai_filtro,
        # TMT 2026-05-22 — paginación
        page=page,
        por_pagina=por_pagina,
        total_filtrado_n=total_filtrado.get("n", 0),
        total_filtrado_importe=total_filtrado.get("total_importe", 0.0),
        total_filtrado_saldo=total_filtrado.get("total_saldo", 0.0),
        paginado=not (is_export or solo_huerfanas),
    )


# =====================================================================
# Backfill Asinfo — TMT 2026-05-22
# =====================================================================
# Reemplaza al script SSM standalone: corre adentro de la app Flask,
# donde las env vars Metabase ya están cargadas y el matcher es el
# mismo que usa /facturas (sin duplicación). Sólo accionistas.
#
# Política de auto-asociación:
#   - score < 0.15 AND mismo cliente → asociar (escribe numf + numf_completo)
#   - sin candidatos AND stat='T' AND saldo=0 → dejar como huérfana cobrada
#     (cobranza vieja, no vale la pena)
#   - sin candidatos AND saldo!=0 → flag para revisión manual (NO marca N
#     automáticamente porque cambiaría la cartera)
#   - score >= 0.15 → dejar para revisión manual

_BACKFILL_SCORE_MAX = 0.15


@facturas_bp.route("/facturas/backfill-asinfo", methods=["GET"])
@requiere_login
@requiere_permiso("facturas.editar")
def backfill_asinfo():
    """Audit + backfill automático de huérfanas Asinfo.

    GET sin args      → preview (no toca DB).
    GET ?apply=1      → aplica los matches que cumplen el threshold.

    Devuelve JSON con conteos + detalle. Pensado para correr desde Chrome
    (un click). No usa POST para evitar tener que cargar CSRF token desde
    una herramienta admin one-shot.
    """
    from modules.facturas import audit_asinfo

    apply = request.args.get("apply") == "1"
    huerfanas = audit_asinfo.auditar_huerfanas(top_k=3, limite=1000)

    aplicados: list[dict] = []
    saltadas_sin_candidatos: list[dict] = []
    saltadas_score_alto: list[dict] = []
    errores_apply: list[dict] = []

    for item in huerfanas:
        pc = item["pc_factura"]
        cands = item["candidatos"]
        mejor_score = item["mejor_score"]
        cli_pc = (pc.get("codigo_cli") or "").strip().upper()

        if not cands:
            saltadas_sin_candidatos.append({
                "id_factura": pc["id_factura"],
                "fecha": pc.get("fecha"),
                "codigo_cli": pc.get("codigo_cli"),
                "cliente": pc.get("cliente"),
                "kg": float(pc.get("kg") or 0),
                "importe": float(pc.get("importe") or 0),
                "saldo": float(pc.get("saldo") or 0),
                "stat": pc.get("stat"),
            })
            continue

        mejor = cands[0]
        cli_ai = (mejor.get("ai_cliente_codigo") or "").strip().upper()
        score = mejor["score"]

        # Política: score bajo + mismo cliente → asociar.
        if score < _BACKFILL_SCORE_MAX and cli_pc == cli_ai:
            if apply:
                try:
                    audit_asinfo.asociar(
                        pc["id_factura"],
                        mejor["ai_numero"],
                        usuario="web",
                    )
                except Exception as _e:
                    errores_apply.append({
                        "id_factura": pc["id_factura"],
                        "ai_numero": mejor.get("ai_numero"),
                        "error": f"{type(_e).__name__}: {_e}",
                    })
                    # Si más del 5% falla, parar para no spamear.
                    if len(errores_apply) > max(20, int(0.05 * len(huerfanas))):
                        break
                    continue
            aplicados.append({
                "id_factura": pc["id_factura"],
                "fecha": pc.get("fecha"),
                "codigo_cli": pc.get("codigo_cli"),
                "kg_pc": float(pc.get("kg") or 0),
                "usd_pc": float(pc.get("importe") or 0),
                "ai_numero": mejor["ai_numero"],
                "ai_tipo": mejor["ai_tipo"],
                "score": score,
            })
        else:
            saltadas_score_alto.append({
                "id_factura": pc["id_factura"],
                "fecha": pc.get("fecha"),
                "codigo_cli": pc.get("codigo_cli"),
                "cliente": pc.get("cliente"),
                "kg": float(pc.get("kg") or 0),
                "importe": float(pc.get("importe") or 0),
                "saldo": float(pc.get("saldo") or 0),
                "mejor_ai_numero": mejor["ai_numero"],
                "mejor_ai_cli": mejor.get("ai_cliente_codigo"),
                "mejor_ai_kg": mejor.get("ai_kg"),
                "mejor_ai_usd": mejor.get("ai_usd"),
                "mejor_score": score,
            })

    if apply:
        # Invalidar cache Asinfo para que el próximo render de /facturas
        # muestre los matches recién aplicados.
        try:
            from modules.asinfo import service as asinfo_service
            asinfo_service.reset_facturas_cache()
        except Exception:
            pass

    return {
        "modo": "apply" if apply else "preview",
        "threshold_score": _BACKFILL_SCORE_MAX,
        "huerfanas_total": len(huerfanas),
        "aplicados_count": len(aplicados),
        "saltadas_score_alto_count": len(saltadas_score_alto),
        "saltadas_sin_candidatos_count": len(saltadas_sin_candidatos),
        "errores_apply_count": len(errores_apply),
        "aplicados": aplicados[:20] if apply else aplicados,
        "saltadas_score_alto": saltadas_score_alto,
        "saltadas_sin_candidatos": saltadas_sin_candidatos,
        "errores_apply": errores_apply[:20],
    }


# =====================================================================
# Carga masiva CSV — batch 13. Mismas columnas que crear() / ALTAS.PRG.
# =====================================================================

FACTURAS_CSV_COLS = [
    # (campo, header legible, required)
    ("fecha",          "Fecha",         True),
    ("codigo_cli",     "Código cliente", True),
    ("kg",             "Kg",            True),
    ("importe",        "Importe",       True),
    ("numf",           "N° factura",    False),
    ("vencimiento",    "Vencimiento",   False),
    ("numf_completo",  "N° completo",   False),
    ("tipo",           "Tipo",          False),
    ("condic",         "Condición",     False),
    ("clave",          "Clave",         False),
]


@facturas_bp.route("/facturas/cargar-csv", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def cargar_csv():
    """Subir CSV con múltiples facturas. Mismos campos que ALTAS.PRG.

    GET con ?plantilla=1 devuelve un CSV vacío con los headers.
    POST procesa el archivo y muestra el reporte per-fila.
    """
    from csv_upload import plantilla_csv, procesar_csv

    if request.args.get("plantilla") == "1":
        csv_str = plantilla_csv(FACTURAS_CSV_COLS)
        resp = _plain_csv_response(csv_str, "plantilla_facturas.csv")
        return resp

    if request.method == "POST":
        f = request.files.get("archivo")
        if not f or not f.filename:
            flash("Subí un archivo CSV.", "warn")
            return redirect(url_for("facturas.cargar_csv"))
        raw = f.read()
        from . import queries as q_facturas
        result = procesar_csv(
            raw, FACTURAS_CSV_COLS, q_facturas.crear,
            usuario=(g.user or {}).get("username", "web"),
        )
        tono = "ok" if result.error == 0 else "warn"
        flash(f"Procesadas {result.total} filas — {result.ok} ok, {result.error} con error.", tono)
        return render_template(
            "facturas/cargar_csv_resultado.html",
            result=result, cols=FACTURAS_CSV_COLS,
        )
    return render_template("facturas/cargar_csv.html", cols=FACTURAS_CSV_COLS)


def _plain_csv_response(csv_str: str, filename: str):
    from flask import Response
    resp = Response("\ufeff" + csv_str, mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# TMT 2026-05-27: snapshots daily de scintela.historia se crearon HOY
# con stock_terminado=0 (cuando el bug estaba activo). Esos snapshots
# tienen USUTI=-1.3M y rompen Utilidad Real porque historia_ultimo_snapshot
# lee la fila m\u00e1s reciente. Endpoint para borrarlos.
@facturas_bp.route("/facturas/admin/borrar-snapshots-historia-hoy", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def borrar_snapshots_historia_hoy():
    """Borra filas de scintela.historia con fecha=HOY (snapshots daily auto).
    Eso fuerza historia_ultimo_snapshot a leer el snapshot mensual previo
    (cierre 30/04/2026) que tiene los valores correctos pre-bug."""
    from flask import jsonify

    dry_run = (request.values.get("dry_run") or "").strip() in ("1", "true", "yes")

    # Buscar TODAS las filas de hoy (fecha::date) — cubre timestamps con hora.
    # Las "rotas" son las que tienen usuti NEGATIVA grande Y ustock <
    # 7M (que pasó al meterse stock_terminado=0). La buena (post-fix)
    # tiene usuti positivo. NO borramos la buena.
    rows_hoy = db.fetch_all(
        """
        SELECT id_historia, fecha, usuario_crea, usuti, ustock, patrimonio
          FROM scintela.historia
         WHERE fecha::date = CURRENT_DATE
         ORDER BY id_historia
        """
    )
    ids_a_borrar = [int(r["id_historia"]) for r in rows_hoy
                    if float(r.get("usuti") or 0) < 0]

    if dry_run:
        return jsonify({
            "ok": True,
            "dry_run": True,
            "filas_hoy_total": len(rows_hoy),
            "filas_a_borrar": len(ids_a_borrar),
            "ids_a_borrar": ids_a_borrar,
            "rows": [
                {"id": r["id_historia"], "fecha": str(r["fecha"]),
                 "usuario_crea": r.get("usuario_crea"),
                 "usuti": float(r.get("usuti") or 0),
                 "ustock": float(r.get("ustock") or 0),
                 "patrimonio": float(r.get("patrimonio") or 0),
                 "BORRAR": (int(r["id_historia"]) in ids_a_borrar)}
                for r in rows_hoy
            ],
        })

    if not ids_a_borrar:
        return jsonify({"ok": True, "filas_borradas": 0, "msg": "Ninguna fila con usuti<0 hoy"})

    n = db.execute(
        f"DELETE FROM scintela.historia WHERE id_historia IN ({','.join(str(i) for i in ids_a_borrar)})"
    )
    return jsonify({"ok": True, "filas_borradas": n, "ids_borrados": ids_a_borrar})


# ---------------------------------------------------------------------------
# Backfill bulk Asinfo \u2192 scintela.factura (corre dentro del web server)
# ---------------------------------------------------------------------------
# TMT 2026-05-26: el script standalone scripts/backfill_facturas_2025_asinfo.py
# no pod\u00eda pegarle a Metabase desde SSM porque el subprocess no hereda las env
# vars del web server (METABASE_URL, ASINFO_CARD_FACTURAS, etc viven en alg\u00fan
# .env de prod no replicable). El web server S\u00cd las tiene cargadas, as\u00ed que
# el backfill corre ac\u00e1. Importa la l\u00f3gica del script (TIPO_MAP, _extract_numf,
# _iter_month_chunks, MARKER) pero la ejecuci\u00f3n es dentro del request.
#
# Idempotente: matchea contra PC existentes por numf_completo y por
# (numf, codigo_cli, fecha). Si la factura ya est\u00e1, NO la inserta.
# Las nuevas las marca usuario_crea='asinfo-backfill' \u2014 el sync DBF las
# preserva (ver import_dbf.py delete_where).

@facturas_bp.route("/facturas/admin/backfill-asinfo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def backfill_asinfo_endpoint():
    """Backfill bulk de facturas Asinfo \u2192 scintela.factura.

    Params (query string o form):
        desde=YYYY-MM-DD    default 2025-01-01
        hasta=YYYY-MM-DD    default hoy
        dry_run=1           default 0 (insertar de verdad)

    Devuelve JSON con resumen. dry_run=1 NO inserta nada \u2014 solo reporta.
    """
    import re
    from calendar import monthrange
    from collections import Counter
    from datetime import date, timedelta

    from flask import jsonify

    from modules.asinfo import aliases as cli_aliases
    from modules.asinfo import service as asinfo_service

    MARKER = "asinfo-backfill"
    TIPO_MAP = {
        "FACTURA":       "F",
        "DEVOLUCION":    "D",
        "NC_FINANCIERA": "C",
        "NTEN":          "N",
        "NCNT":          "X",
    }

    def _extract_numf(s):
        if not s:
            return None
        m = re.findall(r"\d+", str(s))
        if not m:
            return None
        try:
            return int(m[-1])
        except (ValueError, TypeError):
            return None

    def _iter_month_chunks(d_from, d_to):
        cy, cm = d_from.year, d_from.month
        ey, em = d_to.year, d_to.month
        while (cy, cm) <= (ey, em):
            first = date(cy, cm, 1)
            last = date(cy, cm, monthrange(cy, cm)[1])
            yield max(first, d_from), min(last, d_to)
            cm += 1
            if cm > 12:
                cm = 1
                cy += 1

    # Parsing params
    desde_s = (request.values.get("desde") or "2025-01-01").strip()
    hasta_s = (request.values.get("hasta") or today_ec().isoformat()).strip()
    dry_run = (request.values.get("dry_run") or "").strip() in ("1", "true", "yes")
    try:
        desde = date.fromisoformat(desde_s)
        hasta = date.fromisoformat(hasta_s)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"Fecha inv\u00e1lida: {e}"}), 400

    # 1) Trae todo Asinfo en chunks de 1 mes
    chunks = list(_iter_month_chunks(desde, hasta))
    asinfo_rows = []
    chunk_log = []
    for i, (cd, ch) in enumerate(chunks, 1):
        try:
            cr = asinfo_service.facturas_periodo(cd, ch)
        except Exception as e:
            chunk_log.append({"chunk": f"{cd}..{ch}", "error": str(e)})
            continue
        chunk_log.append({"chunk": f"{cd}..{ch}", "rows": len(cr)})
        asinfo_rows.extend(cr)

    if not asinfo_rows:
        return jsonify({
            "ok": False, "error": "Asinfo no devolvi\u00f3 filas",
            "asinfo_disponible": asinfo_service.disponible(),
            "chunks": chunk_log,
        })

    # 2) Carga PC existentes (numf_completo + tripla numf/cli/fecha + max numf)
    pc_rows = db.fetch_all(
        """
        SELECT numf, codigo_cli, fecha,
               COALESCE(NULLIF(TRIM(numf_completo), ''), '') AS nfc
          FROM scintela.factura
         WHERE fecha BETWEEN %s AND %s
        """,
        (desde, hasta),
    )
    by_nfc, by_tripla = set(), set()
    max_numf = 0
    for r in pc_rows:
        nfc = (r.get("nfc") or "").strip().upper()
        if nfc:
            by_nfc.add(nfc)
        n = int(r.get("numf") or 0)
        cli = (r.get("codigo_cli") or "").strip().upper()
        f = r.get("fecha")
        if hasattr(f, "isoformat"):
            f = f.isoformat()
        if n and cli:
            by_tripla.add((n, cli, str(f)))
        if n > max_numf:
            max_numf = n
    row_max = db.fetch_one("SELECT COALESCE(MAX(numf), 0) AS m FROM scintela.factura")
    if row_max and int(row_max.get("m") or 0) > max_numf:
        max_numf = int(row_max["m"])
    siguiente_numf = max_numf + 1

    # 3) Decidir candidatas
    ya_estaban = saltadas_sin_cli = saltadas_sin_importe = 0
    candidatas = []
    por_tipo = Counter()
    for ai in asinfo_rows:
        numero = str(ai.get("numero") or "").strip()
        nfc_key = numero.upper()
        numf = _extract_numf(numero)
        cli_asinfo = (ai.get("cliente_codigo") or "").strip().upper()
        cli_pc = cli_aliases.to_pc(cli_asinfo) if cli_asinfo else ""
        f = ai.get("fecha")
        if hasattr(f, "isoformat"):
            f_iso = f.isoformat()
            fecha_obj = f
        else:
            f_iso = str(f)[:10]
            try:
                fecha_obj = date.fromisoformat(f_iso)
            except ValueError:
                continue

        if nfc_key and nfc_key in by_nfc:
            ya_estaban += 1
            continue
        if numf and cli_pc and (numf, cli_pc, f_iso) in by_tripla:
            ya_estaban += 1
            continue
        if not cli_pc:
            saltadas_sin_cli += 1
            continue

        importe = float(ai.get("usd") or 0)
        kg = float(ai.get("kg") or 0)
        if importe == 0 and kg == 0 and not numf:
            saltadas_sin_importe += 1
            continue

        tipo_asinfo = str(ai.get("tipo") or "FACTURA").upper()
        tipo_pc = TIPO_MAP.get(tipo_asinfo, "F")
        por_tipo[tipo_asinfo] += 1

        if not numf:
            numf = siguiente_numf
            siguiente_numf += 1

        # TMT 2026-05-26: codigo_cli es VARCHAR(5) en scintela.factura. Asinfo
        # a veces trae codigo_cliente con 6+ chars (RUC truncado raro, etc) y
        # rompe el chunk entero con "value too long for type character varying(5)".
        # Truncamos a 5 chars defensivamente. Si en el futuro se cambia la columna
        # a VARCHAR(10), sacar este [:5].
        cli_pc_trunc = cli_pc[:5]
        candidatas.append({
            "numf": numf, "fecha": fecha_obj, "codigo_cli": cli_pc_trunc,
            "kg": kg, "importe": importe, "abono": importe, "saldo": 0,
            "stat": "T", "condic": "CC", "tipo": tipo_pc,
            "vencimiento": fecha_obj + timedelta(days=30),
            "numf_completo": numero or None, "clave": None,
            "usuario_crea": MARKER,
        })

    # TMT 2026-06-03 audit fix: hasta hoy esta función armaba `candidatas`
    # y terminaba sin return — devolvía 500 y nunca insertaba nada. El
    # bloque INSERT (que vivía pegado al final del endpoint siguiente
    # como código muerto) se movió acá adentro.
    resumen = {
        "ok": True,
        "rango": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "asinfo_trajo": len(asinfo_rows),
        "chunks": chunk_log,
        "ya_estaban_en_pc": ya_estaban,
        "saltadas_sin_cli_mapeable": saltadas_sin_cli,
        "saltadas_sin_importe_ni_numf": saltadas_sin_importe,
        "candidatas_a_insertar": len(candidatas),
        "por_tipo_asinfo": dict(por_tipo),
        "dry_run": dry_run,
    }

    if dry_run:
        resumen["sample_candidatas"] = [
            {"fecha": str(c["fecha"]), "codigo_cli": c["codigo_cli"],
             "numf": c["numf"], "numf_completo": c["numf_completo"],
             "tipo": c["tipo"], "importe": c["importe"], "kg": c["kg"]}
            for c in candidatas[:10]
        ]
        return jsonify(resumen)

    # 4) INSERT bulk en chunks de 500
    insertadas = errores = 0
    sql = """
        INSERT INTO scintela.factura
            (numf, fecha, codigo_cli, kg, importe, abono, saldo,
             stat, condic, tipo, vencimiento, numf_completo, clave, usuario_crea)
        VALUES (%(numf)s, %(fecha)s, %(codigo_cli)s, %(kg)s, %(importe)s, %(abono)s, %(saldo)s,
                %(stat)s, %(condic)s, %(tipo)s, %(vencimiento)s, %(numf_completo)s, %(clave)s, %(usuario_crea)s)
    """
    CHUNK = 500
    err_log_bk = []
    for i in range(0, len(candidatas), CHUNK):
        ch = candidatas[i:i+CHUNK]
        try:
            with db.tx() as conn, conn.cursor() as cur:
                for c in ch:
                    cur.execute(sql, c)
                    insertadas += 1
        except Exception as e:
            errores += len(ch)
            err_log_bk.append({"chunk_start": i, "size": len(ch), "error": str(e)[:200]})

    resumen["insertadas"] = insertadas
    resumen["errores"] = errores
    if err_log_bk:
        resumen["errores_detalle"] = err_log_bk
    return jsonify(resumen)


# ---------------------------------------------------------------------------
# Auto-fix huérfanas: registrar aliases sugeridos + backfill numf_completo
# ---------------------------------------------------------------------------
# TMT 2026-05-26: las facturas DBF entran sin numf_completo. Un script
# (backfill_numf_completo_from_asinfo) las matchea contra Asinfo por (numf,
# codigo_cli) y popula numf_completo. PERO si Asinfo usa otro código de
# cliente (AJO vs AJ2), el match falla y la factura queda como "huérfana"
# en /facturas?solo_huerfanas=1.
#
# Este endpoint hace el ciclo completo automáticamente:
# 1) Detecta aliases sugeridos al 100% (mismo cli Asinfo + mismo numf → otro
#    cli PC) — misma lógica que /facturas/desde-asinfo.
# 2) Registra esos aliases.
# 3) Corre backfill_numf_completo para popular numf_completo en las facturas
#    PC que ahora matcheen con sus Asinfo equivalentes.
# 4) Devuelve JSON con resumen.

@facturas_bp.route("/facturas/admin/fix-huerfanas-con-aliases", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def fix_huerfanas_con_aliases():
    """Auto-aplica aliases sugeridos al 100% y re-corre backfill numf_completo.

    Params:
        desde=YYYY-MM-DD   default 2025-01-01
        hasta=YYYY-MM-DD   default hoy
        min_pct=100        umbral % de matching para auto-aplicar alias
        dry_run=1          NO inserta aliases ni actualiza numf_completo
    """
    import re
    from datetime import date as _date

    from flask import jsonify

    from modules.asinfo import aliases as _aliases
    from modules.asinfo import service as asinfo_service

    desde_s = (request.values.get("desde") or "2025-01-01").strip()
    hasta_s = (request.values.get("hasta") or today_ec().isoformat()).strip()
    min_pct = float(request.values.get("min_pct") or 100.0)
    dry_run = (request.values.get("dry_run") or "").strip() in ("1", "true", "yes")
    try:
        desde = _date.fromisoformat(desde_s)
        hasta = _date.fromisoformat(hasta_s)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"Fecha invalida: {e}"}), 400

    # 1) Asinfo del rango
    from calendar import monthrange
    cy, cm = desde.year, desde.month
    ey, em = hasta.year, hasta.month
    asinfo_rows = []
    while (cy, cm) <= (ey, em):
        first = _date(cy, cm, 1)
        last = _date(cy, cm, monthrange(cy, cm)[1])
        cd = max(first, desde)
        ch = min(last, hasta)
        try:
            asinfo_rows.extend(asinfo_service.facturas_periodo(cd, ch) or [])
        except Exception:
            pass
        cm += 1
        if cm > 12:
            cm = 1
            cy += 1

    # 2) PC: numf → set(codigo_cli)
    pc_rows = db.fetch_all(
        """
        SELECT numf, codigo_cli
          FROM scintela.factura
         WHERE fecha BETWEEN %s AND %s
           AND stat <> 'X' AND numf IS NOT NULL AND numf > 0
        """,
        (desde, hasta),
    ) or []
    pc_by_numf: dict[int, set[str]] = {}
    pc_by_numf_cli: set[tuple[int, str]] = set()
    for r in pc_rows:
        try:
            n = int(r.get("numf") or 0)
        except (TypeError, ValueError):
            continue
        if not n:
            continue
        cli_pc = (r.get("codigo_cli") or "").strip().upper()
        pc_by_numf.setdefault(n, set()).add(cli_pc)
        pc_by_numf_cli.add((n, cli_pc))

    def _extract_numf(s):
        if not s:
            return None
        m = re.findall(r"\d+", str(s))
        if not m:
            return None
        try:
            return int(m[-1])
        except (ValueError, TypeError):
            return None

    # 3) Agrupar huérfanas por cliente Asinfo y votar aliases
    aliases_existentes = _aliases.todos()
    alias_existe_para = {a["codigo_asinfo"]: a["codigo_pc"] for a in aliases_existentes}
    grupos: dict[str, dict] = {}
    for r in asinfo_rows:
        tipo = (r.get("tipo") or "").upper()
        if tipo not in ("FACTURA", "NTEN"):
            continue
        numero = (r.get("numero") or "").strip()
        numf = _extract_numf(numero)
        if not numf:
            continue
        cli_asinfo = (r.get("cliente_codigo") or "").strip().upper()
        if not cli_asinfo:
            continue
        # Si esta tripla ya matchea, NO es huérfana
        cli_pc_esperado = _aliases.to_pc(cli_asinfo)
        if (numf, cli_pc_esperado) in pc_by_numf_cli:
            continue
        otros_clis = sorted(c for c in pc_by_numf.get(numf, set())
                            if c and c != cli_pc_esperado)
        grupo = grupos.setdefault(cli_asinfo, {"n": 0, "votos_alias": {}})
        grupo["n"] += 1
        for c_pc in otros_clis:
            grupo["votos_alias"][c_pc] = grupo["votos_alias"].get(c_pc, 0) + 1

    # 4) Aliases candidatos al >= min_pct
    aliases_a_agregar = []
    for cli, grupo in grupos.items():
        if not grupo["votos_alias"]:
            continue
        ganador, votos = max(grupo["votos_alias"].items(), key=lambda kv: kv[1])
        pct = 100.0 * votos / grupo["n"]
        if pct < min_pct:
            continue
        if alias_existe_para.get(cli) == ganador:
            continue
        aliases_a_agregar.append({
            "asinfo": cli, "pc": ganador, "votos": votos,
            "total": grupo["n"], "porcentaje": round(pct, 1),
        })

    # 5) Insertar aliases (a menos que dry_run)
    aliases_agregados = []
    if not dry_run:
        usuario = (
            getattr(g, "user", {}).get("username")
            if hasattr(g, "user") and isinstance(getattr(g, "user", None), dict)
            else "auto-fix"
        )
        for ali in aliases_a_agregar:
            try:
                creado = _aliases.agregar(
                    ali["asinfo"], ali["pc"],
                    nota=f"auto-fix: {ali['votos']}/{ali['total']} ({ali['porcentaje']}%)",
                    usuario=usuario,
                )
                if creado:
                    aliases_agregados.append(ali)
            except Exception as e:
                ali["error"] = str(e)

    # 6) Refrescar cache y correr backfill numf_completo
    _aliases._refrescar()
    bf_stats = {}
    if not dry_run:
        try:
            import os
            import sys
            _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from scripts.backfill_numf_completo_from_asinfo import backfill as bf
            bf_stats = bf(dry_run=False, limite=5000, desde=desde)
        except Exception as e:
            bf_stats = {"error": str(e)}

    # 7) Contar huérfanas restantes (facturas PC sin numf_completo)
    row_huerf = db.fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM scintela.factura
         WHERE fecha >= %s
           AND (kg IS NOT NULL AND kg <> 0)
           AND (stat IS NULL OR stat IN ('Z','A','T','X','N','',' '))
           AND (numf_completo IS NULL OR numf_completo = ''
                OR numf IS NULL OR numf = 0)
           AND (numf_completo IS NULL OR NOT (numf_completo LIKE '#%%'))
        """,
        (desde,),
    )

    return jsonify({
        "ok": True,
        "rango": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "min_pct": min_pct,
        "dry_run": dry_run,
        "asinfo_rows": len(asinfo_rows),
        "aliases_existentes": len(aliases_existentes),
        "aliases_a_agregar_segun_umbral": aliases_a_agregar,
        "aliases_agregados_efectivamente": aliases_agregados,
        "backfill_numf_completo": bf_stats,
        "huerfanas_pc_restantes": int(row_huerf.get("n") or 0) if row_huerf else None,
    })


# ---------------------------------------------------------------------------
# Consolidar duplicados: backfill Asinfo creó pares (asinfo-backfill, dbf)
# para el mismo numf+fecha con cli distinto (faltaba el alias). Este endpoint
# detecta esos pares, copia numf_completo al original DBF, registra el alias
# implícito y borra el duplicado backfilleado.
# ---------------------------------------------------------------------------
@facturas_bp.route("/facturas/admin/consolidar-duplicados-asinfo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def consolidar_duplicados_asinfo():
    """Detecta y consolida facturas duplicadas por backfill Asinfo con alias faltante.

    Cuando un cliente Asinfo (ej AJO) corresponde a un cliente PC distinto (ej AJ2)
    y el alias NO estaba registrado al momento del backfill, mi endpoint insertó
    una factura nueva con cli=AJO en vez de matchear con la DBF existente cli=AJ2.

    Este endpoint:
        1. Detecta pares (asinfo-backfill X, dbf-original Y) con
           X.numf == Y.numf AND X.fecha == Y.fecha AND X.codigo_cli != Y.codigo_cli
        2. Para cada par: copia X.numf_completo -> Y.numf_completo (si Y no lo tenía)
        3. Borra X (la asinfo-backfill duplicada)
        4. Registra alias (X.cli -> Y.cli) por consenso

    Params:
        dry_run=1   default 0 — si 1, solo reporta sin tocar nada
    """
    from collections import Counter

    from flask import jsonify

    from modules.asinfo import aliases as _aliases

    dry_run = (request.values.get("dry_run") or "").strip() in ("1", "true", "yes")
    # min_votos: solo registrar aliases con >= N votos. Default 2 para evitar
    # falsos positivos (coincidencias de numero entre clientes distintos).
    try:
        min_votos = int(request.values.get("min_votos") or 2)
    except (TypeError, ValueError):
        min_votos = 2

    # 1) Detectar pares de duplicados
    # Caso A: mismo numf (DBF y backfill matchean por numero idéntico)
    # Caso B: mismo (kg, importe, fecha) — DBF tiene el numf MAL cargado
    #         (ej. perdió un dígito, "76025" vs Asinfo real "176025").
    #         Ambos casos: cli distinto (alias-relevant).
    pares = db.fetch_all(
        """
        WITH backfill AS (
            SELECT id_factura, codigo_cli, numf_completo, numf, fecha,
                   kg, importe
              FROM scintela.factura
             WHERE usuario_crea = 'asinfo-backfill'
        ),
        candidatos AS (
            -- Caso A: mismo numf
            SELECT a.id_factura AS a_id, a.codigo_cli AS a_cli,
                   a.numf_completo AS a_nfc, a.numf AS numf, a.fecha AS fecha,
                   b.id_factura AS b_id, b.codigo_cli AS b_cli,
                   b.numf_completo AS b_nfc,
                   'A_numf' AS match_via
              FROM backfill a
              JOIN scintela.factura b
                ON a.numf = b.numf
               AND a.fecha = b.fecha
               AND a.codigo_cli <> b.codigo_cli
               AND a.id_factura <> b.id_factura
             WHERE COALESCE(b.usuario_crea, '') <> 'asinfo-backfill'
               AND a.numf IS NOT NULL AND a.numf > 0
            UNION ALL
            -- Caso B: mismo (kg, importe, fecha) pero NUMF DISTINTO
            -- (DBF cargó la factura con numf equivocado; Asinfo trae el correcto)
            SELECT a.id_factura AS a_id, a.codigo_cli AS a_cli,
                   a.numf_completo AS a_nfc, a.numf AS numf, a.fecha AS fecha,
                   b.id_factura AS b_id, b.codigo_cli AS b_cli,
                   b.numf_completo AS b_nfc,
                   'B_kg_imp_fecha' AS match_via
              FROM backfill a
              JOIN scintela.factura b
                ON a.fecha = b.fecha
               AND ABS(a.kg - b.kg) < 0.001
               AND ABS(a.importe - b.importe) < 0.01
               AND a.codigo_cli <> b.codigo_cli
               AND a.numf <> b.numf
               AND a.id_factura <> b.id_factura
             WHERE COALESCE(b.usuario_crea, '') <> 'asinfo-backfill'
               AND a.kg IS NOT NULL AND a.kg <> 0
               AND a.importe IS NOT NULL AND a.importe <> 0
        )
        -- DEDUP: si un par sale tanto por caso A como B, quedarse con A.
        SELECT DISTINCT ON (a_id, b_id)
               a_id, a_cli, a_nfc, numf, fecha, b_id, b_cli, b_nfc, match_via
          FROM candidatos
         ORDER BY a_id, b_id,
                  CASE match_via WHEN 'A_numf' THEN 0 ELSE 1 END
        """
    ) or []

    print(f"consolidar: {len(pares)} pares duplicados detectados", flush=True)

    # 2) Votar aliases por (a_cli, b_cli)
    votos_alias = Counter()
    for p in pares:
        votos_alias[(p["a_cli"], p["b_cli"])] += 1

    aliases_existentes = _aliases.todos()
    alias_existe = {a["codigo_asinfo"]: a["codigo_pc"] for a in aliases_existentes}

    aliases_a_agregar = []
    aliases_descartados_pocos_votos = []
    for (a_cli, b_cli), n in votos_alias.most_common():
        if alias_existe.get(a_cli) == b_cli:
            continue
        item = {"asinfo": a_cli, "pc": b_cli, "votos": n}
        if n < min_votos:
            aliases_descartados_pocos_votos.append(item)
            continue
        aliases_a_agregar.append(item)

    aliases_agregados = []
    if not dry_run:
        for ali in aliases_a_agregar:
            try:
                creado = _aliases.agregar(
                    ali["asinfo"], ali["pc"],
                    nota=f"consolidacion: {ali['votos']} duplicados detectados",
                    usuario="consolidar",
                )
                if creado:
                    aliases_agregados.append(ali)
            except Exception as e:
                ali["error"] = str(e)
        _aliases._refrescar()

    # 3) Para cada par: copiar numf_completo y borrar el backfill duplicado
    #    PROTECCIÓN caso B (match por kg+imp+fecha sin numf): solo procesar
    #    si el alias (a_cli, b_cli) está REGISTRADO en cliente_alias. Sin
    #    esto, pares con kg+importe coincidente pero clientes random (BAR↔RIP)
    #    corromperían data poniendo nfc equivocado al DBF.
    #    Recargamos aliases (puede incluir los recien agregados en esta corrida).
    _aliases._refrescar()
    alias_registrado = {a["codigo_asinfo"]: a["codigo_pc"] for a in _aliases.todos()}

    numf_completo_copiados = 0
    duplicados_borrados = 0
    pares_caso_b_saltados_sin_alias = []
    err_log = []
    if not dry_run:
        for p in pares:
            # Caso B (kg+imp+fecha, sin numf) requiere alias registrado.
            if p.get("match_via") == "B_kg_imp_fecha":
                if alias_registrado.get(p["a_cli"]) != p["b_cli"]:
                    pares_caso_b_saltados_sin_alias.append({
                        "a_cli": p["a_cli"], "b_cli": p["b_cli"],
                        "numf": p["numf"], "fecha": str(p["fecha"]),
                    })
                    continue
            try:
                with db.tx() as conn, conn.cursor() as cur:
                    # ORDEN CRITICO: borrar X primero (libera el numf_completo
                    # del unique constraint), DESPUÉS UPDATE Y. Hacerlo al revés
                    # viola uq_factura_numf_completo porque temporalmente ambos
                    # tendrían el mismo nfc.
                    cur.execute(
                        "DELETE FROM scintela.factura WHERE id_factura = %s "
                        "AND usuario_crea = 'asinfo-backfill'",
                        (p["a_id"],),
                    )
                    if cur.rowcount > 0:
                        duplicados_borrados += 1
                    # Ahora SÍ podemos copiar el nfc a Y sin colisionar
                    if (not (p.get("b_nfc") or "").strip()) and (p.get("a_nfc") or "").strip():
                        cur.execute(
                            "UPDATE scintela.factura SET numf_completo = %s WHERE id_factura = %s",
                            (p["a_nfc"], p["b_id"]),
                        )
                        if cur.rowcount > 0:
                            numf_completo_copiados += 1
            except Exception as e:
                err_log.append({"pair": {"a_id": p["a_id"], "b_id": p["b_id"]},
                                "error": str(e)[:200]})

    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "min_votos": min_votos,
        "pares_detectados": len(pares),
        "aliases_implicitos_detectados": len(aliases_a_agregar),
        "aliases_a_agregar": aliases_a_agregar[:30],
        "aliases_descartados_pocos_votos": aliases_descartados_pocos_votos[:30],
        "aliases_agregados_efectivamente": aliases_agregados,
        "numf_completo_copiados_al_original_dbf": numf_completo_copiados,
        "duplicados_borrados": duplicados_borrados,
        "errores": err_log[:20],
        "pares_por_match_via": dict(Counter(p.get("match_via") for p in pares)),
        "pares_caso_b_saltados_sin_alias": pares_caso_b_saltados_sin_alias[:30] if not dry_run else [],
        "sample_pares": [
            {"numf": p["numf"], "fecha": str(p["fecha"]),
             "a_cli_asinfo_backfill": p["a_cli"], "b_cli_dbf_original": p["b_cli"],
             "a_nfc": p["a_nfc"], "b_nfc": p["b_nfc"],
             "match_via": p.get("match_via")}
            for p in pares[:10]
        ],
    })
    # TMT 2026-06-03 audit fix: el bloque INSERT que estaba acá era código
    # muerto post-return. Pertenecía a backfill_asinfo_endpoint y se movió.
