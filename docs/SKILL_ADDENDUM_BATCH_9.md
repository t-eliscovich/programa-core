# Addendum para `skills/programa-core/SKILL.md` — batch 9

> El skill vive montado read-only adentro del sandbox, así que este batch queda
> como addendum en el repo. Cuando tengas acceso al host, pegá esta sección al
> final del `SKILL.md` (después de "Decisión de dónde seguir").

## Session notes — 2026-04-17 (batch 9: T1 bugs post-deploy + T2 polish + T3 live stock + T4 seguridad operativa)

Sesión larga, 4 tiers de trabajo: correcciones post-deploy (T1), polish XS (T2, cierra Fase 4.1 y 4.3), live stock (T3, cierra Fase 2.2), seguridad operativa sin infra (T4, cierra las partes no-AWS de Fase 3.1). Explícitamente fuera de scope: AWS / deploy — la usuaria pidió "todavia no lo incluyas AWS".

### T1 — bugs descubiertos durante el deploy

**T1.1 — `tx.exec(...)` no existe: rewrite del runner mensual**

`scripts/procesa_provisiones_mensual.py` llamaba `tx.exec(sql_call, params)` y `tx.query_one(sql, params).get(...)`. **`db.tx()` yield-ea un psycopg2 `connection`, no un wrapper.** El método correcto es `with conn.cursor(cursor_factory=RealDictCursor) as cur: cur.execute(sql, params)`. El script rompía siempre al primer `tx.exec` — AttributeError.

Fix: reescribir `_reset_slot()` y `_ejecutar_tarea()` al contrato real:

```python
from psycopg2.extras import RealDictCursor

def _reset_slot(tarea: str, periodo: str, host: str) -> int:
    with db.tx() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("DELETE FROM scintela.ejecuciones_tareas WHERE tarea=%s AND periodo=%s",
                        (tarea, periodo))
            cur.execute("""
                INSERT INTO scintela.ejecuciones_tareas (tarea, periodo, host)
                VALUES (%s, %s, %s)
                RETURNING id_ejecucion
            """, (tarea, periodo, host))
            row = cur.fetchone()
            return int(row["id_ejecucion"])

def _ejecutar_tarea(sql_call: str, params: tuple) -> None:
    with db.tx() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_call, params)
```

Ojo: `_reset_slot` usa `RealDictCursor` porque después lee `row["id_ejecucion"]`. `_ejecutar_tarea` usa cursor default porque no lee nada. Si hacés `row["k"]` sobre un default cursor vas a tener un TypeError que parece no tener nada que ver. Regla: si la función lee del cursor, pasá `cursor_factory=RealDictCursor` explícitamente.

**T1.2 — Tests del runner al contrato real**

`tests/test_procesa_provisiones_mensual.py` tenía un `_TxSentinel` que simulaba el wrapper inexistente. Reescrito como `_FakeConn` + `_FakeCursor` que reflejan psycopg2 de verdad (con `__enter__`/`__exit__` porque `reset_slot` usa el cursor como context manager). 10 tests verdes, +2 regression explícitos:

- `test_ejecutar_tarea_usa_cursor_no_tx_exec` — si alguien reintroduce `tx.exec(...)`, AttributeError.
- `test_reset_slot_fetchone_devuelve_dict` — si alguien saca `cursor_factory=RealDictCursor`, `row["id_ejecucion"]` rompe.

**T1.3 — Migración 0001 que borraba el admin en re-run**

`migrations/0001_seguridad_fks.sql` hacía `TRUNCATE seguridad.rol RESTART IDENTITY CASCADE` *incondicional* cada vez que corría, y el cascade eliminaba todos los usuarios. Si corrías migrate.py dos veces, el admin recién creado desaparecía. Bug real del deploy del 2026-04-16.

Fix: guard por `dropped_count`. Sólo hacer TRUNCATE si realmente había FKs legacy apuntando a otra tabla `rol`. Todo el DO-block comparte la misma variable `dropped_count` para que el TRUNCATE y el CREATE no puedan estar desincronizados:

```sql
DO $$
DECLARE
    rol_oid oid;
    dropped_count int := 0;
    fk_rec record;
BEGIN
    SELECT c.oid INTO rol_oid FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='seguridad' AND c.relname='rol';

    FOR fk_rec IN
        SELECT conname, conrelid::regclass AS rel
          FROM pg_constraint
         WHERE conrelid IN ('seguridad.usuario'::regclass, 'seguridad.permiso'::regclass)
           AND contype='f' AND confrelid <> rol_oid
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', fk_rec.rel, fk_rec.conname);
        dropped_count := dropped_count + 1;
    END LOOP;

    IF dropped_count > 0 THEN
        TRUNCATE seguridad.rol RESTART IDENTITY CASCADE;
        RAISE NOTICE 'Legacy FKs dropped (%). seguridad.rol truncated.', dropped_count;
    END IF;
    -- idempotent ADD CONSTRAINT IF NOT EXISTS via lookups
END $$;
```

Invariante nuevo: **ninguna migración idempotente puede TRUNCATE de forma incondicional.** Si una migración tiene un paso destructivo, tiene que estar guardado por una verificación que demuestre que está en el estado "pre-fix". `dropped_count > 0` es esa prueba acá.

### T2 — polish XS (cierra Fase 4.1 + 4.3 del backlog)

**T2.1 — Fase 4.1: per-column formatter en `exports.csv_response()`**

Firma extendida para aceptar tanto 2-tuple como 3-tuple:

```python
Formatter = Callable[[object], str]
ColumnSpec = Union[tuple[str, str], tuple[str, str, Formatter]]
```

Reglas:

1. **Validación del shape ANTES de escribir al buffer.** Si hay shape inválido, levanta `ValueError` clean. Si validás después, un IndexError en medio del CSV te deja con un archivo corrupto con sólo la cabecera escrita — confuso para debug.
2. **`_apply_fmt()` es defensivo al failure path.** Si el formatter custom tira excepción, cae al default `_fmt()`. Un CSV imperfecto es mejor que un CSV roto a mitad de fila — Excel ES no es elegante mostrando el error.
3. **`None` del formatter se serializa como `""`.** No como el string `"None"`.

Uso típico:

```python
return csv_response(rows, [
    ("codigo_cli", "Código"),
    ("kg", "Kilos", lambda v: f"{v:.0f}"),             # 0 decimales para kg
    ("porcentaje", "% Margen", lambda v: f"{v:.1%}"),  # 1 decimal para %
    ("saldo", "Saldo"),                                # default: 2 decimales
], "aging.csv")
```

6 tests. Dos trampas concretas: (a) `""` en una fila de una sola columna lo quote-ea el csv writer como `'""'` — test rewriteado para usar 2 columnas y verificar contenido semántico, no equality de línea. (b) La validación tenía que moverse arriba del write.

**T2.2 — Fase 4.3: README post-clone bootstrap**

`README.md` reescrito end-to-end. Elementos clave:

- Uso de `.venv` (no `venv` ni `env`). Consistencia con `intela-aws-deploy`.
- Pasos post-clone en orden: clone → venv → requirements → `.env` → `scripts/migrate.py` → `scripts/seed_roles.py` → launcher.
- 10 roles documentados con tabla `modulo.accion` por rol.
- Sección "comandos útiles" (ruff/pytest/migrate).
- Sección "cron mensual" con el runner y link a `intela-aws-deploy`.
- Sección "contribuir" con invariantes resumidos.

### T3 — Fase 2.2: live stock en kg

`scintela.historia` es un snapshot mensual — el stock en kg que ve el gerente queda stale hasta 30 días. Fix: calcular en vuelo sumando compras y restando ventas desde el último snapshot.

`modules/informes/queries.py::stock_kg_live(hoy=None)`:

```python
def stock_kg_live(hoy: date | None = None) -> dict:
    hoy = hoy or date.today()
    h = db.fetch_one("""
        SELECT anio, mes, ustock AS snapshot_kg
        FROM scintela.historia
        ORDER BY anio DESC, mes DESC LIMIT 1
    """)
    if not h:
        return {"snapshot_fecha": None, "snapshot_kg": 0.0, "kg_comprados": 0.0,
                "kg_vendidos": 0.0, "live_kg": 0.0, "dias_desde_snapshot": 0}

    desde = date(h["anio"], h["mes"], 1) + relativedelta(months=1)
    kc = db.fetch_one(
        "SELECT COALESCE(SUM(kg), 0) AS kg FROM scintela.compra WHERE fecha >= %s AND fecha <= %s",
        (desde, hoy),
    )
    kv = db.fetch_one(
        """SELECT COALESCE(SUM(kg), 0) AS kg FROM scintela.factura
           WHERE fecha >= %s AND fecha <= %s AND (stat IS NULL OR stat <> 'Y')""",
        (desde, hoy),
    )
    snapshot_kg = float(h["snapshot_kg"] or 0)
    return {
        "snapshot_fecha": desde - timedelta(days=1),
        "snapshot_kg": snapshot_kg,
        "kg_comprados": float(kc["kg"] or 0),
        "kg_vendidos": float(kv["kg"] or 0),
        "live_kg": snapshot_kg + float(kc["kg"] or 0) - float(kv["kg"] or 0),
        "dias_desde_snapshot": (hoy - (desde - timedelta(days=1))).days,
    }
```

Keys nuevos agregados al dict `kg` de `informe_balance()`: `stock_kg_live`, `stock_kg_diff`, `stock_kg_live_desde`, `stock_kg_dias`.

Template `balance.html` muestra un indicador pequeño debajo del stock del snapshot SÓLO si `dias_desde_snapshot >= 2 AND abs(diff) >= 1 kg`. Esos umbrales evitan ruido cuando estás a un día del cierre o la diferencia es menor que el error de pesaje.

5 tests: sin snapshot, fórmula correcta, rango inclusivo, anuladas excluidas via WHERE en SQL, `dias_desde_snapshot` honra el param `hoy`.

### T4 — Fase 3.1 (partes sin infra): seguridad operativa

**T4.1 — Session timeout por rol (sliding window)**

`auth.py` ahora tiene timeout por rol. Reset en cada request real (no en keepalive). Si expiró, session.clear() + flash + `g.user=None`:

```python
SESSION_TIMEOUT_BY_ROLE: dict[str, timedelta] = {
    "Dueño":         timedelta(hours=8),
    "Administrador": timedelta(hours=8),
    "Gerente":       timedelta(hours=8),
    "Contabilidad":  timedelta(hours=4),
    "Compras":       timedelta(hours=4),
    "Cobranzas":     timedelta(hours=4),
    "Ventas":        timedelta(hours=4),
    "Bodega":        timedelta(hours=12),
    "QC":            timedelta(hours=12),
    "Lectura":       timedelta(hours=24),
}
SESSION_TIMEOUT_DEFAULT = timedelta(hours=4)  # rol nuevo sin entrada → seguro por omisión
```

Keepalive paths (`/static/`, `/healthz`, `/favicon`) **no reinician** el timer. Un monitor haciendo GET /healthz cada 30s no mantiene viva una sesión olvidada.

Parser de `last_activity` tolerante: timestamps naive de sesiones pre-upgrade se asumen UTC, ValueError/TypeError se catchean.

8 tests: mapping, default, parse tolerance, expired clears, active updates, healthz no resetea, session sin last_activity no expira inmediatamente.

**T4.2 — 2FA opt-in (pyotp + QR)**

Piezas:

- `migrations/0008_2fa_y_password_policy.sql` — 4 columnas en `seguridad.usuario`: `totp_secret varchar(64)`, `totp_confirmado_en timestamp`, `password_cambio_en timestamp`, `password_debe_cambiar boolean DEFAULT FALSE`. Idempotente (ADD COLUMN IF NOT EXISTS). Backfill: `password_cambio_en = fecha_crea` (NULL los forzaría a todos a cambiar).

- `modules/two_fa/core.py` — helpers puros testeables sin Flask/DB: `generate_secret()`, `provisioning_uri(username, secret)`, `verify(secret, code, valid_window=1)`. `verify` rechaza non-6-digit antes de tocar pyotp, limpia espacios ("123 456"), catchea excepciones de secrets corruptos.

- `modules/two_fa/views.py` — blueprint `two_fa_bp` con url_prefix="/2fa":
  - `GET/POST /2fa/setup` (`@requiere_login`). GET genera secret en setup-mode, renderiza QR inline como data-URL base64. POST valida primer código y setea `totp_confirmado_en=now()`.
  - `GET/POST /2fa/verify` (rate-limited 5/min). Lee `pending_user_id` de la session. Si verify ok → session.clear() + user_id real + last_activity.
  - `GET/POST /2fa/disable` (`@requiere_login` + password en POST).

- `auth.login()` detecta `totp_confirmado_en`. Si está presente → `session["pending_user_id"]` + redirect a `/2fa/verify`. La sesión completa se setea recién en el verify.

- Templates en `modules/two_fa/templates/two_fa/`: setup/verify/disable. QR inline (NO endpoint separado — expondría secret en server logs y browser cache). Input de código con `font-mono tracking-[0.3em]` y `autocomplete="one-time-code"` para que los iPhones ofrezcan el código de la notificación.

16 tests cubren generate/provisioning/verify con drift ±30s, espacios internos, entradas malformadas, secret inválido, valid_window=0.

**T4.3 — Password policy + must-change-on-first-login**

Endpoint `GET/POST /password/cambiar` en `auth.py` con `@limiter.limit("10 per hour", methods=["POST"])`. Validación:

```python
PASSWORD_MIN_LEN = 10

def _valida_password_nueva(password: str) -> str | None:
    if len(password) < PASSWORD_MIN_LEN:
        return f"La contraseña debe tener al menos {PASSWORD_MIN_LEN} caracteres."
    if not any(c.isalpha() for c in password):
        return "La contraseña debe incluir al menos una letra."
    if not any(c.isdigit() for c in password):
        return "La contraseña debe incluir al menos un número."
    return None
```

Filosofía NIST SP 800-63B — largo + no-reusar importa más que complexity de caracteres. Deliberadamente NO imponemos mayúsculas/minúsculas/símbolos obligatorios.

Validaciones del view: `bcrypt.checkpw(actual, hash)`, `nueva == confirm`, `nueva != actual`. En éxito: actualiza hash + `password_cambio_en=now()` + `password_debe_cambiar=FALSE` + redirect al `next`.

Login redirige a `/password/cambiar?next=` cuando `password_debe_cambiar=TRUE`. Template distingue "forzado" (sin botón volver) vs "voluntario" (con botón volver) por el flag `forzado`.

### Requisitos nuevos

`requirements.txt` gana `pyotp==2.9.0` y `qrcode[pil]==7.4.2`.

### Verificación al cierre

```
pytest -q → 88 passed in 0.71s
```

De 51 (post-batch-8) a 88: +8 session_timeout, +16 two_fa_core, +6 exports, +5 stock_kg_live, +2 regression procesa_provisiones.

### Todavía pendiente de deploy (handoff)

Antes del próximo ship a RDS (scope explícito fuera de esta sesión):

- `migrations/0008_2fa_y_password_policy.sql` — CRÍTICA. El query de login ahora lee `totp_confirmado_en` y `password_debe_cambiar`. Sin esta migración el login rompe con "column does not exist".
- `migrations/0006_bitacora_request_id.sql` y `0007_ejecuciones_tareas.sql` — ya estaban pendientes de batches 6 y 8.

### Invariantes nuevos

13. **`db.tx()` yield-ea un psycopg2 `connection` crudo.** Uso: `with db.tx() as conn: with conn.cursor(cursor_factory=RealDictCursor) as cur: ...`. Si leés por key, `RealDictCursor` explícito; si sólo escribís, cursor default.
14. **Migración destructiva (TRUNCATE/DROP) tiene que estar guardada por condición que demuestre el estado pre-fix.** ADD COLUMN / IF NOT EXISTS son idempotentes por default; los destructivos requieren guard explícito.
15. **`last_activity` se reinicia en paths reales, no en keepalive** (`/static/`, `/healthz`, `/favicon`).
16. **2FA es opt-in.** Usuarios con `totp_secret IS NOT NULL` y `totp_confirmado_en IS NULL` están en "setup-mode" — el login normal los trata como no-2FA. Abandonar el setup a medias no los locka.
17. **QR de 2FA se regenera en cada GET de /2fa/setup, inline como data-URL base64.** Nunca servir como endpoint separado — expondría el secret en server logs y browser cache.
18. **Password policy es largo + variedad mínima, no complexity.** NIST SP 800-63B: 10 chars + al menos una letra + al menos un número. Sin requisitos de mayúsculas/símbolos.

### Files touched this batch

```
scripts/procesa_provisiones_mensual.py            rewrite tx.exec → cursor
tests/test_procesa_provisiones_mensual.py         _FakeConn/_FakeCursor + 2 regression
migrations/0001_seguridad_fks.sql                 guard TRUNCATE por dropped_count
exports.py                                        2/3-tuple ColumnSpec + _apply_fmt
tests/test_exports.py                             NEW — 6 tests
README.md                                         rewrite (bootstrap, roles, cron, contribuir)
modules/informes/queries.py                       + stock_kg_live() + integración en informe_balance()
modules/informes/templates/informes/balance.html  + indicador "hoy: X (+Y)"
tests/test_stock_kg_live.py                       NEW — 5 tests
auth.py                                           + SESSION_TIMEOUT_BY_ROLE + enforce
                                                  + cambiar_password view
                                                  + login redirige a /2fa/verify y /password/cambiar
tests/test_session_timeout.py                     NEW — 8 tests
migrations/0008_2fa_y_password_policy.sql         NEW — 4 columnas
requirements.txt                                  + pyotp==2.9.0, qrcode[pil]==7.4.2
modules/two_fa/__init__.py                        NEW (empty)
modules/two_fa/core.py                            NEW — helpers puros
modules/two_fa/views.py                           NEW — setup/verify/disable
modules/two_fa/templates/two_fa/*.html            NEW — 3 templates
modules/auth/templates/cambiar_password.html      NEW
app.py                                            + register two_fa_bp
tests/test_two_fa_core.py                         NEW — 16 tests
```

### Backlog al cierre de batch 9

Cerrados esta sesión: **Fase 4.1** (CSV per-column formatter), **Fase 4.3** (README bootstrap), **Fase 2.2** (live stock kg), **Fase 3.1 no-infra** (session timeout, 2FA opt-in, password policy).

Queda abierto:

- **Fase 1.1** — SRI facturación electrónica (bloqueante, proyecto propio).
- **Fase 1.2** — Rotar `1n7el4Pyth0n` (bloqueado por "no AWS").
- **Fase 2.1** — Puente formulas_app ↔ Programa Core (decisión arquitectural).
- **Fase 2.3** — Cierre automático de cheque rechazado por conciliación bancaria.
- **Fase 3.1 (infra)** — IP allowlist + rotación de credencial (bloqueado por "no AWS").
- **Fase 3.2** — Dockerfile + compose + GitHub Actions CI + Playwright E2E.
- **Fase 3.3** — Integration tests `@pytest.mark.db`.
- **Fase 4.2** — Medir / simplificar `ThreadPoolExecutor` del dashboard.

### Decisión para la próxima sesión

- **Corta**: Fase 3.2 Dockerfile (sin Docker, cada máquina nueva es un día de config).
- **Media**: Fase 2.1 puente formulas_app (decidir view cross-schema vs REST).
- **Larga**: Fase 1.1 SRI (proyecto propio — requiere contratar firma electrónica).
