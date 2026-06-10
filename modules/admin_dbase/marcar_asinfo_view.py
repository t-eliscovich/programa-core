"""Endpoint /admin/marcar-asinfo-hoy — utility one-off para marcar como
'asinfo-backfill' las facturas cargadas vía /facturas/cargar-desde-asinfo*
que quedaron con usuario_crea del current user (bug histórico pre-bba9c57).

GET: dry-run, muestra qué se marcaría.
POST aplicar=1: ejecuta el UPDATE.

TMT 2026-06-10: el fix forward (commit bba9c57) marca las nuevas cargas
correctamente. Este endpoint cubre el legacy retroactivo. Excluido del
sync normal — es un cleanup manual cuando se detecta cartera/utilidad
inflada por backfill no marcado.
"""
from __future__ import annotations

from flask import Blueprint, Response, render_template_string, request

import db
from auth import requiere_login, requiere_permiso

bp = Blueprint("marcar_asinfo", __name__, url_prefix="/admin/marcar-asinfo-hoy")


_PAGE = """\
<!doctype html>
<html lang=es><head><meta charset=utf-8><title>Marcar facturas Asinfo</title>
<style>
  body{font-family:system-ui;max-width:900px;margin:24px auto;padding:0 12px}
  table{border-collapse:collapse;width:100%;font-size:13px;margin-top:12px}
  th,td{border:1px solid #ddd;padding:4px 8px;text-align:right}
  th:nth-child(3),td:nth-child(3),th:nth-child(7),td:nth-child(7){text-align:left}
  .ok{background:#e7f5e8;padding:8px;border:1px solid #6c6;border-radius:4px}
  .warn{background:#fff7d8;padding:8px;border:1px solid #cb8;border-radius:4px}
  button{padding:8px 16px;font-weight:bold;background:#b00;color:#fff;border:0;
         border-radius:4px;cursor:pointer;margin-top:12px}
  .stats{font-size:14px;margin:8px 0}
  code{background:#eee;padding:2px 6px;border-radius:3px}
</style>
</head><body>
<h1>Marcar facturas Asinfo retroactivo</h1>
<p>Cleanup one-off: marca con <code>usuario_crea='asinfo-backfill'</code> las
facturas que se cargaron via /facturas/cargar-desde-asinfo* HOY y quedaron con
el usuario actual en vez del marker. Esto sirve para que los filtros
NO_BACKFILL_WHERE de los reports live (TOTF, calcular_kpis) las excluyan
hasta el cierre mensual.</p>

<form method=POST>
  <p class=stats>
    Encontradas <b>{{ n_total }}</b> facturas candidatas a marcar,
    SUM(saldo) = <b>${{ '%.2f'|format(sum_saldo) }}</b>.
  </p>

  {% if aplicado %}
  <div class=ok>
    <b>✓ Aplicado.</b> {{ n_aplicado }} filas actualizadas.
  </div>
  {% elif n_total == 0 %}
  <div class=ok>Nada para marcar. Salí.</div>
  {% else %}
  <div class=warn>
    <b>Dry-run.</b> Revisá el preview abajo. Si se ve OK, apretá el botón
    para aplicar el UPDATE en transacción.
  </div>
  <button type=submit name=aplicar value=1
    onclick="return confirm('¿Confirmar UPDATE? Marca {{ n_total }} facturas con usuario_crea=asinfo-backfill.');">
    APLICAR UPDATE ({{ n_total }} filas)
  </button>
  {% endif %}
</form>

<h2>Preview (top 50)</h2>
<table>
  <thead>
    <tr>
      <th>id_factura</th><th>fecha</th><th>fecha_crea</th><th>codigo_cli</th>
      <th>importe</th><th>saldo</th><th>usuario_crea</th><th>numf_completo</th>
    </tr>
  </thead>
  <tbody>
    {% for r in rows %}
    <tr>
      <td>{{ r.id_factura }}</td>
      <td>{{ r.fecha }}</td>
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

<p style="margin-top:24px"><a href="/informes/balance">← Volver a balance</a></p>
</body></html>
"""


# Filtro canónico: facturas creadas HOY (UTC; el server corre UTC y la
# columna fecha_crea es timestamp default CURRENT_TIMESTAMP en UTC) que
# tienen un numf_completo con formato Asinfo (XXX-XXX-XXXXXXXXX, 15 caracteres
# con guiones en posiciones 4 y 8) y que NO están ya marcadas como backfill
# ni como dbf-import.
_WHERE_CANDIDATAS = """
    fecha_crea >= (CURRENT_DATE - INTERVAL '1 day')
    AND COALESCE(usuario_crea, '') NOT IN ('asinfo-backfill', 'dbf-import')
    AND numf_completo IS NOT NULL
    AND numf_completo ~ '^[0-9]{3}-[0-9]{3}-[0-9]{9}$'
"""


@bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def index() -> Response:
    aplicado = False
    n_aplicado = 0

    if request.method == "POST" and request.form.get("aplicar") == "1":
        with db.tx() as conn:
            db.execute(
                f"""
                UPDATE scintela.factura
                   SET usuario_crea = 'asinfo-backfill'
                 WHERE {_WHERE_CANDIDATAS}
                """,
                (),
                conn=conn,
            )
            # psycopg2 doesn't always return rowcount via db.execute; query again
            row = db.fetch_one(
                """
                SELECT COUNT(*) AS n
                  FROM scintela.factura
                 WHERE fecha_crea >= (CURRENT_DATE - INTERVAL '1 day')
                   AND usuario_crea = 'asinfo-backfill'
                """,
                (),
                conn=conn,
            )
            n_aplicado = int(row["n"] or 0) if row else 0
        aplicado = True

    # After update (if any), candidates should be 0
    stats = db.fetch_one(
        f"""
        SELECT COUNT(*) AS n, COALESCE(SUM(saldo), 0) AS sum_saldo
          FROM scintela.factura
         WHERE {_WHERE_CANDIDATAS}
        """,
    ) or {"n": 0, "sum_saldo": 0}

    rows = db.fetch_all(
        f"""
        SELECT id_factura, fecha, fecha_crea, codigo_cli,
               importe, saldo, usuario_crea, numf_completo
          FROM scintela.factura
         WHERE {_WHERE_CANDIDATAS}
         ORDER BY fecha_crea DESC, id_factura DESC
         LIMIT 50
        """,
    ) or []

    return Response(
        render_template_string(
            _PAGE,
            aplicado=aplicado,
            n_aplicado=n_aplicado,
            n_total=int(stats.get("n") or 0),
            sum_saldo=float(stats.get("sum_saldo") or 0),
            rows=rows,
        ),
        mimetype="text/html",
    )
