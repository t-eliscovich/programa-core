"""La LISTA DE PRECIOS de Asinfo, comparada con la de Programa Core.

TMT 2026-09-02, Tamara: *"si cambian los precios en asinfo tenemos que
cambiarlos en programa core… tener metodo de importar cambios"*. Hasta hoy la
comparación se hacía a mano (09/08/2026, hoja `Comparativa precios
Asinfo-dBase-Programa.xlsx`); esto la deja viva al pie de /precios.

Dónde vive la lista en Asinfo
-----------------------------
`lista_precios` (id 1, "Lista de precios por Tonos") → `version_lista_precios`
(una versión por cambio, con `valido_desde` / `valido_hasta`; la vigente es la
que no venció) → `detalle_version_lista_precios` (UN precio por PRODUCTO, o sea
por tela+color: ~19.600 filas). El tono va en `id_atributo_1 = 201` ("Tonos")
con `id_valor_atributo_1` → `valor_atributo.codigo` BLN/BJS/MDS/JSP/FRT — las
MISMAS cinco clases de Programa Core (más ESP, especiales, que acá no entra).

Cómo se pasa de 19.600 precios a las 60 celdas de Programa
---------------------------------------------------------
Programa tiene UNA columna por familia de tela; Asinfo separa por subcategoría
(Jersey 105, Jersey 1.2, Cuellos T28…). `MAPA` dice qué subcategorías caen en
cada columna, y por cada (columna, tono) se toma el precio MODAL (el que más
productos tienen), no el promedio — ver [[feedback_precio_modal_no_promedio]]:
en la lista conviven restos de cargas viejas (9,8174 al lado de 9,82) y
productos de relleno a 0,01.

🚨 Asinfo guarda NETO, igual que la base de Programa. La comparación se hace en
la escala que ve el usuario (c/IVA, 2 decimales): 7,94 y 7,9391 son el mismo
9,13. Al aplicar se escribe el neto de Asinfo con 4 decimales.

Decisión de Tamara (02/09): **revisar y confirmar** — la pantalla muestra las
diferencias y alguien con `precios.editar` tilda cuáles aplicar. Nada se pisa
solo. Lo que SÍ corre solo es el AVISO: cada hora se mira el número de versión
vigente y, si cambió, campanita.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time

import db

from . import queries

_LOG = logging.getLogger(__name__)

# Tono de Asinfo → clase de scintela.precios.
TONO_A_CLASE: dict[str, int] = {
    "BLN": 1, "BJS": 2, "MDS": 3, "JSP": 4, "FRT": 5,
}

# Columna de scintela.precios → subcategorías de Asinfo (categoría, subcategoría)
# que cotizan con esa escalera. Verificado contra la versión 554 (09/08/2026 y
# 02/09/2026). Las que NO están acá (Jersey Boca, Jersey Forro Spun, Piquetex,
# Rib Micro…) tienen escalera propia y Programa no las cotiza.
MAPA: dict[str, list[tuple[str, str]]] = {
    "jersey": [
        ("Jersey", "Jersey 105"), ("Jersey", "Jersey 1.2"), ("Jersey", "Jersey 110"),
        ("Jersey", "Jersey 2.4"), ("Jersey", "Jersey 2.6"), ("Jersey", "Jersey 3"),
        ("Jersey", "Jersey 3.2"), ("Jersey", "Jersey 3.5"), ("Jersey", "Jersey 3.8"),
        ("Jersey", "Jersey 4.2"), ("Jersey", "Jersey 95"),
        ("Jersey", "Jersey Forro"), ("Jersey", "Jersey Forro 1.2"),
        ("Jersey", "Jersey Listado"), ("Jersey", "Jersey Paris"),
    ],
    "rib": [("Rib", "Rib Normal"), ("Rib", "Rib Acanalado")],
    "pique": [("Pique", "Pique Especial"), ("Pique", "Pique Especial 200")],
    "cuellos": [
        ("Cuellos", "Cuellos"), ("Cuellos", "Cuellos Spun"),
        ("Cuellos", "Cuellos T28"), ("Cuellos", "Cuellos T30"),
        ("Cuellos", "Cuellos T32"), ("Cuellos", "Cuellos T34"),
        ("Cuellos", "Cuellos T36"), ("Cuellos", "Cuellos T38"),
        ("Cuellos", "Cuellos T40"), ("Cuellos", "Cuellos T42"),
        ("Cuellos", "Cuellos T44"), ("Cuellos", "Cuellos T46"),
        ("Cuellos", "Cuellos T48"),
        ("Puños", "Puños"), ("Puños", "Puños Spun"),
    ],
    "toper": [("Toper", "Toper"), ("Toper", "Toper 1.5"), ("Toper", "Toper 1.7")],
    "falso": [  # FLEECE — `falso` es el nombre de columna heredado de PRECIOS.DBF
        ("Fleece", "Fleece 96 Perchado"), ("Fleece", "Fleece 96 Sin Perchar"),
        ("Fleece", "Fleece 2.2"), ("Fleece", "Fleece 102"),
    ],
    # LYCRA de la hoja es FLEECE LYCRA (Alex, 03/09/2026): Jersey Lycra vale
    # un centavo menos en cada tono y era lo que seguía la columna hasta ese día.
    "lycra": [("Lycra", "Fleece Lycra")],
    "alemania": [("Poliester", "Alemania"), ("Poliester", "Alemania 1.2")],
    "kiana": [
        ("Poliester", "Kiana"), ("Poliester", "Kiana 1.2"),
        ("Poliester", "Kiana 415x90"), ("Poliester", "Kiana Forro"),
        ("Poliester", "Kiana Mundial"),
    ],
    "medical": [("Poliester", "Medical")],
    "micro": [("Poliester", "Microfibra"), ("Poliester", "Microfibra 1.2")],
    "james": [("Poliester", "James 1.2")],
}

# Telas de PRECIO ÚNICO (scintela.precio_plano.tela) → subcategoría de Asinfo.
# Se toma el modal juntando TODOS los tonos: en Asinfo estas valen lo mismo
# para todos. SUPLEX es Fleece Suplex, no Jersey Lycra Suplex (09/08/2026).
MAPA_PLANO: dict[str, list[tuple[str, str]]] = {
    "SCUBA": [("Lycra", "SCUBA")],
    "SUPLEX": [("Fleece", "Fleece Suplex")],
    "BELTIS": [("Poliester", "Beltis")],
    "NATY": [("Poliester", "Naty")],
}

# Un precio modal con menos apoyo que esto se muestra pero no se sugiere
# aplicar: puede ser un producto suelto cargado a mano.
PUREZA_MIN = 0.5

# Precio modal por (categoría, subcategoría, tono) de la versión VIGENTE.
# `precio_unitario > 0.01` saca los productos de relleno (491 a 0,01 en la 554).
_SQL_VIGENTE = """
WITH vig AS (
  SELECT TOP 1 id_version_lista_precios AS idv, valido_desde
    FROM version_lista_precios
   WHERE id_lista_precios = 1
     AND valido_desde <= CAST(GETDATE() AS date)
     AND (valido_hasta IS NULL OR valido_hasta >= CAST(GETDATE() AS date))
   ORDER BY valido_desde DESC, id_version_lista_precios DESC),
det AS (
  SELECT LTRIM(RTRIM(p.nombre_categoria_producto))    AS categoria,
         LTRIM(RTRIM(p.nombre_subcategoria_producto)) AS subcategoria,
         COALESCE(v.codigo, '-')                       AS tono,
         ROUND(d.precio_unitario, 2)                   AS precio,
         COUNT(*)                                      AS n
    FROM detalle_version_lista_precios d
    JOIN vig ON vig.idv = d.id_version_lista_precios
    JOIN producto p ON p.id_producto = d.id_producto
    LEFT JOIN valor_atributo v
      ON v.id_valor_atributo = d.id_valor_atributo_1 AND d.id_atributo_1 = 201
   WHERE d.precio_unitario > 0.01
   GROUP BY p.nombre_categoria_producto, p.nombre_subcategoria_producto,
            v.codigo, ROUND(d.precio_unitario, 2))
SELECT (SELECT idv FROM vig)          AS version,
       (SELECT valido_desde FROM vig) AS desde,
       categoria, subcategoria, tono, precio, n
  FROM det
"""

_SQL_VERSION = """
SELECT TOP 1 id_version_lista_precios AS version, valido_desde AS desde
  FROM version_lista_precios
 WHERE id_lista_precios = 1
   AND valido_desde <= CAST(GETDATE() AS date)
   AND (valido_hasta IS NULL OR valido_hasta >= CAST(GETDATE() AS date))
 ORDER BY valido_desde DESC, id_version_lista_precios DESC
"""


# ---------------------------------------------------------------------------
# Traer de Asinfo (caché SOLO del éxito — ver programa-core-integraciones)
# ---------------------------------------------------------------------------
_TTL_SECS = 600
_cache: dict = {}
_cache_lock = threading.Lock()


def _norm(s) -> str:
    return " ".join(str(s or "").split()).lower()


def traer_asinfo() -> tuple[list[dict], bool]:
    """Las filas crudas de `_SQL_VIGENTE`. `(filas, contestó)`."""
    from modules._lib import metabase_client as mc

    if not mc.disponible():
        return [], False
    filas, contesto = mc.fetch_dataset_estado(2, _SQL_VIGENTE, max_results=20000)
    return list(filas or []), bool(contesto)


def escalera_asinfo(filas: list[dict]) -> dict:
    """Agrupa las filas crudas en {(categoria, subcategoria): {tono: [(precio, n)]}}
    y devuelve además la versión y su fecha."""
    version = None
    desde = None
    por_sub: dict[tuple[str, str], dict[str, list[tuple[float, int]]]] = {}
    for r in filas:
        if version is None and r.get("version") is not None:
            version = int(r["version"])
            desde = str(r.get("desde") or "")[:10]
        key = (_norm(r.get("categoria")), _norm(r.get("subcategoria")))
        tono = str(r.get("tono") or "-").upper()
        try:
            precio = round(float(r.get("precio")), 2)
            n = int(r.get("n") or 0)
        except (TypeError, ValueError):
            continue
        por_sub.setdefault(key, {}).setdefault(tono, []).append((precio, n))
    return {"version": version, "desde": desde, "por_sub": por_sub}


def _modal(votos: list[tuple[float, int]]) -> tuple[float | None, int, int]:
    """(precio modal, n del modal, n total). Empate → el más alto."""
    suma: dict[float, int] = {}
    for precio, n in votos:
        suma[precio] = suma.get(precio, 0) + n
    if not suma:
        return None, 0, 0
    total = sum(suma.values())
    precio, n = max(suma.items(), key=lambda kv: (kv[1], kv[0]))
    return precio, n, total


def _votos(por_sub: dict, subs: list[tuple[str, str]],
           tonos: list[str]) -> list[tuple[float, int]]:
    out: list[tuple[float, int]] = []
    for cat, sub in subs:
        d = por_sub.get((_norm(cat), _norm(sub))) or {}
        for t in tonos:
            out.extend(d.get(t) or [])
    return out


def _iva2(neto) -> float | None:
    if neto is None:
        return None
    return round(queries.precio_con_iva(float(neto)), 2)


def comparar(filas_asinfo: list[dict], matriz: list[dict], planos: list[dict]) -> dict:
    """Las celdas de Programa que difieren de Asinfo. Sin base ni Metabase:
    recibe todo, para que un test lo pruebe con datos sueltos.

    Devuelve {version, desde, diferencias: [...], iguales, sin_dato: [...]}.
    Cada diferencia: {tipo: 'matriz'|'plano', clase, columna|id, tela,
    clase_desc, asinfo_neto, pc_neto, asinfo_iva, pc_iva, n, total, pureza,
    aplicable}. `aplicable` = el modal tiene apoyo (pureza ≥ PUREZA_MIN).
    """
    esc = escalera_asinfo(filas_asinfo)
    por_sub = esc["por_sub"]
    etiquetas = dict(queries.TELAS)
    clases_desc = {int(f["clase"]): str(f.get("descripcio") or "").strip()
                   for f in matriz}
    difs: list[dict] = []
    iguales = 0
    sin_dato: list[str] = []

    for col, _etq in queries.TELAS:
        subs = MAPA.get(col) or []
        for fila in matriz:
            clase = int(fila["clase"])
            tono = next((t for t, c in TONO_A_CLASE.items() if c == clase), None)
            if tono is None:
                continue
            precio, n, total = _modal(_votos(por_sub, subs, [tono]))
            if precio is None:
                sin_dato.append(f"{etiquetas.get(col, col)}/{clases_desc.get(clase, clase)}")
                continue
            pc = fila.get(col)
            pc_f = float(pc) if pc is not None else None
            if _iva2(pc_f) == _iva2(precio):
                iguales += 1
                continue
            pureza = round(n / total, 2) if total else 0.0
            difs.append({
                "tipo": "matriz", "clase": clase, "columna": col,
                "tela": etiquetas.get(col, col.upper()),
                "clase_desc": clases_desc.get(clase, str(clase)),
                "asinfo_neto": precio, "pc_neto": pc_f,
                "asinfo_iva": _iva2(precio), "pc_iva": _iva2(pc_f),
                "n": n, "total": total, "pureza": pureza,
                "aplicable": pureza >= PUREZA_MIN,
            })

    for p in planos:
        if p.get("ref_col"):
            continue
        tela = str(p.get("tela") or "").strip().upper()
        subs = MAPA_PLANO.get(tela)
        if not subs:
            continue
        precio, n, total = _modal(_votos(por_sub, subs, list(TONO_A_CLASE)))
        if precio is None:
            sin_dato.append(tela)
            continue
        pc = p.get("precio")
        pc_f = float(pc) if pc is not None else None
        if _iva2(pc_f) == _iva2(precio):
            iguales += 1
            continue
        pureza = round(n / total, 2) if total else 0.0
        difs.append({
            "tipo": "plano", "id": int(p["id"]), "tela": tela,
            "clase_desc": "Todos los colores",
            "asinfo_neto": precio, "pc_neto": pc_f,
            "asinfo_iva": _iva2(precio), "pc_iva": _iva2(pc_f),
            "n": n, "total": total, "pureza": pureza,
            "aplicable": pureza >= PUREZA_MIN,
        })

    return {
        "version": esc["version"], "desde": esc["desde"],
        "diferencias": difs, "iguales": iguales, "sin_dato": sin_dato,
    }


def diferencias(forzar: bool = False) -> dict:
    """Lo que ve el bloque de /precios. Nunca levanta.

    {ok, contesto, version, desde, diferencias, iguales, sin_dato, error?}.
    Caché de 10 min SOLO del éxito; si Metabase no contesta y hay un último
    valor bueno, se devuelve ese con `viejo=True`.
    """
    now = _time.monotonic()
    with _cache_lock:
        c = _cache.get("res")
        if c and not forzar and (now - c[0]) < _TTL_SECS:
            return dict(c[1])
    try:
        filas, contesto = traer_asinfo()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("lista de precios de Asinfo: %s", e)
        filas, contesto = [], False
    if not contesto or not filas:
        with _cache_lock:
            c = _cache.get("res")
        if c:
            return {**c[1], "viejo": True}
        return {"ok": False, "contesto": False, "version": None, "desde": None,
                "diferencias": [], "iguales": 0, "sin_dato": [],
                "error": "Asinfo no contestó"}
    try:
        res = comparar(filas, queries.matriz(), queries.precio_plano())
    except Exception as e:  # noqa: BLE001
        _LOG.warning("comparar precios con Asinfo: %s", e)
        return {"ok": False, "contesto": True, "version": None, "desde": None,
                "diferencias": [], "iguales": 0, "sin_dato": [],
                "error": str(e)[:200]}
    res.update({"ok": True, "contesto": True})
    with _cache_lock:
        _cache["res"] = (now, dict(res))
    return res


def limpiar_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Aplicar: escribe el neto de Asinfo en las celdas elegidas
# ---------------------------------------------------------------------------
def clave_de(d: dict) -> str:
    if d.get("tipo") == "matriz":
        return f"m_{int(d['clase'])}_{d['columna']}"
    return f"p_{int(d['id'])}"


def aplicar(claves: list[str], usuario: str) -> dict:
    """`claves` = ['m_<clase>_<col>', 'p_<id>', …] tildadas en la pantalla.

    El precio NO viene del formulario: se relee de Asinfo (o del caché) y se
    escribe ese. Devuelve {aplicados, no_encontrados, errores}.
    """
    res = diferencias()
    por_clave = {clave_de(d): d for d in res.get("diferencias") or []}
    aplicados = 0
    no_enc: list[str] = []
    errores: list[str] = []
    for k in claves:
        d = por_clave.get(k)
        if not d:
            no_enc.append(k)
            continue
        neto = round(float(d["asinfo_neto"]), 4)
        try:
            if d["tipo"] == "matriz":
                queries.actualizar_precio(int(d["clase"]), d["columna"], neto, usuario)
            else:
                queries.actualizar_precio_plano(int(d["id"]), neto, usuario)
            aplicados += 1
        except Exception as e:  # noqa: BLE001
            errores.append(f"{d['tela']}/{d['clase_desc']}: {e}")
    if aplicados:
        limpiar_cache()
    return {"aplicados": aplicados, "no_encontrados": no_enc, "errores": errores}


# ---------------------------------------------------------------------------
# Versión nueva → campanita. Corre solo, una vez por hora, desde el hilo de
# fondo (modules/_lib/autocarga_facturas.py). PRECIOS_ASINFO_AUTO=0 lo apaga.
# ---------------------------------------------------------------------------
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.precios_asinfo_version (
    id        SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version   INTEGER,
    desde     DATE,
    visto     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_CHECK_MIN_SECS = 3600
_auto_lock = threading.Lock()
_auto_ultimo = 0.0


def asegurar_tabla() -> None:
    db.execute(_BOOTSTRAP_SQL)


def version_vista() -> dict | None:
    asegurar_tabla()
    return db.fetch_one(
        "SELECT version, desde, visto FROM scintela.precios_asinfo_version WHERE id = 1"
    )


def version_asinfo() -> dict | None:
    """{version, desde} de la vigente, o None si Metabase no contestó."""
    from modules._lib import metabase_client as mc

    if not mc.disponible():
        return None
    filas, contesto = mc.fetch_dataset_estado(2, _SQL_VERSION, max_results=1)
    if not contesto or not filas:
        return None
    r = filas[0]
    try:
        return {"version": int(r["version"]), "desde": str(r.get("desde") or "")[:10]}
    except (TypeError, ValueError, KeyError):
        return None


def chequear_version() -> dict:
    """Compara la versión vigente con la última vista. Si cambió, la guarda y
    deja la campanita. {cambio, version, desde, n_diferencias}.

    La PRIMERA vez (tabla vacía) sólo guarda: no hay con qué comparar y no
    tiene sentido avisar "cambió" por estrenar el chequeo.
    """
    nueva = version_asinfo()
    if not nueva:
        return {"cambio": False, "version": None}
    vista = version_vista()
    if vista and vista.get("version") == nueva["version"]:
        return {"cambio": False, "version": nueva["version"]}
    db.execute(
        """
        INSERT INTO scintela.precios_asinfo_version (id, version, desde, visto)
             VALUES (1, %(v)s, %(d)s, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO UPDATE
            SET version = EXCLUDED.version, desde = EXCLUDED.desde,
                visto = CURRENT_TIMESTAMP
        """,
        {"v": nueva["version"], "d": nueva["desde"] or None},
    )
    limpiar_cache()
    if vista is None:
        return {"cambio": False, "version": nueva["version"],
                "desde": nueva["desde"], "primera_vez": True}
    n_dif = len(diferencias().get("diferencias") or [])
    _avisar(nueva, n_dif)
    return {"cambio": True, "version": nueva["version"],
            "desde": nueva["desde"], "n_diferencias": n_dif}


def _avisar(nueva: dict, n_dif: int) -> None:
    from modules.avisos.queries import avisar

    desde = nueva.get("desde") or ""
    if len(desde) == 10:
        desde = f"{desde[8:10]}/{desde[5:7]}/{desde[0:4]}"
    titulo = f"Asinfo publicó una lista de precios nueva (versión {nueva['version']})"
    detalle = f"Vigente desde el {desde}. " + (
        f"{n_dif} precio(s) de Programa Core quedaron distintos: "
        "revisalos al pie de Precios y aplicá los que correspondan."
        if n_dif else "Los precios de Programa Core siguen iguales a los de Asinfo."
    )
    avisar(
        fuente="precios", nivel="alerta" if n_dif else "ok",
        titulo=titulo, detalle=detalle[:200], cantidad=n_dif or None,
        url="/precios#precios-asinfo",
        clave=f"precios-asinfo-version-{nueva['version']}",
    )


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo: a lo sumo una vez por hora. Nunca levanta."""
    res: dict = {"corrio": False}
    if os.environ.get("PRECIOS_ASINFO_AUTO", "1") == "0":
        return res
    global _auto_ultimo
    ahora = _time.monotonic()
    with _auto_lock:
        if _auto_ultimo and (ahora - _auto_ultimo) < _CHECK_MIN_SECS:
            return res
        _auto_ultimo = ahora
    try:
        res["corrio"] = True
        res["reporte"] = chequear_version()
    except Exception as e:  # noqa: BLE001 — el hilo no se cae por esto
        _LOG.warning("versión de precios de Asinfo (fondo): %s", e)
    return res


def health() -> dict:
    """Para /admin/health/precios-asinfo: alerta si hay precios distintos a la
    lista vigente de Asinfo. Si Asinfo no contesta, ok (no es un problema
    contable) con `sin_datos`."""
    res = diferencias()
    if not res.get("ok"):
        return {"ok": True, "alerts": [],
                "stats": {"sin_datos": True, "error": res.get("error")}}
    difs = res.get("diferencias") or []
    alerts = []
    if difs:
        ej = ", ".join(f"{d['tela']}/{d['clase_desc']} {d['pc_iva']}→{d['asinfo_iva']}"
                       for d in difs[:4])
        alerts.append({
            "severity": "medium", "category": "precios_distintos_asinfo",
            "msg": (f"{len(difs)} precio(s) de /precios distintos a la lista "
                    f"vigente de Asinfo (versión {res.get('version')}): {ej}"
                    f"{'…' if len(difs) > 4 else ''}. Revisar al pie de /precios."),
        })
    return {"ok": not alerts, "alerts": alerts,
            "stats": {"version": res.get("version"), "desde": res.get("desde"),
                      "diferencias": len(difs), "iguales": res.get("iguales"),
                      "sin_dato": res.get("sin_dato"),
                      "viejo": bool(res.get("viejo"))}}
