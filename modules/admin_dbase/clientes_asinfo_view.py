"""/admin/clientes-asinfo — qué código le da ASINFO a cada ficha duplicada.

⭐ POR QUÉ (TMT 2026-08-04). `scintela.cliente` tiene **20 códigos repetidos**:
dos empresas DISTINTAS con el mismo código de 3 letras. No es un error de
carga suelto, es **estructural**: el código son las INICIALES del nombre, así
que dos clientes con las mismas iniciales colisionan solos. `LEC` es a la vez
"Luis Ernesto Cañamar" y "Lola Emperatriz Cisneros"; `LUL` es "Luis Llugla" y
"Luis Lopez".

Y el código repetido **duplica la plata**, porque todo el sistema JOINea por
`codigo_cli`:
  - la comisión de un vendedor se infló $4.341,86,
  - el estado de cuenta muestra el saldo DOS veces (el caso GUF del 03/08:
    dos fichas con el mismo 15.104,31).

**Asinfo es la autoridad sobre el código de cliente.** Es el ERP que factura;
el código que Asinfo le da a un RUC es el bueno. Ya está verificado con el
caso testigo: el RUC `1591718165001` (ASOTEXMANA) en Asinfo es **NUF**, y en
PC estaba cargado como **GUF** — que en realidad es Guadalupe Fiallos.
Resolver esa colisión fue, literalmente, mirar Asinfo. Esta pantalla hace ese
paso para los 20 casos de una sola vez, en vez de a mano y uno por uno.

## Qué hace y qué NO hace

**SÓLO LECTURA.** No escribe ni en Postgres ni en Asinfo. Es la pantalla que
se mira ANTES de decidir; el borrado de la fila sobrante se hace después, por
`/clientes/<id>/eliminar` (que ya sabe que con el código repetido borrar la
sobrante no deja nada huérfano).

**Fail-soft.** Si Metabase no está configurado o no contesta, la pantalla
igual muestra el lado PC (las fichas duplicadas, sus facturas y sus cheques) y
avisa arriba que el lado Asinfo no está disponible. Nunca 500: el diagnóstico
del lado PC ya vale por sí solo, y quedarse sin pantalla porque el puente se
cayó es lo peor de los dos mundos.

**El cruce es por `ruc10`** — los primeros 10 dígitos del RUC — que es la
clave PC↔Asinfo que ya usa el repo (ver `modules/clientes/mail_asinfo.ruc10`:
en Ecuador el RUC de persona natural es la cédula (10) + '001', PC guarda a
veces la cédula pelada y Asinfo casi siempre el RUC completo).
"""
from __future__ import annotations

import logging
import re

from flask import Blueprint, render_template

from auth import requiere_login, requiere_permiso
from modules.clientes.mail_asinfo import ruc10

_LOG = logging.getLogger("programa_core.admin.clientes_asinfo")

bp = Blueprint(
    "admin_clientes_asinfo",
    __name__,
    url_prefix="/admin/clientes-asinfo",
    template_folder="templates",
)

#: Id de la base de Asinfo en Metabase (SQL Server). Ver skill
#: `programa-core-integraciones`.
DB_ASINFO = 2

#: Techo de RUCs que se le mandan a Asinfo en una consulta. Hoy son ~20
#: códigos × 2 fichas = ~40; el techo es para que un día en que la data se
#: degrade no se arme un IN() de miles de literales.
MAX_RUCS = 400


# ---------------------------------------------------------------------------
# Lado PC
# ---------------------------------------------------------------------------

#: Las fichas de TODO código repetido, con cuántas facturas y cheques cuelgan
#: de ese código. Los movimientos apuntan al CÓDIGO, no al `id_cliente`, así
#: que los conteos son POR CÓDIGO y salen iguales para las dos fichas: eso es
#: justamente lo que hace que la plata se cuente dos veces.
SQL_DUPLICADOS = """
WITH dup AS (
    SELECT UPPER(TRIM(codigo_cli)) AS cod
      FROM scintela.cliente
     WHERE COALESCE(TRIM(codigo_cli), '') <> ''
     GROUP BY UPPER(TRIM(codigo_cli))
    HAVING COUNT(*) > 1
),
fact AS (
    SELECT UPPER(TRIM(codigo_cli)) AS cod, COUNT(*) AS n
      FROM scintela.factura
     GROUP BY UPPER(TRIM(codigo_cli))
),
chq AS (
    SELECT UPPER(TRIM(codigo_cli)) AS cod, COUNT(*) AS n
      FROM scintela.cheque
     GROUP BY UPPER(TRIM(codigo_cli))
)
SELECT c.id_cliente,
       UPPER(TRIM(c.codigo_cli))          AS codigo_cli,
       COALESCE(NULLIF(TRIM(c.nombre), ''), '') AS nombre,
       COALESCE(TRIM(c.ruc), '')          AS ruc,
       COALESCE(TRIM(c.vend), '')         AS vend,
       COALESCE(c.activo, TRUE)           AS activo,
       COALESCE(fact.n, 0)                AS n_facturas,
       COALESCE(chq.n, 0)                 AS n_cheques
  FROM scintela.cliente c
  JOIN dup       ON dup.cod  = UPPER(TRIM(c.codigo_cli))
  LEFT JOIN fact ON fact.cod = UPPER(TRIM(c.codigo_cli))
  LEFT JOIN chq  ON chq.cod  = UPPER(TRIM(c.codigo_cli))
 ORDER BY UPPER(TRIM(c.codigo_cli)), c.id_cliente
"""


def fichas_duplicadas() -> list[dict]:
    """Las fichas de PC cuyo `codigo_cli` está repetido. Fail-soft → []."""
    import db as _db

    try:
        return [dict(r) for r in (_db.fetch_all(SQL_DUPLICADOS) or [])]
    except Exception as exc:  # noqa: BLE001 -- una pantalla de diagnóstico no cae
        _LOG.warning("clientes-asinfo: el lado PC falló: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Lado Asinfo (SQL Server, vía Metabase)
# ---------------------------------------------------------------------------


def sql_asinfo(ruc10s: list[str]) -> str:
    """SQL **SQL Server** que pide el código de Asinfo para esos RUCs.

    Dialecto: `TOP n` (no `LIMIT`), `ISNULL` (no `COALESCE` a secas por
    consistencia con el resto del repo), `LEN` + `LTRIM/RTRIM`.

    El `IN (...)` se interpola a mano porque `fetch_dataset` manda SQL nativa
    sin parámetros posicionales. Es seguro: `ruc10()` ya dejó sólo dígitos y
    acá se vuelve a exigir `^[0-9]{10}$` — nada que venga del usuario llega
    crudo a la query.
    """
    limpios = sorted({r for r in ruc10s if re.fullmatch(r"[0-9]{10}", r or "")})
    if not limpios:
        return ""
    in_list = ", ".join(f"'{r}'" for r in limpios[:MAX_RUCS])
    return f"""
SELECT TOP {MAX_RUCS * 3}
       LEFT(LTRIM(RTRIM(e.identificacion)), 10)     AS ruc10,
       LTRIM(RTRIM(ISNULL(e.codigo, '')))           AS codigo,
       LTRIM(RTRIM(ISNULL(e.nombre_fiscal, '')))    AS nombre_fiscal,
       LTRIM(RTRIM(ISNULL(e.nombre_comercial, ''))) AS nombre_comercial,
       ISNULL(e.indicador_cliente, 0)               AS indicador_cliente,
       e.id_empresa                                 AS id_empresa
  FROM empresa e
 WHERE LEN(LTRIM(RTRIM(ISNULL(e.identificacion, '')))) >= 10
   AND LEFT(LTRIM(RTRIM(e.identificacion)), 10) IN ({in_list})
 ORDER BY ruc10, indicador_cliente DESC, codigo
"""


def codigos_de_asinfo(ruc10s: list[str]) -> tuple[dict[str, list[dict]], bool]:
    """`(mapa ruc10 → [fichas de Asinfo], contestó)`.

    `contestó` es False si Metabase no está configurado, se cayó o tiró
    timeout — que NO es lo mismo que "Asinfo no conoce ese RUC". La pantalla
    tiene que poder decir las dos cosas distintas: mostrar "Asinfo no lo
    tiene" cuando el puente anduvo y no hubo fila, y "no disponible" cuando
    ni se llegó a preguntar.
    """
    from modules._lib import metabase_client as mc

    sql = sql_asinfo(ruc10s)
    if not sql or not mc.disponible():
        return {}, False
    try:
        filas, contesto = mc.fetch_dataset_estado(DB_ASINFO, sql, max_results=MAX_RUCS * 3)
    except Exception as exc:  # noqa: BLE001 -- fail-soft, igual que mail_asinfo
        _LOG.warning("clientes-asinfo: Asinfo no contestó: %s", exc)
        return {}, False
    if not contesto:
        return {}, False

    mapa: dict[str, list[dict]] = {}
    for f in filas or []:
        clave = ruc10(f.get("ruc10"))
        cod = str(f.get("codigo") or "").strip().upper()
        if not clave:
            continue
        mapa.setdefault(clave, []).append({
            "codigo": cod,
            "nombre_fiscal": str(f.get("nombre_fiscal") or "").strip()[:120],
            "nombre_comercial": str(f.get("nombre_comercial") or "").strip()[:120],
            "es_cliente": str(f.get("indicador_cliente") or "0").strip() in ("1", "True", "true"),
            "id_empresa": f.get("id_empresa"),
        })
    return mapa, True


# ---------------------------------------------------------------------------
# El cruce — función PURA, testeable sin Postgres ni Metabase
# ---------------------------------------------------------------------------

#: Estado de cada ficha frente a Asinfo. El orden importa: es el que usa la
#: pantalla para decidir el color.
COINCIDE = "coincide"          # Asinfo le da a este RUC el mismo código
DIFIERE = "difiere"            # Asinfo dice OTRO código → la ficha está mal
SIN_ASINFO = "sin_asinfo"      # el puente anduvo pero Asinfo no tiene el RUC
SIN_RUC = "sin_ruc"            # la ficha de PC no tiene RUC usable
NO_DISPONIBLE = "no_disponible"  # ni se pudo preguntar

ETIQUETA_ESTADO = {
    COINCIDE: "Coincide con Asinfo",
    DIFIERE: "Asinfo le da OTRO código",
    SIN_ASINFO: "Asinfo no tiene este RUC",
    SIN_RUC: "La ficha de PC no tiene RUC",
    NO_DISPONIBLE: "Asinfo no disponible",
}


def cruzar(fichas: list[dict], mapa: dict[str, list[dict]], asinfo_ok: bool) -> list[dict]:
    """Agrupa las fichas por código y le pega a cada una el veredicto de Asinfo.

    Devuelve una lista de grupos:
        {codigo_cli, n_fichas, n_facturas, n_cheques, hay_ganador, fichas: [...]}

    `hay_ganador` es True cuando EXACTAMENTE una de las fichas del grupo
    coincide con el código de Asinfo: ese es el caso resuelto (la otra sobra).
    Si coinciden dos, o ninguna, la decisión sigue siendo humana y la pantalla
    no finge lo contrario.
    """
    grupos: dict[str, dict] = {}
    for f in fichas or []:
        cod = str(f.get("codigo_cli") or "").strip().upper()
        clave = ruc10(f.get("ruc"))
        candidatos = mapa.get(clave) or []
        codigos_asinfo = [c["codigo"] for c in candidatos if c.get("codigo")]

        if not asinfo_ok:
            estado = NO_DISPONIBLE
        elif not clave:
            estado = SIN_RUC
        elif not codigos_asinfo:
            estado = SIN_ASINFO
        elif cod in codigos_asinfo:
            estado = COINCIDE
        else:
            estado = DIFIERE

        fila = dict(f)
        fila["ruc10"] = clave
        fila["estado"] = estado
        fila["etiqueta"] = ETIQUETA_ESTADO.get(estado, "")
        fila["asinfo_codigo"] = codigos_asinfo[0] if codigos_asinfo else ""
        fila["asinfo_codigos"] = codigos_asinfo
        fila["asinfo_nombre"] = (
            candidatos[0].get("nombre_fiscal") or candidatos[0].get("nombre_comercial") or ""
        ) if candidatos else ""

        g = grupos.setdefault(cod, {
            "codigo_cli": cod,
            "n_facturas": int(f.get("n_facturas") or 0),
            "n_cheques": int(f.get("n_cheques") or 0),
            "fichas": [],
        })
        g["fichas"].append(fila)

    salida = []
    for cod in sorted(grupos):
        g = grupos[cod]
        g["n_fichas"] = len(g["fichas"])
        g["hay_ganador"] = sum(1 for x in g["fichas"] if x["estado"] == COINCIDE) == 1
        salida.append(g)
    return salida


def resumen(grupos: list[dict]) -> dict:
    """Contadores para el encabezado."""
    fichas = [f for g in grupos for f in g["fichas"]]
    return {
        "codigos": len(grupos),
        "fichas": len(fichas),
        "resueltos": sum(1 for g in grupos if g.get("hay_ganador")),
        "difieren": sum(1 for f in fichas if f["estado"] == DIFIERE),
        "sin_asinfo": sum(1 for f in fichas if f["estado"] in (SIN_ASINFO, SIN_RUC)),
    }


# ---------------------------------------------------------------------------
# Vista
# ---------------------------------------------------------------------------


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    """Pantalla de diagnóstico. GET y nada más: no hay POST en este módulo."""
    fichas = fichas_duplicadas()
    rucs = sorted({ruc10(f.get("ruc")) for f in fichas} - {""})
    mapa, asinfo_ok = codigos_de_asinfo(rucs)
    grupos = cruzar(fichas, mapa, asinfo_ok)
    return render_template(
        "admin_dbase/clientes_asinfo.html",
        grupos=grupos,
        resumen=resumen(grupos),
        asinfo_ok=asinfo_ok,
        n_rucs=len(rucs),
    )
