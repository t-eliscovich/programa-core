---
name: programa-core-integraciones
description: Bringing live data from Asinfo (Intela ERP, SQL Server) and formulas_app (textile dyeing app, PostgreSQL) into Programa Core for display in views. Covers the bridge architecture (Metabase API for Asinfo, direct Postgres pool for formulas_app), the read-only reader role on RDS, env-var contracts, the shared Metabase client, the adapter factory pattern (Fake / Metabase / Postgres), and the gotchas that already burned past sessions (date columns as TEXT, integer-categorical casts, token expiration, cross-database vs cross-schema, fail-closed adapters). Use this skill whenever the user mentions Asinfo, ERP, Metabase, formulas_app data, `costos_ot`, "traer data de", or asks to wire a Programa Core view to data living outside its own DB. Pair with `programa-core` (the host app), `intela-formulas-app` (source schema), and `intela-aws-deploy` (where Metabase, the ERP and the RDS live).
---

# Programa Core — integraciones (Asinfo + formulas_app)

Programa Core consume data de tres orígenes distintos: su propia DB Postgres (canon), **Asinfo** (el ERP histórico de Intela, SQL Server) y **formulas_app** (la app de tintorería que vive en el mismo RDS). Esta skill explica cómo conectar cada uno sin meter drivers raros, sin duplicar credenciales, y manteniendo el principio operativo de Programa Core: **el host nunca se rompe porque una fuente externa cae — degradás silenciosamente.**

Si el cambio toca el deploy / EC2 / SG / SSM, también cargar `intela-aws-deploy`. Si el cambio toca el schema de origen en tintorería, también `intela-formulas-app`.

## El panorama de una sola mirada

```
              ┌────────────────────────────────────────────────────────────────┐
              │                         EC2 us-east-2                          │
              │                                                                │
   factory IP │   ┌──────────────┐                ┌─────────────────────┐      │
  ────────────┼──▶│ Programa     │──── psycopg2 ──┤  RDS Postgres       │      │
              │   │ Core (5050+) │      pool      │  ┌────────────────┐ │      │
              │   │              │       (canon)  │  │ db "intela"    │ │      │
              │   │              │                │  │  scintela.*    │ │      │
              │   │              │                │  │  seguridad.*   │ │      │
              │   │              │                │  └────────────────┘ │      │
              │   │              │                │  ┌────────────────┐ │      │
              │   │              │── psycopg2 ────┼─▶│ db "postgres"  │ │      │
              │   │              │   pool         │  │  (formulas_app)│ │      │
              │   │              │   (read-only)  │  └────────────────┘ │      │
              │   │              │                │  ┌────────────────┐ │      │
              │   │              │                │  │ db "metabase"  │ │      │
              │   │              │                │  │  (metadata)    │ │      │
              │   │              │                │  └────────────────┘ │      │
              │   │              │                └─────────────────────┘      │
              │   │              │                                             │
              │   │              │── HTTP API ───┐                             │
              │   │              │   (token)     │                             │
              │   │              │               ▼                             │
              │   │              │     ┌──────────────────┐                    │
              │   │              │     │ Metabase :3000   │                    │
              │   │              │     │ DB1 metadata     │                    │
              │   │              │     │ DB2 Asinfo (SQL  │── private VPN ────▶│ Asinfo SQL Server
              │   │              │     │     Server)     ─┼─────────────────── │ 213.165.237.20:1401
              │   │              │     │ DB3 formulas_app │                    │
              │   └──────────────┘     │     (RDS)        │                    │
              │                        └──────────────────┘                    │
              └────────────────────────────────────────────────────────────────┘
```

Hay **tres caminos** y cada uno tiene su uso óptimo:

| Camino | Qué consume | Por qué este |
|---|---|---|
| `db.*` (pool propio) | `scintela.*`, `seguridad.*` | Canon. Lo que escribe Programa Core. |
| `formulas_db.*` (pool a "postgres" DB) | `ordenes`, `orden_lineas`, `orden_piezas`, `formulas`, `formula_items`, `productos`, `inventario`, `compras`, `costos` | Mismo cluster RDS, distinta DB. Latencia ~5ms, JOINs eficientes adentro de formulas_app. **Bridge preferido para formulas_app.** |
| `metabase_client.fetch_card(N)` | Datos de Asinfo (DB 2 en Metabase) y opcionalmente data de formulas_app vía card guardada | Asinfo no es reachable directo desde Programa Core (firewall + dialecto). Metabase ya tiene la conexión y las cards SQL están escritas y debugueadas. **Bridge único para Asinfo.** |

**Regla de oro**: nunca importar `pymssql` ni driver SQL Server en Programa Core. Si necesitás algo de Asinfo, pasa por Metabase.

## Variables de entorno — contrato

Las nuevas vars que tiene que llevar el `.env` (y el env "Machine" del EC2 cuando deploy):

```bash
# ---------------------------------------------------------------------------
# Bridge formulas_app (Postgres directo, mismo RDS, otra DB)
# ---------------------------------------------------------------------------
# Conexión read-only. El usuario `programa_core_reader` solo tiene SELECT
# en las tablas listadas en setup_formulas_reader.py.
FORMULAS_DATABASE_URL=postgresql://programa_core_reader:<pwd>@intela-db.c988ucsko537.us-east-2.rds.amazonaws.com:5432/postgres?sslmode=require
FORMULAS_POOL_MIN=1
FORMULAS_POOL_MAX=4

# ---------------------------------------------------------------------------
# Bridge Metabase (para Asinfo + fallback de formulas_app si hace falta)
# ---------------------------------------------------------------------------
# En EC2: http://localhost:3000 (mismo box). En dev local: dejar vacío para
# que el adapter degrade a "no disponible".
METABASE_URL=http://localhost:3000
METABASE_USERNAME=integracion@intela.com.ec
METABASE_PASSWORD=<password>

# Card IDs — DB 2 (Asinfo / ERP SQL Server). Documentados en
# intela-aws-deploy SKILL.md sección "Metabase dashboard & card inventory".
ASINFO_CARD_VENDEDOR_USD=116
ASINFO_CARD_VENDEDOR_KG=163
ASINFO_CARD_CLIENTE_KG=164

# Card opcional formulas_app (DB 3) — solo si la usás como fallback del
# adapter de costos_ot.
METABASE_QUESTION_ID_COSTOS_OT=

# ---------------------------------------------------------------------------
# Adapter switches
# ---------------------------------------------------------------------------
# fake / metabase / postgres. Default = fake (dev sin config).
# En producción: postgres (formulas_app vive en el mismo RDS).
COSTOS_OT_ADAPTER=postgres
```

`COSTOS_OT_ADAPTER` ya existe en el código (`modules/costos_ot/adapters.py`). Las otras vars son nuevas.

Convenciones a respetar:
- **Nunca commitear** `.env` real. `.env.example` solo lleva placeholders.
- **Rotación** de la `METABASE_PASSWORD` y de la password de `programa_core_reader`: se rota vía script en EC2 (sección "Rotación" más abajo). Programa Core lo lee del env de máquina; reinicio del Scheduled Task lo refresca.
- **En dev local** (Mac), las vars de Metabase y `FORMULAS_DATABASE_URL` quedan vacías → los adapters degradan a `disponible()=False` y los módulos consumidores muestran un placeholder. No hay forma de pegarle al ERP desde una laptop fuera de la red de la fábrica de todas formas.

## Lado RDS — el rol `programa_core_reader`

Mismo patrón que `metabase_reader` (ver `intela-aws-deploy`): un rol read-only que vive en la DB `postgres` del cluster, con SELECT explícito en las tablas que Programa Core quiere leer de formulas_app.

```sql
-- corrido por scripts/setup_formulas_reader.py vía SSM (la DB es privada)
DROP OWNED BY programa_core_reader CASCADE;
DROP ROLE IF EXISTS programa_core_reader;
CREATE ROLE programa_core_reader WITH LOGIN PASSWORD '<rotable>';

GRANT CONNECT ON DATABASE "postgres" TO programa_core_reader;
GRANT USAGE ON SCHEMA public TO programa_core_reader;

-- Tablas explícitamente expuestas. Si formulas_app agrega tablas internas
-- (queue jobs, audit, etc.) NO les damos SELECT por default.
GRANT SELECT ON
    public.ordenes,
    public.orden_lineas,
    public.orden_piezas,
    public.formulas,
    public.formula_items,
    public.productos,
    public.inventario,
    public.inventario_ajustes,
    public.compras,
    public.costos,
    public.proveedores
  TO programa_core_reader;

-- Para que tablas nuevas heredadas del owner default también queden visibles
-- si las agregamos a la GRANT explícita arriba en el futuro.
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
  GRANT SELECT ON TABLES TO programa_core_reader;
```

El `<owner>` se completa del usuario que parsea `DATABASE_URL` en EC2 (igual que en el setup de `metabase_reader`). Idempotente: re-correrlo simplemente rota la contraseña.

Por qué SELECT explícito en tabla-por-tabla y no `SELECT ON ALL TABLES IN SCHEMA public`:
- formulas_app tiene tablas internas (jobs, locks, ajustes manuales firmados) que no queremos exponer.
- El día que formulas_app agrega una tabla sensible nueva, no se filtra silenciosamente.
- El rol funciona como un **contrato leíble**: estas son las superficies acopladas entre las dos apps.

### Cuando agregás una superficie nueva

Si necesitás leer una tabla de formulas_app que no está en la lista de arriba:

1. Pedírsela a la persona que mantiene formulas_app (verificar que sea data que querés acoplar — no algo interno temporal).
2. Agregar la tabla al `GRANT SELECT` en `scripts/setup_formulas_reader.py`.
3. Re-correr el script en EC2 (idempotente).
4. Documentarla acá en este SKILL.md.

## Lado Programa Core — los pools y clientes

### `modules/_lib/formulas_db.py` — pool read-only a formulas_app

Pool aparte del pool principal de `db.py`. Misma forma (`SimpleConnectionPool`, `RealDictCursor`, helpers `fetch_one/fetch_all`), pero apuntando a `FORMULAS_DATABASE_URL`.

```python
"""Pool read-only contra la DB de formulas_app (mismo cluster RDS, otra DB).

Convención dura:
    - NUNCA escribir desde acá. El rol DB ya es SELECT-only; este módulo solo
      ofrece fetch_* / fetch_one. No exponemos execute/commit.
    - Si FORMULAS_DATABASE_URL no está seteada, init_pool() no abre nada y
      cada helper devuelve [] / None con un log a WARNING. Programa Core sigue
      vivo aunque formulas_app esté caído o no esté configurado todavía.
"""
import logging
import os
from contextlib import contextmanager

from psycopg2 import pool
from psycopg2.extras import RealDictCursor

_log = logging.getLogger("programa_core.formulas_db")
_pool: pool.SimpleConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    url = os.environ.get("FORMULAS_DATABASE_URL", "").strip()
    if not url:
        _log.info("FORMULAS_DATABASE_URL vacío — bridge a formulas_app deshabilitado")
        return
    _pool = pool.SimpleConnectionPool(
        minconn=int(os.environ.get("FORMULAS_POOL_MIN", "1")),
        maxconn=int(os.environ.get("FORMULAS_POOL_MAX", "4")),
        dsn=url,
    )


def disponible() -> bool:
    return _pool is not None


@contextmanager
def _conn():
    if _pool is None:
        yield None
        return
    c = _pool.getconn()
    try:
        yield c
    finally:
        _pool.putconn(c)


def fetch_all(sql: str, params=()) -> list[dict]:
    if _pool is None:
        return []
    try:
        with _conn() as c:
            with c.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    except Exception as e:
        _log.warning("formulas_db.fetch_all falló: %s", e)
        return []


def fetch_one(sql: str, params=()) -> dict | None:
    if _pool is None:
        return None
    try:
        with _conn() as c:
            with c.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return cur.fetchone()
    except Exception as e:
        _log.warning("formulas_db.fetch_one falló: %s", e)
        return None
```

Wireado en `app.py:create_app()` después de `db.init_pool()`:

```python
from modules._lib import formulas_db
formulas_db.init_pool()  # no-op si FORMULAS_DATABASE_URL está vacía
```

### `modules/_lib/metabase_client.py` — cliente HTTP compartido

Hoy esa lógica vive duplicada dentro de `MetabaseAdapter` en `modules/costos_ot/adapters.py`. Al agregar Asinfo, la **extraemos a un módulo compartido** y `MetabaseAdapter` pasa a usarlo. Una sola implementación de login + refresh.

```python
"""Cliente Metabase compartido para todos los bridges externos.

- Login lazy + refresh on 401.
- fetch_card(card_id) -> list[dict].
- fetch_card_parameterized(card_id, params) -> list[dict] (cuando la card
  tiene template-tags).
- Siempre fail-soft: cualquier excepción se loguea y devuelve [].
"""
import logging
import os

_log = logging.getLogger("programa_core.metabase_client")
_session_token: str | None = None


def _url() -> str | None:
    u = os.environ.get("METABASE_URL", "").strip()
    return u.rstrip("/") if u else None


def _creds() -> tuple[str | None, str | None]:
    return (os.environ.get("METABASE_USERNAME"), os.environ.get("METABASE_PASSWORD"))


def disponible() -> bool:
    return bool(_url() and all(_creds()))


def _login(requests_mod) -> str | None:
    global _session_token
    user, pwd = _creds()
    if not (_url() and user and pwd):
        return None
    try:
        r = requests_mod.post(
            f"{_url()}/api/session",
            json={"username": user, "password": pwd},
            timeout=5,
        )
        r.raise_for_status()
        _session_token = r.json().get("id")
        return _session_token
    except Exception as e:
        _log.warning("Metabase login falló: %s", e)
        return None


def fetch_card(card_id: int | str, params: list[dict] | None = None) -> list[dict]:
    """POST /api/card/<id>/query/json, con refresh on 401.

    `params` opcional: lista de {"type": "category", "target": [...], "value": ...}
    siguiendo el formato Metabase para template-tags.
    """
    if not card_id or not disponible():
        return []
    try:
        import requests
    except ImportError:
        _log.warning("requests no disponible — Metabase bridge devuelve []")
        return []
    global _session_token
    token = _session_token or _login(requests)
    if not token:
        return []
    url = f"{_url()}/api/card/{card_id}/query/json"
    body = {"parameters": params} if params else {}
    try:
        r = requests.post(url, json=body, headers={"X-Metabase-Session": token}, timeout=15)
        if r.status_code == 401:
            _session_token = None
            token = _login(requests)
            if not token:
                return []
            r = requests.post(url, json=body, headers={"X-Metabase-Session": token}, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        _log.warning("Metabase fetch_card(%s) falló: %s", card_id, e)
        return []
```

> **Advertencia sobre los snippets SQL que siguen.** Los nombres de columna
> (`o.pcod`, `o.codigo_cli`, `o.kg_pieza`, `o.fecha_cierre`, `p.activo`, etc.)
> vienen heredados del `PostgresAdapter` original de `costos_ot/adapters.py`
> + de la skill `intela-formulas-app`. Como Programa Core nunca corrió este
> bridge en vivo contra la formulas_app real (siempre estuvo en
> `COSTOS_OT_ADAPTER=fake`), **algunos nombres pueden estar desactualizados o
> ser placeholders**. Antes de pegar estos SQLs en producción:
> 1. Cargá la skill `intela-formulas-app` y abrí `formulas_app/database.py`
>    para confirmar nombres exactos de columna.
> 2. Probá la query con `formulas_db.fetch_all` apuntando a la RDS real
>    (después de que el rol `programa_core_reader` esté creado) y validá
>    que el resultset tenga las claves que tu template espera.
> Los snippets sirven como esqueleto — la sintaxis, los `TO_DATE`, el patrón
> de cast `::text`, los joins lógicos — pero los nombres físicos hay que
> reconfirmarlos.

### Refactor de `modules/costos_ot/adapters.py`

`MetabaseAdapter` se reduce a un wrapping de `metabase_client.fetch_card`:

```python
@dataclass
class MetabaseAdapter:
    fuente: str = "metabase"

    def disponible(self) -> bool:
        from modules._lib import metabase_client
        return metabase_client.disponible() and bool(os.environ.get("METABASE_QUESTION_ID_COSTOS_OT"))

    def costos_por_cliente(self, codigo_cli: str) -> list[OTCosto]:
        from modules._lib import metabase_client
        card_id = os.environ.get("METABASE_QUESTION_ID_COSTOS_OT")
        rows = metabase_client.fetch_card(card_id)
        codigo_cli = (codigo_cli or "").strip().upper()
        return [self._row_to_costo(r) for r in rows
                if str(r.get("cliente_codigo", "")).upper() == codigo_cli]
    # ... resto igual
```

Y `PostgresAdapter` se rehace para que use `formulas_db` (porque hoy asume cross-schema en el mismo DB, lo cual NO es cierto — formulas_app vive en una DB distinta del mismo cluster, no en un schema distinto):

```python
@dataclass
class PostgresAdapter:
    fuente: str = "postgres"

    def disponible(self) -> bool:
        from modules._lib import formulas_db
        return formulas_db.disponible()

    def costos_por_cliente(self, codigo_cli: str) -> list[OTCosto]:
        from modules._lib import formulas_db
        codigo_cli = (codigo_cli or "").strip().upper()
        if not codigo_cli:
            return []
        rows = formulas_db.fetch_all(
            """
            SELECT
                o.pcod                                    AS n_orden,
                TO_DATE(o.fecha, 'DD/MM/YYYY')            AS fecha_cierre,
                o.codigo_cli                              AS cliente_codigo,
                f.color || ' · ' || f.categoria           AS descripcion,
                SUM(ol.cantidad_kg)                       AS kg,
                CASE WHEN SUM(ol.cantidad_kg) > 0
                     THEN SUM(ol.cantidad_kg * p.us) / SUM(ol.cantidad_kg)
                     ELSE 0 END                           AS costo_kg
              FROM ordenes o
              JOIN formulas f ON f.cod = o.codigo
              JOIN orden_lineas ol ON ol.orden_id = o.id
              JOIN productos p ON p.num = ol.producto_num
             WHERE UPPER(o.codigo_cli) = %s
               AND o.fecha_cierre IS NOT NULL
             GROUP BY o.id, o.pcod, o.fecha, o.codigo_cli, f.color, f.categoria
             ORDER BY TO_DATE(o.fecha, 'DD/MM/YYYY') DESC NULLS LAST
            """,
            (codigo_cli,),
        )
        return [self._row_to_costo(r) for r in rows]
```

**Atención al `TO_DATE(o.fecha, 'DD/MM/YYYY')`**: en formulas_app, `ordenes.fecha` está como TEXT formato dd/mm/yyyy. Un `ORDER BY o.fecha DESC` ordena alfabéticamente y devuelve la "fecha" equivocada. Esto ya está documentado en `intela-aws-deploy` y es un gotcha recurrente.

## Módulos consumidores — convención

Cada superficie que necesita data externa va en su propio módulo bajo `modules/`. Tres reglas duras:

1. **Una sola superficie por módulo.** No mezclar Asinfo + formulas_app en el mismo `views.py`.
2. **El módulo nunca rompe el host.** Si el adapter levanta o devuelve `[]`, la vista renderiza un placeholder ("Sin datos disponibles en este momento") en lugar de 500.
3. **Fuente visible en la UI.** Cuando estamos en `fake` mode, se ve un badge gris ("DATA DE EJEMPLO") en la esquina. En `metabase` / `postgres` mode también, pero discreto. El usuario tiene que saber si está mirando data real.

Ejemplo — `modules/asinfo/views.py`:

```python
import os
from flask import Blueprint, render_template

from auth import requiere_permiso
from modules._lib import metabase_client

bp = Blueprint("asinfo", __name__, url_prefix="/asinfo", template_folder="templates")


@bp.route("/ventas-vendedor")
@requiere_permiso("informes.ver")
def ventas_vendedor():
    card_id = os.environ.get("ASINFO_CARD_VENDEDOR_USD")
    rows = metabase_client.fetch_card(card_id) if card_id else []
    fuente = "metabase" if rows else ("vacío" if metabase_client.disponible() else "no_configurado")
    return render_template("asinfo/ventas_vendedor.html", rows=rows, fuente=fuente)
```

Y `modules/tintura/views.py`:

```python
from flask import Blueprint, render_template

from auth import requiere_permiso
from modules._lib import formulas_db

bp = Blueprint("tintura", __name__, url_prefix="/tintura", template_folder="templates")


@bp.route("/ordenes")
@requiere_permiso("informes.ver")
def ordenes():
    rows = formulas_db.fetch_all(
        """
        SELECT
            o.pcod                                AS orden,
            TO_DATE(o.fecha, 'DD/MM/YYYY')        AS fecha,
            o.codigo_cli                          AS cliente,
            f.color, f.categoria,
            o.kg_pieza::text || ' kg'             AS kg,
            o.jet::text                           AS jet,
            o.estado
          FROM ordenes o
          LEFT JOIN formulas f ON f.cod = o.codigo
         ORDER BY TO_DATE(o.fecha, 'DD/MM/YYYY') DESC NULLS LAST
         LIMIT 200
        """
    )
    return render_template(
        "tintura/ordenes.html",
        rows=rows,
        disponible=formulas_db.disponible(),
    )


@bp.route("/stock")
@requiere_permiso("stock.ver")
def stock():
    # Stock = última lectura de inventario por producto (ver intela-formulas-app
    # para por qué inventario es daily snapshot y la "current" es la más reciente).
    rows = formulas_db.fetch_all(
        """
        WITH latest AS (
            SELECT producto_num, MAX(TO_DATE(fecha, 'DD/MM/YYYY')) AS fecha
              FROM inventario
             GROUP BY producto_num
        )
        SELECT p.familia, p.num_visible, p.nombre,
               i.cantidad_kg AS stock, i.fecha,
               p.us           AS precio_us
          FROM productos p
          LEFT JOIN latest l ON l.producto_num = p.num
          LEFT JOIN inventario i ON i.producto_num = p.num
                                AND TO_DATE(i.fecha, 'DD/MM/YYYY') = l.fecha
         WHERE p.activo = TRUE
         ORDER BY p.familia, p.num_visible
        """
    )
    return render_template("tintura/stock.html", rows=rows)
```

Ambas vistas registran su blueprint en `app.py:create_app()` con `app.register_blueprint(bp)`.

## Permisos

- `informes.ver` ya existe y es lo correcto para vistas de lectura cross-app.
- Si querés un permiso específico, agregalo en `config/roles.py` y corré `python scripts/migrate.py --force 0003` (la migración upsertea roles + permisos en cada deploy).
- Los roles **QC** y **Bodega** ya tienen sentido para tintura — QC ve órdenes y stock, Bodega ve solo stock.

## Gotchas RDS aprendidos en el deploy real (2026-05-21)

Tres errores que mataron tres veces el `setup_formulas_reader.py` antes de quedar verde. Cualquier `setup_*_reader.py` futuro contra RDS tiene que tenerlos en cuenta de entrada.

**1. `DROP OWNED BY <role>` falla con `UndefinedObject` si el rol no existe.**
La intuición es "DROP OWNED es idempotente como DROP IF EXISTS" — no lo es. Si el rol nunca se creó, levanta `psycopg2.errors.UndefinedObject: role "X" does not exist`. Patrón correcto:
```python
cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'X'")
if cur.fetchone() is not None:
    cur.execute("DROP OWNED BY X CASCADE")
    cur.execute("DROP ROLE X")
```
NO sirve `DROP ROLE IF EXISTS` solo, porque no limpia los GRANTs previos — la próxima `CREATE ROLE` arranca con la pizarra colgada de ownerships viejos.

**2. En RDS el master user NO es full-superuser → necesita membership para `DROP OWNED BY`.**
RDS hostea como `rds_superuser`, no como `superuser` real. Aún siendo el admin, no podés droppearle objetos a un rol del que no sos miembro. Sale: `psycopg2.errors.InsufficientPrivilege: permission denied to drop objects. Only roles with privileges of role "X" may drop objects owned by it.`
Fix: dar membership al admin antes del DROP:
```python
cur.execute(
    sql.SQL("GRANT {} TO {}").format(
        sql.Identifier("programa_core_reader"),
        sql.Identifier(admin_user),
    )
)
cur.execute("DROP OWNED BY programa_core_reader CASCADE")
cur.execute("DROP ROLE programa_core_reader")
```
El GRANT no necesita revocarse — el rol se va a borrar dos líneas después.

**3. `GRANT SELECT ON public.<tabla>` falla si la tabla no existe.**
Una tabla mal incluida en `EXPOSED_TABLES` (porque el SKILL fuente decía "costos" pero "costos" es un cálculo on-the-fly, no una tabla real) mata toda la corrida. Y como `setup_*_reader.py` no es transaccional (necesita `AUTOCOMMIT` para `CREATE ROLE`), las tablas que SÍ se grantearon antes quedan committeadas con el rol a medio armar.
Patrón defensive:
```python
granted, missing = [], []
for tbl in EXPOSED_TABLES:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (tbl,),
    )
    if cur.fetchone() is None:
        missing.append(tbl)
        continue
    cur.execute(sql.SQL("GRANT SELECT ON public.{} TO X").format(sql.Identifier(tbl)))
    granted.append(tbl)
print(f"granted: {granted}  missing: {missing}")
```
Imprimir las "missing" en el output al final ayuda a detectar drift de schema entre la skill y la realidad sin tener que abrir el log de error.

### Tablas reales de formulas_app (al 2026-05-21)

Confirmadas leyendo `init_db()` en `formulas_app/database.py`:

`productos, formulas, formula_items, ordenes, orden_piezas, orden_lineas, inventario, inventario_ajustes, compras, precio_historial, tela_catalog`.

**NO existen** como tabla aunque uno esperaría:
- `costos` → es `get_costos_report()` calculada on-the-fly.
- `proveedores` → es un campo string en `productos.prov` y `compras.proveedor`, no una entidad separada.
- `clientes` → no existe el concepto en formulas_app (ver sección "Cliente en formulas_app — no existe").

Cuando agregues una tabla a `EXPOSED_TABLES`, abrí `database.py` de formulas_app y buscá `CREATE TABLE IF NOT EXISTS <tabla>` ANTES de pushear. Una tabla nueva mal escrita rompe la próxima corrida del setup.

## Gotchas heredados (no los rebusques)

**Date columns como TEXT 'DD/MM/YYYY' en formulas_app**
- `ordenes.fecha`, `inventario.fecha`, `inventario_ajustes.fecha`, `compras.fecha`.
- Cualquier `ORDER BY`, `MIN`, `MAX`, comparación con fecha = `TO_DATE(<col>, 'DD/MM/YYYY')` SIEMPRE.
- Sin el cast, `MIN(fecha)` devuelve la fecha "01/01/2020" en lugar de la más temprana real porque ordena alfabéticamente.

**`num_visible` y `jet` son `integer` pero categóricos**
- En SQL: `LOWER(num_visible)` rompe. Cast: `LOWER(num_visible::text)` o `num_visible::text LIKE`.
- En charts: si los pasás a Metabase como integer, te los pone en eje continuo. Pasalos como `jet::text` cuando son buckets.

**`pcod` filter en Programa Core**
- `formulas_app` usa el filtro Jinja `pcod` para mostrar productos como `(familia, num_visible)`. Si tu vista en Programa Core muestra productos de formulas_app, **NO importes el filtro de allá**. Replicalo en Programa Core con un cache local — o mejor, devolvélo armado desde el SQL: `p.familia || '-' || p.num_visible::text AS pcod`.

**Token Metabase expira en 14 días**
- El cliente maneja el refresh en `401`. No hace falta tocar nada. Pero si Programa Core arranca y la primera llamada falla con 401 dos veces seguidas (login también falló) → la password de Metabase rotó. Ver "Rotación" abajo.

**Una sola DB de Postgres por proceso, pero múltiples pools**
- Programa Core tiene **dos pools**: `db` (canon, escribe) y `formulas_db` (read-only). No los mezcles. Una transacción de cobranza en `db.tx()` NO ve las tablas de formulas_app — necesitarías un `postgres_fdw` para eso, y no vale la pena por ahora (display-only).

**Si en el futuro hace falta JOIN nativo entre `scintela.*` y `formulas_app.public.*`**
- Habilitar `postgres_fdw` en la DB `intela`, hacer `CREATE SERVER formulas_app` apuntando al mismo cluster pero DB `postgres`, `IMPORT FOREIGN SCHEMA public LIMIT TO (...)` con las tablas listadas en el rol reader.
- Esto te deja escribir `JOIN formulas_app.ordenes o ON o.codigo_cli = c.codigo_cli` desde la DB de Programa Core.
- **No lo activamos hoy** porque (a) display-only no lo necesita, (b) un FDW agrega un punto de falla y latencia menos predecible que un pool aparte, (c) los pools separados ya nos dan el aislamiento de blast-radius que queremos.

**Fail-closed, no fail-open**
- Si el adapter devuelve `[]` no significa "no hay data" — puede significar "Metabase está caído" o "rol DB sin permisos". El módulo consumidor distingue los tres estados:
  - `disponible() == False` → "no configurado" (env vacío)
  - `disponible() == True` y `rows == []` → "sin datos" (legítimo)
  - excepción → loggeada como WARNING, también devuelve `[]`
- La UI muestra un badge tenue ("Fuente: Metabase · sin datos") en lugar de un error rojo. Empuja al usuario a chequear cuándo es legítimo y cuándo no.

## Setup en EC2 — corre una vez

### 1. Crear el rol `programa_core_reader` en la DB `postgres`

Script en `scripts/setup_formulas_reader.py` (commit + push → GitHub Actions deploy al servidor). Después correrlo vía SSM:

```bash
# en CloudShell
aws ssm send-command \
  --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable(\"DATABASE_URL\",\"Machine\"); C:\\Python312\\python.exe C:\\programa-core\\scripts\\setup_formulas_reader.py"]' \
  --output text --query 'Command.CommandId'
```

El script lee `DATABASE_URL` de la máquina, conecta a la DB `postgres` (no a la `intela`), corre el SQL del rol (sección "Lado RDS" arriba), y al final imprime la connection string lista para pegar en `FORMULAS_DATABASE_URL`. Idempotente: re-correrlo rota la contraseña.

### 2. Setear las env vars nuevas a nivel Machine

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["[Environment]::SetEnvironmentVariable(\"FORMULAS_DATABASE_URL\", \"<output del paso 1>\", \"Machine\"); [Environment]::SetEnvironmentVariable(\"METABASE_URL\", \"http://localhost:3000\", \"Machine\"); [Environment]::SetEnvironmentVariable(\"METABASE_USERNAME\", \"<user>\", \"Machine\"); [Environment]::SetEnvironmentVariable(\"METABASE_PASSWORD\", \"<pwd>\", \"Machine\"); [Environment]::SetEnvironmentVariable(\"ASINFO_CARD_VENDEDOR_USD\", \"116\", \"Machine\"); [Environment]::SetEnvironmentVariable(\"ASINFO_CARD_VENDEDOR_KG\", \"163\", \"Machine\"); [Environment]::SetEnvironmentVariable(\"ASINFO_CARD_CLIENTE_KG\", \"164\", \"Machine\"); [Environment]::SetEnvironmentVariable(\"COSTOS_OT_ADAPTER\", \"postgres\", \"Machine\")"]'
```

### 3. Reiniciar el Scheduled Task de Programa Core

Para que las nuevas env vars de Machine se carguen:

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["Stop-ScheduledTask -TaskName ProgramaCore; Start-Sleep 2; Start-ScheduledTask -TaskName ProgramaCore"]'
```

### 4. Smoke test

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["(Invoke-WebRequest -UseBasicParsing http://localhost:5050/tintura/ordenes).StatusCode; (Invoke-WebRequest -UseBasicParsing http://localhost:5050/asinfo/ventas-vendedor).StatusCode"]'
```

Ambos deberían dar `200`. Si `/tintura/ordenes` da 200 con `disponible=False` en la vista, el `FORMULAS_DATABASE_URL` no se cargó — chequear que el Scheduled Task se reinició.

## Rotación de credenciales

**Password de `programa_core_reader`** (Postgres):

```bash
# 1. Correr el setup script en EC2, que dropa+recrea el rol con una password nueva
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable(\"DATABASE_URL\",\"Machine\"); C:\\Python312\\python.exe C:\\programa-core\\scripts\\setup_formulas_reader.py"]'

# 2. Tomar la connection string que imprime el script
# 3. Set FORMULAS_DATABASE_URL machine env var + restart scheduled task (sección "Setup paso 2 y 3")
```

**Password de Metabase**: rotás el usuario `integracion@intela.com.ec` desde Admin → People en Metabase, luego actualizás `METABASE_PASSWORD` machine var, restart scheduled task.

**Llave de panico**: si sospechás que `FORMULAS_DATABASE_URL` se filtró, el blast radius está limitado por (a) el rol es SELECT-only en una lista cerrada de tablas, (b) la DB es privada — el atacante necesita network access al RDS. Aun así: rotar inmediato.

## Tests

`tests/test_costos_ot.py` ya cubre el patrón con FakeAdapter + mock de los otros dos. Cuando agregues módulos nuevos (`asinfo`, `tintura`), seguí el mismo shape:

- Mock de `metabase_client.fetch_card` con `unittest.mock.patch` — no hagas HTTP real en tests.
- Mock de `formulas_db.fetch_all` con `patch("modules._lib.formulas_db.fetch_all")` que devuelve fixtures inline.
- Un test de "adapter no disponible" → la vista igual renderiza 200 con un placeholder.

Los tests no necesitan Postgres ni Metabase corriendo. Si querés validar contra la DB real, eso va aparte en `tests/integration/` con marker `@pytest.mark.db` que el CI saltea por default.

## Lo que NO está en este bridge (por ahora)

- **Escritura de Programa Core hacia formulas_app**. No hay write-back de OT cerrada → factura, ni de cobro → estado de OT. Ese puente (bidireccional) está en el backlog como "Puente bidireccional formulas_app → Core" — requiere decisión sobre quién es la fuente de verdad de cada estado.
- **Sync periódico a tablas mirror en `scintela.*`**. Decidimos display en vivo. Si en el futuro un reporte pesado tarda mucho, antes de armar un job de sync probamos materialized view + refresh nocturno desde formulas_app.
- **Embed de dashboards Metabase con iframe firmado**. Útil cuando quieras mostrar el chart completo (no la data cruda). Patrón:
  ```python
  import jwt, time
  payload = {"resource": {"dashboard": <id>}, "params": {}, "exp": int(time.time()) + 600}
  token = jwt.encode(payload, METABASE_SECRET_KEY, algorithm="HS256")
  iframe_url = f"{METABASE_URL}/embed/dashboard/{token}#bordered=true&titled=false"
  ```
  Esto requiere habilitar embedding en Metabase Admin → Embedding y guardarse `MB_EMBEDDING_SECRET_KEY`. **No activado todavía** — cuando lo necesites, agregalo a este SKILL.md.

## Checklist cuando vas a agregar un nuevo bridge

1. ¿La data ya existe en una card de Metabase? → MetabaseAdapter / metabase_client.fetch_card, anotar el ID en env var con prefijo `ASINFO_CARD_*` o `FORMULAS_CARD_*`.
2. ¿Es de formulas_app y no hay card? → SQL directo via `formulas_db.fetch_all`. **Antes de escribir el SQL** abrí `formulas_app/database.py` (skill `intela-formulas-app`) para confirmar nombres de columna y tipos. Si la tabla no está expuesta, agregarla al `GRANT SELECT` y re-correr el setup.
3. ¿Es del ERP/Asinfo y no hay card? → Crear card en Metabase primero (que sea la fuente única de verdad de esa query), después referenciar desde Programa Core. No metas SQL Server directo.
4. ¿La superficie cambia con frecuencia? → Card de Metabase (escribís SQL, no código). ¿Es muy parametrizable? → Pool directo a formulas_db (Python te da más control).
5. Test con FakeAdapter / mock primero, después wire al adapter real.
6. La vista renderiza con `disponible=False` y devuelve 200 (no 500). Siempre.

Si después de todo eso tenés un caso que el SKILL.md no cubre, agregalo acá. La skill es viva.

## Schema real de formulas_app (verificado 2026-05-21)

Esto es lo que el `init_db()` de `formulas_app/database.py` crea — los nombres físicos son **estos**, no los que asumió la primera versión del PostgresAdapter (que referenciaba `o.pcod`, `o.codigo_cli`, `o.fecha_cierre` — ninguna existe).

| Tabla | Columnas relevantes |
|---|---|
| `productos` | `num` (PK), `num_visible`, `nombre`, `prov`, `us` (precio weighted-avg), `unidad`, `familia` |
| `formulas` | `cod` (PK), `color`, `categoria`, `grupo` |
| `formula_items` | `id`, `formula_cod`, `producto_num`, `cantidad`, `orden`, `etapa` |
| `ordenes` | `id`, `numero` (UNIQUE, user-facing), `fecha`, `codigo` → `formulas.cod`, `kil`, `jet`, `rel`, `lit`, `created_at`, `tela_cruda_kg`, `tela_terminada_kg`, `fecha_terminado`, `es_reproceso`, `observaciones`, `bano_numero` |
| `orden_piezas` | `id`, `orden_id`, `tipo`, `categoria`, `tela`, `cantidad` |
| `orden_lineas` | `id`, `orden_id`, `producto_num`, `cantidad_kg`, `precio_us`, `orden_seq`, `etapa`, `ajustes` (jsonb) |
| `inventario` | `id`, `producto_num`, `cantidad`, `fecha`, `nota`, `created_at` |
| `inventario_ajustes` | `id`, `producto_num`, `fecha`, `cantidad`, `motivo`, `created_at` |
| `compras` | `id`, `producto_num`, `fecha`, `proveedor`, `factura`, `cantidad`, `precio_us`, `nota`, `created_at` |
| `precio_historial` | append-only, no leer desde Programa Core |
| `tela_catalog` | `id`, `categoria`, `tela`, `orden` (catálogo editable de telas) |

**Formato de columnas `fecha` (TEXT) — atención al mix de formatos**:
- `ordenes.fecha` → `'DD/MM/YYYY'` (legacy DBF). Cualquier ORDER BY / MIN / MAX necesita `TO_DATE(fecha, 'DD/MM/YYYY')`.
- `ordenes.fecha_terminado` → `'YYYY-MM-DD'` (ISO, agregado en migración). Comparable lex.
- `inventario.fecha`, `inventario_ajustes.fecha`, `compras.fecha` → `'YYYY-MM-DD'` (ISO). `get_inventario_for_date()` en formulas_app usa `WHERE fecha <= %s` lex, lo que solo funciona en ISO.

**(El SKILL `intela-aws-deploy` dice que `inventario.fecha` es DD/MM/YYYY — está desactualizado en ese punto. La fuente correcta es siempre `formulas_app/database.py`.)**

## Cliente en formulas_app — no existe

Hallazgo crítico (2026-05-21): formulas_app **NO tiene clientes**.

- No hay tabla `clientes`.
- No hay columna `cliente_codigo` / `codigo_cli` / `customer` en `ordenes` ni en ninguna otra tabla.
- El `numero` de orden es secuencial (`get_next_order_number()`), no encodea cliente.
- El template `ordenes.html` no muestra cliente.

Esto es **por diseño**: formulas_app es una app de tintorería pura — qué fórmula, qué jet, qué kilos. El "para quién" vive en el ERP Asinfo, no acá. Programa Core nunca necesita pedirle a formulas_app costos por cliente porque formulas_app no tiene esa data.

Consecuencias prácticas:

- **`modules/costos_ot/adapters.py::PostgresAdapter` queda permanentemente deshabilitado** (`disponible()=False`, `costos_por_cliente()` retorna `[]` con log). Se conserva la clase para no romper el adapter pattern, pero no se implementa.
- **El bridge útil** entre Programa Core y formulas_app es `modules/tintura/` (este SKILL más abajo), que expone exactamente lo que formulas_app sí sabe: kg in/out por orden, desperdicio, stock de químicos.
- Si en el futuro alguien necesita "costos por cliente desde tintorería", el camino es agregar `cliente_codigo` a `ordenes` en formulas_app — decisión que **NO** se toma desde Programa Core.

## Módulo `modules/tintura/` — worked example

`service.py` expone tres funciones planas (no class, no adapter):

- `tinturado_resumen(limite=500, solo_terminadas=False, creacion_desde=None, creacion_hasta=None, terminado_desde=None, terminado_hasta=None)` → `list[TinturadoOrden]` con `numero, fecha, fecha_terminado, formula_cod, color, categoria, kilos_planeados, tela_cruda_kg, tela_terminada_kg, desperdicio_kg, jet, es_reproceso, observaciones`. `desperdicio_kg` se calcula solo si ambos `tela_cruda_kg` y `tela_terminada_kg` están cargados; si la orden todavía no terminó, queda `None`. Dos rangos independientes: `creacion_*` filtra cuándo entró la orden (sobre `ordenes.fecha`, DD/MM/YYYY); `terminado_*` filtra cuándo salió (sobre `ordenes.fecha_terminado`, ISO). Pasar `terminado_*` implica `solo_terminadas=True`.
- `desperdicio_periodo(desde, hasta, por="terminado")` → dict `{ordenes_count, kilos_crudo_total, kilos_terminado_total, desperdicio_kg_total, desperdicio_pct}`. El toggle `por`:
  - `"terminado"` (default): "de las órdenes que SALIERON terminadas en este período…". Cifra estable.
  - `"creacion"`: "de las órdenes que ENTRARON en este período…". Las que todavía no terminaron suman crudo pero no terminado, así que el desperdicio queda inflado mientras el período esté abierto.
- `stock_quimicos()` → `list[StockProducto]` con `num, num_visible, familia, nombre, unidad, precio_us, stock_kg, fecha_lectura, nota`. El `stock_kg` es la **última lectura manual** del operario (replica `get_inventario_for_date` de formulas_app). Productos sin lecturas llegan con `stock_kg=0.0, fecha_lectura=None`. **Versión baseline** — no suma compras ni ajustes posteriores. Si necesitás el stock real al día, usá `stock_quimicos_al_dia()`.
- `stock_quimicos_al_dia(fecha=hoy)` → `list[StockProductoAlDia]` con `lectura_kg, fecha_lectura, ajustes_kg, compras_kg, consumo_kg, stock_al_dia_kg` — replica la fórmula `final = última lectura + ajustes posteriores + compras posteriores − consumo en órdenes terminadas posteriores`. El `consumo_kg` es `SUM(orden_lineas.cantidad_kg)`; **no incluye los ajustes JSONB intra-línea** de formulas_app (aproximación). Si en algún momento se necesita la cifra exacta hay que parsear `orden_lineas.ajustes` (jsonb) — está documentado en el code.

Las dos funciones son fail-soft via `formulas_db.fetch_all` — si el pool no está configurado o la query rompe, devuelven `[]`/`None` con log a WARNING. Nunca levantan.

**Lo que NO hace** (a propósito):
- No expone vistas / templates / blueprints. Solo data. Cuando alguien necesite mostrar esto en pantalla, importa el service y arma la UI encima.
- No suma ajustes ni compras posteriores a la última lectura — el "stock teórico al día" (= última lectura + compras − consumido + ajustes) es una query distinta que se agrega cuando se pida.
- No filtra por cliente (formulas_app no lo sabe — ver sección anterior).

Patrón a seguir si agregás otra función a `modules/tintura/service.py`:

1. SQL contra `formulas_db.fetch_all(...)` con nombres de columna del schema verificado (sección "Schema real" arriba).
2. Dataclass `frozen=True` como retorno, con `to_dict()` que serializa fechas a ISO.
3. Helper interno `_row_to_X` que mapea el dict del DB al dataclass, tolerante a nulls (`_f`, `_fo`, `_parse_iso`, `_parse_ddmmyyyy`).
4. Test con `patch("modules.tintura.service.formulas_db.fetch_all", return_value=[...])`. Sin DB real.

## Estado actual de implementación (al 2026-05-21)

Implementado en Programa Core:

- ✅ `modules/_lib/metabase_client.py` + tests (12).
- ✅ `modules/_lib/formulas_db.py` + tests (14). `init_pool()` wireado en `app.py:create_app()`.
- ✅ `scripts/setup_formulas_reader.py` — listo para correr vía SSM en EC2.
- ✅ `modules/costos_ot/adapters.py` refactorizado:
  - `MetabaseAdapter` ahora usa el cliente compartido.
  - `PostgresAdapter` deshabilitado permanentemente (formulas_app no tiene clientes — ver sección "Cliente en formulas_app").
- ✅ `modules/tintura/service.py` + tests — `tinturado_resumen()`, `desperdicio_periodo()`, `stock_quimicos()`, `stock_quimicos_al_dia()`.
- ✅ `modules/asinfo/service.py` + tests — `fetch_card_from_env()` y wrappers `ventas_vendedor_usd/kg`, `ventas_cliente_kg` contra cards Metabase 116/163/164.
- ✅ `GET /healthz/integraciones` — endpoint que reporta estado de los dos bridges. Útil para smoke post-deploy.
- ✅ `docs/DEPLOY_INTEGRACIONES.md` — checklist paso a paso (CloudShell + SSM) para activar todo.

Pendiente:

- ❌ Rol `programa_core_reader` en RDS — no creado. Seguir `docs/DEPLOY_INTEGRACIONES.md` paso 2.
- ❌ Env vars Machine en EC2 — paso 3 del checklist.
- ❌ Vistas / templates que consuman `tintura/service.py` y `asinfo/service.py` cuando se decidan los dueños de cada superficie.

Próximos pasos:

1. (Vos, en CloudShell) Ejecutar `docs/DEPLOY_INTEGRACIONES.md` pasos 1-5. Al final el smoke test `/healthz/integraciones` debería devolver `reachable: true` en ambos bridges.
2. (Equipo) Decidir qué vistas necesitan los datos de `tintura/` — ¿panel del dueño? ¿reporte de QC? ¿link desde `/cartera`? Ese diseño define los templates a armar.
3. (Equipo) Wirear UI consumidora de `asinfo/` (cards 116/163/164 son los reportes canónicos del ERP).

Mantener esta lista actualizada en este SKILL.md a medida que las cosas se vayan tachando.

## Stock por LOTE de Asinfo — schema + surface (verificado 2026-06-09)

Réplica del reporte oficial **"Stock Valorado por Lote"** del ERP. A diferencia
de `stock_asinfo()` (consolida por producto sobre todas las bodegas), esto baja
al **lote individual** con sus atributos. **Solo cantidad (kg)** — los dólares de
Asinfo no son confiables (vienen del programa, no del ERP).

### Tablas (Asinfo = Metabase DB 2, SQL Server)

| Tabla | Para qué | Columnas clave |
|---|---|---|
| `saldo_producto_lote` | snapshot **diario** de saldo por producto·bodega·lote (~3,5M filas) | `id_saldo_producto_lote`, `fecha` (date), `saldo` (numeric), `id_bodega`, `id_lote`, `id_producto` |
| `saldo_producto` | snapshot diario por producto·bodega (sin lote) — lo usa `stock_asinfo()` | igual pero sin `id_lote` |
| `lote` | dimensión lote, atributos en slots **EAV** | `id_lote`, `codigo`, `activo`, `id_valor_atributo_1..10` (cada slot apunta a `valor_atributo`) |
| `valor_atributo` | valor de cada atributo | `id_valor_atributo`, `id_atributo`, `nombre` |
| `atributo` | catálogo de atributos | `id_atributo`, `nombre` |
| `producto` | master | `id_producto`, `codigo`, `descripcion`, `nombre`, `id_categoria_producto`, `id_unidad` |
| `categoria_producto` | tejido/categoría | `id_categoria_producto`, `nombre` |
| `unidad` | unidad de medida | `id_unidad`, `codigo` (KG), `nombre` |
| `bodega` | bodegas | `id_bodega`, `nombre` |

**Mapa `atributo` (id → nombre):** `1`=Acabado · `2`=Calidad (PRI/SEG) · `3`=Color ·
`51`=Estampado · `101`=Titulo Hilo · `103`=Proveedor · `151`=Fallas PT · `152`=Fallas TC.

**Bodegas (`id_bodega`):** `1`=Colorantes y Auxiliares · `51`=Hilo · `52`=Tela Cruda ·
`53`=Producto Terminado · `151`=Reproceso · `201`=Cuarentena.

### Patrón de query (clave: snapshot + UNPIVOT de atributos)

El "stock actual" = **último snapshot** por (producto, bodega, lote) con
`ROW_NUMBER() OVER (PARTITION BY id_producto,id_bodega,id_lote ORDER BY fecha DESC,
id_saldo_producto_lote DESC)` y `rn=1 AND saldo>0`. Igual que `stock_asinfo()`
pero agregando `id_lote` a la partición.

Los atributos del lote son EAV posicional: se resuelven con `UNPIVOT` de los 10
slots `id_valor_atributo_N` → join a `valor_atributo` → pivot con
`MAX(CASE WHEN va.id_atributo = N THEN va.nombre END)`. Ver
`modules/asinfo/service.py::stock_asinfo_lote()` para el SQL completo.

### Anclas de reconciliación (2026-06-09, contra reporte oficial)

| Bodega | Total kg lote-level | Reporte ERP |
|---|---|---|
| Hilo (51) | 1.790.694 | ✓ |
| Tela Cruda (52) | 255.795 | 255.660 (±0,06%, drift intradía — todos los lotes `activo=1`) |
| Prod. Terminado (53) | 347.390 | ✓ |

(El product-level `stock_asinfo()` ya estaba verificado al centavo: Bodega Hilo
1.767.920,41 kg. El lote-level da ~1,3% más porque incluye lotes que el reporte
no-lote filtra.)

### Surface en Programa Core

- `modules/asinfo/service.py::stock_asinfo_lote(id_bodega, q="", limit=50000)` —
  detalle de lotes de una bodega (bodega **requerida**: el universo son ~95k lotes,
  Hilo sola ~61k). El filtro `q` se empuja al SQL con `LIKE` (no bajar filas de más).
  Cache 10 min. Fail-soft. Cantidad únicamente.
- `modules/asinfo/service.py::stock_asinfo_lote_totales()` — totales por bodega
  (landing + ancla de reconciliación). GROUP BY barato, cache 10 min.
- `modules/stock_asinfo/views.py::lote()` → `GET /stock/asinfo-lote`
  (perm `stock.ver` — concedido a todos los roles). Sin `?bodega=` → landing con totales por bodega; con
  `?bodega=<id>` → detalle con filtros (categoría, título hilo, proveedor, calidad)
  + export CSV. Template `stock_asinfo/lote.html`.
- Tests: `tests/test_asinfo.py` (mock `metabase_client.fetch_dataset`).

**Gotcha:** la unidad NO está en una columna `unidad` de `producto` — se trae de la
tabla `unidad` (`COALESCE(NULLIF(u.codigo,''), u.nombre, 'KG')`). `producto.id_unidad`
es la FK.

## Importaciones Asinfo ↔ compras/anticipos del programa (Nota link, 2026-06-09)

La lista **"Importación"** del ERP cruza con las compras/anticipos del programa
por un código embebido en la **Nota**. Esto resuelve el problema de los dólares:
la cantidad/importación vive en Asinfo, pero el **importe confiable en USD** está
en `scintela.compra` del programa (los dólares de Asinfo NO son confiables).

### Origen en Asinfo (Metabase DB 2)

- `factura_proveedor_importacion` (extiende la factura con datos de importación) →
  `factura_proveedor` (la factura: `numero` = "IM-0000537", `descripcion` = **la Nota**,
  `fecha`, `fecha_recepcion`, `total` REFERENCIAL, `id_empresa`) → `empresa`
  (`nombre_comercial`/`nombre_fiscal`, `codigo`).
- **OJO:** `empresa.codigo` (código Asinfo) ≠ el código del programa. Para Ariescope
  coinciden (AC=AC) pero More Human es `EXT0059` en Asinfo y `MH` en el programa;
  Aartimpex es `AART` vs `AI`. El código que importa es el del PROGRAMA, que va en la Nota.

### El código en la Nota

Formato: `<2-3 letras><espacio><número>`, casi siempre entre paréntesis al final.
Las letras = `scintela.proveedor.codigo_prov`; el número = `scintela.compra.numero`.
Ejemplos reales y su desorden (todos parseados OK):
`( AC 36)`, `( MH  63)` (doble espacio), `( AC 22` (sin cerrar), `( AC 25)\n` (basura),
`( AI 15 )  ----1` (split de factura), `( MH 64-65 )` (rango → suma 64 y 65).

**Parser:** `concepto_parser.parse_nota_importacion(nota)` →
`{prov, numero, numero_hasta, codigo, raw}` o `{}`. Prefiere el código entre
paréntesis y toma la **última** coincidencia (evita el falso positivo de
"INV 2026030405 ..." que también parece letras+número).

### Surface en Programa Core

- `modules/asinfo/service.py::importaciones_asinfo(limite=400)` — trae las
  importaciones de Asinfo (vía `fetch_dataset(2, ...)`), cache 5 min, fail-soft.
- `modules/importaciones/service.py::importaciones_con_cruce()` — parsea la Nota
  de cada una y cruza contra `scintela.compra` (UNA query:
  `WHERE UPPER(codigo_prov)=ANY(%s) AND numero=ANY(%s)`). Soporta rangos (suma
  las N compras). Fail-soft: si la DB del programa cae, las importaciones se
  muestran igual sin cruce.
- `modules/importaciones/views.py::lista()` → `GET /importaciones` (perm
  `stock.ver` — concedido a todos los roles). Tabla con código (badge), total Asinfo (referencial, tenue) y
  el importe del programa enlazado a `compras.editar`. Filtros: q + estado
  (cruzadas / con código sin cruce / sin código). Export CSV. Registrado en
  `app.py` (sin url_prefix → `/importaciones`).
- Tests: `tests/test_importaciones.py` (parser + cruce + render; mocks de
  `asinfo_service.importaciones_asinfo` y `db.fetch_all`).

**Join key (confirmar con la dueña si algún caso no matchea):** `(codigo_prov, numero)`.
Para Ariescope #36 el match es directo; si la dueña usa otro criterio de numeración
para anticipos, ajustar solo `_buscar_compras()` en el service.

### Nav + permisos (2026-06-09)

Sección **Stock** en el sidebar (`templates/base.html`, `data-key="stock"`, después
de Bancos): Resumen (`stock.lista`, kg+$ del programa) · Por lote (`stock_asinfo.lote`)
· Por producto (`stock_asinfo.lista`) · Importaciones (`importaciones.lista`).
Químicos NO va acá — sigue en Tintorería (perm `tintura.ver`).

**Permiso `stock.ver`**: la dueña pidió que Stock lo vea TODO el mundo. `stock.ver`
ya existía como reservado; ahora está concedido a **todos** los roles en
`config/roles.py` (Accionista/Administrador por `*`, Bodega ya lo tenía, +8
agregados). Las 4 vistas (`stock.lista`, `stock_asinfo.lista`, `stock_asinfo.lote`,
`importaciones.lista`) y el guard del nav pasaron de `informes.ver` → `stock.ver`.
OJO: Resumen (`/stock`) muestra el stock VALORADO en $ del programa — ahora visible
para todos los roles. Si se quiere restringir solo eso, volver `stock.lista` a
`informes.ver` (1 línea).

### Tie-out + display (audit 2026-06-09)

**No sumar `v_saldo_producto_lote` (la vista del reporte).** Tiene **1.421 fechas**
(todos los snapshots diarios 2022→hoy) y además está EXPLOTADA (varias filas por
lote — una por slot de atributo). Sumarla cruda da ~15.000.000 kg en Hilo. El
reporte oficial aplica "Hasta: <fecha>" + último-por-lote encima. **La fuente
confiable sigue siendo la tabla raw `saldo_producto_lote` con ROW_NUMBER (último
snapshot por producto·bodega·lote)** — lo que ya hace `stock_asinfo_lote()`.

**Tie-out (mismo snapshot, 2026-06-09):** lote-level reconcilia con product-level
(`saldo_producto`, que está probado al centavo vs el Excel del ERP) al 99,97–99,98 %:
Hilo 1.784.592 vs 1.784.974 · Tela Cruda 256.603 vs 256.676 · PT 345.501 vs 345.560.
El residual (~0,03 %) son productos cuyo stock no está 100 % descompuesto en lotes
(ej. Hilo `22/1-65:35-PEI`: 783 kg en el saldo de producto, sin lote). Colorantes
(bodega 1) NO lleva lote (0 en lote, 55k en producto). `inventario_comprometido` ≈ 0,
el reporte usa `saldo` crudo.

**Display:** el nombre útil es `producto.nombre` (`descripcion` viene vacío); el
`codigo` es SKU corto/ID — va secundario (muted). Color (atributo 3) es real sólo en
Producto Terminado (NEGRO/BLANCO/MARINO…); en crudo/hilo viene 'TELA CRUDA' (= ruido,
se oculta cuando `color == tejido`). Tablas con `class="sortable"` (orden por columna).
