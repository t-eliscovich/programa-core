"""Endpoint /admin/abonos-historicos — vincula cheques históricos del dBase a
sus facturas (crea el chequesxfact que el dBase nunca guardó).

CONTEXTO (TMT 2026-06-07): en el dBase, CHEQUES.DBF NO tiene ningún campo que
referencie a la factura — el abono se guarda como un número suelto en
FACTURAS.DBF.ABONO. Por eso, al sincronizar, PC tiene el cheque (de
CHEQUES.DBF) pero "colgado": la factura muestra `abono > 0` pero "0 cheques
aplicados", y la cuenta corriente no ve el +abono (solo correcciones hechas
en PC). Este endpoint reconstruye el vínculo cheque→factura:

- Buscás la factura por numf.
- Lista los cheques de ese cliente que están SIN vincular a ninguna factura
  (candidatos a ser el cheque que la abonó).
- Elegís cuál(es) vincular. Al aplicar: INSERT en chequesxfact + se recalcula
  `factura.abono = SUM(chequesxfact)` (así el +original y cualquier reversa
  conviven y netean bien; la cuenta corriente vuelve a cuadrar).

DRY-RUN por defecto. Con `apply=1` ejecuta en una transacción. Idempotente:
si el cheque ya está vinculado a esa factura, lo saltea.

OJO: es un arreglo de PC. El próximo sync del dBase pisa `factura.abono` con
el valor del DBF (el dBase manda). Sirve para ver el rastro completo ahora.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, render_template_string, request

from auth import requiere_login, requiere_permiso

bp = Blueprint("abonos_historicos", __name__, url_prefix="/admin/abonos-historicos")


_TPL = """
<!doctype html><meta charset="utf-8">
<title>Abonos históricos — vincular cheque a factura</title>
<style>
 body{font:14px system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#0f172a}
 h1{font-size:1.25rem} table{border-collapse:collapse;width:100%;margin:1rem 0}
 th,td{border:1px solid #e2e8f0;padding:.4rem .6rem;text-align:left;font-size:13px}
 th{background:#f8fafc} .r{text-align:right;font-variant-numeric:tabular-nums}
 .ok{color:#047857} .warn{color:#b45309} .err{color:#b91c1c}
 input[type=text]{padding:.4rem;border:1px solid #cbd5e1;border-radius:6px;width:160px}
 button{padding:.45rem .9rem;border-radius:6px;border:0;background:#0ea5e9;color:#fff;cursor:pointer}
 .apply{background:#059669} .muted{color:#64748b;font-size:12px}
 .box{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:.8rem 1rem;margin:1rem 0}
</style>
<h1>Abonos históricos — vincular cheque del dBase a su factura</h1>
<p class="muted">El dBase no guarda qué cheque pagó cada factura. Acá reconstruís el
vínculo: elegí la factura, mirá los cheques del cliente sin vincular, y vinculá
el que corresponda. Recalcula <code>abono = suma de movimientos</code>.
<strong>El próximo sync del dBase pisa el abono</strong> (es un arreglo de PC).</p>

<form method="get">
  <label>N° factura (numf): <input type="text" name="numf" value="{{ numf or '' }}" autofocus></label>
  <button type="submit">Buscar</button>
</form>

{% for m in msg %}<div class="box {{ m[0] }}">{{ m[1] }}</div>{% endfor %}

{% if fact %}
<div class="box">
  <strong>Factura {{ fact.numf_completo or fact.numf }}</strong> · cliente {{ fact.codigo_cli }}<br>
  importe <b class="r">{{ '%.2f'|format(fact.importe or 0) }}</b> ·
  abono actual <b class="r">{{ '%.2f'|format(fact.abono or 0) }}</b> ·
  saldo <b class="r">{{ '%.2f'|format(fact.saldo or 0) }}</b> · stat {{ fact.stat }}
  <div class="muted">Ya vinculados (chequesxfact): {{ ya|length }} · suma {{ '%.2f'|format(suma_ya) }}</div>
</div>

{% if candidatos %}
<form method="get">
  <input type="hidden" name="numf" value="{{ fact.numf }}">
  <input type="hidden" name="apply" value="1">
  <table>
    <tr><th></th><th>id_cheque</th><th>fecha</th><th>fecha dep.</th><th>banco</th><th>stat</th><th class="r">importe</th></tr>
    {% for c in candidatos %}
    <tr>
      <td><input type="checkbox" name="ch" value="{{ c.id_cheque }}"
                 {% if candidatos|length == 1 %}checked{% endif %}></td>
      <td>{{ c.id_cheque }}</td><td>{{ c.fecha }}</td><td>{{ c.fechad }}</td>
      <td>{{ c.banco }}</td><td>{{ c.stat }}</td>
      <td class="r">{{ '%.2f'|format(c.importe or 0) }}</td>
    </tr>
    {% endfor %}
  </table>
  <button class="apply" type="submit">Vincular seleccionados y recalcular abono</button>
  <span class="muted">(tildá el/los cheque(s) que pagaron esta factura)</span>
</form>
{% else %}
<p class="muted">No hay cheques de {{ fact.codigo_cli }} sin vincular. (O ya están todos linkeados, o no hay cheques de ese cliente en PC.)</p>
{% endif %}
{% endif %}
"""


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("facturas.editar")
def vincular():
    import db  # lazy — igual que los otros admin_dbase views (evita romper el arranque)

    numf_raw = (request.values.get("numf") or "").strip()
    do_apply = request.values.get("apply") == "1"
    sel = request.values.getlist("ch")
    msg: list[tuple[str, str]] = []
    fact = None
    candidatos: list[dict] = []
    ya: list[dict] = []
    suma_ya = 0.0

    if numf_raw.isdigit():
        numf = int(numf_raw)
        fact = db.fetch_one(
            "SELECT id_factura, numf, numf_completo, codigo_cli, importe, abono, saldo, stat "
            "FROM scintela.factura WHERE numf = %s ORDER BY id_factura LIMIT 1",
            (numf,),
        )
        if not fact:
            msg.append(("err", f"No existe factura con numf={numf}."))
        else:
            id_fact = fact["id_factura"]
            ya = db.fetch_all(
                "SELECT id_cheque, importe FROM scintela.chequesxfact WHERE id_fact = %s",
                (id_fact,),
            ) or []
            suma_ya = sum(float(r["importe"] or 0) for r in ya)
            ya_ids = {r["id_cheque"] for r in ya}

            candidatos = db.fetch_all(
                """
                SELECT c.id_cheque, c.fecha, c.fechad, c.importe, c.banco,
                       c.no_banco, c.stat
                  FROM scintela.cheque c
                 WHERE c.codigo_cli = %s
                   AND COALESCE(c.importe, 0) <> 0
                   AND NOT EXISTS (
                       SELECT 1 FROM scintela.chequesxfact x
                        WHERE x.id_cheque = c.id_cheque)
                 ORDER BY c.fechad NULLS LAST, c.id_cheque
                """,
                (fact["codigo_cli"],),
            ) or []

            if do_apply:
                ids = [int(s) for s in sel if str(s).isdigit()]
                if not ids:
                    msg.append(("warn", "No seleccionaste ningún cheque."))
                else:
                    elegidos = [c for c in candidatos if c["id_cheque"] in ids]
                    if not elegidos:
                        msg.append(("err", "Los cheques elegidos no son candidatos válidos (¿ya vinculados?)."))
                    else:
                        importe_fact = float(fact["importe"] or 0)
                        usuario = "web-abono-historico"
                        try:
                            with db.tx() as conn, conn.cursor() as cur:
                                # snapshot acumulado: arrancamos del SUM actual
                                acum = suma_ya
                                for c in elegidos:
                                    imp = float(c["importe"] or 0)
                                    acum += imp
                                    abono_f = acum
                                    saldo_f = importe_fact - acum
                                    stat_f = "T" if saldo_f <= 0.01 else "A"
                                    cur.execute(
                                        """
                                        INSERT INTO scintela.chequesxfact
                                            (id_cheque, id_fact, fechaing, codigo_cli,
                                             importe, no_banco, abono_f, saldo_f,
                                             stat_f, usuario_crea)
                                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                        """,
                                        (
                                            c["id_cheque"], id_fact,
                                            c.get("fechad") or c.get("fecha"),
                                            fact["codigo_cli"], imp, c.get("no_banco"),
                                            abono_f, saldo_f, stat_f, usuario,
                                        ),
                                    )
                                # recomputar abono = SUM(chequesxfact)
                                cur.execute(
                                    "SELECT COALESCE(SUM(importe),0) FROM scintela.chequesxfact WHERE id_fact = %s",
                                    (id_fact,),
                                )
                                nuevo_abono = float(cur.fetchone()[0] or 0)
                                nuevo_saldo = importe_fact - nuevo_abono
                                nuevo_stat = "T" if nuevo_saldo <= 0.01 else ("A" if nuevo_abono > 0.01 else (fact["stat"] or "Z"))
                                cur.execute(
                                    "UPDATE scintela.factura SET abono=%s, saldo=%s, stat=%s, usuario_modifica=%s WHERE id_factura=%s",
                                    (nuevo_abono, nuevo_saldo, nuevo_stat, usuario, id_fact),
                                )
                            msg.append((
                                "ok",
                                f"Vinculado {len(elegidos)} cheque(s). "
                                f"abono recalculado = {nuevo_abono:.2f}, saldo = {nuevo_saldo:.2f}, stat {nuevo_stat}.",
                            ))
                            # refrescar estado
                            fact = db.fetch_one(
                                "SELECT id_factura, numf, numf_completo, codigo_cli, importe, abono, saldo, stat "
                                "FROM scintela.factura WHERE id_factura = %s",
                                (id_fact,),
                            )
                            ya = db.fetch_all(
                                "SELECT id_cheque, importe FROM scintela.chequesxfact WHERE id_fact = %s",
                                (id_fact,),
                            ) or []
                            suma_ya = sum(float(r["importe"] or 0) for r in ya)
                            candidatos = [c for c in candidatos if c["id_cheque"] not in {x["id_cheque"] for x in ya}]
                        except Exception as e:  # noqa: BLE001
                            msg.append(("err", f"Error al vincular: {e}"))

    return render_template_string(
        _TPL, numf=numf_raw, fact=fact, candidatos=candidatos,
        ya=ya, suma_ya=suma_ya, msg=msg,
    )
