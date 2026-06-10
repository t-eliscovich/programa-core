"""Endpoint /admin/regenerar-snapshot — borra + recrea el snapshot mensual.

TMT 2026-06-10. Problema: el snapshot 31/05 (PATANT) está congelado con
los datos del momento de creación. Si las queries del balance cambian
(ej. se quita el filtro `asinfo-backfill`), el snapshot ya guardado queda
inconsistente con el balance live → utilidad = patr_live − patant_stale
infla por el delta.

GET muestra preview de snapshots del mes seleccionado. POST aplicar=1
borra los snapshots del mes target + invoca `crear_snapshot_historia()`
con el código actual.

Read-only por default (GET). Sólo `usuarios.admin`.
"""
from __future__ import annotations

from flask import Blueprint, Response, render_template_string, request

import db
from auth import requiere_login, requiere_permiso
from filters import today_ec

bp = Blueprint(
    "regen_snapshot", __name__, url_prefix="/admin/regenerar-snapshot"
)


_PAGE = """\
<!doctype html>
<html lang=es><head><meta charset=utf-8><title>Regenerar snapshot</title>
<style>
  body{font-family:system-ui;max-width:1100px;margin:24px auto;padding:0 12px}
  table{border-collapse:collapse;width:100%;font-size:13px;margin-top:12px}
  th,td{border:1px solid #ddd;padding:4px 8px;text-align:right}
  th:nth-child(2),td:nth-child(2),th:nth-child(8),td:nth-child(8){text-align:left}
  .ok{background:#e7f5e8;padding:8px;border:1px solid #6c6;border-radius:4px}
  .warn{background:#fff7d8;padding:8px;border:1px solid #cb8;border-radius:4px}
  button{padding:8px 16px;font-weight:bold;background:#b00;color:#fff;border:0;
         border-radius:4px;cursor:pointer;margin-top:12px}
  button.restore{background:#0a0;font-weight:bold;color:#fff}
  input[type=number]{padding:4px 8px;width:80px}
  form.inline{display:inline-block}
  code{background:#eee;padding:2px 6px;border-radius:3px}
</style>
</head><body>
<h1>Regenerar snapshot de scintela.historia</h1>

<div style="background:#fee;border:1px solid #f00;padding:8px;margin:8px 0;border-radius:4px">
  <b>RESTAURAR snapshot 2026-05-31 original (id 205, perdido por bug
  balance_components_as_of)</b><br>
  Si el snapshot del 31/05 quedó con patrimonio bajo ($15M) tras un regen
  fallido, este botón lo restaura con los valores conocidos del original
  (patrimonio=20469347).
  <form method=POST class=inline>
    <input type=hidden name=restore_205 value=1>
    <button class=restore type=submit
      onclick="return confirm('Restaurar snapshot 31/05 a valores del id=205?');">
      RESTAURAR 2026-05-31 (id 205)
    </button>
  </form>
</div>

<div style="background:#e7f5e8;border:1px solid #6c6;padding:8px;margin:8px 0;border-radius:4px">
  <b>AJUSTAR snapshot 31/05 con backfill de mayo (opción A — utilidad PC == dBase)</b><br>
  Suma al snapshot del 31/05 los saldos de facturas con
  <code>usuario_crea='asinfo-backfill'</code> y <code>fecha &le; 2026-05-31</code>.
  Esas son las facturas que en dBase ya estaban al cierre 31/05 pero PC
  trajo después vía Asinfo manual. Sin este ajuste, PC infla utilidad
  (~$500k) vs dBase. <b>Idempotente</b>: si lo corrés 2x, no suma 2 veces
  (lee el cart actual y agrega solo el delta del backfill que aún no se
  computa).
  <form method=POST class=inline>
    <input type=hidden name=ajustar_backfill_31_05 value=1>
    <button type=submit style="background:#0a0;color:#fff"
      onclick="return confirm('Ajustar snapshot 31/05 sumando backfill de mayo?');">
      AJUSTAR 2026-05-31 con backfill de mayo
    </button>
  </form>
</div>
<p>Borra los snapshots del mes target en <code>scintela.historia</code> y
crea uno nuevo con el código actual. Útil cuando cambian las queries del
balance (ej. revert de filtros) y el snapshot queda desincronizado.</p>

<form method=GET style="margin-bottom:16px">
  Año: <input type="number" name="anio" value="{{ anio }}" min="2020" max="2030">
  Mes: <input type="number" name="mes" value="{{ mes }}" min="1" max="12">
  <button type=submit style="background:#345;font-weight:normal">Mirar</button>
</form>

<h2>Snapshots actuales en {{ anio }}-{{ '%02d'|format(mes) }}</h2>
{% if snapshots %}
<table>
  <thead><tr>
    <th>id</th><th>fecha</th><th>patrimonio</th><th>cart</th><th>banco</th>
    <th>ustock</th><th>fecha_crea</th><th>usuario_crea</th>
  </tr></thead>
  <tbody>
    {% for s in snapshots %}
    <tr>
      <td>{{ s.id_historia }}</td>
      <td>{{ s.fecha }}</td>
      <td>{{ '%.2f'|format(s.patrimonio or 0) }}</td>
      <td>{{ '%.2f'|format(s.cart or 0) }}</td>
      <td>{{ '%.2f'|format(s.banco or 0) }}</td>
      <td>{{ '%.2f'|format(s.ustock or 0) }}</td>
      <td>{{ s.fecha_crea.strftime('%Y-%m-%d %H:%M') if s.fecha_crea else '' }}</td>
      <td>{{ s.usuario_crea or '' }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p><i>No hay snapshots para ese mes todavía.</i></p>
{% endif %}

<form method=POST style="margin-top:24px">
  <input type=hidden name=anio value="{{ anio }}">
  <input type=hidden name=mes value="{{ mes }}">

  {% if aplicado %}
  <div class=ok>
    <b>✓ Regenerado.</b> Borradas {{ n_borrados }} filas, creada 1 nueva
    (id={{ id_nuevo }}, patrimonio=${{ '%.2f'|format(patrimonio_nuevo) }}).
  </div>
  {% elif error %}
  <div class=warn><b>Error:</b> {{ error }}</div>
  {% else %}
  <div class=warn>
    <b>Acción destructiva:</b> esto va a borrar {{ snapshots|length }} fila(s) de
    <code>scintela.historia</code> del mes y recalcular el snapshot del último
    día del mes con las queries actuales. NO se hace backup automático.
  </div>
  <button type=submit name=aplicar value=1
    onclick="return confirm('Borrar {{ snapshots|length }} snapshots de {{ anio }}-{{ mes }} y recrear?');">
    REGENERAR SNAPSHOT {{ anio }}-{{ '%02d'|format(mes) }}
  </button>
  {% endif %}
</form>

<p style="margin-top:24px"><a href="/informes/balance">← Volver a balance</a></p>
</body></html>
"""


@bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def index() -> Response:
    hoy = today_ec()
    try:
        anio = int(request.values.get("anio") or hoy.year)
        mes = int(request.values.get("mes") or hoy.month)
    except (TypeError, ValueError):
        anio, mes = hoy.year, hoy.month
    mes = max(1, min(mes, 12))

    aplicado = False
    n_borrados = 0
    id_nuevo: int | None = None
    patrimonio_nuevo = 0.0
    error: str | None = None

    # Botón "ajustar_backfill_31_05" — TMT decisión 2026-06-10 opción A.
    # Suma al cart/patrimonio del snapshot 31/05 los saldos de las facturas
    # backfill que tienen fecha <= 31/05 (= ventas de mayo que en dBase ya
    # estaban al cierre, pero PC trajo después vía Asinfo manual).
    # IDEMPOTENTE: usa un marker en sistema_meta para no doble-aplicar.
    if request.method == "POST" and request.form.get("ajustar_backfill_31_05") == "1":
        try:
            with db.tx() as conn:
                # 1. Computar el saldo de backfill que pertenece a mayo.
                row = db.fetch_one(
                    """
                    SELECT COALESCE(SUM(saldo), 0) AS s
                      FROM scintela.factura
                     WHERE COALESCE(usuario_crea, '') = 'asinfo-backfill'
                       AND fecha <= '2026-05-31'::date
                       AND (stat IS NULL OR stat IN ('Z','A','',' '))
                       AND COALESCE(saldo, 0) > 0
                    """, conn=conn,
                ) or {}
                backfill_mayo_saldo = float(row.get("s") or 0)

                # 2. Marker idempotente en sistema_meta.
                meta_key = "snapshot_31_05_backfill_ajuste"
                meta_row = db.fetch_one(
                    "SELECT valor FROM scintela.sistema_meta WHERE clave = %s",
                    (meta_key,), conn=conn,
                )
                ya_aplicado = float(meta_row["valor"]) if meta_row else 0.0
                delta_a_sumar = backfill_mayo_saldo - ya_aplicado

                # 3. Buscar el snapshot 31/05 más reciente.
                snap_row = db.fetch_one(
                    """
                    SELECT id_historia, cart, patrimonio
                      FROM scintela.historia
                     WHERE fecha = '2026-05-31'::date
                     ORDER BY id_historia DESC LIMIT 1
                    """, conn=conn,
                )
                if not snap_row:
                    error = "No hay snapshot 31/05 en scintela.historia"
                else:
                    target_id = int(snap_row["id_historia"])
                    cart_actual = float(snap_row.get("cart") or 0)
                    patr_actual = float(snap_row.get("patrimonio") or 0)

                    # 4. UPDATE solo el delta (idempotente).
                    if abs(delta_a_sumar) > 0.01:
                        db.execute(
                            """
                            UPDATE scintela.historia
                               SET cart = cart + %s,
                                   patrimonio = patrimonio + %s
                             WHERE id_historia = %s
                            """,
                            (delta_a_sumar, delta_a_sumar, target_id),
                            conn=conn,
                        )
                    # 5. Actualizar marker.
                    if meta_row:
                        db.execute(
                            "UPDATE scintela.sistema_meta SET valor = %s WHERE clave = %s",
                            (str(backfill_mayo_saldo), meta_key),
                            conn=conn,
                        )
                    else:
                        db.execute(
                            """
                            INSERT INTO scintela.sistema_meta (clave, valor)
                            VALUES (%s, %s)
                            """,
                            (meta_key, str(backfill_mayo_saldo)),
                            conn=conn,
                        )
                    aplicado = True
                    n_borrados = 0
                    id_nuevo = target_id
                    patrimonio_nuevo = patr_actual + delta_a_sumar
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    # Botón "restore_205" — restaura el snapshot 31/05 con valores hardcoded
    # del id=205 que se perdió cuando regen falló por balance_components_as_of
    # incompleto. TMT 2026-06-10 fix de emergencia.
    elif request.method == "POST" and request.form.get("restore_205") == "1":
        try:
            with db.tx() as conn:
                # Borrar TODOS los snapshots del 2026-05
                db.execute(
                    """
                    DELETE FROM scintela.historia
                     WHERE EXTRACT(YEAR FROM fecha) = 2026
                       AND EXTRACT(MONTH FROM fecha) = 5
                    """,
                    conn=conn,
                )
                # INSERT con valores = HISTORIA.DBF 31/05 EXACTOS (verificado
                # campo a campo 2026-06-10; retiro=0 como el dBase — antes
                # tenía 241600 duplicado de usret, error de transcripción)
                # (vistos en /informes/balance/utilidad-debug pre-regen)
                res = db.execute_returning(
                    """
                    INSERT INTO scintela.historia
                        (fecha, stock, kcom, ktej, ktin, ustock, uqui, kvent,
                         uvent, costo, ucom, utej, utin, gasto, gstotal,
                         banco, cart, deuda, retiro, patrimonio, anticipos,
                         dolar, maquinaria, realty, usret, usuti,
                         fecha_crea, usuario_crea)
                    VALUES ('2026-05-31'::date,
                            2323544, 173823, 331207, 329103, 7689579,
                            232546, 295688, 2538201, 3074260, 472036,
                            253886, 297291, 255884, 807061,
                            2600053, 7055192, 2150418, 0,
                            20469347, 1493681, 0,
                            1140800, 2407914, 241600, 595061,
                            CURRENT_TIMESTAMP, 'restore-original-205')
                    RETURNING id_historia
                    """,
                    conn=conn,
                )
                id_nuevo = int(res["id_historia"]) if res else None
                patrimonio_nuevo = 20469347.0
                aplicado = True
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    elif request.method == "POST" and request.form.get("aplicar") == "1":
        try:
            from modules.informes import queries as iq
            with db.tx() as conn:
                # 1. Borrar TODOS los snapshots del mes target
                rows = db.fetch_all(
                    """
                    SELECT id_historia FROM scintela.historia
                     WHERE EXTRACT(YEAR FROM fecha) = %s
                       AND EXTRACT(MONTH FROM fecha) = %s
                    """,
                    (anio, mes), conn=conn,
                ) or []
                n_borrados = len(rows)
                if n_borrados:
                    db.execute(
                        """
                        DELETE FROM scintela.historia
                         WHERE EXTRACT(YEAR FROM fecha) = %s
                           AND EXTRACT(MONTH FROM fecha) = %s
                        """,
                        (anio, mes), conn=conn,
                    )
            # 2. Recrear (fuera de la tx del DELETE — crear_snapshot_historia
            #    abre su propia tx con advisory lock)
            res = iq.crear_snapshot_historia(anio, mes, usuario="regen-admin")
            if res.get("aplicado"):
                aplicado = True
                id_nuevo = res.get("id_historia")
                # Re-fetch para el patrimonio nuevo
                row = db.fetch_one(
                    "SELECT patrimonio FROM scintela.historia WHERE id_historia = %s",
                    (id_nuevo,),
                ) or {}
                patrimonio_nuevo = float(row.get("patrimonio") or 0)
            else:
                error = f"Re-creación falló: {res.get('razon')}"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    # Snapshot listing
    snapshots = db.fetch_all(
        """
        SELECT id_historia, fecha, patrimonio, cart, banco, ustock,
               fecha_crea, usuario_crea
          FROM scintela.historia
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND EXTRACT(MONTH FROM fecha) = %s
         ORDER BY fecha DESC, id_historia DESC
        """,
        (anio, mes),
    ) or []

    return Response(
        render_template_string(
            _PAGE,
            anio=anio, mes=mes,
            snapshots=snapshots,
            aplicado=aplicado,
            n_borrados=n_borrados,
            id_nuevo=id_nuevo,
            patrimonio_nuevo=patrimonio_nuevo,
            error=error,
        ),
        mimetype="text/html",
    )
