"""Refresca el catálogo de costos de tintura (scintela.tinto_costos) desde el
DBF — TMT 2026-06-23 (dueña).

Contexto: la planilla /informes/tinto-carga valida el código contra
scintela.tinto_costos (color + $/kg). El catálogo se sembró una sola vez
(mig 0083) desde lo que había en scintela.tinto ESE día, que es "solo mes en
curso" → códigos viejos (LIF, AZU, MEN, RPA, GRC…) nunca entraron y la carga
los rechaza con "no está en el catálogo".

El maestro real del dBase es F:\\STAND\\COSTOS.DBF (vive en la fábrica). Este
módulo lo usa si viene en el tarball; si no, reconstruye el catálogo desde el
TINTO.DBF COMPLETO (costo = Σimporte/Σkg por código, color = el más reciente),
que es lo que da paridad con el dBase (allá IMPORTE = KG × COSTO).

Se engancha como paso post-sync en /admin/dbase-sync (igual que clientes-import
y posdat-reconcile): cuando la dueña hace "sync completo", el catálogo se
actualiza solo. NO pisa costos editados a mano (salvo que venga COSTOS.DBF, que
es la fuente autoritativa).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_read_dbf():
    """Carga _read_dbf del import_dbf REAL (paridad de encoding/lectura)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "import_dbf.py"
    spec = importlib.util.spec_from_file_location("_import_dbf_tinto_costos", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod._read_dbf


def _find(extract_dir: Path, *names: str) -> Path | None:
    want = {n.upper() for n in names}
    for p in extract_dir.iterdir():
        if p.is_file() and p.name.upper() in want:
            return p
    return None


def _catalogo_desde_costos(path: Path, read_dbf) -> dict[str, dict]:
    """COSTOS.DBF: maestro autoritativo. Auto-detecta campos cod/color/costo."""
    rows = read_dbf(path)
    if not rows:
        return {}
    cols = {k.upper(): k for k in rows[0].keys()}

    def pick(*cands):
        for c in cands:
            if c in cols:
                return cols[c]
        return None

    f_cod = pick("COD", "CODIGO", "CODE")
    f_color = pick("COLOR", "NOMBRE", "DESCRIP", "DESCRIPCION")
    f_costo = pick("COSTO", "PRECIO", "VALOR", "PCOSTO")
    if not f_cod:
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        cod = (str(r.get(f_cod) or "")).strip().upper()[:5]
        if not cod or cod == "MAN":
            continue
        color = (str(r.get(f_color) or "")).strip()[:30] if f_color else ""
        try:
            costo = round(float(r.get(f_costo) or 0), 4) if f_costo else 0.0
        except (TypeError, ValueError):
            costo = 0.0
        out[cod] = {"color": color, "costo": costo}
    return out


def _catalogo_desde_tinto(path: Path, read_dbf) -> dict[str, dict]:
    """TINTO.DBF completo: costo = Σimporte/Σkg, color = el más reciente."""
    rows = read_dbf(path)
    agg: dict[str, dict] = {}
    for r in rows:
        cod = (str(r.get("COD") or "")).strip().upper()[:5]
        if not cod or cod == "MAN":
            continue
        stat = (str(r.get("STAT") or "")).strip().upper()
        if stat in ("X", "Y"):
            continue
        try:
            kg = float(r.get("KG") or 0)
            imp = float(r.get("IMPORTE") or 0)
        except (TypeError, ValueError):
            kg = imp = 0.0
        d = agg.setdefault(cod, {"kg": 0.0, "imp": 0.0, "color": ""})
        d["kg"] += kg
        d["imp"] += imp
        color = (str(r.get("COLOR") or "")).strip()[:30]
        if color:
            d["color"] = color  # rows ya vienen en orden de archivo; gana el último no vacío
    out: dict[str, dict] = {}
    for cod, d in agg.items():
        if d["kg"] <= 0 or d["imp"] <= 0:
            # sin base para costo — igual lo dejamos con costo 0 para que exista
            out[cod] = {"color": d["color"], "costo": 0.0}
        else:
            out[cod] = {"color": d["color"], "costo": round(d["imp"] / d["kg"], 4)}
    return out


def refresh_from_dir(extract_dir, aplicar: bool = True):
    """Generador que loguea y refresca scintela.tinto_costos desde el DBF.

    Prefiere COSTOS.DBF (autoritativo: pisa color y costo). Si no está, usa
    TINTO.DBF en modo conservador: inserta códigos faltantes y rellena el color
    si está vacío, pero NO pisa costos ya cargados a mano.
    """
    import db

    extract_dir = Path(extract_dir)
    read_dbf = _load_read_dbf()

    costos_path = _find(extract_dir, "COSTOS.DBF")
    tinto_path = _find(extract_dir, "TINTO.DBF")

    if costos_path is not None:
        catalogo = _catalogo_desde_costos(costos_path, read_dbf)
        fuente = "COSTOS.DBF (maestro)"
        autoritativo = True
    elif tinto_path is not None:
        catalogo = _catalogo_desde_tinto(tinto_path, read_dbf)
        fuente = "TINTO.DBF (histórico — costo = Σimporte/Σkg)"
        autoritativo = False
    else:
        yield "  (no vino COSTOS.DBF ni TINTO.DBF — se omite el refresh del catálogo)"
        return

    if not catalogo:
        yield f"  ({fuente}: 0 códigos leídos — se omite)"
        return

    yield f"  fuente: {fuente} — {len(catalogo)} códigos"

    # Tabla puede no existir en DB fresca.
    if db.fetch_one("SELECT to_regclass('scintela.tinto_costos') AS t").get("t") is None:
        yield "  scintela.tinto_costos no existe (correr mig 0083 en /admin/migraciones) — se omite"
        return

    existentes = {
        r["cod"]: r
        for r in (db.fetch_all(
            "SELECT cod, COALESCE(color,'') AS color, COALESCE(costo,0) AS costo "
            "FROM scintela.tinto_costos"
        ) or [])
    }

    nuevos, color_rellenos, actualizados = 0, 0, 0
    for cod, info in sorted(catalogo.items()):
        ex = existentes.get(cod)
        if ex is None:
            nuevos += 1
            if aplicar:
                db.execute(
                    "INSERT INTO scintela.tinto_costos (cod, color, costo, usuario_crea) "
                    "VALUES (%s, %s, %s, 'dbf-sync') ON CONFLICT (cod) DO NOTHING",
                    (cod, info["color"], info["costo"]),
                )
        elif autoritativo:
            # COSTOS.DBF manda: pisar color y costo.
            if ex["color"] != info["color"] or float(ex["costo"]) != float(info["costo"]):
                actualizados += 1
                if aplicar:
                    db.execute(
                        "UPDATE scintela.tinto_costos "
                        "SET color=%s, costo=%s, fecha_modifica=CURRENT_TIMESTAMP, "
                        "    usuario_modifica='dbf-sync' WHERE cod=%s",
                        (info["color"], info["costo"], cod),
                    )
        else:
            # TINTO.DBF: conservador — solo rellenar color si está vacío.
            if not (ex["color"] or "").strip() and info["color"]:
                color_rellenos += 1
                if aplicar:
                    db.execute(
                        "UPDATE scintela.tinto_costos "
                        "SET color=%s, fecha_modifica=CURRENT_TIMESTAMP, "
                        "    usuario_modifica='dbf-sync' WHERE cod=%s",
                        (info["color"], cod),
                    )

    modo = "APLICADO" if aplicar else "DRY-RUN"
    yield (f"  [{modo}] catálogo: +{nuevos} nuevos, "
           f"{actualizados} actualizados, {color_rellenos} colores rellenados "
           f"(total previo {len(existentes)})")


# ---------------------------------------------------------------------------
# Importar TODOS los colores desde formulas_app (la app de tintorería).
# TMT 2026-06-29 (dueña): "que estén TODOS los colores de la app de tintura".
# El catálogo scintela.tinto_costos solo tenía ~118 códigos (COSTOS.DBF) y
# faltaban colores. formulas_app (formulas_db, read-only) es el maestro vivo
# de fórmulas/colores: tabla public.formulas (cod PK, color, categoria, grupo).
# Traemos TODA la lista y la upserteamos al catálogo, CONSERVADOR:
#   - inserta los códigos faltantes (costo = 0; formulas_app no tiene $/kg de
#     tela, el costo se carga después por COSTOS.DBF / a mano);
#   - rellena el color si en el catálogo está vacío;
#   - NUNCA pisa el costo ni un color ya cargado (no destruye ediciones del PC).
# El `cod` se normaliza a UPPER y se trunca a 5 chars, igual que la validación
# de /informes/tinto-carga (cod[:5].upper()) y la columna varchar(5), así el
# lookup de la planilla matchea exacto.
# ---------------------------------------------------------------------------

def _catalogo_desde_formulas_app() -> dict[str, dict]:
    """Lee public.formulas de formulas_app (bridge read-only) y arma el dict
    cod -> {color, costo}. Devuelve {} si el bridge no está disponible o falla
    (fail-soft: el host nunca se rompe por culpa del bridge)."""
    from modules._lib import formulas_db

    if not formulas_db.disponible():
        return {}
    rows = formulas_db.fetch_all(
        """
        SELECT cod,
               COALESCE(color, '')     AS color,
               COALESCE(categoria, '') AS categoria
          FROM formulas
         ORDER BY cod
        """
    )
    out: dict[str, dict] = {}
    for r in rows or []:
        cod = (str(r.get("cod") or "")).strip().upper()[:5]
        if not cod or cod == "MAN":
            continue
        color = (str(r.get("color") or "")).strip()
        categoria = (str(r.get("categoria") or "")).strip()
        # Si hay lugar, anexamos la categoría al color para dar contexto
        # (ej. "AZUL MARINO · Color Fuerte"), respetando el límite de 30.
        if color and categoria and len(color) + 3 + len(categoria) <= 30:
            color = f"{color} · {categoria}"
        color = color[:30]
        # formulas_app puede repetir cod tras el truncado a 5 — gana el primero
        # con color no vacío.
        if cod in out and not (color and not out[cod]["color"]):
            continue
        out[cod] = {"color": color, "costo": 0.0}
    return out


def _costos_por_cod_desde_materias():
    """Costo $/kg por código de color, reusando la pregunta canónica de Metabase
    'Detalle de Órdenes (costo materias)' (card 165 por defecto) — el MISMO costo
    que formulas_app calcula (receta x precios x relación de baño). Promedio
    PONDERADO por kg sobre las órdenes que devuelve. Devuelve {cod: costo_kg}.
    Fail-soft: {} si Metabase no está disponible. TMT 2026-06-29 (dueña: 'trae el
    costo tmbn, no costo 0')."""
    import os
    try:
        from modules._lib import metabase_client
    except Exception:
        return {}
    if not metabase_client.disponible():
        return {}
    card = os.environ.get("METABASE_CARD_COSTOS_MATERIAS", "165")
    try:
        rows = metabase_client.fetch_card(card)
    except Exception:
        return {}
    agg: dict[str, list] = {}
    for r in rows or []:
        cod = (str(r.get("Cod") or r.get("cod") or "")).strip().upper()[:5]
        if not cod:
            continue
        try:
            kg = float(r.get("Kg") or r.get("kg") or 0)
            ckg = float(r.get("u$/kg") or r.get("costo_kg") or 0)
        except (TypeError, ValueError):
            continue
        if kg <= 0 or ckg <= 0:
            continue
        a = agg.setdefault(cod, [0.0, 0.0])
        a[0] += kg
        a[1] += kg * ckg
    return {cod: round(c / kg, 4) for cod, (kg, c) in agg.items() if kg > 0}


def refresh_from_formulas_app(aplicar: bool = True):
    """Generador que importa TODOS los colores de formulas_app al catálogo
    scintela.tinto_costos. Conservador: inserta faltantes (costo 0), rellena
    color vacío; NO pisa costos ni colores ya cargados. Idempotente."""
    import db

    catalogo = _catalogo_desde_formulas_app()
    if not catalogo:
        yield ("  (bridge formulas_app no disponible o sin fórmulas — "
               "verificá FORMULAS_DATABASE_URL en el server; no se importó nada)")
        return

    yield f"  fuente: formulas_app (public.formulas) — {len(catalogo)} códigos"

    if db.fetch_one("SELECT to_regclass('scintela.tinto_costos') AS t").get("t") is None:
        yield "  scintela.tinto_costos no existe (correr mig 0083 en /admin/migraciones) — se omite"
        return

    existentes = {
        r["cod"]: r
        for r in (db.fetch_all(
            "SELECT cod, COALESCE(color,'') AS color, COALESCE(costo,0) AS costo "
            "FROM scintela.tinto_costos"
        ) or [])
    }

    costos_cod = _costos_por_cod_desde_materias()
    yield (f"  costos canónicos (Metabase 'costo materias'): {len(costos_cod)} códigos"
           if costos_cod else
           "  ⚠ no pude traer costos de Metabase (METABASE_* en el server) — "
           "los nuevos quedan en costo 0; cargalo por COSTOS.DBF o a mano")

    nuevos, color_rellenos, costo_rellenos = 0, 0, 0
    for cod, info in sorted(catalogo.items()):
        costo = float(costos_cod.get(cod, 0) or 0)
        ex = existentes.get(cod)
        if ex is None:
            nuevos += 1
            if aplicar:
                db.execute(
                    "INSERT INTO scintela.tinto_costos (cod, color, costo, usuario_crea) "
                    "VALUES (%s, %s, %s, 'formulas-app') ON CONFLICT (cod) DO NOTHING",
                    (cod, info["color"], costo),
                )
        else:
            # Conservador: rellenar color si está vacío y costo SOLO si está en 0
            # (no pisa costos ya cargados a mano / por COSTOS.DBF).
            sets, params = [], []
            if not (ex["color"] or "").strip() and info["color"]:
                sets.append("color=%s"); params.append(info["color"]); color_rellenos += 1
            if float(ex["costo"] or 0) == 0 and costo > 0:
                sets.append("costo=%s"); params.append(costo); costo_rellenos += 1
            if sets and aplicar:
                params.append(cod)
                db.execute(
                    f"UPDATE scintela.tinto_costos SET {', '.join(sets)}, "
                    "fecha_modifica=CURRENT_TIMESTAMP, usuario_modifica='formulas-app' "
                    "WHERE cod=%s",
                    tuple(params),
                )

    modo = "APLICADO" if aplicar else "DRY-RUN"
    yield (f"  [{modo}] catálogo: +{nuevos} nuevos, "
           f"{color_rellenos} colores rellenados, {costo_rellenos} costos rellenados "
           f"(total previo {len(existentes)}). Costo = promedio $/kg ponderado de "
           f"las órdenes (formulas_app); los que no tienen órdenes quedan en 0.")
