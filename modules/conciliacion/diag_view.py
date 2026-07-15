"""Endpoint diagnóstico /admin/diag-pendientes-banco — TMT 2026-06-02.

La dueña preguntó: '548 pendientes para conciliar, no estarán repetidos?'.
El dedupe por número de documento que pusimos en mig 0062 corre al subir
extracto NUEVO — no limpia lo que ya estaba en banco_historicos_pendientes
del backfill viejo (migs 0056-0058).

Este endpoint cuenta cuántas filas pendientes hay duplicadas por
(no_banco, documento) y muestra ejemplos. Si hay muchos duplicados,
podemos limpiarlos.
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, jsonify, request

import db as _db
from auth import requiere_login, requiere_permiso


def _usuario_actual() -> str:
    """Wrapper lazy para evitar circular imports cuando el módulo
    diag_view se carga ANTES de modules.conciliacion.views."""
    try:
        from modules.conciliacion.views import _usuario_actual as _ua
        return _ua()
    except Exception:
        from flask import session
        return (session.get("usuario") or "web")[:50]

_LOG = logging.getLogger("programa_core.conciliacion.diag")
_BANCO_PICHINCHA = 10

bp = Blueprint(
    "conciliacion_diag",
    __name__,
    url_prefix="/admin/diag-pendientes-banco",
)


@bp.route("/inspeccionar-mov", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def inspeccionar_mov():
    """Diag puntual (read-only): dado uno o más documentos del banco, muestra el
    estado del histórico (conciliado_en/match), los matches por documento, y las
    transacciones PC con ese |monto| (stat). Para entender por qué un mov ya
    conciliado sigue apareciendo pendiente. ?docs=28481473,28481477"""
    def _clean(rows):
        return [{k: (str(v) if v is not None else None) for k, v in r.items()} for r in rows]
    docs = [d.strip() for d in (request.args.get("docs") or request.args.get("doc") or "").split(",") if d.strip()]
    out: dict = {}
    montos: list = []
    for _m in (request.args.get("montos") or "").split(","):
        _m = _m.strip()
        if _m:
            try:
                montos.append(round(abs(float(_m)), 2))
            except ValueError:
                pass
    for doc in docs:
        histos = _db.fetch_all(
            "SELECT id, no_banco, fecha, documento, monto, tipo, conciliado_en, conciliado_por, conciliado_match_id "
            "FROM scintela.banco_historicos_pendientes WHERE documento = %s ORDER BY id", (doc,)) or []
        matches = _db.fetch_all(
            "SELECT id, id_transaccion, real_fecha, real_documento, real_monto, real_tipo, deshecho_en, no_banco "
            "FROM scintela.banco_conciliacion_match WHERE real_documento = %s ORDER BY id", (doc,)) or []
        for h in histos:
            try:
                montos.append(round(abs(float(h["monto"])), 2))
            except (TypeError, ValueError):
                pass
        out[doc] = {"histos": _clean(histos), "matches_por_doc": _clean(matches)}
    tx = []
    if montos:
        tx = _db.fetch_all(
            "SELECT id_transaccion, fecha, documento, importe, stat, no_banco "
            "FROM scintela.transacciones_bancarias WHERE ROUND(ABS(importe),2) = ANY(%s) ORDER BY fecha", (montos,)) or []
    matches_por_tx = []
    if tx:
        ids = [r["id_transaccion"] for r in tx if r.get("id_transaccion") is not None]
        if ids:
            matches_por_tx = _db.fetch_all(
                "SELECT id, id_transaccion, real_fecha, real_documento, real_monto, deshecho_en "
                "FROM scintela.banco_conciliacion_match WHERE id_transaccion = ANY(%s) ORDER BY id", (ids,)) or []
    histos_monto = []
    matches_monto = []
    if montos:
        histos_monto = _db.fetch_all(
            "SELECT id, no_banco, fecha, documento, monto, tipo, conciliado_en, conciliado_por "
            "FROM scintela.banco_historicos_pendientes WHERE ROUND(ABS(monto),2) = ANY(%s) ORDER BY fecha", (montos,)) or []
        matches_monto = _db.fetch_all(
            "SELECT * FROM scintela.banco_conciliacion_match "
            "WHERE ROUND(ABS(real_monto),2) = ANY(%s) ORDER BY id", (montos,)) or []
    fecha = (request.args.get("fecha") or "").strip()
    tx_fecha = []; ext_fecha = None
    if fecha:
        tx_fecha = _db.fetch_all(
            "SELECT id_transaccion, fecha, documento, importe, stat, no_banco FROM scintela.transacciones_bancarias "
            "WHERE fecha = %s ORDER BY importe", (fecha,)) or []
    out["_tx_por_monto"] = _clean(tx)
    out["_matches_de_esas_tx"] = _clean(matches_por_tx)
    out["_histos_por_monto"] = _clean(histos_monto)
    out["_matches_por_monto"] = _clean(matches_monto)
    out["_tx_por_fecha"] = _clean(tx_fecha)
    return jsonify(out)


@bp.route("/estado-sesion-dump", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def estado_sesion_dump():
    """Read-only: dumpea los buckets de estado_sesion(sesion) filtrando por texto
    en el concepto — para ver en qué bucket cae un mov y si está cruzado."""
    from modules.conciliacion import sesion as _s
    sid = int(request.args.get("sesion_id") or 0)
    filtro = (request.args.get("filtro") or "").upper()
    ses = _s.sesion_por_id(sid) if sid else None
    if not ses:
        return jsonify({"error": "sesion no encontrada", "sesion_id": sid}), 404
    no_banco = ses.get("no_banco")
    try:
        est = _s.estado_sesion(ses, no_banco)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"estado_sesion fallo: {e!r}"}), 500
    out = {"sesion_id": sid, "no_banco": no_banco, "buckets": {}}
    for bname in ("manual_banco", "impuestos", "manual_programa", "transferencias"):
        rows = []
        for it in (est.get(bname) or []):
            mv = it.get("mov")
            if mv is None:
                continue
            concepto = str(getattr(mv, "concepto", "") or "")
            if filtro and filtro not in concepto.upper():
                continue
            rows.append({
                "fecha": str(getattr(mv, "fecha", None)),
                "documento": str(getattr(mv, "documento", "") or ""),
                "monto": str(getattr(mv, "monto", 0) or 0),
                "tipo": str(getattr(mv, "tipo", "") or ""),
                "concepto": concepto[:70],
                "es_historico": bool(it.get("es_historico")),
                "cruzado": str(it.get("cruzado") or it.get("match") or it.get("programa") or ""),
                "keys": [k for k in it.keys()],
            })
        out["buckets"][bname] = rows
    return jsonify(out)


@bp.route("/recruzar-perdidos", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def recruzar_perdidos():
    """REPARACIÓN puntual (TMT 2026-07-15): reconstruye matches de banco que se
    PERDIERON cuando borrar-sesion los borró en duro por ventana de tiempo. El
    programa quedó marcado conciliado (stat='*') pero el banco sin el cruce → los
    movimientos reaparecían en el export de pendientes (caso importación INV30405
    + cheques 15542/15543 que Alex ya había cruzado).

    Recrea el match usando el mov REAL del extracto de la sesión (por documento),
    con la MISMA firma (fecha+doc+monto+tipo) → el matcher los excluye de
    pendientes igual que si se volvieran a cruzar en la pantalla. Reversible desde
    'Deshacer conciliados'. Idempotente (ON CONFLICT DO NOTHING).

    Params (GET o POST):
      sesion_id : de qué sesión tomar los movs del extracto.
      match     : pares doc:id_transaccion separados por coma (banco↔programa).
      accept    : docs a aceptar como real_only (banco sin contraparte de banco
                  en el programa — ej. importaciones cargadas como anticipo).
      confirm   : 'SI' para ejecutar. Sin eso = dry-run (muestra el plan).
    """
    from modules.conciliacion import sesion as _s
    from modules.conciliacion.matcher_banco import confirmar_match, confirmar_real_only

    sid = int(request.values.get("sesion_id") or 0)
    ses = _s.sesion_por_id(sid) if sid else None
    if not ses:
        return jsonify({"error": "sesion no encontrada", "sesion_id": sid}), 404
    no_banco = ses.get("no_banco") or _BANCO_PICHINCHA
    confirmar = (request.values.get("confirm") or "").strip().upper() == "SI"
    usuario = _usuario_actual()

    movs = _s.cargar_movs(ses) or []
    por_doc: dict = {}
    for mv in movs:
        d = (getattr(mv, "documento", "") or "").strip()
        if d:
            por_doc.setdefault(d, mv)

    pares = []
    for tok in (request.values.get("match") or "").split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        d, _, idtx = tok.partition(":")
        try:
            pares.append((d.strip(), int(idtx.strip())))
        except ValueError:
            pass
    accepts = [d.strip() for d in (request.values.get("accept") or "").split(",") if d.strip()]

    def _movinfo(mv):
        return {"fecha": str(getattr(mv, "fecha", "")),
                "monto": str(getattr(mv, "monto", "")),
                "tipo": getattr(mv, "tipo", ""),
                "concepto": str(getattr(mv, "concepto", ""))[:60]}

    plan = []
    resultados = []
    for d, idtx in pares:
        mv = por_doc.get(d)
        if not mv:
            plan.append({"doc": d, "accion": "match", "id_transaccion": idtx,
                         "estado": "MOV_NO_ENCONTRADO_EN_EXTRACTO"})
            continue
        info = {"doc": d, "accion": "match", "id_transaccion": idtx, **_movinfo(mv)}
        plan.append(info)
        if confirmar:
            try:
                n = confirmar_match(no_banco, mv, idtx, estado="matched",
                                    usuario=usuario, metodo="reparacion_match_perdido")
                resultados.append({**info, "insertado": int(n or 0)})
            except Exception as e:  # noqa: BLE001
                resultados.append({**info, "error": str(e)})
    for d in accepts:
        mv = por_doc.get(d)
        if not mv:
            plan.append({"doc": d, "accion": "accept",
                         "estado": "MOV_NO_ENCONTRADO_EN_EXTRACTO"})
            continue
        info = {"doc": d, "accion": "accept(real_only)", **_movinfo(mv)}
        plan.append(info)
        if confirmar:
            try:
                n = confirmar_real_only(no_banco, mv, usuario=usuario)
                resultados.append({**info, "insertado": int(n or 0)})
            except Exception as e:  # noqa: BLE001
                resultados.append({**info, "error": str(e)})

    return jsonify({
        "sesion_id": sid, "no_banco": no_banco, "confirmado": confirmar,
        "movs_en_extracto": len(por_doc),
        "plan": plan, "resultados": resultados,
        "nota": ("dry-run: agregá &confirm=SI para ejecutar"
                 if not confirmar else "EJECUTADO — reversible desde Deshacer conciliados"),
    })


@bp.route("/auditar-sin-programa", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def auditar_sin_programa():
    """AUDITORÍA (solo lectura, TMT 2026-07-15): lista todas las conciliaciones
    ACTIVAS que NO tienen movimiento del programa atado (id_transaccion IS NULL).
    Son las que en la pantalla de matches muestran PROGRAMA = 0.00: se aceptó el
    lado banco sin cruzarlo contra un mov del programa.

    Separa por 'estado':
      - real_only_ok  → aceptación deliberada (banco sin equivalente en el
        programa; ej. comisiones, o importaciones cargadas como anticipo). OK.
      - matched       → dice 'cruzado' pero SIN contraparte del programa. Estas
        son las conciliaciones 'a medias' a revisar (ej. el grupo de la
        importación de −297.215,46 del 03/07).

    Agrupa por confirm_batch_id (cada grupo que la dueña armó de una) y ordena
    por magnitud del monto.
    """
    no_banco = int(request.args.get("no_banco") or _BANCO_PICHINCHA)
    rows = _db.fetch_all(
        """
        SELECT COALESCE(confirm_batch_id, '(sin batch)') AS batch,
               estado,
               COALESCE(metodo, '') AS metodo,
               COUNT(*) AS n_items,
               MIN(real_fecha)::TEXT AS desde,
               MAX(real_fecha)::TEXT AS hasta,
               ROUND(SUM(CASE WHEN UPPER(COALESCE(real_tipo,'C'))='C'
                              THEN real_monto ELSE -real_monto END), 2) AS suma_signada,
               MIN(real_concepto) AS ejemplo,
               MIN(creado_en)::TEXT AS creado,
               MIN(usuario) AS usuario
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s
           AND deshecho_en IS NULL
           AND id_transaccion IS NULL
         GROUP BY confirm_batch_id, estado, COALESCE(metodo, '')
         ORDER BY ABS(SUM(CASE WHEN UPPER(COALESCE(real_tipo,'C'))='C'
                               THEN real_monto ELSE -real_monto END)) DESC
        """,
        (no_banco,),
    ) or []
    def _clean(r):
        return {k: (float(v) if hasattr(v, "is_finite") else v) for k, v in r.items()}
    grupos = [_clean(r) for r in rows]

    # HUÉRFANOS: matches que SÍ tienen id_transaccion pero apuntan a un mov del
    # programa que YA NO EXISTE. Estos son los que 'se cruzaron contra algo' y
    # despues el mov del programa se borró (típicamente el sync dBase re-numera
    # transacciones_bancarias y el relink por tx_firma no lo recuperó). En la
    # pantalla de matches se ven con PROGRAMA=0.00 aunque antes cuadraban.
    huerfanos = _db.fetch_all(
        """
        SELECT COALESCE(m.confirm_batch_id, '(sin batch)') AS batch,
               m.estado,
               COALESCE(m.metodo, '') AS metodo,
               COUNT(*) AS n_items,
               MIN(m.real_fecha)::TEXT AS desde,
               MAX(m.real_fecha)::TEXT AS hasta,
               ROUND(SUM(CASE WHEN UPPER(COALESCE(m.real_tipo,'C'))='C'
                              THEN m.real_monto ELSE -m.real_monto END), 2) AS suma_signada,
               MIN(m.real_concepto) AS ejemplo,
               MIN(m.tx_firma) AS tx_firma_ej,
               MIN(m.creado_en)::TEXT AS creado,
               MIN(m.usuario) AS usuario
          FROM scintela.banco_conciliacion_match m
         WHERE m.no_banco = %s
           AND m.deshecho_en IS NULL
           AND m.id_transaccion IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1 FROM scintela.transacciones_bancarias t
                  WHERE t.id_transaccion = m.id_transaccion
           )
         GROUP BY m.confirm_batch_id, m.estado, COALESCE(m.metodo, '')
         ORDER BY ABS(SUM(CASE WHEN UPPER(COALESCE(m.real_tipo,'C'))='C'
                               THEN m.real_monto ELSE -m.real_monto END)) DESC
        """,
        (no_banco,),
    ) or []
    huerfanos = [_clean(r) for r in huerfanos]

    def _resumir(lst):
        res: dict = {}
        for r in lst:
            est = r.get("estado") or "?"
            d = res.setdefault(est, {"grupos": 0, "items": 0, "suma_signada": 0.0})
            d["grupos"] += 1
            d["items"] += int(r.get("n_items") or 0)
            d["suma_signada"] = round(d["suma_signada"] + float(r.get("suma_signada") or 0), 2)
        return res

    return jsonify({
        "no_banco": no_banco,
        "sin_programa": {
            "descripcion": "id_transaccion NULL — aceptados sin cruzar contra el programa",
            "total_grupos": len(grupos),
            "resumen_por_estado": _resumir(grupos),
            "grupos": grupos,
        },
        "huerfanos": {
            "descripcion": "cruzados contra un mov del programa que YA NO EXISTE (se borró por debajo, ej. sync dBase) — ESTOS son el 'algo raro'",
            "total_grupos": len(huerfanos),
            "resumen_por_estado": _resumir(huerfanos),
            "grupos": huerfanos,
        },
    })


@bp.route("/restaurar-contraparte", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def restaurar_contraparte():
    """REPARACIÓN (TMT 2026-07-15): vuelve a atar la contraparte del programa de
    los matches que el relink viejo dejó en NULL (o apuntando a un id muerto).

    El match conserva su `tx_firma` = identidad del mov del programa original
    (fecha|documento|importe|numref|concepto). Aunque la firma EXACTA ya no
    resuelva (cambió numref/concepto tras re-importar), la identidad
    fecha+documento+importe SÍ alcanza para reencontrar el mismo movimiento en
    transacciones_bancarias. Solo restaura cuando hay UN ÚNICO candidato exacto
    (no ambiguo). Dry-run por defecto; &confirm=SI aplica.

    Así la columna PROGRAMA vuelve a mostrar cuánto venía del programa y el DIFF
    vuelve a cuadrar, sin re-conciliar nada a mano.
    """
    no_banco = int(request.args.get("no_banco") or _BANCO_PICHINCHA)
    confirmar = (request.args.get("confirm") or "").strip().upper() == "SI"
    rotos = _db.fetch_all(
        """
        SELECT m.id, m.id_transaccion, m.tx_firma
          FROM scintela.banco_conciliacion_match m
         WHERE m.no_banco = %s AND m.deshecho_en IS NULL
           AND m.tx_firma IS NOT NULL AND m.tx_firma <> ''
           AND (m.id_transaccion IS NULL OR NOT EXISTS (
                 SELECT 1 FROM scintela.transacciones_bancarias t
                  WHERE t.id_transaccion = m.id_transaccion))
        """,
        (no_banco,),
    ) or []
    plan = []
    aplicados = 0
    ambiguos = 0
    sin_candidato = 0
    for r in rotos:
        firma = r.get("tx_firma") or ""
        parts = firma.split("|")
        if len(parts) < 3:
            continue
        f_fecha, f_doc, f_imp = parts[0].strip(), parts[1].strip(), parts[2].strip()
        # parts[3]=numreferencia (el campo MÁS volátil tras re-importar — NO lo
        # usamos), parts[4]=LEFT(concepto,40). Incluimos el concepto para NO
        # atar por error dos movimientos del mismo monto/fecha (ej. varios
        # cheques de $1.000 el mismo día) — el concepto los distingue.
        f_concepto = parts[4] if len(parts) >= 5 else ""
        try:
            imp = round(float(f_imp), 2)
        except (ValueError, TypeError):
            continue
        cands = _db.fetch_all(
            """
            SELECT id_transaccion
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
               AND fecha::TEXT = %s
               AND COALESCE(documento, '') = %s
               AND ROUND(importe, 2) = %s
               AND COALESCE(LEFT(concepto, 40), '') = %s
            """,
            (no_banco, f_fecha, f_doc, imp, f_concepto),
        ) or []
        info = {"match_id": r["id"], "firma": firma[:70],
                "id_actual": r.get("id_transaccion"), "candidatos": len(cands)}
        if len(cands) == 1:
            nid = cands[0]["id_transaccion"]
            info["nuevo_id_transaccion"] = nid
            plan.append(info)
            if confirmar:
                _db.execute(
                    "UPDATE scintela.banco_conciliacion_match "
                    "SET id_transaccion = %s WHERE id = %s AND deshecho_en IS NULL",
                    (nid, r["id"]),
                )
                aplicados += 1
        elif len(cands) == 0:
            sin_candidato += 1
        else:
            ambiguos += 1
            info["nota"] = "AMBIGUO (varios candidatos) — no se toca"
            plan.append(info)
    return jsonify({
        "no_banco": no_banco,
        "confirmado": confirmar,
        "rotos_con_firma": len(rotos),
        "restaurables_unicos": len([p for p in plan if p.get("nuevo_id_transaccion")]),
        "ambiguos": ambiguos,
        "sin_candidato": sin_candidato,
        "aplicados": aplicados,
        "plan": plan[:200],
        "nota": ("dry-run: agregá &confirm=SI para aplicar"
                 if not confirmar else "APLICADO — la columna PROGRAMA vuelve a mostrar el valor"),
    })


def _chequeo_cruces(no_banco: int) -> dict:
    """RED DE SEGURIDAD (TMT 2026-07-15): detecta grupos MAL ATADOS — movimientos
    del programa cuyos matches del banco NO suman su importe (más de 0.50). Caza
    los cruces "corridos" (banco atado al programa equivocado) apenas aparecen,
    sin importar de dónde vengan. Corre solo después de cada sync + a pedido."""
    import bank_helpers as _bh
    rows = _db.fetch_all(
        """
        SELECT tb.id_transaccion, tb.documento, tb.importe,
               COALESCE(tb.usuario_crea, '') AS uc, LEFT(tb.concepto, 40) AS concepto,
               COUNT(*) AS n_banco,
               SUM(CASE WHEN UPPER(COALESCE(m.real_tipo, 'C')) = 'C'
                        THEN m.real_monto ELSE -m.real_monto END) AS suma_banco
          FROM scintela.banco_conciliacion_match m
          JOIN scintela.transacciones_bancarias tb ON tb.id_transaccion = m.id_transaccion
         WHERE m.no_banco = %s AND m.deshecho_en IS NULL
           AND m.id_transaccion IS NOT NULL AND m.estado = 'matched'
           AND m.real_monto IS NOT NULL
         GROUP BY tb.id_transaccion, tb.documento, tb.importe, tb.usuario_crea, tb.concepto
        """, (no_banco,)) or []
    mal = []
    for r in rows:
        prog = float(_bh._signed_delta((r["documento"] or "").upper(), float(r["importe"] or 0), r["uc"]))
        suma = float(r["suma_banco"] or 0)
        diff = round(suma - prog, 2)
        if abs(diff) > 0.50:
            mal.append({"id_transaccion": r["id_transaccion"], "programa": round(prog, 2),
                        "suma_banco": round(suma, 2), "diff": diff,
                        "n_banco": int(r["n_banco"]), "concepto": r["concepto"]})
    mal.sort(key=lambda x: abs(x["diff"]), reverse=True)
    return {"no_banco": no_banco, "n_programas_revisados": len(rows),
            "n_mal_atados": len(mal), "suma_diff": round(sum(x["diff"] for x in mal), 2),
            "mal_atados": mal}


@bp.route("/chequeo-cruces", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def chequeo_cruces():
    """A pedido: lista los grupos mal atados (banco no suma su programa)."""
    no_banco = int(request.args.get("no_banco") or _BANCO_PICHINCHA)
    return jsonify(_chequeo_cruces(no_banco))


@bp.route("/reatar-grupo", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def reatar_grupo():
    """RE-ATA un grupo corrido (TMT 2026-07-15). Re-asigna cada match del grupo a
    su movimiento del programa del MISMO monto usando asignar_banco_a_programa
    (1:1 + N:1), pero SOLO contra los movimientos del programa REALES que ya están
    en el grupo (no sale a buscar afuera → bounded y seguro; mismo batch → sin
    conflicto single-claim; los programas siguen conciliados). Los matches que no
    consiguen un programa real de su monto quedan con id_transaccion NULL (muestran
    el valor de la firma). Dry-run por defecto; &confirm=SI aplica. Devuelve los
    enlaces viejos para revertir."""
    import bank_helpers as _bh
    from modules.conciliacion.banco_v2_view import asignar_banco_a_programa
    no_banco = int(request.args.get("no_banco") or _BANCO_PICHINCHA)
    batch = (request.args.get("batch") or "").strip()
    doc = (request.args.get("doc") or "").strip()
    confirmar = (request.args.get("confirm") or "").strip().upper() == "SI"
    if not batch and doc:
        r = _db.fetch_one(
            "SELECT confirm_batch_id FROM scintela.banco_conciliacion_match "
            "WHERE no_banco=%s AND deshecho_en IS NULL AND real_documento=%s "
            "AND confirm_batch_id IS NOT NULL ORDER BY id DESC LIMIT 1", (no_banco, doc))
        batch = (r or {}).get("confirm_batch_id") or ""
    if not batch:
        return jsonify({"error": "no encontré batch (pasá ?batch= o ?doc=)"}), 400
    rows = _db.fetch_all(
        "SELECT id, real_documento, real_monto, real_tipo, real_concepto, id_transaccion "
        "FROM scintela.banco_conciliacion_match "
        "WHERE no_banco=%s AND deshecho_en IS NULL AND confirm_batch_id=%s ORDER BY id",
        (no_banco, batch)) or []
    ids = [r["id_transaccion"] for r in rows if r.get("id_transaccion")]
    tx_map = {}
    if ids:
        for t in _db.fetch_all(
            "SELECT id_transaccion, importe, documento, COALESCE(usuario_crea,'') AS uc "
            "FROM scintela.transacciones_bancarias WHERE id_transaccion = ANY(%s)", (ids,)) or []:
            tx_map[int(t["id_transaccion"])] = t

    def _bsign(tipo, monto):
        m = float(monto or 0)
        return round(m if (tipo or "").upper() == "C" else -m, 2)

    # Pool de programa = movimientos REALES ya en el grupo (id_transaccion vivo).
    prog_signed = {}
    for idtx, t in tx_map.items():
        prog_signed[idtx] = round(float(_bh._signed_delta((t["documento"] or "").upper(), float(t["importe"] or 0), t["uc"])), 2)

    banco_firmados = []
    binfo = {}
    for r in rows:
        bkey = int(r["id"])
        bsig = _bsign(r["real_tipo"], r["real_monto"])
        banco_firmados.append((bkey, bsig))
        binfo[bkey] = {"monto": bsig, "doc": r["real_documento"],
                       "concepto": str(r.get("real_concepto") or "")[:38],
                       "id_transaccion_viejo": r.get("id_transaccion")}

    prog_firmados = [(k, v) for k, v in prog_signed.items()]
    asign, sobrantes = asignar_banco_a_programa(banco_firmados, prog_firmados, tol=0.50)

    # Nuevo id_transaccion por match.
    nuevo = {}
    for pid, bkeys in asign.items():
        for bk in bkeys:
            nuevo[bk] = pid
    cambios = []
    for bk, info in binfo.items():
        viejo = info["id_transaccion_viejo"]
        nid = nuevo.get(bk)  # None → queda sin programa (firma)
        if (viejo or None) != (nid or None):
            cambios.append({"match_id": bk, "monto": info["monto"], "doc": info["doc"],
                            "concepto": info["concepto"],
                            "id_viejo": viejo, "id_nuevo": nid,
                            "prog_nuevo_monto": (prog_signed.get(nid) if nid else None)})

    aplicados = 0
    if confirmar and cambios:
        for c in cambios:
            _db.execute(
                "UPDATE scintela.banco_conciliacion_match SET id_transaccion = %s "
                "WHERE id = %s AND deshecho_en IS NULL",
                (c["id_nuevo"], c["match_id"]),
            )
            aplicados += 1

    return jsonify({
        "batch": batch, "n_matches": len(rows), "n_programa_real": len(prog_signed),
        "confirmado": confirmar, "n_cambios": len(cambios), "aplicados": aplicados,
        "sobrantes_sin_programa_real": len(sobrantes),
        "cambios": cambios,
        "nota": ("dry-run: agregá &confirm=SI para aplicar (guardá este JSON para revertir con id_viejo)"
                 if not confirmar else "APLICADO — reversible con los id_viejo de este JSON"),
    })


@bp.route("/rematch-grupo", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def rematch_grupo():
    """RE-EMPAREJADOR (dry-run, TMT 2026-07-15): dado un grupo (?doc= o ?batch=),
    re-arma la combinación CORRECTA banco↔programa por monto usando la MISMA
    lógica del sistema (asignar_banco_a_programa: 1:1 exacto + N:1 por suma, ej.
    1.500+1.500=3.000). Muestra qué ataría con qué y qué queda realmente suelto.
    SOLO LECTURA — no toca nada. Sirve para ver el desorden real de un grupo
    corrido antes de decidir."""
    import bank_helpers as _bh
    from modules.conciliacion.banco_v2_view import asignar_banco_a_programa
    no_banco = int(request.args.get("no_banco") or _BANCO_PICHINCHA)
    batch = (request.args.get("batch") or "").strip()
    doc = (request.args.get("doc") or "").strip()
    if not batch and doc:
        r = _db.fetch_one(
            "SELECT confirm_batch_id FROM scintela.banco_conciliacion_match "
            "WHERE no_banco=%s AND deshecho_en IS NULL AND real_documento=%s "
            "AND confirm_batch_id IS NOT NULL ORDER BY id DESC LIMIT 1", (no_banco, doc))
        batch = (r or {}).get("confirm_batch_id") or ""
    if not batch:
        return jsonify({"error": "no encontré batch (pasá ?batch= o ?doc=)"}), 400
    rows = _db.fetch_all(
        "SELECT id, real_documento, real_monto, real_tipo, real_concepto, "
        "id_transaccion, tx_firma FROM scintela.banco_conciliacion_match "
        "WHERE no_banco=%s AND deshecho_en IS NULL AND confirm_batch_id=%s ORDER BY id",
        (no_banco, batch)) or []
    ids = [r["id_transaccion"] for r in rows if r.get("id_transaccion")]
    tx_map = {}
    if ids:
        for t in _db.fetch_all(
            "SELECT id_transaccion, importe, documento, COALESCE(usuario_crea,'') AS uc "
            "FROM scintela.transacciones_bancarias WHERE id_transaccion = ANY(%s)", (ids,)) or []:
            tx_map[int(t["id_transaccion"])] = t

    def _bsign(tipo, monto):
        m = float(monto or 0)
        return m if (tipo or "").upper() == "C" else -m

    def _fparse(firma):
        p = (firma or "").split("|")
        if len(p) < 3:
            return None
        try:
            return float(_bh._signed_delta((p[1] or "").upper(), float(p[2]), ""))
        except ValueError:
            return None

    banco_firmados = []
    banco_info = {}
    prog_signed = {}   # key -> signed
    prog_info = {}
    for r in rows:
        bkey = f"b{r['id']}"
        bsig = round(_bsign(r["real_tipo"], r["real_monto"]), 2)
        banco_firmados.append((bkey, bsig))
        banco_info[bkey] = {"monto": bsig, "doc": r["real_documento"],
                            "concepto": str(r.get("real_concepto") or "")[:40]}
        idtx = r.get("id_transaccion")
        if idtx is not None and int(idtx) in tx_map:
            t = tx_map[int(idtx)]
            psig = round(float(_bh._signed_delta((t["documento"] or "").upper(), float(t["importe"] or 0), t["uc"])), 2)
            pkey = f"live:{int(idtx)}"
        else:
            ps = _fparse(r.get("tx_firma"))
            if ps is None:
                continue
            psig = round(ps, 2)
            pkey = f"firma:{r.get('tx_firma')}"
        if pkey not in prog_signed:
            prog_signed[pkey] = psig
            prog_info[pkey] = {"monto": psig}

    prog_firmados = [(k, v) for k, v in prog_signed.items()]
    asign, sobrantes = asignar_banco_a_programa(banco_firmados, prog_firmados, tol=0.50)

    pares = []
    banco_usados = set()
    for pkey, bkeys in asign.items():
        suma = round(sum(banco_info[b]["monto"] for b in bkeys), 2)
        banco_usados.update(bkeys)
        pares.append({
            "programa": prog_info[pkey]["monto"],
            "banco_items": [banco_info[b] for b in bkeys],
            "suma_banco": suma,
            "cuadra": abs(suma - prog_info[pkey]["monto"]) <= 0.50,
        })
    prog_sin_banco = [{"monto": prog_info[k]["monto"]} for k in prog_signed if k not in asign]
    banco_sobrantes = [banco_info[b] for b in sobrantes]

    return jsonify({
        "batch": batch,
        "n_banco": len(banco_firmados), "n_programa": len(prog_firmados),
        "n_pares_ok": sum(1 for p in pares if p["cuadra"]),
        "n_pares_desiguales": sum(1 for p in pares if not p["cuadra"]),
        "banco_sin_par": banco_sobrantes,
        "programa_sin_par": prog_sin_banco,
        "suma_banco_sobrante": round(sum(x["monto"] for x in banco_sobrantes), 2),
        "suma_programa_sobrante": round(sum(x["monto"] for x in prog_sin_banco), 2),
        "pares": pares,
    })


@bp.route("/diagnosticar-grupo", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def diagnosticar_grupo():
    """DEEP DIVE de un grupo (confirm_batch_id). Dado ?doc= (un documento de
    banco del grupo) o ?batch=, dumpea TODOS los items con: monto banco firmado,
    si el mov PC vive, su importe firmado, la firma, y la diferencia por item.
    Calcula total banco vs total programa con la MISMA lógica de la pantalla
    (live dedup por id_transaccion + roto dedup por tx_firma) y desglosa de dónde
    sale la diferencia. Solo lectura."""
    import bank_helpers as _bh
    no_banco = int(request.args.get("no_banco") or _BANCO_PICHINCHA)
    batch = (request.args.get("batch") or "").strip()
    doc = (request.args.get("doc") or "").strip()
    if not batch and doc:
        r = _db.fetch_one(
            "SELECT confirm_batch_id FROM scintela.banco_conciliacion_match "
            "WHERE no_banco=%s AND deshecho_en IS NULL AND real_documento=%s "
            "AND confirm_batch_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (no_banco, doc))
        batch = (r or {}).get("confirm_batch_id") or ""
    if not batch:
        return jsonify({"error": "no encontré batch (pasá ?batch= o ?doc=)"}), 400
    rows = _db.fetch_all(
        "SELECT id, real_fecha::TEXT AS real_fecha, real_documento, real_monto, real_tipo, "
        "real_concepto, id_transaccion, tx_firma, estado, COALESCE(metodo,'') AS metodo "
        "FROM scintela.banco_conciliacion_match "
        "WHERE no_banco=%s AND deshecho_en IS NULL AND confirm_batch_id=%s ORDER BY id",
        (no_banco, batch)) or []
    ids = [r["id_transaccion"] for r in rows if r.get("id_transaccion")]
    tx_map = {}
    if ids:
        for t in _db.fetch_all(
            "SELECT id_transaccion, importe, documento, COALESCE(usuario_crea,'') AS uc "
            "FROM scintela.transacciones_bancarias WHERE id_transaccion = ANY(%s)", (ids,)) or []:
            tx_map[int(t["id_transaccion"])] = t

    def _bsign(tipo, monto):
        m = float(monto or 0)
        return m if (tipo or "").upper() == "C" else -m

    def _fparse(firma):
        p = (firma or "").split("|")
        if len(p) < 3:
            return None, None
        try:
            return (p[1] or "").upper(), float(p[2])
        except ValueError:
            return None, None

    items = []
    suma_banco = 0.0
    prog_total = 0.0
    seen_id = set()
    seen_firma = set()
    for r in rows:
        b = _bsign(r["real_tipo"], r["real_monto"])
        suma_banco += b
        idtx = r.get("id_transaccion")
        vive = idtx is not None and int(idtx) in tx_map
        prog_live = None
        if vive:
            t = tx_map[int(idtx)]
            prog_live = float(_bh._signed_delta((t["documento"] or "").upper(), float(t["importe"] or 0), t["uc"]))
        fd, fi = _fparse(r.get("tx_firma"))
        prog_firma = float(_bh._signed_delta(fd, fi, "")) if fd is not None else None
        aporte = 0.0
        fuente = "—(ya contado o sin prog)"
        if vive:
            if int(idtx) not in seen_id:
                seen_id.add(int(idtx))
                aporte = prog_live
                fuente = "live"
        elif prog_firma is not None:
            if r.get("tx_firma") not in seen_firma:
                seen_firma.add(r.get("tx_firma"))
                aporte = prog_firma
                fuente = "firma"
        prog_total += aporte
        _prog_item = prog_live if vive else prog_firma
        items.append({
            "doc": r["real_documento"], "fecha": r["real_fecha"], "tipo": r["real_tipo"],
            "banco": round(b, 2), "id_tx": idtx, "vive": vive,
            "prog_live": (round(prog_live, 2) if prog_live is not None else None),
            "prog_firma": (round(prog_firma, 2) if prog_firma is not None else None),
            "aporte_prog": round(aporte, 2), "fuente_prog": fuente,
            "diff_item": round(b - (_prog_item or 0), 2),
            "concepto": str(r.get("real_concepto") or "")[:50],
        })
    items_con_diff = [it for it in items if abs(it["diff_item"]) > 0.01]
    return jsonify({
        "batch": batch, "n_items": len(rows),
        "suma_banco": round(suma_banco, 2),
        "prog_total_pantalla": round(prog_total, 2),
        "diferencia": round(suma_banco - prog_total, 2),
        "n_movs_pc_vivos": len(seen_id), "n_por_firma": len(seen_firma),
        "items_con_diferencia": items_con_diff,
        "items": items,
    })


@bp.route("/quitar-del-extracto", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def quitar_del_extracto():
    """REPARACIÓN (TMT 2026-07-15): saca del extracto de una sesión movimientos
    DUPLICADOS y deshace sus matches. Caso: la importación INV30405 ya la
    concilió Alex por el backlog (grupo −297.215) y se volvió a contar por el
    extracto (débitos −297.929,09 + −14.896,45) → doble conteo. Esto los quita
    del payload (NO vuelven a pendientes) y soft-deshace cualquier match de esos
    documentos. Dry-run por defecto; &confirm=SI aplica. Devuelve las filas
    removidas para poder reponerlas si hiciera falta.
    """
    import json as _json
    from modules.conciliacion import sesion as _s
    sid = int(request.args.get("sesion_id") or 0)
    docs = [d.strip() for d in (request.args.get("docs") or "").split(",") if d.strip()]
    ses = _s.sesion_por_id(sid) if sid else None
    if not ses:
        return jsonify({"error": "sesion no encontrada", "sesion_id": sid}), 404
    if not docs:
        return jsonify({"error": "faltan docs (?docs=...)"}), 400
    no_banco = ses.get("no_banco") or _BANCO_PICHINCHA
    confirmar = (request.args.get("confirm") or "").strip().upper() == "SI"

    payload = ses.get("extracto_payload") or []
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = []
    docset = set(docs)
    removidas = [r for r in payload if str(r.get("documento") or "").strip() in docset]
    quedan = [r for r in payload if str(r.get("documento") or "").strip() not in docset]

    matches = _db.fetch_all(
        "SELECT id, real_documento, real_monto, estado, COALESCE(metodo,'') AS metodo "
        "FROM scintela.banco_conciliacion_match "
        "WHERE no_banco = %s AND deshecho_en IS NULL AND real_documento = ANY(%s)",
        (no_banco, docs),
    ) or []

    out = {
        "sesion_id": sid, "no_banco": no_banco, "confirmado": confirmar, "docs": docs,
        "payload_antes": len(payload), "payload_despues": len(quedan),
        "filas_removidas": [{"documento": r.get("documento"), "fecha": r.get("fecha"),
                             "monto": r.get("monto"), "tipo": r.get("tipo"),
                             "concepto": str(r.get("concepto") or "")[:60]} for r in removidas],
        "matches_a_deshacer": [{"id": m["id"], "doc": m["real_documento"],
                                "monto": str(m["real_monto"]), "estado": m["estado"],
                                "metodo": m["metodo"]} for m in matches],
    }
    if confirmar:
        _db.execute(
            "UPDATE scintela.banco_conciliacion_sesion SET extracto_payload = %s::jsonb WHERE id = %s",
            (_json.dumps(quedan), sid),
        )
        n = 0
        if matches:
            n = _db.execute(
                "UPDATE scintela.banco_conciliacion_match "
                "SET deshecho_en = CURRENT_TIMESTAMP, deshecho_por = %s "
                "WHERE no_banco = %s AND deshecho_en IS NULL AND real_documento = ANY(%s)",
                ("quitar-duplicado", no_banco, docs),
            ) or 0
        out["matches_deshechos"] = n
        out["nota"] = "APLICADO — removidos del extracto y matches deshechos"
    else:
        out["nota"] = "dry-run: agregá &confirm=SI para aplicar"
    return jsonify(out)


@bp.route("/e2e-cleanup", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def e2e_cleanup():
    """Limpia state generado por e2e-borrar-sesion fallido.

    Borra matches con usuario='e2e-test' y sesiones con usuario='e2e-test',
    revierte histos conciliados por e2e-test.
    """
    out = {"ok": True}
    with _db.tx() as conn:
        # 1. Reset histos
        n_h = _db.execute(
            """
            UPDATE scintela.banco_historicos_pendientes
               SET conciliado_en = NULL, conciliado_por = NULL, conciliado_match_id = NULL
             WHERE conciliado_por = 'e2e-test'
            """,
            (), conn=conn,
        ) or 0
        out["histos_reset"] = n_h
        # 2. Borrar matches
        n_m = _db.execute(
            "DELETE FROM scintela.banco_conciliacion_match WHERE usuario = 'e2e-test'",
            (), conn=conn,
        ) or 0
        out["matches_borrados"] = n_m
        # 3. Borrar sesiones
        n_s = _db.execute(
            "DELETE FROM scintela.banco_conciliacion_sesion WHERE usuario = 'e2e-test'",
            (), conn=conn,
        ) or 0
        out["sesiones_borradas"] = n_s
    return jsonify(out)


@bp.route("/e2e-borrar-sesion-v2", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def e2e_borrar_sesion_v2():
    """E2E v2: lifecycle real usando el módulo borrar_sesion directamente.

    En lugar de invocar el HTTP endpoint vía test_client (CSRF/auth issues),
    importamos y llamamos el módulo conciliación con un request mock.
    """
    from flask import current_app
    out = {"ok": True, "steps": []}

    # ─── PRE snapshot ─────────────────────────────────────────────
    pre_libros = float((_db.fetch_one(
        "SELECT saldo FROM scintela.transacciones_bancarias WHERE no_banco=%s ORDER BY fecha DESC, id_transaccion DESC LIMIT 1",
        (_BANCO_PICHINCHA,)) or {}).get("saldo") or 0)
    pre_matches = (_db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match WHERE no_banco=%s AND deshecho_en IS NULL",
        (_BANCO_PICHINCHA,)) or {}).get("n", 0)
    pre_histos = (_db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_historicos_pendientes WHERE no_banco=%s AND conciliado_en IS NULL",
        (_BANCO_PICHINCHA,)) or {}).get("n", 0)
    out["pre"] = {"libros": pre_libros, "matches_activos": pre_matches, "histos_pendientes": pre_histos}

    # ─── STEP 1: crear sesion ────────────────────────────────────
    import uuid as _uuid
    sesion_id = None
    try:
        row = _db.execute_returning(
            """
            INSERT INTO scintela.banco_conciliacion_sesion
                (no_banco, usuario, extracto_hash, extracto_nombre, extracto_payload, abierta_en)
            VALUES (%s, 'e2e-test', NULL, 'e2e-test.xlsx', '[]'::jsonb, NOW())
            RETURNING id
            """,
            (_BANCO_PICHINCHA,),
        )
        sesion_id = row.get("id") if row else None
        out["steps"].append({"step": 1, "sesion_id": sesion_id})
    except Exception as e:
        out["steps"].append({"step": 1, "error": str(e)[:200]})
        out["ok"] = False
        return jsonify(out)

    # ─── STEP 2: crear match dummy ───────────────────────────────
    hist = _db.fetch_one(
        "SELECT id, fecha, documento, monto, tipo FROM scintela.banco_historicos_pendientes WHERE no_banco=%s AND conciliado_en IS NULL ORDER BY id LIMIT 1",
        (_BANCO_PICHINCHA,))
    match_id = None
    if hist:
        try:
            batch_id = _uuid.uuid4().hex
            mrow = _db.execute_returning(
                """
                INSERT INTO scintela.banco_conciliacion_match
                    (no_banco, estado, metodo, real_fecha, real_documento, real_monto, real_tipo,
                     confirm_batch_id, usuario, creado_en)
                VALUES (%s, 'matched', 'e2e-test', %s, %s, %s, %s, %s, 'e2e-test', NOW())
                RETURNING id
                """,
                (_BANCO_PICHINCHA, hist["fecha"], hist["documento"],
                 hist["monto"], hist["tipo"], batch_id),
            )
            match_id = mrow.get("id") if mrow else None
            _db.execute(
                "UPDATE scintela.banco_historicos_pendientes SET conciliado_en=NOW(), conciliado_por='e2e-test', conciliado_match_id=%s WHERE id=%s",
                (match_id, hist["id"]),
            )
            out["steps"].append({"step": 2, "match_id": match_id, "hist_id": hist["id"]})
        except Exception as e:
            out["steps"].append({"step": 2, "error": str(e)[:200]})

    # ─── STEP 3: llamar banco_borrar_sesion DIRECTO via test_request_context ──
    # Approach: empujamos un request context con form data + session login
    # heredada del request actual, y llamamos la función view directamente.
    # Esto evita loopback HTTP (que en Windows/Waitress puede dar 10061) y
    # también las gymnastics de test_client con cookies/CSRF.
    try:
        from flask import current_app
        from flask import session as _flask_session
        # Snapshot del session actual (que tiene login válido)
        outer_session = dict(_flask_session)
        with current_app.test_request_context(
            "/conciliacion/banco-v2/borrar-sesion",
            method="POST",
            data={"sesion_id": str(sesion_id)},
        ):
            # Copiar la sesión Flask del request externo (incluye login)
            for k, v in outer_session.items():
                _flask_session[k] = v
            # Llamar la función view directo (saltea decoradores ya validados arriba)
            from modules.conciliacion.banco_v2_view import banco_borrar_sesion
            # banco_borrar_sesion tiene @requiere_login + @requiere_permiso;
            # como copiamos session, debería pasar. El response es un Redirect.
            response = banco_borrar_sesion()
            # response puede ser Response object o tuple
            if hasattr(response, "status_code"):
                status = response.status_code
                loc = (response.headers.get("Location", "") or "")[:120]
            else:
                status = "?"
                loc = repr(response)[:120]
        out["steps"].append({"step": 3, "status": status, "loc": loc})
    except Exception as e:
        import traceback as _tb
        out["steps"].append({"step": 3, "error": str(e)[:250], "tb": _tb.format_exc()[-500:]})

    # ─── POST snapshot + validation ──────────────────────────────
    post_libros = float((_db.fetch_one(
        "SELECT saldo FROM scintela.transacciones_bancarias WHERE no_banco=%s ORDER BY fecha DESC, id_transaccion DESC LIMIT 1",
        (_BANCO_PICHINCHA,)) or {}).get("saldo") or 0)
    post_matches = (_db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match WHERE no_banco=%s AND deshecho_en IS NULL",
        (_BANCO_PICHINCHA,)) or {}).get("n", 0)
    post_histos = (_db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_historicos_pendientes WHERE no_banco=%s AND conciliado_en IS NULL",
        (_BANCO_PICHINCHA,)) or {}).get("n", 0)
    sesion_existe = bool((_db.fetch_one(
        "SELECT 1 AS x FROM scintela.banco_conciliacion_sesion WHERE id=%s",
        (sesion_id,)) or {}).get("x"))
    out["post"] = {"libros": post_libros, "matches_activos": post_matches,
                   "histos_pendientes": post_histos, "sesion_existe": sesion_existe}
    out["diffs"] = {
        "libros_delta": round(post_libros - pre_libros, 2),
        "matches_delta": post_matches - pre_matches,
        "histos_delta": post_histos - pre_histos,
    }
    val = []
    val.append("✓ libros unchanged" if abs(out["diffs"]["libros_delta"]) < 0.5 else f"❌ libros drift {out['diffs']['libros_delta']:+.2f}")
    val.append("✓ matches count restored" if out["diffs"]["matches_delta"] == 0 else f"❌ matches drift {out['diffs']['matches_delta']:+d}")
    val.append("✓ histos count restored" if out["diffs"]["histos_delta"] == 0 else f"❌ histos drift {out['diffs']['histos_delta']:+d}")
    val.append("✓ sesion deleted" if not sesion_existe else "❌ sesion still exists")
    out["validations"] = val
    out["ok"] = all("✓" in v for v in val)
    return jsonify(out)


@bp.route("/e2e-borrar-sesion", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def e2e_borrar_sesion():
    """E2E test del lifecycle borrar-sesion. Inserta una sesión + un match
    dummy, llama borrar_sesion, valida que TODO quede limpio.

    Returns: dict con snapshot pre/post + diff. ok=True si todo limpio.
    """
    import uuid

    out = {"ok": True, "steps": []}

    # ─── PRE: snapshot ────────────────────────────────────────────
    pre_libros_row = _db.fetch_one(
        """
        SELECT saldo FROM scintela.transacciones_bancarias
         WHERE no_banco = %s ORDER BY fecha DESC, id_transaccion DESC LIMIT 1
        """,
        (_BANCO_PICHINCHA,),
    ) or {}
    pre_libros = float(pre_libros_row.get("saldo") or 0)
    pre_matches = (_db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match WHERE no_banco = %s AND deshecho_en IS NULL",
        (_BANCO_PICHINCHA,),
    ) or {}).get("n", 0)
    pre_histos_pendientes = (_db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_historicos_pendientes WHERE no_banco = %s AND conciliado_en IS NULL",
        (_BANCO_PICHINCHA,),
    ) or {}).get("n", 0)
    out["pre"] = {"libros": pre_libros, "matches_activos": pre_matches,
                  "histos_pendientes": pre_histos_pendientes}

    # ─── STEP 1: crear sesion test ───────────────────────────────
    try:
        with _db.tx() as conn:
            sesion_row = _db.execute_returning(
                """
                INSERT INTO scintela.banco_conciliacion_sesion
                    (no_banco, usuario, extracto_hash, extracto_nombre, extracto_payload, abierta_en)
                VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                RETURNING id
                """,
                (_BANCO_PICHINCHA, "e2e-test", None, "e2e-test.xlsx", "[]"),
                conn=conn,
            )
            sesion_id = sesion_row.get("id") if sesion_row else None
        out["steps"].append({"step": 1, "msg": f"Sesion creada id={sesion_id}"})
    except Exception as e:
        out["ok"] = False
        out["steps"].append({"step": 1, "error": str(e)[:200]})
        return jsonify(out)

    # ─── STEP 2: crear 1 match dummy linkeado a esta sesion ─────
    # Tomamos cualquier hist disponible para asociar.
    hist_row = _db.fetch_one(
        """
        SELECT id, fecha, documento, monto, tipo FROM scintela.banco_historicos_pendientes
         WHERE no_banco = %s AND conciliado_en IS NULL
         ORDER BY id ASC LIMIT 1
        """,
        (_BANCO_PICHINCHA,),
    )
    match_id_creado = None
    if hist_row:
        try:
            batch_id = uuid.uuid4().hex
            row = _db.execute_returning(
                """
                INSERT INTO scintela.banco_conciliacion_match (
                    no_banco, estado, metodo,
                    real_fecha, real_documento, real_monto, real_tipo,
                    confirm_batch_id, usuario, creado_en
                )
                VALUES (%s, 'matched', 'e2e-test', %s, %s, %s, %s, %s, 'e2e-test', NOW())
                RETURNING id
                """,
                (
                    _BANCO_PICHINCHA, hist_row["fecha"], hist_row["documento"],
                    hist_row["monto"], hist_row["tipo"], batch_id,
                ),
            )
            match_id_creado = row.get("id") if row else None
            # Marcar el hist conciliado
            _db.execute(
                """
                UPDATE scintela.banco_historicos_pendientes
                   SET conciliado_en = NOW(),
                       conciliado_por = 'e2e-test',
                       conciliado_match_id = %s
                 WHERE id = %s
                """,
                (match_id_creado, hist_row["id"]),
            )
            out["steps"].append({"step": 2,
                "msg": f"Match dummy creado id={match_id_creado} link hist={hist_row['id']}"})
        except Exception as e:
            out["ok"] = False
            out["steps"].append({"step": 2, "error": str(e)[:200]})

    # ─── STEP 3: llamar borrar-sesion programaticamente ─────────
    # Vamos a importar el endpoint y llamarlo via test_client de Flask.
    try:
        from flask import current_app
        client = current_app.test_client()
        # Necesitamos auth cookie — la heredamos del request actual.
        from flask import request as _req
        cookies = _req.cookies
        for name, value in cookies.items():
            client.set_cookie(domain="programa.intela.com.ec", key=name, value=value)
        # CSRF token del session
        from flask_wtf.csrf import generate_csrf
        csrf = generate_csrf()
        resp = client.post(
            "/conciliacion/banco-v2/borrar-sesion",
            data={"sesion_id": str(sesion_id), "csrf_token": csrf},
            follow_redirects=False,
        )
        out["steps"].append({"step": 3, "status": resp.status_code,
            "redirect": resp.headers.get("Location", "")[:80]})
    except Exception as e:
        out["ok"] = False
        out["steps"].append({"step": 3, "error": str(e)[:200]})

    # ─── POST: snapshot ──────────────────────────────────────────
    post_libros_row = _db.fetch_one(
        """
        SELECT saldo FROM scintela.transacciones_bancarias
         WHERE no_banco = %s ORDER BY fecha DESC, id_transaccion DESC LIMIT 1
        """,
        (_BANCO_PICHINCHA,),
    ) or {}
    post_libros = float(post_libros_row.get("saldo") or 0)
    post_matches = (_db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match WHERE no_banco = %s AND deshecho_en IS NULL",
        (_BANCO_PICHINCHA,),
    ) or {}).get("n", 0)
    post_histos_pendientes = (_db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_historicos_pendientes WHERE no_banco = %s AND conciliado_en IS NULL",
        (_BANCO_PICHINCHA,),
    ) or {}).get("n", 0)
    sesion_existe = (_db.fetch_one(
        "SELECT 1 AS x FROM scintela.banco_conciliacion_sesion WHERE id = %s",
        (sesion_id,),
    ) or {}).get("x")
    out["post"] = {"libros": post_libros, "matches_activos": post_matches,
                   "histos_pendientes": post_histos_pendientes,
                   "sesion_existe": bool(sesion_existe)}
    out["diffs"] = {
        "libros_delta": round(post_libros - pre_libros, 2),
        "matches_delta": post_matches - pre_matches,
        "histos_delta": post_histos_pendientes - pre_histos_pendientes,
    }
    # Validación final
    validations = []
    if abs(out["diffs"]["libros_delta"]) > 0.5:
        validations.append(f"❌ libros drift {out['diffs']['libros_delta']:+.2f}")
    else:
        validations.append("✓ libros unchanged")
    if out["diffs"]["matches_delta"] != 0:
        validations.append(f"❌ matches drift {out['diffs']['matches_delta']:+d}")
    else:
        validations.append("✓ matches count restored")
    if out["diffs"]["histos_delta"] != 0:
        validations.append(f"❌ histos drift {out['diffs']['histos_delta']:+d}")
    else:
        validations.append("✓ histos count restored")
    if out["post"]["sesion_existe"]:
        validations.append("❌ sesion still exists")
    else:
        validations.append("✓ sesion deleted")
    out["validations"] = validations
    out["ok"] = all("✓" in v for v in validations)
    return jsonify(out)


@bp.route("/verificar-ids-vivos", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def verificar_ids_vivos():
    """Toma listas de bancsis_ids y hist_ids del payload y verifica que sigan vivos."""
    body = request.get_json(silent=True) or {}
    bancsis_ids = [int(x) for x in (body.get("bancsis_ids") or "").split(",") if x.strip().isdigit()]
    hist_ids = [int(x) for x in (body.get("hist_ids") or "").split(",") if x.strip().isdigit()]
    out = {"ok": True}
    out["bancsis_request"] = len(bancsis_ids)
    out["hist_request"] = len(hist_ids)
    if bancsis_ids:
        rows = _db.fetch_all(
            """
            SELECT id_transaccion, fecha, documento, importe,
                   COALESCE(stat,'') AS stat
              FROM scintela.transacciones_bancarias
             WHERE id_transaccion = ANY(%s) AND no_banco = %s
            """,
            (bancsis_ids, _BANCO_PICHINCHA),
        ) or []
        out["bancsis_vivos"] = [
            {**r, "fecha": str(r["fecha"]), "importe": float(r["importe"] or 0)}
            for r in rows
        ]
        out["bancsis_perdidos"] = sorted(set(bancsis_ids) - {r["id_transaccion"] for r in rows})
    if hist_ids:
        rows_h = _db.fetch_all(
            """
            SELECT id, no_banco, fecha, documento, monto, tipo,
                   conciliado_en, conciliado_por
              FROM scintela.banco_historicos_pendientes
             WHERE id = ANY(%s)
            """,
            (hist_ids,),
        ) or []
        out["hist_vivos"] = [
            {**r, "fecha": str(r["fecha"]),
             "monto": float(r["monto"] or 0),
             "conciliado_en": str(r.get("conciliado_en") or "")}
            for r in rows_h
        ]
        out["hist_perdidos"] = sorted(set(hist_ids) - {r["id"] for r in rows_h})
        out["hist_disponibles_para_match"] = [r["id"] for r in rows_h if not r.get("conciliado_en")]
        out["hist_ya_conciliados"] = [r["id"] for r in rows_h if r.get("conciliado_en")]
    return jsonify(out)


@bp.route("/estado-banco-completo", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def estado_banco_completo():
    """Estado completo del banco para diagnosticar drift de libros.

    Devuelve:
      - últimas 30 txs Pichincha con saldos
      - todas las sesiones recientes
      - todos los matches activos y deshechos recientes
      - txs PC-only (no DBF) en el último mes
    """
    out = {"ok": True}
    # 1. Top 30 txs (últimas por fecha + id)
    out["top_txs"] = _db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, importe, saldo, concepto,
               COALESCE(usuario_crea,'') AS usuario_crea
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
         ORDER BY fecha DESC, id_transaccion DESC
         LIMIT 30
        """,
        (_BANCO_PICHINCHA,),
    ) or []
    # Convertir Decimals/dates a primitivos
    out["top_txs"] = [
        {**r, "fecha": str(r["fecha"]), "importe": float(r["importe"] or 0),
         "saldo": float(r["saldo"] or 0)}
        for r in out["top_txs"]
    ]
    # 2. Sesiones (todas las recientes)
    out["sesiones"] = _db.fetch_all(
        """
        SELECT id, no_banco, abierta_en, cerrada_en, usuario, matches_hechos,
               extracto_nombre
          FROM scintela.banco_conciliacion_sesion
         WHERE no_banco = %s
         ORDER BY abierta_en DESC
         LIMIT 20
        """,
        (_BANCO_PICHINCHA,),
    ) or []
    out["sesiones"] = [
        {**r, "abierta_en": str(r["abierta_en"]),
         "cerrada_en": str(r.get("cerrada_en") or "")}
        for r in out["sesiones"]
    ]
    # 3. Matches activos
    out["matches_activos_count"] = _db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s AND deshecho_en IS NULL
        """,
        (_BANCO_PICHINCHA,),
    ).get("n", 0)
    # 4. Matches deshechos en últimos 30 días
    out["matches_deshechos_recientes"] = _db.fetch_all(
        """
        SELECT id, real_fecha, real_documento, real_monto, real_tipo,
               id_transaccion, confirm_batch_id, creado_en, deshecho_en,
               deshecho_por
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s
           AND deshecho_en IS NOT NULL
           AND deshecho_en >= NOW() - INTERVAL '30 days'
         ORDER BY deshecho_en DESC
         LIMIT 100
        """,
        (_BANCO_PICHINCHA,),
    ) or []
    out["matches_deshechos_recientes"] = [
        {**r,
         "real_fecha": str(r.get("real_fecha") or ""),
         "real_monto": float(r.get("real_monto") or 0),
         "creado_en": str(r.get("creado_en") or ""),
         "deshecho_en": str(r.get("deshecho_en") or "")}
        for r in out["matches_deshechos_recientes"]
    ]
    # 5. PC-only txs últimas
    out["pc_only_txs"] = _db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, importe, saldo, concepto,
               COALESCE(usuario_crea,'') AS usuario_crea
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND COALESCE(usuario_crea,'') NOT IN ('','dbf-import','asinfo-backfill','dbase-sync')
         ORDER BY fecha DESC, id_transaccion DESC
         LIMIT 50
        """,
        (_BANCO_PICHINCHA,),
    ) or []
    out["pc_only_txs"] = [
        {**r, "fecha": str(r["fecha"]),
         "importe": float(r["importe"] or 0),
         "saldo": float(r["saldo"] or 0)}
        for r in out["pc_only_txs"]
    ]
    out["last_libros"] = out["top_txs"][0]["saldo"] if out["top_txs"] else None
    # 6. Bitácora entries de borrar sesión + conciliación (audit trail recuperable)
    try:
        out["bitacora_acciones_recientes"] = _db.fetch_all(
            """
            SELECT id_bitacora, ts, usuario, metodo, ruta, modulo, accion,
                   entidad, id_entidad, status_http, payload, resumen
              FROM scintela.bitacora_acciones
             WHERE ts >= NOW() - INTERVAL '2 days'
               AND (
                    ruta ILIKE '%conciliacion%'
                 OR ruta ILIKE '%dbase-sync%'
                 OR ruta ILIKE '%borrar%'
                 OR modulo = 'conciliacion'
                 OR entidad IN ('match', 'sesion', 'tx_bancaria')
               )
             ORDER BY ts DESC
             LIMIT 100
            """,
            (),
        ) or []
        out["bitacora_acciones_recientes"] = [
            {**r, "ts": str(r.get("ts") or ""),
             "resumen": (r.get("resumen") or "")[:200],
             # FULL payload (jsonb) — necesario para recuperar hist_ids etc.
             "payload": r.get("payload")}
            for r in out["bitacora_acciones_recientes"]
        ]
    except Exception as e:
        out["bitacora_error"] = str(e)[:200]
    return jsonify(out)


@bp.route("/matches-duplicados-id-tx", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def matches_duplicados_id_tx():
    """TMT 2026-06-03 audit blindaje: detecta el bug single-claim roto.

    Reporta matches activos donde N matches comparten el mismo
    id_transaccion (mismo PC tx matcheado N veces contra N banco distintos).
    Si los N matches tienen el MISMO confirm_batch_id, es agrupado legítimo
    (caso impuestos). Si tienen batch_ids distintos, es bug — la dueña
    matcheó el mismo PC tx en sesiones/clicks distintos.
    """
    rows = _db.fetch_all(
        """
        WITH agg AS (
            SELECT id_transaccion,
                   COUNT(*) AS n_matches,
                   COUNT(DISTINCT COALESCE(confirm_batch_id, '')) AS n_batches,
                   ARRAY_AGG(id ORDER BY id) AS match_ids,
                   ARRAY_AGG(real_documento ORDER BY id) AS real_docs,
                   ARRAY_AGG(real_monto ORDER BY id) AS real_montos,
                   ARRAY_AGG(confirm_batch_id ORDER BY id) AS batch_ids
              FROM scintela.banco_conciliacion_match
             WHERE no_banco = %s
               AND deshecho_en IS NULL
               AND id_transaccion IS NOT NULL
             GROUP BY id_transaccion
            HAVING COUNT(*) > 1
        )
        SELECT agg.*,
               tb.fecha AS tx_fecha,
               tb.documento AS tx_documento,
               tb.importe AS tx_importe,
               tb.concepto AS tx_concepto,
               (agg.n_batches > 1) AS es_bug_multi_batch
          FROM agg
          LEFT JOIN scintela.transacciones_bancarias tb
            ON tb.id_transaccion = agg.id_transaccion
         ORDER BY agg.n_matches DESC, agg.id_transaccion DESC
         LIMIT 200
        """,
        (_BANCO_PICHINCHA,),
    ) or []
    bugs = [r for r in rows if r.get("es_bug_multi_batch")]
    grupos_ok = [r for r in rows if not r.get("es_bug_multi_batch")]
    return jsonify({
        "ok": True,
        "no_banco": _BANCO_PICHINCHA,
        "total_groups": len(rows),
        "n_bugs_multi_batch": len(bugs),
        "n_grupos_legitimos_mismo_batch": len(grupos_ok),
        "bugs_multi_batch_top20": [
            {
                "id_transaccion": int(r["id_transaccion"]),
                "n_matches": int(r["n_matches"]),
                "n_batches": int(r["n_batches"]),
                "tx_fecha": str(r.get("tx_fecha") or ""),
                "tx_documento": r.get("tx_documento"),
                "tx_importe": float(r.get("tx_importe") or 0),
                "tx_concepto": (r.get("tx_concepto") or "")[:50],
                "match_ids": list(r.get("match_ids") or []),
                "real_docs": list(r.get("real_docs") or []),
                "real_montos": [float(x) for x in (r.get("real_montos") or [])],
                "batch_ids": list(r.get("batch_ids") or []),
            }
            for r in bugs[:20]
        ],
    })


@bp.route("/borrar-matches-duplicados", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def borrar_matches_duplicados():
    """Deshacer matches duplicados que comparten id_transaccion sin batch.

    Para cada id_transaccion con N>1 matches y N>1 batches distintos:
      - Conservar el match con id MENOR (el primero creado).
      - Soft-undo el resto (deshecho_en = NOW, deshecho_por = 'mig-dup-cleanup').
      - Revertir conciliado_en en histos linkeados a los deshechos.

    Idempotente: si no quedan dupes, no hace nada.
    """
    dupes = _db.fetch_all(
        """
        SELECT id_transaccion,
               ARRAY_AGG(id ORDER BY id) AS match_ids,
               COUNT(DISTINCT COALESCE(confirm_batch_id, '')) AS n_batches
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s
           AND deshecho_en IS NULL
           AND id_transaccion IS NOT NULL
         GROUP BY id_transaccion
        HAVING COUNT(*) > 1 AND COUNT(DISTINCT COALESCE(confirm_batch_id, '')) > 1
        """,
        (_BANCO_PICHINCHA,),
    ) or []
    n_deshechos = 0
    ids_deshechos = []
    for r in dupes:
        match_ids = list(r.get("match_ids") or [])
        if len(match_ids) <= 1:
            continue
        # Conservar el primero (menor id), deshacer el resto.
        keep_id = match_ids[0]
        deshacer_ids = match_ids[1:]
        try:
            with _db.tx() as conn:
                # Soft-undo
                n = _db.execute(
                    """
                    UPDATE scintela.banco_conciliacion_match
                       SET deshecho_en = CURRENT_TIMESTAMP,
                           deshecho_por = 'audit-dup-cleanup-2026-06-03'
                     WHERE id = ANY(%s)
                       AND deshecho_en IS NULL
                    """,
                    (deshacer_ids,), conn=conn,
                ) or 0
                # Revertir histos linkeados
                _db.execute(
                    """
                    UPDATE scintela.banco_historicos_pendientes
                       SET conciliado_en = NULL,
                           conciliado_por = NULL,
                           conciliado_match_id = NULL
                     WHERE conciliado_match_id = ANY(%s)
                    """,
                    (deshacer_ids,), conn=conn,
                )
                n_deshechos += int(n)
                ids_deshechos.extend(deshacer_ids)
        except Exception as e:
            _LOG.warning("borrar_matches_duplicados id_tx=%s falló: %s",
                         r.get("id_transaccion"), e)
    return jsonify({
        "ok": True,
        "n_deshechos": n_deshechos,
        "ids_deshechos": ids_deshechos[:200],
        "n_grupos_procesados": len(dupes),
    })


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def diagnose():
    """Cuenta pendientes totales + duplicados por (no_banco, documento)."""
    out: dict = {"ok": True, "no_banco": _BANCO_PICHINCHA}

    # 1. Total pendientes (no conciliados).
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s AND conciliado_en IS NULL
            """,
            (_BANCO_PICHINCHA,),
        )
        out["pendientes_totales"] = int(row["n"]) if row else 0
    except Exception as e:
        out["pendientes_totales"] = None
        out["error_total"] = str(e)

    # 2. Pendientes con documento vacío (no se pueden dedupear).
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
               AND (documento IS NULL OR documento = '')
            """,
            (_BANCO_PICHINCHA,),
        )
        out["pendientes_sin_documento"] = int(row["n"]) if row else 0
    except Exception as e:
        out["pendientes_sin_documento"] = None
        out["error_sin_doc"] = str(e)

    # 2.5. Cuántas filas marcó la mig 0063 como conciliadas (post-deploy).
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s AND conciliado_por = 'mig-0063-dedupe'
            """,
            (_BANCO_PICHINCHA,),
        )
        out["mig_0063_dedupeadas"] = int(row["n"]) if row else 0
    except Exception as e:
        out["error_mig0063"] = str(e)

    # 2.6. Duplicados REALES por firma estricta (la que usa mig 0063):
    # (no_banco, documento, tipo, monto, fecha). Si esto da 0, no hay nada
    # para dedupear y los 548 son pendientes únicos reales.
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS grupos,
                   COALESCE(SUM(extras), 0) AS filas_extra
              FROM (
                SELECT COUNT(*) - 1 AS extras
                  FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s
                   AND conciliado_en IS NULL
                   AND documento IS NOT NULL AND documento <> ''
                 GROUP BY no_banco, documento, tipo, monto, fecha
                HAVING COUNT(*) > 1
              ) t
            """,
            (_BANCO_PICHINCHA,),
        )
        out["dup_estrictos_grupos"] = int(row["grupos"]) if row else 0
        out["dup_estrictos_filas_extra"] = int(row["filas_extra"]) if row else 0
    except Exception as e:
        out["error_dup_estrictos"] = str(e)

    # 3. Documentos duplicados — grupos de (no_banco, documento) con >1 fila pendiente.
    # TMT 2026-06-02 dueña: 'puede ser que esos no esten duplicados entonces?'
    # Ahora desglosamos por tipo: si todas las ocurrencias tienen el mismo
    # tipo (todos C o todos D), es duplicado real. Si tienen tipos mezclados
    # (C+D), es un par legítimo (cargo + reverso, neto $0) — NO dedupear.
    try:
        rows = _db.fetch_all(
            """
            SELECT documento,
                   COUNT(*) AS n,
                   SUM(monto) AS suma_monto,
                   MIN(fecha) AS fecha_min, MAX(fecha) AS fecha_max,
                   COUNT(*) FILTER (WHERE tipo = 'C') AS n_creditos,
                   COUNT(*) FILTER (WHERE tipo = 'D') AS n_debitos,
                   ARRAY_AGG(DISTINCT tipo) AS tipos
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
               AND documento IS NOT NULL AND documento <> ''
             GROUP BY documento
            HAVING COUNT(*) > 1
             ORDER BY n DESC, documento ASC
             LIMIT 30
            """,
            (_BANCO_PICHINCHA,),
        ) or []
        out["docs_duplicados_top30"] = [
            {
                "documento": r["documento"],
                "ocurrencias": int(r["n"]),
                "suma_monto": float(r["suma_monto"] or 0),
                "fecha_min": str(r["fecha_min"]) if r.get("fecha_min") else None,
                "fecha_max": str(r["fecha_max"]) if r.get("fecha_max") else None,
                "n_creditos": int(r["n_creditos"] or 0),
                "n_debitos": int(r["n_debitos"] or 0),
                "tipos": list(r.get("tipos") or []),
                "es_duplicado_real": (
                    int(r["n_creditos"] or 0) == 0 or int(r["n_debitos"] or 0) == 0
                ),
            }
            for r in rows
        ]
        # Conteo global desglosando duplicados reales (mismo tipo) vs pares (C+D).
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS grupos_total,
                   COUNT(*) FILTER (WHERE n_c = 0 OR n_d = 0) AS grupos_dup_real,
                   COUNT(*) FILTER (WHERE n_c > 0 AND n_d > 0) AS grupos_par_cd,
                   COALESCE(SUM(extras) FILTER (WHERE n_c = 0 OR n_d = 0), 0) AS filas_extra_dup_real,
                   COALESCE(SUM(extras) FILTER (WHERE n_c > 0 AND n_d > 0), 0) AS filas_extra_par_cd
              FROM (
                SELECT COUNT(*) - 1 AS extras,
                       COUNT(*) FILTER (WHERE tipo='C') AS n_c,
                       COUNT(*) FILTER (WHERE tipo='D') AS n_d
                  FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s
                   AND conciliado_en IS NULL
                   AND documento IS NOT NULL AND documento <> ''
                 GROUP BY documento
                HAVING COUNT(*) > 1
              ) t
            """,
            (_BANCO_PICHINCHA,),
        )
        out["grupos_duplicados_total"] = int(row["grupos_total"]) if row else 0
        out["grupos_dup_mismo_tipo"] = int(row["grupos_dup_real"]) if row else 0
        out["grupos_par_C_D"] = int(row["grupos_par_cd"]) if row else 0
        out["filas_extra_dup_mismo_tipo"] = int(row["filas_extra_dup_real"]) if row else 0
        out["filas_extra_par_C_D"] = int(row["filas_extra_par_cd"]) if row else 0
        # Backward compat con clave vieja.
        out["filas_extra_duplicadas"] = out["filas_extra_dup_mismo_tipo"]
        out["grupos_duplicados"] = out["grupos_dup_mismo_tipo"]
    except Exception as e:
        out["error_dup"] = str(e)

    # 4. Duplicados cruzados extracto-de-sesión vs histos: docs que están
    #    en el payload de la sesión abierta Y EN banco_historicos_pendientes.
    try:
        from modules.conciliacion import sesion as _ses
        s = _ses.sesion_abierta(_BANCO_PICHINCHA)
        if s:
            movs = _ses.cargar_movs(s)
            docs_sesion = {
                (m.documento or "").strip().upper()
                for m in movs if m.documento
            }
            docs_sesion.discard("")
            if docs_sesion:
                rows = _db.fetch_all(
                    """
                    SELECT documento, COUNT(*) AS n
                      FROM scintela.banco_historicos_pendientes
                     WHERE no_banco = %s
                       AND conciliado_en IS NULL
                       AND UPPER(documento) = ANY(%s::text[])
                     GROUP BY documento
                     LIMIT 30
                    """,
                    (_BANCO_PICHINCHA, list(docs_sesion)),
                ) or []
                out["docs_en_sesion_y_histos"] = [
                    {"documento": r["documento"], "ocurrencias": int(r["n"])}
                    for r in rows
                ]
                out["n_docs_solapados"] = len(rows)
            else:
                out["docs_en_sesion_y_histos"] = []
        else:
            out["sesion_abierta"] = False
    except Exception as e:
        out["error_solapados"] = str(e)

    return jsonify(out)


@bp.route("/reset-y-cargar", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def reset_y_cargar():
    """Reemplaza TODO el historial de pendientes banco con la lista
    provista en el body JSON. Operación atómica:

      1. DELETE banco_historicos_pendientes pendientes (WHERE conciliado_en IS NULL).
      2. Vacía el extracto_payload de la sesión abierta del banco.
      3. INSERTa todas las filas del payload.

    Body esperado:
      {"records": [{fecha, concepto, documento, monto, tipo, detalle}, ...]}

    Devuelve contadores. La dueña pidió esto explícitamente:
    'borres el historial y cargues estos movimientos como historial'.
    """
    body = request.get_json(silent=True) or {}
    records = body.get("records") or []
    if not isinstance(records, list) or not records:
        return jsonify({"ok": False, "error": "body.records vacío o inválido"}), 400

    no_banco = _BANCO_PICHINCHA
    out = {"ok": True, "no_banco": no_banco}

    try:
        with _db.tx() as conn:
            # 1) Borrar pendientes.
            n_del = _db.execute(
                """
                DELETE FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s AND conciliado_en IS NULL
                """,
                (no_banco,),
                conn=conn,
            ) or 0
            out["histos_borrados"] = int(n_del)

            # 2) Vaciar payload de la sesión abierta.
            from modules.conciliacion import sesion as _ses
            sesion = _ses.sesion_abierta(no_banco)
            if sesion:
                _db.execute(
                    """
                    UPDATE scintela.banco_conciliacion_sesion
                       SET extracto_payload = '[]'::jsonb
                     WHERE id = %s
                    """,
                    (int(sesion["id"]),),
                    conn=conn,
                )
                out["sesion_payload_vaciada"] = int(sesion["id"])
            else:
                out["sesion_payload_vaciada"] = None

            # 3) Insertar las filas nuevas. Detectar si la columna `codigo`
            # existe — la mig 0064 puede no estar aplicada en prod.
            tiene_codigo = False
            try:
                r_col = _db.fetch_one(
                    """
                    SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'scintela'
                       AND table_name = 'banco_historicos_pendientes'
                       AND column_name = 'codigo'
                    """,
                    conn=conn,
                )
                tiene_codigo = bool(r_col)
            except Exception:
                tiene_codigo = False
            out["tiene_codigo_col"] = tiene_codigo

            if tiene_codigo:
                sql = """
                    INSERT INTO scintela.banco_historicos_pendientes
                        (no_banco, fecha, concepto, documento, monto, tipo,
                         oficina, detalle, fuente, creado_por, codigo)
                    VALUES (%s, %s, %s, %s, %s::numeric, %s, %s, %s, %s, %s, %s)
                """
            else:
                sql = """
                    INSERT INTO scintela.banco_historicos_pendientes
                        (no_banco, fecha, concepto, documento, monto, tipo,
                         oficina, detalle, fuente, creado_por)
                    VALUES (%s, %s, %s, %s, %s::numeric, %s, %s, %s, %s, %s)
                """

            n_ins = 0
            errores = []
            for i, r in enumerate(records):
                try:
                    params = [
                        no_banco,
                        r.get("fecha"),
                        (r.get("concepto") or "")[:120],
                        (r.get("documento") or "")[:40],
                        str(r.get("monto") or 0),
                        (r.get("tipo") or "C")[:1],
                        "",  # oficina
                        (r.get("detalle") or "")[:30],
                        "feb2023-xlsx-2026-06-02",
                        _usuario_actual()[:50],
                    ]
                    if tiene_codigo:
                        params.append((r.get("codigo") or "")[:20])
                    _db.execute(sql, tuple(params), conn=conn)
                    n_ins += 1
                except Exception as e:
                    errores.append({"i": i, "doc": r.get("documento"), "err": str(e)[:120]})
            out["insertados"] = n_ins
            if errores:
                out["errores"] = errores[:20]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    return jsonify(out)


@bp.route("/borrar-conciliados-sesion", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def borrar_conciliados_sesion():
    """Borra todos los matches creados en la ventana de la sesión abierta
    actual. Resetea stat='*' en las txs PC asociadas (excepto las que
    vinieron del DBF original) y conciliado_en en los histos linkeados.

    TMT 2026-06-02 dueña: 'borremos estos 36, que son de otra sesion y
    no sirven'.
    """
    no_banco = _BANCO_PICHINCHA
    from modules.conciliacion import sesion as _ses
    sesion = _ses.sesion_abierta(no_banco)
    if not sesion:
        return jsonify({"ok": False, "error": "no hay sesión abierta"}), 400

    abierta_en = sesion.get("abierta_en")
    out = {"ok": True, "sesion_id": sesion["id"]}

    try:
        with _db.tx() as conn:
            # 1) Capturar IDs de matches afectados (para reset histos / stat).
            rows = _db.fetch_all(
                """
                SELECT id, id_transaccion
                  FROM scintela.banco_conciliacion_match
                 WHERE no_banco = %s
                   AND creado_en >= %s
                """,
                (no_banco, abierta_en),
                conn=conn,
            ) or []
            match_ids = [r["id"] for r in rows]
            tx_ids = [r["id_transaccion"] for r in rows if r.get("id_transaccion")]
            out["matches_encontrados"] = len(match_ids)
            out["txs_afectadas"] = len(set(tx_ids))

            # 2) Reset conciliado_en en histos linkeados (vuelven a pendientes).
            if match_ids:
                n_hist = _db.execute(
                    """
                    UPDATE scintela.banco_historicos_pendientes
                       SET conciliado_en = NULL,
                           conciliado_match_id = NULL,
                           conciliado_por = NULL
                     WHERE conciliado_match_id = ANY(%s)
                    """,
                    (match_ids,),
                    conn=conn,
                ) or 0
                out["histos_revertidos"] = int(n_hist)

            # 3) Reset stat='*' en txs PC (excepto las que vinieron del DBF
            # original — esas las mantiene el sync con dBase).
            if tx_ids:
                n_stat = _db.execute(
                    """
                    UPDATE scintela.transacciones_bancarias
                       SET stat = NULL
                     WHERE id_transaccion = ANY(%s)
                       AND no_banco = %s
                       AND usuario_crea NOT IN ('dbf-import', 'asinfo-backfill')
                    """,
                    (list(set(tx_ids)), no_banco),
                    conn=conn,
                ) or 0
                out["stat_reset"] = int(n_stat)

            # 4) Hard-delete de los matches.
            if match_ids:
                n_del = _db.execute(
                    """
                    DELETE FROM scintela.banco_conciliacion_match
                     WHERE id = ANY(%s)
                    """,
                    (match_ids,),
                    conn=conn,
                ) or 0
                out["matches_borrados"] = int(n_del)

            # 5) Reset stat='*' orfanos: TODOS los PCs marcados conciliados
            # sin match activo. Incluye dbf-import porque las conciliaciones
            # N:M con código viejo dejaron PCs dbf-import sin match. La
            # próxima sync dBase los restablece si en dBase siguen '*'.
            try:
                n_orphan = _db.execute(
                    """
                    UPDATE scintela.transacciones_bancarias tb
                       SET stat = NULL
                     WHERE tb.no_banco = %s
                       AND TRIM(COALESCE(tb.stat, '')) = '*'
                       AND NOT EXISTS (
                           SELECT 1 FROM scintela.banco_conciliacion_match m
                            WHERE m.id_transaccion = tb.id_transaccion
                              AND m.deshecho_en IS NULL
                       )
                    """,
                    (no_banco,),
                    conn=conn,
                ) or 0
                out["stat_orphans_limpiados"] = int(n_orphan)
            except Exception as e:
                _LOG.warning("limpiar stat orfans falló: %s", e)

            # 6) Reset contador de la sesión.
            _db.execute(
                """
                UPDATE scintela.banco_conciliacion_sesion
                   SET matches_hechos = 0
                 WHERE id = %s
                """,
                (int(sesion["id"]),),
                conn=conn,
            )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    return jsonify(out)


@bp.route("/stat-orphans", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def stat_orphans():
    """Lista PCs con stat='*' sin match activo.

    GET: solo lista (dry-run).
    POST con ?fix=1: resetea stat=NULL en esos PCs.
    """
    no_banco = _BANCO_PICHINCHA
    rows = _db.fetch_all(
        """
        SELECT t.id_transaccion, t.fecha, t.documento, t.importe,
               t.numreferencia, t.usuario_crea, t.fecha_crea, t.concepto
          FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = %s
           AND TRIM(COALESCE(t.stat, '')) = '*'
           AND NOT EXISTS (
               SELECT 1 FROM scintela.banco_conciliacion_match m
                WHERE m.id_transaccion = t.id_transaccion
                  AND m.deshecho_en IS NULL
           )
         ORDER BY t.fecha DESC, t.id_transaccion DESC
         LIMIT 500
        """,
        (no_banco,),
    ) or []

    out = {
        "ok": True,
        "n_orphans": len(rows),
        "ejemplos": [
            {
                "id": r["id_transaccion"],
                "fecha": str(r["fecha"]) if r.get("fecha") else None,
                "doc": r.get("documento"),
                "importe": float(r.get("importe") or 0),
                "numref": r.get("numreferencia"),
                "usuario_crea": r.get("usuario_crea"),
                "fecha_crea": str(r["fecha_crea"]) if r.get("fecha_crea") else None,
                "concepto": (r.get("concepto") or "")[:60],
            }
            for r in rows[:50]
        ],
    }

    if request.method == "POST" and request.args.get("fix") == "1":
        # Filtro: solo PCs recientes (últimos 30 días). Excluye dbf legacy.
        try:
            n = _db.execute(
                """
                UPDATE scintela.transacciones_bancarias t
                   SET stat = NULL
                 WHERE t.no_banco = %s
                   AND TRIM(COALESCE(t.stat, '')) = '*'
                   AND t.fecha_crea >= NOW() - INTERVAL '30 days'
                   AND NOT EXISTS (
                       SELECT 1 FROM scintela.banco_conciliacion_match m
                        WHERE m.id_transaccion = t.id_transaccion
                          AND m.deshecho_en IS NULL
                   )
                """,
                (no_banco,),
            ) or 0
            out["fix_aplicado"] = True
            out["resetados"] = int(n)
        except Exception as e:
            out["fix_error"] = str(e)
    return jsonify(out)


@bp.route("/borrar-no-feb2023", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def borrar_no_feb2023():
    """Borra pendientes en banco_historicos_pendientes cuya fuente NO sea
    el load de la dueña 'feb2023-xlsx-2026-06-02'. Los demás son legacy.

    GET = dry-run con contadores.
    POST = ejecuta DELETE.
    """
    no_banco = _BANCO_PICHINCHA
    row = _db.fetch_one(
        """
        SELECT
          COUNT(*) FILTER (WHERE conciliado_en IS NULL) AS pend_total,
          COUNT(*) FILTER (WHERE conciliado_en IS NULL
                            AND fuente NOT LIKE 'feb2023%%') AS pend_no_feb,
          COUNT(*) FILTER (WHERE conciliado_en IS NULL
                            AND fuente LIKE 'feb2023%%') AS pend_feb
          FROM scintela.banco_historicos_pendientes
         WHERE no_banco = %s
        """,
        (no_banco,),
    )
    out = {
        "ok": True,
        "pend_total": int(row["pend_total"]) if row else 0,
        "pend_feb2023": int(row["pend_feb"]) if row else 0,
        "pend_no_feb2023": int(row["pend_no_feb"]) if row else 0,
    }
    if request.method == "POST":
        try:
            n = _db.execute(
                """
                DELETE FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s
                   AND conciliado_en IS NULL
                   AND (fuente IS NULL OR fuente NOT LIKE 'feb2023%%')
                """,
                (no_banco,),
            ) or 0
            out["borrados"] = int(n)
            out["modo"] = "ejecutado"
        except Exception as e:
            out["error"] = str(e)[:200]
    else:
        out["modo"] = "dry-run"
    return jsonify(out)


@bp.route("/borrar-ac-duplicados", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def borrar_ac_duplicados():
    """Borra las entries AC SALDO + IN OP de PC.transacciones_bancarias que
    duplican los ND reales del banco. También borra matches + recompute saldos.

    GET = dry-run con la lista.
    POST = ejecuta.
    """
    no_banco = _BANCO_PICHINCHA
    # Identificar candidatos: doc='ND' AND concepto LIKE 'AC % SALDO' o 'IN OP AC %' o 'RR OP AC%'
    rows = _db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, importe, concepto, stat
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND fecha >= '2026-06-01'
           AND documento = 'ND'
           AND (
               concepto LIKE 'AC %% SALDO%%'
            OR concepto LIKE 'IN OP AC%%'
            OR concepto LIKE 'RR OP AC%%'
            OR concepto = 'CORRECC'
            OR concepto = 'GS BANCO'
           )
         ORDER BY fecha, id_transaccion
        """,
        (no_banco,),
    ) or []
    ids = [r["id_transaccion"] for r in rows]
    suma = sum(float(r.get("importe") or 0) for r in rows)
    out = {
        "ok": True,
        "candidatos": len(ids),
        "suma_importes": round(suma, 2),
        "ids": ids,
        "preview": [
            {"id": r["id_transaccion"], "doc": r["documento"],
             "importe": float(r["importe"] or 0),
             "concepto": r.get("concepto"), "stat": r.get("stat")}
            for r in rows[:30]
        ],
    }
    if request.method != "POST":
        out["modo"] = "dry-run"
        return jsonify(out)

    try:
        with _db.tx() as conn:
            # 1) Borrar matches que apuntan a esos ids.
            n_match = _db.execute(
                """
                DELETE FROM scintela.banco_conciliacion_match
                 WHERE id_transaccion = ANY(%s)
                """,
                (ids,),
                conn=conn,
            ) or 0
            # 2) Borrar las txs.
            n_del = _db.execute(
                """
                DELETE FROM scintela.transacciones_bancarias
                 WHERE id_transaccion = ANY(%s) AND no_banco = %s
                """,
                (ids, no_banco),
                conn=conn,
            ) or 0
            # 3) Recompute saldos desde el primer mov.
            import bank_helpers
            primera = _db.fetch_one(
                """
                SELECT fecha FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s AND fecha IS NOT NULL
                 ORDER BY fecha ASC LIMIT 1
                """,
                (no_banco,),
                conn=conn,
            )
            n_rec = 0
            if primera and primera.get("fecha"):
                n_rec = bank_helpers.recompute_saldos_desde(
                    conn, no_banco=no_banco, no_cta=None,
                    ancla_fecha=primera["fecha"],
                ) or 0
        out["modo"] = "ejecutado"
        out["matches_borrados"] = int(n_match)
        out["txs_borradas"] = int(n_del)
        out["saldos_recompute"] = int(n_rec)
    except Exception as e:
        out["error"] = str(e)[:300]
    return jsonify(out)


@bp.route("/borrar-nd-dobles-20260609", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def borrar_nd_dobles_20260609():
    """One-off TMT 2026-06-09: borra las ND peladas duplicadas de la carga
    manual del 09/06 (bug Enter-submit en /bancos/nuevo-movimiento).

    Candidatas = ND Pichincha del 09/06/2026, concepto VACÍO, usuario_crea
    'andres', NO conciliadas (stat <> '*'). Eso da exactamente 29016 y
    29018 — las gemelas peladas de 29017 (cae) y 29019 (53 cae). La 29022
    (sin concepto pero stat='*') NO se toca.

    Borra tx + mov_doble linkeados + recompute saldos. GET = dry-run.
    """
    no_banco = _BANCO_PICHINCHA
    rows = _db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, importe, concepto, stat,
               usuario_crea
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND fecha = '2026-06-09'
           AND documento = 'ND'
           AND TRIM(COALESCE(concepto, '')) = ''
           AND TRIM(COALESCE(usuario_crea, '')) = 'andres'
           AND COALESCE(stat, '') <> '*'
         ORDER BY id_transaccion
        """,
        (no_banco,),
    ) or []
    ids = [r["id_transaccion"] for r in rows]
    out = {
        "ok": True,
        "candidatos": len(ids),
        "ids": ids,
        "preview": [
            {"id": r["id_transaccion"], "doc": r["documento"],
             "importe": float(r["importe"] or 0),
             "concepto": r.get("concepto"), "stat": r.get("stat"),
             "usuario": r.get("usuario_crea")}
            for r in rows
        ],
    }
    if request.method != "POST":
        out["modo"] = "dry-run"
        return jsonify(out)
    if not ids:
        out["modo"] = "ejecutado"
        out["txs_borradas"] = 0
        return jsonify(out)

    try:
        with _db.tx() as conn:
            n_match = _db.execute(
                "DELETE FROM scintela.banco_conciliacion_match "
                "WHERE id_transaccion = ANY(%s)",
                (ids,), conn=conn,
            ) or 0
            n_md = _db.execute(
                """
                DELETE FROM scintela.mov_doble
                 WHERE (origen_table = 'transacciones_bancarias' AND origen_id = ANY(%s))
                    OR (destino_table = 'transacciones_bancarias' AND destino_id = ANY(%s))
                """,
                (ids, ids), conn=conn,
            ) or 0
            n_del = _db.execute(
                "DELETE FROM scintela.transacciones_bancarias "
                "WHERE id_transaccion = ANY(%s) AND no_banco = %s",
                (ids, no_banco), conn=conn,
            ) or 0
            import bank_helpers
            primera = _db.fetch_one(
                "SELECT fecha FROM scintela.transacciones_bancarias "
                "WHERE no_banco = %s AND fecha IS NOT NULL "
                "ORDER BY fecha ASC LIMIT 1",
                (no_banco,), conn=conn,
            )
            n_rec = 0
            if primera and primera.get("fecha"):
                n_rec = bank_helpers.recompute_saldos_desde(
                    conn, no_banco=no_banco, no_cta=None,
                    ancla_fecha=primera["fecha"],
                ) or 0
        out["modo"] = "ejecutado"
        out["matches_borrados"] = int(n_match)
        out["mov_doble_borrados"] = int(n_md)
        out["txs_borradas"] = int(n_del)
        out["saldos_recompute"] = int(n_rec)
    except Exception as e:
        out["error"] = str(e)[:300]
    return jsonify(out)


@bp.route("/test-relink-direct", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def test_relink_direct():
    """Test directo: usa UPDATE plain (no función) para relink."""
    no_banco = _BANCO_PICHINCHA
    pcs = _db.fetch_all(
        """
        SELECT id_transaccion FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND fecha >= '2026-06-01'
           AND COALESCE(TRIM(stat), '') <> '*'
         ORDER BY id_transaccion ASC LIMIT 3
        """,
        (no_banco,),
    ) or []
    test_ids = []
    try:
        for p in pcs:
            _db.execute(
                """
                INSERT INTO scintela.banco_conciliacion_match
                    (no_banco, estado, id_transaccion, tx_firma, usuario)
                VALUES (%s, 'matched', %s, scintela.compute_tx_firma(%s), 'direct-test')
                """,
                (no_banco, p["id_transaccion"], p["id_transaccion"]),
            )
        new_matches = _db.fetch_all(
            "SELECT id, id_transaccion, tx_firma FROM scintela.banco_conciliacion_match WHERE no_banco=%s AND usuario='direct-test' ORDER BY id DESC LIMIT 3",
            (no_banco,),
        ) or []
        test_ids = [m["id"] for m in new_matches]
        originals = {m["id"]: m["id_transaccion"] for m in new_matches}

        # Break
        max_id = _db.fetch_one("SELECT MAX(id_transaccion) AS m FROM scintela.transacciones_bancarias WHERE no_banco=%s", (no_banco,)) or {}
        huerfano = int(max_id.get("m") or 0) + 999999
        _db.execute("UPDATE scintela.banco_conciliacion_match SET id_transaccion = %s WHERE id = ANY(%s)", (huerfano, test_ids))

        # Direct relink via correlated subquery
        n_updated = _db.execute(
            """
            UPDATE scintela.banco_conciliacion_match m
               SET id_transaccion = (
                 SELECT t.id_transaccion
                   FROM scintela.transacciones_bancarias t
                  WHERE t.no_banco = m.no_banco
                    AND (COALESCE(t.fecha::TEXT, '') || '|'
                      || COALESCE(t.documento, '') || '|'
                      || COALESCE(t.importe::TEXT, '0') || '|'
                      || COALESCE(t.numreferencia::TEXT, '') || '|'
                      || COALESCE(LEFT(t.concepto, 40), '')) = m.tx_firma
                  ORDER BY t.id_transaccion ASC LIMIT 1
               )
             WHERE m.id = ANY(%s)
               AND m.tx_firma IS NOT NULL
            """,
            (test_ids,),
        )

        # Post-update state
        recovered = _db.fetch_all(
            "SELECT id, id_transaccion FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)",
            (test_ids,),
        ) or []
        rec_map = {r["id"]: r["id_transaccion"] for r in recovered}
        ok_count = sum(1 for mid, orig in originals.items() if rec_map.get(mid) == orig)

        return jsonify({
            "n_updated_reported": n_updated,
            "originals": originals,
            "recovered": rec_map,
            "ok": ok_count == len(originals),
            "ok_count": ok_count,
        })
    finally:
        if test_ids:
            try:
                _db.execute("DELETE FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)", (test_ids,))
            except Exception:
                pass


@bp.route("/probe-fn-source", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def probe_fn_source():
    """Devuelve el source de la función relink y los triggers en la tabla."""
    fn = _db.fetch_one(
        "SELECT pg_get_functiondef(p.oid) AS body FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid WHERE n.nspname = 'scintela' AND p.proname = 'relink_matches_post_sync'",
    ) or {}
    triggers = _db.fetch_all(
        """
        SELECT t.tgname, pg_get_triggerdef(t.oid) AS def
          FROM pg_trigger t
          JOIN pg_class c ON t.tgrelid = c.oid
          JOIN pg_namespace n ON c.relnamespace = n.oid
         WHERE n.nspname = 'scintela' AND c.relname = 'banco_conciliacion_match'
           AND NOT t.tgisinternal
        """,
    ) or []
    return jsonify({"fn_body": (fn.get("body") or "")[:5000], "triggers": triggers})


@bp.route("/test-relink-trace", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def test_relink_trace():
    """Trace de TODA la secuencia con ids intermedios:
    1) Crea, registra firmas + ids.
    2) Break.
    3) Captura estado pre-relink.
    4) Relink.
    5) Captura estado post-relink.
    6) Reporta diff.
    7) Cleanup."""
    no_banco = _BANCO_PICHINCHA
    pcs = _db.fetch_all(
        """
        SELECT id_transaccion FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND fecha >= '2026-06-01'
           AND COALESCE(TRIM(stat), '') <> '*'
         ORDER BY id_transaccion ASC LIMIT 3
        """,
        (no_banco,),
    ) or []
    test_ids = []
    try:
        # 1) Crear
        for p in pcs:
            _db.execute(
                "INSERT INTO scintela.banco_conciliacion_match (no_banco, estado, id_transaccion, usuario) VALUES (%s, 'matched', %s, 'trace-test')",
                (no_banco, p["id_transaccion"]),
            )
        created = _db.fetch_all(
            "SELECT id, id_transaccion, tx_firma FROM scintela.banco_conciliacion_match WHERE no_banco=%s AND usuario='trace-test' ORDER BY id DESC LIMIT 3",
            (no_banco,),
        ) or []
        test_ids = [m["id"] for m in created]
        s1 = [{"id": m["id"], "tx": m["id_transaccion"], "f": m["tx_firma"]} for m in created]

        # 2) Break
        max_id = _db.fetch_one("SELECT MAX(id_transaccion) AS m FROM scintela.transacciones_bancarias WHERE no_banco=%s", (no_banco,)) or {}
        huerfano = int(max_id.get("m") or 0) + 999999
        _db.execute("UPDATE scintela.banco_conciliacion_match SET id_transaccion = %s WHERE id = ANY(%s)", (huerfano, test_ids))

        # 3) Pre-relink
        pre = _db.fetch_all("SELECT id, id_transaccion, tx_firma FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)", (test_ids,)) or []
        s2 = [{"id": m["id"], "tx": m["id_transaccion"], "f": m["tx_firma"]} for m in pre]

        # 4) Para cada match, ejecutar el SELECT del relink manualmente y comparar con lo que la función reportará
        manual_results = []
        for m in pre:
            row = _db.fetch_one(
                """
                SELECT t.id_transaccion AS new_id
                  FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = %s
                   AND (COALESCE(t.fecha::TEXT, '') || '|'
                     || COALESCE(t.documento, '') || '|'
                     || COALESCE(t.importe::TEXT, '0') || '|'
                     || COALESCE(t.numreferencia::TEXT, '') || '|'
                     || COALESCE(LEFT(t.concepto, 40), '')) = %s
                 ORDER BY t.id_transaccion ASC LIMIT 1
                """,
                (no_banco, m["tx_firma"]),
            ) or {}
            manual_results.append({"match_id": m["id"], "subquery_new_id": row.get("new_id")})

        # 5) Relink
        rel = _db.fetch_one("SELECT * FROM scintela.relink_matches_post_sync(%s)", (no_banco,)) or {}

        # 6) Post-relink
        post = _db.fetch_all("SELECT id, id_transaccion FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)", (test_ids,)) or []
        s3 = [{"id": m["id"], "tx": m["id_transaccion"]} for m in post]

        return jsonify({
            "huerfano_set": huerfano,
            "step1_created": s1,
            "step2_post_break": s2,
            "step3_manual_subquery": manual_results,
            "step4_relink_returned": {"matches_total": rel.get("matches_total"), "relinked": rel.get("relinked"), "sin_firma": rel.get("sin_firma"), "sin_match": rel.get("sin_match")},
            "step5_post_relink": s3,
        })
    finally:
        if test_ids:
            try:
                _db.execute("DELETE FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)", (test_ids,))
            except Exception:
                pass


@bp.route("/probe-relink-step", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def probe_relink_step():
    """Trace de cada paso del relink:
    1) Crear match, capturar firma.
    2) Break id (capturar firma post-update — chequea si trigger la pisó).
    3) Probar el subquery del relink manualmente.
    4) Cleanup."""
    no_banco = _BANCO_PICHINCHA
    pcs = _db.fetch_all(
        """
        SELECT id_transaccion FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND fecha >= '2026-06-01'
           AND COALESCE(TRIM(stat), '') <> '*'
         ORDER BY id_transaccion ASC LIMIT 1
        """,
        (no_banco,),
    ) or []
    if not pcs:
        return jsonify({"ok": False, "note": "no hay pendientes"})

    test_id = None
    try:
        # 1) Crear
        _db.execute(
            """
            INSERT INTO scintela.banco_conciliacion_match (no_banco, estado, id_transaccion, usuario)
            VALUES (%s, 'matched', %s, 'probe-step')
            """,
            (no_banco, pcs[0]["id_transaccion"]),
        )
        m1 = _db.fetch_one(
            "SELECT id, id_transaccion, tx_firma FROM scintela.banco_conciliacion_match WHERE no_banco=%s AND usuario='probe-step' ORDER BY id DESC LIMIT 1",
            (no_banco,),
        ) or {}
        test_id = m1.get("id")
        firma_inicial = m1.get("tx_firma")
        id_orig = m1.get("id_transaccion")

        # 2) Break id (set to huérfano)
        max_id = _db.fetch_one("SELECT MAX(id_transaccion) AS m FROM scintela.transacciones_bancarias WHERE no_banco=%s", (no_banco,)) or {}
        huerfano = int(max_id.get("m") or 0) + 999999
        _db.execute(
            "UPDATE scintela.banco_conciliacion_match SET id_transaccion = %s WHERE id = %s",
            (huerfano, test_id),
        )
        m2 = _db.fetch_one(
            "SELECT id, id_transaccion, tx_firma FROM scintela.banco_conciliacion_match WHERE id = %s",
            (test_id,),
        ) or {}
        firma_post_break = m2.get("tx_firma")

        # 3) Probar subquery del relink manualmente
        encontrado = _db.fetch_one(
            """
            SELECT t.id_transaccion AS new_id,
                   (COALESCE(t.fecha::TEXT, '') || '|'
                 || COALESCE(t.documento, '') || '|'
                 || COALESCE(t.importe::TEXT, '0') || '|'
                 || COALESCE(t.numreferencia::TEXT, '') || '|'
                 || COALESCE(LEFT(t.concepto, 40), '')) AS firma_calc
              FROM scintela.transacciones_bancarias t
             WHERE t.no_banco = %s
               AND (COALESCE(t.fecha::TEXT, '') || '|'
                 || COALESCE(t.documento, '') || '|'
                 || COALESCE(t.importe::TEXT, '0') || '|'
                 || COALESCE(t.numreferencia::TEXT, '') || '|'
                 || COALESCE(LEFT(t.concepto, 40), '')) = %s
             ORDER BY t.id_transaccion ASC LIMIT 1
            """,
            (no_banco, firma_post_break),
        ) or {}

        # 4) Check NOT EXISTS para dead_matches CTE
        ne = _db.fetch_one(
            """
            SELECT NOT EXISTS (
                SELECT 1 FROM scintela.transacciones_bancarias t
                 WHERE t.id_transaccion = %s
            ) AS no_existe
            """,
            (huerfano,),
        ) or {}

        return jsonify({
            "id_orig": id_orig,
            "huerfano_set": huerfano,
            "firma_inicial": firma_inicial,
            "firma_post_break": firma_post_break,
            "firma_iguales": firma_inicial == firma_post_break,
            "subquery_manual_encuentra": encontrado.get("new_id"),
            "subquery_firma_calc": encontrado.get("firma_calc"),
            "huerfano_no_existe": ne.get("no_existe"),
        })
    finally:
        if test_id:
            try:
                _db.execute("DELETE FROM scintela.banco_conciliacion_match WHERE id = %s", (test_id,))
            except Exception:
                pass


@bp.route("/probe-firmas", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def probe_firmas():
    """Compara firmas: la guardada en match vs la que computamos de tx ahora."""
    no_banco = _BANCO_PICHINCHA
    pcs = _db.fetch_all(
        """
        SELECT t.id_transaccion,
               COALESCE(t.fecha::TEXT, '') || '|'
            || COALESCE(t.documento, '') || '|'
            || COALESCE(t.importe::TEXT, '0') || '|'
            || COALESCE(t.numreferencia::TEXT, '') || '|'
            || COALESCE(LEFT(t.concepto, 40), '') AS firma_calc
          FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = %s AND t.fecha >= '2026-06-01'
           AND COALESCE(TRIM(t.stat), '') <> '*'
         ORDER BY t.id_transaccion ASC LIMIT 3
        """,
        (no_banco,),
    ) or []
    if len(pcs) < 1:
        return jsonify({"ok": False, "note": "sin PC pendientes para probar"})
    # Create one match, capture firma, compare
    test_id = None
    try:
        with _db.tx() as conn:
            _db.execute(
                """
                INSERT INTO scintela.banco_conciliacion_match
                    (no_banco, estado, id_transaccion, usuario)
                VALUES (%s, 'matched', %s, 'probe-firmas')
                """,
                (no_banco, pcs[0]["id_transaccion"]),
                conn=conn,
            )
            m = _db.fetch_one(
                """
                SELECT id, id_transaccion, tx_firma
                  FROM scintela.banco_conciliacion_match
                 WHERE no_banco = %s AND usuario = 'probe-firmas'
                 ORDER BY id DESC LIMIT 1
                """,
                (no_banco,),
                conn=conn,
            ) or {}
            test_id = m.get("id")
        result = {
            "pc_id": pcs[0]["id_transaccion"],
            "pc_firma_calc": pcs[0]["firma_calc"],
            "match_firma_stored": m.get("tx_firma"),
            "iguales": pcs[0]["firma_calc"] == m.get("tx_firma"),
        }
    finally:
        if test_id:
            try:
                _db.execute(
                    "DELETE FROM scintela.banco_conciliacion_match WHERE id = %s",
                    (test_id,),
                )
            except Exception:
                pass
    return jsonify(result)


@bp.route("/test-relink-full", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def test_relink_full():
    """Test integral: crea 3 matches fake, captura ids, rompe ids,
    relinkea, verifica recovery, cleanup. Devuelve OK/FAIL."""
    no_banco = _BANCO_PICHINCHA
    pcs = _db.fetch_all(
        """
        SELECT id_transaccion FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND fecha >= '2026-06-01'
           AND COALESCE(TRIM(stat), '') <> '*'
         ORDER BY id_transaccion ASC LIMIT 3
        """,
        (no_banco,),
    ) or []
    if len(pcs) < 3:
        return jsonify({"ok": False, "note": "menos de 3 PC pendientes para usar"})

    test_ids = []
    try:
        # 1) Create 3 fake matches via direct INSERT con tx_firma vía SQL helper
        with _db.tx() as conn:
            for p in pcs:
                res = _db.execute(
                    """
                    INSERT INTO scintela.banco_conciliacion_match
                        (no_banco, estado, id_transaccion, tx_firma, usuario)
                    VALUES (%s, 'matched', %s,
                            scintela.compute_tx_firma(%s), 'test-stress')
                    RETURNING id
                    """,
                    (no_banco, p["id_transaccion"], p["id_transaccion"]),
                    conn=conn,
                )
                # res is int (rows affected). We need the returned id.
            # Capture the matches we just created
            new_matches = _db.fetch_all(
                """
                SELECT id, id_transaccion, tx_firma
                  FROM scintela.banco_conciliacion_match
                 WHERE no_banco = %s AND usuario = 'test-stress'
                 ORDER BY id DESC LIMIT 3
                """,
                (no_banco,),
                conn=conn,
            ) or []
            test_ids = [m["id"] for m in new_matches]

        # Verify firma was populated by trigger
        n_sin_firma = sum(1 for m in new_matches if not m.get("tx_firma"))
        if n_sin_firma:
            # cleanup and return
            _db.execute(
                "DELETE FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)",
                (test_ids,),
            )
            return jsonify({"ok": False, "test": "trigger_firma", "n_sin_firma": n_sin_firma})

        # 2) Break id_transaccion (simulate sync that lost rows)
        max_id = _db.fetch_one(
            "SELECT MAX(id_transaccion) AS m FROM scintela.transacciones_bancarias WHERE no_banco = %s",
            (no_banco,),
        ) or {}
        huerfano = int(max_id.get("m") or 0) + 999999
        originals = {m["id"]: m["id_transaccion"] for m in new_matches}
        _db.execute(
            "UPDATE scintela.banco_conciliacion_match SET id_transaccion = %s WHERE id = ANY(%s)",
            (huerfano, test_ids),
        )

        # 3) Run relink
        rel = _db.fetch_one(
            "SELECT * FROM scintela.relink_matches_post_sync(%s)",
            (no_banco,),
        ) or {}

        # 4) Verify recovery
        recovered = _db.fetch_all(
            "SELECT id, id_transaccion FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)",
            (test_ids,),
        ) or []
        rec_map = {r["id"]: r["id_transaccion"] for r in recovered}
        ok_count = sum(1 for mid, orig in originals.items() if rec_map.get(mid) == orig)

        result = {
            "ok": ok_count == len(originals),
            "n_created": len(test_ids),
            "n_recovered": ok_count,
            "relink_reported": int(rel.get("relinked") or 0),
            "originals": originals,
            "recovered": rec_map,
        }
    finally:
        # 5) Cleanup
        if test_ids:
            try:
                _db.execute(
                    "DELETE FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)",
                    (test_ids,),
                )
            except Exception:
                pass
    return jsonify(result)


def _relink_py(no_banco: int) -> dict:
    """Relink en Python (no pl/pgsql que misteriosamente no persiste UPDATE).
    UPDATE plain via correlated subquery. Devuelve counts."""
    total = _db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match WHERE no_banco = %s AND deshecho_en IS NULL",
        (no_banco,),
    ) or {}
    sin_firma = _db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s AND deshecho_en IS NULL
           AND tx_firma IS NULL AND id_transaccion IS NOT NULL
        """,
        (no_banco,),
    ) or {}
    # TMT 2026-07-15 (dueña + Alex — bug "PROGRAMA 0.00 / algo raro"): ANTES este
    # UPDATE hacía `SET id_transaccion = (subconsulta)`. Cuando la subconsulta NO
    # encontraba el mov nuevo por tx_firma devolvía NULL → y el UPDATE ponía
    # id_transaccion = NULL, BORRANDO la contraparte del programa de un match que
    # antes cuadraba (DIFF=0). Por eso reaparecían conciliaciones "a medias" con
    # PROGRAMA 0.00 tras cada sync. FIX: solo re-atar cuando SÍ se encuentra el
    # mov nuevo (sub.new_id IS NOT NULL). Si no se encuentra, se DEJA el match
    # como estaba (queda huérfano, recuperable en el próximo sync) — NUNCA se
    # pisa con NULL. Ver /auditar-sin-programa (sección huerfanos).
    n_relinked = _db.execute(
        """
        UPDATE scintela.banco_conciliacion_match m
           SET id_transaccion = sub.new_id
          FROM (
            SELECT m2.id AS mid,
                   (SELECT t.id_transaccion
                      FROM scintela.transacciones_bancarias t
                     WHERE t.no_banco = m2.no_banco
                       AND (COALESCE(t.fecha::TEXT, '') || '|'
                         || COALESCE(t.documento, '') || '|'
                         || COALESCE(t.importe::TEXT, '0') || '|'
                         || COALESCE(t.numreferencia::TEXT, '') || '|'
                         || COALESCE(LEFT(t.concepto, 40), '')) = m2.tx_firma
                     ORDER BY t.id_transaccion ASC LIMIT 1) AS new_id
              FROM scintela.banco_conciliacion_match m2
             WHERE m2.no_banco = %s
               AND m2.deshecho_en IS NULL
               AND m2.tx_firma IS NOT NULL
               AND m2.id_transaccion IS NOT NULL
               AND NOT EXISTS (
                 SELECT 1 FROM scintela.transacciones_bancarias t2
                  WHERE t2.id_transaccion = m2.id_transaccion
               )
          ) sub
         WHERE m.id = sub.mid
           AND sub.new_id IS NOT NULL
        """,
        (no_banco,),
    ) or 0
    sin_match = _db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match m
         WHERE m.no_banco = %s AND m.deshecho_en IS NULL
           AND m.id_transaccion IS NOT NULL
           AND NOT EXISTS (
             SELECT 1 FROM scintela.transacciones_bancarias t
              WHERE t.id_transaccion = m.id_transaccion
           )
        """,
        (no_banco,),
    ) or {}
    # TMT 2026-06-03 dueña: 'lo unico conciliado sea el * del dbase'.
    # NO re-aplicar stat='*' después del sync. Si dBase no lo trae '*',
    # la fila vuelve a aparecer en pendientes y la dueña puede re-matchear.
    # El match record persiste como historial, pero no fuerza stat.
    return {
        "matches_total": int(total.get("n") or 0),
        "relinked": int(n_relinked or 0),
        "sin_firma": int(sin_firma.get("n") or 0),
        "sin_match": int(sin_match.get("n") or 0),
    }


@bp.route("/revert-my-test-stats", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def revert_my_test_stats():
    """Revierte stat='*' que dejé por accidente en mis tests anteriores
    (el viejo relink re-aplicaba stat). Solo rows sin match activo."""
    no_banco = _BANCO_PICHINCHA
    test_ids = [23537, 23538, 23541, 23542, 23543, 23544]
    rows_pre = _db.fetch_all(
        """
        SELECT id_transaccion, stat, documento, importe
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND id_transaccion = ANY(%s)
           AND TRIM(COALESCE(stat,'')) = '*'
           AND NOT EXISTS (
             SELECT 1 FROM scintela.banco_conciliacion_match m
              WHERE m.id_transaccion = scintela.transacciones_bancarias.id_transaccion
                AND m.deshecho_en IS NULL
           )
        """,
        (no_banco, test_ids),
    ) or []
    n = _db.execute(
        """
        UPDATE scintela.transacciones_bancarias
           SET stat = NULL
         WHERE no_banco = %s AND id_transaccion = ANY(%s)
           AND TRIM(COALESCE(stat,'')) = '*'
           AND NOT EXISTS (
             SELECT 1 FROM scintela.banco_conciliacion_match m
              WHERE m.id_transaccion = scintela.transacciones_bancarias.id_transaccion
                AND m.deshecho_en IS NULL
           )
        """,
        (no_banco, test_ids),
    ) or 0
    return jsonify({"ok": True, "reverted": n, "rows_pre": [
        {"id": r["id_transaccion"], "doc": r["documento"], "importe": float(r["importe"] or 0)} for r in rows_pre
    ]})


@bp.route("/dump-matches-batch", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def dump_matches_batch():
    rows = _db.fetch_all(
        """
        SELECT id, confirm_batch_id, creado_en::TEXT AS creado, real_monto, real_documento
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s AND deshecho_en IS NULL
         ORDER BY id
        """,
        (_BANCO_PICHINCHA,),
    ) or []
    return jsonify({"rows": [
        {"id": r["id"], "batch": r["confirm_batch_id"], "creado": r["creado"],
         "monto": float(r["real_monto"]) if r["real_monto"] is not None else None,
         "doc": r["real_documento"]}
        for r in rows
    ]})


@bp.route("/backfill-batch-by-second", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def backfill_batch_by_second():
    """Asigna batch_id a matches viejos agrupándolos por (no_banco, usuario,
    truncado al segundo). Útil cuando mig 0072 no se aplicó automáticamente."""
    n = _db.execute(
        """
        WITH grupos AS (
            SELECT id,
                   'legacy_' || no_banco || '_' || usuario || '_' ||
                     to_char(date_trunc('second', creado_en), 'YYYYMMDDHH24MISS') AS bid
              FROM scintela.banco_conciliacion_match
             WHERE confirm_batch_id IS NULL
        )
        UPDATE scintela.banco_conciliacion_match m
           SET confirm_batch_id = g.bid
          FROM grupos g
         WHERE m.id = g.id
        """,
    )
    return jsonify({"ok": True, "filas_actualizadas": int(n or 0)})


@bp.route("/find-drift-source", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def find_drift_source():
    """Lista las txs PC con stat='*' en 06-01+ que NO tienen contraparte
    en el extracto sesión actual (= drift fuente)."""
    no_banco = _BANCO_PICHINCHA
    # Get extracto firmas
    sess = _db.fetch_one(
        "SELECT extracto_payload FROM scintela.banco_conciliacion_sesion WHERE no_banco=%s AND cerrada_en IS NULL ORDER BY abierta_en DESC LIMIT 1",
        (no_banco,),
    ) or {}
    payload = sess.get("extracto_payload") or []
    if isinstance(payload, str):
        try: payload = json.loads(payload)
        except Exception: payload = []
    if isinstance(payload, dict):
        payload = payload.get("extracto") or payload.get("movs") or []
    extr_keys = set()
    for m in (payload or []):
        if not isinstance(m, dict): continue
        f = str(m.get("fecha") or "")
        try: amt = round(float(m.get("monto") or m.get("importe") or 0), 2)
        except Exception: amt = 0
        extr_keys.add((f, amt))

    # Get PC stat='*' rows in 06-01+
    rows = _db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, importe, concepto, stat, usuario_crea
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND fecha >= '2026-06-01'
           AND TRIM(COALESCE(stat,'')) = '*'
         ORDER BY fecha, id_transaccion
        """,
        (no_banco,),
    ) or []

    sin_extracto = []
    for r in rows:
        f = str(r.get("fecha") or "")
        try: amt = round(float(r.get("importe") or 0), 2)
        except Exception: amt = 0
        if (f, amt) not in extr_keys:
            sin_extracto.append({
                "id": r["id_transaccion"], "fecha": f, "doc": r.get("documento"),
                "importe": amt, "concepto": (r.get("concepto") or "")[:60],
            })

    # Suma signed (PC convention)
    sum_signed = 0
    for r in sin_extracto:
        doc = r["doc"] or ""
        sign = 1 if doc in ("DE","TR","NC","IN","AC","XX") else -1
        sum_signed += sign * r["importe"]

    return jsonify({
        "n_pc_stat_star_06_01plus": len(rows),
        "n_extracto_payload": len(payload or []),
        "n_sin_match_en_extracto": len(sin_extracto),
        "sum_signed_drift_potencial": round(sum_signed, 2),
        "sample": sin_extracto[:30],
    })


@bp.route("/sync-matches-counter", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def sync_matches_counter():
    """One-shot: alinear sesion.matches_hechos con la realidad."""
    no_banco = _BANCO_PICHINCHA
    n = _db.execute(
        """
        UPDATE scintela.banco_conciliacion_sesion s
           SET matches_hechos = (
             SELECT COUNT(*) FROM scintela.banco_conciliacion_match m
              WHERE m.no_banco = s.no_banco AND m.deshecho_en IS NULL
           )
         WHERE s.no_banco = %s AND s.cerrada_en IS NULL
        """,
        (no_banco,),
    )
    return jsonify({"ok": True, "rows_updated": n})


@bp.route("/test-relink-py", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def test_relink_py():
    """Test del relink Python (no SQL function)."""
    no_banco = _BANCO_PICHINCHA
    pcs = _db.fetch_all(
        """
        SELECT id_transaccion FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND fecha >= '2026-06-01'
           AND COALESCE(TRIM(stat), '') <> '*'
         ORDER BY id_transaccion ASC LIMIT 3
        """,
        (no_banco,),
    ) or []
    test_ids = []
    try:
        for p in pcs:
            _db.execute(
                """
                INSERT INTO scintela.banco_conciliacion_match
                    (no_banco, estado, id_transaccion, tx_firma, usuario)
                VALUES (%s, 'matched', %s, scintela.compute_tx_firma(%s), 'py-test')
                """,
                (no_banco, p["id_transaccion"], p["id_transaccion"]),
            )
        new_m = _db.fetch_all(
            "SELECT id, id_transaccion FROM scintela.banco_conciliacion_match WHERE no_banco=%s AND usuario='py-test' ORDER BY id DESC LIMIT 3",
            (no_banco,),
        ) or []
        test_ids = [m["id"] for m in new_m]
        originals = {m["id"]: m["id_transaccion"] for m in new_m}
        # Break
        max_id = _db.fetch_one("SELECT MAX(id_transaccion) AS m FROM scintela.transacciones_bancarias WHERE no_banco=%s", (no_banco,)) or {}
        huerfano = int(max_id.get("m") or 0) + 999999
        _db.execute("UPDATE scintela.banco_conciliacion_match SET id_transaccion = %s WHERE id = ANY(%s)", (huerfano, test_ids))
        # Relink via Python
        relink_result = _relink_py(no_banco)
        # Check
        recovered = _db.fetch_all("SELECT id, id_transaccion FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)", (test_ids,)) or []
        rec_map = {r["id"]: r["id_transaccion"] for r in recovered}
        ok_count = sum(1 for mid, orig in originals.items() if rec_map.get(mid) == orig)
        return jsonify({
            "ok": ok_count == len(originals),
            "n_created": len(test_ids),
            "n_recovered": ok_count,
            "relink": relink_result,
            "originals": originals,
            "recovered": rec_map,
        })
    finally:
        if test_ids:
            try:
                _db.execute("DELETE FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)", (test_ids,))
            except Exception:
                pass


@bp.route("/test-relink", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def test_relink():
    """Test endpoint: invoca relink_matches_post_sync sin haber sincronizado.
    Debe devolver 0 relinked si todo está consistente."""
    r = _db.fetch_one(
        "SELECT * FROM scintela.relink_matches_post_sync(%s)",
        (_BANCO_PICHINCHA,),
    ) or {}
    return jsonify({
        "matches_total": int(r.get("matches_total") or 0),
        "relinked": int(r.get("relinked") or 0),
        "sin_firma": int(r.get("sin_firma") or 0),
        "sin_match": int(r.get("sin_match") or 0),
    })


@bp.route("/test-fake-sync", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def test_fake_sync():
    """Test destructivo: simula un sync rompiendo id_transaccion de matches.
    1) Captura ids actuales de matches activos.
    2) Modifica id_transaccion a -1 (huérfano artificial) preservando firma.
    3) Llama relink → debe recuperar todos.
    4) Reporta antes/después.

    SOLO ejecutar si querés stress-test del relink."""
    no_banco = _BANCO_PICHINCHA
    # Capture
    snapshot = _db.fetch_all(
        """
        SELECT id, id_transaccion, tx_firma
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s AND deshecho_en IS NULL
           AND id_transaccion IS NOT NULL
        """,
        (no_banco,),
    ) or []
    n_pre = len(snapshot)
    if n_pre == 0:
        return jsonify({"ok": True, "n_pre": 0, "note": "no hay matches activos, no se testea"})

    # Save originals & break them (set id_transaccion to a value that doesn't exist)
    max_id_row = _db.fetch_one(
        "SELECT MAX(id_transaccion) AS m FROM scintela.transacciones_bancarias WHERE no_banco = %s",
        (no_banco,),
    ) or {}
    huerfano_id = int(max_id_row.get("m") or 0) + 999999

    originals = {row["id"]: row["id_transaccion"] for row in snapshot}
    try:
        with _db.tx() as conn:
            for mid, orig_tx in originals.items():
                _db.execute(
                    "UPDATE scintela.banco_conciliacion_match SET id_transaccion = %s WHERE id = %s",
                    (huerfano_id, mid),
                    conn=conn,
                )
        # Now relink
        r = _db.fetch_one(
            "SELECT * FROM scintela.relink_matches_post_sync(%s)",
            (no_banco,),
        ) or {}
        # Check recovery
        recovered = _db.fetch_all(
            "SELECT id, id_transaccion FROM scintela.banco_conciliacion_match WHERE id = ANY(%s)",
            (list(originals.keys()),),
        ) or []
        recovered_map = {row["id"]: row["id_transaccion"] for row in recovered}
        ok_recovery = sum(1 for mid, orig in originals.items() if recovered_map.get(mid) == orig)
        bad_recovery = sum(1 for mid, orig in originals.items() if recovered_map.get(mid) != orig)
        return jsonify({
            "ok": bad_recovery == 0,
            "n_pre": n_pre,
            "relinked_reported": int(r.get("relinked") or 0),
            "ok_recovery": ok_recovery,
            "bad_recovery": bad_recovery,
        })
    except Exception as e:
        # Restore
        try:
            with _db.tx() as conn:
                for mid, orig_tx in originals.items():
                    _db.execute(
                        "UPDATE scintela.banco_conciliacion_match SET id_transaccion = %s WHERE id = %s",
                        (orig_tx, mid),
                        conn=conn,
                    )
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)[:300]})


@bp.route("/stress", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def stress():
    """Stress test: corre múltiples probes contra el modelo de conciliación
    y reporta inconsistencias. Read-only."""
    from modules.conciliacion import balance_pichincha as _bp
    no_banco = _BANCO_PICHINCHA
    findings = []

    # 1) Invariance check: bank_predicted DEBE = libros - pend_pc_neto + pend_banco_total_neto
    b = _bp.calcular(no_banco)
    libros = float(b.get("saldo") or 0)
    pend_pc_neto = float(b.get("pendientes_conciliar_neto") or 0)
    pend_banco_neto_total = float(b.get("neto_pendientes_total") or b.get("neto_pendientes") or 0)
    esperado_actual = float(b.get("saldo_banco_esperado") or 0)
    esperado_calc = round(libros - pend_pc_neto + pend_banco_neto_total, 2)
    if abs(esperado_calc - esperado_actual) > 0.01:
        findings.append({
            "test": "invariance_math",
            "ok": False,
            "esperado_actual": esperado_actual,
            "esperado_calc": esperado_calc,
            "diff": esperado_actual - esperado_calc,
        })
    else:
        findings.append({"test": "invariance_math", "ok": True})

    # 2) Counts consistency: pend_pc cred + deb count must equal total pendientes count
    n_pc_total = int(b.get("n_pendientes_conciliar") or 0)
    n_pc_split = int(b.get("n_pendientes_pc_cred") or 0) + int(b.get("n_pendientes_pc_deb") or 0)
    findings.append({
        "test": "pc_count_split",
        "ok": n_pc_total == n_pc_split,
        "total": n_pc_total, "split_sum": n_pc_split,
    })

    # 3) Counts consistency banco
    n_banco_total = int(b.get("n_pendientes_banco_total") or 0)
    n_banco_split = int(b.get("n_pendientes_banco_cred") or 0) + int(b.get("n_pendientes_banco_deb") or 0) + int(b.get("n_pendientes_banco_extracto") or 0)
    findings.append({
        "test": "banco_count_total",
        "ok": n_banco_total == n_banco_split,
        "total": n_banco_total, "split_sum": n_banco_split,
    })

    # 4) Matches dead: matches con id_transaccion apuntando a fila inexistente
    dead = _db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match m
         WHERE m.no_banco = %s AND m.deshecho_en IS NULL
           AND m.id_transaccion IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM scintela.transacciones_bancarias t
                WHERE t.id_transaccion = m.id_transaccion
           )
        """,
        (no_banco,),
    ) or {}
    findings.append({
        "test": "matches_id_orphan",
        "ok": int(dead.get("n") or 0) == 0,
        "n": int(dead.get("n") or 0),
    })

    # 5) Matches sin tx_firma (no podrán sobrevivir un sync)
    sin_firma = _db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match m
         WHERE m.no_banco = %s AND m.deshecho_en IS NULL
           AND m.tx_firma IS NULL AND m.id_transaccion IS NOT NULL
        """,
        (no_banco,),
    ) or {}
    findings.append({
        "test": "matches_sin_firma",
        "ok": int(sin_firma.get("n") or 0) == 0,
        "n": int(sin_firma.get("n") or 0),
    })

    # 6) stat='*' orfans: filas con stat='*' sin match activo Y sin estar en histos
    stat_orf = _db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = %s AND TRIM(COALESCE(t.stat,'')) = '*'
           AND NOT EXISTS (
               SELECT 1 FROM scintela.banco_conciliacion_match m
                WHERE m.id_transaccion = t.id_transaccion AND m.deshecho_en IS NULL
           )
           AND t.usuario_crea = 'dbf-import'
        """,
        (no_banco,),
    ) or {}
    findings.append({
        "test": "stat_orfans_no_dbf",
        "ok": True,  # ok=True porque dbf-import naturalmente trae stat='*'; este es informativo
        "n": int(stat_orf.get("n") or 0),
        "note": "stat='*' venidos de dbf-import son legítimos (concilados en dBase)",
    })

    # 7) Sesión.matches_hechos vs matches reales en DB
    sm = _db.fetch_one(
        """
        SELECT s.id AS sesion_id, s.matches_hechos AS contador,
               (SELECT COUNT(*) FROM scintela.banco_conciliacion_match m
                 WHERE m.no_banco = s.no_banco AND m.deshecho_en IS NULL) AS reales
          FROM scintela.banco_conciliacion_sesion s
         WHERE s.no_banco = %s AND s.cerrada_en IS NULL
         ORDER BY s.abierta_en DESC LIMIT 1
        """,
        (no_banco,),
    ) or {}
    findings.append({
        "test": "sesion_matches_hechos_consistency",
        "ok": int(sm.get("contador") or 0) == int(sm.get("reales") or 0),
        "contador_sesion": int(sm.get("contador") or 0),
        "matches_reales": int(sm.get("reales") or 0),
        "note": "contador 'matches hechos' del header no decrementa al borrar",
    })

    # 8) Extracto sesión sin extracto_payload válido
    sx = _db.fetch_one(
        """
        SELECT id, jsonb_typeof(extracto_payload) AS tipo,
               CASE
                 WHEN jsonb_typeof(extracto_payload) = 'array'
                   THEN jsonb_array_length(extracto_payload)
                 ELSE 0 END AS n
          FROM scintela.banco_conciliacion_sesion
         WHERE no_banco = %s AND cerrada_en IS NULL
         ORDER BY abierta_en DESC LIMIT 1
        """,
        (no_banco,),
    ) or {}
    findings.append({
        "test": "sesion_extracto_payload",
        "ok": int(sx.get("n") or 0) > 0,
        "n": int(sx.get("n") or 0),
        "tipo": sx.get("tipo"),
    })

    # 9) Histos duplicados (firma exacta)
    dup_h = _db.fetch_one(
        """
        SELECT COUNT(*) - COUNT(DISTINCT (fecha, documento, tipo, monto)) AS dups
          FROM scintela.banco_historicos_pendientes
         WHERE no_banco = %s AND conciliado_en IS NULL
        """,
        (no_banco,),
    ) or {}
    findings.append({
        "test": "histos_dup_estrictos",
        "ok": int(dup_h.get("dups") or 0) == 0,
        "n_dups": int(dup_h.get("dups") or 0),
    })

    # 10) Resumen
    n_fail = sum(1 for f in findings if not f.get("ok"))
    return jsonify({
        "ok": n_fail == 0,
        "n_findings": len(findings),
        "n_fail": n_fail,
        "findings": findings,
    })


@bp.route("/dump-balance", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def dump_balance():
    """Dump del dict balance que produce balance_pichincha.calcular()."""
    from modules.conciliacion import balance_pichincha as _bp
    b = _bp.calcular(_BANCO_PICHINCHA)
    return jsonify({
        k: (str(v) if hasattr(v, 'isoformat') else v)
        for k, v in b.items()
        if k not in ("pendientes_conciliar_rows",)
    })


@bp.route("/dump-todo", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def dump_todo():
    """Dump COMPLETO: sesión payload extracto + transacciones_bancarias 06-01+
    + histos pendientes + matches activos. Para investigar diff residual."""
    no_banco = _BANCO_PICHINCHA

    # 1) Sesión abierta + payload
    sesion = _db.fetch_one(
        """
        SELECT id, no_banco, extracto_payload
          FROM scintela.banco_conciliacion_sesion
         WHERE no_banco = %s AND cerrada_en IS NULL
         ORDER BY abierta_en DESC LIMIT 1
        """,
        (no_banco,),
    )
    extracto = []
    if sesion and sesion.get("extracto_payload"):
        p = sesion["extracto_payload"]
        if isinstance(p, str):
            try: p = json.loads(p)
            except Exception: p = {}
        if isinstance(p, list):
            extracto = p
        else:
            extracto = p.get("extracto") or p.get("movs") or []

    # 2) Matches activos
    matches = _db.fetch_all(
        """
        SELECT id, id_transaccion, real_documento, real_monto, real_fecha,
               real_tipo, real_concepto, estado
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s
         ORDER BY id
        """,
        (no_banco,),
    ) or []

    # 3) Transacciones 06-01+
    txs = _db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, importe, concepto, stat
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND fecha >= '2026-06-01'
         ORDER BY fecha, id_transaccion
        """,
        (no_banco,),
    ) or []

    # 4) Histos pendientes (no FEB2023 separados)
    histos_all = _db.fetch_all(
        """
        SELECT id, fecha, documento, monto, tipo, fuente, conciliado_en
          FROM scintela.banco_historicos_pendientes
         WHERE no_banco = %s AND conciliado_en IS NULL
         ORDER BY fecha
        """,
        (no_banco,),
    ) or []

    # Aggregations
    def _sum_signed_pc(rows):
        cred = 0; deb = 0
        for r in rows:
            doc = r.get("documento") or ""
            imp = float(r.get("importe") or r.get("monto") or 0)
            if doc in ("DE","TR","NC","IN","AC","XX"):
                cred += imp
            else:
                deb += imp
        return {"cred": round(cred,2), "deb": round(deb,2)}

    def _sum_extracto(rows):
        cred = 0; deb = 0
        for r in rows:
            t = (r.get("tipo") or r.get("clase") or "").upper()
            imp = float(r.get("monto") or r.get("importe") or 0)
            if t in ("C","CRED","CREDITO","CREDITOS"):
                cred += abs(imp)
            elif t in ("D","DEB","DEBITO","DEBITOS"):
                deb += abs(imp)
            elif imp > 0:
                cred += imp
            else:
                deb += abs(imp)
        return {"cred": round(cred,2), "deb": round(deb,2)}

    txs_pend = [t for t in txs if (t.get("stat") or "") != "*"]
    txs_concil = [t for t in txs if (t.get("stat") or "") == "*"]

    # Match-back: rows del extracto con coincidencia en matches por (fecha, monto, tipo)
    extracto_pend = []
    extracto_match = []
    matched_keys = set()
    for m in matches:
        if m.get("real_monto") is not None:
            matched_keys.add((str(m.get("real_fecha")), float(m.get("real_monto") or 0), m.get("real_documento")))

    for r in extracto:
        key = (str(r.get("fecha")), float(r.get("monto") or r.get("importe") or 0), r.get("tipo") or r.get("documento"))
        if key in matched_keys:
            extracto_match.append(r)
        else:
            extracto_pend.append(r)

    return jsonify({
        "ok": True,
        "sesion": {"id": sesion.get("id") if sesion else None, "extracto_n": len(extracto)},
        "extracto_pend": {"n": len(extracto_pend), "sum": _sum_extracto(extracto_pend)},
        "extracto_match": {"n": len(extracto_match), "sum": _sum_extracto(extracto_match)},
        "txs_06_01plus": {
            "total": len(txs),
            "pendientes": {"n": len(txs_pend), "sum": _sum_signed_pc(txs_pend)},
            "conciliados": {"n": len(txs_concil), "sum": _sum_signed_pc(txs_concil)},
        },
        "matches_activos": len(matches),
        "histos_pend": {"n": len(histos_all), "sum": _sum_signed_pc(histos_all)},
        "sample_extracto_pend": extracto_pend[:20],
        "sample_extracto_match": extracto_match[:10],
    })


@bp.route("/inspect-recent", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def inspect_recent():
    """Lista todas las txs recientes (06-01 y 06-02) con detalle."""
    no_banco = _BANCO_PICHINCHA
    rows = _db.fetch_all(
        """
        SELECT t.id_transaccion, t.fecha, t.documento, t.importe, t.concepto,
               t.numreferencia, t.stat, t.usuario_crea, t.no_cta
          FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = %s
           AND t.fecha >= '2026-06-01'
         ORDER BY t.fecha, t.documento, t.id_transaccion
        """,
        (no_banco,),
    ) or []
    return jsonify({
        "ok": True,
        "n": len(rows),
        "rows": [
            {
                "id": r["id_transaccion"],
                "fecha": str(r["fecha"]) if r.get("fecha") else None,
                "doc": r.get("documento"),
                "importe": float(r.get("importe") or 0),
                "concepto": (r.get("concepto") or "")[:80],
                "numref": r.get("numreferencia"),
                "stat": r.get("stat"),
                "usuario": r.get("usuario_crea"),
            }
            for r in rows
        ]
    })


@bp.route("/cuadre-saldos", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def cuadre_saldos():
    """Desglose detallado de saldos PC vs Banco para encontrar la diferencia."""
    no_banco = _BANCO_PICHINCHA
    out = {"ok": True, "no_banco": no_banco}

    # 1) Saldo libros PC (último mov).
    try:
        row = _db.fetch_one(
            """
            SELECT saldo, fecha, id_transaccion, documento, importe, concepto
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s AND saldo IS NOT NULL
             ORDER BY fecha DESC, id_transaccion DESC LIMIT 1
            """,
            (no_banco,),
        )
        out["libros_ultimo"] = dict(row) if row else None
        if row:
            out["libros_ultimo"]["fecha"] = str(row["fecha"]) if row.get("fecha") else None
            out["libros_ultimo"]["saldo"] = float(row["saldo"] or 0)
            out["libros_ultimo"]["importe"] = float(row["importe"] or 0)
    except Exception as e:
        out["error_libros"] = str(e)

    # 2) Suma TXs 06-02 por tipo.
    try:
        rows = _db.fetch_all(
            """
            SELECT documento,
                   CASE WHEN documento IN ('DE','TR','NC','IN','AC','XX') THEN 'CRED'
                        WHEN documento IN ('CH','ND','DB','GS','PA') THEN 'DEB'
                        ELSE 'OTRO' END AS clase,
                   COUNT(*) AS n,
                   COALESCE(SUM(importe), 0) AS suma,
                   COALESCE(SUM(CASE WHEN TRIM(COALESCE(stat,'')) = '*' THEN importe ELSE 0 END), 0) AS suma_conciliada
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s AND fecha >= '2026-06-01'
             GROUP BY documento
             ORDER BY clase, documento
            """,
            (no_banco,),
        ) or []
        out["txs_recientes"] = [
            {"doc": r["documento"], "clase": r["clase"], "n": int(r["n"]),
             "suma": float(r["suma"] or 0), "conciliada": float(r["suma_conciliada"] or 0)}
            for r in rows
        ]
    except Exception as e:
        out["error_txs"] = str(e)

    # 3) Counters reales.
    try:
        row = _db.fetch_one(
            """
            SELECT
              (SELECT COUNT(*) FROM scintela.transacciones_bancarias
                WHERE no_banco = %s AND TRIM(COALESCE(stat,'')) = '*') AS pc_conciliadas,
              (SELECT COUNT(*) FROM scintela.transacciones_bancarias
                WHERE no_banco = %s AND TRIM(COALESCE(stat,'')) <> '*') AS pc_pendientes,
              (SELECT COUNT(*) FROM scintela.banco_historicos_pendientes
                WHERE no_banco = %s AND conciliado_en IS NULL) AS histos_pend,
              (SELECT COUNT(*) FROM scintela.banco_conciliacion_match
                WHERE no_banco = %s AND deshecho_en IS NULL) AS matches_activos
            """,
            (no_banco, no_banco, no_banco, no_banco),
        )
        if row:
            out["counts"] = {k: int(v) for k, v in row.items()}
    except Exception as e:
        out["error_counts"] = str(e)

    # 4) Saldo de pendientes PC (lo que falta sumar/restar al libros para conciliar).
    try:
        row = _db.fetch_one(
            """
            SELECT
              COALESCE(SUM(CASE WHEN documento IN ('DE','TR','NC','IN','AC','XX') THEN importe ELSE 0 END), 0) AS pend_pc_cred,
              COALESCE(SUM(CASE WHEN documento IN ('CH','ND','DB','GS','PA') THEN importe ELSE 0 END), 0) AS pend_pc_deb
              FROM scintela.transacciones_bancarias t
             WHERE t.no_banco = %s
               AND TRIM(COALESCE(t.stat, '')) <> '*'
               AND NOT EXISTS (SELECT 1 FROM scintela.banco_conciliacion_match m
                                WHERE m.id_transaccion = t.id_transaccion AND m.deshecho_en IS NULL)
            """,
            (no_banco,),
        )
        out["pend_pc"] = {
            "cred": float(row["pend_pc_cred"] or 0),
            "deb": float(row["pend_pc_deb"] or 0),
            "neto": float(row["pend_pc_cred"] or 0) - float(row["pend_pc_deb"] or 0),
        }
    except Exception as e:
        out["error_pend_pc"] = str(e)

    # 5) Suma pendientes banco (histos + extracto sin matchear).
    try:
        row = _db.fetch_one(
            """
            SELECT
              COALESCE(SUM(CASE WHEN tipo='C' THEN monto ELSE 0 END), 0) AS hist_cred,
              COALESCE(SUM(CASE WHEN tipo='D' THEN monto ELSE 0 END), 0) AS hist_deb,
              COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s AND conciliado_en IS NULL
            """,
            (no_banco,),
        )
        out["pend_histos"] = {
            "cred": float(row["hist_cred"] or 0),
            "deb": float(row["hist_deb"] or 0),
            "n": int(row["n"] or 0),
        }
    except Exception as e:
        out["error_pend_histos"] = str(e)

    return jsonify(out)


@bp.route("/match-potencial", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def match_potencial():
    """Para cada pendiente banco (no conciliado), buscar si existe una
    transacción PC con numreferencia o no_cheque que matchee el documento.

    TMT 2026-06-02 dueña: 'transferencias por numero de doc no encuentra
    ninguna, podes aunque sea buscar si para el pasado hubiera matcheado?'.

    Considera TODO el universo de transacciones_bancarias (incluyendo ya
    conciliadas, para saber si la estrategia PASS 0 tiene fundamento).
    """
    out = {"ok": True, "no_banco": _BANCO_PICHINCHA}

    # 1) Match contra numreferencia y no_cheque (vía chequextransaccion).
    # no_cheque NO está en transacciones_bancarias — vive en scintela.cheque
    # ligado vía chequextransaccion. numreferencia es INTEGER en algunos
    # casos → casteamos a text para comparar con documento.
    try:
        rows = _db.fetch_all(
            """
            WITH pend AS (
                SELECT documento, monto, tipo, fecha
                  FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s
                   AND conciliado_en IS NULL
                   AND documento IS NOT NULL AND documento <> ''
            ),
            txs AS (
                SELECT id_transaccion,
                       CAST(numreferencia AS TEXT) AS numref,
                       documento AS doc_pc, importe, fecha, stat
                  FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s
            ),
            cheques AS (
                SELECT DISTINCT CAST(ch.no_cheque AS TEXT) AS no_cheque,
                                cxt.id_transaccion
                  FROM scintela.cheque ch
                  JOIN scintela.chequextransaccion cxt
                    ON cxt.id_cheque = ch.id_cheque
                 WHERE ch.no_cheque IS NOT NULL
            )
            SELECT
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM txs t WHERE t.numref = pend.documento
                )) AS match_por_numref,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM cheques c WHERE c.no_cheque = pend.documento
                )) AS match_por_no_cheque,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM txs t WHERE t.doc_pc = pend.documento
                )) AS match_por_doc_pc,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM txs t WHERE t.numref = pend.documento
                    UNION
                    SELECT 1 FROM cheques c WHERE c.no_cheque = pend.documento
                )) AS match_cualquiera,
                COUNT(*) AS pendientes_totales
              FROM pend
            """,
            (_BANCO_PICHINCHA, _BANCO_PICHINCHA),
        ) or []
        if rows:
            r = rows[0]
            out["pendientes_totales"] = int(r.get("pendientes_totales") or 0)
            out["match_por_numreferencia"] = int(r.get("match_por_numref") or 0)
            out["match_por_no_cheque"] = int(r.get("match_por_no_cheque") or 0)
            out["match_por_documento_pc"] = int(r.get("match_por_doc_pc") or 0)
            out["match_cualquiera"] = int(r.get("match_cualquiera") or 0)
    except Exception as e:
        out["error_agregado"] = str(e)

    # 2) Top 15 ejemplos concretos de matches potenciales.
    try:
        ejemplos = _db.fetch_all(
            """
            SELECT h.documento, h.monto, h.fecha AS fecha_banco, h.tipo,
                   t.id_transaccion,
                   CAST(t.numreferencia AS TEXT) AS numref_pc,
                   t.fecha AS fecha_pc, t.importe, t.documento AS doc_pc,
                   t.stat,
                   (CASE WHEN TRIM(COALESCE(t.stat,'')) = '*' THEN 'conciliado_dbase'
                         ELSE 'pendiente_pc' END) AS estado_pc
              FROM scintela.banco_historicos_pendientes h
              JOIN scintela.transacciones_bancarias t
                ON t.no_banco = h.no_banco
               AND CAST(t.numreferencia AS TEXT) = h.documento
             WHERE h.no_banco = %s
               AND h.conciliado_en IS NULL
               AND h.documento IS NOT NULL AND h.documento <> ''
             ORDER BY h.fecha DESC, h.documento
             LIMIT 15
            """,
            (_BANCO_PICHINCHA,),
        ) or []
        out["ejemplos_match_potencial"] = [
            {
                "documento": e.get("documento"),
                "monto_banco": float(e.get("monto") or 0),
                "fecha_banco": str(e.get("fecha_banco")) if e.get("fecha_banco") else None,
                "tipo": e.get("tipo"),
                "id_transaccion": e.get("id_transaccion"),
                "numref_pc": e.get("numref_pc"),
                "doc_pc": e.get("doc_pc"),
                "fecha_pc": str(e.get("fecha_pc")) if e.get("fecha_pc") else None,
                "importe_pc": float(e.get("importe") or 0),
                "estado_pc": e.get("estado_pc"),
            }
            for e in ejemplos
        ]
    except Exception as e:
        out["error_ejemplos"] = str(e)

    # 3) Match por (monto, fecha) — fallback más relajado.
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes h
             WHERE h.no_banco = %s
               AND h.conciliado_en IS NULL
               AND EXISTS (
                   SELECT 1 FROM scintela.transacciones_bancarias t
                    WHERE t.no_banco = h.no_banco
                      AND ABS(t.importe) = h.monto
                      AND t.fecha BETWEEN h.fecha - INTERVAL '3 days'
                                       AND h.fecha + INTERVAL '3 days'
               )
            """,
            (_BANCO_PICHINCHA,),
        )
        out["match_por_monto_fecha_3d"] = int(row["n"]) if row else 0
    except Exception as e:
        out["error_monto_fecha"] = str(e)

    return jsonify(out)


@bp.route("/cleanup-sesion", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def cleanup_sesion_payload():
    """Quita del payload de la sesión abierta los documentos que YA están
    en banco_historicos_pendientes (no conciliados).

    TMT 2026-06-02: el dedupe de mig 0062 protege uploads FUTUROS. Esta
    acción es una limpieza one-shot del payload ya cargado, para corregir
    el solapamiento histórico señalado por `docs_en_sesion_y_histos`.

    GET = dry-run (devuelve cuántos se sacarían).
    POST = ejecuta el cleanup (UPDATE del payload jsonb).
    """
    from modules.conciliacion import sesion as _ses

    s = _ses.sesion_abierta(_BANCO_PICHINCHA)
    if not s:
        return jsonify({"ok": False, "error": "no hay sesión abierta"}), 400

    # 1) Firmas EN HISTOS (no usar _firmas_ya_conocidas porque incluye
    # el propio payload de la sesión → cada fila se encontraría como
    # duplicada de sí misma y removeríamos TODO el payload).
    # TMT 2026-06-02 fix crítico: aquí solo queremos histos + matches
    # activos, NO el payload actual.
    sigs_histos: set[tuple] = set()
    try:
        rows = _db.fetch_all(
            """
            SELECT documento, fecha, tipo, monto
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND documento IS NOT NULL AND documento <> ''
               AND conciliado_en IS NULL
            """,
            (_BANCO_PICHINCHA,),
        ) or []
        for r in rows:
            sigs_histos.add(_ses._firma_mov(
                r.get("documento"), "",
                r.get("tipo"), r.get("monto"), r.get("fecha"),
            ))
    except Exception as e:
        return jsonify({"ok": False, "error": f"query histos falló: {e}"}), 500

    # 2) Recorrer payload de la sesión y filtrar por firma.
    movs = _ses.cargar_movs(s)
    keep: list = []
    removed: list[str] = []
    for m in movs:
        if not m.documento:
            keep.append(m)
            continue
        sig = _ses._firma_mov(m.documento, getattr(m, "codigo", ""),
                              m.tipo, m.monto, m.fecha)
        if sig in sigs_histos:
            removed.append(f"{m.documento}/{m.codigo}/{m.tipo}/{m.monto}")
            continue
        keep.append(m)

    result = {
        "ok": True,
        "sesion_id": s["id"],
        "payload_antes": len(movs),
        "payload_despues": len(keep),
        "removidos": len(removed),
        "ejemplos_removidos": removed[:20],
    }

    if request.method == "GET":
        result["modo"] = "dry-run"
        result["nota"] = "POST a este mismo endpoint para ejecutar"
        return jsonify(result)

    # Ejecutar el cleanup — UPDATE del payload jsonb.
    new_payload = json.dumps([_ses._mov_to_dict(m) for m in keep])
    try:
        _db.execute(
            """
            UPDATE scintela.banco_conciliacion_sesion
               SET extracto_payload = %s::jsonb
             WHERE id = %s
            """,
            (new_payload, int(s["id"])),
        )
        result["modo"] = "ejecutado"
    except Exception as e:
        return jsonify({"ok": False, "error": f"update falló: {e}"}), 500

    return jsonify(result)
