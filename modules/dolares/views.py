"""Anticipos en USD — listado y vista agrupada de scintela.dolares."""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import db
from auth import requiere_login, requiere_permiso, tiene_permiso
from error_messages import flash_exc
from exports import csv_response
from filters import today_ec
from parsers import parse_date, parse_monto

from . import queries

dolares_bp = Blueprint("dolares", __name__, template_folder="templates")


def _safe(fn, default):
    try:
        return fn(), None
    except Exception as e:
        return default, str(e)


def _nombres_clientes(codigos: list[str]) -> dict[str, str]:
    """Mapeo cta (= codigo_cli, 3 chars) → nombre cliente.

    Una sola consulta para todas las cuentas, evita N+1.
    """
    codigos = [c for c in codigos if c]
    if not codigos:
        return {}
    rows = db.fetch_all(
        """
        SELECT UPPER(TRIM(codigo_cli)) AS cta, nombre
        FROM scintela.cliente
        WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)
        """,
        (codigos,),
    )
    return {r["cta"]: r["nombre"] for r in rows or []}


# TMT 2026-07-06 (dueña): /anticipos/ se retira y su gente entra ACÁ. Quien
# hoy usaba /anticipos tenía facturas.ver (Bodega/Alex, Ventas) pero NO
# informes.ver — mismo patrón granular que /informes/deudas (2026-07-01):
# se acepta cualquiera de los dos permisos, sin aflojar nada de escritura.
@dolares_bp.route("/dolares")
@requiere_login
def lista():
    if not (tiene_permiso("informes.ver") or tiene_permiso("facturas.ver")):
        abort(404)
    """Anticipos en USD — vista moderna agrupada por cuenta.

    El total de los anticipos vivos (st vacío) coincide con el campo
    ANTICIPOS del balance. La vista trae:
      · 4 KPIs hero (total vivo, partidas, cuentas, aplicados acumulados)
      · cards por cuenta con saldo vivo + ranking de mayor a menor
      · tabla detallada con filtros (cuenta/fecha/solo vivos)
    """
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    cta   = (request.args.get("cta") or "").strip() or None
    q     = (request.args.get("q") or "").strip() or None
    solo_vivos = request.args.get("solo_vivos", "1") != "0"
    # Filtro por código en UN solo campo: "AC 31" → cuenta AC + concepto 31.
    # Si viene `codigo`, pisa cuenta/concepto (atajo de la dueña 2026-07-10).
    codigo = (request.args.get("codigo") or "").strip()
    if codigo:
        import re as _re_cod
        mcod = _re_cod.match(r"^\s*([A-Za-z]{2,3})\s*0*(\d{1,6})?", codigo)
        if mcod:
            cta = mcod.group(1).upper()
            q = mcod.group(2) if mcod.group(2) else q
    # Mes de recibido (la fecha de recepción se cuelga de Asinfo, se filtra en
    # Python porque no vive en scintela.dolares).
    recibido_mes = (request.args.get("recibido_mes") or "").strip() or None
    filas, error = _safe(
        lambda: queries.lista(
            desde=desde, hasta=hasta, cta=cta, solo_vivos=solo_vivos, q=q,
        ),
        [],
    )
    # Colgar a cada anticipo la RECEPCIÓN de su importación Asinfo (im_numero,
    # fecha_recepcion_im, kg_im). Fail-soft e independiente del _safe de arriba:
    # si Asinfo cae, la lista igual se muestra (las columnas quedan en blanco).
    try:
        from modules.importaciones import service as _import_service
        _import_service.adjuntar_recepcion_asinfo(filas)
    except Exception:  # noqa: BLE001
        pass
    if recibido_mes:
        filas = [
            f for f in filas
            if str(f.get("fecha_recepcion_im") or "").startswith(recibido_mes)
        ]
    cuentas, _ = _safe(lambda: queries.por_cuenta(solo_vivos=True), [])
    res, _ = _safe(queries.resumen, {})

    # Enriquecer cuentas con nombre del cliente (una sola query, no N+1).
    nombres = _nombres_clientes([c["cta"] for c in cuentas])
    for c in cuentas:
        c["nombre"] = nombres.get(c["cta"]) or ""

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"), ("cta", "Cuenta"),
                ("concepto", "Concepto"), ("importe", "Importe"),
                ("st", "Estado"), ("clave", "Clave"),
                ("fecha_recepcion_im", "Recibido"),
                ("kg_im", "Kg importación"), ("im_numero", "Importación"),
            ],
            filename="anticipos.csv",
        )
    return render_template(
        "dolares/lista.html",
        filas=filas, cuentas=cuentas, resumen=res,
        desde=desde, hasta=hasta, cta=cta, q=q,
        codigo=codigo or None, recibido_mes=recibido_mes,
        solo_vivos=solo_vivos, error=error,
        hoy=today_ec().isoformat(),
    )


@dolares_bp.route("/dolares/convertir-seleccion", methods=["POST"])
@requiere_login
@requiere_permiso("compras.crear")
def convertir_seleccion():
    """Convierte los anticipos TILDADOS en la lista principal a una compra (BAP),
    en un paso con confirmación en el frente.

    Modelo (dueña 2026-07-10): el kg vive en el STOCK (la importación de Asinfo),
    NO en la compra — muchas compras (SALDO/CAE/seguro) pueden mapear a un solo
    stock, así que el kg NO se escribe en la compra (kg=None); el importe (USD)
    de los anticipos ACUMULA contra ese stock. Concepto = nº de la importación
    (para que la compra matchee con ella); tipo H (hilado).
    """
    codigo_prov = (request.form.get("codigo_prov") or "").strip().upper()
    ids_raw = request.form.getlist("id_dolares")
    try:
        ids = [int(x) for x in ids_raw if x and str(x).strip()]
    except ValueError:
        flash("IDs de anticipos inválidos.", "warn")
        return redirect(url_for("dolares.lista"))
    concepto = (request.form.get("concepto") or "").strip()
    motivo = (request.form.get("motivo") or "").strip()
    if not codigo_prov or not ids:
        flash("Seleccioná anticipos de una sola importación para convertir.", "warn")
        return redirect(request.referrer or url_for("dolares.lista"))
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.convertir_a_compra(
            codigo_prov=codigo_prov, ids_anticipos=ids,
            concepto=concepto, tipo_compra="H", kg=None,
            motivo=motivo, usuario=usuario,
        )
        flash(
            f"BAP: {r['n_anticipos']} anticipo(s) de {codigo_prov} → compra "
            f"N° {r['numero_compra']} ({r['comprobante']}) por $ {r['importe_total']:.2f}. "
            f"Los kg quedan en el stock de la importación.",
            "ok",
        )
        return redirect(url_for("compras.lista", q=codigo_prov))
    except ValueError as e:
        flash(str(e), "warn")
        return redirect(request.referrer or url_for("dolares.lista"))
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude convertir los anticipos", e)
        return redirect(request.referrer or url_for("dolares.lista"))


# ---------------------------------------------------------------------------
# Alta y cancelación directa de anticipos — MOVIDO de modules/anticipos.
# TMT 2026-07-06 (dueña): "/anticipos/ borrar, tiene que ser esta pantalla
# /dolares". Misma lógica de negocio que el flujo dBase (TMT 2026-06-11):
# ST=' ' = vivo (suma a ANTICIPOS del balance, INFORMES.PRG L58); cancelar
# = ST='B'. Permisos de ESCRITURA intactos: facturas.crear (igual que
# tenían anticipos.nuevo / anticipos.cancelar — no se afloja).
# ---------------------------------------------------------------------------

@dolares_bp.route("/dolares/nuevo-anticipo", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def nuevo_anticipo():
    """Alta de anticipo directo en scintela.dolares (ex anticipos.nuevo)."""
    try:
        fecha = parse_date(request.form.get("fecha")) or today_ec()
        cta = (request.form.get("cta") or "").strip().upper()[:3]
        concepto = (request.form.get("concepto") or "").strip()[:100]
        # parse_monto = parser canónico de plata (EU: 1.234,56) — el input
        # del form es texto, no type=number, para aceptar formato EU.
        monto = parse_monto(request.form.get("importe"))
        importe = round(float(monto), 2) if monto is not None else 0.0
        if not cta or importe <= 0:
            flash("Faltan datos (cliente / importe).", "warn")
            return redirect(url_for("dolares.lista"))
        usuario = (getattr(g, "user", None) or {}).get("username", "web")
        # clave = operador (paridad dBase). g.user no trae 'clave' → prefijo
        # del username en mayúsculas (andres→AND). Mismo patrón que compras/gastos.
        clave = (getattr(g, "user", None) or {}).get("clave") or usuario[:3].upper()
        import bank_helpers
        import mov_doble as _md
        from periodo_guard import asegurar_fecha_abierta
        # TMT 2026-07-07 (dueña): cargar un anticipo USD RESTA del banco (ND
        # automática desde Pichincha, igual que anticipos de importación) +
        # registra el par en mov_doble → aparece en /historial. Todo atómico.
        _BANCO_PICHINCHA = 10
        asegurar_fecha_abierta(fecha)
        with db.tx() as conn:
            dol_row = db.execute_returning(
                "INSERT INTO scintela.dolares "
                "(fecha, cta, concepto, importe, st, clave, usuario_crea) "
                "VALUES (%s, %s, %s, %s, ' ', %s, %s) RETURNING id_dolares",
                (fecha, cta, concepto, importe, clave, usuario),
                conn=conn,
            ) or {}
            id_dolares = dol_row.get("id_dolares")
            mov_b = bank_helpers.insert_movimiento_bancario(
                conn, no_banco=_BANCO_PICHINCHA, no_cta=None, fecha=fecha,
                documento="ND", importe=importe,
                concepto=(f"ANTICIPO {cta} {concepto}").strip()[:50],
                prov=(cta or None), usuario=usuario,
            )
            id_tx = mov_b.get("id_transaccion")
            _md.registrar(
                conn=conn, tipo="dolares_anticipo",
                origen_table="dolares", origen_id=id_dolares,
                destino_table="transacciones_bancarias", destino_id=id_tx,
                importe=importe, fecha=fecha,
                concepto=(f"Anticipo USD {cta} $ {importe:.2f} (ND Pichincha)")[:200],
                usuario=usuario,
                metadata={"cta": cta, "no_banco": _BANCO_PICHINCHA, "id_transaccion": id_tx},
            )
        flash(f"Anticipo {cta} $ {importe:,.2f} registrado — ND Pichincha (resta del banco).", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No se pudo registrar el anticipo", e)
    return redirect(url_for("dolares.lista"))


@dolares_bp.route("/dolares/anticipo/<int:id_dolares>/cancelar", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def cancelar_anticipo(id_dolares: int):
    """Cancela un anticipo vivo → ST='B' (ex anticipos.cancelar)."""
    # TMT 2026-07-07 (dueña): cancelar un anticipo también REVIERTE la ND del
    # banco — compensa con una NC(+) del mismo importe (paper trail) y marca el
    # mov_doble reversado. Atómico. Espejo de importaciones/pago.deshacer.
    import bank_helpers
    from periodo_guard import asegurar_fecha_abierta
    usuario = (getattr(g, "user", None) or {}).get("username", "web")
    try:
        with db.tx() as conn:
            row = db.fetch_one(
                "SELECT id_dolares, cta, importe FROM scintela.dolares "
                "WHERE id_dolares = %s AND (st IS NULL OR TRIM(COALESCE(st,'')) = '')",
                (id_dolares,), conn=conn,
            )
            if not row:
                flash("No se encontró o ya estaba cancelado.", "warn")
                return redirect(url_for("dolares.lista"))
            md = db.fetch_one(
                "SELECT id_mov_doble, destino_id FROM scintela.mov_doble "
                "WHERE origen_table='dolares' AND origen_id=%s "
                "  AND tipo='dolares_anticipo' AND estado='activo' "
                "ORDER BY id_mov_doble DESC LIMIT 1",
                (id_dolares,), conn=conn,
            )
            if md and md.get("destino_id"):
                tx = db.fetch_one(
                    "SELECT no_banco, no_cta FROM scintela.transacciones_bancarias "
                    "WHERE id_transaccion = %s", (md["destino_id"],), conn=conn,
                )
                if tx:
                    fecha_rev = today_ec()
                    asegurar_fecha_abierta(fecha_rev)
                    bank_helpers.insert_movimiento_bancario(
                        conn, no_banco=int(tx["no_banco"]), no_cta=tx.get("no_cta"),
                        fecha=fecha_rev, documento="NC",
                        importe=abs(float(row["importe"] or 0)),
                        concepto=(f"REVERSO ANTICIPO {row.get('cta') or ''}").strip()[:50],
                        usuario=usuario,
                    )
                db.execute(
                    "UPDATE scintela.mov_doble SET estado='reversado' WHERE id_mov_doble = %s",
                    (md["id_mov_doble"],), conn=conn,
                )
            db.execute(
                "UPDATE scintela.dolares SET st = 'B' WHERE id_dolares = %s",
                (id_dolares,), conn=conn,
            )
        flash("Anticipo cancelado (ST=B) — se revirtió la ND del banco.", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No se pudo cancelar el anticipo", e)
    return redirect(url_for("dolares.lista"))


@dolares_bp.route("/dolares/convertir-lote", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("compras.crear")
def convertir_lote():
    """BAP — conciliación split-screen anticipos USD → compra (BANCOS.PRG).

    TMT 2026-07-08 (dueña): "no hace falta tantos pasos". Se retira el flujo
    de 3 pasos (tabla por proveedor → anticipos del proveedor → convertir) y
    se reemplaza por UNA pantalla partida en dos:
      · IZQUIERDA: los anticipos vivos (st vacío) de todos los proveedores
        tipo H, con checkbox para seleccionar.
      · DERECHA: las importaciones (`importaciones_con_cruce`, la misma data
        de "Ingreso de hilado"), como referencia, con un radio para elegir
        la importación destino.
    Un único filtro (ej. "AC 15") filtra AMBOS lados por prov + nº (JS, sobre
    data-attributes). Se selecciona a un lado y al otro como una conciliación.

    POST: ejecuta `queries.convertir_a_compra()` con el proveedor de la
    importación elegida (o de los anticipos si no se eligió ninguna) y el
    concepto = nº de la importación (para que la compra matchee con ella).

    Permisos: `compras.crear` (estamos creando una compra).
    """
    if request.method == "POST":
        codigo_prov = (request.form.get("codigo_prov") or "").strip().upper()
        ids_raw = request.form.getlist("id_dolares")
        try:
            ids = [int(x) for x in ids_raw if x and str(x).strip()]
        except ValueError:
            flash("IDs de anticipos inválidos.", "warn")
            return redirect(url_for("dolares.convertir_lote"))
        # concepto = nº de la importación elegida en el panel derecho (así la
        # compra queda matcheada a ella). Si no se eligió ninguna, el JS manda
        # como fallback la ref de los propios anticipos.
        concepto = (request.form.get("concepto") or "").strip()
        tipo_compra = (request.form.get("tipo_compra") or "H").strip().upper()
        fecha = parse_date(request.form.get("fecha")) or today_ec()
        kg = parse_monto(request.form.get("kg"))
        motivo = (request.form.get("motivo") or "").strip()

        if not codigo_prov:
            flash("No pude determinar el proveedor (elegí una importación o "
                  "anticipos de un mismo proveedor).", "warn")
            return redirect(url_for("dolares.convertir_lote"))
        if not ids:
            flash("Seleccioná al menos un anticipo para convertir.", "warn")
            return redirect(url_for("dolares.convertir_lote"))

        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.convertir_a_compra(
                codigo_prov=codigo_prov,
                ids_anticipos=ids,
                fecha=fecha,
                concepto=concepto,
                tipo_compra=tipo_compra,
                kg=kg,
                motivo=motivo,
                usuario=usuario,
            )
            flash(
                f"BAP: {r['n_anticipos']} anticipo(s) de {codigo_prov} "
                f"convertidos a compra N° {r['numero_compra']} "
                f"({r['comprobante']}) por $ {r['importe_total']:.2f}.",
                "ok",
            )
            # Compras no tiene endpoint de detalle por id — redirigimos al
            # listado de compras filtrado por proveedor.
            return redirect(url_for("compras.lista", q=codigo_prov))
        except ValueError as e:
            flash(str(e), "warn")
            return redirect(url_for("dolares.convertir_lote"))
        except Exception as e:  # noqa: BLE001
            flash_exc("No pude convertir los anticipos", e)
            return redirect(url_for("dolares.convertir_lote"))

    # ── GET: conciliación split-screen ─────────────────────────────────────
    import re as _re

    # IZQUIERDA — anticipos vivos de proveedores tipo H (todos, de una).
    # Ref (nº) = primer número del concepto (mismo parseo del flujo viejo).
    anticipos, _ = _safe(
        lambda: queries.anticipos_vivos(tipos_filter=["H"]),
        [],
    )
    for _a in anticipos:
        _m = _re.search(r"\d+", str(_a.get("concepto") or ""))
        _a["ref"] = _m.group(0) if _m else ""
    # TMT 2026-07-08 (dueña "ordenalo"): agrupar por código de proveedor y nº,
    # igual que el panel de importaciones — así los del mismo AC quedan juntos.
    anticipos.sort(key=lambda a: (
        str(a.get("cta") or "").upper(),
        int(a["ref"]) if str(a.get("ref") or "").isdigit() else 10**9,
        a.get("fecha") or "",
    ))

    # DERECHA — importaciones (importaciones_con_cruce). Fail-soft: si Asinfo
    # está caído o el service explota, seguimos mostrando la izquierda y
    # marcamos "sin importaciones" en el panel derecho (nunca rompe la vista).
    importaciones: list[dict] = []
    imp_error: str | None = None
    try:
        from modules.importaciones import service as _imp_service
        raw = _imp_service.importaciones_con_cruce()
        for r in raw:
            prov = (r.get("prov") or "").strip().upper()
            num = r.get("numero")
            ref = str(num) if num is not None else ""
            # Valor de la importación: compra o anticipo USD confiable del
            # programa (importe_programa); si no, Σ movimientos (anticipo_aplicado).
            valor = r.get("importe_programa")
            if valor is None:
                _ap = float(r.get("anticipo_aplicado") or 0)
                valor = _ap or None
            importaciones.append({
                "im_numero": r.get("im_numero") or "",
                "prov": prov,
                "codigo": r.get("codigo") or "",
                "ref": ref,
                "nota": r.get("nota") or "",
                "kg": r.get("kg"),
                "proveedor": r.get("proveedor") or "",
                "valor": valor,
                "fuente": (r.get("fuente") or ""),
            })
        # Con código primero (matcheables), luego por prov y nº.
        importaciones.sort(key=lambda x: (
            0 if x["prov"] and x["ref"] else 1,
            x["prov"],
            int(x["ref"]) if x["ref"].isdigit() else 0,
        ))
    except Exception as e:  # noqa: BLE001
        imp_error = str(e)
        importaciones = []

    total_anticipos_usd = sum(float(a.get("importe") or 0) for a in anticipos)

    return render_template(
        "dolares/convertir_lote.html",
        anticipos=anticipos,
        importaciones=importaciones,
        imp_error=imp_error,
        total_anticipos_usd=total_anticipos_usd,
        hoy=today_ec().isoformat(),
    )


@dolares_bp.route("/dolares/cargar-quimicos", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("compras.crear")
def cargar_quimicos():
    """Cargar químicos — anticipos de proveedores tipo Q → compra tipo Q.

    TMT 2026-07-09 (dueña): botón análogo a "convertir a compra (hilado)"
    pero para químicos. Diferencia clave con el de hilo: NO hay importación
    que conciliar (los químicos no pasan por el circuito de importaciones de
    Asinfo) — es directo anticipo → compra. Por eso es una sola columna, sin
    panel derecho.

    Filtra los anticipos vivos a proveedores `scintela.proveedor.tipo='Q'`
    (mismo mecanismo `tipos_filter` que el de hilo usa con ['H']). La compra
    se crea con `tipo_compra='Q'`, así entra como "compras químicos" del mes
    (la que lee el balance / flujo por `compra.tipo='Q'`).

    Permisos: `compras.crear` (estamos creando una compra), igual que el BAP
    de hilo.
    """
    if request.method == "POST":
        codigo_prov = (request.form.get("codigo_prov") or "").strip().upper()
        ids_raw = request.form.getlist("id_dolares")
        try:
            ids = [int(x) for x in ids_raw if x and str(x).strip()]
        except ValueError:
            flash("IDs de anticipos inválidos.", "warn")
            return redirect(url_for("dolares.cargar_quimicos"))
        concepto = (request.form.get("concepto") or "").strip()
        fecha = parse_date(request.form.get("fecha")) or today_ec()
        kg = parse_monto(request.form.get("kg"))
        motivo = (request.form.get("motivo") or "").strip()

        if not codigo_prov:
            flash("Elegí anticipos de un mismo proveedor de químicos.", "warn")
            return redirect(url_for("dolares.cargar_quimicos"))
        if not ids:
            flash("Seleccioná al menos un anticipo para cargar.", "warn")
            return redirect(url_for("dolares.cargar_quimicos"))

        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.convertir_a_compra(
                codigo_prov=codigo_prov,
                ids_anticipos=ids,
                fecha=fecha,
                concepto=concepto,
                tipo_compra="Q",
                kg=kg,
                motivo=motivo,
                usuario=usuario,
            )
            flash(
                f"Químicos: {r['n_anticipos']} anticipo(s) de {codigo_prov} "
                f"cargados como compra N° {r['numero_compra']} "
                f"({r['comprobante']}) por $ {r['importe_total']:.2f}.",
                "ok",
            )
            return redirect(url_for("compras.lista", q=codigo_prov))
        except ValueError as e:
            flash(str(e), "warn")
            return redirect(url_for("dolares.cargar_quimicos"))
        except Exception as e:  # noqa: BLE001
            flash_exc("No pude cargar los químicos", e)
            return redirect(url_for("dolares.cargar_quimicos"))

    # ── GET: una sola columna de anticipos de proveedores tipo Q ────────────
    import re as _re

    anticipos, _ = _safe(
        lambda: queries.anticipos_vivos(tipos_filter=["Q"]),
        [],
    )
    for _a in anticipos:
        _m = _re.search(r"\d+", str(_a.get("concepto") or ""))
        _a["ref"] = _m.group(0) if _m else ""
    anticipos.sort(key=lambda a: (
        str(a.get("cta") or "").upper(),
        int(a["ref"]) if str(a.get("ref") or "").isdigit() else 10**9,
        a.get("fecha") or "",
    ))

    total_anticipos_usd = sum(float(a.get("importe") or 0) for a in anticipos)

    return render_template(
        "dolares/cargar_quimicos.html",
        anticipos=anticipos,
        total_anticipos_usd=total_anticipos_usd,
        hoy=today_ec().isoformat(),
    )


@dolares_bp.route("/dolares/reversar-conversion/<int:id_mov_doble>",
                  methods=["GET", "POST"])
@requiere_login
@requiere_permiso("compras.crear")
def reversar_conversion(id_mov_doble: int):
    """Deshace una conversión BAP (anticipo→compra) desde /historial.

    GET: pantalla de confirmación. POST: ejecuta queries.reversar_conversion()
    (restaura los anticipos a vivos + borra la compra BAP, atómico).
    """
    if request.method == "GET":
        return render_template(
            "_confirmar_accion.html",
            titulo="Deshacer conversión a compra (BAP)",
            mensaje=(
                "Vas a deshacer esta conversión: se ELIMINA la compra creada y "
                "los anticipos vuelven a estar vivos (sin consumir). Queda "
                "registrado en /historial."
            ),
            accion_url=url_for("dolares.reversar_conversion",
                               id_mov_doble=id_mov_doble),
            volver_url=url_for("historial.lista"),
            motivo_requerido=False,
            motivo_obligatorio=False,
            confirm_label="Deshacer conversión",
        )
    motivo = (request.form.get("motivo") or "").strip()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.reversar_conversion(id_mov_doble, motivo=motivo, usuario=usuario)
        flash(
            f"Conversión deshecha: compra {r['comprobante']} eliminada, "
            f"{r['restaurados']} anticipo(s) restaurados a vivos.",
            "ok",
        )
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude deshacer la conversión", e)
    return redirect(url_for("historial.lista"))
