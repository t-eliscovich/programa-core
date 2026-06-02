"""Sesión persistente de conciliación bancaria.

TMT 2026-05-28 dueña: 'puede quedar abierta esa pagina hasta no cerrar la
conciliacion?'. Sí — guardamos el extracto parseado en
`scintela.banco_conciliacion_sesion` (migration 0060).

TMT 2026-06-02 dueña: 'no quiero cerrar la sesion, quiero dejar seguir
editando. borremos lo necesario.' La sesión vive para siempre — una por
banco (migration 0062 reemplaza el unique (no_banco, usuario) por
(no_banco)). Cada upload de extracto se MERGEA en la única sesión del
banco, dedupeando por número de documento contra:

    - el payload actual (movs ya cargados en la sesión)
    - banco_historicos_pendientes (filas crudas del banco ya conocidas)
    - banco_conciliacion_match.real_documento (filas ya conciliadas)

Cada vez que la pantalla post-procesar carga, re-corre el matcher contra
los movs no conciliados y arma los buckets:

    - manual:        real_only y bancsis_only para checkbox 1:1 manual.
    - impuestos:     real_only categorizados como COMISION (auto-acoplar).
    - transferencias: matches con razón 'P0' (doc-id exacto).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

import db
from modules.conciliacion.matcher_banco import (
    ConciliacionBanco,
    matchear_extracto_banco,
)
from modules.conciliacion.parser_banco import MovBanco

_LOG = logging.getLogger("programa_core.conciliacion.sesion")


# ─── Serialización MovBanco ↔ JSON ────────────────────────────────────


def _mov_to_dict(m: MovBanco) -> dict:
    return {
        "fecha": m.fecha.isoformat() if m.fecha else None,
        "concepto": m.concepto or "",
        "documento": m.documento or "",
        "monto": str(m.monto) if m.monto is not None else "0",
        "saldo": str(m.saldo) if m.saldo is not None else "0",
        "codigo": m.codigo or "",
        "tipo": m.tipo or "",
        "oficina": m.oficina or "",
    }


def _dict_to_mov(d: dict) -> MovBanco:
    return MovBanco(
        fecha=date.fromisoformat(d["fecha"]) if d.get("fecha") else None,
        concepto=d.get("concepto", ""),
        documento=d.get("documento", ""),
        monto=Decimal(d.get("monto") or "0"),
        saldo=Decimal(d.get("saldo") or "0"),
        codigo=d.get("codigo", ""),
        tipo=d.get("tipo", ""),
        oficina=d.get("oficina", ""),
    )


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ─── Detección de migration 0060 ──────────────────────────────────────


def tabla_existe() -> bool:
    """¿Corrió la migration 0060? Cacheado por proceso."""
    if hasattr(tabla_existe, "_cache"):
        return tabla_existe._cache
    try:
        row = db.fetch_one(
            """
            SELECT 1
              FROM information_schema.tables
             WHERE table_schema='scintela'
               AND table_name='banco_conciliacion_sesion'
            """
        )
        tabla_existe._cache = bool(row)
    except Exception:
        tabla_existe._cache = False
    return tabla_existe._cache


# ─── CRUD básico de sesión ────────────────────────────────────────────


def sesion_abierta(no_banco: int, usuario: str | None = None) -> dict | None:
    """La única sesión abierta del banco, o None.

    TMT 2026-06-02: drop filtro por usuario. Mig 0062 dejó UNA sola sesión
    abierta por banco (sin importar quién la abrió). El param `usuario` se
    deja como kwarg compatible con llamadores viejos pero se ignora.
    """
    return db.fetch_one(
        """
        SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
               extracto_hash, extracto_nombre, extracto_payload,
               matches_hechos, pdf_path
          FROM scintela.banco_conciliacion_sesion
         WHERE no_banco = %s AND cerrada_en IS NULL
         ORDER BY abierta_en DESC
         LIMIT 1
        """,
        (int(no_banco),),
    )


def sesion_por_id(sesion_id: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
               extracto_hash, extracto_nombre, extracto_payload,
               matches_hechos, pdf_path
          FROM scintela.banco_conciliacion_sesion
         WHERE id = %s
        """,
        (int(sesion_id),),
    )


def matches_de_sesion(sesion: dict) -> list[dict]:
    """Lista todo lo que se conciliaron en esta sesión: matches del extracto
    Y los que vinieron de un histórico (mismo concepto: una fila por match).

    Aproximamos por ventana temporal: banco_conciliacion_match.creado_en
    entre abierta_en y cerrada_en (o NOW si está abierta).

    Cada fila lleva campo `tipo`:
      - 'historico': el match tiene un banco_historicos_pendientes que lo
                     referencia vía conciliado_match_id. Chip morado.
      - 'match'   : todo lo demás. Chip verde.

    TMT 2026-05-29 dueña: 'ESTOS FUERON UN SOLO MOVIMIENTO PORQUE APARECE
    EN DOS ROWS?'. Antes hacíamos dos queries y sumábamos — el match y el
    histórico aparecían como filas separadas. Fix: un solo SELECT con LEFT
    JOIN al histórico; si existe → tipo='historico', si no → tipo='match'.
    Una conciliación = una fila.
    """
    if not sesion:
        return []
    no_banco = int(sesion.get("no_banco") or 0)
    abierta = sesion.get("abierta_en")
    cerrada = sesion.get("cerrada_en")
    if not no_banco or not abierta:
        return []
    filtro_undo = ""
    try:
        from modules.conciliacion.matcher_banco import _tiene_migration_47
        if _tiene_migration_47():
            filtro_undo = "AND m.deshecho_en IS NULL"
    except Exception:
        pass

    sql = f"""
        SELECT CASE WHEN h.id IS NOT NULL THEN 'historico' ELSE 'match' END AS tipo,
               m.id, m.estado, m.creado_en, m.usuario,
               m.real_fecha, m.real_documento, m.real_monto, m.real_tipo, m.real_concepto,
               m.id_transaccion,
               tb.fecha       AS tb_fecha,
               tb.documento   AS tb_documento,
               tb.importe     AS tb_importe,
               tb.numreferencia AS tb_numreferencia,
               tb.concepto    AS tb_concepto,
               tb.prov        AS tb_prov,
               h.id           AS historico_id
          FROM scintela.banco_conciliacion_match m
          LEFT JOIN scintela.transacciones_bancarias tb
            ON tb.id_transaccion = m.id_transaccion
          LEFT JOIN scintela.banco_historicos_pendientes h
            ON h.conciliado_match_id = m.id
         WHERE m.no_banco = %s
           AND m.creado_en >= %s
           AND m.creado_en <= COALESCE(%s, CURRENT_TIMESTAMP)
           {filtro_undo}
         ORDER BY m.creado_en DESC
         LIMIT 1000
    """
    try:
        rows = db.fetch_all(sql, (no_banco, abierta, cerrada)) or []
        return [dict(r) for r in rows]
    except Exception as e:
        _LOG.warning("matches_de_sesion falló: %s", e)
        return []


def _documentos_ya_conocidos(no_banco: int) -> set[str]:
    """Set de números de documento ya en el sistema para este banco.

    Junta tres fuentes:
      - banco_historicos_pendientes.documento (filas del banco ya cargadas)
      - banco_conciliacion_match.real_documento (matches activos)
      - extracto_payload de la sesión abierta (si existe)

    TMT 2026-06-02 dueña: 'que compare numero de documento de banco y si
    ya esta en nuestra lista, no agregarse'. Dedupe row-level por documento.
    """
    docs: set[str] = set()
    try:
        rows = db.fetch_all(
            """
            SELECT DISTINCT documento
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s AND documento IS NOT NULL AND documento <> ''
            """,
            (int(no_banco),),
        ) or []
        docs.update((r.get("documento") or "").strip().upper() for r in rows)
    except Exception as e:
        _LOG.warning("dedupe: historicos query falló: %s", e)
    try:
        # Detectar si la columna deshecho_en existe (migration 0047).
        filtro_undo = ""
        try:
            from modules.conciliacion.matcher_banco import _tiene_migration_47
            if _tiene_migration_47():
                filtro_undo = "AND deshecho_en IS NULL"
        except Exception:
            pass
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT real_documento
              FROM scintela.banco_conciliacion_match
             WHERE no_banco = %s
               AND real_documento IS NOT NULL
               AND real_documento <> ''
               {filtro_undo}
            """,
            (int(no_banco),),
        ) or []
        docs.update((r.get("real_documento") or "").strip().upper() for r in rows)
    except Exception as e:
        _LOG.warning("dedupe: matches query falló: %s", e)
    # Documentos en el payload de la sesión actual.
    abierta = sesion_abierta(int(no_banco))
    if abierta:
        try:
            for m in cargar_movs(abierta):
                if m.documento:
                    docs.add(str(m.documento).strip().upper())
        except Exception:
            pass
    # Limpiar vacíos defensivamente.
    docs.discard("")
    return docs


def crear_sesion(
    no_banco: int,
    usuario: str,
    movs: Iterable[MovBanco],
    *,
    extracto_hash: str | None = None,
    extracto_nombre: str | None = None,
) -> tuple[int, int, int]:
    """Mergea movs nuevos en la sesión abierta del banco. Si no hay, crea una.

    Dedupea por número de documento contra (historicos ∪ matches activos ∪
    payload existente). Solo agrega filas con `documento` que no se haya
    visto antes para ese banco.

    Returns:
        (sesion_id, n_added, n_skipped)

    TMT 2026-06-02 dueña: reformado para soportar "una sesión continua por
    banco". Cada upload mete filas nuevas; las repetidas (mismo documento)
    se descartan silenciosamente. Si no hay sesión abierta, la crea.
    """
    movs = list(movs)
    no_banco = int(no_banco)

    # Filtrar movs cuyo documento ya está conocido (otras filas, otros uploads).
    docs_existentes = _documentos_ya_conocidos(no_banco)
    nuevos: list[MovBanco] = []
    skipped = 0
    # Dedupe interno por si el mismo upload trae el documento repetido.
    docs_en_upload: set[str] = set()
    for m in movs:
        doc = (m.documento or "").strip().upper()
        if not doc:
            # Sin documento → no podemos dedupear; lo dejamos pasar.
            nuevos.append(m)
            continue
        if doc in docs_existentes or doc in docs_en_upload:
            skipped += 1
            continue
        docs_en_upload.add(doc)
        nuevos.append(m)

    abierta = sesion_abierta(no_banco)
    if abierta:
        # MERGE: concatenar payload existente + nuevos.
        existentes = cargar_movs(abierta)
        merged = existentes + nuevos
        payload = json.dumps([_mov_to_dict(m) for m in merged])
        nombre = (extracto_nombre or abierta.get("extracto_nombre") or "")[:200]
        db.execute(
            """
            UPDATE scintela.banco_conciliacion_sesion
               SET extracto_payload = %s::jsonb,
                   extracto_nombre = %s,
                   extracto_hash = COALESCE(%s, extracto_hash)
             WHERE id = %s
            """,
            (payload, nombre, extracto_hash, int(abierta["id"])),
        )
        return (int(abierta["id"]), len(nuevos), skipped)

    # No hay sesión abierta → crear una.
    payload = json.dumps([_mov_to_dict(m) for m in nuevos])
    row = db.execute_returning(
        """
        INSERT INTO scintela.banco_conciliacion_sesion
            (no_banco, usuario, extracto_hash, extracto_nombre, extracto_payload)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (no_banco, usuario[:50], extracto_hash, (extracto_nombre or "")[:200], payload),
    )
    sid = int(row["id"]) if row else 0
    # Snapshot inicial.
    try:
        from modules.conciliacion import saldo_snapshot as _ss
        _ss.snapshot(
            no_banco=no_banco,
            evento_tipo="sesion_abierta",
            evento_ref=str(sid),
            usuario=usuario,
            descripcion=f"apertura sesión #{sid}",
        )
    except Exception as e:
        _LOG.warning("snapshot apertura sesión #%s falló: %s", sid, e)
    return (sid, len(nuevos), skipped)


def incrementar_matches(sesion_id: int, n: int = 1) -> None:
    db.execute(
        """
        UPDATE scintela.banco_conciliacion_sesion
           SET matches_hechos = matches_hechos + %s
         WHERE id = %s
        """,
        (int(n), int(sesion_id)),
    )


def listar_sesiones(no_banco: int | None = None, limit: int = 100) -> list[dict]:
    if no_banco:
        return db.fetch_all(
            """
            SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
                   extracto_nombre, matches_hechos, pdf_path
              FROM scintela.banco_conciliacion_sesion
             WHERE no_banco = %s
             ORDER BY COALESCE(cerrada_en, abierta_en) DESC
             LIMIT %s
            """,
            (int(no_banco), int(limit)),
        ) or []
    return db.fetch_all(
        """
        SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
               extracto_nombre, matches_hechos, pdf_path
          FROM scintela.banco_conciliacion_sesion
         ORDER BY COALESCE(cerrada_en, abierta_en) DESC
         LIMIT %s
        """,
        (int(limit),),
    ) or []


# ─── Recuperar payload y re-correr matcher ────────────────────────────


def cargar_movs(sesion: dict) -> list[MovBanco]:
    """De la fila DB → lista MovBanco."""
    payload = sesion.get("extracto_payload") or []
    # psycopg2 RealDictCursor devuelve jsonb como str o list dependiendo del driver.
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    return [_dict_to_mov(d) for d in payload]


# ─── Bucketización del resultado del matcher ──────────────────────────


def _es_comision(cat) -> bool:
    """True si la categoría es COMISION (impuestos bancarios, comisiones, SENAE)."""
    if not cat:
        return False
    return (getattr(cat, "grupo", "") or "").upper() == "COMISION"


def bucketizar(res: ConciliacionBanco) -> dict:
    """Toma el resultado del matcher y lo divide en 3 buckets para los 3 tabs.

    Returns:
        {
          'manual_banco': list[(MovBanco, Categorizado, idx)],   # real_only NO comision
          'manual_programa': list[(MovBancsis, Categorizado, idx)],  # bancsis_only todos
          'impuestos': list[(MovBanco, Categorizado, idx)],      # real_only COMISION
          'transferencias': list[Match],                          # matches que vinieron por doc (P0)
          'sugerencias': list[Match],                             # resto de matches (P1-P4)
        }

    Nota: PASS 0 hoy NO se persiste en Match.razon, pero el matcher devuelve
    razon='Doc-ID exacto (PASS 0)'. Detectamos por substring 'PASS 0' en razon.
    """
    real_only = res.real_only or []
    bancsis_only = res.bancsis_only or []
    real_cats = res.real_only_cats or [None] * len(real_only)
    bancsis_cats = res.bancsis_only_cats or [None] * len(bancsis_only)

    manual_banco = []
    impuestos = []
    for i, mov in enumerate(real_only):
        cat = real_cats[i] if i < len(real_cats) else None
        bucket = impuestos if _es_comision(cat) else manual_banco
        bucket.append({"mov": mov, "cat": cat, "idx": i})

    manual_programa = []
    for i, mov in enumerate(bancsis_only):
        cat = bancsis_cats[i] if i < len(bancsis_cats) else None
        manual_programa.append({"mov": mov, "cat": cat, "idx": i})

    # Orden: mayor a menor por monto (la dueña pidió de mayor a menor).
    manual_banco.sort(key=lambda x: float(x["mov"].monto or 0), reverse=True)
    manual_programa.sort(key=lambda x: abs(float(x["mov"].importe or 0)), reverse=True)
    impuestos.sort(key=lambda x: float(x["mov"].monto or 0), reverse=True)

    transferencias = []
    sugerencias = []
    for m in (res.matches or []):
        razon = (m.razon or "").upper()
        if "PASS 0" in razon or "P0" in razon or "DOC-ID" in razon or "DOC ID" in razon:
            transferencias.append(m)
        else:
            sugerencias.append(m)

    return {
        "manual_banco": manual_banco,
        "manual_programa": manual_programa,
        "impuestos": impuestos,
        "transferencias": transferencias,
        "sugerencias": sugerencias,
    }


# ─── Helper top-level: cargar sesión + buckets ────────────────────────


def _cargar_historicos_pendientes(no_banco: int) -> list[dict]:
    """Históricos del banco que quedaron sin conciliar — se mezclan en el
    panel Banco del tab Manual para que la dueña los vea junto con los
    movs del extracto actual.

    TMT 2026-05-29 dueña: 'no me estan apareciendo los historicos sin
    conciliar'. Antes solo veía real_only del matcher (los del extracto
    de la sesión actual). Los históricos viven en
    scintela.banco_historicos_pendientes con conciliado_en IS NULL.
    """
    try:
        rows = db.fetch_all(
            """
            SELECT id, fecha, documento, concepto, monto, tipo, oficina, detalle
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
             ORDER BY ABS(monto) DESC
             LIMIT 500
            """,
            (int(no_banco),),
        ) or []
        return rows
    except Exception as e:
        _LOG.warning("_cargar_historicos_pendientes falló: %s", e)
        return []


def estado_sesion(sesion: dict, no_banco: int) -> dict:
    """De la sesión DB → buckets listos para renderizar.

    Re-corre el matcher cada vez (porque entre la apertura de la sesión y
    ahora puede haberse conciliado algo desde otra pestaña, o haber llegado
    movs PC nuevos). El matcher ya excluye los `id_transaccion` con match
    activo en `banco_conciliacion_match`.
    """
    movs = cargar_movs(sesion)
    historicos = _cargar_historicos_pendientes(no_banco)
    if not movs:
        # Sin extracto en sesión, pero puede haber históricos para conciliar.
        manual_banco_hist = [
            {"mov": _hist_to_mov_like(h), "cat": None, "idx": -1,
             "es_historico": True, "id_historico": int(h["id"])}
            for h in historicos
        ]
        # TMT 2026-05-29 dueña: 'sin movimientos del programa' — bug.
        # Cuando se abre sesión sin extracto, manual_programa quedaba
        # vacío. Fix: cargar BANCSIS pendientes con el MISMO filtro que
        # usa balance.calcular() (stat<>'*' AND NOT EXISTS match), así
        # cuadra con el contador de "Pendientes en programa".
        # Antes usaba cargar_bancsis que trae TODO el universo del banco
        # (~1000+ filas) — mostraba demasiados. TMT 2026-05-29 segunda
        # iteración: 'aca hay demasiados movimientos para conciliar'.
        manual_programa: list[dict] = []
        try:
            from modules.conciliacion.matcher_banco import MovBancsis as _MovBk
            rows_pc = db.fetch_all(
                """
                SELECT tb.id_transaccion, tb.fecha, tb.documento, tb.concepto,
                       tb.importe, tb.numreferencia, tb.no_banco, tb.saldo,
                       tb.prov, tb.fecha_crea
                  FROM scintela.transacciones_bancarias tb
                 WHERE tb.no_banco = %s
                   AND TRIM(COALESCE(tb.stat, '')) <> '*'
                   AND NOT EXISTS (
                       SELECT 1 FROM scintela.banco_conciliacion_match m
                        WHERE m.id_transaccion = tb.id_transaccion
                          AND m.deshecho_en IS NULL
                   )
                 ORDER BY ABS(tb.importe) DESC, tb.fecha DESC
                 LIMIT 1000
                """,
                (int(no_banco),),
            ) or []
            # Resolver nombre de cliente/proveedor en batch.
            codigos = {
                (r.get("prov") or "").strip().upper()
                for r in rows_pc if (r.get("prov") or "").strip()
            }
            nombres = {}
            if codigos:
                try:
                    rows_cli = db.fetch_all(
                        """
                        SELECT codigo_cli AS cod, nombre FROM scintela.cliente
                         WHERE UPPER(codigo_cli) = ANY(%s::text[])
                        """,
                        (list(codigos),),
                    ) or []
                    nombres = {
                        (r["cod"] or "").strip().upper(): (r.get("nombre") or "").strip()
                        for r in rows_cli
                    }
                except Exception:
                    pass
            for i, r in enumerate(rows_pc):
                prov = (r.get("prov") or "").strip().upper()
                bk = _MovBk(
                    id_transaccion=int(r["id_transaccion"]),
                    fecha=r.get("fecha"),
                    documento=str(r.get("documento") or "").strip().upper(),
                    concepto=str(r.get("concepto") or "").strip(),
                    importe=float(r.get("importe") or 0),
                    numreferencia=str(r.get("numreferencia") or "").strip(),
                    no_banco=int(r.get("no_banco") or 0),
                    saldo=float(r["saldo"]) if r.get("saldo") is not None else None,
                    prov=prov,
                    prov_nombre=nombres.get(prov, ""),
                    fecha_crea=r.get("fecha_crea"),
                )
                manual_programa.append({"mov": bk, "cat": None, "idx": i})
        except Exception as e:
            _LOG.warning("cargar pendientes programa sin extracto falló: %s", e)
        return {
            "manual_banco": manual_banco_hist,
            "manual_programa": manual_programa,
            "impuestos": [], "transferencias": [], "sugerencias": [],
            "matcher_extracto_desde": None, "matcher_extracto_hasta": None,
        }
    try:
        res = matchear_extracto_banco(movs, no_banco=no_banco)
    except Exception as e:
        _LOG.warning("matchear_extracto_banco falló: %s", e)
        return {
            "manual_banco": [], "manual_programa": [],
            "impuestos": [], "transferencias": [], "sugerencias": [],
            "matcher_extracto_desde": None, "matcher_extracto_hasta": None,
        }
    buckets = bucketizar(res)
    # Mezclar los históricos al inicio del panel Banco (más viejos arriba).
    hist_items = [
        {"mov": _hist_to_mov_like(h), "cat": None, "idx": -1,
         "es_historico": True, "id_historico": int(h["id"])}
        for h in historicos
    ]
    buckets["manual_banco"] = hist_items + (buckets.get("manual_banco") or [])
    buckets["matcher_extracto_desde"] = res.extracto_desde
    buckets["matcher_extracto_hasta"] = res.extracto_hasta
    buckets["n_historicos_pendientes"] = len(historicos)
    return buckets


def _hist_to_mov_like(h: dict):
    """Wrap un row de banco_historicos_pendientes en algo que el template
    puede tratar como mov banco. Atributos esperados por el template:
    fecha, concepto, documento, monto, tipo.
    """
    import types
    m = types.SimpleNamespace()
    m.fecha = h.get("fecha")
    m.concepto = h.get("concepto") or ""
    m.documento = h.get("documento") or ""
    try:
        m.monto = float(h.get("monto") or 0)
    except (TypeError, ValueError):
        m.monto = 0
    m.tipo = (h.get("tipo") or "C").upper()
    return m
