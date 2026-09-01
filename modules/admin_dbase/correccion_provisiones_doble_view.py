"""Endpoint /admin/correccion-provisiones-doble — corrige el doble cobro de
provisiones YY/RT del 01/09/2026.

Tamara 2026-09-01: el cron del día 1 (`scripts/procesa_provisiones_mensual.py`)
todavía llamaba a la procedure VIEJA `CALL scintela.procesa_provisiones(fecha)`
(carga el MES COMPLETO de las 12 provisiones YY/RT de un saque) al mismo
tiempo que `persistir_acumulacion_yy()` (`modules/posdat/queries.py`) — el
motor ÚNICO de acumulación YY/RT desde la decisión de la dueña del
2026-06-10, que corre solo (lazy, en cada request a /informes/balance o
/posdat, sin cron). Las dos rutas escriben sobre el MISMO
`scintela.posdat.importe`, en el MISMO sentido — resultado: cada una de las
12 provisiones se cargó DOS VECES el 01/09/2026 (mes completo + día),
sumando $724.275,00 de más a la deuda (Pasivos) y hundiendo la utilidad del
día en ese monto sin que fuera negocio real.

`scripts/procesa_provisiones_mensual.py` ya se corrigió (se sacó la tarea
vieja del cron, no se va a repetir). Este endpoint corrige el DATO que ya
quedó mal cargado: resta de cada una de las 12 filas YY/RT el monto exacto
que se cargó de más (el "mes completo" viejo), dejando sólo la cuota DIARIA
correcta ($24.142,50 en total) que persistir_acumulacion_yy() sí debía
aplicar hoy.

Los montos a restar están hardcodeados porque son un hecho histórico
puntual (lo que se cargó de más el 01/09/2026, ni un centavo más) — no una
fórmula que deba recalcularse cada vez que se corra este endpoint. Matchea
por `clave_canonica_yy` (prov + prefijo de concepto), no por texto exacto,
para sobrevivir si alguien edita el concepto.

Idempotente: marca `sistema_meta.correccion_provisiones_doble_2026_09_01`
al aplicar; si ya está marcada, GET y POST muestran "ya aplicado" y el POST
no vuelve a tocar nada. Además, cada fila se valida contra el importe
ESPERADO antes de restar (si alguien ya la tocó a mano, no se le resta a
ciegas).

Read-only por default (GET). Sólo `usuarios.admin`.
"""
from __future__ import annotations

from flask import Blueprint, Response, render_template_string, request

import db
from auth import requiere_login, requiere_permiso
from modules.posdat.queries import clave_canonica_yy

bp = Blueprint(
    "correccion_provisiones_doble", __name__,
    url_prefix="/admin/correccion-provisiones-doble",
)

MARCA = "correccion_provisiones_doble_2026_09_01"

#: (clave_canonica, monto_cargado_de_mas) — el "mes completo" que la
#: procedure vieja aplicó el 01/09/2026 encima de la cuota diaria correcta
#: que ya había puesto persistir_acumulacion_yy(). Verificado línea por
#: línea contra /informes/dia del 01/09 (24 "Deuda corregida": el par
#: grande/24.142,50-ésimo daba EXACTO grande/30 en cada caso).
MONTOS_DE_MAS: tuple[tuple[tuple[str, str], float], ...] = (
    (("YY", "^A,E,C"), 195750.00),
    (("RT", ""), 182700.00),
    (("YY", "^SUELDOS"), 130500.00),
    (("YY", "^SR"), 71775.00),
    (("YY", "^SS"), 52200.00),
    (("YY", "^AB"), 28275.00),
    (("YY", "^13"), 21750.00),
    (("YY", "^ALQUILER"), 15225.00),
    (("YY", "~INCOB"), 8700.00),
    (("YY", "^14"), 6525.00),
    (("YY", "~INTER"), 6525.00),
    (("YY", "^JP"), 4350.00),
)


def _marca_aplicada() -> str | None:
    row = db.fetch_one(
        "SELECT valor FROM scintela.sistema_meta WHERE clave = %s", (MARCA,)
    )
    return row["valor"] if row else None


def _filas_yy_rt() -> list[dict]:
    return db.fetch_all(
        """
        SELECT id_posdat, prov, concepto, importe
          FROM scintela.posdat
         WHERE UPPER(TRIM(prov)) IN ('YY', 'RT')
           AND COALESCE(banc, 0) = 0
           AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    ) or []


def _preview() -> dict:
    """Empareja cada fila YY/RT viva con su corrección conocida (si hay).

    Sólo lectura. Devuelve {filas: [...], total_a_restar, sin_match: [...]}
    -- `sin_match` son montos conocidos que NO encontraron fila (para no
    aplicar en silencio menos de lo esperado).
    """
    filas = _filas_yy_rt()
    por_clave: dict[tuple[str, str], dict] = {}
    for f in filas:
        clave = clave_canonica_yy(f["prov"], f["concepto"])
        if clave not in por_clave or float(f["importe"] or 0) > float(por_clave[clave]["importe"] or 0):
            por_clave[clave] = f

    filas_preview = []
    sin_match = []
    total = 0.0
    for clave, monto in MONTOS_DE_MAS:
        fila = por_clave.get(clave)
        if not fila:
            sin_match.append({"clave": clave, "monto": monto})
            continue
        actual = float(fila["importe"] or 0)
        filas_preview.append({
            "id_posdat": fila["id_posdat"],
            "prov": fila["prov"],
            "concepto": fila["concepto"],
            "clave": clave,
            "importe_actual": actual,
            "monto_a_restar": monto,
            "importe_corregido": round(actual - monto, 2),
            "matchea": actual >= monto - 0.005,
        })
        total += monto
    return {"filas": filas_preview, "sin_match": sin_match, "total_a_restar": round(total, 2)}


_PAGE = """\
<!doctype html>
<html lang=es><head><meta charset=utf-8><title>Correccion provisiones doble carga</title>
<style>
  body{font-family:system-ui;max-width:1000px;margin:24px auto;padding:0 12px}
  table{border-collapse:collapse;width:100%;font-size:13px;margin-top:12px}
  th,td{border:1px solid #ddd;padding:4px 8px;text-align:right}
  th:nth-child(2),td:nth-child(2){text-align:left}
  .ok{background:#e7f5e8;padding:8px;border:1px solid #6c6;border-radius:4px}
  .warn{background:#fff7d8;padding:8px;border:1px solid #cb8;border-radius:4px}
  .bad{background:#fee;padding:8px;border:1px solid #f00;border-radius:4px}
  button{padding:8px 16px;font-weight:bold;background:#0a0;color:#fff;border:0;
         border-radius:4px;cursor:pointer;margin-top:12px}
</style>
</head><body>
<h1>Correccion: provisiones YY/RT cargadas dos veces el 01/09/2026</h1>
<p>El cron del dia 1 corrio la procedure vieja (mes completo) ADEMAS del
motor unico diario -- cada una de las 12 provisiones se cargo dos veces.
Esto resta SOLO el monto exacto de mas, deja la cuota diaria correcta.</p>

{% if ya_aplicada %}
<div class="ok"><b>OK -- Ya aplicada</b> el {{ ya_aplicada }}. No se vuelve a tocar nada.</div>
{% elif aplicado %}
<div class="ok">
  <b>OK -- Corregido.</b> Se restaron ${{ '%.2f'|format(total_restado) }} en {{ n }} filas.
  <table>
    <thead><tr><th>id</th><th>Concepto</th><th>Importe final</th></tr></thead>
    <tbody>
    {% for r in resultado %}
      <tr><td>{{ r.id_posdat }}</td><td>{{ r.concepto }}</td><td>{{ '%.2f'|format(r.importe) }}</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
  {% if error %}<div class="bad"><b>Error:</b> {{ error }}</div>{% endif %}
  {% if preview.sin_match %}
  <div class="warn">
    <b>No encontre fila para {{ preview.sin_match|length }} de los 12 montos conocidos.</b>
    No se va a aplicar nada hasta que esto se entienda (para no restar de menos).
    <ul>{% for s in preview.sin_match %}<li>{{ s.clave }} -- ${{ '%.2f'|format(s.monto) }}</li>{% endfor %}</ul>
  </div>
  {% endif %}
  <table>
    <thead><tr><th>id</th><th>Concepto</th><th>Prov</th><th>Importe actual</th>
      <th>A restar</th><th>Importe corregido</th><th>Matchea</th></tr></thead>
    <tbody>
    {% for f in preview.filas %}
      <tr{% if not f.matchea %} class="bad"{% endif %}>
        <td>{{ f.id_posdat }}</td><td>{{ f.concepto }}</td><td>{{ f.prov }}</td>
        <td>{{ '%.2f'|format(f.importe_actual) }}</td>
        <td>{{ '%.2f'|format(f.monto_a_restar) }}</td>
        <td>{{ '%.2f'|format(f.importe_corregido) }}</td>
        <td>{{ 'si' if f.matchea else 'NO -- no se toca' }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  <p>Total a restar: <b>${{ '%.2f'|format(preview.total_a_restar) }}</b>
     ({{ preview.filas|length }} de 12 filas encontradas)</p>
  {% if puede_aplicar %}
  <form method=POST>
    <input type=hidden name=csrf_token value="{{ csrf_token() }}">
    <button type=submit name=aplicar value=1
      onclick="return confirm('Restar ${{ '%.2f'|format(preview.total_a_restar) }} de las provisiones YY/RT (correccion del doble cobro de hoy)?');">
      APLICAR CORRECCION
    </button>
  </form>
  {% endif %}
{% endif %}
<p style="margin-top:24px"><a href="/posdat?tab=yy">Volver a posdatados</a></p>
</body></html>
"""


@bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def index() -> Response:
    ya_aplicada = _marca_aplicada()
    aplicado = False
    resultado: list[dict] = []
    total_restado = 0.0
    error: str | None = None

    if request.method == "POST" and request.form.get("aplicar") == "1" and not ya_aplicada:
        prev = _preview()
        if prev["sin_match"]:
            error = "Hay montos conocidos sin fila que los matchee -- no se aplico nada."
        elif not prev["filas"] or any(not f["matchea"] for f in prev["filas"]):
            error = "Alguna fila ya no tiene el importe esperado (se corrigio a mano?) -- no se aplico nada."
        else:
            with db.tx() as conn:
                for f in prev["filas"]:
                    row = db.execute_returning(
                        """
                        UPDATE scintela.posdat
                           SET importe = importe - %(monto)s
                         WHERE id_posdat = %(id)s
                        RETURNING id_posdat, concepto, importe
                        """,
                        {"monto": f["monto_a_restar"], "id": f["id_posdat"]},
                        conn=conn,
                    )
                    if row:
                        resultado.append(row)
                        total_restado += f["monto_a_restar"]
                db.execute(
                    """
                    INSERT INTO scintela.sistema_meta (clave, valor)
                    VALUES (%s, CURRENT_TIMESTAMP::text)
                    ON CONFLICT (clave) DO NOTHING
                    """,
                    (MARCA,),
                    conn=conn,
                )
            aplicado = True
            ya_aplicada = _marca_aplicada()

    preview = _preview() if not ya_aplicada else {"filas": [], "sin_match": [], "total_a_restar": 0.0}
    puede_aplicar = (
        not ya_aplicada and not error and preview["filas"]
        and not preview["sin_match"] and all(f["matchea"] for f in preview["filas"])
    )

    return Response(
        render_template_string(
            _PAGE,
            ya_aplicada=ya_aplicada, aplicado=aplicado, resultado=resultado,
            total_restado=total_restado, n=len(resultado), error=error,
            preview=preview, puede_aplicar=puede_aplicar,
        ),
        mimetype="text/html",
    )
