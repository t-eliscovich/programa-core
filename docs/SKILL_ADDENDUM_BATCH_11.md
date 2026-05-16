# Addendum para `skills/programa-core/SKILL.md` — batch 11

> El skill vive montado read-only adentro del sandbox, así que este batch queda
> como addendum en el repo. Cuando haya acceso al host, pegá esta sección al
> final del `SKILL.md` (después de batch 10), junto con los addendums 9 y 10
> que siguen pendientes de integración.

## Sesión 2026-04-17 — batch 11 (todo el backlog abierto: Fases 1, 2, 3, 4)

Sesión larga que cerró 8 items del backlog (todo lo que no requería acceso AWS
vivo ni .p12 de firma electrónica contratado). Decisión explícita de la
usuaria: "todo" + "fake data para formulas_app y live stock, ambos viven en
Metabase atm, cuando pasemos a Windows los conectamos".

### Tier 1 — Fase 2.1 puente formulas_app (con adapter intercambiable)

Módulo nuevo `modules/costos_ot/` expone costos por OT cerrada con tres
adaptadores implementados que se eligen vía env var `COSTOS_OT_ADAPTER`:

- **FakeAdapter** (default) — datos sintéticos realistas (JTX/TEX/MOD con OTs
  plausibles). Para dev y demo. `disponible()=True`.
- **MetabaseAdapter** — lee de una pregunta guardada en Metabase vía API.
  Env vars: `METABASE_URL / METABASE_USERNAME / METABASE_PASSWORD /
  METABASE_QUESTION_ID_COSTOS_OT`. Token de sesión refreshed lazy en 401.
- **PostgresAdapter** — vista cross-schema `scintela.vw_costos_ordenes`. Zero
  round-trips extra cuando formulas_app viva en el mismo RDS.

Contrato uniforme:
```python
class CostosOTAdapter(Protocol):
    def costos_por_cliente(self, codigo_cli: str) -> list[OTCosto]: ...
    def costos_por_factura(self, id_factura: int) -> list[OTCosto]: ...
    def disponible(self) -> bool: ...
```

`OTCosto` dataclass frozen con `n_orden, fecha_cierre, cliente_codigo,
descripcion, kg, costo_total, costo_kg, fuente`. El campo `fuente` se muestra
en la UI (pill) para que quede claro de dónde viene la data.

Integración: `modules/facturas/templates/facturas/detalle.html` embed un
fragment HTMX (`hx-get /costos-ot/cliente/<codigo_cli>/fragment hx-trigger=load`)
que muestra las OTs asociadas al cliente con totales y promedio $/kg. Resiliente:
si el adapter falla, render cae a empty-row con mensaje amigable — el detalle
de factura nunca rompe por un backend externo.

Vista full en `/costos-ot/cliente/<codigo_cli>` para cuando el gerente quiere
auditar todas las OTs cerradas de un cliente.

**Regla del módulo**: el resto del app NUNCA importa un adapter concreto —
siempre pasa por `modules.costos_ot.service`. Así el switch fake→metabase→
postgres es un cambio de env var, no de código. Los tests pueden inyectar
adapters custom via `service.reset_adapter(adapter)`.

**26 tests** — fake (9), metabase (5), postgres (3), factory (4), service (3),
rutas HTTP con cliente conocido/desconocido (2).

### Tier 2 — Fase 3.1 infra: IP allowlist middleware

`ip_allowlist.py` en el root. Middleware `before_request` que corre DESPUÉS
de `load_logged_in_user` y valida que la IP del cliente esté en la allowlist
del rol. Env vars per-rol:
```
ROLE_IP_ALLOWLIST_DUENO=190.152.1.0/24,190.152.2.100
ROLE_IP_ALLOWLIST_CONTABILIDAD=190.152.1.0/24
```

**Default-allow**: si un rol no tiene env var configurada, pasa — sin cambios
en el comportamiento. Agregar la allowlist es opt-in por rol.

Normalización del nombre de rol a ASCII upper (Dueño → ROLE_IP_ALLOWLIST_DUENO),
soporte CIDR + IPs exactas + múltiples entradas comma-separated, respeta
`X-Forwarded-For` si `TRUST_PROXY=1` (para cuando esté detrás de ELB/CloudFront).

Skip-paths: `/static/`, `/favicon`, `/healthz`, `/login`, `/logout` — nunca
bloquear login o assets, así el usuario locked out puede al menos ver "acceso
bloqueado" renderizado.

Cuando se bloquea: HTML de 403 con username + rol + IP detectada + ruta + ts
para que el usuario le mande eso al admin.

**21 tests** — env key normalization, CIDR parsing, allowlist_for_role con
y sin config, ip_permitida_para_rol con varios casos, middleware enforce en
Flask app real (bloquea / permite / skip).

### Tier 3 — Fase 4.2 ThreadPoolExecutor del dashboard (instrumentación + switch)

`modules/dashboard/views.py` ahora tiene:

- `_dashboard_mode()` — lee env `DASHBOARD_MODE`, acepta `parallel` (default),
  `sequential`, `seq`, `serial`.
- `_run_with_fallback()` — corre la task capturando excepciones, devuelve
  `(resultado, ms, error|None)`. Comparable entre los dos modos.
- `_parallel()` refactored — corre en paralelo o secuencial según env, loggea
  timings estructurados a `programa_core.dashboard.timings` (DEBUG) con el
  desglose por tarea y el overhead del pool.

Script nuevo `scripts/dashboard_profile.py` que corre N iteraciones en cada
modo contra la DB real y reporta p50/p95/min/max por modo + per-query. Devuelve
recomendación automática: "simplificar a secuencial" si gap <10ms, "mantener
paralelo" si gap >50ms, "decisión marginal" entre.

**19 tests** — modo default, aliases del env, fallback a parallel ante valores
raros, run_with_fallback happy/excepción, ambos modos devuelven mismos
resultados, secuencial corre en orden determinista, paralelo es más rápido con
I/O sleep (sanity check), tasks vacías no rompe.

**No toma decisión arquitectural**: en el sandbox sin DB real no se puede
correr el benchmark. Cuando se corra en prod contra la RDS, se puede decidir
con números duros si vale la pena simplificar a secuencial.

### Tier 4 — Fase 1.2 runbook de rotación de credencial

`docs/RUNBOOK_rotar_credencial.md` — procedimiento documentado paso-a-paso
para rotar `1n7el4Pyth0n` en RDS + AWS Secrets Manager. Bloqueado de
ejecución por falta de acceso AWS, pero el runbook está completo y es
procesable en <30min por alguien con las credenciales AWS.

Pasos: (0) inventariar dónde está la pwd vieja · (1) generar nueva pwd con
openssl · (2) modify-db-instance en RDS · (3) secretsmanager put-secret-value
o edición directa de .env en EC2 vía SSM · (4) rotar también usuario de
formulas_app si aplica · (5) verificar pwd vieja rechaza + nueva conecta +
/healthz 200 · (6) checklist final + rollback documentado.

### Tier 5 — Fase 3.2 Dockerfile + docker-compose + GitHub Actions CI

- `Dockerfile` multi-stage (python:3.11-slim): builder compila deps (libpq),
  runtime ligero con libpq5 + curl + waitress. Usuario `app` no-root. Expone
  5050. Por ahora sin `HEALTHCHECK` (el endpoint `/healthz` no existe en el
  app — TODO para un próximo batch chico).
- `docker-compose.yml` — servicio `db` (postgres:16-alpine) + `app` + `test`
  (profile). El `app` espera `db.healthy`. Volume `pgdata` persiste entre
  runs. Env vars de dev en plano (`app_local_only` / `dev-only-secret-key`).
- `.dockerignore` — excluye venvs, caches, INTELA.rar, el dump SQL, tests,
  scripts/_archive, secrets. Crítico porque el dump pesa 5.8MB y no debe
  terminar en la imagen.
- `.github/workflows/ci.yml` — dos jobs:
  1. `lint-and-test` — checkout, Python 3.11 con cache pip, instalar deps,
     `ruff check .`, `pytest -q`, smoke test de rutas, pytest -m db
     (continue-on-error hasta que todos los tests `@pytest.mark.db` estén
     saneados).
  2. `docker-build` — buildx con cache GHA, no pushea. Sanity build tras los
     tests pasar.
- `Makefile` — `make setup / migrate / seed / run / test / lint / fmt /
  docker-up / docker-down / docker-logs / docker-test / clean`.

### Tier 6 — Fase 3.3 integration tests con pytest-postgresql

- `tests/conftest_db.py` — fixtures `real_pg_dsn`, `real_db_conn`, `migrated_db`.
  Detectan DB vía env vars (CI service o local), la conexión se prueba
  activamente: si falla, skip (NO error) — los tests `@pytest.mark.db` se
  auto-skip-ean cuando no hay DB.
- `tests/conftest.py` — import opcional de los fixtures del db-conftest con
  `contextlib.suppress(ImportError)`. Si pytest-postgresql no está instalado,
  los fixtures no se registran y los tests `@pytest.mark.db` skip-ean.
- `tests/test_integration_migrations.py` — 6 tests: migraciones idempotentes,
  `scintela.factura` tiene columnas esperadas, `seguridad.rol` tiene seed,
  `scintela.ejecuciones_tareas` existe (0007), `scintela.factura_electronica`
  existe (0009), `scintela.bitacora_acciones.request_id` existe (0006).
- `tests/test_integration_flows.py` — 3 tests: reversar cheque D→R aplica
  STOP automático al cliente, Z→R no lo aplica (anulación administrativa),
  reversar cheque ya en R levanta ValueError.
- `requirements.txt` agrega `pytest-postgresql==6.1.1` + `psycopg[binary]==3.2.3`
  (coexiste con `psycopg2-binary` que sigue siendo el driver del app).

### Tier 7 — Fase 2.3 conciliación bancaria + cheque rechazado automático

Módulo nuevo `modules/conciliacion/` — cierra el gap "cheque rechazado como
estado de primera clase" que venía del batch 3 notes.

**`parser.py`**: tolerante a los quirks de bancos ecuatorianos. Detecta encoding
(UTF-8/CP1252), BOM, separador (`;` o `,`), fechas (DD/MM/YYYY, ISO, guiones),
montos con coma decimal es-EC o punto decimal ISO, negativos con paréntesis.
Reconoce headers de Pichincha vs Internacional. Líneas inválidas se ignoran
(no rompen el batch). Devuelve `list[BancoLinea]` uniforme.

**`matcher.py`**: compara líneas del estado de cuenta vs cheques en stat='D'
con fechad en el rango. Match requiere:
- Referencia matchea `no_cheque` (exacto, con leading zeros strippeados) O
  el número aparece en el concepto ("CHEQUE 1234 DEVUELTO").
- Importe matchea débito o crédito del banco (± 0.01 por rounding).

Cheques en stat='D' con `fechad` antigua (>3 días) sin match pasan a la
bandeja `sospechosos`. Un match contra débito del banco indica rebote real.

**UI**:
- `GET/POST /conciliacion` — upload del CSV → `resultado.html` con los
  sospechosos arriba (rojo) y los matches abajo (slate). Cada sospechoso
  tiene un botón "Marcar como rebotado" que dispara `POST /conciliacion/
  confirmar-rebote`.
- `POST /conciliacion/confirmar-rebote` — llama `cheques.queries.reversar()`
  con motivo "Rechazado por banco (conciliación)". El reversar ya existe
  desde batch 5 y pasa el cliente a STOP automáticamente cuando el stat
  previo era D/A.
- `GET /conciliacion/bandeja` — lista persistida en session (no en DB)
  para no agregar una tabla nueva que requeriría migración. La bandeja se
  regenera con cada nueva conciliación.

**Decisión explícita**: NO hay bulk-confirm. La contadora revisa uno por uno
y confirma. Queremos que alguien mire cada caso — a veces el banco está
atrasado, no es un rebote.

Sidebar: "Conciliación" bajo Operaciones, gateado por `bancos.conciliar`
(permiso que ya existía en Contabilidad).

**30 tests** — parser (14: fechas, montos, CSVs completos, encoding, BOM,
separador, líneas inválidas, vacío, header-only), matcher (7: monto exacto/
tolerancia/rechazo, crédito, referencia exacta/ceros/en-concepto), matchear
integración (8: happy path, sospecha, reciente no-sospecha, rebote flag
débito, importe distinto no matchea, vacío).

### Tier 8 — Fase 1.1.2/3/4 firma + envío SOAP SRI

Las 3 sub-fases del SRI que batch 10 dejó como stubs quedan implementadas:

**`firma.py`** — `firmar_xml(xml_str, p12_path, p12_password)` con signxml +
cryptography.pkcs12 + lxml. Canonicalización exc-c14n, SHA1, RSA-SHA1, el
<ds:Signature> embebe como hijo del root <factura> (XAdES-BES enveloped).

Sin .p12 levanta `FirmaNoConfiguradaError` (hereda de NotImplementedError
para que no se confunda con error de datos). Si el .p12 no parsea, password
mala, XML inválido, etc → `FirmaFalloError`.

`firmar_xml_de_env()` lee `SRI_P12_PATH` + `SRI_P12_PASSWORD`. `firma_configurada()`
útil para mostrar el estado en UI sin intentar firmar de verdad.

**Dependencias nuevas** (cuando se active): agregar a `requirements.txt`:
```
signxml==3.2.2
cryptography>=42.0
lxml>=5.0
```

**`envio.py`** — cliente SOAP con `requests` + envelopes a mano (no zeep).
Decisión: requests >> zeep porque (a) zeep parsea WSDL al import, +5-10s por
primer request, (b) los WSDL del SRI cambian rara vez, el envelope cabe en
~20 líneas, (c) manejar reintento + timeout es más transparente.

Reintento exponencial sólo en 5xx/Timeout/ConnectionError (4xx es error del
cliente — no reintentar). Env vars: `SRI_HTTP_TIMEOUT_SEG=30`,
`SRI_REINTENTOS=3`, `SRI_REINTENTO_BACKOFF_SEG=2` (crece 2, 4, 8).

Parseo de respuestas con regex — minimal y explícito. `enviar_a_recepcion()`
devuelve `{estado, mensajes, raw}`. `consultar_autorizacion()` devuelve
`{estado, numero_autorizacion, fecha_autorizacion, mensajes, raw}`. Fechas
del SRI parseadas con múltiples formatos (timezone-aware o naive).

**`queries.py`** (módulo SRI) suma 4 funciones nuevas:
- `guardar_firma()` — persiste XML firmado + transición a 'firmado'. Sólo
  desde 'borrador'/'rechazado'.
- `marcar_enviado()` — estado fugaz, intentos++, ultimo_intento_en=now().
- `actualizar_respuesta_recepcion(respuesta)` — RECIBIDA→'enviado',
  DEVUELTA→'rechazado' con mensaje legible extraído del primer <mensaje>.
- `actualizar_respuesta_autorizacion(respuesta)` — AUTORIZADO→'autorizado' +
  guarda numero_autorizacion + fecha_autorizacion; NO AUTORIZADO→'rechazado';
  EN PROCESO → no transiciona (deja para re-consultar).

**Views nuevas** en `modules/sri/views.py`:
- `POST /sri/factura_electronica/<id>/firmar` — arma flow firma → guardar.
- `POST /sri/factura_electronica/<id>/enviar` — flow recepción → sleep 3s →
  autorización en un solo request. Actualiza estado y flashes según qué
  devuelva el SRI. Si la autorización queda EN PROCESO, flash amigable
  pidiendo que se re-consulte más tarde.
- `POST /sri/factura_electronica/<id>/consultar` — re-consulta autorización.
  Úsese cuando `enviar()` quedó EN PROCESO.

**Todos gateados por `sri.emitir`** (ya existía en Contabilidad y Cobranzas
desde batch 10).

**Dos tests nuevos** reemplazan los stubs antiguos:
- `tests/test_sri_firma.py` — 8 tests: XML vacío levanta, p12 no existe
  levanta NoConfiguradaError, firmar_xml_de_env sin SRI_P12_PATH levanta,
  firma_configurada() correcto según env, password en bytes no rompe.
- `tests/test_sri_envio.py` — 16 tests: URLs por ambiente, validaciones de
  entrada, parseo de recepción (RECIBIDA/DEVUELTA), parseo de autorización
  (AUTORIZADO/EN PROCESO/DESCONOCIDO, fechas con y sin timezone), HTTP mock
  del happy path, 4xx no reintenta, 5xx reintenta hasta N, timeout reintenta.

Los 2 tests en `test_sri_xml.py::TestEnvioStubs` que expected `EnvioNoConfiguradoError`
en los stubs se re-escribieron a validaciones de input (envelope vacío, clave
de longitud incorrecta).

**Requirement nuevo**: `requests>=2.32.0` para el cliente SOAP. Documentado
también para el MetabaseAdapter del puente formulas_app.

### Métricas al cierre

- `ruff check .` — 9 errores pre-existentes (todos UP038 `isinstance(x, (A, B))`
  que son estilos, no bugs). No son de este batch. El diff de este batch es
  ruff-clean en cada módulo nuevo.
- `pytest -q` — **269 passed, 9 skipped** (los integration tests que requieren
  DB real — CI los correrá con el postgres service del workflow).
- `python tests/test_routes_smoke.py` — **50 GET routes walked, 0 failures**
  (subió de 48 en batch 10: +2 = `costos_ot.por_cliente`, `conciliacion.index`).

### Invariantes nuevos (consolidar al skill)

19. **Backend-agnostic adapters**. Cuando un feature depende de un backend
    externo que puede cambiar (Metabase hoy, Postgres mañana), implementar
    como adapter intercambiable con factory + fachada + fake. Nunca importar
    el adapter concreto desde fuera del módulo. Ver `modules/costos_ot/` como
    referencia.
20. **Resilient fragments**. Partials HTMX que leen de backends externos
    nunca deben poder romper la página host. Fallo del backend = render
    vacío con mensaje amigable, NO 500.
21. **Default-allow para middlewares de access control**. Agregar una política
    (IP allowlist, feature flag, etc) sin env var configurada = passthrough.
    Así la feature es backwards-compatible por rol y nadie queda locked out
    por accidente.
22. **requests para clientes SOAP**. No zeep. La magia del WSDL-parsing al
    import no paga sus costos en este proyecto — envelope a mano cabe en
    ~20 líneas y el WSDL del SRI cambia una vez por año.
23. **Reintento sólo en transient errors**. 5xx/Timeout/ConnectionError =
    reintentar con backoff exponencial. 4xx = failfast (el cliente tiene
    que arreglar el envelope, reintentar no ayuda).
24. **Los tests `@pytest.mark.db` skip-ean cuando no hay DB**. NO error.
    El fixture prueba activamente la conexión en vez de confiar sólo en env
    vars. Un DB_HOST con server caído = skip, no error.

### Archivos tocados este batch

```
# Nuevos módulos
modules/costos_ot/__init__.py                                    NEW
modules/costos_ot/adapters.py                                    NEW  (FakeAdapter, MetabaseAdapter, PostgresAdapter + factory)
modules/costos_ot/service.py                                     NEW  (fachada)
modules/costos_ot/views.py                                       NEW
modules/costos_ot/templates/costos_ot/lista.html                 NEW
modules/costos_ot/templates/costos_ot/fragment.html              NEW  (embed HTMX)
modules/conciliacion/__init__.py                                 NEW
modules/conciliacion/parser.py                                   NEW  (CSV Pichincha / Internacional / genérico)
modules/conciliacion/matcher.py                                  NEW  (match cheques vs estado cuenta)
modules/conciliacion/queries.py                                  NEW
modules/conciliacion/views.py                                    NEW  (upload + bandeja + confirmar-rebote)
modules/conciliacion/templates/conciliacion/{upload,resultado,bandeja}.html NEW

# Infraestructura
ip_allowlist.py                                                  NEW  (middleware)
Dockerfile                                                       NEW  (multi-stage py3.11)
.dockerignore                                                    NEW
docker-compose.yml                                               NEW  (db + app + test)
.github/workflows/ci.yml                                         NEW  (lint-and-test + docker-build)
Makefile                                                         NEW  (setup/migrate/run/test/docker-*)
docs/RUNBOOK_rotar_credencial.md                                 NEW
scripts/dashboard_profile.py                                     NEW  (benchmark parallel vs seq)

# Modificados
modules/dashboard/views.py                                       + _dashboard_mode, _run_with_fallback, _parallel instrumentado
app.py                                                           + register costos_ot_bp, conciliacion_bp + enforce_allowlist hook
auth.py                                                          UTC = timezone.utc (py3.10 compat)
modules/facturas/templates/facturas/detalle.html                 + fragment HTMX de costos OT
templates/base.html                                              + nav "Conciliación" (bancos.conciliar)
modules/sri/firma.py                                             rewrite stub → real impl (signxml + pkcs12)
modules/sri/envio.py                                             rewrite stub → real impl (requests + envelopes + reintentos)
modules/sri/queries.py                                           + guardar_firma, marcar_enviado, actualizar_respuesta_*
modules/sri/views.py                                             + /firmar, /enviar, /consultar
requirements.txt                                                 + requests, pytest-postgresql, psycopg[binary]

# Tests
tests/test_costos_ot.py                                          NEW  (26 tests)
tests/test_ip_allowlist.py                                       NEW  (21 tests)
tests/test_dashboard_mode.py                                     NEW  (19 tests)
tests/test_conciliacion.py                                       NEW  (30 tests)
tests/test_sri_firma.py                                          NEW  (8 tests)
tests/test_sri_envio.py                                          NEW  (16 tests)
tests/test_integration_migrations.py                             NEW  (6 tests, @pytest.mark.db)
tests/test_integration_flows.py                                  NEW  (3 tests, @pytest.mark.db)
tests/conftest_db.py                                             NEW  (fixtures live-DB)
tests/conftest.py                                                + import opcional de conftest_db
tests/test_sri_xml.py                                            2 tests re-escritos (stubs → validaciones)
tests/test_session_timeout.py                                    UTC = timezone.utc (py3.10 compat)
```

### Backlog post-batch-11

**Bloqueado por entorno** (no AWS / no .p12):

- Fase 1.1.2 firma electrónica: contratar .p12 con CA ecuatoriana
  (BanEcuador/Security Data/Uanataca). Cuando llegue, set `SRI_P12_PATH` y
  `SRI_P12_PASSWORD` en .env y agregar a requirements.txt: `signxml==3.2.2`,
  `cryptography>=42.0`, `lxml>=5.0`. La ruta `/sri/factura_electronica/<id>/firmar`
  ya está wireada — debería funcionar sin cambios de código.
- Fase 1.2 rotación de `1n7el4Pyth0n`: ejecutar `docs/RUNBOOK_rotar_credencial.md`
  con acceso AWS.
- Fase 3.1 infra-infra: configurar las env vars `ROLE_IP_ALLOWLIST_*` en el
  .env de producción una vez que se sepa la IP fija de la fábrica.

**Ejecutable cuando haya acceso a datos reales**:

- Correr `scripts/dashboard_profile.py` en prod para medir si vale la pena
  simplificar `_parallel()` a secuencial.
- Cambiar `COSTOS_OT_ADAPTER` de `fake` a `metabase` (paso intermedio) o
  `postgres` (paso final cuando formulas_app migre al RDS + se cree la vista
  `scintela.vw_costos_ordenes`).

**Pendientes arquitecturales menores**:

- Endpoint `/healthz` en el app (mencionado en el Dockerfile como "si existe,
  agregar HEALTHCHECK"). 5 minutos de trabajo, no se hizo este batch para
  scope.
- Notas de crédito/débito electrónicas (SRI). Cada tipo tiene su XSD propio —
  reutilizar `core.py` (la clave de acceso es uniforme) pero escribir un
  generador XML por tipo.
- ATS (Anexo Transaccional Simplificado). Reporte XML mensual al SRI de
  compras/ventas. Distinto a los comprobantes — no es un flujo de autorización
  sino exportación.
- Puente bidireccional formulas_app → Programa Core (trigger cuando cierra
  OT) — cuando exista el mapping factura→OT en formulas_app.

### Decisión para la próxima sesión

- **Corta**: endpoint `/healthz` + HEALTHCHECK en Dockerfile; correr
  `scripts/dashboard_profile.py` y tomar la decisión parallel-vs-seq.
- **Media**: Notas de crédito electrónicas SRI (cuando hagan falta por la
  operación real).
- **Larga**: Implementar firma + envío real contra certificación SRI cuando
  llegue el .p12. End-to-end test con una factura de prueba real.
