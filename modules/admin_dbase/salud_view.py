"""Endpoint /admin/salud — corre `scripts/check_salud_dia.py` desde la app.

POR QUÉ EXISTE (TMT 2026-07-29): el chequeo de salud existía desde mayo pero
**no lo corría nadie** — es un script suelto en scripts/, sin cron ni workflow,
que había que ejecutar a mano con las env vars puestas. Un health check que
nadie corre no sirve de nada: el 29/07 la Utilidad Real se mostró ~495.000 más
baja durante minutos (196.010 en vez de 687.519) y lo descubrió la dueña
mirando la pantalla, no el chequeo.

Acá se expone por la UI, que es donde alguien lo va a ver. SOLO LECTURA: los
checks son SELECTs y llamadas a los bridges; ninguno escribe.

Streaming igual que /admin/dbase-compare/run — algunos checks tardan (BANCOS
recorre el running de cada banco, BRIDGES toca Metabase).
"""
from __future__ import annotations

import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path

from flask import Blueprint, Response, render_template_string, request, stream_with_context

import db
from auth import requiere_login, requiere_permiso

bp = Blueprint("salud", __name__, url_prefix="/admin/salud")

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "check_salud_dia.py"


def _cargar_checks():
    """Importa el script como módulo (scripts/ no es paquete)."""
    spec = importlib.util.spec_from_file_location("check_salud_dia", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"no pude cargar {_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_TPL = """
<!doctype html><meta charset="utf-8">
<title>Chequeo de salud — Programa Core</title>
<style>
 body{font:14px system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#0f172a}
 h1{font-size:1.25rem;margin-bottom:.3rem}
 .muted{color:#64748b;font-size:13px}
 button{padding:.55rem 1.1rem;border-radius:6px;border:0;background:#0f172a;color:#fff;
        cursor:pointer;font-size:14px;margin-top:1rem}
 ul{font-size:13px;color:#475569;line-height:1.7}
 code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12px}
</style>
<p><a href="/" style="font-size:13px">&larr; Volver al menú</a></p>
<h1>Chequeo de salud</h1>
<p class="muted">Pasada de invariantes sobre la base. <strong>No cambia nada</strong>: sólo mira y reporta.</p>
<ul>
  <li><strong>Caja, Gastos, Bancos, Mov. doble, Cheques, Facturas, Posdat, Cheques×Factura,
      Reversibilidad</strong> — que el programa no se contradiga a sí mismo
      (saldos que cierren, nada huérfano, todo reversible).</li>
  <li><strong>Provisiones</strong> — que las 12 cuotas diarias sigan siendo las del
      <code>MENU.PRG</code>. Si alguien las cambia en el FoxPro, salta acá.
      Necesita que se haya subido un tarball <em>con MENU.PRG</em> en
      Comparación con dBase.</li>
  <li><strong>Bridges</strong> — que Asinfo conteste y que la utilidad del balance
      esté saliendo de Asinfo y no del dBase por un fallback silencioso.</li>
</ul>
<form method="get" action="/admin/salud/run">
  <button type="submit">Correr chequeo</button>
</form>
<p class="muted" style="margin-top:.6rem">
  ¿Necesitás ver las filas concretas detrás de un error?
  <a href="/admin/salud/run?verbose=1">correr con detalle</a>.
</p>
<p class="muted">
  ¿Y el <em>por qué</em>? <a href="/admin/salud/diag">diagnóstico profundo</a> —
  los movimientos alrededor del drift del banco, los cheques de cada factura
  descuadrada, y las filas de mov_doble sin reverso. Solo lectura.
</p>
"""


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def form():
    return render_template_string(_TPL)


@bp.route("/run", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def run():
    # ?verbose=1 → cada sección lista las filas concretas, no sólo el conteo.
    # Sirve para ir del "45 reversados sin id_reverso" al detalle sin abrir
    # una consola de SQL contra producción.
    verbose = (request.args.get("verbose") or "").strip() in ("1", "true", "si")

    def _gen():
        yield ("=== CHEQUEO DE SALUD — Programa Core (solo lectura) ===\n"
               + ("    [modo detalle]\n" if verbose else
                  "    (agregá ?verbose=1 a la URL para ver las filas concretas)\n")
               + "\n")
        try:
            chk = _cargar_checks()
        except Exception as exc:  # noqa: BLE001
            yield f"[ERROR] no pude cargar el chequeo: {exc!r}\n"
            return

        chk._resultados.clear()
        for nombre, fn in chk.ALL_CHECKS.items():
            buf = io.StringIO()
            try:
                # Los checks imprimen a stdout; se captura y se re-emite para
                # que salga por el stream y no por el log del servidor.
                with redirect_stdout(buf):
                    fn(verbose=verbose)
            except Exception as exc:  # noqa: BLE001
                yield buf.getvalue()
                yield f"  [ERR]  {nombre}: explotó → {exc!r}\n"
                chk._resultados.append((nombre.upper(), chk.ERROR, repr(exc)))
                continue
            yield buf.getvalue()

        n_err = sum(1 for _, st, _ in chk._resultados if st.strip() == "[ERR]")
        n_warn = sum(1 for _, st, _ in chk._resultados if st.strip() == "[WARN]")
        n_ok = sum(1 for _, st, _ in chk._resultados if st.strip() == "[OK]")
        yield "\n" + "═" * 64 + "\n"
        yield f"  Resumen: {n_ok} OK · {n_warn} WARN · {n_err} ERR\n"
        if n_err == 0 and n_warn == 0:
            yield "  ✓ Todo verde.\n"
        elif n_err == 0:
            yield "  ~ Sólo warnings — operable, pero conviene mirar.\n"
        else:
            yield "  ✗ Hay errores — mirar antes de cerrar el día.\n"
        yield "═" * 64 + "\n"

    return Response(stream_with_context(_gen()), mimetype="text/plain; charset=utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# /admin/salud/diag — el "¿y esto de dónde sale?" de los errores rojos.
#
# POR QUÉ (TMT 2026-07-29): el chequeo dice QUÉ está mal y en qué fila, pero
# para entender POR QUÉ había que abrir una consola de SQL contra producción.
# Tres errores llevan meses en rojo (el drift de Pichincha, las facturas con
# saldo≠importe−abono y las sobre-abonadas) y nadie los miró justamente por
# eso. Esta pantalla trae el CONTEXTO de cada uno — los movimientos alrededor
# del drift, los cheques aplicados a cada factura descuadrada — que es lo que
# hace falta para decidir si es plata faltante o un artefacto de la migración.
#
# SOLO LECTURA, igual que el resto del módulo.
# ═══════════════════════════════════════════════════════════════════════════

_DOCS_ENTRADA = {"DE", "TR", "XX", "NC", "IN"}


def _signed_delta(doc, imp) -> float:
    """Mismo signo que bank_helpers.signo_documento y que check_bancos."""
    imp = float(imp or 0)
    if imp < 0:
        return imp
    return imp if (doc or "").upper().strip() in _DOCS_ENTRADA else -imp


def _diag_banco(no_banco: int, ventana: int = 12):
    """Camina el running del banco y devuelve el contexto del primer drift.

    Devuelve `(filas_ventana, info)` donde `info` trae el índice del drift
    dentro de la ventana. La ventana son las N filas antes y N después, con
    el delta y el saldo calculado de cada una — sin eso no se puede ver si
    el salto es de UNA fila (un importe/signo mal) o si el running venía
    arrastrado desde antes.
    """
    filas = db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, no_documento, importe, saldo,
               COALESCE(concepto, '') AS concepto,
               COALESCE(prov, '')     AS prov
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
         ORDER BY fecha ASC, id_transaccion ASC
        """,
        (int(no_banco),),
    ) or []
    ancla_i = next((i for i, f in enumerate(filas) if f.get("saldo") is not None), None)
    if ancla_i is None:
        return [], None
    saldo_calc = float(filas[ancla_i]["saldo"] or 0)
    calc_por_fila: dict = {filas[ancla_i]["id_transaccion"]: saldo_calc}
    drift_i = None
    for i in range(ancla_i + 1, len(filas)):
        f = filas[i]
        saldo_calc = round(saldo_calc + _signed_delta(f["documento"], f["importe"]), 2)
        calc_por_fila[f["id_transaccion"]] = saldo_calc
        sr = f.get("saldo")
        if sr is not None and abs(saldo_calc - float(sr)) > 0.01 and drift_i is None:
            drift_i = i
            break
    if drift_i is None:
        return [], None
    lo = max(ancla_i, drift_i - ventana)
    hi = min(len(filas), drift_i + ventana + 1)
    return (
        [dict(f, _calc=calc_por_fila.get(f["id_transaccion"])) for f in filas[lo:hi]],
        {"drift_id": filas[drift_i]["id_transaccion"], "total_filas": len(filas)},
    )


_SQL_FACT_DESCUADRADAS = """
    SELECT f.id_factura, f.numf, f.numf_completo, f.codigo_cli, f.fecha,
           f.importe, f.abono, f.saldo, f.stat, f.condic,
           COALESCE(f.observacion, '') AS observacion,
           ROUND(f.importe - f.abono - f.saldo, 2) AS diff,
           (SELECT COUNT(*) FROM scintela.chequesxfact x
             WHERE x.id_fact = f.id_factura)                AS n_cheques,
           COALESCE((SELECT SUM(x.importe) FROM scintela.chequesxfact x
             WHERE x.id_fact = f.id_factura), 0)            AS aplicado,
           COALESCE((SELECT SUM(r.rete) FROM scintela.retencion r
             WHERE r.numf = f.numf AND r.codigo_cli = f.codigo_cli), 0) AS retenido,
           (SELECT COUNT(*) FROM scintela.mov_doble m
             WHERE m.origen_table = 'factura' AND m.origen_id = f.id_factura
               AND m.tipo IN ('factura_cerrada_a_t', 'factura_stat_cambio',
                              'totalizar_estado_cuenta')
               AND m.estado = 'activo')                    AS n_cierres
      FROM scintela.factura f
     WHERE ABS(f.importe - f.abono - f.saldo) > 0.01
       AND TRIM(COALESCE(f.stat, '')) NOT IN ('X', 'Y')
     ORDER BY ABS(f.importe - f.abono - f.saldo) DESC
"""

_SQL_SOBRE_ABONADAS = """
    SELECT f.id_factura, f.numf, f.numf_completo, f.codigo_cli, f.fecha,
           f.importe, f.abono, f.saldo, f.stat,
           SUM(x.importe)                       AS aplicado,
           COUNT(*)                             AS n,
           ROUND(SUM(x.importe) - f.importe, 2) AS exceso,
           MIN(x.fechaing) AS desde, MAX(x.fechaing) AS hasta
      FROM scintela.factura f
      JOIN scintela.chequesxfact x ON x.id_fact = f.id_factura
     WHERE TRIM(COALESCE(f.stat, '')) NOT IN ('X', 'Y')
     GROUP BY f.id_factura, f.numf, f.numf_completo, f.codigo_cli, f.fecha,
              f.importe, f.abono, f.saldo, f.stat
    HAVING SUM(x.importe) - f.importe > 0.01
     ORDER BY SUM(x.importe) - f.importe DESC
"""

_SQL_LINKS_ROTOS = """
    SELECT m.id_mov_doble, m.tipo, m.origen_table, m.origen_id,
           m.destino_table, m.destino_id, m.importe, m.fecha_operacion,
           COALESCE(m.usuario, '') AS usuario,
           COALESCE(m.concepto, '') AS concepto,
           m.batch_id
      FROM scintela.mov_doble m
     WHERE m.estado = 'reversado' AND m.id_reverso IS NULL
       AND NOT EXISTS (
             SELECT 1 FROM scintela.mov_doble h
              WHERE h.estado = 'reverso'
                AND (   (h.origen_table = m.origen_table
                         AND h.origen_id = m.origen_id)
                     OR (m.batch_id IS NOT NULL AND h.batch_id = m.batch_id) )
       )
     ORDER BY m.tipo, m.id_mov_doble
"""


@bp.route("/diag", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def diag():
    """Contexto de los errores rojos del chequeo. Solo lectura."""
    def _gen():
        yield ("=== DIAGNÓSTICO PROFUNDO — contexto de los errores rojos ===\n"
               "    (solo lectura: ni un UPDATE)\n\n")

        # ── 1) Drift del running bancario ──────────────────────────────
        yield "┌─ A) BANCOS — contexto del primer drift del running ───────────\n"
        for b in db.fetch_all(
            "SELECT no_banco, COALESCE(nombre,'') AS nombre FROM scintela.banco "
            " WHERE EXISTS (SELECT 1 FROM scintela.transacciones_bancarias t "
            "               WHERE t.no_banco = banco.no_banco) "
            " ORDER BY no_banco") or []:
            try:
                filas, info = _diag_banco(int(b["no_banco"]))
            except Exception as exc:  # noqa: BLE001
                yield f"  #{b['no_banco']}: no pude caminarlo → {exc!r}\n"
                continue
            if not info:
                yield f"  #{b['no_banco']} {b['nombre'][:12]}: sin drift.\n"
                continue
            yield (f"  #{b['no_banco']} {b['nombre'][:12]} — drift en "
                   f"tx#{info['drift_id']} ({info['total_filas']} filas en total)\n")
            yield ("     tx        fecha       doc  nro        importe      "
                   "running       calc         Δ  concepto\n")
            for f in filas:
                sr = f.get("saldo")
                calc = f.get("_calc")
                d = (float(sr) - float(calc)) if (sr is not None and calc is not None) else None
                marca = " ←DRIFT" if f["id_transaccion"] == info["drift_id"] else ""
                yield (f"     {f['id_transaccion']:<9} {str(f['fecha'])[:10]}  "
                       f"{(f['documento'] or ''):<4} "
                       f"{str(f.get('no_documento') or '')[:9]:<9} "
                       f"{float(f['importe'] or 0):>13,.2f} "
                       f"{(float(sr) if sr is not None else 0):>13,.2f} "
                       f"{(float(calc) if calc is not None else 0):>13,.2f} "
                       f"{(d if d is not None else 0):>10,.2f}  "
                       f"{(f.get('prov') or '')[:6]} {f['concepto'][:34]}{marca}\n")
            yield "\n"

        # ── 2) Facturas descuadradas ───────────────────────────────────
        yield "┌─ B) FACTURAS con saldo ≠ importe − abono ─────────────────────\n"
        desc = db.fetch_all(_SQL_FACT_DESCUADRADAS) or []
        yield (f"  {len(desc)} factura(s). 'diff' = importe − abono − saldo "
               "(positivo = el saldo quedó MÁS BAJO de lo que corresponde).\n")
        yield ("  id_fact   numf      cli   stat      importe       abono  "
               "     saldo        diff  ch  aplicado  retenido cierres\n")
        _tot_diff = 0.0
        for f in desc:
            _tot_diff += float(f["diff"] or 0)
            yield (f"  {f['id_factura']:<9} {str(f['numf'])[:9]:<9} "
                   f"{(f['codigo_cli'] or '')[:5]:<5} {(f['stat'] or '')[:4]:<4} "
                   f"{float(f['importe'] or 0):>12,.2f} "
                   f"{float(f['abono'] or 0):>11,.2f} "
                   f"{float(f['saldo'] or 0):>11,.2f} "
                   f"{float(f['diff'] or 0):>11,.2f} "
                   f"{int(f['n_cheques'] or 0):>3} "
                   f"{float(f['aplicado'] or 0):>9,.2f} "
                   f"{float(f['retenido'] or 0):>9,.2f} "
                   f"{int(f['n_cierres'] or 0):>4}\n")
        yield f"  TOTAL diff: {_tot_diff:,.2f}\n"
        yield ("  (cierres = mov_doble activos de tipo factura_cerrada_a_t / "
               "factura_stat_cambio / totalizar_estado_cuenta)\n\n")

        # ── 3) Sobre-abonadas ──────────────────────────────────────────
        yield "┌─ C) FACTURAS sobre-abonadas (Σ cheques aplicados > importe) ──\n"
        sob = db.fetch_all(_SQL_SOBRE_ABONADAS) or []
        _tot_exc = sum(float(r["exceso"] or 0) for r in sob)
        yield f"  {len(sob)} factura(s), exceso total $ {_tot_exc:,.2f}\n"
        yield ("  id_fact   numf      cli   stat      importe    aplicado  n "
               "      exceso  desde       hasta\n")
        for r in sob:
            yield (f"  {r['id_factura']:<9} {str(r['numf'])[:9]:<9} "
                   f"{(r['codigo_cli'] or '')[:5]:<5} {(r['stat'] or '')[:4]:<4} "
                   f"{float(r['importe'] or 0):>12,.2f} "
                   f"{float(r['aplicado'] or 0):>11,.2f} "
                   f"{int(r['n'] or 0):>2} "
                   f"{float(r['exceso'] or 0):>12,.2f}  "
                   f"{str(r['desde'])[:10]}  {str(r['hasta'])[:10]}\n")
        # ¿Cuántas están en las DOS listas? Si se solapan mucho, es un solo
        # fenómeno contado dos veces, no dos problemas distintos.
        ids_desc = {f["id_factura"] for f in desc}
        ids_sob = {r["id_factura"] for r in sob}
        yield (f"\n  Solapamiento B∩C: {len(ids_desc & ids_sob)} factura(s) "
               f"(B={len(ids_desc)}, C={len(ids_sob)})\n\n")

        # ── 4) Links rotos de mov_doble ────────────────────────────────
        yield "┌─ D) MOV_DOBLE — reversados sin reverso (filas concretas) ─────\n"
        rotos = db.fetch_all(_SQL_LINKS_ROTOS) or []
        yield f"  {len(rotos)} fila(s)\n"
        for m in rotos:
            yield (f"  #{m['id_mov_doble']:<6} {(m['tipo'] or '')[:32]:<32} "
                   f"{(m['origen_table'] or '')[:8]}#{m['origen_id']}"
                   f"→{(m['destino_table'] or '')[:8]}#{m['destino_id']} "
                   f"$ {float(m['importe'] or 0):>11,.2f} "
                   f"{str(m['fecha_operacion'])[:10]} "
                   f"{(m['usuario'] or '')[:8]:<8} "
                   f"{'batch' if m.get('batch_id') else '     '} "
                   f"{(m['concepto'] or '')[:48]}\n")
        yield "\n═══ fin ═══\n"

    return Response(stream_with_context(_gen()), mimetype="text/plain; charset=utf-8")
