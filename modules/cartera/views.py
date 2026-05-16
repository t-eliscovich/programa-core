"""Cartera — aging report + acción de stop automático + bulk actions."""
from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from parsers import parse_int

from . import queries

cartera_bp = Blueprint("cartera", __name__, template_folder="templates")


def _parse_codigos(raw: str) -> set[str]:
    """`?codigos=JTX,BED,CLR` → {'JTX','BED','CLR'}. Ignora vacíos y repetidos."""
    if not raw:
        return set()
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


@cartera_bp.route("/cartera/aging")
@requiere_login
@requiere_permiso("cartera.ver")
def aging():
    try:
        filas = queries.aging_buckets()
        totales = queries.aging_totales()
        error = None
    except Exception as e:
        filas, totales, error = [], {}, str(e)

    # Filtro opcional por selección — se usa desde el botón
    # "Exportar seleccionados" con `?codigos=JTX,BED,CLR`.
    codigos_sel = _parse_codigos(request.args.get("codigos", ""))
    if codigos_sel:
        filas = [f for f in filas if (f.get("codigo_cli") or "").upper() in codigos_sel]
        # Recalcular totales sobre la selección — suma en Python, está filtrada.
        totales = {
            "b0_30":      sum(float(f.get("b0_30")   or 0) for f in filas),
            "b31_60":     sum(float(f.get("b31_60")  or 0) for f in filas),
            "b61_90":     sum(float(f.get("b61_90")  or 0) for f in filas),
            "b90_plus":   sum(float(f.get("b90_plus") or 0) for f in filas),
            "total":      sum(float(f.get("saldo_total") or 0) for f in filas),
            "n_facturas": sum(int(f.get("n_facturas") or 0) for f in filas),
            "n_clientes": len(filas),
        }

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo_cli", "Código"),
                ("nombre", "Cliente"),
                ("vend", "Vend."),
                ("telefono", "Teléfono"),
                ("correo", "Email"),
                ("stop", "Stop"),
                ("cupo", "Cupo"),
                ("n_facturas", "N° fact."),
                ("b0_30", "0-30"),
                ("b31_60", "31-60"),
                ("b61_90", "61-90"),
                ("b90_plus", "90+"),
                ("saldo_total", "Saldo total"),
                ("vence_mas_viejo", "Venc. más viejo"),
                ("dias_mora_max", "Días mora máx"),
            ],
            filename="cartera_aging.csv",
        )

    return render_template(
        "cartera/aging.html",
        filas=filas,
        totales=totales,
        error=error,
        codigos_seleccionados=sorted(codigos_sel) if codigos_sel else [],
    )


@cartera_bp.route("/cartera/grupos")
@requiere_login
@requiere_permiso("cartera.ver")
def grupos():
    """Cartera consolidada por grupo de cliente (scintela.grupo_cliente).

    Reemplaza la opción `INFORMES > GRUPOS` del menú dBase legacy.
    """
    try:
        filas = queries.aging_por_grupo()
        error = None
    except Exception as e:
        filas, error = [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo_grupo", "Cód. grupo"),
                ("nombre_padre", "Cliente padre"),
                ("n_hijos", "N° de hijos"),
                ("hijos", "Códigos hijos"),
                ("saldo_total", "Saldo total"),
            ],
            filename="cartera_por_grupo.csv",
        )

    total = sum(float(f.get("saldo_total") or 0) for f in filas)
    return render_template(
        "cartera/grupos.html",
        filas=filas, total=total, error=error,
    )


@cartera_bp.route("/cartera/controlc")
@requiere_login
@requiere_permiso("cartera.ver")
def controlc():
    """ITEM #4 — Vista de comparación cartera hoy vs snapshot anterior.

    Reemplazo de la PROCEDURE CONTROLC de dBase (MENU.PRG L1582-1660).
    `?fecha_snap=YYYY-MM-DD` para elegir snapshot específico; sin param
    toma el más reciente disponible que NO sea de hoy.
    """
    fecha_param = (request.args.get("fecha_snap") or "").strip() or None
    try:
        data = queries.comparar_contra_snapshot(fecha_param)
        snapshots = queries.snapshots_disponibles(limite=60)
        error = data.get("error")
    except Exception as e:  # noqa: BLE001
        data = {"filas": [], "fecha_snapshot": None, "totales": {}}
        snapshots = []
        error = str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            data.get("filas") or [],
            columnas=[
                ("codigo_cli", "Código"),
                ("nombre", "Cliente"),
                ("saldo_snapshot", "Saldo snapshot"),
                ("saldo_hoy", "Saldo hoy"),
                ("diferencia", "Diferencia"),
                ("n_facturas_snapshot", "N° fact snapshot"),
                ("n_facturas_hoy", "N° fact hoy"),
                ("delta_n", "Δ N° fact"),
            ],
            filename=f"cartera_controlc_{data.get('fecha_snapshot') or 'sin_snapshot'}.csv",
        )

    return render_template(
        "cartera/controlc.html",
        filas=data.get("filas") or [],
        fecha_snapshot=data.get("fecha_snapshot"),
        totales=data.get("totales") or {},
        snapshots=snapshots,
        error=error,
    )


@cartera_bp.route("/cartera/por-color")
@requiere_login
@requiere_permiso("cartera.ver")
def por_color():
    """ITEM #11 — Cruce cartera × color × cliente.

    Replica de la rama "ESTADISTICA" de INFORMES.PRG L830-918. Por las
    limitaciones del schema actual la fila por cliente sale SIN colores —
    queda flagueada como decisión humana (ver query).
    """
    meses = parse_int(request.args.get("meses")) or 3
    if meses < 1:
        meses = 1
    if meses > 24:
        meses = 24
    try:
        data = queries.cartera_por_cliente_y_color(meses_atras=meses)
        error = None
    except Exception as e:  # noqa: BLE001
        data = {
            "filas": [], "colores_orden": queries.COLORES_LEGACY,
            "kg_por_color_global": {}, "fuente_color": "n/a",
            "necesita_decision": True, "nota_decision": str(e),
            "meses_atras": meses, "fecha_desde": "",
        }
        error = str(e)

    if request.args.get("export") == "csv":
        # CSV simple: cliente + saldo + 14 columnas color en 0 (placeholder).
        columnas = [("codigo_cli", "Código"),
                    ("nombre", "Cliente"),
                    ("saldo_total", "Saldo total")]
        filas_csv = []
        for f in data.get("filas") or []:
            row = {"codigo_cli": f["codigo_cli"], "nombre": f["nombre"],
                   "saldo_total": f["saldo_total"]}
            for col in data.get("colores_orden") or []:
                row[col] = (f.get("colores") or {}).get(col, 0)
            filas_csv.append(row)
        for col in data.get("colores_orden") or []:
            columnas.append((col, col))
        return csv_response(filas_csv, columnas=columnas,
                            filename="cartera_por_color.csv")

    return render_template(
        "cartera/por_color.html",
        filas=data.get("filas") or [],
        colores_orden=data.get("colores_orden") or [],
        kg_por_color_global=data.get("kg_por_color_global") or {},
        fuente_color=data.get("fuente_color"),
        necesita_decision=data.get("necesita_decision", False),
        nota_decision=data.get("nota_decision") or "",
        meses_atras=data.get("meses_atras", meses),
        fecha_desde=data.get("fecha_desde", ""),
        error=error,
    )


@cartera_bp.route("/cartera/stop-automatico", methods=["POST"])
@requiere_login
@requiere_permiso("stop_cliente.editar")
def stop_automatico():
    """Marca en STOP a todo cliente con facturas vencidas > umbral_dias.

    Confirmación: un form con un campo `umbral_dias` (default 90) y un
    `confirmar=1` hidden que obliga a pasar dos veces. La primera vista
    muestra qué clientes serían afectados; la segunda ejecuta.
    """
    umbral = parse_int(request.form.get("umbral_dias")) or 90
    if umbral < 1:
        umbral = 90

    confirmar = (request.form.get("confirmar") or "").strip() == "1"
    if not confirmar:
        # Preview — no cambios, muestra quién sería afectado y pide confirmar.
        try:
            candidatos = queries.clientes_con_vencido(umbral)
        except Exception as e:
            flash_exc("No pude calcular el preview", e)
            return redirect(url_for("cartera.aging"))
        return render_template(
            "cartera/stop_preview.html",
            candidatos=candidatos,
            umbral_dias=umbral,
        )

    # Confirmado — ejecutar.
    try:
        usuario = (g.user or {}).get("username", "web")
        resultado = queries.aplicar_stop_automatico(umbral, usuario=usuario)
    except Exception as e:
        flash_exc("No pude aplicar stop automático", e)
        return redirect(url_for("cartera.aging"))

    n = resultado["n"]
    if n == 0:
        flash("Ningún cliente cumple el criterio — nada que hacer.", "info")
    else:
        codigos = ", ".join(resultado["codigos"][:8])
        extra = "" if n <= 8 else f" (+{n - 8} más)"
        flash(
            f"{n} cliente(s) pasaron a STOP por mora > {umbral}d: {codigos}{extra}.",
            "ok",
        )
    return redirect(url_for("cartera.aging"))
