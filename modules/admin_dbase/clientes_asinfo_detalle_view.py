"""/admin/clientes-asinfo/<codigo> — quién es quién, factura por factura.

⭐ POR QUÉ (TMT 2026-08-04). La pantalla madre (`clientes_asinfo_view`) dice
QUÉ pasa con cada código repetido y qué habría que hacer. Para 5 de los 7
casos abiertos eso alcanza. Para **BLP** (75 facturas + 3 cheques) y **BRC**
(18 facturas) no: ahí las dos empresas comparten el código, los movimientos
guardan el **CÓDIGO y no la ficha**, y no hay forma —desde PC solo— de saber
cuál factura es de cuál. Por eso `plan_cambio_codigo` rechaza el cambio, y con
razón: renombrar hoy le regalaría a una de las dos toda la plata de la otra.

El dato SÍ existe, pero del otro lado: **Asinfo factura contra el RUC**. Cada
factura de Asinfo cuelga de una `empresa`, y cada `empresa` tiene un RUC
propio. Cruzando el número de factura de PC contra el de Asinfo se sabe, una
por una, de quién es. Esta pantalla hace ese cruce.

De paso resuelve otros dos casos que también dependían de mirar Asinfo a mano:

* **la dirección** (caso MTE — dos fichas de MODITEX con direcciones distintas
  y la pregunta es cuál rige). Asinfo la tiene en `direccion_empresa`.
* **la búsqueda por NOMBRE** (caso JRP — Judith tiene cargado el RUC de Jorge,
  así que preguntarle a Asinfo *por RUC* devuelve a Jorge las dos veces y no
  aporta nada). Buscando por nombre aparece su ficha real, con su RUC.

**SÓLO LECTURA.** No escribe ni en Postgres ni en Asinfo, igual que la madre.

**Fail-soft.** Sin Metabase la pantalla igual muestra el lado PC (las fichas y
las facturas del código) y avisa que el lado Asinfo no está disponible.
"""
from __future__ import annotations

import logging
import re

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from modules.clientes.mail_asinfo import ruc10

_LOG = logging.getLogger("programa_core.admin.clientes_asinfo_detalle")

bp = Blueprint(
    "admin_clientes_asinfo_detalle",
    __name__,
    url_prefix="/admin/clientes-asinfo",
    template_folder="templates",
)

#: Id de la base de Asinfo en Metabase (SQL Server). Ver skill
#: `programa-core-integraciones`.
DB_ASINFO = 2

#: Techo de facturas que se le piden a Asinfo. Los clientes de estos pares
#: tienen decenas, no miles; el techo es para que un RUC de un cliente grande
#: no traiga media base.
MAX_FACTURAS = 4000

#: Techo de resultados de la búsqueda por nombre.
MAX_NOMBRES = 50


# ---------------------------------------------------------------------------
# Lado PC
# ---------------------------------------------------------------------------

SQL_FICHAS = """
SELECT c.id_cliente,
       UPPER(TRIM(c.codigo_cli))                    AS codigo_cli,
       COALESCE(NULLIF(TRIM(c.nombre), ''), '')     AS nombre,
       COALESCE(TRIM(c.ruc), '')                    AS ruc,
       COALESCE(TRIM(c.direccion1), '')             AS direccion1,
       COALESCE(TRIM(c.telefono), '')               AS telefono,
       COALESCE(TRIM(c.vend), '')                   AS vend
  FROM scintela.cliente c
 WHERE UPPER(TRIM(c.codigo_cli)) = %(cod)s
 ORDER BY c.id_cliente
"""

#: Las facturas que cuelgan del CÓDIGO. `numf_completo` es el N° SRI cuando la
#: fila vino de Asinfo; `numf` es el número pelado (el que se tipeó a mano o
#: llegó del dBase). El cruce usa los dos.
SQL_FACTURAS = """
SELECT f.numf,
       COALESCE(TRIM(f.numf_completo), '')  AS numf_completo,
       f.fecha,
       COALESCE(f.importe, 0)               AS importe,
       COALESCE(f.saldo, 0)                 AS saldo,
       COALESCE(TRIM(f.stat), '')           AS stat
  FROM scintela.factura f
 WHERE UPPER(TRIM(f.codigo_cli)) = %(cod)s
 ORDER BY f.fecha DESC NULLS LAST, f.numf DESC
"""


def fichas_del_codigo(codigo: str) -> list[dict]:
    import db as _db
    try:
        return [dict(r) for r in (_db.fetch_all(SQL_FICHAS, {"cod": codigo}) or [])]
    except Exception as exc:  # noqa: BLE001 -- pantalla de diagnóstico, no cae
        _LOG.warning("clientes-asinfo detalle: fichas fallaron: %s", exc)
        return []


def facturas_del_codigo(codigo: str) -> list[dict]:
    import db as _db
    try:
        return [dict(r) for r in (_db.fetch_all(SQL_FACTURAS, {"cod": codigo}) or [])]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("clientes-asinfo detalle: facturas fallaron: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Número de factura — la clave del cruce
# ---------------------------------------------------------------------------


def numero_corto(valor) -> int | None:
    """`'001-099-000180441'` → `180441`. `'180441'` → `180441`. Si no, None.

    Es el mismo criterio que `facturas/vigia_anuladas._numf_de_numero`: se
    toma el último segmento del N° SRI, que es el secuencial. Los dos lados
    del cruce se normalizan así, porque PC guarda a veces el N° completo
    (`numf_completo`, cuando la fila la trajo la carga de Asinfo) y a veces
    sólo el secuencial (`numf`, cuando se tipeó a mano o vino del dBase).
    """
    if valor is None:
        return None
    ultimo = str(valor).strip().split("-")[-1]
    if not ultimo:
        return None
    try:
        return int(ultimo)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Lado Asinfo (SQL Server, vía Metabase) — SQL en dialecto SQL Server
# ---------------------------------------------------------------------------


def _rucs_limpios(ruc10s) -> list[str]:
    """Sólo los que son exactamente 10 dígitos. Nada más entra a la SQL.

    El `IN (...)` se interpola a mano porque `fetch_dataset` manda SQL nativa
    sin binds. Este filtro es lo que hace que eso sea seguro.
    """
    return sorted({r for r in (ruc10s or []) if re.fullmatch(r"[0-9]{10}", r or "")})


def sql_empresas(ruc10s: list[str]) -> str:
    """Ficha de Asinfo de cada RUC: nombre, código y **dirección principal**.

    La dirección vive en `direccion_empresa.descripcion`, no en `empresa`
    (verificado 2026-08-04 contra INFORMATION_SCHEMA). Se toma la marcada
    `indicador_direccion_principal`; si no hay ninguna marcada, la primera.
    Es lo que resuelve el caso MTE sin preguntarle a nadie.
    """
    limpios = _rucs_limpios(ruc10s)
    if not limpios:
        return ""
    in_list = ", ".join(f"'{r}'" for r in limpios)
    return f"""
SELECT TOP {len(limpios) * 4}
       LEFT(LTRIM(RTRIM(e.identificacion)), 10)     AS ruc10,
       LTRIM(RTRIM(ISNULL(e.identificacion, '')))   AS ruc_completo,
       LTRIM(RTRIM(ISNULL(e.nombre_fiscal, '')))    AS nombre_fiscal,
       LTRIM(RTRIM(ISNULL(e.nombre_comercial, ''))) AS nombre_comercial,
       LTRIM(RTRIM(ISNULL(de.descripcion, '')))     AS direccion,
       LTRIM(RTRIM(ISNULL(de.telefono1, '')))       AS telefono,
       ISNULL(de.indicador_direccion_principal, 0)  AS es_principal
  FROM empresa e
  LEFT JOIN direccion_empresa de ON de.id_empresa = e.id_empresa
 WHERE LEN(LTRIM(RTRIM(ISNULL(e.identificacion, '')))) >= 10
   AND LEFT(LTRIM(RTRIM(e.identificacion)), 10) IN ({in_list})
 ORDER BY ruc10, es_principal DESC
"""


def sql_facturas(ruc10s: list[str]) -> str:
    """Las facturas VIVAS de esos RUCs. `estado <> 0` = no anuladas.

    Es el mismo filtro que usa `asinfo.service` en todo el repo: `estado = 0`
    es anulada, y una anulada no debería decidir de quién es un código.
    """
    limpios = _rucs_limpios(ruc10s)
    if not limpios:
        return ""
    in_list = ", ".join(f"'{r}'" for r in limpios)
    return f"""
SELECT TOP {MAX_FACTURAS}
       LEFT(LTRIM(RTRIM(e.identificacion)), 10)  AS ruc10,
       LTRIM(RTRIM(ISNULL(fc.numero, '')))       AS numero,
       fc.fecha                                  AS fecha,
       ISNULL(fc.total, 0)                       AS total
  FROM factura_cliente fc
  JOIN empresa e ON e.id_empresa = fc.id_empresa
 WHERE fc.estado <> 0
   AND LEFT(LTRIM(RTRIM(e.identificacion)), 10) IN ({in_list})
 ORDER BY fc.fecha DESC
"""


def sql_por_nombre(texto: str) -> str:
    """Empresas de Asinfo cuyo nombre contiene el texto.

    El caso JRP: Judith tiene cargado el RUC de Jorge, así que preguntar por
    RUC devuelve a Jorge las dos veces. Por nombre aparece la ficha real de
    ella, con su RUC — que es exactamente el dato que falta.

    El texto se limpia a letras, dígitos y espacios antes de interpolarse: no
    quedan comillas, guiones ni comodines que puedan cambiar la consulta.
    """
    limpio = re.sub(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]", " ", str(texto or "")).strip()
    limpio = re.sub(r"\s+", " ", limpio).upper()
    if len(limpio) < 3:
        return ""
    return f"""
SELECT TOP {MAX_NOMBRES}
       LTRIM(RTRIM(ISNULL(e.identificacion, '')))   AS ruc_completo,
       LTRIM(RTRIM(ISNULL(e.nombre_fiscal, '')))    AS nombre_fiscal,
       LTRIM(RTRIM(ISNULL(e.nombre_comercial, ''))) AS nombre_comercial,
       ISNULL(e.indicador_cliente, 0)               AS es_cliente
  FROM empresa e
 WHERE UPPER(ISNULL(e.nombre_fiscal, '')) LIKE '%{limpio}%'
    OR UPPER(ISNULL(e.nombre_comercial, '')) LIKE '%{limpio}%'
 ORDER BY e.indicador_cliente DESC, e.nombre_fiscal
"""


def _consultar(sql: str, max_results: int) -> tuple[list[dict], bool]:
    """`(filas, contestó)`. Nunca levanta."""
    from modules._lib import metabase_client as mc

    if not sql or not mc.disponible():
        return [], False
    try:
        filas, contesto = mc.fetch_dataset_estado(DB_ASINFO, sql, max_results=max_results)
    except Exception as exc:  # noqa: BLE001 -- fail-soft, igual que la madre
        _LOG.warning("clientes-asinfo detalle: Asinfo no contestó: %s", exc)
        return [], False
    return ([dict(f) for f in (filas or [])], bool(contesto))


def empresas_de_asinfo(ruc10s: list[str]) -> tuple[dict[str, dict], bool]:
    """`(mapa ruc10 → ficha de Asinfo, contestó)`. Se queda con la principal."""
    filas, ok = _consultar(sql_empresas(ruc10s), max_results=len(_rucs_limpios(ruc10s)) * 4 or 1)
    mapa: dict[str, dict] = {}
    for f in filas:
        clave = ruc10(f.get("ruc10"))
        if not clave or clave in mapa:
            continue  # el ORDER BY ya puso la principal primero
        mapa[clave] = {
            "ruc_completo": str(f.get("ruc_completo") or "").strip(),
            "nombre_fiscal": str(f.get("nombre_fiscal") or "").strip()[:120],
            "nombre_comercial": str(f.get("nombre_comercial") or "").strip()[:120],
            "direccion": str(f.get("direccion") or "").strip()[:200],
            "telefono": str(f.get("telefono") or "").strip()[:40],
        }
    return mapa, ok


def facturas_de_asinfo(ruc10s: list[str]) -> tuple[dict[int, dict], bool]:
    """`(mapa N° corto → {ruc10, numero, fecha, total}, contestó)`.

    Si dos RUCs distintos tuvieran el mismo número corto —no debería pasar,
    el secuencial es del emisor y el emisor es Intela— gana el primero y el
    otro se descarta: preferimos no cruzar antes que cruzar mal.
    """
    filas, ok = _consultar(sql_facturas(ruc10s), max_results=MAX_FACTURAS)
    mapa: dict[int, dict] = {}
    for f in filas:
        n = numero_corto(f.get("numero"))
        clave = ruc10(f.get("ruc10"))
        if n is None or not clave or n in mapa:
            continue
        mapa[n] = {
            "ruc10": clave,
            "numero": str(f.get("numero") or "").strip(),
            "fecha": f.get("fecha"),
            "total": f.get("total"),
        }
    return mapa, ok


# ---------------------------------------------------------------------------
# El cruce — función PURA
# ---------------------------------------------------------------------------

#: La factura no aparece en Asinfo para ninguno de los dos RUCs. No es un
#: error: puede ser vieja (anterior a Asinfo), del dBase, o tipeada a mano.
SIN_DUENO = "sin_dueno"


def cruzar_facturas(fichas: list[dict], facturas_pc: list[dict],
                    mapa_asinfo: dict[int, dict]) -> dict:
    """De quién es cada factura del código, según Asinfo.

    Devuelve::

        {"filas": [...factura + id_dueno/nombre_dueno...],
         "por_ficha": [{id_cliente, nombre, ruc10, n, importe, saldo}],
         "sin_dueno": {"n": N, "importe": X, "saldo": Y},
         "concluyente": bool}

    `concluyente` es True cuando **todas** las facturas encontraron dueño. Es
    la condición para animarse a separar: si quedan facturas sin dueño, la
    plata todavía no está toda repartida y renombrar seguiría siendo una
    apuesta. La pantalla no lo redondea de más.
    """
    por_ruc = {}
    for f in fichas or []:
        clave = ruc10(f.get("ruc"))
        if clave and clave not in por_ruc:
            por_ruc[clave] = f

    acum: dict[object, dict] = {}
    for f in fichas or []:
        acum[f["id_cliente"]] = {
            "id_cliente": f["id_cliente"],
            "nombre": f.get("nombre") or "",
            "ruc10": ruc10(f.get("ruc")),
            "n": 0, "importe": 0.0, "saldo": 0.0,
        }
    huerfanas = {"n": 0, "importe": 0.0, "saldo": 0.0}

    filas = []
    for fac in facturas_pc or []:
        n = numero_corto(fac.get("numf_completo")) or numero_corto(fac.get("numf"))
        hit = mapa_asinfo.get(n) if n is not None else None
        dueno = por_ruc.get(hit["ruc10"]) if hit else None

        fila = dict(fac)
        fila["numero_corto"] = n
        fila["asinfo_numero"] = hit["numero"] if hit else ""
        fila["id_dueno"] = dueno["id_cliente"] if dueno else None
        fila["nombre_dueno"] = dueno["nombre"] if dueno else ""
        fila["estado"] = "asignada" if dueno else SIN_DUENO
        filas.append(fila)

        destino = acum[dueno["id_cliente"]] if dueno else huerfanas
        destino["n"] += 1
        destino["importe"] += float(fac.get("importe") or 0)
        destino["saldo"] += float(fac.get("saldo") or 0)

    return {
        "filas": filas,
        "por_ficha": list(acum.values()),
        "sin_dueno": huerfanas,
        "concluyente": bool(filas) and huerfanas["n"] == 0,
    }


def direcciones_difieren(ficha: dict, asinfo: dict | None) -> bool:
    """¿La dirección de PC no es la que Asinfo tiene para ese RUC?

    Comparación laxa a propósito (mayúsculas, sin puntuación): la misma
    dirección tipeada en dos momentos casi nunca coincide carácter a carácter,
    y marcar en rojo un "CDLA." contra un "CDLA" sería ruido.
    """
    def _norm(s):
        return re.sub(r"[^0-9A-Z]", "", str(s or "").upper())

    a, b = _norm(ficha.get("direccion1")), _norm((asinfo or {}).get("direccion"))
    if not a or not b:
        return False
    return a not in b and b not in a


# ---------------------------------------------------------------------------
# Vista
# ---------------------------------------------------------------------------


@bp.route("/<codigo>", methods=["GET"])
@requiere_login
# TMT 2026-08-24 (dueña): permiso PROPIO, no `admin_dbase.ver` (mig 0210).
# El aviso de los códigos repetidos lleva acá, y el que tiene que resolverlos
# es Alex, que es INT. Darle `admin_dbase.ver` le abriría el panel de
# administración entero; esto le abre esta pantalla y nada más.
@requiere_permiso("clientes.duplicados")
def detalle(codigo: str):
    """Pantalla de diagnóstico de UN código repetido. GET y nada más."""
    cod = re.sub(r"[^0-9A-Za-z]", "", str(codigo or ""))[:3].upper()
    fichas = fichas_del_codigo(cod)
    facturas_pc = facturas_del_codigo(cod)

    rucs = sorted({ruc10(f.get("ruc")) for f in fichas} - {""})
    empresas, ok_emp = empresas_de_asinfo(rucs)
    mapa_fact, ok_fac = facturas_de_asinfo(rucs)
    asinfo_ok = ok_emp or ok_fac

    for f in fichas:
        clave = ruc10(f.get("ruc"))
        f["ruc10"] = clave
        f["asinfo"] = empresas.get(clave)
        f["direccion_difiere"] = direcciones_difieren(f, empresas.get(clave))

    # Buscar por nombre: sólo si la persona lo pidió. Por defecto se propone
    # el nombre de la ficha que Asinfo no reconoce — que es el caso JRP.
    q = (request.args.get("q") or "").strip()
    resultados_nombre, ok_nombre = ([], False)
    if q:
        resultados_nombre, ok_nombre = _consultar(sql_por_nombre(q), max_results=MAX_NOMBRES)

    return render_template(
        "admin_dbase/clientes_asinfo_detalle.html",
        codigo=cod,
        fichas=fichas,
        cruce=cruzar_facturas(fichas, facturas_pc, mapa_fact),
        asinfo_ok=asinfo_ok,
        q=q,
        resultados_nombre=resultados_nombre,
        busqueda_ok=ok_nombre,
    )
