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
       COALESCE(TRIM(c.direccion1), '')   AS direccion1,
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
        # TMT 2026-08-04 — en Asinfo `empresa.codigo` **es el RUC**, no un
        # código corto: el código de 3 letras que comparte con Programa Core
        # vive en **`nombre_comercial`**. Se ve en la ficha de AS2 del RUC
        # 1591718165001 → Código = 1591718165001, Nombre Comercial = **NUF**,
        # Nombre Fiscal = "ASOCIACIÓN … ASOTEXMANA". Comparando contra
        # `codigo` la pantalla decía "1802931442001 ✗ no es ALP" en TODAS las
        # filas: ruido, no información. `codigo` queda de fallback por si
        # alguna ficha vieja sí lo tiene cargado corto.
        comercial = str(f.get("nombre_comercial") or "").strip().upper()
        cod = comercial if 0 < len(comercial) <= 4 else str(f.get("codigo") or "").strip().upper()
        if len(cod) > 4:
            # Un RUC no es un código de cliente: mejor "Asinfo no lo tiene"
            # que un ✗ que nadie puede accionar.
            cod = ""
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
        g["que_hacer"] = que_hacer(g)
        salida.append(g)
    return salida


# ---------------------------------------------------------------------------
# «Qué hacer» — la columna que convierte el diagnóstico en una acción
# ---------------------------------------------------------------------------
# TMT 2026-08-04. La pantalla decía muy bien QUÉ PASA y no decía nada de QUÉ
# HACER, así que cada uno de los 20 casos había que volver a razonarlo de
# cero mirando el RUC, el nombre y el conteo de movimientos. Peor: los 20 NO
# son el mismo problema, y tratarlos igual es lo que hizo perder una tarde —
# en 14 de los 20 la ficha con el código bueno YA EXISTE en PC con el mismo
# RUC (es el mismo cliente cargado dos veces, se borra la sobrante), y sólo
# en 6 son dos empresas reales chocando (ahí hay que renombrar a una).
#
# Todo lo de acá abajo es PURO: entra un grupo, sale un veredicto. Sin DB,
# sin Metabase, testeable a mano.

#: Es el mismo cliente cargado dos veces (mismo RUC, mismo nombre).
DUPLICADA = "duplicada"
#: Mismo RUC pero nombres que no se parecen → una tiene el RUC mal cargado.
RUC_SOSPECHOSO = "ruc_sospechoso"
#: Dos empresas reales y Asinfo dice cuál se queda con el código.
RENOMBRAR = "renombrar"
#: Dos empresas reales y Asinfo no conoce a ninguna → decide la dueña.
DECIDIR = "decidir"
#: A una de las fichas le falta el RUC: sin RUC no se le puede preguntar.
FALTA_RUC = "falta_ruc"
#: Asinfo le da el mismo código a las dos. No lo resuelve solo.
AMBIGUO = "ambiguo"
#: Ni se pudo preguntar (Metabase caído o sin configurar).
PENDIENTE_ASINFO = "pendiente_asinfo"

#: Palabras que no distinguen a una persona/empresa de otra: si se cuelan en
#: la comparación de nombres, "MODITEX S.A." y "MODITEX CIA LTDA" dejan de
#: parecerse. Se sacan antes de comparar.
_RUIDO_NOMBRE = frozenset({
    "SA", "S.A", "CIA", "LTDA", "CIALTDA", "SAS", "SCC", "EIRL",
    "DEL", "LOS", "LAS", "DE", "LA", "EL", "Y",
})


def _tokens(nombre: str) -> frozenset[str]:
    """Palabras significativas de un nombre, sin tildes ni ruido societario."""
    import unicodedata

    plano = unicodedata.normalize("NFKD", str(nombre or "").upper())
    plano = "".join(ch for ch in plano if not unicodedata.combining(ch))
    crudas = [p for p in re.split(r"[^A-Z0-9]+", plano) if p]
    return frozenset(p for p in crudas if len(p) >= 3 and p not in _RUIDO_NOMBRE)


def mismo_nombre(a: str, b: str) -> bool:
    """¿Los dos nombres son la MISMA persona escrita distinto?

    El criterio es **contención**, no igualdad: la carga duplicada casi
    siempre es el nombre completo contra el nombre corto —
    "ALEXA MAGDALENA PINCAY" ⊂ "ALEXA MAGDALENA PINCAY ESPIN". Igualdad
    exacta no los agarraría y una similitud difusa agarraría de más.

    Es lo único que separa el caso "cargado dos veces" (se borra la sobrante)
    del caso JRP — Jorge Rosero Pozo y Judith del Rocío Pantoja Pozo, dos
    personas DISTINTAS que en PC tienen el mismo RUC porque una lo tiene mal.
    Comparten el apellido Pozo y nada más; sin este corte la pantalla diría
    "borrá una" y se perdería un cliente real.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta


def _link_cambiar(f: dict) -> dict:
    return {
        "texto": f"Ponerle código propio a #{f['id_cliente']} {f.get('nombre') or ''}".strip(),
        "href": f"/clientes/{f['id_cliente']}/cambiar-codigo",
    }


def que_hacer(grupo: dict) -> dict:
    """El veredicto accionable de un código repetido.

    Devuelve ``{clase, titulo, detalle, advertencia, acciones}``. `acciones`
    son links a pantallas que EXISTEN (`/clientes?q=`, `/clientes/<id>/
    cambiar-codigo`, `/clientes/<cod>/editar`); no se inventa ninguna ruta.
    """
    fichas = list(grupo.get("fichas") or [])
    cod = str(grupo.get("codigo_cli") or "")
    n_fact = int(grupo.get("n_facturas") or 0)
    n_chq = int(grupo.get("n_cheques") or 0)
    rucs = {f.get("ruc10") for f in fichas if f.get("ruc10")}
    sin_ruc = [f for f in fichas if not f.get("ruc10")]
    coinciden = [f for f in fichas if f.get("estado") == COINCIDE]
    perdedoras = [f for f in fichas if f.get("estado") != COINCIDE]
    asinfo_ok = not any(f.get("estado") == NO_DISPONIBLE for f in fichas)

    # La advertencia de plata mezclada vale para TODOS los casos de "dos
    # empresas": es la razón por la que `plan_cambio_codigo` va a rechazar el
    # cambio, y decirlo acá evita el viaje hasta la pantalla para comerse el
    # error.
    advertencia = ""
    if n_fact or n_chq:
        advertencia = (
            f"Ojo: {n_fact} facturas y {n_chq} cheques cuelgan del código "
            f"{cod} y no se sabe de cuál de las fichas son (los movimientos "
            "guardan el CÓDIGO, no la ficha). Mientras el código tenga "
            "movimientos, la pantalla de cambiar código lo va a rechazar: "
            "hay que separarlos primero mirando Asinfo por RUC."
        )

    # (1) Todas las fichas con el MISMO RUC → no son dos empresas.
    if len(rucs) == 1 and not sin_ruc and len(fichas) > 1:
        nombres = [str(f.get("nombre") or "") for f in fichas]
        base = nombres[0]
        if all(mismo_nombre(base, n) for n in nombres[1:]):
            direcciones = {str(f.get("direccion1") or "").strip().upper() for f in fichas}
            direcciones.discard("")
            extra = ""
            if len(direcciones) > 1:
                extra = (
                    " Las direcciones no coinciden — quedate con la ficha de "
                    "la dirección vigente, el dato de la otra se pierde."
                )
            return {
                "clase": DUPLICADA,
                "titulo": "Es el mismo cliente cargado dos veces",
                "detalle": (
                    f"Las {len(fichas)} fichas tienen el mismo RUC y el mismo "
                    "nombre: no son dos empresas. Borrá la sobrante. Mientras "
                    "quede una ficha con el código, los movimientos siguen "
                    "resolviendo — no queda nada huérfano." + extra
                ),
                "advertencia": "",
                "acciones": [{"texto": f"Buscar {cod} en clientes y borrar la sobrante",
                              "href": f"/clientes?q={cod}"}],
            }
        return {
            "clase": RUC_SOSPECHOSO,
            "titulo": "Mismo RUC, nombres distintos → un RUC está mal cargado",
            "detalle": (
                "Las fichas comparten el RUC pero son personas distintas, así "
                "que una lo tiene mal. Conseguí el RUC verdadero y corregilo: "
                "recién ahí Asinfo puede decir de quién es el código. No "
                "borres ninguna."
            ),
            "advertencia": advertencia,
            "acciones": [{"texto": f"Editar la ficha {cod}", "href": f"/clientes/{cod}/editar"}],
        }

    # (2) Ni se pudo preguntar.
    if not asinfo_ok:
        return {
            "clase": PENDIENTE_ASINFO,
            "titulo": "Falta la respuesta de Asinfo",
            "detalle": (
                "Los RUC son distintos, así que son dos empresas, pero sin "
                "Asinfo no se sabe de cuál es el código. Volvé a cargar la "
                "pantalla cuando el puente vuelva."
            ),
            "advertencia": advertencia,
            "acciones": [],
        }

    # (3) A alguna ficha le falta el RUC.
    if sin_ruc:
        quienes = ", ".join(f"#{f['id_cliente']}" for f in sin_ruc)
        return {
            "clase": FALTA_RUC,
            "titulo": "Falta el RUC",
            "detalle": (
                f"La ficha {quienes} no tiene RUC usable, y el RUC es la única "
                "clave contra Asinfo. Cargáselo y volvé: con eso el caso "
                "probablemente se resuelva solo."
            ),
            "advertencia": advertencia,
            "acciones": [{"texto": f"Editar la ficha {cod}", "href": f"/clientes/{cod}/editar"}],
        }

    # (4) Asinfo resuelve: exactamente una ficha coincide.
    if len(coinciden) == 1:
        gana = coinciden[0]
        return {
            "clase": RENOMBRAR,
            "titulo": f"Asinfo dice que {cod} es de {gana.get('nombre') or '—'}",
            "detalle": (
                f"Son dos empresas reales. {cod} se queda en la ficha "
                f"#{gana['id_cliente']}; a la otra hay que ponerle un código "
                "propio (la pantalla propone los libres). Borrar no es opción: "
                "se perdería un cliente."
            ),
            "advertencia": advertencia,
            "acciones": [_link_cambiar(f) for f in perdedoras],
        }

    # (5) Asinfo le da el mismo código a más de una.
    if len(coinciden) > 1:
        return {
            "clase": AMBIGUO,
            "titulo": "Asinfo le da el mismo código a las dos",
            "detalle": (
                f"{len(coinciden)} fichas con RUC distinto y Asinfo dice {cod} "
                "para todas. O hay un duplicado del lado de Asinfo, o los RUC "
                "de PC están cruzados. Hay que mirarlo en Asinfo antes de "
                "tocar nada acá."
            ),
            "advertencia": advertencia,
            "acciones": [],
        }

    # (6) Ninguna coincide.
    otros = sorted({c for f in fichas for c in (f.get("asinfo_codigos") or [])})
    if otros:
        return {
            "clase": DECIDIR,
            "titulo": f"Asinfo no le da {cod} a ninguna de las dos",
            "detalle": (
                f"Asinfo les da {', '.join(otros)}. O sea que ninguna de las "
                f"dos debería estar usando {cod}: hay que renombrarlas a las "
                "dos, o corregir el RUC si el que está mal es el de PC."
            ),
            "advertencia": advertencia,
            "acciones": [_link_cambiar(f) for f in fichas],
        }
    return {
        "clase": DECIDIR,
        "titulo": "Asinfo no conoce ninguno de los dos RUC",
        "detalle": (
            f"Son dos empresas reales y Asinfo no tiene a ninguna, así que no "
            f"hay autoridad externa: decidí vos cuál se queda con {cod} y "
            "ponele código propio a la otra."
        ),
        "advertencia": advertencia,
        "acciones": [_link_cambiar(f) for f in fichas],
    }


def resumen(grupos: list[dict]) -> dict:
    """Contadores para el encabezado."""
    fichas = [f for g in grupos for f in g["fichas"]]
    clases = [(g.get("que_hacer") or {}).get("clase") for g in grupos]
    return {
        "codigos": len(grupos),
        "fichas": len(fichas),
        "resueltos": sum(1 for g in grupos if g.get("hay_ganador")),
        "difieren": sum(1 for f in fichas if f["estado"] == DIFIERE),
        "sin_asinfo": sum(1 for f in fichas if f["estado"] in (SIN_ASINFO, SIN_RUC)),
        # Cuántos se arreglan borrando una fila (no hace falta decidir nada) y
        # cuántos necesitan a la dueña. Es el número que dice cuánto falta.
        "duplicadas": sum(1 for c in clases if c == DUPLICADA),
        "necesitan_decision": sum(1 for c in clases if c != DUPLICADA),
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
