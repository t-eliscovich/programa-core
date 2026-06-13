"""Anticipos (scintela.dolares) — alta y cancelación directa en PC.

TMT 2026-06-11 dueña: sin sync, los anticipos tipeados en el dBase (DOLARES)
no llegan más a PC — "la construís ahora". Réplica del flujo dBase:
ST=' ' = vivo (suma a ANTIC del balance, INFORMES.PRG L58); cancelar = ST='B'
(el estado de baja más usado en DOLARES.DBF). El comparador [5] vigila el Δ.
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, flash, g, redirect, render_template_string, request, url_for

import db
from auth import requiere_login, requiere_permiso

bp = Blueprint("anticipos", __name__, url_prefix="/anticipos")

TPL = """
{% extends 'base.html' %}
{% block content %}
<div class="max-w-4xl mx-auto px-4 py-6">
  <h1 class="text-xl font-bold mb-1">Anticipos</h1>
  <p class="text-sm text-slate-500 mb-4">Vivos (ST en blanco) — suman a ANTICIPOS
  del balance, igual que DOLARES del dBase. Total vivo:
  <b>$ {{ total | money_es }}</b> · {{ filas|length }} partidas</p>

  <form method="post" action="{{ url_for('anticipos.nuevo') }}"
        class="mb-6 flex flex-wrap gap-2 items-end text-sm">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>Fecha<br><input type="date" name="fecha" value="{{ hoy }}" required class="border rounded px-2 py-1"></label>
    <label>Cliente (CTA)<br><input name="cta" maxlength="3" required placeholder="ABC" class="border rounded px-2 py-1 w-20 uppercase"></label>
    <label>Concepto<br><input name="concepto" maxlength="100" class="border rounded px-2 py-1 w-64"></label>
    <label>Importe USD<br><input name="importe" type="number" step="0.01" min="0.01" required class="border rounded px-2 py-1 w-32"></label>
    <button class="px-4 py-1.5 rounded bg-sky-600 text-white">+ Anticipo</button>
  </form>

  <table class="w-full text-sm">
    <thead><tr class="text-left text-slate-500 border-b">
      <th class="py-1 pr-3">Fecha</th><th class="pr-3">CTA</th><th class="pr-3">Concepto</th>
      <th class="pr-3 text-right">Importe</th><th class="pr-3">Origen</th><th></th>
    </tr></thead>
    <tbody>
    {% for r in filas %}
      <tr class="border-b border-slate-100 dark:border-slate-800">
        <td class="py-1 pr-3">{{ r.fecha or '—' }}</td>
        <td class="pr-3 font-mono">{{ r.cta }}</td>
        <td class="pr-3">{{ (r.concepto or '')[:50] }}</td>
        <td class="pr-3 text-right font-semibold">{{ (r.importe or 0) | money_es }}</td>
        <td class="pr-3 text-slate-400 text-xs">{{ r.usuario_crea or '—' }}</td>
        <td>
          <form method="post" action="{{ url_for('anticipos.cancelar', id_dolares=r.id_dolares) }}"
                onsubmit="return confirm('Cancelar anticipo {{ r.cta }} $ {{ '%.2f' % (r.importe or 0) }}?')">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <button class="text-xs text-red-600 hover:underline">cancelar</button>
          </form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="6" class="py-3 text-slate-400">Sin anticipos vivos.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
"""


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("facturas.ver")
def lista():
    filas = db.fetch_all(
        """
        SELECT id_dolares, fecha, cta, concepto, importe, usuario_crea
          FROM scintela.dolares
         WHERE st IS NULL OR TRIM(COALESCE(st, '')) = ''
         ORDER BY fecha DESC NULLS LAST, id_dolares DESC
        """
    ) or []
    total = sum(float(r.get("importe") or 0) for r in filas)
    return render_template_string(TPL, filas=filas, total=total, hoy=date.today().isoformat())


@bp.route("/nuevo", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def nuevo():
    try:
        fecha = request.form.get("fecha") or date.today().isoformat()
        cta = (request.form.get("cta") or "").strip().upper()[:3]
        concepto = (request.form.get("concepto") or "").strip()[:100]
        importe = round(float(request.form.get("importe") or 0), 2)
        if not cta or importe <= 0:
            flash("Faltan datos (cliente / importe).", "warn")
            return redirect(url_for("anticipos.lista"))
        usuario = (getattr(g, "user", None) or {}).get("username", "web")
        db.execute(
            "INSERT INTO scintela.dolares (fecha, cta, concepto, importe, st, usuario_crea) "
            "VALUES (%s, %s, %s, %s, ' ', %s)",
            (fecha, cta, concepto, importe, usuario),
        )
        flash(f"Anticipo {cta} $ {importe:,.2f} registrado (suma a ANTICIPOS).", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"No se pudo registrar: {e}", "error")
    return redirect(url_for("anticipos.lista"))


@bp.route("/<int:id_dolares>/cancelar", methods=["POST"])
@requiere_login
@requiere_permiso("facturas.crear")
def cancelar(id_dolares: int):
    n = db.execute(
        "UPDATE scintela.dolares SET st = 'B' WHERE id_dolares = %s "
        "AND (st IS NULL OR TRIM(COALESCE(st,'')) = '')",
        (id_dolares,),
    )
    flash("Anticipo cancelado (ST=B)." if n else "No se encontró o ya estaba cancelado.", "ok" if n else "warn")
    return redirect(url_for("anticipos.lista"))
