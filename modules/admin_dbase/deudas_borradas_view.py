"""Pantalla /admin/deudas-borradas — vuelve a poner las deudas de proveedores
que desaparecieron de `scintela.posdat` sin haberse pagado.

Tamara 2026-09-03: el 01/09/2026 a las 06:00 EC el cron del día 1 corrió por
última vez la procedure VIEJA `scintela.procesa_provisiones` (herencia del
dBase, ya borrada por la migración 0240). Además de cargar el mes completo
de provisiones, esa procedure hacía

    DELETE FROM scintela.posdat WHERE fechad < hoy AND num <> 0

sin mirar `banc`: borró todas las posdat vencidas con número — las pagadas
(historia) y 19 VIVAS (banc=0) por $28.233,27 de tejedores y químicos
(M REYES ×11, AQ ×3, A PONCE, R UNDA ×2, PO, QC). En el dBase el mismo
DELETE era inofensivo porque el sync diario volvía a traer las filas; sin
dBase, quedaron borradas. Efecto: +28.233 de utilidad falsa en septiembre,
19 deudas que ya no figuran en /posdat, y /compras mostrando esas compras
como "Pagada: Sí" (esa columna deduce el pago de que NO quede deuda viva).

Cómo se detecta, sin hardcodear ids: toda compra a crédito deja en
`mov_doble` un `compra_a_posdat` (o `compra_saldo_a_posdat`) que apunta al
posdat que creó. Si ese posdat YA NO EXISTE, la compra no está anulada, no se
pagó al contado (`no_banco`, `cuenta_pagada`, `id_transaccion` vacíos) y
ningún otro movimiento tocó ese posdat después (pago, anulación,
restauración), la deuda se borró por fuera de la app. Eso es lo que la
pantalla lista y, con confirmación, vuelve a crear con los MISMOS datos de la
compra (fecha, vencimiento, proveedor, número, importe, concepto), banc=0.

Cada restauración deja un `mov_doble` tipo `posdat_restaurada`
(compra → posdat nuevo, metadata con el id borrado): así queda en /historial
y la pantalla no la vuelve a ofrecer. Todo o nada. Sólo `usuarios.admin`.

El health `deudas_desaparecidas` de /admin/health/all usa la MISMA consulta
(`detectar()`) para avisar si vuelve a pasar.
"""
from __future__ import annotations

from flask import Blueprint, Response, g, render_template_string, request

import db
import mov_doble as _md
from auth import requiere_login, requiere_permiso
from filters import today_ec

bp = Blueprint(
    "deudas_borradas", __name__,
    url_prefix="/admin/deudas-borradas",
)

TIPO_MD = "posdat_restaurada"

_SQL_DETECTAR = """
SELECT m.id_mov_doble, m.destino_id AS id_posdat_borrado,
       m.importe AS importe_md, m.fecha_operacion,
       c.id_compra, c.fecha, c.fechad, c.codigo_prov, c.numero,
       c.importe, c.concepto, c.clave, c.kg
  FROM scintela.mov_doble m
  JOIN scintela.compra c ON c.id_compra = m.origen_id
 WHERE m.tipo IN ('compra_a_posdat', 'compra_saldo_a_posdat')
   AND m.origen_table = 'compra'
   AND m.destino_table = 'posdat'
   AND m.estado = 'activo'
   -- el posdat que la compra creó ya no está
   AND NOT EXISTS (SELECT 1 FROM scintela.posdat p
                    WHERE p.id_posdat = m.destino_id)
   -- la compra sigue viva y no se pagó al contado
   AND COALESCE(c.stat, '') <> 'Y'
   AND COALESCE(c.no_banco, 0) = 0
   AND COALESCE(btrim(c.cuenta_pagada), '') = ''
   AND c.id_transaccion IS NULL
   -- nadie más tocó ese posdat (pago, anulación, restauración previa)
   AND NOT EXISTS (SELECT 1 FROM scintela.mov_doble o
                    WHERE o.id_mov_doble <> m.id_mov_doble
                      AND ((o.destino_table = 'posdat' AND o.destino_id = m.destino_id)
                        OR (o.origen_table = 'posdat' AND o.origen_id = m.destino_id)
                        OR (o.tipo = %(tipo_rest)s AND o.origen_table = 'compra'
                            AND o.origen_id = c.id_compra)))
 ORDER BY c.codigo_prov, c.numero, c.id_compra
"""


def detectar() -> list[dict]:
    """Las deudas borradas por fuera de la app. Vacío = todo en orden."""
    return db.fetch_all(_SQL_DETECTAR, {"tipo_rest": TIPO_MD}) or []


def resumen() -> dict:
    """Para el health: cuántas y cuánto suman."""
    filas = detectar()
    return {
        "n": len(filas),
        "total": round(sum(float(f.get("importe") or 0) for f in filas), 2),
        "proveedores": sorted({(f.get("codigo_prov") or "").strip() for f in filas}),
    }


def restaurar(filas: list[dict], usuario: str) -> list[dict]:
    """Vuelve a crear las posdat de `filas` (salida de `detectar()`), todo o
    nada. Devuelve lo GUARDADO (RETURNING), no lo que se quiso guardar."""
    creadas: list[dict] = []
    with db.tx() as conn:
        for f in filas:
            row = db.execute_returning(
                """
                INSERT INTO scintela.posdat
                    (fecha, fechad, prov, num, importe, concepto, banc, clave,
                     usuario_crea)
                VALUES (%(fecha)s, %(fechad)s, %(prov)s, %(num)s, %(importe)s,
                        %(concepto)s, 0, %(clave)s, %(usuario)s)
                RETURNING id_posdat, fecha, fechad, prov, num, importe, concepto
                """,
                {
                    "fecha": f["fecha"],
                    "fechad": f.get("fechad") or f["fecha"],
                    "prov": (f["codigo_prov"] or "").upper().strip(),
                    "num": f.get("numero") or 0,
                    "importe": f["importe"],
                    "concepto": (f.get("concepto") or "")[:50] or None,
                    "clave": ((f.get("clave") or None) and str(f["clave"])[:3]),
                    "usuario": f"restaurar-deudas:{usuario}"[:50],
                },
                conn=conn,
            ) or {}
            if not row.get("id_posdat"):
                raise RuntimeError(
                    f"No se pudo crear la deuda de la compra #{f['id_compra']}"
                )
            _md.registrar(
                conn=conn,
                tipo=TIPO_MD,
                origen_table="compra",
                origen_id=f["id_compra"],
                destino_table="posdat",
                destino_id=row["id_posdat"],
                importe=f["importe"],
                fecha=today_ec(),
                concepto=(
                    f"Deuda restaurada {f['codigo_prov']} #{f.get('numero') or ''} "
                    f"— borrada por procesa_provisiones el 01/09/2026"
                )[:200],
                usuario=usuario,
                metadata={
                    "id_posdat_borrado": f.get("id_posdat_borrado"),
                    "codigo_prov": f.get("codigo_prov"),
                    "numero_compra": f.get("numero"),
                },
            )
            creadas.append(row)
    return creadas


_PAGE = """\
<!doctype html>
<html lang=es><head><meta charset=utf-8><title>Deudas borradas</title>
<style>
  body{font-family:system-ui;max-width:1100px;margin:24px auto;padding:0 12px;color:#0f172a}
  table{border-collapse:collapse;width:100%;font-size:13px;margin-top:12px}
  th,td{border:1px solid #ddd;padding:4px 8px;text-align:right;white-space:nowrap}
  th.l,td.l{text-align:left;white-space:normal}
  .ok{background:#e7f5e8;padding:8px;border:1px solid #6c6;border-radius:4px}
  .bad{background:#fee;padding:8px;border:1px solid #f00;border-radius:4px}
  .nota{color:#475569;font-size:13px}
  button{padding:8px 16px;font-weight:bold;background:#0a0;color:#fff;border:0;
         border-radius:4px;cursor:pointer;margin-top:12px}
</style>
</head><body>
<h1>Deudas de proveedores que desaparecieron sin pagarse</h1>
<p class="nota">El 01/09/2026 a las 06:00 la procedure vieja del cierre de mes
borró de Posdatados las deudas vencidas con número, sin mirar si estaban
pagadas. Acá se listan las que quedaron sin deuda y sin pago: la compra
existe, no está anulada, no se pagó al contado y ningún cheque ni débito la
canceló. Al confirmar, se vuelven a crear con los mismos datos de la compra.</p>

{% if error %}<div class="bad"><b>Error:</b> {{ error }} — no se cambió nada.</div>{% endif %}

{% if creadas %}
<div class="ok">
  <b>Listo.</b> Se volvieron a poner {{ creadas|length }} deudas por
  ${{ '{:,.2f}'.format(total_creado) }}. Ya están en <a href="/posdat">Posdatados</a>
  y en el Historial.
  <table>
    <thead><tr><th>Posdat</th><th class="l">Proveedor</th><th>N°</th><th>Fecha</th>
      <th>Vence</th><th>Importe</th><th class="l">Concepto</th></tr></thead>
    <tbody>
    {% for r in creadas %}
      <tr><td>{{ r.id_posdat }}</td><td class="l">{{ r.prov }}</td><td>{{ r.num }}</td>
        <td>{{ r.fecha }}</td><td>{{ r.fechad }}</td>
        <td>{{ '{:,.2f}'.format(r.importe) }}</td><td class="l">{{ r.concepto }}</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endif %}

{% if not filas %}
  {% if not creadas %}<div class="ok"><b>No hay deudas borradas.</b> Todas las compras a crédito tienen su deuda viva, su pago o su anulación.</div>{% endif %}
{% else %}
  <table>
    <thead><tr><th>Compra</th><th class="l">Proveedor</th><th>N°</th><th>Fecha</th>
      <th>Vencía</th><th>Importe</th><th class="l">Concepto</th><th>Posdat borrado</th></tr></thead>
    <tbody>
    {% for f in filas %}
      <tr>
        <td><a href="/compras/{{ f.id_compra }}">#{{ f.id_compra }}</a></td>
        <td class="l">{{ f.codigo_prov }}</td><td>{{ f.numero }}</td>
        <td>{{ f.fecha }}</td><td>{{ f.fechad }}</td>
        <td>{{ '{:,.2f}'.format(f.importe) }}</td>
        <td class="l">{{ f.concepto }}</td><td>{{ f.id_posdat_borrado }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  <p>Total: <b>${{ '{:,.2f}'.format(total) }}</b> en {{ filas|length }} deudas.
     Al volver a ponerlas, el pasivo sube ese monto y la utilidad del mes baja lo mismo.</p>
  <form method=POST>
    <input type=hidden name=csrf_token value="{{ csrf_token() }}">
    <button type=submit name=restaurar value=1
      onclick="return confirm('Volver a poner {{ filas|length }} deudas por ${{ '{:,.2f}'.format(total) }} en Posdatados?');">
      VOLVER A PONER LAS DEUDAS
    </button>
  </form>
{% endif %}

<p class="nota" style="margin-top:20px">Las deudas cargadas a mano en Posdatados
(sin compra) no se pueden reconstruir desde acá: hay que cargarlas de nuevo en
<a href="/posdat">Posdatados</a>.</p>
<p style="margin-top:24px"><a href="/posdat">Volver a posdatados</a> ·
   <a href="/compras">Compras</a></p>
</body></html>
"""


@bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def index() -> Response:
    error: str | None = None
    creadas: list[dict] = []

    if request.method == "POST" and request.form.get("restaurar") == "1":
        filas = detectar()
        if not filas:
            error = "No quedó nada por restaurar."
        else:
            usuario = (getattr(g, "user", None) or {}).get("username") or "web"
            try:
                creadas = restaurar(filas, usuario)
            except Exception as e:  # noqa: BLE001
                error = str(e)

    filas = detectar()
    total = round(sum(float(f.get("importe") or 0) for f in filas), 2)
    total_creado = round(sum(float(r.get("importe") or 0) for r in creadas), 2)
    return Response(
        render_template_string(
            _PAGE, filas=filas, total=total, error=error,
            creadas=creadas, total_creado=total_creado,
        ),
        mimetype="text/html",
    )
