"""Endpoint /admin/cheques-fechas-deposito — backfill de la fecha de depósito
(FECHING) desde CHEQUES.DBF para cheques depositados que quedaron sin ella.

TMT 2026-07-20 (dueña: "¿no podés traer el campo depositado del dBase?").
Contexto: la columna Depositado de /cheques = cheque.fechaing. El sync del
dBase la trae (FECHING), pero un cheque depositado en el dBase DESPUÉS del
último sync queda en PC con fechaing NULL → "—" en pantalla.

Política CONSERVADORA (display-only, no toca estados/banco/saldos):
  - Solo cheques PC con stat B/A y fechaing IS NULL.
  - Solo filas del DBF con FECHING y STAT depositado (B/V/A/W/I/J/K —
    V/W/I/J/K = variantes legacy de depositado del FoxPro).
  - Match por (CLIENTE, IMPORTE, BANCO); si el DBF tiene varias candidatas,
    desempata por NB == no_banco. Ambiguo → se SALTEA y se lista.
  - NUNCA por importe solo (regla de la casa).

Lee CHEQUES.DBF del tarball ya subido a /admin/dbase-compare (mismo
EXTRACT_DIR) — no hace falta subir nada de nuevo si el compare está fresco.
GET = vista previa; POST /aplicar = escribe SOLO fechaing.
"""
from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template_string, request, url_for

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "cheques_feching",
    __name__,
    url_prefix="/admin/cheques-fechas-deposito",
)

# Stats "depositado" en el dBase crudo (antes del remap del sync V→B).
_DBF_STATS_DEPOSITADO = {"B", "V", "A", "W", "I", "J", "K"}
# Lado PC: solo depositados vigentes.
_PC_STATS = ("B", "A")


def _clave(cli, imp, banco) -> tuple:
    return (
        (cli or "").strip().upper(),
        round(float(imp or 0), 2),
        (banco or "").strip().upper(),
    )


def _leer_dbf_cheques() -> tuple[list[dict], str | None, str | None]:
    """Lee CHEQUES.DBF del EXTRACT_DIR del compare. → (filas, mtime_str, error)."""
    from modules.admin_dbase.dbase_compare_view import EXTRACT_DIR

    p = EXTRACT_DIR / "CHEQUES.DBF"
    if not p.exists():
        return [], None, (
            "No hay CHEQUES.DBF en el tarball del comparador. "
            "Subí el tarball fresco en /admin/dbase-compare primero."
        )
    try:
        import dbfread

        filas = list(dbfread.DBF(str(p), char_decode_errors="replace", load=False))
    except Exception as e:  # pragma: no cover - depende del archivo
        return [], None, f"No pude leer CHEQUES.DBF: {e}"
    mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    return filas, mtime, None


def calcular_propuestas(pc_rows: list[dict], dbf_rows: list[dict]) -> dict:
    """Función PURA (testeable): matchea y devuelve propuestas + salteados.

    pc_rows:  filas de scintela.cheque (stat B/A, fechaing NULL).
    dbf_rows: filas crudas del DBF (dicts con CLIENTE/IMPORTE/BANCO/NB/STAT/FECHING).
    """
    dbf_por_clave: dict[tuple, list[dict]] = {}
    for r in dbf_rows:
        stat = (str(r.get("STAT") or "")).strip().upper()
        feching = r.get("FECHING")
        if stat not in _DBF_STATS_DEPOSITADO or not isinstance(feching, date):
            continue
        dbf_por_clave.setdefault(
            _clave(r.get("CLIENTE"), r.get("IMPORTE"), r.get("BANCO")), []
        ).append(r)

    pc_por_clave: dict[tuple, list[dict]] = {}
    for c in pc_rows:
        pc_por_clave.setdefault(
            _clave(c.get("codigo_cli"), c.get("importe"), c.get("banco")), []
        ).append(c)

    propuestas, salteados = [], []
    for k, pcs in pc_por_clave.items():
        cands = dbf_por_clave.get(k, [])
        if not cands:
            for c in pcs:
                salteados.append((c, "sin fila depositada con FECHING en el DBF"))
            continue
        if len(pcs) > 1:
            for c in pcs:
                salteados.append((c, f"{len(pcs)} cheques PC iguales (ambiguo)"))
            continue
        c = pcs[0]
        if len(cands) > 1:
            # Desempate por NB == no_banco.
            nb = c.get("no_banco")
            cands_nb = [r for r in cands if nb is not None and int(r.get("NB") or 0) == int(nb)]
            if len(cands_nb) != 1:
                salteados.append((c, f"{len(cands)} filas del DBF matchean (ambiguo)"))
                continue
            cands = cands_nb
        propuestas.append((c, cands[0].get("FECHING")))
    return {"propuestas": propuestas, "salteados": salteados}


def _cargar_lados():
    import db

    pc_rows = db.fetch_all(
        """
        SELECT id_cheque, no_cheque, codigo_cli, importe, banco, no_banco,
               stat, fecha, fechad
          FROM scintela.cheque
         WHERE UPPER(COALESCE(stat,'')) = ANY(%s) AND fechaing IS NULL
        """,
        (list(_PC_STATS),),
    ) or []
    dbf_rows, mtime, err = _leer_dbf_cheques()
    return pc_rows, dbf_rows, mtime, err


_TPL = """
{% extends 'base.html' %}
{% block contenido %}
<div class="max-w-4xl mx-auto p-4">
  <h1 class="text-xl font-bold mb-1">Fechas de depósito desde el dBase</h1>
  <p class="text-sm text-slate-500 mb-4">
    Completa SOLO la columna Depositado (fechaing) de cheques B/A que no la
    tienen, leyendo FECHING de CHEQUES.DBF{% if mtime %} (tarball del {{ mtime }}){% endif %}.
    No toca estados ni saldos.
  </p>
  {% if err %}
    <div class="p-3 rounded border border-amber-300 bg-amber-50 text-sm mb-4">{{ err }}
      · <a class="underline" href="/admin/dbase-compare">ir al comparador</a></div>
  {% endif %}

  <h2 class="font-semibold text-sm mb-1">Se completan ({{ propuestas|length }})</h2>
  {% if propuestas %}
  <table class="w-full text-sm mb-4">
    <tr class="text-left text-xs text-slate-500">
      <th class="pr-2">Cliente</th><th class="pr-2">N°</th><th class="pr-2 text-right">Importe</th>
      <th class="pr-2">Banco</th><th class="pr-2">Stat</th><th>Depositado ←</th>
    </tr>
    {% for c, f in propuestas %}
    <tr class="border-t border-slate-100">
      <td class="pr-2">{{ c.codigo_cli }}</td>
      <td class="pr-2 font-mono">{{ c.no_cheque or c.id_cheque }}</td>
      <td class="pr-2 text-right font-mono">{{ c.importe | money_es }}</td>
      <td class="pr-2">{{ c.banco }}</td>
      <td class="pr-2 font-mono">{{ c.stat }}</td>
      <td class="font-mono text-emerald-700">{{ f | fecha_es }}</td>
    </tr>
    {% endfor %}
  </table>
  <form method="post" action="{{ url_for('cheques_feching.aplicar') }}" class="mb-6">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <button type="submit"
            class="px-4 py-2 rounded text-sm font-semibold text-white bg-emerald-600 hover:bg-emerald-700">
      ✓ Aplicar {{ propuestas|length }} fecha(s)
    </button>
  </form>
  {% else %}
    <p class="text-sm text-slate-500 mb-4">Nada para completar.</p>
  {% endif %}

  <h2 class="font-semibold text-sm mb-1">Salteados ({{ salteados|length }})</h2>
  {% if salteados %}
  <table class="w-full text-sm">
    <tr class="text-left text-xs text-slate-500">
      <th class="pr-2">Cliente</th><th class="pr-2">N°</th><th class="pr-2 text-right">Importe</th>
      <th class="pr-2">Banco</th><th>Motivo</th>
    </tr>
    {% for c, m in salteados %}
    <tr class="border-t border-slate-100">
      <td class="pr-2">{{ c.codigo_cli }}</td>
      <td class="pr-2 font-mono">{{ c.no_cheque or c.id_cheque }}</td>
      <td class="pr-2 text-right font-mono">{{ c.importe | money_es }}</td>
      <td class="pr-2">{{ c.banco }}</td>
      <td class="text-slate-500">{{ m }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
    <p class="text-sm text-slate-500">Ninguno.</p>
  {% endif %}
</div>
{% endblock %}
"""


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def preview():
    pc_rows, dbf_rows, mtime, err = _cargar_lados()
    r = calcular_propuestas(pc_rows, dbf_rows)
    return render_template_string(
        _TPL, propuestas=r["propuestas"], salteados=r["salteados"],
        mtime=mtime, err=err,
    )


@bp.route("/aplicar", methods=["POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def aplicar():
    import db
    from flask import g

    pc_rows, dbf_rows, _mtime, err = _cargar_lados()
    if err:
        flash(err, "warn")
        return redirect(url_for("cheques_feching.preview"))
    r = calcular_propuestas(pc_rows, dbf_rows)
    usuario = (g.user or {}).get("username", "web")
    n = 0
    with db.tx() as conn:
        for c, feching in r["propuestas"]:
            n += db.execute(
                """
                UPDATE scintela.cheque
                   SET fechaing = %s, usuario_modifica = %s,
                       fecha_modifica = CURRENT_TIMESTAMP
                 WHERE id_cheque = %s AND fechaing IS NULL
                   AND UPPER(COALESCE(stat,'')) = ANY(%s)
                """,
                (feching, usuario, c["id_cheque"], list(_PC_STATS)),
                conn=conn,
            ) or 0
    flash(f"{n} fecha(s) de depósito completadas desde el dBase.", "ok")
    return redirect(url_for("cheques_feching.preview"))
