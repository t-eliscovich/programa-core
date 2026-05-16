# Addendum para `skills/programa-core/SKILL.md` — batch 12

> Mismo patrón que batches 9/10/11: pegar al final del SKILL.md cuando haya
> acceso al host.

## Sesión 2026-04-17 — batch 12 (las 3 chicas post-batch 11)

Sesión corta de cierre después del batch 11 grande. Tres ítems chicos que no
requieren AWS ni infra.

### T1 — Endpoint `/healthz` + HEALTHCHECK activado

`modules/healthz/` con dos niveles, separados según el pattern estándar
(k8s/ECS/docker):

- **GET /healthz** — liveness. No toca DB. Si falla, restartear container.
- **GET /healthz/ready** — readiness. Hace `SELECT 1` con timeout corto. Si la
  DB está caída, 503 (quitar del LB pero NO restart — la app está viva).

Separar liveness vs readiness evita el anti-pattern "DB transient → restart
cascade de containers". Si la DB tarda 30s en volver, queremos que los workers
esperen, no que se restarteen en loop.

`Dockerfile` ahora activa el `HEALTHCHECK` apuntando a `/healthz`:
```
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD curl -fsS http://localhost:5050/healthz || exit 1
```

`tests/test_routes_smoke.py` ajustado para TOLERAR 503 específicamente en
`healthz.readiness` (la DB stubbeada no satisface `SELECT 1 AS ok`, el 503
es intencional). Todas las otras rutas siguen con threshold `>=500 = fail`.

7 tests. Path `/healthz` ya estaba en `_SKIP_PREFIXES` del middleware
`ip_allowlist` y `bitacora` — el endpoint nunca se bloquea.

### T2 — Notas de crédito electrónicas SRI

Cierra el pendiente de batch 10 "cuando hagan falta por la operación real".

- **`modules/sri/xml_nota_credito.py`** — generador NC v1.1.0 con `<notaCredito>`
  como root (NO `<factura>`), `<infoNotaCredito>` con los 3 campos únicos de NC:
  `<codDocModificado>`, `<numDocModificado>`, `<fechaEmisionDocSustento>` — la
  referencia al comprobante que se modifica. `<motivo>` texto libre.

- **Diferencias con factura XML**:
  - codDoc = `'04'` (no `'01'`).
  - detalles usan `<codigoInterno>`, no `<codigoPrincipal>`.
  - `<valorModificacion>` reemplaza `<importeTotal>`.
  - NO tiene `<pagos>` — una NC no registra forma de pago.
  - `<rise>` vacío obligatorio (cuando no aplica) — el XSD lo marca required.

- **`modules/sri/queries.py::crear_borrador_nota_credito()`** — inserta un
  registro `scintela.factura_electronica` con `tipo_comprobante='04'`.
  Guarda metadata en `respuesta_sri` JSONB (`{doc_modificado, motivo,
  id_factura_origen}`) para auditoría. `mensaje_error` arranca con una nota
  legible tipo `"NC contra 001-001-123: devolución de mercadería"` — se
  reescribe si después hay un rechazo SRI real.

- **Rutas nuevas**:
  - `GET /sri/factura_electronica/<id>/nota-credito` — form con motivo +
    valor_modificacion (prellenado con el importe de la factura original).
  - `POST /sri/factura_electronica/<id>/nota-credito` — genera XML NC,
    secuencial y clave de acceso nuevos, persiste en borrador. **Sólo contra
    facturas en estado `autorizado`** — una factura en borrador/rechazado
    NO genera NC (se regenera la original).

- **Botón en `/sri/factura_electronica/<id>`** aparece cuando
  `fe.estado == 'autorizado'` AND `fe.tipo_comprobante == '01'` (no es ya otra
  NC) AND el user tiene `sri.emitir`.

- **Después de generar la NC**: las rutas `/firmar` y `/enviar` ya existentes
  del batch 11 FUNCIONAN sin cambios. El mismo pipeline de firma + envío SOAP
  cubre todos los tipos de comprobante del SRI — NC, notas de débito, guías
  de remisión — porque el XML ya está construido y sólo hay que firmarlo
  como XAdES-BES. Cuando llegue el .p12, la NC se firma idéntico a la factura.

**17 tests** — cálculo de totales (una/varias líneas/vacío), estructura XML
(root, codDoc='04', refs al doc modificado, motivo, fecha con barras,
codigoInterno, totalConImpuestos, clave embebida, valorModificacion),
validación estructural (OK, no parseable, root incorrecto, sin detalles, sin
refs).

### T3 — Validación docker-compose + runbook local

Docker no está en el sandbox (era esperado). Lo que se hizo:

- Validación del YAML con Python puro:
  - `docker-compose.yml` → services (db/app/test), volumes (pgdata), healthcheck.
  - `.github/workflows/ci.yml` → jobs lint-and-test + docker-build con los
    steps correctos.
- **`docs/RUNBOOK_docker_local.md`** — procedimiento completo para arrancar
  el app en la Mac de la user. 6 pasos + troubleshooting + diferencias con
  prod. Formato: `docker compose up -d → migrate → seed_roles → curl /healthz
  → login`.

### Métricas al cierre de batch 12

- `pytest -q` → **293 passed, 9 skipped** (batch 11 cerró con 269 → +24 NC/healthz).
- `python tests/test_routes_smoke.py` → **53 GET routes, 0 failures**.
- `ruff check` en los archivos nuevos: **clean** (los 8 errores globales son
  pre-existentes de batches 9/10 en `exports.py` / `filters.py` / `parsers.py`
  / `scripts/procesa_provisiones_mensual.py` / `tests/test_exports.py`).

### Archivos nuevos / modificados

```
# Nuevos
modules/healthz/__init__.py                   NEW
modules/healthz/views.py                      NEW (liveness + readiness)
modules/sri/xml_nota_credito.py               NEW (generator NC XSD v1.1.0)
modules/sri/templates/sri/nc_form.html        NEW
docs/RUNBOOK_docker_local.md                  NEW
tests/test_healthz.py                         NEW (7 tests)
tests/test_sri_nota_credito.py                NEW (17 tests)

# Modificados
Dockerfile                                    HEALTHCHECK activado
app.py                                        + register healthz_bp
modules/sri/queries.py                        + crear_borrador_nota_credito()
modules/sri/views.py                          + nc_form, nc_generar routes
modules/sri/templates/sri/detalle.html        + botón "Emitir nota de crédito"
tests/test_routes_smoke.py                    tolerar 503 en healthz.readiness
ip_allowlist.py                               comentario: /healthz/ready cubre
```

### Invariantes nuevos

25. **Liveness ≠ readiness**. Liveness dice "el proceso responde" — si falla,
    restart del container. Readiness dice "puede servir tráfico" — si falla,
    sacar del LB pero NO restart. Separar los dos evita el anti-pattern
    "DB transient → cascade restart".
26. **NC sólo contra facturas autorizadas**. Una factura en borrador/rechazado
    no genera NC — se regenera la original. Una factura anulada no puede
    tener NC (no hay nada que anular).
27. **Las rutas /firmar y /enviar del módulo SRI son polimórficas**. Funcionan
    sobre factura electrónica y NC sin cambios. El XML ya está armado; firma
    + envío son idénticos para todos los tipos de comprobante del SRI.
28. **Una factura puede tener N notas de crédito**. A diferencia del mapping
    1:1 factura↔comprobante electrónico, las NC son comprobantes independientes
    con su propia `clave_acceso` única. Cada NC es su propio `factura_electronica`
    row con `tipo_comprobante='04'`.

### Backlog residual post-batch-12

**Bloqueado por entorno** (sin cambios desde batch 11):
- Firma electrónica real (.p12 contratado + env vars).
- Rotación de `1n7el4Pyth0n` (acceso AWS).
- IP allowlist configurada con la IP real de la fábrica.

**Ejecutable cuando haya datos reales**:
- Benchmark `scripts/dashboard_profile.py` contra RDS.
- `COSTOS_OT_ADAPTER=metabase|postgres`.

**Nuevos items chicos abiertos en esta sesión**:
- **Notas de débito** (SRI tipo '06'). Mismo pattern que NC — copiar
  `xml_nota_credito.py` a `xml_nota_debito.py`, cambiar codDoc a '06', ajustar
  el elemento raíz a `<notaDebito>`. ~30 min de trabajo cuando haga falta.
- **Guías de remisión** (SRI tipo '06' también pero XSD distinto). Para cuando
  la bodega empiece a despachar.
- **ATS (Anexo Transaccional Simplificado)** mensual — exportación al SRI de
  compras+ventas. No es un comprobante, es un reporte XML distinto.

### Decisión para la próxima sesión

- **Cuando haya .p12**: implementar firma real end-to-end + test contra
  ambiente de certificación del SRI con una factura de prueba. Es el salto
  de "scaffold listo" a "pipeline fiscal operativo".
- **Mientras tanto**: notas de débito + guías de remisión cuando la operación
  lo pida. Cada uno ~30 min reusando el pattern de NC.
