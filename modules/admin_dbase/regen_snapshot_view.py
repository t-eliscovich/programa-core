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

import calendar
from datetime import date

from flask import Blueprint, Response, render_template_string, request

import db
from auth import requiere_login, requiere_permiso
from filters import today_ec


def _fecha_cierre_de(anio: int, mes: int) -> date:
    """Último día de (anio, mes) — la única fecha que `crear_snapshot_historia`
    escribe. Tamara 2026-09-01: extraído para que el DELETE de "REGENERAR
    SNAPSHOT" borre por esta fecha EXACTA, nunca por año/mes (ver el
    incidente en el docstring de la vista)."""
    return date(anio, mes, calendar.monthrange(anio, mes)[1])

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
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
    <input type=hidden name=restore_205 value=1>
    <button class=restore type=submit
      onclick="return confirm('Restaurar snapshot 31/05 a valores del id=205?');">
      RESTAURAR 2026-05-31 (id 205)
    </button>
  </form>
</div>

<div style="background:#e7f5e8;border:1px solid #6c6;padding:8px;margin:8px 0;border-radius:4px">
  <div style="border:2px solid #2563eb;background:#eff6ff;padding:12px;margin:10px 0;border-radius:6px">
    <b>ANCLAR cierre 30/06 a los valores del dBase (PATANT correcto)</b><br>
    Junio es un cierre histórico: su cartera ya no se puede reconstruir desde PC
    (las facturas se cobraron después). Este botón fija el 30/06 a los valores
    EXACTOS del dBase (patrimonio=20.785.914). De acá en más las fotos diarias
    lo evitan.
    <form method="post" style="margin-top:6px" onsubmit="return confirm('Anclar 30/06 a 20.785.914 (borra los snapshots de junio y crea el correcto)?');">
      <input type=hidden name=csrf_token value="{{ csrf_token() }}">
      <input type="hidden" name="restore_junio_dbase" value="1">
      <button type="submit" style="background:#2563eb;color:#fff;padding:6px 12px;border:0;border-radius:4px;font-weight:bold">
        ANCLAR 2026-06-30 al dBase (20.785.914)
      </button>
    </form>
  </div>

  <div style="border:2px solid #b45309;background:#fffbeb;padding:12px;margin:10px 0;border-radius:6px">
    <b>ANCLAR cierre 31/08 a los ultimos valores conocidos (incidente regen 2026-09-01)</b><br>
    El 31/08 se cerro un dia tarde (despues de medianoche), asi que el cierre cayo en la rama
    <code>informe_balance_as_of</code>. Esa rama tiene dos problemas conocidos para un mes que
    ya paso: la cartera reconstruida no cierra bien, y <code>_flujos_vivos_del_mes</code>
    devuelve <code>{}</code> por esa rama (gasto/gstotal/kcom/ktej/ktin salen en cero -- el
    mismo bug que ya afecto julio 2026). Un click sobre REGENERAR SNAPSHOT 2026-08 ya borro
    las 31 fotos diarias de agosto y dejo una fila con patrimonio $19.055.150 y utilidad
    NEGATIVA -$1.877.445 -- visiblemente rota.<br><br>
    Este boton restaura patrimonio/cartera/banco/stock/deuda/retiros/utilidad a los ultimos
    valores buenos conocidos (foto diaria id=510 del 2026-09-01 01:05, cruzada contra el
    calculo independiente del simulacro-cierre -- ambos coinciden). Las columnas de DETALLE
    (kcom/ktej/ktin/ucom/utej/utin/gasto/gstotal/kvent/uvent/costo/stock/anticipos/dolar/
    maquinaria/realty) no tienen fuente independiente para agosto: se completan con los
    valores de julio (id=296) como placeholder -- mejor que dejarlos en cero, pero
    <b>no son el dato real de agosto</b>. Si alguien vio la pantalla completa antes del
    incidente (Andres guardo el snapshot id=508 a las 22:20 del 31/08), esos valores de
    detalle se pueden corregir a mano despues.<br><br>
    <b>Tamara 2026-09-01:</b> 10 de esas columnas (kcom/ucom/ktej/utej/ktin/utin/gasto/
    gstotal/kvent/uvent) YA tienen una reconstruccion real mas abajo en esta misma
    pantalla ("Detalle real de {{ anio }}-{{ '%02d'|format(mes) }}"), sacada de compras/
    xgast/factura/tinto filtrados por fecha a agosto -- no julio. stock/anticipos/
    maquinaria/realty/costo siguen sin fuente independiente (mismo problema que la
    cartera).
    <form method="post" style="margin-top:6px">
      <input type=hidden name=csrf_token value="{{ csrf_token() }}">
      <input type="hidden" name="restore_agosto_manual" value="1">
      <button type="submit" style="background:#b45309;color:#fff;padding:6px 12px;border:0;border-radius:4px;font-weight:bold">
        ANCLAR 2026-08-31 (patrimonio $21.732.772,07)
      </button>
    </form>
  </div>

  <b>AJUSTAR snapshot 31/05 con backfill de mayo (opción A — utilidad PC == dBase)</b><br>
  Suma al snapshot del 31/05 los saldos de facturas con
  <code>usuario_crea='asinfo-backfill'</code> y <code>fecha &le; 2026-05-31</code>.
  Esas son las facturas que en dBase ya estaban al cierre 31/05 pero PC
  trajo después vía Asinfo manual. Sin este ajuste, PC infla utilidad
  (~$500k) vs dBase. <b>Idempotente</b>: si lo corrés 2x, no suma 2 veces
  (lee el cart actual y agrega solo el delta del backfill que aún no se
  computa).
  <form method=POST class=inline>
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
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

{% if detalle_preview %}
<h2>Detalle real de {{ anio }}-{{ '%02d'|format(mes) }} (kcom/ucom/ktej/utej/ktin/utin/gasto/gstotal/kvent/uvent)</h2>
<p>Reconstruido de las tablas propias de PC (compras/xgast/factura/tinto) filtradas
por fecha a ese mes -- NO julio, NO un placeholder. No toca patrimonio, cartera,
banco, stock, deuda ni utilidad: <b>sólo estas 10 columnas de detalle.</b></p>
<table>
  <thead><tr><th>Campo</th><th>En historia hoy</th><th>Reconstruido</th><th style="text-align:left">Fuente</th></tr></thead>
  <tbody>
    {% for campo, etiqueta in [("kcom","Materia prima (kg)"), ("ucom","Materia prima ($)"),
                                ("ktej","Tejeduría (kg)"), ("utej","Tejeduría ($)"),
                                ("ktin","Colorantes (kg)"), ("utin","Tintorería, gastos de proceso ($)"),
                                ("gasto","Administración ($)"), ("gstotal","Total gastos ($)"),
                                ("kvent","Ventas (kg)"), ("uvent","Ventas ($)")] %}
    <tr>
      <td style="text-align:left">{{ etiqueta }}</td>
      <td>{{ '%.2f'|format((detalle_actual or {}).get(campo) or 0) }}</td>
      <td><b>{{ '%.2f'|format(detalle_preview.campos[campo]) }}</b></td>
      <td style="text-align:left;font-size:11px;color:#555">
        {% if campo in ("kcom","ucom") %}{{ detalle_preview.fuente["ucom/kcom"] }}
        {% elif campo in ("ktej","utej") %}{{ detalle_preview.fuente["utej/ktej"] }}
        {% elif campo == "utin" %}{{ detalle_preview.fuente["utin"] }}
        {% elif campo == "ktin" %}{{ detalle_preview.fuente["ktin"] }}
        {% elif campo in ("gasto","gstotal") %}{{ detalle_preview.fuente["gasto/gstotal"] }}
        {% else %}{{ detalle_preview.fuente["kvent/uvent"] }}
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<form method=POST style="margin-top:12px">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=hidden name=anio value="{{ anio }}">
  <input type=hidden name=mes value="{{ mes }}">
  <input type=hidden name=anio_detalle value="{{ anio }}">
  <input type=hidden name=mes_detalle value="{{ mes }}">

  {% if aplicado_detalle %}
  <div class=ok>
    <b>✓ Detalle aplicado.</b> Fila id={{ id_detalle_nuevo }}: kcom={{ '%.2f'|format(detalle_aplicado.kcom) }},
    ucom={{ '%.2f'|format(detalle_aplicado.ucom) }}, ktej={{ '%.2f'|format(detalle_aplicado.ktej) }},
    utej={{ '%.2f'|format(detalle_aplicado.utej) }}, ktin={{ '%.2f'|format(detalle_aplicado.ktin) }},
    utin={{ '%.2f'|format(detalle_aplicado.utin) }}, gasto={{ '%.2f'|format(detalle_aplicado.gasto) }},
    gstotal={{ '%.2f'|format(detalle_aplicado.gstotal) }}, kvent={{ '%.2f'|format(detalle_aplicado.kvent) }},
    uvent={{ '%.2f'|format(detalle_aplicado.uvent) }}. Patrimonio/utilidad NO se tocaron
    (patrimonio={{ '%.2f'|format(detalle_aplicado.patrimonio) }}, usuti={{ '%.2f'|format(detalle_aplicado.usuti) }}).
  </div>
  {% elif error_detalle %}
  <div class=warn><b>Error:</b> {{ error_detalle }}</div>
  {% else %}
  <button type=submit name=aplicar_detalle_real value=1 style="background:#0a0"
    onclick="return confirm('Pisar SOLO las 10 columnas de detalle del cierre {{ anio }}-{{ '%02d'|format(mes) }} con estos valores reconstruidos? No toca patrimonio ni cartera.');">
    APLICAR DETALLE REAL DE {{ anio }}-{{ '%02d'|format(mes) }}
  </button>
  {% endif %}
</form>
{% endif %}

{% if balance_fijo_preview %}
<h2>Balance fijo real de {{ anio }}-{{ '%02d'|format(mes) }} (maquinaria/realty/stock)</h2>
<p>Maquinaria y realty: <code>SUM(inicial-amortizac)</code> de <code>scintela.activos</code> --
sólo válido porque la amortización de este mes YA corrió (si no, esta sección no aparece).
Stock (kg): de <code>scintela.iniciales</code>, la misma fuente que ya usa el balance live.
<b>Anticipos NO se reconstruye</b> (depende del cruce vivo con Asinfo, mismo problema que la
cartera) -- si hay un valor verificado para este mes cargado en el código
(<code>_ANTICIPOS_VERIFICADOS</code>), se aplica también; si no, se deja como está.</p>
<table>
  <thead><tr><th>Campo</th><th>En historia hoy</th><th>Reconstruido</th><th style="text-align:left">Fuente</th></tr></thead>
  <tbody>
    {% for campo, etiqueta in [("maquinaria","Maquinaria/equipo ($)"), ("realty","Terrenos/edificios ($)"),
                                ("stock","Stock hilado+tejido+terminado (kg)")] %}
    <tr>
      <td style="text-align:left">{{ etiqueta }}</td>
      <td>{{ '%.2f'|format((balance_fijo_actual or {}).get(campo) or 0) }}</td>
      <td><b>{{ '%.2f'|format(balance_fijo_preview.campos[campo]) }}</b></td>
      <td style="text-align:left;font-size:11px;color:#555">{{ balance_fijo_preview.fuente[campo] }}</td>
    </tr>
    {% endfor %}
    <tr>
      <td style="text-align:left">Anticipos ($)</td>
      <td colspan=3 style="text-align:left">
        en historia hoy: {{ '%.2f'|format((balance_fijo_actual or {}).get('anticipos') or 0) }} —
        sin fuente reconstruible; se aplica sólo si hay un valor verificado
        para este mes en <code>_ANTICIPOS_VERIFICADOS</code> (código)
      </td>
    </tr>
  </tbody>
</table>

<form method=POST style="margin-top:12px">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
  <input type=hidden name=anio value="{{ anio }}">
  <input type=hidden name=mes value="{{ mes }}">
  <input type=hidden name=anio_balance value="{{ anio }}">
  <input type=hidden name=mes_balance value="{{ mes }}">

  {% if aplicado_balance_fijo %}
  <div class=ok>
    <b>✓ Balance fijo aplicado.</b> Fila id={{ id_balance_fijo_nuevo }}:
    maquinaria={{ '%.2f'|format(balance_fijo_aplicado.maquinaria) }},
    realty={{ '%.2f'|format(balance_fijo_aplicado.realty) }},
    stock={{ '%.2f'|format(balance_fijo_aplicado.stock) }},
    anticipos={{ '%.2f'|format(balance_fijo_aplicado.anticipos) }}.
    Patrimonio/utilidad NO se tocaron
    (patrimonio={{ '%.2f'|format(balance_fijo_aplicado.patrimonio) }}, usuti={{ '%.2f'|format(balance_fijo_aplicado.usuti) }}).
  </div>
  {% elif error_balance_fijo %}
  <div class=warn><b>Error:</b> {{ error_balance_fijo }}</div>
  {% else %}
  <button type=submit name=aplicar_balance_fijo_real value=1 style="background:#0a0"
    onclick="return confirm('Pisar maquinaria/realty/stock (y anticipos si hay valor verificado en el código) del cierre {{ anio }}-{{ '%02d'|format(mes) }}? No toca patrimonio ni cartera.');">
    APLICAR BALANCE FIJO REAL DE {{ anio }}-{{ '%02d'|format(mes) }}
  </button>
  {% endif %}
</form>
{% elif balance_fijo_razon %}
<p style="color:#a60;font-size:13px">Balance fijo de {{ anio }}-{{ '%02d'|format(mes) }}: {{ balance_fijo_razon }}</p>
{% endif %}

<form method=POST style="margin-top:24px">
  <input type=hidden name=csrf_token value="{{ csrf_token() }}">
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
    <b>Acción destructiva:</b> esto va a borrar SÓLO la fila del cierre
    (fecha exacta = último día de {{ anio }}-{{ '%02d'|format(mes) }}, si
    existe) y recalcular esa fila con las queries actuales. NO toca las
    fotos diarias de otros días del mes. NO se hace backup automático.
    Si el mes ya pasó y hoy no es el mismo día del cierre, cae en la rama
    <code>as_of</code> (aproximada) salvo que esté dentro de los 2 días de
    gracia — ver el docstring de <code>crear_snapshot_historia</code>.
  </div>
  <button type=submit name=aplicar value=1
          onclick="return confirm('Regenerar el cierre de {{ anio }}-{{ \'%02d\'|format(mes) }}? Esto borra y recalcula esa fila con las queries actuales.');">
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

    # Estado del botón "aplicar_detalle_real" (detalle real de un mes cerrado).
    aplicado_detalle = False
    id_detalle_nuevo: int | None = None
    detalle_aplicado: dict | None = None
    error_detalle: str | None = None

    # Estado del botón "aplicar_balance_fijo_real" (maquinaria/realty/stock
    # de un mes cerrado + anticipos a mano, ver historia_balance_fijo_mes_cerrado).
    aplicado_balance_fijo = False
    id_balance_fijo_nuevo: int | None = None
    balance_fijo_aplicado: dict | None = None
    error_balance_fijo: str | None = None
    balance_fijo_razon: str | None = None

    # Anticipos verificados por mes -- SIN reconstrucción propia posible
    # (depende del cruce vivo con Asinfo, mismo problema que la cartera).
    # Tamara 2026-09-01: "esto hay que hacer por backend, no que el usuario
    # pueda hacer" -- no es un campo para completar desde la pantalla, es
    # un dato de código, igual que los demás números de las ANCLAR de
    # arriba. Fuente: "Vista previa cierre 31-08-2026.pdf" (verificado
    # contra Total Activo − Pasivo = Patrimonio, exacto).
    _ANTICIPOS_VERIFICADOS = {
        (2026, 8): 3183858.00,
    }

    # Materia Prima (kcom/ucom) -- Tamara 2026-09-02 "no es algo menor":
    # probado en vivo que NINGUNA reconstrucción automática cuadra para un
    # mes ya cerrado (ver el docstring largo en historia_detalle_mes_cerrado:
    # el SUM crudo repite recargos de importación, y kg_hilado_mes -- que
    # debería arreglarlo -- da PEOR porque su índice de importaciones es el
    # estado ACTUAL de Asinfo, no el de agosto). Misma categoría que
    # anticipos: se completa con el valor verificado de un papel externo.
    # Fuente: "Vista previa cierre 31-08-2026.pdf", tabla COMPRAS HILADO.
    _MATERIA_PRIMA_VERIFICADA = {
        (2026, 8): {"kcom": 408972.0, "ucom": 1289746.0},
    }

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
                # Tamara 2026-09-01 — borrar SOLO la fila del cierre (fecha
                # exacta), nunca el mes entero: un DELETE por año/mes se
                # lleva puestas también las fotos diarias intermedias (el
                # mismo bug que el 2026-09-01 borró las 31 fotos de agosto).
                db.execute(
                    """
                    DELETE FROM scintela.historia
                     WHERE fecha = '2026-05-31'::date
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

    # Botón "restore_junio_dbase" — ancla el cierre 30/06 a los valores EXACTOS
    # del HISTORIA.DBF del dBase (verificado campo a campo 2026-07-01, tarball
    # 20:44). Junio es histórico: su cartera ya no se puede reconstruir desde PC
    # (las facturas se cobraron después), así que el único cierre correcto es el
    # que el dBase capturó a tiempo. De acá en más las FOTOS DIARIAS
    # (crear_snapshot_diario) evitan tener que hacer esto.
    elif request.method == "POST" and request.form.get("restore_junio_dbase") == "1":
        try:
            with db.tx() as conn:
                # Tamara 2026-09-01 — fecha exacta, no el mes entero (ver
                # comentario en restore_205 más arriba).
                db.execute(
                    """
                    DELETE FROM scintela.historia
                     WHERE fecha = '2026-06-30'::date
                    """,
                    conn=conn,
                )
                res = db.execute_returning(
                    """
                    INSERT INTO scintela.historia
                        (fecha, stock, kcom, ktej, ktin, ustock, uqui, kvent,
                         uvent, costo, ucom, utej, utin, gasto, gstotal,
                         banco, cart, deuda, retiro, patrimonio, anticipos,
                         dolar, maquinaria, realty, usret, usuti,
                         fecha_crea, usuario_crea)
                    VALUES ('2026-06-30'::date,
                            2231673, 259866, 306315, 341884, 7410428, 144637,
                            335700, 2858932, 3491906, 772762, 119940, 360290,
                            294944, 775174, 3083974, 7249666, 3137367, 0,
                            20785914, 2535063, 0, 1106050, 2393464, 320578,
                            637145, CURRENT_TIMESTAMP, 'ancla-dbase-30-06')
                    RETURNING id_historia
                    """,
                    conn=conn,
                )
                id_nuevo = int(res["id_historia"]) if res else None
                patrimonio_nuevo = 20785914.0
                aplicado = True
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    # Botón "restore_agosto_manual" — ancla el cierre 31/08 a los últimos
    # valores buenos conocidos. TMT 2026-09-01: un click sobre REGENERAR
    # SNAPSHOT 2026-08 (con la sesión ya sin confirm()) corrió un día tarde
    # (después de medianoche), cayó en la rama `informe_balance_as_of` y
    # produjo un patrimonio $2,7M por debajo de lo real y utilidad NEGATIVA
    # — la cartera reconstruida por esa rama no cierra bien para un mes que
    # ya pasó (mismo síntoma que documenta /admin/health/simulacro-cierre) y
    # `_flujos_vivos_del_mes` devuelve {} por esa rama (mismo bug que ya
    # afectó julio 2026: gasto/gstotal/kcom/ktej/ktin en cero). patrimonio/
    # cart/banco/ustock/usret/usuti son los de la foto diaria id=510 del
    # 2026-09-01 01:05 (snapshot-diario), cruzados contra el cálculo
    # independiente del simulacro-cierre — coinciden. deuda/uqui vienen de
    # ese mismo cálculo independiente. Las columnas de detalle
    # (kcom/ktej/ktin/ucom/utej/utin/gasto/gstotal/kvent/uvent/costo/stock/
    # anticipos/dolar/maquinaria/realty) NO tienen fuente independiente para
    # agosto — se completan con julio (id=296) como placeholder, mejor que
    # dejarlas en cero pero NO son el dato real de agosto.
    elif request.method == "POST" and request.form.get("restore_agosto_manual") == "1":
        try:
            with db.tx() as conn:
                # Tamara 2026-09-01 — fecha exacta, no el mes entero (ver
                # comentario en restore_205 más arriba — este es justo el
                # botón que arregla el incidente causado por ESE bug).
                db.execute(
                    """
                    DELETE FROM scintela.historia
                     WHERE fecha = '2026-08-31'::date
                    """,
                    conn=conn,
                )
                res = db.execute_returning(
                    """
                    INSERT INTO scintela.historia
                        (fecha, stock, kcom, ktej, ktin, ustock, uqui, kvent,
                         uvent, costo, ucom, utej, utin, gasto, gstotal,
                         banco, cart, deuda, retiro, patrimonio, anticipos,
                         dolar, maquinaria, realty, usret, usuti,
                         fecha_crea, usuario_crea)
                    VALUES ('2026-08-31'::date,
                            2506522.5, 332304.71, 285828.86, 274359.44,
                            8727036.69, 341307.24, 332304.71, 2832518.84,
                            3491906.0, 1003590.85, 139003.75, 381612.43,
                            318659.25, 839275.43,
                            1675042.40, 7758369.89, 3355956.28, 149003.95,
                            21732772.07, 2159970.67,
                            0.0, 1072300.0, 2379014.0,
                            149003.95, 651173.41,
                            CURRENT_TIMESTAMP, 'ancla-agosto-manual-2026-09-01')
                    RETURNING id_historia
                    """,
                    conn=conn,
                )
                id_nuevo = int(res["id_historia"]) if res else None
                patrimonio_nuevo = 21732772.07
                aplicado = True
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    # Botón "aplicar_detalle_real" — Tamara 2026-09-01: "no de julio. del
    # cierre. busca y ponelo bien" — pisa SÓLO las 10 columnas de DETALLE
    # (kcom/ucom/ktej/utej/ktin/utin/gasto/gstotal/kvent/uvent) de la fila
    # YA ANCLADA de un mes cerrado, con el dato real reconstruido por
    # `historia_detalle_mes_cerrado` (tablas propias de PC filtradas por
    # fecha a ese mes). Deliberadamente NO toca patrimonio/cart/banco/
    # stock/deuda/anticipos/dolar/maquinaria/realty/usret/usuti/costo/
    # ustock/uqui/retiro -- esos ya están anclados a los últimos valores
    # buenos conocidos (ver el botón ANCLAR de arriba) y reconstruirlos
    # "como estaban tal día" tiene el mismo problema sin resolver que la
    # cartera.
    elif request.method == "POST" and request.form.get("aplicar_balance_fijo_real") == "1":
        try:
            from modules.informes import queries as iq

            anio_b = int(request.form.get("anio_balance") or anio)
            mes_b = int(request.form.get("mes_balance") or mes)
            rec = iq.historia_balance_fijo_mes_cerrado(anio_b, mes_b)
            if not rec.get("ok"):
                error_balance_fijo = rec.get("razon", "no se pudo reconstruir")
            else:
                campos = dict(rec["campos"])
                _antic = _ANTICIPOS_VERIFICADOS.get((anio_b, mes_b))
                _fecha_cierre_b = _fecha_cierre_de(anio_b, mes_b)
                if _antic is not None:
                    campos["anticipos"] = _antic
                    res = db.execute_returning(
                        """
                        UPDATE scintela.historia
                           SET maquinaria = %(maquinaria)s, realty = %(realty)s,
                               stock = %(stock)s, anticipos = %(anticipos)s
                         WHERE fecha = %(fecha)s
                        RETURNING id_historia, maquinaria, realty, stock,
                                  anticipos, patrimonio, usuti
                        """,
                        {**campos, "fecha": _fecha_cierre_b},
                    )
                else:
                    # Sin valor verificado para este mes -- no se toca
                    # anticipos, sólo lo que sí sabemos reconstruir.
                    res = db.execute_returning(
                        """
                        UPDATE scintela.historia
                           SET maquinaria = %(maquinaria)s, realty = %(realty)s,
                               stock = %(stock)s
                         WHERE fecha = %(fecha)s
                        RETURNING id_historia, maquinaria, realty, stock,
                                  anticipos, patrimonio, usuti
                        """,
                        {**campos, "fecha": _fecha_cierre_b},
                    )
                if res:
                    id_balance_fijo_nuevo = int(res["id_historia"])
                    balance_fijo_aplicado = dict(res)
                    aplicado_balance_fijo = True
                else:
                    error_balance_fijo = f"no hay fila de historia con fecha = {_fecha_cierre_b} (¿está anclado el cierre?)"
        except Exception as e:
            error_balance_fijo = f"{type(e).__name__}: {e}"

    elif request.method == "POST" and request.form.get("aplicar_detalle_real") == "1":
        try:
            from modules.informes import queries as iq

            anio_d = int(request.form.get("anio_detalle") or anio)
            mes_d = int(request.form.get("mes_detalle") or mes)
            rec = iq.historia_detalle_mes_cerrado(anio_d, mes_d)
            if not rec.get("ok"):
                error_detalle = rec.get("razon", "no se pudo reconstruir")
            else:
                campos = dict(rec["campos"])
                _mp_verif = _MATERIA_PRIMA_VERIFICADA.get((anio_d, mes_d))
                if _mp_verif:
                    campos["kcom"] = _mp_verif["kcom"]
                    campos["ucom"] = _mp_verif["ucom"]
                _fecha_cierre_d = _fecha_cierre_de(anio_d, mes_d)
                res = db.execute_returning(
                    """
                    UPDATE scintela.historia
                       SET kcom = %(kcom)s, ucom = %(ucom)s,
                           ktej = %(ktej)s, utej = %(utej)s,
                           ktin = %(ktin)s, utin = %(utin)s,
                           gasto = %(gasto)s, gstotal = %(gstotal)s,
                           kvent = %(kvent)s, uvent = %(uvent)s
                     WHERE fecha = %(fecha)s
                    RETURNING id_historia, kcom, ucom, ktej, utej, ktin,
                              utin, gasto, gstotal, kvent, uvent,
                              patrimonio, usuti
                    """,
                    {**campos, "fecha": _fecha_cierre_d},
                )
                if res:
                    id_detalle_nuevo = int(res["id_historia"])
                    detalle_aplicado = dict(res)
                    aplicado_detalle = True
                else:
                    error_detalle = f"no hay fila de historia con fecha = {_fecha_cierre_d} (¿está anclado el cierre?)"
        except Exception as e:
            error_detalle = f"{type(e).__name__}: {e}"

    elif request.method == "POST" and request.form.get("aplicar") == "1":
        try:
            from modules.informes import queries as iq

            # Tamara 2026-09-01 (incidente 2026-09-01) — borrar SOLO la fila
            # del cierre (fecha EXACTA = último día del mes), nunca el mes
            # entero: un DELETE por año/mes se lleva puestas también las
            # fotos diarias intermedias que haya en ese mes. Este mismo
            # DELETE-por-mes fue lo que borró las 31 fotos diarias de agosto
            # el día del incidente. `crear_snapshot_historia` sólo escribe
            # UNA fila (la del último día) de todos modos, así que borrar
            # más que esa fila nunca fue necesario.
            # Tamara 2026-09-02 — el borrado ya NO se hace acá.
            #
            # Hasta hoy esta pantalla borraba la fila del cierre en su propia
            # transacción y RECIÉN DESPUÉS llamaba a crear_snapshot_historia,
            # que abre otra. Entre el commit del DELETE y el INSERT,
            # `scintela.historia` se quedaba SIN el cierre — y `PATANT` (que es
            # `historia_ultimo_mes().patrimonio`) caía al cierre anterior, meses
            # más viejo y millones más bajo. Como `utilidad = patr − PATANT`, el
            # balance mostraba una utilidad inflada por esa diferencia a
            # cualquiera que lo abriera en esa ventana. Y la ventana no es
            # instantánea: recrear la foto llama a `informe_balance()`, que sale
            # a Asinfo y a formulas_app.
            #
            # Pasó el 02/09/2026: entre las 08:02 y las 08:20 —18 minutos— la
            # utilidad estuvo en +1.971.282 en vez de −709.403. Se ve en
            # /informes/traza como un Δ de ±2,68 M sin nada atribuido.
            #
            # `crear_snapshot_historia(forzar=True)` hace el DELETE y el INSERT
            # DENTRO de una sola transacción con advisory lock, que es
            # exactamente lo que hacía falta: nunca hay un instante sin cierre.
            # El borrado de acá era la forma de conseguir el efecto de `forzar`
            # sin pasarlo; pasarlo es mejor y no deja hueco.
            _fecha_cierre = _fecha_cierre_de(anio, mes)
            n_borrados = len(db.fetch_all(
                "SELECT id_historia FROM scintela.historia WHERE fecha = %s",
                (_fecha_cierre,),
            ) or [])  # sólo para el reporte: quién se va a pisar.
            res = iq.crear_snapshot_historia(
                anio, mes, usuario="regen-admin", forzar=True
            )
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

    # Vista previa del detalle reconstruido (SOLO lectura -- no escribe
    # nada). Tamara 2026-09-01: mirar antes de aplicar. Se intenta siempre
    # que (anio, mes) sea un mes ya cerrado; si no, queda en None y la
    # sección no se muestra.
    detalle_preview = None
    detalle_actual = None
    try:
        from modules.informes import queries as iq

        _rec = iq.historia_detalle_mes_cerrado(anio, mes)
        if _rec.get("ok"):
            _mp_verif_preview = _MATERIA_PRIMA_VERIFICADA.get((anio, mes))
            if _mp_verif_preview:
                _rec = dict(_rec, campos={**_rec["campos"], **_mp_verif_preview})
                _rec["fuente"] = dict(_rec["fuente"])
                _rec["fuente"]["ucom/kcom"] = (
                    "Vista previa cierre 31-08-2026.pdf (tabla COMPRAS HILADO) -- "
                    "sin reconstrucción automática confiable, ver docstring"
                )
            detalle_preview = _rec
            _row_actual = db.fetch_one(
                """
                SELECT id_historia, kcom, ucom, ktej, utej, ktin, utin,
                       gasto, gstotal, kvent, uvent
                  FROM scintela.historia
                 WHERE fecha = %s
                """,
                (_fecha_cierre_de(anio, mes),),
            )
            detalle_actual = dict(_row_actual) if _row_actual else None
    except Exception:  # noqa: BLE001 -- vista previa, nunca romper la pantalla
        detalle_preview = None

    # Vista previa del balance fijo (maquinaria/realty/stock) — mismo patrón
    # que detalle_preview de arriba, sólo lectura.
    balance_fijo_preview = None
    balance_fijo_actual = None
    try:
        from modules.informes import queries as iq

        _recb = iq.historia_balance_fijo_mes_cerrado(anio, mes)
        if _recb.get("ok"):
            balance_fijo_preview = _recb
            _row_b = db.fetch_one(
                """
                SELECT id_historia, maquinaria, realty, stock, anticipos
                  FROM scintela.historia
                 WHERE fecha = %s
                """,
                (_fecha_cierre_de(anio, mes),),
            )
            balance_fijo_actual = dict(_row_b) if _row_b else None
        else:
            balance_fijo_razon = _recb.get("razon")
    except Exception:  # noqa: BLE001 -- vista previa, nunca romper la pantalla
        balance_fijo_preview = None

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
            detalle_preview=detalle_preview,
            detalle_actual=detalle_actual,
            aplicado_detalle=aplicado_detalle,
            id_detalle_nuevo=id_detalle_nuevo,
            detalle_aplicado=detalle_aplicado,
            error_detalle=error_detalle,
            balance_fijo_preview=balance_fijo_preview,
            balance_fijo_actual=balance_fijo_actual,
            balance_fijo_razon=balance_fijo_razon,
            aplicado_balance_fijo=aplicado_balance_fijo,
            id_balance_fijo_nuevo=id_balance_fijo_nuevo,
            balance_fijo_aplicado=balance_fijo_aplicado,
            error_balance_fijo=error_balance_fijo,
        ),
        mimetype="text/html",
    )
