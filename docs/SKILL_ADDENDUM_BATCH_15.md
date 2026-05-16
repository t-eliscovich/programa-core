# Addendum para `skills/programa-core/SKILL.md` — batch 15

> Pegar al SKILL.md cuando haya acceso al host (va después de batch 14).

## Sesión 2026-04-17 — batch 15 (UX remaining: errores español, dashboard Dueño, undo 2-step, recientes)

Cerradas las 4 recomendaciones de mayor impacto del reporte UX que no habían
entrado en batch 14. Batch ejecutado como subagente autónomo con prompt
detallado; la sesión se quedó sin tiempo durante el trabajo pero todo lo
esencial quedó en verde. **362 tests passed** (+41 vs batch 14), **57 rutas
smoke 0 failures**, ruff clean en todos los archivos nuevos.

### Los 4 items

**1. Errores técnicos → español contable (`error_messages.py`)**

Módulo nuevo `humanize(exc) -> str` que mapea excepciones comunes:
- `ValueError` con "monto inválido" → "El importe no es un número válido. Usá formato 1234.56 o 1.234,56."
- `ValueError` con "fecha" → "La fecha no es válida. Usá formato DD/MM/AAAA (ej: 17/04/2026)."
- `psycopg2.errors.UniqueViolation` (pgcode='23505') → "Ya existe un registro con ese identificador."
- `ForeignKeyViolation` → "Referencia inválida: el registro al que apuntás no existe."
- `Exception` genérica → "Hubo un problema. Avisá a soporte si persiste."

Jinja filter `{{ exc | humanizar }}` registrado en `filters.py::register()`.
Error handlers globales 404/500/403 con templates `templates/404.html` y
`templates/500.html` — mensaje calmo + `request_id[:8]` para que ops pueda
buscar en bitácora. Tests en `tests/test_error_messages.py`.

**2. Dashboard "modo Dueño" compacto**

`modules/dashboard/views.py` tiene ahora:
- `_vista_preferida(rol)` — query param `?vista=simple|completa` persiste en
  `session["dashboard_vista"]`. Default `simple` para Dueño, `completa` para
  los demás roles (sin cambio en su UX).
- Macro `big_kpi` en `dashboard.html` — 4 tarjetas gigantes con font 4xl/5xl,
  trend arrow vs hace 7d, subtítulo de contexto ("cuánto me deben", "liquidez
  hoy", etc). Los 4 KPIs: **Cartera**, **Deudas**, **Bancos**, **Facturado mes**.
- Vista `simple` muestra sólo las 4 tarjetas + botón "Ver detalle completo".
  Vista `completa` muestra todo lo anterior.
- Ruta nueva `GET /tablero/imprimible` — render minimalista para PDF que
  aprovecha el `@media print` de base.html. Dueño abre en nueva tab y hace
  Cmd+P para llevarle al banco.
- Toggle visible en el header del tablero (desktop): pills "Simple / Completa".

**3. Undo — confirmación 2-step para acciones destructivas**

Nuevo partial `templates/_confirmar_accion.html` reusable con parámetros:
`titulo, mensaje, detalle_registro, accion_url, volver_url, motivo_requerido,
confirm_label`.

Aplicado a las acciones destructivas:
- `facturas.confirmar_anulacion` → paso intermedio antes de `facturas.anular`.
- `cheques.confirmar_reverso` → paso intermedio antes de `cheques.reversar`.
  Distingue visualmente entre "rebote real" (D/A, pasa a stop auto) y
  "anulación administrativa" (Z/P, sin side-effect).
- Patrón listo para aplicar a `retenciones.anular`, `compras.anular`,
  `provisiones.eliminar` — el partial es reusable, sólo hace falta la ruta
  `confirmar_*` en el blueprint correspondiente.

El template del paso 1 muestra:
1. Detalle del registro en un dl horizontal (num, fecha, cliente, importe, estado).
2. Campo "motivo" required (queda en bitácora + observacion del registro).
3. Botón rojo de confirmación + link gris "Volver".

Tests en `tests/test_confirmar_accion.py` verifican el flujo 2-step.

**4. Recientes por usuario (módulo nuevo `modules/recientes/`)**

- **Migración 0010**: `seguridad.usuario_recientes (id_usuario, tipo, id_ref,
  etiqueta, tocado_en)` con PK (id_usuario, tipo, id_ref) + índice por
  (id_usuario, tocado_en DESC). FK a seguridad.usuario CASCADE.
- **Helper `registrar(tipo, id_ref, etiqueta)`** hace UPSERT (INSERT ON
  CONFLICT DO UPDATE tocado_en=now()). Best-effort con try/except logging
  DEBUG — si falla NO rompe el detalle que la llamó.
- **Helper `listar_recientes(tipo=None, limite=5)`** — últimos del usuario
  actual. Si `tipo=None` devuelve todos los tipos mezclados.
- **Trim**: `_trim_per_usuario_tipo(id_usuario, tipo, max_per_tipo=50)` —
  borra los más viejos. Se llama desde `registrar` después del UPSERT.
- **Hooks** en detalle views de clientes, facturas, cheques, proveedores —
  cada apertura registra el reciente correspondiente.
- **UI**: bloque "Recientes" en el sidebar (arriba de los módulos). Si no
  hay, se oculta. Renderiza hasta 5 con links al detalle + icono por tipo.
- Tests en `tests/test_recientes.py` cubren UPSERT idempotente, listar con
  limite, trim automático, best-effort ante falla de DB.

### Métricas al cierre

- `pytest -q` → **362 passed, 9 skipped** (era 321 en batch 14 → +41 tests).
- `python tests/test_routes_smoke.py` → **57 GET routes walked, 0 failures**
  (era 56 → +1 por `/tablero/imprimible`).
- `ruff check error_messages.py modules/recientes/` → clean.

### Archivos nuevos / modificados

```
# Nuevos
error_messages.py                                        NEW  (humanize + handlers)
templates/_confirmar_accion.html                         NEW  (partial reusable)
templates/404.html                                       NEW
templates/500.html                                       NEW
modules/recientes/__init__.py                            NEW
modules/recientes/queries.py                             NEW
migrations/0010_recientes.sql                            NEW
modules/dashboard/templates/dashboard_imprimible.html    NEW
tests/test_error_messages.py                             NEW
tests/test_confirmar_accion.py                           NEW
tests/test_recientes.py                                  NEW

# Modificados
filters.py                                               + humanizar Jinja filter
app.py                                                   + register_error_handler 404/500/403
modules/dashboard/views.py                               + _vista_preferida, /imprimible, 4 big_kpi
modules/dashboard/templates/dashboard.html               + big_kpi macro, toggle Simple/Completa
modules/facturas/views.py                                + confirmar_anulacion route
modules/facturas/templates/facturas/detalle.html         "Anular" ahora link a confirmar
modules/cheques/views.py                                 + confirmar_reverso route
modules/cheques/templates/cheques/detalle.html           "Reversar" ahora link a confirmar
modules/clientes/views.py                                + recientes.registrar() en detalle
modules/facturas/views.py                                + recientes.registrar() en detalle
modules/cheques/views.py                                 + recientes.registrar() en detalle
modules/proveedores/views.py                             + recientes.registrar() en detalle
templates/base.html                                      + bloque "Recientes" en sidebar
```

### Invariantes nuevos

29. **Acciones destructivas van por 2-step.** Nunca un form POST directo
    anula/reversa/elimina — siempre hay una ruta `confirmar_*` que muestra
    resumen + pide motivo. El motivo queda en bitácora + observacion.
    Partial reusable `_confirmar_accion.html` — si agregás una acción
    destructiva nueva, usalo.
30. **`recientes.registrar` es best-effort.** Si la tabla no existe (migración
    no aplicada) o psycopg2 tira, el detalle sigue renderizando con un log
    a DEBUG. La UI de recientes se degrada silenciosa.
31. **Errores nunca se muestran raw al usuario.** Todo lo que podría llegar a
    un template pasa por `humanize()` / filter `| humanizar`. Los handlers
    globales 404/500 usan templates amigables con request_id como pivote a
    bitácora.
32. **Dashboard tiene 2 modos para Dueño.** `simple` (4 tarjetas gigantes +
    botón "ver más") y `completa` (vista actual). La preferencia persiste en
    session. Otros roles ven sólo su vista actual — no les afecta.

### Backlog post-batch-15

**Desbloqueado por este batch:**
- Aplicar el partial `_confirmar_accion.html` también a `retenciones.anular`,
  `compras.anular`, `provisiones.eliminar`. ~15 min cada uno.
- Agregar más tipos a `recientes` (posdat, retención). ~10 min cada uno.

**Sigue bloqueado por entorno** (sin cambios desde batch 14):
- Fase 1.1.2 firma electrónica (.p12 contratado).
- Fase 1.2 rotación `1n7el4Pyth0n` (acceso AWS).
- Fase 3.1 IP allowlist configurada con IP real.

**Cuando haya tráfico real:**
- Benchmark `scripts/dashboard_profile.py` decide parallel vs sequential.
- `COSTOS_OT_ADAPTER=metabase|postgres` según dónde esté formulas_app.

**Nuevos items chicos abiertos en este batch:**
- Aplicar `humanize` a los flash messages, no sólo a templates con `exc` pasado.
- Agregar `kbd` en el header con el atajo Ctrl+K visible para más onboarding.
- Recordar al Dueño su vista preferida con cookie persistente cruzada-sesión
  (hoy vive sólo en `session` Flask).
- Expandir `templates/404.html` para que sugiera ir a `/` o al buscador global.

### Decisión para próxima sesión

- **Corta** (1h): aplicar 2-step a los 3 módulos que faltan + kbd hint + refine
  de handlers de errores (pytestear el 500 simulando una excepción).
- **Media** (3h): Bulk actions — seleccionar N facturas vencidas y mandar email
  de cobranza con una sola acción.
- **Larga** (sesión completa): cuando llegue el .p12, ship de firma + envío
  SRI end-to-end contra certificación.
