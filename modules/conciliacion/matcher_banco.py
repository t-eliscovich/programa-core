"""Matcher bidireccional: extracto del banco (REAL) ↔ scintela.transacciones_bancarias (BANCSIS).

TMT 2026-05-22 — Para cada movimiento del banco real intentamos encontrar la
transacción equivalente en BANCSIS. Devolvemos 3 grupos:

    matched         — match exacto o probable (mismo signo C/D, monto ± $1, fecha ± 5 días)
    real_only       — solo está en REAL (no en BANCSIS)  → Mov 1 de la ecuación de saldo
    bancsis_only    — solo está en BANCSIS (no en REAL)  → Mov 2

Verificación de saldo:
    SALDO_REAL_final = SALDO_BANCSIS_final + Σ(real_only) - Σ(bancsis_only)
(tolerancia ± $100)

Mapeo de tipos:
    Banco REAL Tipo='C' (crédito → entra plata)  ↔  BANCSIS documento IN ('DE','TR','AC','NC')
    Banco REAL Tipo='D' (débito  → sale plata)   ↔  BANCSIS documento IN ('CH','ND','DB')

Persistencia: los matches confirmados quedan en scintela.banco_conciliacion_match
y NO vuelven a aparecer en sesiones siguientes (el matcher los excluye).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import db
from modules.conciliacion.parser_banco import MovBanco

_LOG = logging.getLogger("programa_core.conciliacion.matcher_banco")


# Mapping tipo banco ↔ documento BANCSIS.
# Créditos coinciden con bank_helpers.DOCS_ENTRADA (canónico del programa).
# Débitos: lista expandida + cualquier doc no-crédito se trata como débito.
_DOCS_CREDITO = ("DE", "TR", "XX", "NC", "IN", "AC")  # entra plata
_DOCS_DEBITO = ("CH", "ND", "DB", "GS", "PA")          # sale plata (lista de los más comunes)
_BANCO_PICHINCHA_NO = 10  # confirmado en /bancos/10 (2026-05-22)


@dataclass
class MovBancsis:
    """Fila de scintela.transacciones_bancarias (BANCSIS)."""

    id_transaccion: int
    fecha: date
    documento: str
    concepto: str
    importe: float
    numreferencia: str
    no_banco: int
    saldo: float | None
    prov: str = ""           # código cliente/proveedor (CLI/PROV)
    prov_nombre: str = ""    # nombre legible (vía LEFT JOIN clientes)
    es_agrupado: bool = False  # mov del programa que suma N cheques (dep.N ch)
    n_cheques: int = 0         # cuántos cheques agrupa
    no_cheque_rel: str = ""    # TMT 2026-05-26: no_cheque del cheque ligado (vía chequextransaccion). Usado por PASS 0 para matchear contra extracto.documento.
    doc_banco_rel: str = ""    # TMT 2026-05-27: cheque.doc_banco (N° comprobante banco) del cheque ligado. Usado por PASS 0 para matchear contra extracto.documento — es lo que la dueña edita inline.
    fecha_crea: object = None  # TMT 2026-05-27: timestamp del INSERT (para agrupar lotes ≤120s en panel conciliación, igual que /bancos y /cheques)

    @property
    def tipo_real(self) -> str:
        """Mapeo inverso: ¿qué Tipo C/D del banco le correspondería?"""
        if self.documento in _DOCS_CREDITO:
            return "C"
        if self.documento in _DOCS_DEBITO:
            return "D"
        return "?"


@dataclass
class Match:
    """Un movimiento REAL del banco emparejado con un MovBancsis (o N en match grupal N-a-1)."""

    real: MovBanco
    bancsis: MovBancsis
    score: float       # 0 = perfecto. Mayor = más drift.
    razon: str         # explicación legible
    # TMT 2026-05-26 dueña: para match N-a-1 (1 depósito banco ↔ N cheques
    # programa del mismo día). bancsis es uno solo (el "representativo");
    # componentes son TODOS los bancsis que componen la suma. Si N=1, vacío.
    componentes: list = field(default_factory=list)


@dataclass
class MatchGrupal:
    """N cheques del banco → 1 mov agrupado del programa (dep.N ch).

    No se persiste 1-a-1 (limita el schema). Visual + KPI solo.
    """
    bancsis: MovBancsis           # el agrupado del programa
    reales: list[MovBanco] = field(default_factory=list)
    suma_reales: float = 0.0
    diff_monto: float = 0.0
    razon: str = ""


@dataclass
class Categorizado:
    """Categoría asociada a un mov (banco real o BANCSIS).

    Se calcula post-match y se adjunta al resultado para que el template
    pueda agrupar y mostrar labels legibles.
    """
    codigo: str            # ej: SALIDA_PAGO_PROVEEDOR
    grupo: str             # ENTRADA | SALIDA | COMISION | OTRO
    label: str             # "Pago a proveedor"
    abrev: str = "?"       # "P" | "TR" | "CH" | etc — chip compacto
    cliente: str = ""      # extraído del concepto o por AI (codigo corto)
    cliente_nombre: str = ""  # TMT 2026-05-26: nombre largo extraído ej "LAAZ RODRIGUEZ ANGELICA"
    descripcion: str = ""  # frase legible (solo cuando hay AI)
    fuente: str = "regex"  # regex | ai | ai-cache | tipo-fallback


@dataclass
class ConciliacionBanco:
    matches: list[Match] = field(default_factory=list)
    real_only: list[MovBanco] = field(default_factory=list)
    bancsis_only: list[MovBancsis] = field(default_factory=list)
    # Categorías paralelas: misma longitud y orden que real_only/bancsis_only
    real_only_cats: list[Categorizado] = field(default_factory=list)
    bancsis_only_cats: list[Categorizado] = field(default_factory=list)
    matches_cats: list[Categorizado] = field(default_factory=list)
    # Info de la ventana usada (para mostrar en la UI)
    extracto_desde: date | None = None
    extracto_hasta: date | None = None
    ventana_dias: int = 0
    bancsis_cargados: int = 0  # cuántas tx BANCSIS entraron al matcher
    # Sugerencias por real_only: índice → [{id_transaccion, importe, fecha, prov, ...}]
    sugerencias_real_only: dict = field(default_factory=dict)
    # Conteos por pasada del matcher (P1/P2/P3/P4): para mostrar en UI
    matches_por_pasada: dict = field(default_factory=lambda: {"P1": 0, "P2": 0, "P3": 0, "P4": 0})
    # Agrupados del programa (dep.N ch) — excluidos de la conciliación 1-a-1.
    # Solo se reportan como info en el header.
    bancsis_agrupados: list[MovBancsis] = field(default_factory=list)
    # Saldos del extracto
    saldo_real_final: Decimal = Decimal(0)
    saldo_real_fecha: date | None = None
    saldo_bancsis_final: float = 0.0
    saldo_bancsis_fecha: date | None = None
    # Totales por grupo (signados)
    total_real_only_signed: float = 0.0
    total_bancsis_only_signed: float = 0.0


def _tiene_migration_47() -> bool:
    """¿Corrió la migration 0047 (columnas deshecho_en/metodo)?

    Cacheado por proceso. Si la migration no corrió todavía, el código
    sigue andando con la lógica vieja (sin soft-undo, sin método).
    """
    if hasattr(_tiene_migration_47, "_cache"):
        return _tiene_migration_47._cache
    row = db.fetch_one(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'scintela'
           AND table_name = 'banco_conciliacion_match'
           AND column_name = 'deshecho_en'
        """
    )
    _tiene_migration_47._cache = bool(row)
    return _tiene_migration_47._cache


def _ya_conciliadas(no_banco: int, desde: date, hasta: date) -> tuple[set[int], set[tuple]]:
    """Devuelve:
        set de id_transaccion (BANCSIS) ya conciliados (y NO deshechos)
        set de firma REAL (fecha, documento, monto str, tipo) ya conciliados
    """
    filtro_undo = "AND deshecho_en IS NULL" if _tiene_migration_47() else ""
    rows = db.fetch_all(
        f"""
        SELECT id_transaccion, real_fecha, real_documento, real_monto, real_tipo
          FROM scintela.banco_conciliacion_match
         WHERE no_banco = %s
           {filtro_undo}
           AND (real_fecha IS NULL OR real_fecha BETWEEN %s AND %s)
        """,
        (no_banco, desde - timedelta(days=30), hasta + timedelta(days=30)),
    ) or []
    ids_bancsis: set[int] = set()
    firmas_real: set[tuple] = set()
    for r in rows:
        if r.get("id_transaccion"):
            ids_bancsis.add(int(r["id_transaccion"]))
        if r.get("real_fecha") and r.get("real_documento"):
            firmas_real.add((
                r["real_fecha"],
                (r.get("real_documento") or "").strip(),
                f"{Decimal(str(r.get('real_monto') or 0)):.2f}",
                (r.get("real_tipo") or "").strip().upper(),
            ))

    # TMT 2026-05-27 dueña: 'en dbase, pichincha, ya existe que movimientos
    # fueron conciliados y cuales no'. PICHINCH.DBF tiene un campo STAT:
    # '*' = conciliado, '' = no conciliado. El sync DBF lo trae a
    # scintela.transacciones_bancarias.stat. Tratamos stat='*' como
    # conciliado pre-existente — el matcher los excluye automáticamente.
    try:
        rows_dbf = db.fetch_all(
            """
            SELECT id_transaccion
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
               AND fecha BETWEEN %s AND %s
               AND TRIM(COALESCE(stat, '')) = '*'
            """,
            (no_banco, desde - timedelta(days=30), hasta + timedelta(days=30)),
        ) or []
        for r in rows_dbf:
            if r.get("id_transaccion"):
                ids_bancsis.add(int(r["id_transaccion"]))
    except Exception:
        pass  # fail-soft: si la columna no existe, seguimos con lo registrado en PC

    return ids_bancsis, firmas_real


import re as _re

# Patrones para extraer cliente/proveedor del concepto del banco.
# Hay dos formas:
#   A) Código corto interno (3-5 letras) tipo "ch.LTM", "tr JTX", "1 ch.BFV"
#   B) Nombre completo del banco real tipo "TRANSFERENCIA DIRECTA DE
#      AGUILAR RUIZ JACQUELINE DEL CARMEN" — el banco te da el nombre.

_RE_CODIGO_INTERNO = _re.compile(
    r"(?:^|\s)(?:\d+\s+)?(?:ch\.?|tr\.?|nc\.?|trf\.?|dep\.?\s*ch\.?)\s*([A-Za-z]{2,5})\b",
    _re.IGNORECASE,
)

# TMT 2026-05-26 dueña: patrón de SALIDAS Pichincha. Conceptos tipo:
#   "2605150C3EHP-INTELA C-PAG-ANTICIPO"   → cliente EHP
#   "2605180C70HQ-INTELA C-PAG-ANT CARVAJAL" → cliente 0HQ o HQ (ambiguo)
# Tomamos los 2-5 chars (letras+dígitos) inmediatamente antes de -INTELA.
# La validación contra catálogo se hace en _extraer_cliente_concepto.
_RE_PICHINCHA_SALIDA = _re.compile(
    r"([A-Z0-9]{2,5})-INTELA[\s\-]*C[\s\-]*PAG",
    _re.IGNORECASE,
)

_RE_NOMBRE_LARGO = _re.compile(
    r"(?:TRANSFERENCIA\s+(?:DIRECTA|INTERBANCARIA|INTERNA)?\s*"
    r"(?:DE|A|RECIBIDA\s+DE|ENVIADA\s+A)\s+|"
    r"DEP[OÓ]SITO\s+(?:DE|EFECTIVO\s+DE)\s+|"
    r"PAGO\s+(?:A|DE)\s+|"
    r"COBRO\s+(?:DE|A)\s+)"
    r"([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ \.\-]{4,})",
    _re.IGNORECASE,
)


def _extraer_cliente_concepto(concepto: str, codigos_validos: set[str] | None = None) -> tuple[str, str]:
    """Devuelve (codigo_corto, nombre_largo) extraídos del concepto.

    Cualquiera puede ser '' si no se detecta. Si `codigos_validos` se pasa,
    el código devuelto SOLO se devuelve si está en el set (válida contra
    catálogo real de clientes + proveedores + aliases).

    >>> _extraer_cliente_concepto("1 ch.LTM")
    ('LTM', '')
    >>> _extraer_cliente_concepto("TRANSFERENCIA DIRECTA DE AGUILAR RUIZ JACQUELINE")[1]
    'AGUILAR RUIZ JACQUELINE'
    >>> _extraer_cliente_concepto("2605150C3EHP-INTELA C-PAG-ANTICIPO")[0]
    'EHP'
    """
    if not concepto:
        return "", ""
    # Candidatos en orden de prioridad. El primero válido gana.
    candidatos: list[str] = []
    m = _RE_CODIGO_INTERNO.search(concepto)
    if m:
        candidatos.append(m.group(1).upper().strip())
    # TMT 2026-05-26 dueña: patrón Pichincha salida (...XXX-INTELA C-PAG-...).
    # Generamos VARIOS sub-candidatos del prefix porque el código puede ser
    # los 2, 3 o 4 chars antes de -INTELA. Probamos del más largo al más
    # corto. Si codigos_validos está pasado, validamos contra él.
    m_pich = _RE_PICHINCHA_SALIDA.search(concepto)
    if m_pich:
        cand_full = m_pich.group(1).upper().strip()
        # Generar slices del más largo al más corto: cand_full, cand_full[1:], cand_full[2:]
        for i in range(len(cand_full)):
            sub = cand_full[i:]
            if sub and not sub.isdigit() and sub not in candidatos:
                candidatos.append(sub)
    # Buscar el primer candidato válido. Si no hay codigos_validos pasados,
    # devolver el primer candidato no-numérico (best-effort).
    codigo = ""
    for cand in candidatos:
        if codigos_validos:
            if cand in codigos_validos:
                codigo = cand
                break
        else:
            # Sin validación, fallback al viejo behavior — primer no-numérico.
            if cand and not cand.isdigit():
                codigo = cand
                break

    nombre = ""
    m2 = _RE_NOMBRE_LARGO.search(concepto)
    if m2:
        nombre = " ".join(m2.group(1).strip().split())
        nombre = nombre.upper()
    return codigo, nombre


# Cache del set de códigos válidos (cliente + proveedor + aliases).
# TTL 5 min — refresca con el cambio de catálogo o de aliases.
import time as _time
_CODIGOS_CACHE: dict = {"ts": 0.0, "set": set()}
_CODIGOS_TTL = 300


def codigos_validos_set() -> set[str]:
    """Devuelve set en memoria de todos los códigos cliente + proveedor + aliases.

    Para validar códigos extraídos del concepto. Cache TTL 5 min — fail-soft
    devuelve set vacío si DB falla, lo cual hace fall back al best-effort.
    """
    now = _time.time()
    if now - _CODIGOS_CACHE["ts"] < _CODIGOS_TTL and _CODIGOS_CACHE["set"]:
        return _CODIGOS_CACHE["set"]
    out: set[str] = set()
    try:
        for r in (db.fetch_all("SELECT codigo_cli FROM scintela.cliente") or []):
            c = (r.get("codigo_cli") or "").strip().upper()
            if c:
                out.add(c)
        for r in (db.fetch_all("SELECT codigo_prov FROM scintela.proveedor") or []):
            c = (r.get("codigo_prov") or "").strip().upper()
            if c:
                out.add(c)
        # Aliases Asinfo (CL2, AJ2, J3C) también son válidos.
        try:
            from modules.asinfo.aliases import todos as _aliases_todos
            for a in _aliases_todos():
                c = (a.get("codigo_asinfo") or "").strip().upper()
                if c:
                    out.add(c)
        except Exception:
            pass
    except Exception:
        out = set()
    if out:
        _CODIGOS_CACHE["set"] = out
        _CODIGOS_CACHE["ts"] = now
    return out


def _codigo_desde_concepto(concepto: str) -> str:
    """Compat shim — solo el código corto. Llama a _extraer_cliente_concepto."""
    return _extraer_cliente_concepto(concepto)[0]


# Detecta agrupados del programa tipo "dep.25 ch.", "dep 25 ch", "25 ch.LTM", etc.
# Cuando N >= 2 es un agrupado (1 deposito que suma N cheques).
_RE_AGRUPADO = _re.compile(r"\b(?:dep\.?|deposito|dep\s+ch)\s*\.?\s*(\d+)\s*ch\b|\b(\d+)\s+ch\.", _re.IGNORECASE)


def _detectar_agrupado(concepto: str) -> int:
    """Devuelve N (>=2) si el concepto es un depósito agrupado, 0 si no."""
    if not concepto:
        return 0
    m = _RE_AGRUPADO.search(concepto)
    if not m:
        return 0
    n_str = m.group(1) or m.group(2)
    try:
        n = int(n_str)
    except (ValueError, TypeError):
        return 0
    return n if n >= 2 else 0


def _resolver_clientes(rows: list[dict]) -> dict[str, str]:
    """Para cada `prov` (explícito o extraído del concepto), trae el nombre.

    Una sola query batch en vez de N+1. Devuelve {codigo_upper: nombre}.
    """
    codigos: set[str] = set()
    for r in rows:
        prov = (r.get("prov") or "").strip()
        if prov:
            codigos.add(prov.upper())
        cod_concepto = _codigo_desde_concepto(r.get("concepto") or "")
        if cod_concepto:
            codigos.add(cod_concepto)
    if not codigos:
        return {}
    rows_cli = db.fetch_all(
        """
        SELECT UPPER(TRIM(codigo_cli)) AS codigo_cli, nombre
          FROM scintela.cliente
         WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)
        """,
        (list(codigos),),
    ) or []
    return {r["codigo_cli"]: (r.get("nombre") or "").strip() for r in rows_cli}


def cargar_bancsis(no_banco: int, desde: date, hasta: date) -> list[MovBancsis]:
    """Trae todas las transacciones BANCSIS del banco en el rango.

    Resuelve el cliente con esta cascada:
      1. `tb.prov` si está poblado (explícito, mayor confianza).
      2. Regex sobre `tb.concepto` para extraer códigos embebidos (ej:
         "1 ch.LTM" → LTM). Cubre el caso típico de Pichincha donde el
         prov NO se carga pero el concepto trae el código.
    Después de resolver el código, joinea contra `scintela.cliente` por
    `codigo_cli` (en una sola query batch — no N+1).
    """
    # TMT 2026-05-26 dueña: traer también no_cheque del cheque ligado vía
    # chequextransaccion. Lo usa PASS 0 para matchear extracto.documento contra
    # tanto numreferencia COMO no_cheque (el banco a veces trae uno, a veces
    # otro). string_agg cubre el caso N:1 (un mov banco que agrupa N cheques).
    rows = db.fetch_all(
        """
        SELECT tb.id_transaccion, tb.fecha, tb.documento, tb.concepto, tb.importe,
               tb.numreferencia, tb.no_banco, tb.saldo, tb.prov, tb.fecha_crea,
               (SELECT STRING_AGG(DISTINCT ch.no_cheque::text, ',')
                  FROM scintela.chequextransaccion cxt
                  JOIN scintela.cheque ch ON ch.id_cheque = cxt.id_cheque
                 WHERE cxt.id_transaccion = tb.id_transaccion) AS no_cheques_rel,
               -- TMT 2026-05-27 dueña: 'si tiene el 4 del final. debuggia bien'
               -- cheque #1805 doc_banco=155032144 pero NO tiene chequextransaccion
               -- linkeada al bancsis #8811. Agregamos un fallback: si el cheque
               -- está huérfano (sin chequextransaccion) pero su fechad+importe
               -- coincide con tb.fecha+tb.importe, lo consideramos linkeado.
               -- Esto permite que el doc_banco editado inline funcione aunque
               -- el lote de depósito no se haya creado vía depositar_lote().
               (SELECT STRING_AGG(DISTINCT NULLIF(TRIM(ch.doc_banco), ''), ',')
                  FROM scintela.cheque ch
                 WHERE NULLIF(TRIM(ch.doc_banco), '') IS NOT NULL
                   AND (
                     EXISTS (SELECT 1 FROM scintela.chequextransaccion cxt
                              WHERE cxt.id_cheque = ch.id_cheque
                                AND cxt.id_transaccion = tb.id_transaccion)
                     OR
                     (
                       ch.fechad = tb.fecha
                       AND ABS(ch.importe - tb.importe) < 0.01
                       AND NOT EXISTS (SELECT 1 FROM scintela.chequextransaccion cxt2
                                        WHERE cxt2.id_cheque = ch.id_cheque)
                     )
                   )
               ) AS doc_banco_rel
          FROM scintela.transacciones_bancarias tb
         WHERE tb.no_banco = %s
           AND tb.fecha BETWEEN %s AND %s
         ORDER BY tb.fecha ASC, tb.id_transaccion ASC
        """,
        (no_banco, desde, hasta),
    ) or []
    nombre_por_codigo = _resolver_clientes(rows)
    out: list[MovBancsis] = []
    for r in rows:
        prov_explicito = str(r.get("prov") or "").strip()
        codigo_concepto, nombre_concepto = _extraer_cliente_concepto(r.get("concepto") or "")
        # Cascada: explícito si len>=3 → código del concepto → fallback al prov chico
        # Razón: el prov histórico en muchas filas tiene 2 chars (legacy) que NO
        # matchean con los codigo_cli reales (3 chars). El código del concepto
        # ("ch.CCH") es más confiable. Fix Tamara 2026-05-23.
        if len(prov_explicito) >= 3:
            prov_resuelto = prov_explicito.upper()
        elif codigo_concepto:
            prov_resuelto = codigo_concepto.upper()
        else:
            prov_resuelto = prov_explicito.upper()
        # Nombre: 1) BD si hay match por código, 2) nombre largo extraído, 3) nada
        prov_nombre = nombre_por_codigo.get(prov_resuelto, "") or nombre_concepto
        concepto_str = str(r.get("concepto") or "").strip()
        n_ch = _detectar_agrupado(concepto_str)
        out.append(MovBancsis(
            id_transaccion=int(r["id_transaccion"]),
            fecha=r["fecha"],
            documento=str(r.get("documento") or "").strip().upper(),
            concepto=concepto_str,
            importe=float(r.get("importe") or 0),
            numreferencia=str(r.get("numreferencia") or "").strip(),
            no_banco=int(r.get("no_banco") or 0),
            saldo=float(r.get("saldo")) if r.get("saldo") is not None else None,
            prov=prov_resuelto,
            prov_nombre=prov_nombre,
            es_agrupado=(n_ch > 0),
            n_cheques=n_ch,
            no_cheque_rel=str(r.get("no_cheques_rel") or "").strip(),
            doc_banco_rel=str(r.get("doc_banco_rel") or "").strip(),
            fecha_crea=r.get("fecha_crea"),
        ))
    return out


def _mapa_doc_banco_a_bancsis(no_banco: int, refs: set[str]) -> dict[str, list[int]]:
    """Para cada doc_banco normalizado en `refs`, devuelve la lista de
    id_transaccion BANCSIS (de `no_banco`) a los que el cheque está ligado
    vía chequextransaccion.

    TMT 2026-05-29 dueña: 'en conciliar por documento no funciona, este
    cheque con este documento no funciono'. El JOIN STRING_AGG de
    cargar_bancsis a veces no surfacea doc_banco si el cheque/BANCSIS
    quedó fuera del rango temporal o de algún caso borde. Esta función
    es una red de seguridad: consulta directamente cheque.doc_banco y
    sigue el link explícito, sin depender de fechas o agregados.
    """
    if not refs:
        return {}
    # Normalizamos en SQL también, para que comparemos peras con peras:
    # extracto.documento ya viene normalizado por _norm_doc (sin ceros a
    # izquierda); replicamos lo mismo sobre cheque.doc_banco.
    refs_clean = [r for r in refs if r]
    if not refs_clean:
        return {}
    try:
        rows = db.fetch_all(
            """
            SELECT
              CASE
                WHEN NULLIF(REGEXP_REPLACE(TRIM(ch.doc_banco), '^0+', ''), '') IS NULL
                  THEN TRIM(ch.doc_banco)
                ELSE REGEXP_REPLACE(TRIM(ch.doc_banco), '^0+', '')
              END                                       AS ref_norm,
              cxt.id_transaccion                        AS id_transaccion
              FROM scintela.cheque ch
              JOIN scintela.chequextransaccion cxt
                ON cxt.id_cheque = ch.id_cheque
              JOIN scintela.transacciones_bancarias tb
                ON tb.id_transaccion = cxt.id_transaccion
             WHERE tb.no_banco = %s
               AND NULLIF(TRIM(ch.doc_banco), '') IS NOT NULL
               AND (
                 TRIM(ch.doc_banco) = ANY(%s::text[])
                 OR REGEXP_REPLACE(TRIM(ch.doc_banco), '^0+', '') = ANY(%s::text[])
               )
            """,
            (int(no_banco), refs_clean, refs_clean),
        ) or []
    except Exception as e:
        _LOG.warning("_mapa_doc_banco_a_bancsis falló: %s", e)
        return {}
    out: dict[str, list[int]] = {}
    for r in rows:
        ref = (r.get("ref_norm") or "").strip()
        if not ref:
            continue
        out.setdefault(ref, []).append(int(r["id_transaccion"]))
    return out


def _firma_real(m: MovBanco) -> tuple:
    return (
        m.fecha,
        m.documento.strip(),
        f"{Decimal(m.monto):.2f}",
        (m.tipo or "").upper(),
    )


def _es_tipo_compatible(tipo_real: str, doc_bancsis: str) -> bool:
    """Compatibilidad Tipo C/D del banco real ↔ doc BANCSIS.

    Doc en _DOCS_CREDITO → crédito.
    Doc en _DOCS_DEBITO + cualquier OTRO → débito (asunción canónica
    de bank_helpers: 'cualquier otro RESTA').
    """
    doc = (doc_bancsis or "").upper().strip()
    es_credito = doc in _DOCS_CREDITO
    if tipo_real == "C":
        return es_credito
    if tipo_real == "D":
        return not es_credito  # cualquier no-crédito es débito
    return False


def _calcular_ventana_dias(fechas: list[date], default: int = 2) -> int:
    """Calcula días de tolerancia según la regla de Tamara (2026-05-23):

      - Default: ±2 días.
      - Si la fecha más reciente o la primera del extracto cae en
        VIERNES o LUNES → ±4 días, para cubrir el fin de semana en
        cualquier dirección (las tx del sábado/domingo se acreditan
        el lunes; las del lunes pueden estar relacionadas con el viernes).

    Cualquier sesión puede ampliar manualmente pasando otro `dias_tolerancia`.
    """
    if not fechas:
        return default
    ultima = max(fechas)
    primera = min(fechas)
    # weekday(): lunes=0, viernes=4, sábado=5, domingo=6
    if ultima.weekday() in (0, 4) or primera.weekday() in (0, 4):
        return 4
    return default


def matchear_extracto_banco(
    movs_real: Iterable[MovBanco],
    no_banco: int = _BANCO_PICHINCHA_NO,
    dias_tolerancia: int | None = None,
    monto_tolerancia: float = 0.01,  # TMT 2026-05-26 dueña: era 5.0 — matcheaba $0.49 con $2.00. Bajado a centavo para que P1 solo de exactos.
) -> ConciliacionBanco:
    """Cross-reference REAL vs BANCSIS bidireccional.

    Args:
        movs_real: parsed del xlsx Pichincha
        no_banco: 1 (Pichincha) — default
        dias_tolerancia: ventana de fechas para matching probable
        monto_tolerancia: USD, diff máxima de monto para considerar match

    Returns:
        ConciliacionBanco con matches, real_only, bancsis_only y saldos.
    """
    movs_real = list(movs_real)
    if not movs_real:
        return ConciliacionBanco()

    fechas_real = [m.fecha for m in movs_real]
    desde = min(fechas_real)
    hasta = max(fechas_real)

    # Ventana flexible: ±3d default, ±5d si última fecha es viernes/lunes.
    # TMT 2026-05-26 dueña: la ventana ±1d era demasiado estricta — extractos
    # como el del 13-14 mayo tenían 64 sugerencias por monto pero 0 matches
    # porque el drift de fecha excedía 1 día. Subido a ±3 default.
    if dias_tolerancia is None:
        dias_tolerancia = _calcular_ventana_dias(fechas_real, default=3)

    # Cargamos BANCSIS con ventana AMPLIA hacia adelante (banco se atrasa) y
    # un poco hacia atrás (programa cargado antes que banco). Las pasadas
    # P2-P4 trabajan dentro de este universo cargado.
    ventana_carga_atras = 1
    ventana_carga_adelante = max(dias_tolerancia, 15)
    bancsis = cargar_bancsis(
        no_banco,
        desde - timedelta(days=ventana_carga_atras),
        hasta + timedelta(days=ventana_carga_adelante),
    )
    bancsis_total = len(bancsis)

    # Excluimos los ya conciliados.
    ids_excl, firmas_excl = _ya_conciliadas(no_banco, desde, hasta)
    bancsis = [b for b in bancsis if b.id_transaccion not in ids_excl]
    movs_real_filtrados = [m for m in movs_real if _firma_real(m) not in firmas_excl]

    res = ConciliacionBanco()
    res.extracto_desde = desde
    res.extracto_hasta = hasta
    res.ventana_dias = dias_tolerancia
    res.bancsis_cargados = bancsis_total

    bancsis_usado: set[int] = set()
    cont_p0 = cont_p1 = cont_p2 = cont_p3 = cont_p4 = 0

    # ─── PASS 0: match EXACTO por documento del banco (numreferencia) ───
    # TMT 2026-05-26 dueña: "el número de documento del cheque es la regla
    # número 1 para match". Si extracto.documento == BANCSIS.numreferencia
    # (ambos no vacíos y != "0") → match directo, sin importar fecha/monto.
    # El humano cargó la misma referencia de comprobante → es match seguro.
    def _norm_doc(s: str) -> str:
        s = (s or "").strip().upper()
        if s in ("", "0", "00", "000", "0000"):
            return ""
        # Quitar ceros a la izquierda para que "00012345" == "12345".
        try:
            return str(int(s))
        except ValueError:
            return s

    # TMT 2026-05-26 dueña: helper para clasificar movs reales antes del match.
    # `_es_comision_real` mira el concepto contra la regex de COMISION_BANCARIA/
    # IMPUESTO. Si matchea → el mov NO va a Coinciden, sí va al card de
    # agrupado por día. Esto evita matches falsos como '-0.49 vs 2.00'.
    from modules.conciliacion.categorizar import categorizar as _categorizar_min

    def _es_comision_real(m: MovBanco) -> bool:
        try:
            cat = _categorizar_min(m.concepto or "", m.tipo or "")
            return (cat.grupo or "").upper() == "COMISION"
        except Exception:
            return False

    def _es_cheque_entrante(real: MovBanco, bk: MovBancsis) -> bool:
        """True si el match candidato es un cheque depositado (real C ↔ bk DE).

        La dueña pidió que los cheques se matcheen SOLO por suma del día
        (P1.5), nunca por cliente individual. Esto descarta P2/P3.5 para
        cheques pero los permite para TR (transferencias).
        """
        return (real.tipo or "").upper() == "C" and (bk.documento or "").upper() == "DE"

    # TMT 2026-05-29 dueña: 'en conciliar por documento no funciona, este
    # cheque con este documento no funciono'. Red de seguridad: lookup
    # DIRECTO de cheque.doc_banco → BANCSIS (vía chequextransaccion).
    # Independiente del STRING_AGG de cargar_bancsis (que tenía algunos
    # casos borde donde no surfaceaba el doc_banco). Cubre el caso:
    # cheque depositado, doc_banco cargado inline, extracto trae ese
    # mismo número → tiene que pegar sí o sí.
    refs_extracto: set[str] = set()
    for _m in movs_real_filtrados:
        _r = _norm_doc(_m.documento)
        if _r:
            refs_extracto.add(_r)
    doc_banco_a_bk: dict[str, list[int]] = _mapa_doc_banco_a_bancsis(no_banco, refs_extracto)
    bancsis_por_id: dict[int, MovBancsis] = {bk.id_transaccion: bk for bk in bancsis}

    movs_post_p0: list[MovBanco] = []
    for real in movs_real_filtrados:
        ref_real = _norm_doc(real.documento)
        if not ref_real:
            movs_post_p0.append(real)
            continue
        match_bk = None

        # ── 0a) Red de seguridad: lookup directo cheque.doc_banco ──
        # Si hay cheques con doc_banco=ref_real linkeados a BANCSIS, elegir
        # el primero disponible (mismo banco, no usado, tipo compatible).
        for tx_id in doc_banco_a_bk.get(ref_real, []):
            if tx_id in bancsis_usado:
                continue
            bk_candidato = bancsis_por_id.get(tx_id)
            if bk_candidato is None:
                continue  # estaba fuera del rango cargado o ya conciliado
            if not _es_tipo_compatible(real.tipo, bk_candidato.documento):
                continue
            match_bk = bk_candidato
            break

        if match_bk is None:
            for bk in bancsis:
                if bk.id_transaccion in bancsis_usado:
                    continue
                # TMT 2026-05-26 dueña: 'en conciliacion buscar doc id de banco
                # y doc is programa'. Comparamos extracto.documento contra
                # TODOS los doc-ids del programa: numreferencia + N° de cheques
                # ligados (vía chequextransaccion) + doc_banco editado inline
                # en /cheques. Cubre los 3 lugares donde un humano puede haber
                # guardado el comprobante banco.
                refs_bk = set()
                r1 = _norm_doc(bk.numreferencia)
                if r1:
                    refs_bk.add(r1)
                # no_cheque_rel puede ser "1234" o "1234,1235,..." (string_agg).
                for raw in (bk.no_cheque_rel or "").split(","):
                    rn = _norm_doc(raw)
                    if rn:
                        refs_bk.add(rn)
                # TMT 2026-05-27 dueña: 'ACABO DE METERLE EL NUMERO DE DOCUMENTO
                # A ESTA TRANSFERENCIA, VEAMOS QUE EL MATCH DE PERFECTO ACA'.
                # cheque.doc_banco (editable inline en /cheques) es lo que la
                # dueña carga cuando el banco le da un N° de comprobante.
                for raw in (bk.doc_banco_rel or "").split(","):
                    rn = _norm_doc(raw)
                    if rn:
                        refs_bk.add(rn)
                if ref_real not in refs_bk:
                    continue
                # Tipo compatible (no matchear un débito con un crédito por
                # coincidencia de referencia).
                if not _es_tipo_compatible(real.tipo, bk.documento):
                    continue
                match_bk = bk
                break
        if match_bk is not None:
            diff_dias = abs((real.fecha - match_bk.fecha).days)
            diff_monto = abs(float(real.monto) - match_bk.importe)
            razon = (
                f"P0·doc-banco {ref_real} · BANCSIS #{match_bk.id_transaccion}"
                + (f" · Δ{diff_dias}d ${diff_monto:.2f}" if (diff_dias or diff_monto > 0.01) else "")
            )
            res.matches.append(Match(real=real, bancsis=match_bk, score=0.0, razon=razon))
            bancsis_usado.add(match_bk.id_transaccion)
            cont_p0 += 1
        else:
            movs_post_p0.append(real)

    # ─── PASS 1: estricto (tipo + monto±tol + fecha±tol) ────────────────
    real_sin_match: list[MovBanco] = []

    for real in movs_post_p0:
        # TMT 2026-05-26 dueña: comisiones NUNCA van a Coinciden; salen
        # por el flow de agrupado por día. Las saltamos en P1/2/3/3.5/4.
        if _es_comision_real(real):
            real_sin_match.append(real)
            continue
        candidatos: list[tuple[float, MovBancsis]] = []
        for bk in bancsis:
            if bk.id_transaccion in bancsis_usado:
                continue
            if not _es_tipo_compatible(real.tipo, bk.documento):
                continue
            diff_monto = abs(float(real.monto) - bk.importe)
            if diff_monto > monto_tolerancia:
                continue
            diff_dias = abs((real.fecha - bk.fecha).days)
            if diff_dias > dias_tolerancia:
                continue
            score = diff_dias + diff_monto * 10
            candidatos.append((score, bk))
        if candidatos:
            # TMT 2026-05-26 dueña: "si hay mismo monto priorizar monto".
            # Si hay ≥1 candidato con monto EXACTO (diff < 1ct), descartar
            # los demás. Entre los de monto exacto, ganan los de fecha más
            # cercana. Sin esto, un candidato con monto cercano (no exacto)
            # + misma fecha podía ganarle a uno con monto exacto en otro día.
            exactos = [
                (s, bk) for (s, bk) in candidatos
                if abs(float(real.monto) - bk.importe) < 0.01
            ]
            if exactos:
                candidatos = exactos
            candidatos.sort(key=lambda t: t[0])
            score, bk = candidatos[0]
            diff_dias = abs((real.fecha - bk.fecha).days)
            diff_monto = abs(float(real.monto) - bk.importe)
            razon = (
                f"P1·exacto · BANCSIS #{bk.id_transaccion}"
                if (diff_dias == 0 and diff_monto < 0.01)
                else f"P1·probable · BANCSIS #{bk.id_transaccion} · Δ{diff_dias}d ${diff_monto:.2f}"
            )
            res.matches.append(Match(real=real, bancsis=bk, score=score, razon=razon))
            bancsis_usado.add(bk.id_transaccion)
            cont_p1 += 1
        else:
            real_sin_match.append(real)

    # ─── PASS 1.5: match GRUPAL N-a-1 por suma del día ──────────────────
    # TMT 2026-05-26 dueña: 'no matchees cada cheque con su cliente, tenemos
    # que matchear cuanto cheques total ese dia deposite cuanto dice el
    # deposito del banco'.
    # Para cada real (tipo C) sin match: buscar bancsis tipo C (DE/NC) del
    # mismo día que sumados EXACTO igualen el monto del depósito banco.
    # Si encuentra → match N-a-1 (1 fila banco ↔ N filas programa).
    # Usa ventana ±2d para tolerar viernes-lunes.
    real_sin_match_p15: list[MovBanco] = []
    for real in real_sin_match:
        if (real.tipo or "").upper() != "C":
            real_sin_match_p15.append(real)
            continue
        # Bancsis candidatos: tipo C (entrada), mismo día ±2d, no usados.
        candidatos = [
            bk for bk in bancsis
            if bk.id_transaccion not in bancsis_usado
            and bk.documento in _DOCS_CREDITO
            and real.fecha and bk.fecha
            and abs((real.fecha - bk.fecha).days) <= 2
        ]
        if len(candidatos) < 2:
            real_sin_match_p15.append(real)
            continue
        # Buscar SUBSET cuya suma sea EXACTA (±1ct) al monto del banco.
        # Greedy: ordenar por importe desc, sumar mientras no exceda.
        target = round(float(real.monto), 2)
        candidatos.sort(key=lambda b: -b.importe)
        subset: list[MovBancsis] = []
        suma = 0.0
        for bk in candidatos:
            if round(suma + bk.importe, 2) <= target + 0.01:
                subset.append(bk)
                suma = round(suma + bk.importe, 2)
                if abs(suma - target) < 0.01:
                    break
        if abs(suma - target) >= 0.01 or len(subset) < 2:
            # Greedy no llegó al exacto — intentamos TODOS los del día como ultimo recurso.
            mismo_dia = [bk for bk in candidatos if bk.fecha == real.fecha]
            if mismo_dia and abs(sum(bk.importe for bk in mismo_dia) - target) < 0.01:
                subset = mismo_dia
                suma = target
            else:
                real_sin_match_p15.append(real)
                continue
        # Match grupal: usar el primero como "representativo" (con razón),
        # componentes son todos los cheques ordenados de mayor a menor monto.
        if len(subset) >= 2 and abs(suma - target) < 0.01:
            principal = subset[0]
            # TMT 2026-05-26 dueña: 'monto de cada cheque ordenar de mayor a
            # menor'. Componentes son dicts completos con info para el UI.
            ordenado = sorted(subset, key=lambda b: -b.importe)
            componentes = [
                {
                    "id_transaccion": bk.id_transaccion,
                    "fecha": bk.fecha.isoformat() if bk.fecha else None,
                    "importe": float(bk.importe),
                    "concepto": bk.concepto,
                    "documento": bk.documento,
                    "prov": bk.prov,
                    "prov_nombre": bk.prov_nombre,
                    "no_cheque_rel": bk.no_cheque_rel,
                }
                for bk in ordenado
            ]
            razon = f"P1.5·grupo {len(subset)} cheques del {real.fecha:%d/%m} ${target:.2f}"
            res.matches.append(Match(
                real=real, bancsis=principal, score=0.0,
                razon=razon, componentes=componentes,
            ))
            for bk in subset:
                bancsis_usado.add(bk.id_transaccion)
            cont_p1 += 1
        else:
            real_sin_match_p15.append(real)
    real_sin_match = real_sin_match_p15

    # ─── PASS 2: cliente extraído + monto exacto (fecha cualquiera) ─────
    # Para los REAL sin match en P1, buscar BANCSIS con mismo CLIENTE
    # (código corto extraído del concepto vs prov de BANCSIS) y monto exacto.
    # Cubre el caso de drift de fecha grande con cliente conocido.
    aun_sin_match: list[MovBanco] = []
    for real in real_sin_match:
        # Skip comisiones (van al card agrupado por día).
        if _es_comision_real(real):
            aun_sin_match.append(real)
            continue
        codigo_concepto, _nombre = _extraer_cliente_concepto(real.concepto)
        if not codigo_concepto:
            aun_sin_match.append(real)
            continue
        candidatos: list[tuple[int, MovBancsis]] = []
        for bk in bancsis:
            if bk.id_transaccion in bancsis_usado:
                continue
            if not _es_tipo_compatible(real.tipo, bk.documento):
                continue
            # TMT 2026-05-26 dueña: NO matchear cheques cheque-por-cliente
            # individual. Los cheques solo van por P0 (doc) o P1.5 (suma día).
            if _es_cheque_entrante(real, bk):
                continue
            if abs(float(real.monto) - bk.importe) > monto_tolerancia:
                continue
            # Match por cliente: prov BANCSIS == código extraído del banco
            if (bk.prov or "").upper().strip() != codigo_concepto.upper():
                continue
            diff_dias = abs((real.fecha - bk.fecha).days)
            candidatos.append((diff_dias, bk))
        if candidatos:
            candidatos.sort(key=lambda t: t[0])
            diff_dias, bk = candidatos[0]
            diff_monto = abs(float(real.monto) - bk.importe)
            razon = f"P2·cliente {codigo_concepto} · BANCSIS #{bk.id_transaccion} · Δ{diff_dias}d ${diff_monto:.2f}"
            res.matches.append(Match(real=real, bancsis=bk, score=diff_dias + 100, razon=razon))
            bancsis_usado.add(bk.id_transaccion)
        else:
            aun_sin_match.append(real)

    # ─── PASS 3: monto EXACTO único (fecha cualquiera, sin cliente) ─────
    # Si para un REAL sin match hay exactamente 1 BANCSIS sin usar del mismo
    # monto + tipo, lo enlazamos. Si hay varios, sigue a P4 (match grupal).
    sin_match_pass3: list[MovBanco] = []
    for real in aun_sin_match:
        if _es_comision_real(real):
            sin_match_pass3.append(real)
            continue
        candidatos = []
        for bk in bancsis:
            if bk.id_transaccion in bancsis_usado:
                continue
            if not _es_tipo_compatible(real.tipo, bk.documento):
                continue
            # Skip cheques entrantes (van por P0/P1.5).
            if _es_cheque_entrante(real, bk):
                continue
            if abs(float(real.monto) - bk.importe) > 0.01:
                continue
            candidatos.append(bk)
        if len(candidatos) == 1:
            bk = candidatos[0]
            diff_dias = abs((real.fecha - bk.fecha).days)
            razon = f"P3·monto único · BANCSIS #{bk.id_transaccion} · Δ{diff_dias}d"
            res.matches.append(Match(real=real, bancsis=bk, score=diff_dias + 200, razon=razon))
            bancsis_usado.add(bk.id_transaccion)
            cont_p3 += 1
        else:
            sin_match_pass3.append(real)

    # ─── PASS 3.5: cliente + monto CERCANO (±10%, fecha ≤7d) ───────────
    # Para los reales con cliente extraído sin match: si hay un único
    # BANCSIS del mismo cliente con monto razonablemente cercano y fecha
    # cercana, los matcheamos. Cubre redondeos y cambios chicos de monto.
    aun_sin_match_p35: list[MovBanco] = []
    for real in aun_sin_match:
        # P3.5 ELIMINADO para cheques + comisiones (dueña 2026-05-26):
        # generaba ruido falsamente positivo. Solo se ejecuta para
        # TRANSFERENCIAS entrantes/salientes (TR/ND/NC).
        if _es_comision_real(real):
            aun_sin_match_p35.append(real)
            continue
        codigo_concepto, _ = _extraer_cliente_concepto(real.concepto)
        if not codigo_concepto:
            aun_sin_match_p35.append(real)
            continue
        candidatos = []
        for bk in bancsis:
            if bk.id_transaccion in bancsis_usado:
                continue
            if not _es_tipo_compatible(real.tipo, bk.documento):
                continue
            # Skip cheques entrantes (van por P0/P1.5).
            if _es_cheque_entrante(real, bk):
                continue
            if (bk.prov or "").upper().strip() != codigo_concepto.upper():
                continue
            diff_monto = abs(float(real.monto) - bk.importe)
            tope = max(5.0, float(real.monto) * 0.1)
            if diff_monto > tope:
                continue
            diff_dias = abs((real.fecha - bk.fecha).days) if (real.fecha and bk.fecha) else 99
            if diff_dias > 7:
                continue
            candidatos.append((diff_dias + diff_monto, bk))
        if len(candidatos) >= 1:
            candidatos.sort(key=lambda t: t[0])
            _, bk = candidatos[0]
            diff_dias = abs((real.fecha - bk.fecha).days)
            diff_monto = abs(float(real.monto) - bk.importe)
            razon = f"P3.5·cliente {codigo_concepto} ±10% · BANCSIS #{bk.id_transaccion} · Δ{diff_dias}d ${diff_monto:.2f}"
            res.matches.append(Match(real=real, bancsis=bk, score=diff_dias + 150, razon=razon))
            bancsis_usado.add(bk.id_transaccion)
            cont_p2 += 1  # contado como variante de P2
        else:
            aun_sin_match_p35.append(real)
    aun_sin_match = aun_sin_match_p35

    # ─── PASS 4: match GRUPAL por (tipo, monto) ─────────────────────────
    # Tamara 2026-05-23: "si hay 5 de 500 en banco y 5 de 500 en programa,
    # no hace falta asignar uno por uno". Si la cardinalidad coincide,
    # matcheamos arbitrariamente (cualquier asignación da la misma suma).
    from collections import defaultdict
    pendientes_real: dict[tuple, list[MovBanco]] = defaultdict(list)
    for real in sin_match_pass3:
        key = (real.tipo, round(float(real.monto), 2))
        pendientes_real[key].append(real)

    pendientes_bancsis: dict[tuple, list[MovBancsis]] = defaultdict(list)
    for bk in bancsis:
        if bk.id_transaccion in bancsis_usado:
            continue
        tipo_bk = "C" if bk.documento in _DOCS_CREDITO else "D" if bk.documento in _DOCS_DEBITO else "?"
        key = (tipo_bk, round(bk.importe, 2))
        pendientes_bancsis[key].append(bk)

    aun_sin_match_p4: list[MovBanco] = []
    for key, reals_grupo in pendientes_real.items():
        bks_grupo = pendientes_bancsis.get(key, [])
        # TMT 2026-05-26 dueña: P4 grupal arbitrario solo si N ≤ 3 pares.
        # Para N grandes (ej. 10+ cheques) el match arbitrario genera ruido —
        # mejor que vayan a sugerencias para que la dueña los matchee a mano.
        if len(reals_grupo) == len(bks_grupo) and 0 < len(reals_grupo) <= 3:
            # Match grupal arbitrario (FIFO por fecha cercana)
            reals_sorted = sorted(reals_grupo, key=lambda m: m.fecha or date.min)
            bks_sorted = sorted(bks_grupo, key=lambda b: b.fecha or date.min)
            for real, bk in zip(reals_sorted, bks_sorted):
                diff_dias = abs((real.fecha - bk.fecha).days) if (real.fecha and bk.fecha) else 0
                razon = f"P4·grupo {key[0]} ${key[1]} ({len(reals_grupo)} pares) · BANCSIS #{bk.id_transaccion}"
                res.matches.append(Match(real=real, bancsis=bk, score=diff_dias + 300, razon=razon))
                bancsis_usado.add(bk.id_transaccion)
                cont_p4 += 1
        else:
            aun_sin_match_p4.extend(reals_grupo)

    res.real_only = aun_sin_match_p4

    res.matches_por_pasada = {"P1": cont_p1, "P2": cont_p2, "P3": cont_p3, "P4": cont_p4}

    # BANCSIS sin match — primero recolectamos todos.
    bancsis_sin_match = [bk for bk in bancsis if bk.id_transaccion not in bancsis_usado]

    # ─── Excluir AGRUPADOS (Tamara 2026-05-23) ─────────────────────────
    # "Los 25 cheques agrupados no se comparan; los cheques uno por uno
    # ya están en el banco real". El mov agrupado del programa NO debe
    # aparecer en 'Solo en programa' — es un totalizador, no un mov
    # independiente comparable 1-a-1.
    res.bancsis_agrupados = [b for b in bancsis_sin_match if b.es_agrupado]
    res.bancsis_only = [b for b in bancsis_sin_match if not b.es_agrupado]

    # ─── SUGERENCIAS INLINE — más laxas (Tamara 2026-05-23) ─────────────
    # Para cada real_only, listar bancsis_only candidatos con cualquier
    # señal de match razonable:
    #   - mismo monto ±$5 + tipo compatible, O
    #   - mismo cliente extraído del concepto + tipo, O
    #   - monto cercano (±10%) + tipo + fecha ≤30d.
    # Ordenadas por "cercanía" (delta días + delta monto chico arriba).
    _sugerencias_por_real: dict[int, list[dict]] = {}
    for i, real in enumerate(res.real_only):
        codigo_concepto, _ = _extraer_cliente_concepto(real.concepto)
        codigo_concepto_up = codigo_concepto.upper() if codigo_concepto else ""
        candidatos = []
        seen_ids: set[int] = set()
        for bk in res.bancsis_only:
            if bk.id_transaccion in seen_ids:
                continue
            if not _es_tipo_compatible(real.tipo, bk.documento):
                continue
            diff_monto = abs(float(real.monto) - bk.importe)
            diff_dias = abs((real.fecha - bk.fecha).days) if bk.fecha and real.fecha else 99
            # Filtros de relevancia (OR)
            es_monto_cerca = diff_monto <= 5.0
            es_cliente_match = codigo_concepto_up and (bk.prov or "").upper().strip() == codigo_concepto_up
            es_monto_proporcional = diff_monto <= max(5.0, float(real.monto) * 0.1) and diff_dias <= 30
            if not (es_monto_cerca or es_cliente_match or es_monto_proporcional):
                continue
            # Score: monto exacto > cliente match > cercano. Diff días desempata.
            score = diff_dias + diff_monto * 10
            if es_cliente_match:
                score -= 50  # boost
            if diff_monto < 0.01:
                score -= 100  # boost monto exacto
            candidatos.append((score, {
                "id_transaccion": bk.id_transaccion,
                "fecha": bk.fecha.isoformat() if bk.fecha else None,
                "importe": float(bk.importe),
                "documento": bk.documento,
                "concepto": bk.concepto,
                "prov": bk.prov,
                "prov_nombre": bk.prov_nombre,
                "diff_dias": diff_dias,
                "diff_monto": round(diff_monto, 2),
            }))
            seen_ids.add(bk.id_transaccion)
        candidatos.sort(key=lambda t: t[0])
        _sugerencias_por_real[i] = [c for _s, c in candidatos[:10]]  # hasta 10
    res.sugerencias_real_only = _sugerencias_por_real

    # Saldos.
    if movs_real:
        # TMT 2026-05-28 dueña: el "Saldo banco según extracto" daba $0 con
        # fecha 2026-03-18. Causa: movs_real[-1] caía sobre un histórico
        # pendiente inyectado (saldo=0, fecha vieja). Los xlsx parseados
        # SIEMPRE traen saldo > 0; filtramos por saldo != 0 y tomamos el
        # de fecha máxima — saldo running real más reciente del banco.
        # TMT 2026-05-28 dueña: 'el ultimo balance del banco 2,797,649.59
        # porque entonces este Saldo banco según extracto al 2026-05-28
        # 2,796,057.90'. Cuando hay varias filas con la MISMA fecha máxima
        # (típico: muchos movs el día actual), el tiebreak por `id(m)` era
        # memory address — random — y caía sobre una fila intermedia. Fix:
        # usar el ÍNDICE de aparición en movs_real (el parser preserva el
        # orden del xlsx, que es running-balance), así max() devuelve la
        # ÚLTIMA fila del archivo dentro de la fecha máxima.
        try:
            from decimal import Decimal as _Dec
            reales_con_saldo = [
                (idx, m) for idx, m in enumerate(movs_real)
                if (m.saldo or 0) != _Dec("0")
            ]
        except Exception:
            reales_con_saldo = [
                (idx, m) for idx, m in enumerate(movs_real)
                if (m.saldo or 0) != 0
            ]
        if reales_con_saldo:
            _, ultimo = max(reales_con_saldo, key=lambda p: (p[1].fecha, p[0]))
        else:
            ultimo = movs_real[-1]  # fallback (no debería ocurrir en prod)
        res.saldo_real_final = ultimo.saldo
        res.saldo_real_fecha = ultimo.fecha
        # Buscar saldo BANCSIS al final del rango.
        sb = db.fetch_one(
            """
            SELECT saldo, fecha
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
               AND fecha <= %s
               AND saldo IS NOT NULL
             ORDER BY fecha DESC, id_transaccion DESC
             LIMIT 1
            """,
            (no_banco, hasta),
        )
        if sb:
            res.saldo_bancsis_final = float(sb.get("saldo") or 0)
            res.saldo_bancsis_fecha = sb.get("fecha")

    # Totales signados de los grupos solo.
    for m in res.real_only:
        sign = 1 if m.tipo == "C" else -1
        res.total_real_only_signed += sign * float(m.monto)
    for b in res.bancsis_only:
        sign = 1 if b.documento in _DOCS_CREDITO else -1
        res.total_bancsis_only_signed += sign * b.importe

    # ── Categorización (regex + AI fallback con cache) ─────────────────
    _adjuntar_categorias(res)

    return res


def _adjuntar_categorias(res: "ConciliacionBanco") -> None:
    """Calcula y adjunta categorías a los 3 grupos del resultado.

    Fail-graceful: si el módulo ai_categorizar revienta, usamos solo regex.
    """
    try:
        from modules.conciliacion.ai_categorizar import categorizar_con_ai
        usar_ai = True
    except Exception:
        from modules.conciliacion.categorizar import categorizar as categorizar_con_ai  # type: ignore
        usar_ai = False

    # TMT 2026-05-26 dueña: precarga del set de códigos válidos para validar
    # el cliente extraído del concepto. Si el código no existe en cliente/
    # proveedor/aliases, _extraer_cliente_concepto devuelve "" → no mostramos
    # ruido como "0HQ" que no corresponde a nadie.
    _codigos_validos = codigos_validos_set()

    def _to_cat(concepto: str, tipo: str) -> "Categorizado":
        # TMT 2026-05-26 dueña: SIEMPRE intentar extraer codigo + nombre
        # largo del concepto vía _extraer_cliente_concepto. Si AI también
        # devuelve "cliente" (vía extra), gana el de AI por ser más confiable.
        cod_reg, nombre_reg = _extraer_cliente_concepto(concepto, _codigos_validos)
        try:
            if usar_ai:
                cat, extra = categorizar_con_ai(concepto, tipo)
                return Categorizado(
                    codigo=cat.codigo, grupo=cat.grupo, label=cat.label,
                    abrev=getattr(cat, "abrev", "?"),
                    cliente=(extra.get("cliente") or cod_reg or ""),
                    cliente_nombre=(extra.get("descripcion") or nombre_reg or ""),
                    descripcion=extra.get("descripcion") or "",
                    fuente=cat.fuente,
                )
            cat = categorizar_con_ai(concepto, tipo)  # type: ignore
            return Categorizado(
                codigo=cat.codigo, grupo=cat.grupo, label=cat.label,
                abrev=getattr(cat, "abrev", "?"),
                cliente=cod_reg, cliente_nombre=nombre_reg,
                descripcion="", fuente=cat.fuente,
            )
        except Exception:
            return Categorizado(
                codigo="OTRO", grupo="OTRO", label="Sin categorizar",
                abrev="?", cliente=cod_reg, cliente_nombre=nombre_reg,
                descripcion="", fuente="error",
            )

    res.real_only_cats = [_to_cat(m.concepto, m.tipo) for m in res.real_only]
    res.bancsis_only_cats = [_to_cat(b.concepto, b.tipo_real) for b in res.bancsis_only]
    res.matches_cats = [_to_cat(m.real.concepto, m.real.tipo) for m in res.matches]


def confirmar_match(
    no_banco: int,
    real: MovBanco,
    id_transaccion: int | None,
    estado: str = "matched",
    usuario: str = "web",
    metodo: str = "matched_auto",
    conn=None,
) -> int:
    """Inserta un match (o aceptación unilateral) en banco_conciliacion_match.

    estado: 'matched' | 'real_only_ok' | 'bancsis_only_ok'.
    metodo: 'matched_auto' | 'matched_manual' | 'created_from_real' |
            'real_only_ok' | 'bancsis_only_ok'.

    Idempotente: el unique index (no_banco, real_fecha, real_documento, real_monto, real_tipo)
    WHERE deshecho_en IS NULL + ON CONFLICT DO NOTHING evita duplicados activos.
    Si la firma estaba conciliada y deshecha, se puede re-insertar.

    Si la migration 0047 no corrió todavía, omitimos la columna `metodo`.
    """
    # TMT 2026-05-27 dueña: 'que los que importen sean las de dbase'.
    # Dual-write: además del INSERT en match, marcamos stat='*' en la fila
    # PC para que el flag conciliado sea visible en /bancos y en los saldos
    # — el mismo flag que usa dBase. Así PC y dBase quedan visualmente
    # alineados. Si dBase vuelve a sincronizar con stat distinto, gana
    # dBase (la sync es one-way DBF → PC). Es el comportamiento querido.
    # TMT 2026-06-03: tx_firma se llena directo via scintela.compute_tx_firma()
    # SQL helper (mig 0068). Necesaria para sobrevivir el sync (mig 0066).
    if _tiene_migration_47():
        n = db.execute(
            """
            INSERT INTO scintela.banco_conciliacion_match (
                no_banco, estado, metodo,
                real_fecha, real_concepto, real_documento, real_monto, real_tipo,
                real_codigo, real_oficina,
                id_transaccion, tx_firma, usuario
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    scintela.compute_tx_firma(%s), %s)
            ON CONFLICT DO NOTHING
            """,
            (
                no_banco, estado, metodo,
                real.fecha, real.concepto, real.documento,
                real.monto, real.tipo,
                real.codigo, real.oficina,
                id_transaccion, id_transaccion, usuario,
            ),
            conn=conn,
        )
    else:
        # Fallback pre-migration: schema sin columna `metodo`.
        n = db.execute(
            """
            INSERT INTO scintela.banco_conciliacion_match (
                no_banco, estado,
                real_fecha, real_concepto, real_documento, real_monto, real_tipo,
                real_codigo, real_oficina,
                id_transaccion, tx_firma, usuario
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    scintela.compute_tx_firma(%s), %s)
            ON CONFLICT DO NOTHING
            """,
            (
                no_banco, estado,
                real.fecha, real.concepto, real.documento,
                real.monto, real.tipo,
                real.codigo, real.oficina,
                id_transaccion, id_transaccion, usuario,
            ),
            conn=conn,
        )
    # Dual-write: marca stat='*' en la fila PC (solo si es 'matched' y hay id).
    if estado == "matched" and id_transaccion:
        try:
            db.execute(
                """
                UPDATE scintela.transacciones_bancarias
                   SET stat = '*'
                 WHERE id_transaccion = %s
                   AND no_banco = %s
                """,
                (int(id_transaccion), no_banco),
                conn=conn,
            )
        except Exception:
            pass  # no romper el match si falla el stat update

    # TMT 2026-05-27 dueña: 'historicos pendientes deberian aparecer siempre
    # que hago la conciliacion, salvo que sean conciliados'. Si el `real`
    # que se confirmó proviene de un histórico pendiente (matched por firma
    # banco+fecha+documento+monto+tipo), marcarlo como conciliado y linkear
    # al match recién creado. Así no vuelve a inyectarse en próximos uploads.
    try:
        # Buscar el match recién insertado para obtener su id (no devolvimos lo).
        row_match = db.fetch_one(
            """
            SELECT id FROM scintela.banco_conciliacion_match
             WHERE no_banco = %s
               AND real_fecha = %s
               AND COALESCE(real_documento, '') = COALESCE(%s, '')
               AND real_monto = %s
               AND real_tipo = %s
               AND (deshecho_en IS NULL)
             ORDER BY id DESC
             LIMIT 1
            """,
            (no_banco, real.fecha, real.documento, real.monto, (real.tipo or '').upper()),
            conn=conn,
        )
        if row_match and row_match.get("id"):
            db.execute(
                """
                UPDATE scintela.banco_historicos_pendientes
                   SET conciliado_match_id = %s,
                       conciliado_en = CURRENT_TIMESTAMP,
                       conciliado_por = %s
                 WHERE no_banco = %s
                   AND fecha = %s
                   AND COALESCE(documento, '') = COALESCE(%s, '')
                   AND monto = %s
                   AND tipo = %s
                   AND conciliado_en IS NULL
                """,
                (
                    row_match["id"], usuario[:50] if usuario else 'web',
                    no_banco, real.fecha, real.documento, real.monto, (real.tipo or '').upper(),
                ),
                conn=conn,
            )
    except Exception:
        pass  # no romper el match si falla la marca de histórico
    return n


def confirmar_bancsis_only(
    no_banco: int,
    id_transaccion: int,
    usuario: str = "web",
) -> int:
    """Aceptar que un mov BANCSIS NO está en REAL (legítima diferencia)."""
    if _tiene_migration_47():
        return db.execute(
            """
            INSERT INTO scintela.banco_conciliacion_match
                (no_banco, estado, metodo, id_transaccion, tx_firma, usuario)
            VALUES (%s, 'bancsis_only_ok', 'bancsis_only_ok', %s,
                    scintela.compute_tx_firma(%s), %s)
            ON CONFLICT DO NOTHING
            """,
            (no_banco, id_transaccion, id_transaccion, usuario),
        )
    return db.execute(
        """
        INSERT INTO scintela.banco_conciliacion_match
            (no_banco, estado, id_transaccion, tx_firma, usuario)
        VALUES (%s, 'bancsis_only_ok', %s,
                scintela.compute_tx_firma(%s), %s)
        ON CONFLICT DO NOTHING
        """,
        (no_banco, id_transaccion, id_transaccion, usuario),
    )


def confirmar_real_only(
    no_banco: int,
    real: MovBanco,
    usuario: str = "web",
) -> int:
    """Aceptar que un mov REAL NO está en BANCSIS (legítima diferencia,
    sin crear la tx BANCSIS)."""
    return confirmar_match(
        no_banco=no_banco,
        real=real,
        id_transaccion=None,
        estado="real_only_ok",
        metodo="real_only_ok",
        usuario=usuario,
    )


# ─── Fase B (2026-05-23) — Crear tx BANCSIS desde un real_only ────────────


def _documento_bancsis_desde_tipo(tipo: str) -> str:
    """Mapea Tipo C/D del extracto al documento BANCSIS canónico.

    Real Tipo='C' (entrada) → 'DE' (depósito) por defecto. El usuario podrá
    cambiarlo después editando la tx si fuera TR, NC, etc.
    Real Tipo='D' (salida) → 'CH' (cheque emitido) por defecto.
    """
    t = (tipo or "").strip().upper()
    if t == "C":
        return "DE"
    if t == "D":
        return "CH"
    raise ValueError(f"Tipo banco desconocido: {tipo!r}")


def crear_transaccion_desde_real(
    no_banco: int,
    real: MovBanco,
    usuario: str = "web",
    documento: str | None = None,
    no_cta: str | None = None,
    prov: str | None = None,
    concepto_override: str | None = None,
) -> dict:
    """Crea una tx en BANCSIS a partir de un mov real_only y la deja conciliada.

    Atómico: insert tx + recompute saldos + insert match en una sola db.tx().
    Si la fila se inserta al medio (fecha pasada) dispara walk-forward para
    mantener `transacciones_bancarias.saldo` consistente.

    Args:
        no_banco: banco destino.
        real: el MovBanco del extracto que queremos materializar.
        usuario: para auditoría.
        documento: si querés forzar 'TR' o 'NC' en vez del default ('DE'/'CH').
        no_cta: cuenta opcional dentro del banco.
        prov: TMT 2026-05-26 dueña: código cliente/proveedor (≤5 chars) —
            se pasa a `transacciones_bancarias.prov`. Si viene vacío,
            `insert_movimiento_bancario` lo auto-extrae del concepto.
        concepto_override: TMT 2026-05-26 dueña: texto de concepto editado
            por la dueña en el modal (≤50 chars). Si viene vacío, hereda
            del extracto.

    Returns:
        {id_transaccion, saldo_nuevo, match_insertado}
    """
    import bank_helpers

    doc = (documento or _documento_bancsis_desde_tipo(real.tipo)).upper()
    concepto = (concepto_override or real.concepto or "")[:50] or f"Extracto {real.tipo} #{real.documento}"
    numref = None
    if real.documento:
        try:
            numref = int(str(real.documento).strip().lstrip("0") or "0")
        except ValueError:
            numref = None

    with db.tx() as conn:
        ins = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco,
            no_cta=no_cta,
            fecha=real.fecha,
            documento=doc,
            importe=float(real.monto),
            concepto=concepto,
            prov=(prov or "").strip().upper()[:5] or None,
            numreferencia=numref,
            usuario=usuario,
        )
        new_id = ins.get("id_transaccion")

        # Walk-forward: si la fila quedó al medio, recomputar saldos posteriores.
        bank_helpers.recompute_saldos_desde(
            conn,
            no_banco=no_banco,
            no_cta=no_cta,
            ancla_id=int(new_id),
        )

        # Persistir match en la misma tx.
        n = confirmar_match(
            no_banco=no_banco,
            real=real,
            id_transaccion=int(new_id) if new_id else None,
            estado="matched",
            usuario=usuario,
            metodo="created_from_real",
            conn=conn,
        )

    return {
        "id_transaccion": new_id,
        "saldo_nuevo": ins.get("saldo_nuevo"),
        "saldo_anterior": ins.get("saldo_anterior"),
        "documento": doc,
        "match_insertado": bool(n),
    }


def crear_transaccion_agrupada_desde_reals(
    no_banco: int,
    reals: list[MovBanco],
    *,
    fecha: date | None = None,
    concepto: str | None = None,
    prov: str | None = None,
    usuario: str = "web",
) -> dict:
    """Crea UNA tx BANCSIS con la suma de N reals y los concilia N:1.

    TMT 2026-05-26 dueña: cuando el extracto trae 10-30 comisiones/impuestos
    chicos, en lugar de crear 30 txs individuales creamos UNA sola con la
    suma y conciliamos todas las filas del extracto contra ese mismo
    `id_transaccion`. El historial conserva el desglose porque cada match
    apunta al `id_transaccion` compartido pero guarda el `real_*` original.

    Lógica:
        - Suma signada: créditos (+) − débitos (−). Si neto > 0 → documento
          'NC' (nota crédito, entra); si neto < 0 → 'ND' (nota débito, sale).
        - Importe en BANCSIS siempre positivo; el signo lo aplica el doc.
        - Fecha: la pasada por la dueña (default = última del lote).
        - Concepto: el pasado por la dueña (default = "Comisiones e
          impuestos DD/MM-DD/MM"); 50 chars máx.
        - prov: free text de la dueña ("PICH", "SRI", etc); 5 chars máx.

    Atómico en una sola db.tx(): insert + walk-forward + N matches.

    Args:
        no_banco: banco destino.
        reals: lista de MovBanco (mín 2). Mezclan C y D (los netea).
        fecha: fecha del mov BANCSIS. Default = max(reals[i].fecha).
        concepto: concepto del mov BANCSIS. Default auto.
        prov: cliente/proveedor a poner. Default vacío.
        usuario: para auditoría.

    Returns:
        {id_transaccion, saldo_nuevo, documento, n_matches, monto_neto}
    """
    if not reals:
        raise ValueError("reals vacío — necesito al menos 1 mov para agrupar.")
    import bank_helpers

    # Sumas signadas.
    suma_c = sum(float(r.monto) for r in reals if (r.tipo or "").upper() == "C")
    suma_d = sum(float(r.monto) for r in reals if (r.tipo or "").upper() == "D")
    neto = round(suma_c - suma_d, 2)
    if neto == 0:
        raise ValueError(
            "Los movs se compensan (créditos == débitos) — no genero tx "
            "BANCSIS de monto cero. Revisar el subset."
        )

    documento = "NC" if neto > 0 else "ND"
    importe_abs = abs(neto)
    fechas = [r.fecha for r in reals if r.fecha]
    fecha_tx = fecha or (max(fechas) if fechas else date.today())

    if not concepto:
        if fechas:
            fmin, fmax = min(fechas), max(fechas)
            if fmin == fmax:
                concepto = f"Comisiones e impuestos {fmin:%d/%m}"
            else:
                concepto = f"Comisiones e impuestos {fmin:%d/%m}-{fmax:%d/%m}"
        else:
            concepto = "Comisiones e impuestos agrupadas"
    concepto = concepto[:50]

    with db.tx() as conn:
        ins = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco,
            no_cta=None,
            fecha=fecha_tx,
            documento=documento,
            importe=importe_abs,
            concepto=concepto,
            prov=(prov or "").strip().upper()[:5] or None,
            numreferencia=None,
            usuario=usuario,
        )
        new_id = ins.get("id_transaccion")

        # Walk-forward — el mov puede caer al medio.
        bank_helpers.recompute_saldos_desde(
            conn,
            no_banco=no_banco,
            no_cta=None,
            ancla_id=int(new_id),
        )

        # Conciliar las N filas N:1 contra esta misma tx.
        # TMT 2026-05-29 E2E test descubrió que con conn compartida, si UNA
        # llamada a confirmar_match lanza excepción, las siguientes fallan
        # con "current transaction is aborted" pero el `except: pass` viejo
        # las silenciaba → solo 1 match grababa de N. Ahora usamos
        # SAVEPOINT por cada llamada: si una falla, rollback al savepoint
        # y seguimos con las siguientes sin contaminar la outer tx.
        n_matches = 0
        for i, real in enumerate(reals):
            sp_name = f"sp_match_{i}"
            try:
                # Savepoint dentro de la outer tx.
                db.execute(f"SAVEPOINT {sp_name}", conn=conn)
                try:
                    n = confirmar_match(
                        no_banco=no_banco,
                        real=real,
                        id_transaccion=int(new_id) if new_id else None,
                        estado="matched",
                        usuario=usuario,
                        metodo="created_from_real_grouped",
                        conn=conn,
                    )
                    if n:
                        n_matches += 1
                    db.execute(f"RELEASE SAVEPOINT {sp_name}", conn=conn)
                except Exception as e:
                    # Rollback al savepoint para no abortar la outer tx,
                    # loggear y continuar con el siguiente real.
                    db.execute(f"ROLLBACK TO SAVEPOINT {sp_name}", conn=conn)
                    _LOG.warning(
                        "confirmar_match para real (fecha=%s, monto=%s, doc=%s) "
                        "falló dentro de grupo tx=%s: %s",
                        real.fecha, real.monto, real.documento, new_id, e,
                    )
            except Exception as e:
                # Si ni el savepoint funciona, conn está completamente rota.
                # Abortamos el resto del loop — la outer tx hará rollback.
                _LOG.exception("SAVEPOINT/ROLLBACK falló — abortando: %s", e)
                raise

    return {
        "id_transaccion": new_id,
        "saldo_nuevo": ins.get("saldo_nuevo"),
        "saldo_anterior": ins.get("saldo_anterior"),
        "documento": documento,
        "n_matches": n_matches,
        "monto_neto": neto,
    }


# ─── Fase D (2026-05-23) — Match manual, romper match, historial, undo ────


def match_manual(
    no_banco: int,
    real: MovBanco,
    id_transaccion: int,
    usuario: str = "web",
) -> int:
    """Fuerza un match REAL ↔ BANCSIS sin pasar por el scorer.

    Usado desde el modal "Match manual" cuando el matcher no acertó
    (por ejemplo, drift de fecha > 5 días o monto > $1).
    """
    return confirmar_match(
        no_banco=no_banco,
        real=real,
        id_transaccion=int(id_transaccion),
        estado="matched",
        usuario=usuario,
        metodo="matched_manual",
    )


def romper_match(
    match_id: int,
    usuario: str = "web",
) -> int:
    """Marca una fila de banco_conciliacion_match como deshecha.

    Si la migration 0047 corrió: soft-undo (UPDATE deshecho_en).
    Si NO corrió: hard-delete (la fila desaparece — sin audit trail pero
    el mov vuelve a aparecer en el próximo upload).

    TMT 2026-05-28 dueña: 'PERO YO DESCONCILIE DESDE LA PAGINA, DEBERIAN
    VOLVER PARA ATRAS'. Bug fixeado: cuando se crea el match, dual-write
    setea `banco_historicos_pendientes.conciliado_en` (línea ~1251). Al
    deshacer el match había que revertir esa marca también — sino la
    histórica queda "fantasma-conciliada" y no vuelve a la lista de
    pendientes, generando drift sistemático.

    TMT 2026-05-28 dueña v2: 'estos dos depositos hicimos una conciliacion,
    los desconciliamos despues y siguen apareciendo como conciliados'. Bug
    extra: el dual-write también setea `transacciones_bancarias.stat='*'`
    (línea ~1228). Deshacer NO lo revertía → la fila quedaba con stat='*'
    y /bancos la mostraba conciliada vía el fallback dBase (queries.py
    línea ~187). Fix: limpiar stat='*' al deshacer, sólo si no queda otro
    match activo apuntando a la misma id_transaccion (defensa por si un mov
    tiene 2 matches PC apuntándolo — improbable pero no imposible).
    """
    # 0) Antes de marcar deshecho, agarrar la id_transaccion del match
    # para poder revertir stat='*' después.
    row_match = None
    try:
        row_match = db.fetch_one(
            "SELECT id_transaccion FROM scintela.banco_conciliacion_match WHERE id = %s",
            (int(match_id),),
        )
    except Exception:
        pass

    # 1) Limpiar la marca de histórico (si el match estaba apuntado por una).
    try:
        db.execute(
            """
            UPDATE scintela.banco_historicos_pendientes
               SET conciliado_en = NULL,
                   conciliado_por = NULL,
                   conciliado_match_id = NULL
             WHERE conciliado_match_id = %s
            """,
            (int(match_id),),
        )
    except Exception:
        pass  # fail-soft: si la columna no existe (pre-migration), seguimos

    # 2) Soft-undo o hard-delete del match propio.
    if _tiene_migration_47():
        n = db.execute(
            """
            UPDATE scintela.banco_conciliacion_match
               SET deshecho_en = CURRENT_TIMESTAMP,
                   deshecho_por = %s
             WHERE id = %s
               AND deshecho_en IS NULL
            """,
            (usuario[:50], int(match_id)),
        )
    else:
        n = db.execute(
            "DELETE FROM scintela.banco_conciliacion_match WHERE id = %s",
            (int(match_id),),
        )

    # 3) Revertir stat='*' en la fila PC si no queda otro match activo
    # apuntándola. El matcher excluye stat='*' del pool de conciliables, así
    # que al PC-conciliar la fila NO tenía '*' antes → limpiarlo es seguro.
    id_tx = row_match.get("id_transaccion") if row_match else None
    if id_tx:
        try:
            db.execute(
                """
                UPDATE scintela.transacciones_bancarias t
                   SET stat = NULL
                 WHERE t.id_transaccion = %s
                   AND TRIM(COALESCE(t.stat, '')) = '*'
                   AND NOT EXISTS (
                       SELECT 1 FROM scintela.banco_conciliacion_match m
                        WHERE m.id_transaccion = t.id_transaccion
                          AND m.deshecho_en IS NULL
                   )
                """,
                (int(id_tx),),
            )
        except Exception:
            pass  # fail-soft: no romper el deshacer si falla el revert de stat
    return n


def historial(
    no_banco: int | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    incluir_deshechos: bool = False,
    limit: int = 200,
) -> list[dict]:
    """Lista conciliaciones (matches + aceptaciones) para la vista de historial."""
    where = ["1=1"]
    params: list = []
    if no_banco is not None:
        where.append("bcm.no_banco = %s")
        params.append(int(no_banco))
    if desde is not None:
        where.append("(bcm.real_fecha >= %s OR bcm.creado_en::date >= %s)")
        params.extend([desde, desde])
    if hasta is not None:
        where.append("(bcm.real_fecha <= %s OR bcm.creado_en::date <= %s)")
        params.extend([hasta, hasta])
    tiene_47 = _tiene_migration_47()
    if not incluir_deshechos and tiene_47:
        where.append("bcm.deshecho_en IS NULL")
    params.append(int(limit))

    if tiene_47:
        select_extra = "bcm.metodo, bcm.deshecho_en, bcm.deshecho_por,"
    else:
        select_extra = "NULL::text AS metodo, NULL::timestamp AS deshecho_en, NULL::text AS deshecho_por,"

    rows = db.fetch_all(
        f"""
        SELECT bcm.id,
               bcm.no_banco,
               bcm.estado,
               {select_extra}
               bcm.real_fecha,
               bcm.real_concepto,
               bcm.real_documento,
               bcm.real_monto,
               bcm.real_tipo,
               bcm.id_transaccion,
               bcm.usuario,
               bcm.creado_en,
               tb.documento  AS bancsis_documento,
               tb.importe    AS bancsis_importe,
               tb.fecha      AS bancsis_fecha,
               tb.concepto   AS bancsis_concepto,
               tb.prov       AS codigo_cliente
          FROM scintela.banco_conciliacion_match bcm
          LEFT JOIN scintela.transacciones_bancarias tb
            ON tb.id_transaccion = bcm.id_transaccion
         WHERE {" AND ".join(where)}
         ORDER BY bcm.creado_en DESC, bcm.id DESC
         LIMIT %s
        """,
        tuple(params),
    ) or []
    return [dict(r) for r in rows]


def candidatos_match_manual(
    no_banco: int,
    fecha_real: date,
    monto_real: float,
    tipo_real: str,
    ventana_dias: int = 30,
    ventana_monto: float = 50.0,
    limit: int = 30,
) -> list[dict]:
    """BANCSIS filas candidatas para hacer match manual.

    Más laxo que el scorer: ±30 días, ±$50, mismo Tipo C/D. Ordenado por
    'cercanía' (suma absoluta de drift de fecha y monto, igual al scorer).
    """
    doc_filter_in = _DOCS_CREDITO if (tipo_real or "").upper() == "C" else _DOCS_DEBITO
    ya_excluido_clause = "AND deshecho_en IS NULL" if _tiene_migration_47() else ""

    rows = db.fetch_all(
        f"""
        SELECT tb.id_transaccion, tb.fecha, tb.documento, tb.concepto, tb.importe,
               tb.numreferencia, tb.prov, c.nombre AS prov_nombre,
               ABS(EXTRACT(DAY FROM tb.fecha - %s))::int AS diff_dias,
               ABS(tb.importe - %s) AS diff_monto
          FROM scintela.transacciones_bancarias tb
          LEFT JOIN scintela.cliente c ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(tb.prov))
         WHERE tb.no_banco = %s
           AND tb.fecha BETWEEN %s AND %s
           AND ABS(tb.importe - %s) <= %s
           AND UPPER(TRIM(tb.documento)) = ANY(%s)
           AND tb.id_transaccion NOT IN (
              SELECT id_transaccion
                FROM scintela.banco_conciliacion_match
               WHERE id_transaccion IS NOT NULL
                 {ya_excluido_clause}
           )
         ORDER BY diff_dias ASC, diff_monto ASC
         LIMIT %s
        """,
        (
            fecha_real,
            float(monto_real),
            int(no_banco),
            fecha_real - timedelta(days=ventana_dias),
            fecha_real + timedelta(days=ventana_dias),
            float(monto_real),
            float(ventana_monto),
            list(doc_filter_in),
            int(limit),
        ),
    ) or []
    return [
        {
            "id_transaccion": int(r["id_transaccion"]),
            "fecha": r["fecha"].isoformat() if r.get("fecha") else None,
            "documento": (r.get("documento") or "").strip(),
            "concepto": (r.get("concepto") or "").strip(),
            "importe": float(r.get("importe") or 0),
            "numreferencia": (r.get("numreferencia") or ""),
            "prov": (r.get("prov") or "").strip(),
            "prov_nombre": (r.get("prov_nombre") or "").strip(),
            "diff_dias": int(r.get("diff_dias") or 0),
            "diff_monto": float(r.get("diff_monto") or 0),
        }
        for r in rows
    ]
