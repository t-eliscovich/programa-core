"""Consola SQL de sólo lectura — `/admin/sql`.

⭐ POR QUÉ (TMT 2026-08-03). El día del bug de la cadena de saldos, cada
pregunta de datos ("¿cuántas filas tienen el saldo roto?", "¿qué fila se
insertó backdateada?", "¿cuánto suman los pendientes que borró?") costaba
**escribir un endpoint nuevo, pushear, esperar 3 minutos de CI+deploy y
recién ahí ver el número**. Doce preguntas = doce deploys = media jornada
mirando GitHub Actions.

La base es RDS con IP lock: no se llega desde afuera. Las pantallas
existentes contestan lo que ya se pensó de antemano; una pregunta nueva no
tiene pantalla. Esta consola cierra ese agujero: la pregunta se contesta en
segundos y sin tocar el repo.

## Cómo se garantiza que no puede escribir

**No se confía en un filtro de palabras.** Un blacklist de regex se burla
(comentarios, `/**/`, mayúsculas raras, CTEs con `INSERT` adentro). La
garantía real la da Postgres:

    SET TRANSACTION READ ONLY

Dentro de una transacción read-only, el motor **rechaza** INSERT, UPDATE,
DELETE, CREATE, DROP, ALTER, TRUNCATE, COPY FROM, nextval() y cualquier
DDL, con error 25006. No hay SQL que lo esquive. Y al final se hace
`ROLLBACK` siempre, pase lo que pase.

Encima de eso, en orden:

1. `_validar()` — tiene que empezar en SELECT/WITH/EXPLAIN/TABLE/VALUES, no
   puede traer dos sentencias, y no puede nombrar funciones que tocan el
   sistema (`pg_read_file`, `lo_export`, `pg_terminate_backend`, `dblink`…),
   que son read-only para Postgres pero no para nosotros.
2. `SET LOCAL statement_timeout` — una consulta pesada no puede colgar la
   app (el pool tiene pocas conexiones).
3. Tope de filas: se leen `limite + 1` y se avisa si quedó cortada.
4. `_redactar()` — cualquier columna que se llame como una credencial sale
   como `«oculto»`. Un `SELECT * FROM usuarios` no filtra el hash.
5. Bitácora: cada consulta queda en `scintela.sql_console_log` con usuario,
   texto, filas, ms e IP. Sirve para auditar y para volver a correr lo que
   hiciste ayer.

Permiso `usuarios.admin`, igual que el resto de `/admin`. El bootstrap de la
tabla es `CREATE TABLE IF NOT EXISTS` en caliente porque **el deploy no corre
migraciones** — mismo patrón que `saldo_snapshot` y la papelera.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal

from flask import (
    Blueprint,
    Response,
    g,
    jsonify,
    render_template,
    request,
)

import db
from auth import requiere_login, requiere_permiso

bp = Blueprint("sql_console", __name__, url_prefix="/admin/sql",
               template_folder="templates")

_LOG = logging.getLogger("programa_core.sql_console")

LIMITE_DEFAULT = 2000
LIMITE_MAX = 20000
TIMEOUT_MS_DEFAULT = 15000
TIMEOUT_MS_MAX = 120000

# Lo único que puede arrancar una consulta.
_ARRANQUES = ("select", "with", "explain", "table", "values", "show")

# Funciones que Postgres considera read-only pero que salen de la base:
# leen el disco del servidor, matan procesos o abren conexiones afuera.
_FUNCIONES_PROHIBIDAS = (
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "lo_put",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "pg_rotate_logfile", "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "dblink", "dblink_exec", "postgres_fdw_handler",
    "pg_logical_slot_get_changes", "pg_create_physical_replication_slot",
    "set_config", "pg_advisory_lock", "pg_advisory_xact_lock",
)

# Columnas cuyo contenido no se muestra nunca, sin importar la consulta.
_RE_COLUMNA_SENSIBLE = re.compile(
    r"(pass|passwd|password|clave|contrase|hash|salt|token|secret|"
    r"otp|totp|2fa|two_fa|recovery|api_key|apikey)",
    re.IGNORECASE,
)
_OCULTO = "«oculto»"


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------


def _sin_literales(sql: str) -> str:
    """Reemplaza strings, dollar-quotes y comentarios por espacios.

    Sin esto, un `SELECT 'no soy un ; de verdad'` se leería como dos
    sentencias, y un `-- pg_read_file` como una función prohibida. Todos los
    chequeos corren sobre ESTA versión, nunca sobre el texto crudo.
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'" or c == '"':
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:   # '' escapado
                        j += 2
                        continue
                    break
                j += 1
            out.append(" " * (min(j, n - 1) - i + 1))
            i = j + 1
            continue
        if c == "$":                                     # $$ ... $$ / $tag$
            m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j == -1 else j + len(tag)
                out.append(" " * (j - i))
                i = j
                continue
        if c == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue
        if c == "/" and sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(" " * (j - i))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _validar(sql: str) -> str | None:
    """Devuelve el motivo del rechazo, o None si puede correr.

    ⚠ Esto es la SEGUNDA línea de defensa. La primera es
    `SET TRANSACTION READ ONLY`, que la hace Postgres y no se puede burlar.
    Acá sólo se atajan las cosas que Postgres SÍ dejaría pasar en una
    transacción read-only.
    """
    if not (sql or "").strip():
        return "Escribí una consulta."

    limpio = _sin_literales(sql).strip()
    # El `;` final es normal — el que importa es uno en el medio.
    limpio_sin_punto = limpio.rstrip().rstrip(";").rstrip()

    if ";" in limpio_sin_punto:
        return ("Una sentencia por vez. Encontré un `;` en el medio — si es "
                "parte de un texto, ponelo entre comillas.")

    primera = re.match(r"[A-Za-z]+", limpio_sin_punto)
    if not primera or primera.group(0).lower() not in _ARRANQUES:
        return (f"Sólo lectura: la consulta tiene que empezar con "
                f"{', '.join(a.upper() for a in _ARRANQUES)}. "
                f"Esta empieza con «{(primera.group(0) if primera else limpio_sin_punto[:12])}».")

    bajo = limpio_sin_punto.lower()
    for fn in _FUNCIONES_PROHIBIDAS:
        if re.search(rf"\b{re.escape(fn)}\s*\(", bajo):
            return (f"`{fn}()` no se puede usar acá: Postgres la deja pasar en "
                    f"una transacción de sólo lectura, pero sale de la base "
                    f"(disco del servidor, otras conexiones o procesos).")

    # `EXPLAIN ANALYZE` EJECUTA la consulta. En read-only no puede escribir,
    # pero igual conviene que sea explícito y no un descuido.
    if bajo.startswith("explain") and re.search(r"\banalyze\b", bajo):
        return ("`EXPLAIN ANALYZE` corre la consulta de verdad. Usá "
                "`EXPLAIN` solo, o sacá el ANALYZE a propósito y volvé a "
                "mandarla sin él.")
    return None


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------


def _json_safe(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, bytes | memoryview):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, dict | list | tuple):
        return json.loads(json.dumps(v, default=str))
    return v


def _redactar(columnas: list[str], filas: list[list]) -> list[list]:
    """Tapa el contenido de las columnas que parecen credenciales.

    Un `SELECT * FROM scintela.usuarios` es una consulta legítima (ver quién
    entró, qué permisos tiene) y no tiene por qué mostrar el hash.
    """
    idx = [i for i, c in enumerate(columnas) if _RE_COLUMNA_SENSIBLE.search(c or "")]
    if not idx:
        return filas
    for f in filas:
        for i in idx:
            if f[i] is not None:
                f[i] = _OCULTO
    return filas


def correr(sql: str, *, limite: int = LIMITE_DEFAULT,
           timeout_ms: int = TIMEOUT_MS_DEFAULT) -> dict:
    """Corre la consulta en una transacción de SÓLO LECTURA y hace ROLLBACK.

    Devuelve `{ok, columnas, filas, n, truncado, ms, error}`.
    """
    motivo = _validar(sql)
    if motivo:
        return {"ok": False, "error": motivo, "columnas": [], "filas": [],
                "n": 0, "truncado": False, "ms": 0}

    limite = max(1, min(int(limite or LIMITE_DEFAULT), LIMITE_MAX))
    timeout_ms = max(1000, min(int(timeout_ms or TIMEOUT_MS_DEFAULT), TIMEOUT_MS_MAX))
    t0 = time.perf_counter()
    try:
        with db.get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    # ⚠ TIENE que ser lo primero de la transacción. Postgres
                    # sólo acepta SET TRANSACTION antes de la primera query.
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = %s", (timeout_ms,))
                    cur.execute(sql)
                    if cur.description is None:
                        return {"ok": True, "columnas": [], "filas": [], "n": 0,
                                "truncado": False,
                                "ms": int((time.perf_counter() - t0) * 1000),
                                "error": None}
                    columnas = [d[0] for d in cur.description]
                    crudas = cur.fetchmany(limite + 1)
                    truncado = len(crudas) > limite
                    filas = [[_json_safe(v) for v in f] for f in crudas[:limite]]
                    filas = _redactar(columnas, filas)
                    return {"ok": True, "columnas": columnas, "filas": filas,
                            "n": len(filas), "truncado": truncado,
                            "ms": int((time.perf_counter() - t0) * 1000),
                            "error": None}
            finally:
                # Nunca commit. Aunque la transacción sea read-only y no haya
                # nada que confirmar, se cierra explícitamente.
                conn.rollback()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc).strip(), "columnas": [],
                "filas": [], "n": 0, "truncado": False,
                "ms": int((time.perf_counter() - t0) * 1000)}


# ---------------------------------------------------------------------------
# Bitácora
# ---------------------------------------------------------------------------

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.sql_console_log (
    id         BIGSERIAL PRIMARY KEY,
    usuario    TEXT,
    consulta   TEXT,
    filas      INTEGER,
    ms         INTEGER,
    error      TEXT,
    ip         TEXT,
    creado_en  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sqlconsole_ts
    ON scintela.sql_console_log (creado_en DESC);
"""
_bootstrapped = False


def _bootstrap() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        db.execute(_BOOTSTRAP_SQL)
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("bootstrap sql_console_log falló: %s", exc)
    _bootstrapped = True


def _bitacora(sql: str, res: dict) -> None:
    """Fail-soft: si no se puede registrar, la consulta igual se contesta."""
    _bootstrap()
    try:
        db.execute(
            """
            INSERT INTO scintela.sql_console_log
                (usuario, consulta, filas, ms, error, ip)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            ((getattr(g, "user", None) or {}).get("usuario", "?")[:50]
             if isinstance(getattr(g, "user", None), dict)
             else str(getattr(g, "user", "?"))[:50],
             (sql or "")[:8000], res.get("n") or 0, res.get("ms") or 0,
             (res.get("error") or None), (request.remote_addr or "")[:45]),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("no pude registrar la consulta en la bitácora: %s", exc)


# ---------------------------------------------------------------------------
# Recetas — las preguntas que ya nos costaron caro
# ---------------------------------------------------------------------------

RECETAS = [
    {
        "id": "cadena-rota",
        "nombre": "Cadena de saldos rota",
        "que_contesta": "Filas donde el saldo guardado no se mueve por el "
                        "importe de la fila. Cada una es un quiebre.",
        "sql": """-- Quiebres de la cadena de saldos de un banco.
-- |saldo - saldo_anterior| tiene que ser igual a |importe|.
SELECT id_transaccion, fecha, documento, concepto, importe, saldo,
       ROUND(firmado, 2)                        AS delta_saldo,
       ROUND(ABS(ABS(firmado) - ABS(importe)), 2) AS gap
  FROM (
    SELECT id_transaccion, fecha, documento, concepto, importe, saldo,
           saldo - LAG(saldo) OVER (ORDER BY fecha, id_transaccion) AS firmado
      FROM scintela.transacciones_bancarias
     WHERE no_banco = 10
  ) t
 WHERE firmado IS NOT NULL
   AND ABS(ABS(firmado) - ABS(importe)) > 0.02
 ORDER BY fecha""",
    },
    {
        "id": "backdateadas",
        "nombre": "Filas cargadas con fecha vieja",
        "que_contesta": "La causa raíz del 03/08: una fila que entra HOY con "
                        "fecha de HACE UN MES rompe todo lo que camina por id.",
        "sql": """-- Filas cuyo id es posterior pero su fecha es anterior a lo ya cargado.
SELECT id_transaccion, fecha, documento, concepto, importe, saldo,
       max_fecha_previa,
       (max_fecha_previa - fecha) AS dias_hacia_atras
  FROM (
    SELECT *,
           MAX(fecha) OVER (ORDER BY id_transaccion
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
             AS max_fecha_previa
      FROM scintela.transacciones_bancarias
     WHERE no_banco = 10
  ) t
 WHERE fecha < max_fecha_previa
 ORDER BY id_transaccion DESC
 LIMIT 100""",
    },
    {
        "id": "saldo-vs-suma",
        "nombre": "Saldo guardado vs. suma de movimientos",
        "que_contesta": "Si estos dos números no coinciden, el saldo del "
                        "balance no se puede reconstruir sumando.",
        "sql": """SELECT no_banco,
       COUNT(*)                                      AS n_filas,
       MIN(fecha)                                    AS desde,
       MAX(fecha)                                    AS hasta,
       (SELECT saldo FROM scintela.transacciones_bancarias t2
         WHERE t2.no_banco = t.no_banco
         ORDER BY fecha DESC, id_transaccion DESC LIMIT 1) AS saldo_por_fecha,
       (SELECT saldo FROM scintela.transacciones_bancarias t3
         WHERE t3.no_banco = t.no_banco
         ORDER BY id_transaccion DESC LIMIT 1)             AS saldo_por_id
  FROM scintela.transacciones_bancarias t
 GROUP BY no_banco
 ORDER BY no_banco""",
    },
    {
        "id": "pendientes-resumen",
        "nombre": "Pendientes que en realidad son rótulos",
        "que_contesta": "Los $8.304.132,19 de basura del 03/08 entraron así: "
                        "el resumen del Excel cargado como movimientos.",
        "sql": """SELECT id, fecha, concepto, documento, monto, tipo, fuente, creado_en
  FROM scintela.banco_historicos_pendientes
 WHERE UPPER(TRIM(COALESCE(documento, ''))) ~
       '(TOTAL|AJUSTE|DIFERENCIA|SALDO|PENDIENTES BANCO)'
    OR UPPER(TRIM(COALESCE(concepto, ''))) ~
       '^(TOTAL|AJUSTE|DIFERENCIA|SALDO)$'
 ORDER BY monto DESC""",
    },
    {
        "id": "pendientes-resumen-total",
        "nombre": "Resumen de pendientes por banco",
        "que_contesta": "Cuánto suma cada lado del cálculo de la conciliación.",
        "sql": """SELECT no_banco,
       COUNT(*)                                             AS n,
       COUNT(*) FILTER (WHERE fecha IS NULL)                AS sin_fecha,
       ROUND(SUM(CASE WHEN tipo = 'D' THEN -monto ELSE monto END), 2) AS neto,
       ROUND(SUM(monto) FILTER (WHERE tipo <> 'D'), 2)      AS creditos,
       ROUND(SUM(monto) FILTER (WHERE tipo = 'D'), 2)       AS debitos
  FROM scintela.banco_historicos_pendientes
 GROUP BY no_banco
 ORDER BY no_banco""",
    },
    {
        "id": "matches-batch",
        "nombre": "Matches N:M que no cierran",
        "que_contesta": "Agrupado por confirm_batch_id (NO por transacción), "
                        "que es lo que hacía dar 36 falsos positivos.",
        "sql": """WITH b AS (
  SELECT COALESCE(m.confirm_batch_id, 'tx:' || m.id_transaccion::text) AS batch,
         m.id_transaccion, m.real_fecha, m.real_documento,
         m.real_monto, m.real_tipo
    FROM scintela.banco_matches m
   WHERE m.estado = 'matched' AND m.deshecho_en IS NULL
), banco AS (
  SELECT batch, SUM(CASE WHEN real_tipo = 'D' THEN -real_monto ELSE real_monto END) AS monto
    FROM (SELECT DISTINCT batch, real_fecha, real_documento, real_monto, real_tipo FROM b) d
   GROUP BY batch
), prog AS (
  SELECT b.batch, SUM(t.importe) AS monto, COUNT(*) AS n_pc
    FROM (SELECT DISTINCT batch, id_transaccion FROM b) b
    JOIN scintela.transacciones_bancarias t USING (id_transaccion)
   GROUP BY b.batch
)
SELECT banco.batch, banco.monto AS monto_banco, prog.monto AS monto_pc,
       ROUND(ABS(banco.monto) - ABS(prog.monto), 2) AS gap, prog.n_pc
  FROM banco JOIN prog USING (batch)
 WHERE ABS(ABS(banco.monto) - ABS(prog.monto)) > 0.02
 ORDER BY ABS(ABS(banco.monto) - ABS(prog.monto)) DESC""",
    },
    {
        "id": "historia-duplicada",
        "nombre": "Fotos repetidas del mismo día en historia",
        "que_contesta": "Por qué el patrimonio de un mes cerrado cambia solo: "
                        "hay varias filas del mismo día y se lee cualquiera.",
        "sql": """SELECT fecha, COUNT(*) AS fotos,
       ROUND(MAX(patrimonio) - MIN(patrimonio), 2) AS spread_patrimonio,
       ROUND(MAX(utilidad)   - MIN(utilidad), 2)   AS spread_utilidad,
       MIN(id_historia) AS primer_id, MAX(id_historia) AS ultimo_id
  FROM scintela.historia
 GROUP BY fecha
HAVING COUNT(*) > 1
 ORDER BY fecha DESC
 LIMIT 60""",
    },
    {
        "id": "movs-banco",
        "nombre": "Movimientos de un banco en un rango",
        "que_contesta": "El listado crudo, con el delta de saldo al lado. "
                        "Cambiá las fechas y el no_banco.",
        "sql": """SELECT id_transaccion, fecha, documento, concepto, importe, saldo,
       usuario_crea,
       ROUND(saldo - LAG(saldo) OVER (ORDER BY fecha, id_transaccion), 2) AS delta
  FROM scintela.transacciones_bancarias
 WHERE no_banco = 10
   AND fecha BETWEEN '2026-07-01' AND '2026-08-31'
 ORDER BY fecha, id_transaccion""",
    },
    {
        "id": "columnas",
        "nombre": "¿Qué columnas tiene una tabla?",
        "que_contesta": "Para no adivinar nombres. Cambiá el LIKE.",
        "sql": """SELECT table_name, ordinal_position AS pos, column_name,
       data_type, is_nullable
  FROM information_schema.columns
 WHERE table_schema = 'scintela'
   AND table_name LIKE '%banco%'
 ORDER BY table_name, ordinal_position""",
    },
    {
        "id": "bitacora",
        "nombre": "Qué consultas se corrieron acá",
        "que_contesta": "La bitácora de esta misma consola.",
        "sql": """SELECT id, creado_en, usuario, filas, ms,
       LEFT(REPLACE(consulta, E'\\n', ' '), 160) AS consulta, error
  FROM scintela.sql_console_log
 ORDER BY creado_en DESC
 LIMIT 50""",
    },
]


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


def _params() -> tuple[str, int, int, str]:
    src = request.form if request.method == "POST" else request.args
    sql = (src.get("q") or src.get("sql") or "").strip()
    try:
        limite = int(src.get("limite") or LIMITE_DEFAULT)
    except (TypeError, ValueError):
        limite = LIMITE_DEFAULT
    try:
        timeout = int(src.get("timeout_ms") or TIMEOUT_MS_DEFAULT)
    except (TypeError, ValueError):
        timeout = TIMEOUT_MS_DEFAULT
    formato = (src.get("formato") or "").lower().strip()
    if not formato:
        acepta = (request.headers.get("Accept") or "")
        formato = "json" if "application/json" in acepta and "text/html" not in acepta else "html"
    return sql, limite, timeout, formato


@bp.route("", methods=["GET", "POST"])
@bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def consola():
    sql, limite, timeout, formato = _params()
    res = None
    if sql:
        res = correr(sql, limite=limite, timeout_ms=timeout)
        _bitacora(sql, res)

    if formato == "json":
        return jsonify(res or {"ok": True, "filas": [], "columnas": [], "n": 0,
                               "error": None, "ms": 0, "truncado": False})

    if formato == "csv" and res and res["ok"]:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(res["columnas"])
        w.writerows(res["filas"])
        return Response(
            "﻿" + buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=consulta.csv"},
        )

    return render_template("admin_dbase/sql_console.html", sql=sql, res=res,
                           limite=limite, timeout_ms=timeout, recetas=RECETAS)
