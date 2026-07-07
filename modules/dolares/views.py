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
    filas, error = _safe(
        lambda: queries.lista(
            desde=desde, hasta=hasta, cta=cta, solo_vivos=solo_vivos, q=q,
        ),
        [],
    )
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
            ],
            filename="anticipos.csv",
        )
    return render_template(
        "dolares/lista.html",
        filas=filas, cuentas=cuentas, resumen=res,
        desde=desde, hasta=hasta, cta=cta, q=q,
        solo_vivos=solo_vivos, error=error,
        hoy=today_ec().isoformat(),
    )


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
    import mov_doble as _md
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
    """BAP — conversión lote de anticipos USD a compra (BANCOS.PRG:733-819).

    GET sin proveedor: muestra agrupación por proveedor con totales.
    GET con `?prov=XX`: muestra anticipos del proveedor con checkboxes.
    POST: ejecuta `queries.convertir_a_compra()`.

    Permisos: `compras.crear` (estamos creando una compra).
    """
    if request.method == "POST":
        codigo_prov = (request.form.get("codigo_prov") or "").strip().upper()
        ids_raw = request.form.getlist("id_dolares")
        try:
            ids = [int(x) for x in ids_raw if x and str(x).strip()]
        except ValueError:
            flash("IDs de anticipos inválidos.", "warn")
            return redirect(url_for("dolares.convertir_lote",
                                    prov=codigo_prov))
        concepto = (request.form.get("concepto") or "").strip()
        tipo_compra = (request.form.get("tipo_compra") or "H").strip().upper()
        fecha = parse_date(request.form.get("fecha")) or today_ec()
        kg = parse_monto(request.form.get("kg"))
        motivo = (request.form.get("motivo") or "").strip()

        if not codigo_prov:
            flash("Proveedor requerido.", "warn")
            return redirect(url_for("dolares.convertir_lote"))
        if not ids:
            flash("Seleccioná al menos un anticipo para convertir.", "warn")
            return redirect(url_for("dolares.convertir_lote",
                                    prov=codigo_prov))

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
            return redirect(url_for("dolares.convertir_lote",
                                    prov=codigo_prov))
        except Exception as e:  # noqa: BLE001
            flash_exc("No pude convertir los anticipos", e)
            return redirect(url_for("dolares.convertir_lote",
                                    prov=codigo_prov))

    # GET
    prov_sel = (request.args.get("prov") or "").strip().upper() or None
    # TMT 2026-05-20 — pedido dueña: "Lo mismo para hilo (H)". Filtramos
    # proveedores tipo='H' en el wizard de convertir-lote.
    grupos, _ = _safe(
        lambda: queries.anticipos_pendientes_por_proveedor(tipos_filter=["H"]),
        [],
    )

    # Enriquecer con nombre del proveedor (no es N+1 grande — pocos proveedores).
    nombres: dict[str, str] = {}
    try:
        rows = db.fetch_all(
            "SELECT codigo_prov, COALESCE(nombre,'') AS nombre "
            "FROM scintela.proveedor"
        ) or []
        nombres = {r["codigo_prov"]: r["nombre"] for r in rows}
    except Exception:
        pass
    for g_row in grupos:
        g_row["nombre"] = nombres.get(g_row["codigo_prov"]) or ""

    anticipos: list[dict] = []
    if prov_sel:
        anticipos, _ = _safe(
            lambda: queries.anticipos_pendientes_de_proveedor(prov_sel),
            [],
        )

    return render_template(
        "dolares/convertir_lote.html",
        grupos=grupos,
        prov_sel=prov_sel,
        anticipos=anticipos,
        nombres=nombres,
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
