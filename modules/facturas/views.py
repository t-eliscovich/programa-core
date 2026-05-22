"""Listado y detalle de facturas."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

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
        form["fecha"] = datetime.now().date().strftime("%d/%m/%Y")
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
        return redirect(url_for("facturas.detalle", id_factura=creada["id_factura"]))
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
    fact = queries.por_id(id_factura)
    if not fact:
        abort(404)
    if (fact.get("stat") or "").upper() in queries.STATS_ANULADAS:
        flash("La factura está anulada/eliminada — no se puede editar.", "warn")
        return redirect(url_for("facturas.detalle", id_factura=id_factura))

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
            return redirect(url_for("facturas.detalle", id_factura=id_factura))
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


@facturas_bp.route("/facturas/<int:id_factura>/confirmar-anulacion", methods=["GET"])
@requiere_login
@requiere_permiso("facturas.anular")
def confirmar_anulacion(id_factura: int):
    """Paso 1 del 2-step undo: muestra el resumen + pide motivo antes de anular."""
    fact = queries.por_id(id_factura)
    if not fact:
        abort(404)
    if fact.get("stat") == "Y":
        flash("La factura ya está anulada.", "warn")
        return redirect(url_for("facturas.detalle", id_factura=id_factura))
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
        volver_url=url_for("facturas.detalle", id_factura=id_factura),
        motivo_requerido=True,
        confirm_label="Confirmar anulación",
    )


@facturas_bp.route("/facturas/<int:id_factura>/anular", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.anular")
def anular(id_factura: int):
    motivo = (request.form.get("motivo") or "").strip()
    # Motivo opcional — la dueña puede dejarlo vacío (ej. "error de carga"
    # implícito). TMT 2026-05-13.
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.anular(id_factura, motivo=motivo, usuario=usuario)
        flash(f"Factura {id_factura} anulada.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude anular", e)
    return redirect(url_for("facturas.detalle", id_factura=id_factura))


@facturas_bp.route("/facturas/<int:id_factura>")
@requiere_login
@requiere_permiso("facturas.ver")
def detalle(id_factura: int):
    fact = queries.por_id(id_factura)
    if not fact:
        abort(404)
    aplicaciones = queries.cheques_aplicados(id_factura)
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


@facturas_bp.route("/facturas")
@requiere_login
@requiere_permiso("facturas.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    cliente = request.args.get("cliente", "").strip()
    def _parse_num(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return float(str(s).replace(",", "."))
        except ValueError:
            return None
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
        # True si la factura es de ANTES de cuando arrancó Asinfo "limpio"
        # → sabemos que no va a tener match, no es un error.
        f["asinfo_pre_cutoff"] = bool(f.get("fecha") and f["fecha"] < _ASINFO_CUTOFF)
    if 0 < len(filas) <= 6000:
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

                # Mergear: elegir índice según signo del kg de PC.
                #   kg > 0  → buscar en FACTURA+NTEN+NC_FINANCIERA
                #   kg < 0  → buscar en DEVOLUCION+NCNT
                #   kg == 0 → no matchear (NC financiera, ajustes)
                for f in filas:
                    pc_kg = float(f.get("kg") or 0)
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
                    # que los importes coincidan (PC sin IVA == card 199 usd).
                    if r_ai is None:
                        cli_pc = (f.get("codigo_cli") or "").strip().upper()
                        fecha_pc = f.get("fecha")
                        if cli_pc and fecha_pc:
                            key = (cli_pc, str(fecha_pc)[:10], round(pc_kg, 2))
                            candidatos = idx_compuesto.get(key, [])
                            # Solo match si hay UN candidato (evitar ambigüedad).
                            # Validar usd con tolerancia ±0.5 USD.
                            pc_imp = float(f.get("importe") or 0)
                            ok = [c for c in candidatos
                                  if abs(float(c.get("usd") or 0) - pc_imp) < 0.5]
                            if len(ok) == 1:
                                r_ai = ok[0]
                    if r_ai is not None:
                        f["asinfo_kg"] = float(r_ai.get("kg") or 0)
                        f["asinfo_usd"] = float(r_ai.get("usd") or 0)
                        f["asinfo_diff_kg"] = round(f["asinfo_kg"] - pc_kg, 3)
                        f["asinfo_diff_usd"] = round(f["asinfo_usd"] - float(f.get("importe") or 0), 2)
                        f["asinfo_tipo"] = r_ai.get("tipo")
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


@facturas_bp.route("/facturas/backfill-asinfo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("facturas.editar")
def backfill_asinfo():
    """Audit + backfill automático de huérfanas Asinfo.

    GET  → muestra resumen sin tocar nada (preview).
    POST → aplica los matches que cumplen el threshold.
    """
    from modules.facturas import audit_asinfo

    apply = request.method == "POST"
    huerfanas = audit_asinfo.auditar_huerfanas(top_k=3, limite=1000)

    aplicados: list[dict] = []
    saltadas_sin_candidatos: list[dict] = []
    saltadas_score_alto: list[dict] = []

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
                audit_asinfo.asociar(
                    pc["id_factura"],
                    mejor["ai_numero"],
                    usuario=getattr(g, "user", None) and g.user.get("username") or "web",
                )
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
        "modo": "POST aplicado" if apply else "GET preview (no toca DB)",
        "threshold_score": _BACKFILL_SCORE_MAX,
        "huerfanas_total": len(huerfanas),
        "aplicados_count": len(aplicados),
        "saltadas_score_alto_count": len(saltadas_score_alto),
        "saltadas_sin_candidatos_count": len(saltadas_sin_candidatos),
        "aplicados": aplicados,
        "saltadas_score_alto": saltadas_score_alto,
        "saltadas_sin_candidatos": saltadas_sin_candidatos,
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
