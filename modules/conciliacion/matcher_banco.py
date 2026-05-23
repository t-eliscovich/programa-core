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

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import db
from modules.conciliacion.parser_banco import MovBanco


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
    """Un movimiento REAL del banco emparejado con un MovBancsis."""

    real: MovBanco
    bancsis: MovBancsis
    score: float       # 0 = perfecto. Mayor = más drift.
    razon: str         # explicación legible


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
    cliente: str = ""      # extraído del concepto o por AI
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

_RE_NOMBRE_LARGO = _re.compile(
    r"(?:TRANSFERENCIA\s+(?:DIRECTA|INTERBANCARIA|INTERNA)?\s*"
    r"(?:DE|A|RECIBIDA\s+DE|ENVIADA\s+A)\s+|"
    r"DEP[OÓ]SITO\s+(?:DE|EFECTIVO\s+DE)\s+|"
    r"PAGO\s+(?:A|DE)\s+|"
    r"COBRO\s+(?:DE|A)\s+)"
    r"([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ \.\-]{4,})",
    _re.IGNORECASE,
)


def _extraer_cliente_concepto(concepto: str) -> tuple[str, str]:
    """Devuelve (codigo_corto, nombre_largo) extraídos del concepto.

    Cualquiera puede ser '' si no se detecta.

    >>> _extraer_cliente_concepto("1 ch.LTM")
    ('LTM', '')
    >>> _extraer_cliente_concepto("TRANSFERENCIA DIRECTA DE AGUILAR RUIZ JACQUELINE")[1]
    'AGUILAR RUIZ JACQUELINE'
    """
    if not concepto:
        return "", ""
    codigo = ""
    nombre = ""
    m = _RE_CODIGO_INTERNO.search(concepto)
    if m:
        codigo = m.group(1).upper().strip()
    m2 = _RE_NOMBRE_LARGO.search(concepto)
    if m2:
        nombre = " ".join(m2.group(1).strip().split())  # normalizar espacios
        # Sacar trailing tipo "DEL CARMEN ELIZABE" cortado por el banco — está OK
        nombre = nombre.upper()
    return codigo, nombre


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
    rows = db.fetch_all(
        """
        SELECT tb.id_transaccion, tb.fecha, tb.documento, tb.concepto, tb.importe,
               tb.numreferencia, tb.no_banco, tb.saldo, tb.prov
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
        # Cascada: explícito → código del concepto → nombre largo del concepto
        prov_resuelto = (prov_explicito or codigo_concepto or "").upper()
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
        ))
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
    monto_tolerancia: float = 5.0,
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

    # Ventana flexible: ±1d default, ±3d si última fecha es viernes.
    if dias_tolerancia is None:
        dias_tolerancia = _calcular_ventana_dias(fechas_real, default=1)

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

    # ─── PASS 1: estricto (tipo + monto±tol + fecha±tol) ────────────────
    bancsis_usado: set[int] = set()
    real_sin_match: list[MovBanco] = []
    cont_p1 = cont_p2 = cont_p3 = cont_p4 = 0

    for real in movs_real_filtrados:
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

    # ─── PASS 2: cliente extraído + monto exacto (fecha cualquiera) ─────
    # Para los REAL sin match en P1, buscar BANCSIS con mismo CLIENTE
    # (código corto extraído del concepto vs prov de BANCSIS) y monto exacto.
    # Cubre el caso de drift de fecha grande con cliente conocido.
    aun_sin_match: list[MovBanco] = []
    for real in real_sin_match:
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
        candidatos = []
        for bk in bancsis:
            if bk.id_transaccion in bancsis_usado:
                continue
            if not _es_tipo_compatible(real.tipo, bk.documento):
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
        if len(reals_grupo) == len(bks_grupo) and len(reals_grupo) > 0:
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
        # El "saldo final" del REAL es el saldo de la ÚLTIMA fila del xlsx
        # (ordenadas por fecha desc cronológicamente en el extracto). Tomamos
        # el saldo del registro de mayor fecha (y dentro de la misma fecha, el
        # último según orden en el archivo).
        ultimo = movs_real[-1]
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

    def _to_cat(concepto: str, tipo: str) -> "Categorizado":
        try:
            if usar_ai:
                cat, extra = categorizar_con_ai(concepto, tipo)
                return Categorizado(
                    codigo=cat.codigo, grupo=cat.grupo, label=cat.label,
                    abrev=getattr(cat, "abrev", "?"),
                    cliente=extra.get("cliente") or "",
                    descripcion=extra.get("descripcion") or "",
                    fuente=cat.fuente,
                )
            cat = categorizar_con_ai(concepto, tipo)  # type: ignore
            return Categorizado(
                codigo=cat.codigo, grupo=cat.grupo, label=cat.label,
                abrev=getattr(cat, "abrev", "?"),
                cliente="", descripcion="", fuente=cat.fuente,
            )
        except Exception:
            return Categorizado(
                codigo="OTRO", grupo="OTRO", label="Sin categorizar",
                abrev="?", cliente="", descripcion="", fuente="error",
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
    if _tiene_migration_47():
        return db.execute(
            """
            INSERT INTO scintela.banco_conciliacion_match (
                no_banco, estado, metodo,
                real_fecha, real_concepto, real_documento, real_monto, real_tipo,
                real_codigo, real_oficina,
                id_transaccion, usuario
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                no_banco, estado, metodo,
                real.fecha, real.concepto, real.documento,
                real.monto, real.tipo,
                real.codigo, real.oficina,
                id_transaccion, usuario,
            ),
            conn=conn,
        )
    # Fallback pre-migration: schema sin columna `metodo`.
    return db.execute(
        """
        INSERT INTO scintela.banco_conciliacion_match (
            no_banco, estado,
            real_fecha, real_concepto, real_documento, real_monto, real_tipo,
            real_codigo, real_oficina,
            id_transaccion, usuario
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            no_banco, estado,
            real.fecha, real.concepto, real.documento,
            real.monto, real.tipo,
            real.codigo, real.oficina,
            id_transaccion, usuario,
        ),
        conn=conn,
    )


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
                (no_banco, estado, metodo, id_transaccion, usuario)
            VALUES (%s, 'bancsis_only_ok', 'bancsis_only_ok', %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (no_banco, id_transaccion, usuario),
        )
    return db.execute(
        """
        INSERT INTO scintela.banco_conciliacion_match
            (no_banco, estado, id_transaccion, usuario)
        VALUES (%s, 'bancsis_only_ok', %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (no_banco, id_transaccion, usuario),
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

    Returns:
        {id_transaccion, saldo_nuevo, match_insertado}
    """
    import bank_helpers

    doc = (documento or _documento_bancsis_desde_tipo(real.tipo)).upper()
    concepto = (real.concepto or "")[:50] or f"Extracto {real.tipo} #{real.documento}"
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
    """
    if _tiene_migration_47():
        return db.execute(
            """
            UPDATE scintela.banco_conciliacion_match
               SET deshecho_en = CURRENT_TIMESTAMP,
                   deshecho_por = %s
             WHERE id = %s
               AND deshecho_en IS NULL
            """,
            (usuario[:50], int(match_id)),
        )
    # Fallback pre-migration: hard delete
    return db.execute(
        "DELETE FROM scintela.banco_conciliacion_match WHERE id = %s",
        (int(match_id),),
    )


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
               tb.concepto   AS bancsis_concepto
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
