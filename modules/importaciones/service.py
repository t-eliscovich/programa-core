"""Importaciones de Asinfo cruzadas contra las compras/anticipos del programa.

La lista "Importación" del ERP (Asinfo, vía Metabase) trae cada importación
con una `Nota` libre que termina con el código de la compra/anticipo del
programa: 2-3 letras (= `scintela.proveedor.codigo_prov`) + número
(= `scintela.compra.numero`). Ej: "ACMT/EXP/2026-27/8197 ( AC 36)".

Este módulo:
  1. Trae las importaciones de Asinfo (cantidad/fechas/total REFERENCIAL).
  2. Parsea el código de cada Nota (concepto_parser.parse_nota_importacion).
  3. Cruza ese código contra `scintela.compra` para traer el importe
     CONFIABLE en dólares (los dólares de Asinfo no son confiables).

Fail-soft: si Asinfo no responde devuelve []; si la DB del programa falla,
las importaciones igual se muestran sin el cruce.
"""
from __future__ import annotations

import logging
import re

import db
from concepto_parser import (
    anio_importacion,
    parse_nota_importacion,
    parse_ref_anticipo,
)
from modules.asinfo import service as asinfo_service

_LOG = logging.getLogger("programa_core.importaciones")


def _numeros_de(code: dict) -> list[int]:
    """Lista de números que cubre un código (soporta rangos 'MH 64-65')."""
    n = code.get("numero")
    if n is None:
        return []
    hasta = code.get("numero_hasta")
    if hasta and hasta >= n:
        return list(range(n, hasta + 1))
    return [n]


# ── Importaciones PARTIDAS (---1 / ---2) ────────────────────────────────────
# Asinfo parte una misma factura en DOS documentos `factura_proveedor` con la
# MISMA descripción y un sufijo `---1` / `---2`. Cada documento se lleva la
# MITAD de los kilos, pero el costo se carga una sola vez (en una de las dos).
#
# Dónde duele, medido el 31/07/2026 (/admin/health/hilado-stock-debug): la
# compra de MH 66 de julio son 104.894,95 US$. Contra UNA mitad (23.430 kg) da
# **4,477 US$/kg**, un número que no existe en el mercado; contra el grupo
# entero (24.300 + 23.430 = 47.730) da 2,198, que es "falta cargar plata" — y
# efectivamente falta el concepto 67. El 4,477 no era un precio caro: era media
# importación.
#
# La regla pasó por CINCO intentos. Los tres primeros murieron en el escritorio;
# los dos últimos los mató la medición contra las 632 importaciones reales
# (/admin/debug-grupos-partidas, 31/07/2026). Vale la pena dejar el registro,
# porque cada una parecía razonable:
#
#   1. agrupar por (prov, nº, año) → **AC 58**: IM-0000622 (2026-07-20,
#      "ACMT/EXP/2026-27/8368") e IM-0000527 (2026-02-11, "INV HY336-26-1") son
#      las dos AC 58 del mismo año y no son mitades de nada. Sumarlas bajaba el
#      $/kg de julio de 3,0271 a 2,3428 y el stock 284.772.
#   2. exigir el sufijo `---N` → **AC 19** no lo usa: sus dos mitades son
#      "ACMT/EXP/2026-27/7914 (--1--)" y "(--2--)", con el ordinal DENTRO del
#      paréntesis. 11.363,72 + 10.990,42 = 22.354,14, que es exactamente lo que
#      dice el dBase. Con la regla del sufijo se perdían esos 11.000 kg.
#   3. descalificar si la nota trae RANGO → mataba justo los casos buenos:
#      **AC 25** son IM-0000548 e IM-0000549, las dos "AC 25-26"
#      (24.492,24 + 24.494,24 = 48.986,48 ≈ los 48.988 del dBase). El rango no
#      es una señal de peligro: es parte del código que las dos mitades
#      COMPARTEN.
#   4. exigir MISMA fecha exacta → Asinfo fecha los dos documentos en días
#      distintos: **AC 88** a 68 días (2026-01-15 y 2026-03-24, mismos
#      19.812,48 kg cada uno, recibidos los dos el 2026-03-28), AC 23-24 a 39.
#      Y peor: **AI 62** tiene ---1/---2/---3 y sólo dos comparten fecha, así
#      que se sumaba una PARTE del grupo — el error que el plan quería evitar.
#   5. agrupar por (proveedor + nota base) a secas → **MH 68/69/70**: una sola
#      factura del proveedor ("INV HY3821-26") cubre TRES importaciones con
#      códigos distintos. No son mitades: sumarlas daba 71.880 kg donde iban
#      24.300. Lo mismo AI 23 con AI 28 bajo "AYF02748".
#
# Lo que queda, y por qué cada pieza está: el proveedor y la nota base dicen que
# es LA MISMA FACTURA; el código del programa dice que es LA MISMA MERCADERÍA
# (es lo que separa MH 68/69/70); la ventana de fechas evita pegar dos campañas
# que reusan un número de factura (el único caso a más de 68 días en toda la
# historia son 366, y es una nota sin código).
_SEP_ORDINAL_RE = re.compile(r"[-\s]+\d{0,2}\s*$")
_VENTANA_PARTIDA_DIAS = 120

# Banda económica razonable del hilado importado (US$/kg). Fuera de acá el dato
# está mal cargado, no es un precio. NO descalifica la agrupación (ver
# adjuntar_grupo_partidas): sólo avisa.
BANDA_USD_KG = (2.7, 3.4)


def _nota_base(nota) -> str:
    """`nota` → el número de factura del proveedor, sin adornos.

    Saca TODO lo que va entre paréntesis (ahí viven tanto el código del programa
    "( AI 11)" como el ordinal de la partida "(--1--)") y el separador final con
    su ordinal. Las seis notaciones que usa Asinfo caen todas en la misma base:

        "AYF02649 ( AI 11) ---1"                 → "AYF02649"
        "AYF02653 ( AI 15 ) ----2"               → "AYF02653"
        "ACMT/EXP/2026-27/7682 ( AC 88)-------2" → "ACMT/EXP/2026-27/7682"
        "ACMT/EXP/2026-27/7914 (--1--)  (AC 19)" → "ACMT/EXP/2026-27/7914"
        "HY3087-26-1 ( MH 66 -67)--2"            → "HY3087-26-1"
        "MTGE3753--- 2"                          → "MTGE3753"
    """
    txt = re.sub(r"\([^)]*\)", " ", str(nota or ""))
    txt = " ".join(txt.split()).strip()
    return _SEP_ORDINAL_RE.sub("", txt).strip().upper()


def _partida_de(nota) -> tuple[str, int | None]:
    """`nota` → (nota base, ordinal de la partida | None).

    El ordinal es informativo — se muestra, no se usa para decidir. AC 19 lo
    lleva dentro del paréntesis y por eso acá sale None, y aun así agrupa.
    """
    txt = re.sub(r"\([^)]*\)", " ", str(nota or ""))
    txt = " ".join(txt.split()).strip()
    m = re.search(r"-{2,}\s*(\d{1,2})\s*$", txt)
    return _nota_base(nota), (int(m.group(1)) if m else None)


def adjuntar_grupo_partidas(rows: list[dict]) -> None:
    """Muta cada importación agregando su GRUPO de partidas.

        grupo_id       — clave estable del grupo ("IM-0000548+IM-0000549")
        grupo_kg       — kg del grupo COMPLETO (= kg propio si no está partida)
        grupo_ims      — im_numero de los miembros, ordenados
        grupo_completo — False si el grupo se descartó (ver grupo_aviso)
        grupo_aviso    — por qué se descartó, o None

    La clave son TRES cosas, y cada una está por un caso real que se rompió sin
    ella (ver el bloque de arriba):

      · mismo `prov_cod_asinfo` + misma **nota base** → es la misma factura;
      · mismo **código del programa** (prov + nº + nº_hasta) → es la misma
        mercadería. Es lo único que separa MH 68 / 69 / 70, tres importaciones
        distintas bajo una sola factura "INV HY3821-26";
      · fechas dentro de `_VENTANA_PARTIDA_DIAS` → no pegar dos campañas que
        reusan un número de factura.

    **Por defecto cada importación es su propio grupo**: si algo no cierra, el
    número no cambia respecto de hoy.

    Descalificadores (cualquiera ⇒ no se suma y queda el aviso):
      1. el grupo tiene más de 3 miembros — nunca se vio uno así
      2. a algún miembro le falta el kg ⇒ **grupo incompleto**: una suma parcial
         que cambia en cada corrida es peor que el número de hoy (una mitad
         puede quedar fuera del tope de filas que devuelve Asinfo)
      3. los miembros se recibieron en MESES distintos — el número que esto
         alimenta es mensual, y meter en un mes kilos que llegaron en otro es
         justo el error que estamos arreglando

    IDEMPOTENTE sobre filas cacheadas: `grupo_kg` se recalcula siempre desde
    `kg`, nunca acumulando sobre el valor anterior. Las filas de
    `importaciones_asinfo()` se comparten entre llamadas dentro del TTL — una
    pasada que acumulara se aplicaría dos veces en la segunda visita.
    """
    filas = list(rows or [])
    for r in filas:
        im = str(r.get("im_numero") or "").strip()
        r["grupo_id"] = im
        r["grupo_kg"] = float(r.get("kg") or 0.0)
        r["grupo_ims"] = [im] if im else []
        r["grupo_completo"] = True
        r["grupo_aviso"] = None

    cand: dict[tuple, list[dict]] = {}
    for r in filas:
        base, suf = _partida_de(r.get("nota"))
        r["partida_n"] = suf
        if not base:
            continue
        cand.setdefault((
            str(r.get("prov_cod_asinfo") or "").strip().upper(),
            base,
            str(r.get("prov") or "").strip().upper(),
            r.get("numero"),
            r.get("numero_hasta"),
        ), []).append(r)

    for clave, miembros in cand.items():
        if len(miembros) < 2:
            continue  # una sola: no hay nada que sumar
        if clave[3] is None:
            continue  # sin código del programa nunca matchea una compra
        fechas = sorted(_to_date(m.get("fecha")) for m in miembros
                        if _to_date(m.get("fecha")))
        meses = {str(m.get("fecha_recepcion") or "")[:7] for m in miembros}
        aviso = None
        if len(miembros) > 3:
            aviso = f"el grupo tendría {len(miembros)} partidas (máx 3)"
        elif any(m.get("kg") in (None, "") for m in miembros):
            aviso = "grupo incompleto: a una partida le falta el kg"
        elif len(fechas) == len(miembros) and \
                (fechas[-1] - fechas[0]).days > _VENTANA_PARTIDA_DIAS:
            aviso = (f"las partidas están a {(fechas[-1] - fechas[0]).days} días "
                     f"(máx {_VENTANA_PARTIDA_DIAS}): puede ser otra campaña")
        elif len(meses) > 1:
            aviso = f"las partidas se recibieron en meses distintos: {sorted(meses)}"
        if aviso:
            for m in miembros:
                m["grupo_completo"] = False
                m["grupo_aviso"] = aviso
            continue
        ims = sorted(str(m.get("im_numero") or "").strip() for m in miembros)
        gid = "+".join(ims)
        gkg = round(sum(float(m.get("kg") or 0.0) for m in miembros), 2)
        for m in miembros:
            m["grupo_id"] = gid
            m["grupo_kg"] = gkg
            m["grupo_ims"] = list(ims)


def _buscar_compras(refs: set[tuple[str, int]]) -> list[dict]:
    """Filas CRUDAS de compra (con `fecha`) para las refs pedidas.

    El número de la Nota de Asinfo (ej. "AC 98") NO es `scintela.compra.numero`
    (ese campo va vacío en estas compras de importación) sino el **`concepto`**
    de la compra: para los proveedores de importación (Ariescope, More Human,
    Aartimpex…) el concepto es justo ese número ("98", "16", …). Por eso
    cruzamos por `(codigo_prov, concepto-numérico)`. Verificado contra
    /compras en vivo 2026-06-09.

    Devuelve la LISTA de filas (no agregadas): cada una con codigo_prov, ref_num,
    importe y `fecha`. La atribución a cada importación (por año / fecha más
    cercana) la hace `importaciones_con_cruce`, porque el nº de concepto se reusa
    entre años y no se puede sumar por (prov, nº) a secas.

    Una sola query a scintela.compra. Fail-soft: [] si no hay refs o la DB falla.
    """
    if not refs:
        return []
    provs = sorted({p for p, _ in refs})
    numeros = sorted({n for _, n in refs})
    try:
        rows = db.fetch_all(
            """
            SELECT id_compra,
                   UPPER(codigo_prov) AS codigo_prov,
                   NULLIF(regexp_replace(COALESCE(concepto, ''), '[^0-9]', '', 'g'), '')::int
                       AS ref_num,
                   numero, importe, tipo, comprobante, concepto,
                   TO_CHAR(fecha, 'YYYY-MM-DD') AS fecha
              FROM scintela.compra
             WHERE UPPER(codigo_prov) = ANY(%s)
               AND NULLIF(regexp_replace(COALESCE(concepto, ''), '[^0-9]', '', 'g'), '')::int
                   = ANY(%s)
            """,
            (provs, numeros),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("buscar_compras falló: %s", e)
        return []
    return [
        r for r in rows
        if r.get("ref_num") is not None and r.get("codigo_prov") is not None
    ]


def _buscar_anticipos(refs: set[tuple[str, int]]) -> list[dict]:
    """Filas CRUDAS de anticipos USD (con `fecha`) desde scintela.dolares.

    Muchas importaciones se pagan como ANTICIPO USD (crédito/adelanto) y viven
    en `scintela.dolares` (cuenta = código de proveedor, concepto = nº de
    importación + tags CAE/SALDO/MAPFRE/INI). Recién se vuelven compra al
    "Convertir a compra".

    NO agregamos por (cuenta, nº): el nº de concepto se REUSA cada año (hay un
    "AC 31" en 2024 y otro en 2026), así que sumar a secas mezcla importaciones
    de años distintos. Devolvemos cada partida con su `fecha` y la atribución a
    la importación correcta (por fecha más cercana) la hace
    `importaciones_con_cruce`. Verificado contra /dolares en vivo 2026-06-09.

    Fail-soft: [] si no hay refs o la DB falla.
    """
    if not refs:
        return []
    provs = sorted({p for p, _ in refs})
    numeros = sorted({n for _, n in refs})
    try:
        rows = db.fetch_all(
            r"""
            WITH d AS (
                SELECT UPPER(cta) AS cta, importe, concepto,
                       TO_CHAR(fecha, 'YYYY-MM-DD') AS fecha,
                       -- nº del concepto como PALABRA: "31 SALDO"→31 y "AC 95"→95
                       -- (antes anclado al inicio: se perdían los que llevan el código adelante)
                       NULLIF(substring(concepto FROM '\y(\d{1,6})\y'), '')::int AS ref_num
                  FROM scintela.dolares
                 -- SOLO anticipos VIVOS (mismo criterio que /dolares "solo vivos").
                 -- Al convertir a compra el anticipo pasa a st='B' (y 'X' si se
                 -- cancela) → deja de contar acá, igual que sale de /dolares. Así
                 -- el cruce coincide con /dolares y no infla el valor del stock.
                 WHERE UPPER(cta) = ANY(%s)
                   AND COALESCE(st, '') IN ('', ' ')
            )
            SELECT cta, ref_num, importe, fecha, concepto
              FROM d
             WHERE ref_num IS NOT NULL AND ref_num = ANY(%s)
            """,
            (provs, numeros),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("buscar_anticipos falló: %s", e)
        return []
    return [
        r for r in rows
        if r.get("ref_num") is not None and r.get("cta") is not None
    ]


def _to_date(s):
    """'YYYY-MM-DD…' → date | None (fail-soft)."""
    from datetime import date
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


_ATRIB_MAX_DIAS = 300  # ventana movimiento↔importación (< 365 = separa años)


def _anio_de(row: dict) -> dict:
    """{"anio", "de_campana"} de una importación, cacheado en la propia fila."""
    if "anio_im" not in row:
        a = anio_importacion(row.get("nota"), row.get("fecha"))
        row["anio_im"] = a["anio"]
        row["anio_im_campana"] = a["de_campana"]
    return {"anio": row.get("anio_im"), "de_campana": row.get("anio_im_campana")}


def _nearest_import(cands: list[dict], fecha_row, anio: int | None = None) -> dict | None:
    """De las importaciones que comparten (prov, nº), la de fecha más cercana a
    la del movimiento (anticipo/compra), SOLO si cae dentro de la ventana.

    TMT 2026-07-29 (dueña): si el movimiento trae AÑO explícito (concepto
    `58/26`), el año MANDA y se aplica antes que la fecha:

      1. se descartan las importaciones de otro año → si no queda ninguna,
         devuelve None (**sin match** es mejor que mal matcheado: es lo que
         frena la conversión automática contra la importación equivocada);
      2. si entre las del año hay alguna con la CAMPAÑA escrita en la nota,
         ésas ganan sobre las que sólo tienen el año de la fecha (el caso real
         AC 58: IM-0000622 "ACMT/EXP/2026-27" le gana a IM-0000527 "INV
         HY336-26-1", que estaba más cerca en fecha pero es otra cosa);
      3. entre las que quedan, la de fecha más cercana — sin la ventana de 300
         días, que ya no hace falta porque el año desambiguó.

    Sin año explícito, el comportamiento es el de siempre: la de fecha más
    cercana dentro de la ventana `_ATRIB_MAX_DIAS` (< 365 = separa años), y
    None si ninguna cae adentro (no se atribuye en vez de contaminar). Sin
    fecha en el movimiento, sólo se atribuye si hay una única candidata. Así
    los anticipos viejos, que todavía no tienen año, no cambian.
    """
    if anio is not None:
        mismo_anio = [c for c in cands if _anio_de(c)["anio"] == anio]
        if not mismo_anio:
            return None
        con_campana = [c for c in mismo_anio if _anio_de(c)["de_campana"]]
        finalistas = con_campana or mismo_anio
        if len(finalistas) == 1:
            return finalistas[0]
        fr_a = _to_date(fecha_row)
        if fr_a is None:
            return None  # varias del mismo año y sin fecha para desempatar
        mejor, mejor_d = None, None
        for c in finalistas:
            fi = _to_date(c.get("fecha"))
            if fi is None:
                continue
            d = abs((fi - fr_a).days)
            if mejor_d is None or d < mejor_d:
                mejor, mejor_d = c, d
        return mejor or finalistas[0]

    fr = _to_date(fecha_row)
    if fr is None:
        return cands[0] if len(cands) == 1 else None
    best = None
    best_d = None
    for c in cands:
        fi = _to_date(c.get("fecha"))
        if fi is None:
            continue
        d = abs((fi - fr).days)
        if best_d is None or d < best_d:
            best, best_d = c, d
    if best is None:  # ninguna candidata tenía fecha
        return cands[0] if len(cands) == 1 else None
    return best if best_d <= _ATRIB_MAX_DIAS else None


def candidatas_plausibles(cands: list[dict], fecha_row) -> list[dict]:
    """De las importaciones que comparten (prov, nº), las que la atribución SIN
    AÑO podría llegar a elegir para un movimiento fechado `fecha_row`: las que
    caen dentro de la ventana `_ATRIB_MAX_DIAS`.

    TMT 2026-07-30 (dueña): *"todo lo que sea H tiene recibido y tiene que
    pasarse automático"*. El AI 19 y el AI 25 estaban frenados por "el número se
    repite entre campañas" y el número se repetía… en **2024**: IM-0000246 (feb
    2024) contra IM-0000570 (may 2026). Contar TODAS las homónimas trata como
    ambiguo lo que la fecha ya desambiguó sola. Ambigüedad de verdad es la del
    AC 58 (IM-0000622 jul-2026 e IM-0000527 feb-2026, las dos a tiro del mismo
    anticipo): ésa sigue necesitando el año escrito.

    Sin fecha en el movimiento devuelve todas (el atribuidor sólo se anima
    cuando hay una única candidata, así que la cuenta sigue diciendo la verdad).
    """
    cands = list(cands or [])
    fr = _to_date(fecha_row)
    if fr is None:
        return cands
    out = []
    for c in cands:
        fi = _to_date(c.get("fecha"))
        if fi is not None and abs((fi - fr).days) <= _ATRIB_MAX_DIAS:
            out.append(c)
    return out


def grupos_plausibles(cands: list[dict], fecha_row) -> list[str]:
    """Los GRUPOS distintos que `candidatas_plausibles` podría elegir.

    TMT 2026-07-31. Una importación PARTIDA siempre da dos candidatas
    plausibles — pero son la misma mercadería. Contarlas por separado declara
    ambigua una conversión que no lo es, y la campanita pide *"poné el año"*,
    que no arregla nada porque las dos mitades tienen el mismo año. La
    ambigüedad de verdad (AC 58: dos campañas distintas) sigue dando 2 grupos.
    """
    vistos: list[str] = []
    for c in candidatas_plausibles(cands, fecha_row):
        gid = str(c.get("grupo_id") or c.get("im_numero") or "")
        if gid and gid not in vistos:
            vistos.append(gid)
    return vistos


def importaciones_con_cruce(limite: int = 400) -> list[dict]:
    """Importaciones de Asinfo enriquecidas con el código parseado y el cruce
    contra el programa: primero compra (`scintela.compra`), si no hay, anticipo
    USD (`scintela.dolares`).

    Cada fila agrega:
        codigo, prov, numero, numero_hasta
        compra            — None | {ids, first_id, importe_total, n, tipo}
        anticipo          — None | {importe_total, n}
        fuente            — 'compra' | 'anticipo' | None
        importe_programa  — float | None  (USD confiable del programa)
    """
    rows = asinfo_service.importaciones_asinfo(limite=limite)

    # Kg por importación (detalle Asinfo). Fail-soft: {} → columna vacía.
    try:
        kg_map = asinfo_service.importaciones_kg(limite=limite)
    except Exception:  # noqa: BLE001
        kg_map = {}
    for r in rows:
        im = str(r.get("im_numero") or "").strip()
        r["kg"] = kg_map.get(im)

    refs: set[tuple[str, int]] = set()
    for r in rows:
        code = parse_nota_importacion(r.get("nota"))
        r["codigo"] = code.get("codigo")
        r["prov"] = code.get("prov")
        r["numero"] = code.get("numero")
        r["numero_hasta"] = code.get("numero_hasta")
        if r["prov"] and r["numero"] is not None:
            for n in _numeros_de(code):
                refs.add((r["prov"], n))

    # Grupo de partidas ---1/---2. Va DESPUÉS de colgar kg y numero_hasta en
    # todas las filas, porque los dos son entrada de la agrupación.
    adjuntar_grupo_partidas(rows)

    # Índice (prov, nº) → importaciones que usan ese código, para atribuir cada
    # anticipo/compra a SU importación por fecha (el nº se reusa entre años).
    imports_by_ref: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        if r.get("prov") and r.get("numero") is not None:
            for n in _numeros_de(r):
                imports_by_ref.setdefault(
                    (str(r["prov"]).strip().upper(), n), []
                ).append(r)

    compras = _buscar_compras(refs)
    anticipos = _buscar_anticipos(refs)

    # Atribución: cada fila cruda (compra/anticipo) va a la importación de fecha
    # más cercana entre las que comparten su (prov, nº). Acumulamos por im_numero.
    comp_por_im: dict[str, list[dict]] = {}
    for c in compras:
        cands = imports_by_ref.get(
            (str(c.get("codigo_prov")).strip().upper(), int(c["ref_num"]))
        )
        if not cands:
            continue
        im = _nearest_import(cands, c.get("fecha"))
        if im is None:  # fuera de la ventana → no es de ninguna de estas IM
            continue
        comp_por_im.setdefault(im["im_numero"], []).append(c)

    ant_por_im: dict[str, list[dict]] = {}
    for a in anticipos:
        cands = imports_by_ref.get(
            (str(a.get("cta")).strip().upper(), int(a["ref_num"]))
        )
        if not cands:
            continue
        # TMT 2026-07-29: si el concepto trae el año ("58/26"), manda.
        anio_ant = parse_ref_anticipo(a.get("concepto")).get("anio")
        im = _nearest_import(cands, a.get("fecha"), anio=anio_ant)
        if im is None:  # otro año / fuera de la ventana → no es de estas IM
            continue
        ant_por_im.setdefault(im["im_numero"], []).append(a)

    for r in rows:
        r["compra"] = None
        r["anticipo"] = None
        r["fuente"] = None
        r["importe_programa"] = None
        if not (r.get("prov") and r.get("numero") is not None):
            continue
        im = str(r.get("im_numero") or "").strip()

        hits = comp_por_im.get(im, [])
        if hits:
            r["compra"] = {
                "ids": [h["id_compra"] for h in hits],
                "first_id": hits[0]["id_compra"],
                "importe_total": sum(float(h.get("importe") or 0) for h in hits),
                "n": len(hits),
                "tipo": (hits[0].get("tipo") or "").strip(),
                # Desglose por compra (fecha + monto) para el "+" que expande
                # los movimientos en /importaciones. Más nuevo arriba.
                "items": [
                    {"fecha": h.get("fecha"),
                     "importe": float(h.get("importe") or 0),
                     "id_compra": h.get("id_compra")}
                    for h in sorted(
                        hits, key=lambda x: str(x.get("fecha") or ""), reverse=True
                    )
                ],
            }
            r["fuente"] = "compra"
            r["importe_programa"] = r["compra"]["importe_total"]
            continue

        ahits = ant_por_im.get(im, [])
        if ahits:
            r["anticipo"] = {
                "importe_total": sum(float(h.get("importe") or 0) for h in ahits),
                "n": len(ahits),
                # Desglose de los anticipos de /dólares que forman este total —
                # para que el "Ver" muestre las N partidas (antes mostraba solo
                # los movimientos cargados por esta pantalla). Más nuevo arriba.
                "items": [
                    {"fecha": h.get("fecha"),
                     "importe": float(h.get("importe") or 0)}
                    for h in sorted(
                        ahits, key=lambda x: str(x.get("fecha") or ""), reverse=True
                    )
                ],
            }
            r["fuente"] = "anticipo"
            r["importe_programa"] = r["anticipo"]["importe_total"]

    # ── Recepción / anticipos (PC-only, migs 0104+0107+0113) ────────────────
    # Modelo v2 — TMT 2026-07-06 (dueña): "dejamos de predecir cuánto saldría".
    # Sin flujo "Pagar" ni costo estimado: RECIBIR mete los kg al stock, los
    # ANTICIPOS son MOVIMIENTOS (muchos por importación, nada se pisa, ND
    # automática en Pichincha) y el VALOR DEL STOCK de la importación =
    # Σ anticipos pagados. El RESTANTE se carga por /compras → posdat, que es
    # el pasivo real. Ver modules/importaciones/pago.py.
    from modules.importaciones import pago as _pago

    # Sólo las importaciones con código (prov+nº) entran al flujo PC → sólo
    # de ésas pedimos estado/movimientos (las sin código no tienen botones).
    ims: set[str] = {
        str(r.get("im_numero") or "").strip()
        for r in rows
        if r.get("im_numero") and r.get("prov") and r.get("numero") is not None
    }
    estados = _pago.estados_por_im(ims)
    movs_map = _pago.movimientos_por_im(ims)

    for r in rows:
        r["recibido_pc"] = False
        r["kg_recibidos"] = None
        r["fecha_recepcion_pc"] = None
        r["movimientos"] = []
        # Σ anticipos pagados = VALOR DEL STOCK de la importación (v2).
        r["anticipo_aplicado"] = 0.0
        if not (r.get("prov") and r.get("numero") is not None):
            continue
        im = str(r.get("im_numero") or "").strip()
        st = estados.get(im)
        if st:
            r["recibido_pc"] = bool(st["recibido_pc"])
            r["kg_recibidos"] = st["kg_recibidos"]
            r["fecha_recepcion_pc"] = st["fecha_recepcion_pc"]
            r["anticipo_aplicado"] = float(st["anticipo_aplicado"] or 0)
        movs = movs_map.get(im, [])
        r["movimientos"] = movs
        if movs:
            # Los movimientos son la fuente de verdad; el cache
            # anticipo_aplicado podría estar viejo justo tras la mig 0113.
            r["anticipo_aplicado"] = round(
                sum(float(m.get("monto") or 0) for m in movs), 2
            )
    return rows


def costo_hilado_recibido_mes(yy: int, mm: int, limite: int = 1000) -> dict:
    """Costo-a-la-fecha en USD del hilado RECIBIDO en el mes (yy, mm), tomado de
    NUESTRA base: anticipos (`scintela.dolares`) + compras (`scintela.compra`),
    cruzados por código+concepto y ATRIBUIDOS por año (fecha más cercana) para no
    arrastrar el mismo nº de concepto de otro año.

    Mismo universo de importaciones que `asinfo_service.hilado_recibido_mes`
    (recibidas por fecha de recepción). El $ se cuenta UNA sola vez por
    (prov, nº, año): las importaciones partidas (---1/---2) comparten el costo.
    Es "costo-a-la-fecha": crece solo a medida que cargan CAE/saldo/etc.

    `kg_con_costo` son los kg cuyo importe YA está cargado. Los kilos llegan
    desde Asinfo en el momento de la recepción y los dólares recién cuando la
    conversión crea la compra — entre medio (hoy: 8, 27 y 76 minutos) esos kilos
    quedarían entrando "gratis" al promedio ponderado del hilado y revaluarían
    TODO el stock hacia abajo, para rebotar cuando la plata aparece. Por eso la
    valuación usa `kg_con_costo` y no `kg`. TMT 2026-07-31.

    Returns:
        {"us": float, "kg": float, "kg_con_costo": float, "usd_kg": float|None}.
        Fail-soft: ceros.
    """
    _vacio = {"us": 0.0, "kg": 0.0, "kg_con_costo": 0.0, "usd_kg": None}
    try:
        rows = importaciones_con_cruce(limite=limite)
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper la vista
        return dict(_vacio)
    pref = f"{int(yy):04d}-{int(mm):02d}-"
    # Una pasada para juntar las filas del mes con su clave de grupo; el importe
    # vive a nivel (prov, nº, año) y las partidas ---1/---2 lo COMPARTEN, así que
    # "tiene costo" se decide por grupo, no por fila.
    filas: list[tuple] = []
    for r in rows or []:
        if not r.get("recibida"):
            continue
        if not str(r.get("fecha_recepcion") or "").startswith(pref):
            continue
        clave = (
            str(r.get("prov") or "").strip().upper(),
            r.get("numero"),
            str(r.get("fecha") or "")[:4],
        )
        filas.append((clave, float(r.get("kg") or 0.0), r.get("importe_programa")))

    grupos_con_costo = {c for c, _kg, imp in filas if imp}
    total_kg = sum(kg for _c, kg, _i in filas)
    kg_con_costo = sum(kg for c, kg, _i in filas if c in grupos_con_costo)

    total_us = 0.0
    vistos: set[tuple] = set()
    for clave, _kg, imp in filas:
        if not imp or clave in vistos:
            continue
        vistos.add(clave)
        total_us += float(imp)

    usd_kg = round(total_us / kg_con_costo, 4) if kg_con_costo else None
    return {
        "us": round(total_us, 2),
        "kg": round(total_kg, 2),
        "kg_con_costo": round(kg_con_costo, 2),
        "usd_kg": usd_kg,
    }


#: Desde qué fecha de compra cuentan los recargos tardíos. Tamara 05/09/2026:
#: *"no toques nada de julio y agosto, solo septiembre"*. Julio y agosto ya
#: cerraron con su $/kg; recalcularlos movería historia que nadie va a volver
#: a mirar con los mismos ojos.
RECARGOS_TARDIOS_DESDE = "2026-09-01"


def recargos_tardios_mes(yy: int, mm: int, limite: int = 1000) -> dict:
    """US$ de compras de hilo cargadas en el mes (yy, mm) cuya importación se
    RECIBIÓ en un mes anterior — plata que no entra a ningún $/kg sola.

    🚨 Tamara 05/09/2026. Cada importación tiene la compra grande el día que
    Asinfo la recibe y, días después, sus recargos (flete, seguro, CAE:
    ~2.600–3.600 + 124,20). `costo_hilado_recibido_mes` atribuye TODO el costo
    de la importación al MES DE RECEPCIÓN — bien mientras el recargo cae en el
    mismo mes. Cuando cae en el siguiente (AC 39 y MD 1 recibidas el 31/08,
    recargos el 05/09: 6.849,51), el mes de recepción ya cerró y el recargo no
    entra a ningún lado: salió del banco y el costo del hilo nunca lo ve. En
    agosto fueron 26.252 y en julio 18.255, todos los meses. Hasta hoy la única
    salida era apretar "Entrar al precio del hilo" en cada compra (mig 0241);
    *"debería ser automático, no tener que apretar"*.

    Acá se hace automático con la misma semántica que el botón: entra al $/kg
    del mes en que se cargó, como plata sin kilos (los kilos ya entraron en el
    mes de recepción). Las compras ya marcadas `al_precio_hilo` NO se cuentan
    (ya las suma `hilo_al_precio_mes`); las anuladas tampoco. Sólo compras con
    fecha ≥ `RECARGOS_TARDIOS_DESDE`.

    Fail-soft: {"us": 0, "n": 0, "ids": []} si Asinfo no contesta — el $/kg se
    queda como está, igual que el resto de la valuación.
    """
    _vacio = {"us": 0.0, "n": 0, "ids": [], "detalle": []}
    pref = f"{int(yy):04d}-{int(mm):02d}-"
    if pref[:7] < RECARGOS_TARDIOS_DESDE[:7]:
        return dict(_vacio)
    try:
        rows = importaciones_con_cruce(limite=limite)
    except Exception:  # noqa: BLE001 -- fail-soft
        return dict(_vacio)
    candidatos: dict[int, float] = {}
    nombres: dict[int, str] = {}
    for r in rows or []:
        if not r.get("recibida"):
            continue
        rec = str(r.get("fecha_recepcion") or "")[:7]
        if not rec or rec >= pref[:7]:
            continue                      # recibida este mes o después: no es tardío
        codigo = (str(r.get("codigo") or "").strip()
                  or f"{str(r.get('prov') or '').strip()} {r.get('numero')}".strip())
        for it in ((r.get("compra") or {}).get("items") or []):
            f = str(it.get("fecha") or "")[:10]
            if not f.startswith(pref) or f < RECARGOS_TARDIOS_DESDE:
                continue
            try:
                idc = int(it.get("id_compra"))
            except (TypeError, ValueError):
                continue
            candidatos[idc] = float(it.get("importe") or 0)
            nombres[idc] = codigo
    if not candidatos:
        return dict(_vacio)
    # Las anuladas y las que ya entran por el botón salen de la lista, contra
    # la base (los items del cruce no traen ni `stat` ni `al_precio_hilo`).
    try:
        vivas = db.fetch_all(
            """
            SELECT id_compra
              FROM scintela.compra
             WHERE id_compra = ANY(%s)
               AND UPPER(TRIM(COALESCE(tipo, ''))) = 'H'
               AND COALESCE(stat, '') NOT IN ('X', 'Y')
               AND NOT COALESCE(al_precio_hilo, false)
            """, (sorted(candidatos),)) or []
    except Exception as e:  # noqa: BLE001 -- fail-soft
        _LOG.warning("recargos_tardios_mes: no pude filtrar las compras (%s)", e)
        return dict(_vacio)
    ids = sorted(int(v["id_compra"]) for v in vivas)
    # `detalle` es lo que la traza guarda en la foto (`hilado_insumos`) para
    # poder decir después "entraron los recargos de AC 39 y MD 1", no sólo
    # "cambió el $/kg". Tamara 05/09: *"asegurate de tener todas las variables
    # del proceso trackeadas"*.
    detalle = [{"id_compra": i, "codigo": nombres.get(i, ""),
                "importe": round(candidatos[i], 2)} for i in ids]
    return {"us": round(sum(candidatos[i] for i in ids), 2), "n": len(ids),
            "ids": ids, "detalle": detalle}


def compras_hilado_recibidas_mes(yy: int, mm: int, limite: int = 1000) -> dict:
    """Tabla 'Compras hilado' del flujo, pero SOLO RECIBIDAS y por proveedor =
    IMPORTACIÓN + COMPRA LOCAL. MISMA fuente que la fila Ingresos del cuadro de
    movimientos, para que el TOTAL de la tabla coincida exacto con Ingresos.

    Dueña 2026-07-30: "en la tabla de compras mostrame también solo recibidas, así
    el número coincide". Antes la tabla salía de scintela.compra tipo H por fecha
    de carga (incluía no recibidas y no cuadraba con Ingresos).

      · Importaciones: importaciones_con_cruce() recibidas con fecha_recepción en el
        mes. kg = kg_map de Asinfo (MISMO que hilado_recibido_mes); $ =
        importe_programa contado UNA vez por (prov, nº, año) — igual que
        costo_hilado_recibido_mes (las partidas ---1/---2 comparten costo).
      · Locales: compras_locales_con_cruce() recibidas en el mes; $ por tarifario
        (kg × tarifa sin IVA), igual que hilado_local_recibido_mes.

    Devuelve {"filas": [{prov,kg,importe,ukg}], "total": {kg,importe}} ordenado por
    $ desc. Fail-soft: {"filas": [], "total": {"kg":0,"importe":0}}.
    """
    pref = f"{int(yy):04d}-{int(mm):02d}-"
    agg: dict[str, dict] = {}
    vistos: set[tuple] = set()  # (prov, nº, año) → no duplicar $ de partidas
    # -- Importaciones recibidas --
    try:
        rows = importaciones_con_cruce(limite=limite)
    except Exception:  # noqa: BLE001 -- fail-soft
        rows = []
    for r in rows or []:
        if not r.get("recibida"):
            continue
        if not str(r.get("fecha_recepcion") or "").startswith(pref):
            continue
        prov = str(r.get("prov") or "").strip().upper()
        if not prov:
            continue
        a = agg.setdefault(prov, {"kg": 0.0, "importe": 0.0})
        a["kg"] += float(r.get("kg") or 0.0)
        imp = r.get("importe_programa")
        if imp:
            clave = (prov, r.get("numero"), str(r.get("fecha") or "")[:4])
            if clave not in vistos:
                vistos.add(clave)
                a["importe"] += float(imp)
    # -- Compras locales recibidas --
    try:
        from modules.compras_locales import service as _loc
        lfilas = _loc.compras_locales_con_cruce(limite=200)
    except Exception:  # noqa: BLE001 -- fail-soft
        lfilas = []
    for f in lfilas or []:
        if not f.get("recibida"):
            continue
        if not str(f.get("fecha_recepcion") or "").startswith(pref):
            continue
        prov = str(f.get("prov") or "").strip().upper()
        if not prov:
            continue
        kg = float(f.get("kg") or 0.0)
        tar = f.get("tarifa")
        a = agg.setdefault(prov, {"kg": 0.0, "importe": 0.0})
        a["kg"] += kg
        a["importe"] += (kg * float(tar)) if tar else 0.0
    filas = [
        {"prov": p, "kg": v["kg"], "importe": v["importe"],
         "ukg": (v["importe"] / v["kg"] if v["kg"] else 0.0)}
        for p, v in agg.items()
    ]
    filas.sort(key=lambda r: r["importe"], reverse=True)
    return {
        "filas": filas,
        "total": {
            "kg": sum(v["kg"] for v in agg.values()),
            "importe": sum(v["importe"] for v in agg.values()),
        },
    }


def promedio_hilado_usd_kg(limite: int = 1000) -> float | None:
    """$/kg promedio ponderado del hilado importado, TODO de Programa Core:
    Σ(costo real = importe_programa: anticipos+compras) ÷ Σ(kg) de las
    importaciones RECIBIDAS y cruzadas. Es el costo con que se valúa la apertura
    del stock en el Flujo producción, sin depender del dBase.

    El $ se cuenta UNA vez por (prov, nº, año) — importaciones partidas comparten
    costo; los kg se suman de TODAS las filas recibidas. Fail-soft: None si Asinfo
    cae o no hay datos.

    TMT 2026-07-31 — el `continue` salteaba la fila entera cuando faltaba el
    importe. En una importación PARTIDA la mitad `---1` no tiene
    `importe_programa` (el costo vive en la `---2`), así que sus kilos se
    perdían mientras el $ del grupo se contaba entero: **el $/kg salía casi al
    doble**. El docstring decía "los kg se suman de todas las filas recibidas"
    y el código hacía otra cosa. Ahora la falta de importe saltea sólo el $.

    Nota: acá los kg se suman fila por fila a propósito. `grupo_kg` NO se usa —
    sumar el kg del grupo en cada mitad sería exactamente el doble conteo.
    """
    try:
        rows = importaciones_con_cruce(limite=limite)
    except Exception:  # noqa: BLE001
        return None
    total_us = 0.0
    total_kg = 0.0
    vistos: set[tuple] = set()
    # El $ del grupo sólo vale si el grupo aporta kilos: si ninguna de sus filas
    # está recibida no entra, y si entra, entra con TODOS sus kilos recibidos.
    for r in rows or []:
        if not r.get("recibida"):
            continue
        kg = float(r.get("kg") or 0.0)
        if kg:
            total_kg += kg
        imp = r.get("importe_programa")
        if not imp:
            continue
        clave = (str(r.get("prov") or "").upper(), r.get("numero"),
                 str(r.get("fecha") or "")[:4])
        if clave not in vistos:
            vistos.add(clave)
            total_us += float(imp)
    return round(total_us / total_kg, 4) if total_kg else None


def _index_importaciones_por_codigo(limite: int = 400) -> dict[tuple[str, int], list[dict]]:
    """{(prov_upper, numero) -> [importación rows]} desde Asinfo, con kg colgado.

    Cada importación trae im_numero, fecha, fecha_recepcion, recibida, kg. Se usa
    para atribuir por (código, fecha más cercana). Fail-soft: {} si Asinfo cae.
    """
    try:
        rows = asinfo_service.importaciones_asinfo(limite=limite)
        kg_map = asinfo_service.importaciones_kg(limite=limite)
    except Exception:  # noqa: BLE001 -- Asinfo/Metabase caído
        return {}
    # El kg y el rango se cuelgan de TODAS las filas antes de agrupar: la
    # agrupación necesita ver a los dos miembros de una partida, incluso si uno
    # de ellos no tiene código parseable.
    for r in rows or []:
        r["kg"] = kg_map.get(str(r.get("im_numero") or "").strip())
        _c = parse_nota_importacion(r.get("nota"))
        r["prov"] = _c.get("prov")
        r["numero"] = _c.get("numero")
        r["numero_hasta"] = _c.get("numero_hasta")
    adjuntar_grupo_partidas(rows)

    index: dict[tuple[str, int], list[dict]] = {}
    for r in rows or []:
        code = parse_nota_importacion(r.get("nota"))
        prov, numero = code.get("prov"), code.get("numero")
        if not prov or numero is None:
            continue
        for n in _numeros_de(code):  # soporta rangos "MH 64-65"
            index.setdefault((str(prov).strip().upper(), n), []).append(r)
    return index


def adjuntar_recepcion_asinfo(anticipos: list[dict], limite: int = 400) -> None:
    """Muta cada fila de anticipo (scintela.dolares) agregando la RECEPCIÓN de su
    importación de Asinfo: `im_numero`, `fecha_recepcion_im`, `kg_im`.

    Match por (cta, concepto-numérico) + importación de fecha más cercana (misma
    ventana de 300 días que importaciones_con_cruce, así el "31" de 2024 no se
    pega a la importación de 2026). El concepto del anticipo es tipo "31 SALDO":
    el prefijo numérico es el código. Fail-soft: si Asinfo cae, deja los campos en
    None y NUNCA rompe /dolares.
    """
    for a in anticipos or []:  # init siempre → columna estable aunque Asinfo caiga
        a["im_numero"] = None
        a["fecha_recepcion_im"] = None
        a["kg_im"] = None
        # TMT 2026-07-29: `ref` y `anio` salen del MISMO parser que usa el
        # cruce ("58/26 SALDO" → 58 + 2026; "31 SALDO" → 31 sin año).
        # Si alguien YA puso `anio_ref` en la fila (la COLUMNA Año de
        # modules/dolares/anio.py, que corre antes), esa manda: es el dato
        # explícito de la dueña, no una inferencia.
        _p = parse_ref_anticipo(a.get("concepto"))
        a["ref"] = _p["numero"]
        if a.get("anio_ref") is None:
            a["anio_ref"] = _p["anio"]
    if not anticipos:
        return
    index = _index_importaciones_por_codigo(limite=limite)
    if not index:
        return
    for a in anticipos:
        cta = (a.get("cta") or "").strip().upper()
        if not cta or a.get("ref") is None:
            continue
        cands = index.get((cta, int(a["ref"])))
        if not cands:
            continue
        # Con año explícito manda el año; sin año, ventana de 300 días.
        im = _nearest_import(cands, a.get("fecha"), anio=a.get("anio_ref"))
        if im is None:
            continue
        a["im_numero"] = im.get("im_numero")
        a["fecha_recepcion_im"] = im.get("fecha_recepcion")
        # kg del GRUPO: si la importación viene partida en ---1/---2, el
        # anticipo se pagó por la mercadería entera, no por media.
        a["kg_im"] = im.get("grupo_kg", im.get("kg"))
        a["grupo_id_im"] = im.get("grupo_id") or im.get("im_numero")
        a["grupo_ims_im"] = im.get("grupo_ims") or []


def kg_stock_por_compra(compras: list[dict], limite: int = 400) -> dict[str, float]:
    """{prov_upper -> kg total de Asinfo} para compras de importación.

    Cada compra trae `prov`, `ref` (nº del concepto) y `fecha`. El kg pertenece
    al STOCK (la importación de Asinfo), NO a la compra: muchas compras
    (SALDO/CAE/seguro) mapean a un solo stock, así que el kg se cuenta UNA vez
    por importación (dedup por im_numero) y se suma por proveedor. Atribución por
    fecha más cercana (ventana 300 días). Fail-soft: {} si Asinfo cae.
    """
    index = _index_importaciones_por_codigo(limite=limite)
    if not index:
        return {}
    por_prov: dict[str, float] = {}
    vistos: dict[str, set] = {}
    for c in compras or []:
        prov = str(c.get("prov") or "").strip().upper()
        ref = c.get("ref")
        if not prov or ref is None:
            continue
        cands = index.get((prov, int(ref)))
        if not cands:
            continue
        im = _nearest_import(cands, c.get("fecha"))
        if im is None:
            continue
        # Dedup por GRUPO, no por im_numero: una importación partida son dos
        # im_numero distintos que son la MISMA mercadería. Con dedup por
        # im_numero y kg de grupo, cada mitad sumaría el total → doble conteo.
        imn = im.get("grupo_id") or im.get("im_numero")
        seen = vistos.setdefault(prov, set())
        if imn in seen:  # muchas compras → un stock: el kg una sola vez
            continue
        seen.add(imn)
        por_prov[prov] = por_prov.get(prov, 0.0) + float(
            im.get("grupo_kg", im.get("kg")) or 0.0)
    return por_prov


def kg_hilado_faltantes_mes(compras: list[dict], limite: int = 400) -> dict:
    """kg de hilado que FALTAN en las compras H del mes porque viven en la
    IMPORTACIÓN (las BAP convertidas en PC quedan con kg=0 a propósito —
    regla dueña 2026-07-10: "el kg no vive en la compra"). El dBase en
    cambio graba KG+IMPORTE en la misma compra, así que cualquier cálculo
    que sume `compra.kg` (p.ej. el ponderado um_act del balance) queda
    CIEGO a estos kg y se infla (bug 2026-07-17: compra AC de 22.992 kg
    convertida en PC → tarifa hilado +0,036 → utilidad +83k).

    ⚠️ **Acá NO se duplican kilos.** El dedup es por `im_numero`, así que la
    única vía sería que dos compras del mismo (prov, nº) cayeran en
    importaciones distintas. Medido el 03/08 sobre 14 meses con
    `/admin/debug-kg-por-mes`: **cero casos**, incluidos los 9 códigos con dos
    compras (AC 25, AC 15, AC 16, MH 66, AC 92, AC 28, AI 14, AC 32, AI 15) —
    los nueve resuelven a UNA sola importación. Antes de "arreglar" una
    duplicación acá, mostrá un mes con `duplica_kilos = true`. Ver
    `docs/KG_HILADO_GAP_CERRADO_2026-08-03.md`.

    `compras`: filas {prov, ref, fecha, kg} de TODAS las compras H del mes
    (ref = parte numérica del concepto, como en /compras).

    Devuelve dict:
      kg         = total de kg a completar (desde las importaciones)
      sin_match  = filas con kg=0 que NO matchearon una importación con kg
                   (para advertir en el balance — "agarrar el error")
      disponible = False si Asinfo no contestó (fail-soft: kg=0)

    Reglas:
      · el kg de una importación se cuenta UNA sola vez;
      · si otra compra del mes YA trae kg>0 de esa importación (la
        principal, sincronizada del dBase), sus gemelas SALDO/CAE/seguro
        con kg=0 NO vuelven a sumar.
    """
    sin_kg = [c for c in (compras or []) if not float(c.get("kg") or 0)]
    out = {"kg": 0.0, "sin_match": list(sin_kg), "disponible": False}
    if not sin_kg:
        out["disponible"] = True
        return out
    index = _index_importaciones_por_codigo(limite=limite)
    if not index:
        return out
    out["disponible"] = True

    def _im_de(c: dict):
        prov = str(c.get("prov") or "").strip().upper()
        ref = c.get("ref")
        if not prov or ref is None:
            return None
        cands = index.get((prov, int(ref)))
        if not cands:
            return None
        return _nearest_import(cands, c.get("fecha"))

    # Importaciones que ya están representadas por una compra CON kg.
    vistos: set = set()
    for c in compras or []:
        if float(c.get("kg") or 0) > 0:
            im = _im_de(c)
            if im is not None:
                vistos.add(im.get("im_numero"))

    kg_total = 0.0
    sin_match: list[dict] = []
    for c in sin_kg:
        im = _im_de(c)
        im_kg = float((im or {}).get("kg") or 0)
        if im is None or im_kg <= 0:
            sin_match.append(c)
            continue
        imn = im.get("im_numero")
        if imn in vistos:  # muchas compras → un stock: el kg una sola vez
            continue
        vistos.add(imn)
        kg_total += im_kg
    out["kg"] = round(kg_total, 2)
    out["sin_match"] = sin_match
    return out


def kg_hilado_mes(compras: list[dict], limite: int = 400,
                  mes: str | None = None) -> dict:
    """Kg de hilado de las compras H del mes, contados UNA vez por importación
    (o por GRUPO de partidas ---1/---2). **Reemplaza a `SUM(compra.kg)`**, no lo
    completa — ésa es toda la diferencia con `kg_hilado_faltantes_mes`.

    Por qué el reemplazo y no la suma. `compra.kg` está mal por los dos lados a
    la vez, y las dos cosas viven en el mismo SUM:

      · **de más** — los recargos (CAE / flete / seguro) que el dBase grabó
        REPITEN los kg de la fila principal: AC 15, AC 16 y AC 25 aparecen dos
        veces con el mismo kg = **70.354 kg contados dos veces** (medido
        31/07/2026);
      · **de menos** — las importaciones partidas llevan la MITAD: AC 25 48.988
        reales contra 24.494 en PC, AC 19 22.354/10.990, MH 66 46.860/23.430,
        AI 11 = **70.570 kg que faltan**.

    `kg_hilado_faltantes_mes` no podía arreglar ninguna de las dos: sólo mira las
    compras con `kg == 0` (service.py, `sin_kg`), así que una fila con la mitad
    de los kilos nunca entraba, y una fila con los kilos repetidos tampoco.

    Los dos defectos se cancelan hasta 215 kg. Medido: arreglar sólo los recargos
    mueve el $/kg +0,1108 (stock +210.359); arreglar sólo las partidas −0,1112
    (stock −211.003); los dos juntos −0,0003 (stock −644). Por eso van en el
    mismo cambio: **cualquiera de los dos solo mueve la utilidad 210.000**.

    La regla que queda es la de la dueña del 2026-07-10, aplicada sin
    excepciones: *el kg no vive en la compra, vive en el stock*. Para toda compra
    cuyo (prov, nº) sea código de una importación, `compra.kg` se IGNORA y el kg
    sale de la importación, una vez por grupo. Las compras que no son de
    importación (hilo local: HY, EP — cruzan por RUC, no por nota) conservan su
    propio kg.

    `compras`: filas {prov, ref, fecha, kg} de TODAS las compras H del mes.

    Devuelve:
        kg          — el total a usar (ya incluye las compras locales)
        sin_match   — compras de código conocido que no pudieron atribuirse
        fuera_de_banda — grupos RECIBIDOS EN EL MES cuyo $/kg se sale de
                      BANDA_USD_KG: o falta plata por cargar, o faltan kilos
        avisos      — grupos descartados, con el motivo
        disponible  — False si Asinfo no contestó
        kg_compra_ignorado / kg_de_importacion / grupos — para el debug

    **Fail-soft**: si Asinfo no contesta devuelve el `SUM(compra.kg)` de siempre
    y `disponible=False`. El balance ya advierte cuando eso pasa.
    """
    compras = list(compras or [])
    out = {
        "kg": round(sum(float(c.get("kg") or 0) for c in compras), 2),
        "sin_match": [], "avisos": [], "fuera_de_banda": [], "disponible": False,
        "kg_compra_ignorado": 0.0, "kg_de_importacion": 0.0, "grupos": 0,
    }
    index = _index_importaciones_por_codigo(limite=limite)
    if not index:
        return out
    out["disponible"] = True

    total = 0.0
    ignorado = 0.0
    de_import = 0.0
    vistos: set = set()
    avisos: dict[str, str] = {}
    # Para la alarma de banda: el $/kg se mira por GRUPO (Σ importe de todas las
    # compras del grupo ÷ kg del grupo), nunca por fila. Una fila sola es un
    # recargo de CAE contra kg del grupo = 0,15 $/kg, y no significa nada.
    banda: dict[str, dict] = {}
    for c in compras:
        prov = str(c.get("prov") or "").strip().upper()
        ref = c.get("ref")
        kg_compra = float(c.get("kg") or 0)
        cands = index.get((prov, int(ref))) if (prov and ref is not None) else None
        if not cands:
            total += kg_compra  # compra local / sin importación: su kg manda
            continue

        # Es una compra de importación: su kg propio no cuenta, pase lo que pase.
        ignorado += kg_compra
        im = _nearest_import(cands, c.get("fecha"))
        # Dedup por GRUPO. Si no se pudo atribuir, por (prov, nº): los recargos
        # comparten (prov, nº) con su principal, así que igual se cuentan una
        # sola vez aunque la atribución falle.
        clave = (im or {}).get("grupo_id") or f"{prov}:{ref}"
        gkg = float((im or {}).get("grupo_kg") or 0.0)
        b = banda.setdefault(clave, {
            "codigo": f"{prov} {ref}", "kg": gkg, "importe": 0.0,
            "ims": (im or {}).get("grupo_ims") or [],
            "recibida_en_el_mes": str((im or {}).get("fecha_recepcion") or "")[:7],
        })
        b["importe"] += float(c.get("importe") or 0)

        if clave in vistos:
            continue
        vistos.add(clave)
        if im is None or gkg <= 0:
            # No hay de dónde sacarlo: queda el kg de la compra y se AVISA.
            # Si la compra SÍ matchea una importación pero Asinfo no entregó
            # los kg del detalle, se anota el nº: el balance distingue "falta
            # el dato en Asinfo/el puente" de "falta el código en el concepto"
            # y no manda a completar a mano lo que vive allá. TMT 2026-08-30.
            c_avisada = dict(c)
            if im is not None:
                c_avisada["im"] = str(im.get("im_numero") or "") or None
            out["sin_match"].append(c_avisada)
            total += kg_compra
            b["kg"] = kg_compra
            continue
        total += gkg
        de_import += gkg
        out["grupos"] += 1
        if im.get("grupo_aviso"):
            avisos[str(im.get("grupo_id"))] = str(im["grupo_aviso"])

    # La banda sólo se mira en las importaciones RECIBIDAS EN EL MES. Si la
    # mercadería llegó en junio y en julio sólo se cargó el CAE, el $/kg del mes
    # da 0,79 y no significa nada: la plata de la mercadería está en junio. Sin
    # este filtro la alarma gritaría todos los meses y nadie la miraría.
    mes = str(mes or "").strip()[:7]
    if not mes:
        meses = [str(c.get("fecha") or "")[:7] for c in compras if c.get("fecha")]
        mes = max(set(meses), key=meses.count) if meses else ""
    lo, hi = BANDA_USD_KG
    for b in banda.values():
        if b["kg"] <= 0 or b["importe"] <= 0:
            continue
        if mes and b.get("recibida_en_el_mes") and b["recibida_en_el_mes"] != mes:
            continue
        ukg = b["importe"] / b["kg"]
        if lo <= ukg <= hi:
            continue
        out["fuera_de_banda"].append({
            "codigo": b["codigo"], "usd_kg": round(ukg, 4),
            "kg": round(b["kg"], 2), "importe": round(b["importe"], 2),
            "ims": b["ims"],
        })
    out["fuera_de_banda"].sort(key=lambda d: -abs(d["usd_kg"] - (lo + hi) / 2))

    out["kg"] = round(total, 2)
    out["kg_compra_ignorado"] = round(ignorado, 2)
    out["kg_de_importacion"] = round(de_import, 2)
    out["avisos"] = [f"{k}: {v}" for k, v in sorted(avisos.items())]
    return out


def adjuntar_kg_asinfo_a_compras(compras: list[dict], limite: int = 400) -> None:
    """Muta cada compra agregando `kg_asinfo`: el kg de su importación de Asinfo
    (match por codigo_prov + concepto-numérico + fecha más cercana).

    Para las compras de importación (BAP) que quedan con kg 0 — el kg vive en el
    STOCK, acá se MUESTRA por referencia (dueña: "cuando se convierte a compra
    quiero ver los kg"). No escribe nada en la compra. Fail-soft: kg_asinfo=None
    si Asinfo cae o el concepto no matchea una importación.
    """
    for c in compras or []:
        c["kg_asinfo"] = None
    if not compras:
        return
    index = _index_importaciones_por_codigo(limite=limite)
    if not index:
        return
    for c in compras:
        prov = (c.get("codigo_prov") or "").strip().upper()
        m = re.search(r"\b(\d{1,6})\b", c.get("concepto") or "")
        if not prov or not m:
            continue
        cands = index.get((prov, int(m.group(1))))
        if not cands:
            continue
        im = _nearest_import(cands, c.get("fecha"))
        if im is None:
            continue
        # kg del GRUPO: las dos mitades de una partida son una sola mercadería.
        c["kg_asinfo"] = im.get("grupo_kg", im.get("kg"))
        c["im_grupo_ims"] = im.get("grupo_ims") or []
