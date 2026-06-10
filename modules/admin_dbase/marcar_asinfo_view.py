"""Endpoint /admin/marcar-asinfo-hoy — utility one-off para marcar como
'asinfo-backfill' las facturas / compras / dolares cargados HOY que
quedaron con usuario_crea del current user.

GET: dry-run, muestra qué se marcaría.
POST aplicar=1: ejecuta los UPDATEs.

TMT 2026-06-10: el fix forward marca las nuevas cargas correctamente.
Este endpoint cubre el legacy retroactivo. Cubre 3 tablas:
 - scintela.factura (cargadas via /facturas/cargar-desde-asinfo*)
 - scintela.compra  (cargadas via importaciones / manual con marker Asinfo)
 - scintela.dolares (anticipos USD si los hay)
"""
from __future__ import annotations

from flask import Blueprint, Response, render_template_string, request

import db
from auth import requiere_login, requiere_permiso

bp = Blueprint("marcar_asinfo", __name__, url_prefix="/admin/marcar-asinfo-hoy")


_PAGE = """\
<!doctype html>
<html lang=es><head><meta charset=utf-8><title>Marcar facturas/compras Asinfo</title>
<style>
  body{font-family:system-ui;max-width:1200px;margin:24px auto;padding:0 12px}
  table{border-collapse:collapse;width:100%;font-size:12px;margin-top:8px}
  th,td{border:1px solid #ddd;padding:3px 6px;text-align:right}
  th:nth-child(3),td:nth-child(3),th:nth-child(7),td:nth-child(7),th:nth-child(8),td:nth-child(8){text-align:left}
  h2{margin-top:24px;border-top:1px solid #ccc;padding-top:12px}
  .ok{background:#e7f5e8;padding:8px;border:1px solid #6c6;border-radius:4px}
  .warn{background:#fff7d8;padding:8px;border:1px solid #cb8;border-radius:4px}
  button{padding:8px 16px;font-weight:bold;background:#b00;color:#fff;border:0;
         border-radius:4px;cursor:pointer;margin-top:12px}
  .stats{font-size:14px;margin:8px 0}
  code{background:#eee;padding:2px 6px;border-radius:3px}
</style>
</head><body>
<h1>Marcar Asinfo retroactivo (facturas + compras + dolares)</h1>
<p>Cleanup one-off: marca con <code>usuario_crea='asinfo-backfill'</code> filas
cargadas via Asinfo manual HOY que quedaron con usuario actual. El filtro
NO_BACKFILL_WHERE de los reports live las excluye hasta el cierre mensual.</p>

<form method=POST>
  {% if aplicado %}
  <div class=ok>
    <b>✓ Aplicado.</b> Facturas: {{ n_aplicado_factura }} · Compras: {{ n_aplicado_compra }} · Dolares: {{ n_aplicado_dolares }}.
  </div>
  {% elif n_total == 0 %}
  <div class=ok>Nada para marcar. Salí.</div>
  {% else %}
  <div class=warn>
    <b>Dry-run.</b> {{ n_total }} candidatos. SUM saldo facturas:
    <b>${{ '%.2f'|format(sum_factura) }}</b>, SUM importe compras:
    <b>${{ '%.2f'|format(sum_compra) }}</b>, SUM importe dolares:
    <b>${{ '%.2f'|format(sum_dolares) }}</b>.
  </div>
  <button type=submit name=aplicar value=1
    onclick="return confirm('Confirmar UPDATE de {{ n_total }} filas?');">
    APLICAR UPDATE ({{ n_total }} filas)
  </button>
  {% endif %}
</form>

<h2>Facturas candidatas ({{ n_factura }})</h2>
<table>
  <thead><tr>
    <th>id_factura</th><th>fecha</th><th>fecha_crea</th><th>codigo_cli</th>
    <th>importe</th><th>saldo</th><th>usuario_crea</th><th>numf_completo</th>
  </tr></thead>
  <tbody>
    {% for r in rows_factura %}
    <tr>
      <td>{{ r.id_factura }}</td><td>{{ r.fecha }}</td>
      <td>{{ r.fecha_crea.strftime('%Y-%m-%d %H:%M') if r.fecha_crea else '' }}</td>
      <td>{{ r.codigo_cli }}</td>
      <td>{{ '%.2f'|format(r.importe or 0) }}</td>
      <td>{{ '%.2f'|format(r.saldo or 0) }}</td>
      <td>{{ r.usuario_crea or '' }}</td>
      <td>{{ r.numf_completo or '' }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<h2>Compras candidatas ({{ n_compra }})</h2>
<table>
  <thead><tr>
    <th>id_compra</th><th>fecha</th><th>fecha_crea</th><th>codigo_prov</th>
    <th>tipo</th><th>kg</th><th>importe</th><th>usuario_crea</th><th>concepto</th>
  </tr></thead>
  <tbody>
    {% for r in rows_compra %}
    <tr>
      <td>{{ r.id_compra }}</td><td>{{ r.fecha }}</td>
      <td>{{ r.fecha_crea.strftime('%Y-%m-%d %H:%M') if r.fecha_crea else '' }}</td>
      <td>{{ r.codigo_prov or '' }}</td>
      <td>{{ r.tipo or '' }}</td>
      <td>{{ '%.2f'|format(r.kg or 0) }}</td>
      <td>{{ '%.2f'|format(r.importe or 0) }}</td>
      <td>{{ r.usuario_crea or '' }}</td>
      <td>{{ (r.concepto or '')[:40] }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<h2>Dolares (anticipos) candidatos ({{ n_dolares }})</h2>
<table>
  <thead><tr>
    <th>id_dolares</th><th>fecha</th><th>fecha_crea</th><th>cta</th>
    <th>importe</th><th>usuario_crea</th><th>concepto</th><th>comprobante</th>
  </tr></thead>
  <tbody>
    {% for r in rows_dolares %}
    <tr>
      <td>{{ r.id_dolares }}</td><td>{{ r.fecha }}</td>
      <td>{{ r.fecha_crea.strftime('%Y-%m-%d %H:%M') if r.fecha_crea else '' }}</td>
      <td>{{ r.cta or '' }}</td>
      <td>{{ '%.2f'|format(r.importe or 0) }}</td>
      <td>{{ r.usuario_crea or '' }}</td>
      <td>{{ (r.concepto or '')[:40] }}</td>
      <td>{{ r.comprobante or '' }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<p style="margin-top:24px"><a href="/informes/balance">← Volver a balance</a></p>
</body></html>
"""


# Filtros canónicos por tabla
_WHERE_FACTURA = """
    fecha_crea >= (CURRENT_DATE - INTERVAL '1 day')
    AND COALESCE(usuario_crea, '') NOT IN ('asinfo-backfill', 'dbf-import')
    AND numf_completo IS NOT NULL
    AND numf_completo ~ '^[0-9]{3}-[0-9]{3}-[0-9]{9}$'
"""
# Para compras: filtramos por fecha_crea hoy (carga reciente) + usuario_crea
# distinto al marker. Sin restricción de formato porque scintela.compra no
# tiene un campo equivalente a numf_completo. La intención es capturar las
# cargas masivas Asinfo del día.
_WHERE_COMPRA = """
    fecha_crea >= (CURRENT_DATE - INTERVAL '1 day')
    AND COALESCE(usuario_crea, '') NOT IN ('asinfo-backfill', 'dbf-import')
"""
_WHERE_DOLARES = """
    fecha_crea >= (CURRENT_DATE - INTERVAL '1 day')
    AND COALESCE(usuario_crea, '') NOT IN ('asinfo-backfill', 'dbf-import')
"""


def _safe_count(sql_where: str, table: str, sum_col: str) -> dict:
    try:
        row = db.fetch_one(
            f"SELECT COUNT(*) AS n, COALESCE(SUM({sum_col}), 0) AS s "
            f"FROM scintela.{table} WHERE {sql_where}",
        )
        return {"n": int(row.get("n") or 0), "s": float(row.get("s") or 0)} if row else {"n": 0, "s": 0}
    except Exception:
        return {"n": 0, "s": 0}


def _safe_rows(sql_where: str, table: str, cols: str, order: str) -> list:
    try:
        return db.fetch_all(
            f"SELECT {cols} FROM scintela.{table} WHERE {sql_where} "
            f"ORDER BY {order} LIMIT 30"
        ) or []
    except Exception:
        return []


@bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def index() -> Response:
    aplicado = False
    n_aplicado_factura = n_aplicado_compra = n_aplicado_dolares = 0

    if request.method == "POST" and request.form.get("aplicar") == "1":
        with db.tx() as conn:
            for table, where in (
                ("factura", _WHERE_FACTURA),
                ("compra", _WHERE_COMPRA),
                ("dolares", _WHERE_DOLARES),
            ):
                try:
                    db.execute(
                        f"UPDATE scintela.{table} "
                        f"SET usuario_crea = 'asinfo-backfill' WHERE {where}",
                        (), conn=conn,
                    )
                except Exception:
                    pass
            # Counts after update
            for table, key in (
                ("factura", "n_aplicado_factura"),
                ("compra", "n_aplicado_compra"),
                ("dolares", "n_aplicado_dolares"),
            ):
                try:
                    row = db.fetch_one(
                        f"SELECT COUNT(*) AS n FROM scintela.{table} "
                        f"WHERE fecha_crea >= (CURRENT_DATE - INTERVAL '1 day') "
                        f"AND usuario_crea = 'asinfo-backfill'",
                        (), conn=conn,
                    )
                    if key == "n_aplicado_factura":
                        n_aplicado_factura = int(row["n"] or 0) if row else 0
                    elif key == "n_aplicado_compra":
                        n_aplicado_compra = int(row["n"] or 0) if row else 0
                    elif key == "n_aplicado_dolares":
                        n_aplicado_dolares = int(row["n"] or 0) if row else 0
                except Exception:
                    pass
        aplicado = True

    # After update (if any), candidates should be 0
    f_stats = _safe_count(_WHERE_FACTURA, "factura", "saldo")
    c_stats = _safe_count(_WHERE_COMPRA, "compra", "importe")
    d_stats = _safe_count(_WHERE_DOLARES, "dolares", "importe")

    rows_factura = _safe_rows(
        _WHERE_FACTURA, "factura",
        "id_factura, fecha, fecha_crea, codigo_cli, importe, saldo, usuario_crea, numf_completo",
        "fecha_crea DESC, id_factura DESC",
    )
    rows_compra = _safe_rows(
        _WHERE_COMPRA, "compra",
        "id_compra, fecha, fecha_crea, codigo_prov, tipo, kg, importe, usuario_crea, concepto",
        "fecha_crea DESC, id_compra DESC",
    )
    rows_dolares = _safe_rows(
        _WHERE_DOLARES, "dolares",
        "id_dolares, fecha, fecha_crea, cta, importe, usuario_crea, concepto, comprobante",
        "fecha_crea DESC, id_dolares DESC",
    )

    return Response(
        render_template_string(
            _PAGE,
            aplicado=aplicado,
            n_aplicado_factura=n_aplicado_factura,
            n_aplicado_compra=n_aplicado_compra,
            n_aplicado_dolares=n_aplicado_dolares,
            n_factura=f_stats["n"],
            n_compra=c_stats["n"],
            n_dolares=d_stats["n"],
            n_total=f_stats["n"] + c_stats["n"] + d_stats["n"],
            sum_factura=f_stats["s"],
            sum_compra=c_stats["s"],
            sum_dolares=d_stats["s"],
            rows_factura=rows_factura,
            rows_compra=rows_compra,
            rows_dolares=rows_dolares,
        ),
        mimetype="text/html",
    )
