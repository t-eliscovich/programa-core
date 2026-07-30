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
    """signo_documento × importe — la misma regla que bank_helpers.

    SIN caso especial para importes negativos: un ND negativo es la devolución
    de un egreso y la plata sube. Ver la nota en check_bancos.
    """
    return float(imp or 0) * (
        1 if (doc or "").upper().strip() in _DOCS_ENTRADA else -1)


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
        -- OJO con los nombres: la tabla tiene `documento` (DE/CH/ND/NC…) y
        -- `numreferencia`, NO `no_documento`. Pedir la columna equivocada acá
        -- fue el segundo tropiezo de esta pantalla; el try/except lo dijo en
        -- vez de colgarse, que era justamente el punto.
        SELECT id_transaccion, fecha, documento, importe, saldo,
               COALESCE(numreferencia, 0) AS numreferencia,
               COALESCE(concepto, '')      AS concepto,
               COALESCE(prov, '')          AS prov
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
         -- por ID, igual que check_bancos: el running se estampa en el
         -- insert tomando la fila anterior POR ID. Ver la nota larga allá.
         ORDER BY id_transaccion ASC
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
    prev_id = filas[drift_i - 1]["id_transaccion"] if drift_i > 0 else None
    delta = round(float(filas[drift_i]["saldo"] or 0)
                  - float(calc_por_fila[filas[drift_i]["id_transaccion"]]), 2)
    return (
        [dict(f, _calc=calc_por_fila.get(f["id_transaccion"])) for f in filas[lo:hi]],
        {"drift_id": filas[drift_i]["id_transaccion"], "total_filas": len(filas),
         "prev_id": prev_id, "delta": delta},
    )


def _explicar_drift(no_banco: int, info: dict):
    """¿El salto del running es plata que falta, o el check ordena distinto?

    El `saldo` de cada fila lo estampa el trigger EN EL MOMENTO DEL INSERT, o
    sea en orden de inserción. El walk-forward del chequeo camina en orden
    (fecha, id_transaccion). Cuando un movimiento se carga POSTDATADO —un CH
    con fecha futura— los dos órdenes dejan de coincidir y aparece un "drift"
    que no es plata faltante sino dos criterios distintos. `bancos/queries.py`
    ya lo sabe desde el 25/06: el saldo actual lo toma de la última fila con
    fecha <= hoy justamente "para excluir movimientos postdatados con saldo
    viejo".

    Las dos hipótesis se distinguen mirando los ids que caen ENTRE la última
    fila que cerraba y la que no cierra:
      - si el id existe pero con otra fecha → es orden, no plata;
      - si el id NO existe → la fila se borró y su efecto quedó horneado en
        el running: ahí sí falta algo.
    """
    prev_id, drift_id = info.get("prev_id"), info.get("drift_id")
    if not prev_id or not drift_id or drift_id - prev_id <= 1:
        return
    yield (f"     ¿qué hay entre tx#{prev_id} y tx#{drift_id}? "
           f"(Δ = {info.get('delta'):,.2f})\n")
    presentes = db.fetch_all(
        """
        SELECT id_transaccion, no_banco, fecha, documento, importe, saldo,
               COALESCE(concepto, '') AS concepto
          FROM scintela.transacciones_bancarias
         WHERE id_transaccion > %s AND id_transaccion < %s
         ORDER BY id_transaccion
        """, (int(prev_id), int(drift_id))) or []
    vistos = {int(r["id_transaccion"]) for r in presentes}
    for r in presentes:
        marca = "  ← ESTE banco, otra fecha" if int(r["no_banco"]) == int(no_banco) else ""
        yield (f"       tx#{r['id_transaccion']} banco#{r['no_banco']} "
               f"{str(r['fecha'])[:10]} {(r['documento'] or ''):<3} "
               f"$ {float(r['importe'] or 0):>12,.2f}  "
               f"{r['concepto'][:34]}{marca}\n")
    faltantes = [i for i in range(int(prev_id) + 1, int(drift_id)) if i not in vistos]
    if faltantes:
        yield (f"       ids que NO existen en ninguna cuenta: "
               f"{', '.join('#' + str(i) for i in faltantes[:20])}"
               f"{' …' if len(faltantes) > 20 else ''}\n"
               "       → esas filas se BORRARON. Su efecto quedó horneado en\n"
               "         el running de las siguientes: ahí sí falta plata.\n")
    else:
        yield ("       todos los ids existen → el salto es de ORDEN\n"
               "       (running estampado al insertar vs. camino por fecha),\n"
               "       no de plata faltante.\n")


# B) se hace en DOS pasos a propósito. La versión de una sola query tenía
# cuatro subconsultas correlacionadas por fila de `factura`; con la tabla
# entera eso no volvía nunca y la pantalla quedaba colgada. Primero el scan
# simple (¿cuáles están descuadradas?), después los agregados sólo para esos
# ids. Mismo resultado, sin el producto cartesiano.
_SQL_FACT_DESCUADRADAS = """
    SELECT f.id_factura, f.numf, f.numf_completo, f.codigo_cli, f.fecha,
           f.importe, f.abono, f.saldo, f.stat, f.condic,
           -- OJO: scintela.factura NO tiene columna `observacion` (sí la
           -- tienen compra y cheque). Pedirla acá fue el primer error de
           -- esta pantalla. Ver la nota en el informe: facturas.editar()
           -- tiene una rama que escribe `observacion` y explotaría igual.
           ROUND(f.importe - f.abono - f.saldo, 2) AS diff
      FROM scintela.factura f
     WHERE ABS(f.importe - f.abono - f.saldo) > 0.01
       AND TRIM(COALESCE(f.stat, '')) NOT IN ('X', 'Y')
     ORDER BY ABS(f.importe - f.abono - f.saldo) DESC
     LIMIT 200
"""


def _enriquecer_facturas(filas: list[dict]) -> list[dict]:
    """Agrega cheques aplicados / retenido / cierres a un puñado de facturas."""
    if not filas:
        return filas
    ids = [int(f["id_factura"]) for f in filas]
    aplic = {
        int(r["id_fact"]): (int(r["n"] or 0), float(r["suma"] or 0))
        for r in (db.fetch_all(
            "SELECT id_fact, COUNT(*) AS n, SUM(importe) AS suma "
            "  FROM scintela.chequesxfact WHERE id_fact = ANY(%s) "
            " GROUP BY id_fact", (ids,)) or [])
    }
    cierres = {
        int(r["origen_id"]): int(r["n"] or 0)
        for r in (db.fetch_all(
            "SELECT origen_id, COUNT(*) AS n FROM scintela.mov_doble "
            " WHERE origen_table = 'factura' AND origen_id = ANY(%s) "
            "   AND estado = 'activo' "
            "   AND tipo IN ('factura_cerrada_a_t','factura_stat_cambio',"
            "               'totalizar_estado_cuenta') "
            " GROUP BY origen_id", (ids,)) or [])
    }
    # retencion se cruza por (codigo_cli, numf), no por id_factura.
    claves = [(f.get("codigo_cli") or "", int(f.get("numf") or 0)) for f in filas]
    rete: dict = {}
    for r in (db.fetch_all(
        "SELECT codigo_cli, numf, SUM(rete) AS suma FROM scintela.retencion "
        " WHERE numf = ANY(%s) GROUP BY codigo_cli, numf",
        ([k[1] for k in claves],)) or []):
        rete[((r.get("codigo_cli") or ""), int(r.get("numf") or 0))] = float(r["suma"] or 0)
    for f in filas:
        n, suma = aplic.get(int(f["id_factura"]), (0, 0.0))
        f["n_cheques"], f["aplicado"] = n, suma
        f["n_cierres"] = cierres.get(int(f["id_factura"]), 0)
        f["retenido"] = rete.get(((f.get("codigo_cli") or ""), int(f.get("numf") or 0)), 0.0)
    return filas


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
     LIMIT 200
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


_SQL_NUMF0 = """
    SELECT f.id_factura, f.codigo_cli, f.fecha, f.importe, f.abono, f.saldo,
           f.stat, COALESCE(f.usuario_crea, '') AS usuario_crea,
           COALESCE((SELECT SUM(x.importe) FROM scintela.chequesxfact x
                      WHERE x.id_fact = f.id_factura), 0) AS aplicado,
           (SELECT COUNT(*) FROM scintela.chequesxfact x
             WHERE x.id_fact = f.id_factura)               AS n_cheques
      FROM scintela.factura f
     WHERE COALESCE(f.numf, 0) = 0
       AND TRIM(COALESCE(f.stat, '')) NOT IN ('X', 'Y')
     ORDER BY f.fecha DESC, f.id_factura DESC
     LIMIT 400
"""


def _seccion_numf0():
    """Las facturas sin N° SRI, cruzadas contra Asinfo.

    POR QUÉ (pedido de la dueña, 29/07): las facturas con NUMF=0 son las que
    aparecen sobre-abonadas, muchas con importe NEGATIVO, y el pareo contra el
    dBase no las puede tocar — `diff_facturas_sri` las aparta en
    `dbase_sin_numf` porque sin N° SRI no hay con qué aparearlas. Su hipótesis:
    no son facturas. Si en Asinfo son notas de crédito, devoluciones o NTEN,
    entonces el patrón es "documento que no es factura, cargado en
    FACTURAS.DBF sin numerar" — y eso se puede detectar en el origen en vez de
    perseguirlo acá.

    Esta sección busca cada NUMF=0 de PC en Asinfo por (cliente, importe) y
    reporta con qué TIPO de documento matchea. Solo lectura.
    """
    filas = db.fetch_all(_SQL_NUMF0) or []
    yield "┌─ E) FACTURAS NUMF=0 (sin N° SRI) contra Asinfo ───────────────\n"
    if not filas:
        yield "  no hay facturas activas con numf=0.\n"
        return
    fechas = [f["fecha"] for f in filas if f.get("fecha")]
    desde, hasta = (min(fechas), max(fechas)) if fechas else (None, None)
    yield (f"  {len(filas)} fila(s) activas, fechas {desde} → {hasta}\n"
           "  … pidiéndole a Asinfo los documentos del rango\n")

    # Índice de Asinfo por (cliente, |usd|). El importe es la única clave
    # común: PC no guarda el número del documento cuando el dBase no lo trae.
    idx: dict = {}
    idx_importe: dict = {}   # sólo por |usd|, para el segundo intento
    tipos_asinfo: dict = {}
    try:
        from modules.asinfo import service as _asinfo
        docs = _asinfo.facturas_periodo(desde, hasta) if desde else []
        for d in docs:
            tipos_asinfo[d.get("tipo") or "?"] = tipos_asinfo.get(d.get("tipo") or "?", 0) + 1
            k = ((str(d.get("cliente_codigo") or "").strip().upper()),
                 round(abs(float(d.get("usd") or 0)), 2))
            idx.setdefault(k, []).append(d)
            idx_importe.setdefault(k[1], []).append(d)
        yield (f"  Asinfo devolvió {len(docs)} documento(s): "
               + ", ".join(f"{t}={n}" for t, n in sorted(tipos_asinfo.items()))
               + "\n")
    except Exception as exc:  # noqa: BLE001
        yield f"  [Asinfo no contestó: {exc!r}] — sigo sin el cruce\n"

    yield ("  id_fact   cli   fecha        importe       abono     aplicado  ch "
           "usuario_crea      match en Asinfo\n")
    resumen: dict = {}
    for f in filas:
        k = ((f.get("codigo_cli") or "").strip().upper(),
             round(abs(float(f.get("importe") or 0)), 2))
        cand = idx.get(k) or []
        if cand:
            etiqueta = f"{cand[0].get('tipo')} {cand[0].get('numero') or ''}"[:34]
            clave_res = cand[0].get("tipo") or "?"
        else:
            # Segundo intento IGNORANDO el código de cliente. Si el importe
            # aparece en Asinfo pero bajo OTRO código, el documento existe: lo
            # que difiere es cómo cada sistema nombra al mismo cliente. Ya pasó
            # con AJO/AJ2 y CLR/CL2, y hay DOS mecanismos según el caso —
            # `cliente_alias` si Asinfo usa un código propio, y
            # `sucursal_direccion` si Asinfo manda todo bajo la empresa y el
            # dBase parte por dirección de entrega (el caso AJO/AJ2 de julio).
            # Para saber cuál hace falta, primero hay que ver adónde cae.
            otro = idx_importe.get(k[1]) or []
            if otro:
                cli_a = str(otro[0].get("cliente_codigo") or "?").strip().upper()
                etiqueta = (f"! mismo importe, cliente {cli_a}: "
                            f"{otro[0].get('tipo')} {otro[0].get('numero') or ''}")[:52]
                clave_res = f"OTRO CLIENTE ({k[0]} -> {cli_a})"
            else:
                etiqueta = "— no aparece en Asinfo con ese importe"
                clave_res = "SIN MATCH"
        resumen[clave_res] = resumen.get(clave_res, 0) + 1
        yield (f"  {f['id_factura']:<9} {(f['codigo_cli'] or '')[:5]:<5} "
               f"{str(f['fecha'])[:10]} "
               f"{float(f['importe'] or 0):>12,.2f} "
               f"{float(f['abono'] or 0):>11,.2f} "
               f"{float(f['aplicado'] or 0):>12,.2f} "
               f"{int(f['n_cheques'] or 0):>3} "
               f"{(f['usuario_crea'] or 'NULL')[:17]:<17} {etiqueta}\n")

    yield "\n  RESUMEN por tipo de documento en Asinfo:\n"
    for k, n in sorted(resumen.items(), key=lambda kv: -kv[1]):
        yield f"    {k:<22} {n:>4}\n"
    # "¿en todas?" — no: la mayoría de las NUMF=0 no tiene ni un cheque.
    con_ch = [f for f in filas if int(f.get("n_cheques") or 0) > 0]
    sobre = [f for f in con_ch
             if float(f.get("aplicado") or 0) - float(f.get("importe") or 0) > 0.01]
    _exc = sum(float(f["aplicado"] or 0) - float(f["importe"] or 0) for f in sobre)
    yield (f"\n  Con cheques aplicados: {len(con_ch)} de {len(filas)}\n"
           f"  De esas, SOBRE-abonadas: {len(sobre)} "
           f"(exceso $ {_exc:,.2f})\n")
    if sobre:
        top = sorted(sobre, key=lambda f: -(float(f["aplicado"] or 0)
                                            - float(f["importe"] or 0)))[:5]
        _t5 = sum(float(f["aplicado"] or 0) - float(f["importe"] or 0) for f in top)
        yield (f"  Las 5 peores concentran $ {_t5:,.2f} "
               f"({_t5 / _exc * 100:.0f}% del exceso):\n")
        for f in top:
            yield (f"    #{f['id_factura']} {(f['codigo_cli'] or '')[:4]:<4} "
                   f"{str(f['fecha'])[:10]} importe {float(f['importe'] or 0):>10,.2f} "
                   f"aplicado {float(f['aplicado'] or 0):>10,.2f} "
                   f"exceso {float(f['aplicado'] or 0) - float(f['importe'] or 0):>10,.2f}\n")
    negativas = [f for f in filas if float(f.get("importe") or 0) < 0]
    yield (f"\n  Con importe NEGATIVO: {len(negativas)} de {len(filas)} "
           f"(suma {sum(float(f['importe'] or 0) for f in negativas):,.2f})\n")
    porc = {}
    for f in filas:
        u = (f.get("usuario_crea") or "NULL")
        porc[u] = porc.get(u, 0) + 1
    yield "  Quién las cargó (usuario_crea):\n"
    for u, n in sorted(porc.items(), key=lambda kv: -kv[1]):
        yield f"    {u[:24]:<24} {n:>4}\n"
    yield "\n"


def _seccion_diccionario():
    """Facturas que PC tiene bajo OTRO nombre — y qué entrada las arregla.

    POR QUÉ (dueña, 29/07): "hacelo con todos los casos que encuentre para
    facturas en otros nombres". No un parche para VP1: la detección general.

    Cómo funciona. Asinfo manda cada documento con el código de la EMPRESA
    (`cliente_codigo`) y con la DIRECCIÓN de entrega (`id_direccion_empresa`).
    El dBase, en cambio, parte algunos clientes por sucursal y les da código
    propio — AJO/AJ2, y VPM/VP1. Así que la misma factura aparece en Asinfo
    bajo la empresa y en PC bajo la sucursal, y ningún pareo por código la
    encuentra.

    La dirección es la que desempata, y ya está en la card 199 (por eso
    /facturas/desde-asinfo la lee): sólo había que pedirla. Para cada
    documento de Asinfo que PC tiene con el MISMO importe pero OTRO código,
    se propone la entrada de `scintela.sucursal_direccion`
    (id_direccion → código PC) — el mismo mecanismo con el que ya se resolvió
    AJ2 (22657→AJ2).

    Solo lectura: propone, no escribe. Se registra desde
    /facturas/sucursal-direccion/agregar.
    """
    yield "┌─ F) DICCIONARIO — facturas que PC tiene bajo otro nombre ─────\n"
    filas = db.fetch_all(
        """
        SELECT f.id_factura, f.codigo_cli, f.fecha, f.importe, f.numf
          FROM scintela.factura f
         WHERE f.fecha >= CURRENT_DATE - INTERVAL '120 days'
           AND TRIM(COALESCE(f.stat, '')) NOT IN ('X', 'Y')
           AND ABS(COALESCE(f.importe, 0)) > 0.01
        """) or []
    if not filas:
        yield "  sin facturas en los últimos 120 días.\n"
        return
    # PC indexado por |importe| en centavos → set de códigos
    pc_por_imp: dict = {}
    for f in filas:
        c = int(round(abs(float(f["importe"] or 0)) * 100))
        pc_por_imp.setdefault(c, set()).add((f.get("codigo_cli") or "").strip().upper())
    fechas = [f["fecha"] for f in filas if f.get("fecha")]
    desde, hasta = min(fechas), max(fechas)
    yield (f"  PC: {len(filas)} facturas vivas entre {desde} y {hasta}\n"
           "  … pidiéndole a Asinfo los documentos del rango\n")
    try:
        from modules.asinfo import service as _asinfo
        docs = _asinfo.facturas_periodo(desde, hasta) or []
    except Exception as exc:  # noqa: BLE001
        yield f"  [Asinfo no contestó: {exc!r}]\n"
        return
    con_dir = sum(1 for d in docs if d.get("id_direccion_empresa"))
    yield (f"  Asinfo: {len(docs)} documentos, {con_dir} con dirección de "
           f"entrega\n")
    if not con_dir:
        yield ("  [la card no está devolviendo id_direccion_empresa — sin ese "
               "campo\n   no se puede distinguir la sucursal de la empresa]\n\n")

    # (id_direccion, cliente_asinfo, codigo_pc) → [ejemplos]
    prop: dict = {}
    for d in docs:
        cli_a = (str(d.get("cliente_codigo") or "")).strip().upper()
        c = int(round(abs(float(d.get("usd") or 0)) * 100))
        if not cli_a or not c:
            continue
        otros = {x for x in (pc_por_imp.get(c) or set()) if x and x != cli_a}
        if not otros:
            continue
        for pc_cod in otros:
            k = (d.get("id_direccion_empresa"), cli_a, pc_cod)
            prop.setdefault(k, []).append(d)

    if not prop:
        yield "  ✓ ningún documento de Asinfo aparece en PC bajo otro código.\n\n"
        return
    # Ordenar por cantidad de coincidencias: más filas = más confianza.
    ordenadas = sorted(prop.items(), key=lambda kv: -len(kv[1]))
    yield ("  Asinfo dice un código, PC tiene otro, mismo importe. La columna\n"
           "  'dir' es la que hace de árbitro:\n")
    yield "  n    dir        Asinfo → PC       ejemplos (tipo / número)\n"
    for (iddir, cli_a, pc_cod), ej in ordenadas[:40]:
        muestras = ", ".join(
            f"{(e.get('tipo') or '?')[:4]} {e.get('numero') or ''}"
            for e in ej[:2])
        yield (f"  {len(ej):<4} {str(iddir or '—'):<10} "
               f"{cli_a:<4} → {pc_cod:<4}      {muestras[:52]}\n")
    if len(ordenadas) > 40:
        yield f"  … y {len(ordenadas) - 40} combinaciones más\n"

    # Propuestas ACCIONABLES — con el filtro que las hace creíbles.
    #
    # Matchear sólo por importe genera ruido: las notas de crédito financieras
    # repiten montos y cualquier par cliente/importe coincide de casualidad.
    # Se ve en la data: la dirección 22657 aparece apuntando a AJ2 (136 docs)
    # pero también a PGQ (8), ERA (5), VPM (4), JCP (3)…
    #
    # Y ahí está la regla, porque una dirección de entrega pertenece a UN
    # cliente y nada más: si una dirección apunta a varios códigos de PC, todos
    # menos uno son casualidad. Se calcula el DOMINIO — qué porcentaje de las
    # coincidencias de esa dirección se va al código ganador — y sólo se
    # proponen las que ganan claro.
    #
    # Validación del método: el primer resultado por dominio es 22657 → AJ2,
    # que es textualmente la fila del seed de la migración 0132. El detector
    # redescubre una respuesta ya sabida correcta sin que nadie se la diga.
    por_dir: dict = {}
    for (iddir, cli_a, pc_cod), ej in prop.items():
        if not iddir:
            continue
        por_dir.setdefault(iddir, []).append((pc_cod, cli_a, len(ej)))
    UMBRAL_DOMINIO, MIN_DOCS = 0.60, 3
    fuertes, debiles = [], []
    for iddir, cands in por_dir.items():
        total = sum(n for _, _, n in cands)
        pc_cod, cli_a, n = max(cands, key=lambda c: c[2])
        dom = n / total if total else 0
        fila = (iddir, pc_cod, cli_a, n, total, dom)
        (fuertes if (dom >= UMBRAL_DOMINIO and n >= MIN_DOCS) else debiles).append(fila)
    fuertes.sort(key=lambda f: -f[3])

    yield (f"\n  Direcciones con un ganador claro (≥{int(UMBRAL_DOMINIO*100)}% "
           f"de sus coincidencias y ≥{MIN_DOCS} documentos): {len(fuertes)}\n")
    yield "    dir        → PC    docs  dominio   Asinfo la manda como\n"
    for iddir, pc_cod, cli_a, n, total, dom in fuertes:
        yield (f"    {iddir:<10} → {pc_cod:<4} {n:>5}/{total:<4} "
               f"{dom*100:>5.0f}%   {cli_a}\n")
    yield (f"\n  Descartadas por reparto ambiguo: {len(debiles)} direcciones.\n"
           "  Una dirección pertenece a UN cliente: si apunta a varios, es\n"
           "  coincidencia de importes (las notas de crédito repiten montos),\n"
           "  no una sucursal.\n")
    yield ("\n  Registrar en /facturas/sucursal-direccion/agregar "
           "(id_direccion + código PC).\n\n")


def _money_txt(x) -> str:
    """Importe en formato EU/Ecuador (punto=miles, coma=decimal), ancho fijo."""
    s = f"{float(x or 0):,.2f}"
    return f"{s.replace(',', '@').replace('.', ',').replace('@', '.'):>14}"


def _seccion_recode_sucursales():
    """Cuánto movería recodear a la SUCURSAL las facturas ya cargadas.

    POR QUÉ (dueña, 30/07): *"entonces cuanto cambiaria? porque los ultimos 4
    meses ya deberian estar cargados"*. Exacto: la regla nueva (la sucursal se
    detecta por el RUC — ver `asinfo.service.sucursal_por_numero`) arregla lo
    que se cargue de ahora en más, pero lo que YA está en PC entró con el código
    de la MATRIZ. Esta sección mide eso, apareando por **N° de factura** (no por
    importe, que es lo que hace la sección F y por eso trae ruido de notas de
    crédito con montos repetidos).

    Lo que se mueve es la CARTERA POR CLIENTE, no el total: la misma plata
    cambia de cuenta. El número que importa es el SALDO ABIERTO (stat Z/A) —
    eso es lo que alguien va a ir a cobrar al cliente equivocado.

    Solo lectura: mide, no recodea.
    """
    yield "┌─ G) SUCURSALES — cuánto movería el recode ──────────────────────\n"
    filas = db.fetch_all(
        """
        SELECT f.codigo_cli, f.numf, f.numf_completo, f.fecha,
               f.importe, f.saldo, f.stat
          FROM scintela.factura f
         WHERE f.fecha >= CURRENT_DATE - INTERVAL '120 days'
           AND TRIM(COALESCE(f.stat, '')) NOT IN ('X', 'Y')
           AND COALESCE(f.numf, 0) > 0
        """) or []
    if not filas:
        yield "  sin facturas en los últimos 120 días.\n\n"
        return
    # DOS índices, y el orden importa. `numf` es la cola numérica del N° SRI,
    # y la serie de las NOTAS DE CRÉDITO (001-099-0000115xx → 11515) COLISIONA
    # con las facturas viejas de ese mismo número: aparear sólo por numf metía
    # falsos positivos ("11 PC=BED debería ser AJ2"). El N° SRI completo no
    # colisiona nunca, así que manda; numf queda como respaldo para las filas
    # que el sync del dBase cargó sin numf_completo.
    pc_por_completo: dict = {}
    pc_por_numf: dict = {}
    for f in filas:
        pc_por_numf.setdefault(int(f["numf"]), []).append(f)
        nc = (f.get("numf_completo") or "").strip()
        if nc:
            pc_por_completo.setdefault(nc, []).append(f)
    fechas = [f["fecha"] for f in filas if f.get("fecha")]
    desde, hasta = min(fechas), max(fechas)
    yield f"  PC: {len(filas)} facturas vivas entre {desde} y {hasta}\n"
    try:
        from modules.asinfo import service as _asinfo
        docs = _asinfo.facturas_periodo(desde, hasta) or []
    except Exception as exc:  # noqa: BLE001
        yield f"  [Asinfo no contestó: {exc!r}]\n\n"
        return
    de_sucursal = [d for d in docs if d.get("cliente_codigo_facturado")]
    yield (f"  Asinfo: {len(docs)} documentos, {len(de_sucursal)} que la regla "
           f"del RUC manda a una SUCURSAL\n")
    if not de_sucursal:
        yield ("  [la regla no marcó ninguno — ¿Metabase contestó? ver "
               "asinfo.sucursal_por_numero]\n\n")
        return

    import re as _re
    # (matriz → sucursal) → acumulado
    mover: dict = {}
    ya_ok = 0
    sin_cargar = 0
    n_completo = 0
    n_solo_numf = 0
    otro_codigo: dict = {}
    for d in de_sucursal:
        num = str(d.get("numero") or "")
        m = _re.findall(r"\d+", num)
        if not m:
            continue
        numf = int(m[-1])
        rows = pc_por_completo.get(num.strip())
        por_completo = bool(rows)
        if not rows:
            rows = pc_por_numf.get(numf)
        if not rows:
            sin_cargar += 1
            continue
        if por_completo:
            n_completo += 1
        else:
            n_solo_numf += 1
        suc = str(d.get("cliente_codigo") or "").strip().upper()
        matriz = str(d.get("cliente_codigo_facturado") or "").strip().upper()
        for r in rows:
            pc_cod = (r.get("codigo_cli") or "").strip().upper()
            if pc_cod == suc:
                ya_ok += 1
                continue
            if pc_cod != matriz:
                # PC lo tiene bajo un TERCER código: no lo toca un recode
                # automático, va a mano.
                otro_codigo[(pc_cod, suc)] = otro_codigo.get((pc_cod, suc), 0) + 1
                continue
            acc = mover.setdefault((matriz, suc), {"n": 0, "imp": 0.0, "saldo": 0.0})
            acc["n"] += 1
            acc["imp"] += float(r.get("importe") or 0)
            if (r.get("stat") or "").strip().upper() in ("Z", "A", ""):
                acc["saldo"] += float(r.get("saldo") or 0)

    if not mover:
        yield (f"  ✓ nada que recodear: {ya_ok} ya están bajo la sucursal, "
               f"{sin_cargar} no están en PC.\n\n")
        return
    yield "\n  A RECODEAR (PC tiene la matriz, Asinfo dice la sucursal):\n"
    yield "  n      de  →  a        importe        saldo abierto\n"
    tot_n = tot_imp = tot_saldo = 0.0
    for (matriz, suc), a in sorted(mover.items(), key=lambda kv: -kv[1]["n"]):
        yield (f"  {a['n']:<6} {matriz:<4} → {suc:<4}   "
               f"{_money_txt(a['imp'])}  {_money_txt(a['saldo'])}\n")
        tot_n += a["n"]
        tot_imp += a["imp"]
        tot_saldo += a["saldo"]
    yield (f"  {'-' * 58}\n  {int(tot_n):<6} TOTAL          "
           f"{_money_txt(tot_imp)}  {_money_txt(tot_saldo)}\n")
    yield (f"\n  Ya correctas: {ya_ok}   ·   No están en PC: {sin_cargar}\n"
           f"  Apareadas por N° SRI completo: {n_completo}   ·   "
           f"sólo por numf (puede colisionar): {n_solo_numf}\n")
    if otro_codigo:
        yield ("  Bajo un TERCER código (no las toca un recode automático):\n")
        for (pc_cod, suc), n in sorted(otro_codigo.items(), key=lambda kv: -kv[1])[:10]:
            yield f"    {n:<4} PC={pc_cod:<4} debería ser {suc}\n"
    yield ("\n  OJO: el total de cartera NO cambia — la misma plata pasa de la\n"
           "  cuenta de la matriz a la de la sucursal. Lo que cambia es a QUIÉN\n"
           "  se le va a cobrar el saldo abierto de la última columna.\n\n")


@bp.route("/diag", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def diag():
    """Contexto de los errores rojos del chequeo. Solo lectura.

    `?solo=a` … `g` corre UNA sección. Vale la pena: la A camina
    el running completo de cada banco (decenas de miles de filas) y no hay por
    qué esperarla cuando lo que querés ver son las facturas.
    """
    pedido = {c for c in (request.args.get("solo") or "").lower() if c in "abcdefg"}

    def quiero(k: str) -> bool:
        return (not pedido) or (k in pedido)

    def _gen():
        # Si una sección explota, el generador muere en el medio del stream y
        # el navegador se queda cargando PARA SIEMPRE — sin error, sin
        # timeout, sin nada que mirar. Es exactamente lo que pasó la primera
        # vez que se abrió esta pantalla. El try/except convierte "se cuelga"
        # en "dice qué pasó", que es la diferencia entre una herramienta de
        # diagnóstico y otro misterio.
        try:
            yield from _secciones(quiero, pedido)
        except Exception:  # noqa: BLE001
            import traceback
            yield "\n[EXPLOTÓ] la sección se cortó acá:\n"
            yield traceback.format_exc()

    def _secciones(quiero, pedido):
        yield ("=== DIAGNÓSTICO PROFUNDO — contexto de los errores rojos ===\n"
               "    (solo lectura: ni un UPDATE)"
               + (f"  [secciones {''.join(sorted(pedido)).upper()}]" if pedido
                  else "  (?solo=a|b|c|d|e|f|g para una sola)")
               + "\n\n")

        # ── 1) Drift del running bancario ──────────────────────────────
        if quiero("a"):
            yield "┌─ A) BANCOS — contexto del primer drift del running ───────────\n"
        for b in (db.fetch_all(
            "SELECT no_banco, COALESCE(nombre,'') AS nombre FROM scintela.banco "
            " WHERE EXISTS (SELECT 1 FROM scintela.transacciones_bancarias t "
            "               WHERE t.no_banco = banco.no_banco) "
            " ORDER BY no_banco") or []) if quiero("a") else []:
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
                       f"{str(f.get('numreferencia') or '')[:9]:<9} "
                       f"{float(f['importe'] or 0):>13,.2f} "
                       f"{(float(sr) if sr is not None else 0):>13,.2f} "
                       f"{(float(calc) if calc is not None else 0):>13,.2f} "
                       f"{(d if d is not None else 0):>10,.2f}  "
                       f"{(f.get('prov') or '')[:6]} {f['concepto'][:34]}{marca}\n")
            yield from _explicar_drift(int(b["no_banco"]), info)
            yield "\n"

        # ── 2) Facturas descuadradas ───────────────────────────────────
        desc: list = []
        if quiero("b") or quiero("c"):
            # C necesita los ids de B para el solapamiento.
            # Los "…" son marcas de progreso a propósito: el stream sale a
            # medida que avanza, así que si una query tarda se ve DÓNDE está
            # esperando en vez de una pantalla en blanco.
            yield "  … buscando facturas descuadradas\n"
            desc = db.fetch_all(_SQL_FACT_DESCUADRADAS) or []
            yield f"  … {len(desc)} encontradas, buscando sus cheques\n"
            desc = _enriquecer_facturas(desc)
            yield "  … listo\n"
        if quiero("b"):
            yield "┌─ B) FACTURAS con saldo ≠ importe − abono ─────────────────────\n"
            yield (f"  {len(desc)} factura(s). 'diff' = importe − abono − saldo "
                   "(positivo = el saldo quedó MÁS BAJO de lo que corresponde).\n")
            yield ("  id_fact   numf      cli   stat      importe       abono  "
                   "     saldo        diff  ch  aplicado  retenido cierres\n")
        _tot_diff = 0.0
        for f in (desc if quiero("b") else []):
            _tot_diff += float(f["diff"] or 0)
            yield (f"  {f['id_factura']:<9} {str(f['numf'])[:9]:<9} "
                   f"{(f['codigo_cli'] or '')[:5]:<5} {(f['stat'] or '')[:4]:<4} "
                   f"{str(f['fecha'])[:10]} "
                   f"{float(f['importe'] or 0):>12,.2f} "
                   f"{float(f['abono'] or 0):>11,.2f} "
                   f"{float(f['saldo'] or 0):>11,.2f} "
                   f"{float(f['diff'] or 0):>11,.2f} "
                   f"{int(f['n_cheques'] or 0):>3} "
                   f"{float(f['aplicado'] or 0):>9,.2f} "
                   f"{float(f['retenido'] or 0):>9,.2f} "
                   f"{int(f['n_cierres'] or 0):>4}\n")
        if quiero("b"):
            yield f"  TOTAL diff: {_tot_diff:,.2f}\n"
            yield ("  (cierres = mov_doble activos de tipo factura_cerrada_a_t / "
                   "factura_stat_cambio / totalizar_estado_cuenta)\n\n")

        # ── 3) Sobre-abonadas ──────────────────────────────────────────
        sob: list = []
        if quiero("c"):
            yield "┌─ C) FACTURAS sobre-abonadas (Σ cheques aplicados > importe) ──\n"
            yield "  … agrupando chequesxfact por factura\n"
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
        if quiero("c"):
            ids_desc = {f["id_factura"] for f in desc}
            ids_sob = {r["id_factura"] for r in sob}
            yield (f"\n  Solapamiento B∩C: {len(ids_desc & ids_sob)} factura(s) "
                   f"(B={len(ids_desc)}, C={len(ids_sob)})\n\n")

        # ── 4) Links rotos de mov_doble ────────────────────────────────
        rotos: list = []
        if quiero("d"):
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

        # ── 5) Las facturas NUMF=0 contra Asinfo ───────────────────────
        if quiero("e"):
            yield from _seccion_numf0()

        # ── 6) Propuestas para el diccionario de códigos ───────────────
        if quiero("f"):
            yield from _seccion_diccionario()

        # ── 7) Cuánto movería el recode de sucursales ──────────────────
        if quiero("g"):
            yield from _seccion_recode_sucursales()

        yield "\n═══ fin ═══\n"

    return Response(stream_with_context(_gen()), mimetype="text/plain; charset=utf-8")
