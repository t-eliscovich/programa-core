"""AÑO de la importación de cada anticipo USD — TMT 2026-07-29 (dueña).

El número de importación se reusa cada campaña, así que el número solo no
alcanza para saber a qué importación pertenece un anticipo. El año vive acá,
en una tabla PC-only (`scintela.dolares_anio`, mig 0135) enganchada por FIRMA
en vez de por id, porque el sync del dBase hace TRUNCATE de `scintela.dolares`
y regenera los ids.

Firma = (fecha, cta, concepto normalizado, importe, orden). `orden` desempata
las filas idénticas dentro del mismo día — existen de verdad: AC tiene dos
"18 TRA" de $1.298,50.

Además de guardar y leer, este módulo arma la **evidencia** con lo que ya vive
en la base: el historial de anticipos del mismo número (3.180 movimientos desde
2023) y las importaciones de Asinfo con ese código. NO propone un año — la
dueña bajó la sugerencia el 29/07 porque para el AC 77 proponía 2025 cuando el
correcto era 2026. La pantalla muestra los datos; el año lo pone una persona.
"""
from __future__ import annotations

import logging

import db
from concepto_parser import parse_ref_anticipo

_LOG = logging.getLogger("programa_core.dolares.anio")


def _norm(concepto) -> str:
    return (str(concepto or "")).strip().upper()[:100]


def _firma(fila: dict) -> tuple:
    """(fecha, cta, concepto_norm, importe, orden) — estable ante el sync."""
    return (
        str(fila.get("fecha") or "")[:10],
        (fila.get("cta") or "").strip().upper()[:3],
        _norm(fila.get("concepto")),
        round(float(fila.get("importe") or 0), 2),
        int(fila.get("_orden") or 0),
    )


def marcar_orden(filas: list[dict]) -> None:
    """Numera 0,1,2… las filas con firma idéntica (mismo día, cta, concepto e
    importe). Ordena por id_dolares para que el orden sea reproducible."""
    grupos: dict[tuple, int] = {}
    for f in sorted(filas or [], key=lambda x: int(x.get("id_dolares") or 0)):
        clave = (
            str(f.get("fecha") or "")[:10],
            (f.get("cta") or "").strip().upper(),
            _norm(f.get("concepto")),
            round(float(f.get("importe") or 0), 2),
        )
        f["_orden"] = grupos.get(clave, 0)
        grupos[clave] = f["_orden"] + 1


def _clave_laxa(fecha, cta, importe) -> tuple:
    """Firma SIN el concepto — el pedazo que se mueve."""
    return (str(fecha or "")[:10], (cta or "").strip().upper()[:3],
            round(float(importe or 0), 2))


def adjuntar_anio(filas: list[dict]) -> None:
    """Muta cada fila agregando `anio_col` (el guardado) y `anio_ref` (el que
    manda: la columna si está, si no el `/26` del concepto).

    Fail-soft: si la tabla no existe todavía (migración sin correr) o la DB
    falla, deja `anio_col=None` y el año del concepto — o sea, exactamente el
    comportamiento anterior. NUNCA rompe /dolares.

    TMT 2026-07-31 (dueña: *"si se cargó este 11 que puse el año…"*): el AI 11
    tenía el año puesto y la celda salía VACÍA. La firma incluye el
    `concepto_norm`, y el concepto se mueve por abajo — lo edita el ✎ de la
    propia pantalla y lo revierte el TRUNCATE+INSERT del sync del dBase
    (DOLARES.DBF no tiene `delete_where`). Al cambiar el concepto la firma
    guardada deja de matchear y el año, que sigue en la tabla, se vuelve
    invisible: el freno del automático seguía diciendo "falta el año".

    Por eso el rescate LAXO: si la firma exacta no pega, se busca por
    (fecha, cta, importe) — el pedazo que NO se mueve — y sólo se usa cuando
    es **inequívoco**: exactamente UNA fila pendiente y exactamente UNA fila
    guardada sin usar con esa clave. Si hay gemelos, no se adivina.
    """
    if not filas:
        return
    marcar_orden(filas)
    for f in filas:
        f["anio_col"] = None
        f["anio_origen"] = None
        if "anio_ref" not in f:
            f["anio_ref"] = parse_ref_anticipo(f.get("concepto")).get("anio")
    for f in filas:
        if f.get("anio_ref"):
            f["anio_origen"] = "concepto"
    try:
        rows = db.fetch_all(
            """
            SELECT TO_CHAR(fecha, 'YYYY-MM-DD') AS fecha, UPPER(cta) AS cta,
                   concepto_norm, importe, orden, anio
              FROM scintela.dolares_anio
            """
        ) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("dolares_anio no disponible: %s", e)
        return
    mapa = {
        (r["fecha"], (r["cta"] or "").strip().upper(), r["concepto_norm"],
         round(float(r["importe"] or 0), 2), int(r["orden"] or 0)): int(r["anio"])
        for r in rows
    }
    usadas: set[tuple] = set()
    for f in filas:
        firma = _firma(f)
        a = mapa.get(firma)
        if a:
            f["anio_col"] = a
            f["anio_ref"] = a  # la columna MANDA sobre el `/26` del concepto
            f["anio_origen"] = "columna"
            usadas.add(firma)

    # ── rescate LAXO (sólo lo inequívoco) ──────────────────────────────────
    pendientes: dict[tuple, list[dict]] = {}
    for f in filas:
        if f.get("anio_col"):
            continue
        pendientes.setdefault(
            _clave_laxa(f.get("fecha"), f.get("cta"), f.get("importe")), []
        ).append(f)
    if not pendientes:
        return
    libres: dict[tuple, list[dict]] = {}
    for r in rows:
        firma = (r["fecha"], (r["cta"] or "").strip().upper(), r["concepto_norm"],
                 round(float(r["importe"] or 0), 2), int(r["orden"] or 0))
        if firma in usadas:
            continue
        libres.setdefault(
            _clave_laxa(r["fecha"], r["cta"], r["importe"]), []
        ).append(r)
    for clave, fs in pendientes.items():
        cands = libres.get(clave) or []
        if len(fs) != 1 or len(cands) != 1:
            continue  # gemelos: no se adivina
        f, a = fs[0], int(cands[0]["anio"])
        f["anio_col"] = a
        f["anio_ref"] = a
        f["anio_origen"] = "columna-rescatada"
        _LOG.info("año %s rescatado por firma laxa %s (concepto guardado %r ≠ %r)",
                  a, clave, cands[0]["concepto_norm"], _norm(f.get("concepto")))


def fila_por_id(id_dolares: int) -> dict:
    """Trae un anticipo con su `_orden` ya calculado contra sus gemelos.

    El `_orden` sale de comparar contra las filas de firma idéntica (mismo día,
    cuenta, concepto e importe) ordenadas por id_dolares — la misma cuenta que
    hace `marcar_orden` sobre la lista de la pantalla.
    """
    row = db.fetch_one(
        "SELECT id_dolares, TO_CHAR(fecha,'YYYY-MM-DD') AS fecha, cta, "
        "       concepto, importe, COALESCE(TRIM(st),'') AS st "
        "  FROM scintela.dolares WHERE id_dolares = %s",
        (int(id_dolares),),
    )
    if not row:
        raise ValueError(f"No encontré el anticipo id={id_dolares}.")
    gemelos = db.fetch_all(
        "SELECT id_dolares FROM scintela.dolares "
        " WHERE fecha = %s AND UPPER(TRIM(cta)) = %s "
        "   AND UPPER(TRIM(COALESCE(concepto,''))) = %s AND importe = %s "
        " ORDER BY id_dolares",
        (row["fecha"], (row["cta"] or "").strip().upper(),
         _norm(row["concepto"]), row["importe"]),
    ) or []
    ids = [int(g["id_dolares"]) for g in gemelos]
    row["_orden"] = ids.index(int(id_dolares)) if int(id_dolares) in ids else 0
    return row


def guardar(*, fila: dict, anio, usuario: str = "web",
            origen: str = "manual") -> dict:
    """Guarda (o borra, si anio es None/0) el año de un anticipo.

    `fila` tiene que traer fecha, cta, concepto, importe y `_orden` ya marcado
    por `marcar_orden` sobre el conjunto del día.
    """
    fecha, cta, conc, imp, orden = _firma(fila)
    if not fecha or not cta:
        raise ValueError("Anticipo sin fecha o sin cuenta.")
    if anio in (None, "", 0):
        db.execute(
            "DELETE FROM scintela.dolares_anio "
            " WHERE fecha = %s AND UPPER(cta) = %s AND concepto_norm = %s "
            "   AND importe = %s AND orden = %s",
            (fecha, cta, conc, imp, orden),
        )
        return {"anio": None}
    a = int(anio)
    if not (2000 <= a <= 2099):
        raise ValueError(f"Año fuera de rango: {anio}.")
    db.execute(
        """
        INSERT INTO scintela.dolares_anio
            (fecha, cta, concepto_norm, importe, orden, anio, origen,
             usuario_modifica, fecha_modifica)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (fecha, cta, concepto_norm, importe, orden)
        DO UPDATE SET anio = EXCLUDED.anio,
                      origen = EXCLUDED.origen,
                      usuario_modifica = EXCLUDED.usuario_modifica,
                      fecha_modifica = now()
        """,
        (fecha, cta, conc, imp, orden, a, origen[:20], (usuario or "web")[:50]),
    )
    return {"anio": a}


def mudar(*, fila_antes: dict, id_dolares: int, usuario: str = "web") -> int | None:
    """El concepto cambió: mueve el año guardado de la firma vieja a la nueva.

    `fila_antes` es la fila TAL COMO ESTABA (con su `_orden`) antes del UPDATE.
    Sin esto el año quedaba huérfano: la PK de `dolares_anio` incluye
    `concepto_norm` y el ✎ del concepto es justo donde se escribe el año.
    Devuelve el año mudado, o None si no había nada guardado.
    """
    fecha, cta, conc, imp, orden = _firma(fila_antes)
    row = db.fetch_one(
        "SELECT anio, origen FROM scintela.dolares_anio "
        " WHERE fecha = %s AND UPPER(cta) = %s AND concepto_norm = %s "
        "   AND importe = %s AND orden = %s",
        (fecha, cta, conc, imp, orden),
    )
    if not row:
        return None
    a = int(row["anio"])
    db.execute(
        "DELETE FROM scintela.dolares_anio "
        " WHERE fecha = %s AND UPPER(cta) = %s AND concepto_norm = %s "
        "   AND importe = %s AND orden = %s",
        (fecha, cta, conc, imp, orden),
    )
    guardar(fila=fila_por_id(id_dolares), anio=a, usuario=usuario,
            origen=(row.get("origen") or "manual"))
    return a


# ---------------------------------------------------------------------------
# EVIDENCIA (sin sugerir un año — dueña 29/07)
# ---------------------------------------------------------------------------

def _historial_por_ref(ctas: list[str]) -> dict[tuple[str, int], list[dict]]:
    """{(cta, nº) -> [movimientos del número, más nuevo primero]} de TODA la
    historia de scintela.dolares (viene del DOLARES.DBF: 3.180 filas desde
    2023, aplicados incluidos). Es la evidencia de si el ciclo anterior de ese
    número ya cerró."""
    ctas = sorted({(c or "").strip().upper() for c in ctas if c})
    if not ctas:
        return {}
    try:
        rows = db.fetch_all(
            r"""
            SELECT UPPER(cta) AS cta, concepto, importe,
                   TO_CHAR(fecha, 'YYYY-MM-DD') AS fecha,
                   COALESCE(TRIM(st), '') AS st,
                   NULLIF(substring(concepto FROM '\y(\d{1,6})\y'), '')::int AS ref
              FROM scintela.dolares
             WHERE UPPER(cta) = ANY(%s)
             ORDER BY fecha DESC
            """,
            (ctas,),
        ) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("historial de anticipos no disponible: %s", e)
        return {}
    out: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        if r.get("ref") is None:
            continue
        out.setdefault((r["cta"], int(r["ref"])), []).append(r)
    return out


def adjuntar_evidencia(filas: list[dict], index_importaciones=None) -> None:
    """Muta cada fila VIVA sin año agregando `anio_evidencia`.

    TMT 2026-07-29 (dueña): *"no pongas sugerencia entonces"*. Antes esto
    también proponía un año (`anio_sug`) y la pantalla lo ofrecía con un botón
    ✓. Se retiró: para el AC 77 sugería **2025** (Asinfo sólo tiene la
    importación de la campaña 2025-26) cuando el correcto era **2026** — lo
    prueba el historial, no la lista de importaciones. Una sugerencia que puede
    estar mal en el campo que existe para desambiguar es peor que ninguna.

    Queda la EVIDENCIA, que no propone un número: muestra los datos con los que
    se decide.
      · qué importaciones existen con ese código y de qué año;
      · si el ciclo anterior del número ya cerró (cuántos movimientos, cuánta
        plata, hasta cuándo) — que es lo que prueba que el anticipo vivo es del
        ciclo nuevo.
    """
    pendientes = [f for f in (filas or []) if not f.get("anio_col")]
    for f in filas or []:
        f.setdefault("anio_evidencia", "")
    if not pendientes:
        return
    hist = _historial_por_ref([f.get("cta") for f in pendientes])
    if index_importaciones is None:
        try:
            from modules.importaciones import service as _imp
            index_importaciones = _imp._index_importaciones_por_codigo()
        except Exception:  # noqa: BLE001 -- Asinfo caído: seguimos sin importaciones
            index_importaciones = {}

    for f in pendientes:
        cta = (f.get("cta") or "").strip().upper()
        ref = f.get("ref")
        if ref is None:
            ref = parse_ref_anticipo(f.get("concepto")).get("numero")
        if not cta or ref is None:
            continue

        ims = index_importaciones.get((cta, int(ref))) or []
        anios_im = []
        for im in ims:
            try:
                from modules.importaciones.service import _anio_de
                a = _anio_de(im).get("anio")
            except Exception:  # noqa: BLE001
                a = None
            if a:
                anios_im.append((a, im.get("im_numero"), im.get("fecha"),
                                 bool(im.get("recibida"))))

        # ── evidencia ──
        partes = []
        if anios_im:
            detalle = ", ".join(
                f"{n} ({a}{', recibida' if rec else ''})"
                for a, n, _fe, rec in sorted(anios_im, key=lambda x: str(x[2] or ""))
            )
            partes.append(f"Importaciones {cta} {ref}: {detalle}")
        else:
            partes.append(f"No hay ninguna importación {cta} {ref} en Asinfo")

        movs = hist.get((cta, int(ref))) or []
        previos = [
            m for m in movs
            if m.get("st") and str(m.get("fecha") or "") < str(f.get("fecha") or "")
        ]
        if previos:
            total = sum(float(m.get("importe") or 0) for m in previos)
            desde = min(str(m.get("fecha")) for m in previos)
            hasta = max(str(m.get("fecha")) for m in previos)
            partes.append(
                f"Ciclo anterior del nº {ref}: {len(previos)} movimiento(s) "
                f"por $ {total:,.2f}, aplicados entre {desde[8:10]}/{desde[5:7]}/"
                f"{desde[2:4]} y {hasta[8:10]}/{hasta[5:7]}/{hasta[2:4]}"
                + (" — cerrado" if abs(total) > 1 else " — quedó en cero (anulado)")
            )
        else:
            partes.append(f"El nº {ref} no tiene movimientos anteriores aplicados")
        f["anio_evidencia"] = " · ".join(partes)
