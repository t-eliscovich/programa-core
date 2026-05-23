# Plan — terminar `/conciliacion/banco` para que sea user-friendly y racional

**Fecha:** 2026-05-23
**Owner:** Tamara (UI/UX). Implementación: próxima sesión.
**Reemplaza:** nada nuevo; complementa `docs/CONCILIACION_CONTRACT.md` (que sigue activo para `/informes/balance`).

## 1. Filosofía

Una sola pantalla, un solo objetivo: **subir el extracto del banco, ver qué falta de cada lado, resolver, y que mañana NO me vuelva a aparecer lo resuelto.** Cualquier diferencia se resuelve con UN click; cualquier error se deshace con UN click. Sin código de debug. Sin JSON crudo. Sin "fijate vos cómo lo arreglás".

## 2. Estado actual (auditado el 2026-05-23)

| Pieza | Archivo | Estado |
|---|---|---|
| Upload xlsx Pichincha | `modules/conciliacion/templates/conciliacion/banco_upload.html` | OK, minimal |
| Parser xlsx | `modules/conciliacion/parser_banco.py` | OK; tolera fechas y montos con coma |
| Matcher bidireccional | `modules/conciliacion/matcher_banco.py` | OK; greedy por score, excluye ya conciliados |
| Persistencia | `migrations/0046_banco_conciliacion_match.sql` + `scintela.banco_conciliacion_match` | OK; dedupe por firma REAL e id_transaccion |
| Vista resultado | `modules/conciliacion/templates/conciliacion/banco_resultado.html` | Funciona, **pero falta acciones** |
| Confirmar matches (bulk) | `views.py:hub_confirmar_matches` | OK |
| Aceptar bancsis_only (una) | `views.py:hub_aceptar_bancsis_only` | OK |
| **Acción en real_only** | — | **NO EXISTE** |
| **Match manual** | — | **NO EXISTE** |
| **Undo / pantalla de conciliados** | — | **NO EXISTE** |
| **Selector de banco** | — | Hardcoded `_BANCO_PICHINCHA = 10` |
| Endpoint de error con traceback inline | `views.py:514-526` | **DEBUG; hay que sacarlo** |
| Hub `/conciliacion/hub` | `templates/conciliacion/hub.html` | **No incluye el flow nuevo** — 2 cards viejas, el flow `/banco` queda huérfano del menú |

## 3. Decisiones del 2026-05-23 (validadas con Tamara)

- **Real-only → "Crear en BANCSIS"** desde la UI (un click → inserta en `scintela.transacciones_bancarias` vía `bank_helpers.insert_movimiento_bancario`).
- **Sí al selector de banco**, sí al match manual, sí al undo. Todo bajo el lente "racional y simple".
- **Limpieza:** sacar JSON inline de debug; reorganizar `/conciliacion/hub` para que el flow banco sea primario.

## 4. Pantallas finales

### 4.1 `/conciliacion/banco` — paso 1: upload

```
┌──────────────────────────────────────────────┐
│ Conciliación bancaria                        │
│                                              │
│ Banco: [▼ Pichincha (no_banco=10)]           │ ← selector nuevo
│ Archivo: [Seleccionar .xlsx]   [Procesar →]  │
│                                              │
│ Últimos extractos procesados:                │ ← lista breve
│ • 22/05/2026 — 247 movs, 12 sin resolver     │
│ • 15/05/2026 — 198 movs, todo conciliado     │
└──────────────────────────────────────────────┘
```

### 4.2 `/conciliacion/banco` — paso 2: resultado

Header con KPIs (ya existe) + tabla unificada con tabs:

```
┌──── Saldos ─────────────────────────────────────────────────┐
│ Real $X · Programa $Y · Diferencia $Z  [✓ Cuadra] / [✗ Falta]│
│ "Para que cuadren te faltan: 3 movs Solo en Real ($abc),     │
│  1 mov Solo en Programa ($def)"                              │
└─────────────────────────────────────────────────────────────┘

┌─[ Matches (84) ] [ Solo en banco (12) ] [ Solo en sistema (3) ]─┐
│                                                                  │
│ [Filtrar: ____________]  [▼ Tipo: todos]   [Confirmar todos]    │
│                                                                  │
│ Tabla con la pestaña activa.                                     │
└──────────────────────────────────────────────────────────────────┘
```

Las 3 tablas en tabs (no `<details>` apilados) — más ordenado con 250+ filas.

**Acciones por fila:**

| Pestaña | Acciones por fila |
|---|---|
| Matches | `✓ Confirmar` (individual) · `✗ Romper este match` · `🔗 Match manual con otro` |
| Solo en banco (real_only) | **`+ Crear en sistema`** (acción principal) · `Aceptar como diferencia` · `🔗 Match manual con un Solo-en-Programa` |
| Solo en sistema (bancsis_only) | `Aceptar como diferencia` · `🔗 Match manual con un Solo-en-Banco` |

**Match manual:** abre un modal con la lista del otro lado filtrada por monto ±$10 y fecha ±15d, y permite forzar el vínculo aunque el matcher no lo hubiera elegido. Persiste como `estado='matched'` en `banco_conciliacion_match`.

### 4.3 `/conciliacion/banco/historial` — undo

Lista paginada de lo conciliado (`SELECT FROM banco_conciliacion_match`), filtrable por banco y rango. Cada fila tiene `[Deshacer]` que hace `DELETE` (no soft-delete — la idempotencia del matcher lo trae de vuelta en el próximo upload).

### 4.4 `/conciliacion/hub` — reorganizar

```
┌─────────────────────────────────────────────────────┐
│ Conciliación bancaria  ← card grande arriba (full)  │
│ Subí el extracto del banco y resolvé todo en una    │
│ sola pantalla.                                       │
│ [Empezar →]                                          │
└─────────────────────────────────────────────────────┘

Utilidades específicas:
┌──────────────────┐  ┌──────────────────┐
│ Depósitos        │  │ Cheques          │
│ pendientes       │  │ rebotados        │
│ (Excel sistema)  │  │ (CSV banco viejo)│
└──────────────────┘  └──────────────────┘
```

Banco = principal. Los otros 2 quedan como utilidades para flujos específicos (cuando solo querés ver depósitos sin todo el extracto, o cuando estás cazando rebotes y querés que dispare `cheques.reversar()` + STOP).

## 5. Cambios por archivo

### Backend

| Archivo | Cambio |
|---|---|
| `modules/conciliacion/views.py` | (a) sacar el `return {..., traceback: ...}, 500` (líneas 514-526) y reemplazarlo por `flash_exc(...)` + redirect al upload. (b) aceptar `no_banco` desde el form. (c) endpoints nuevos: `/banco/crear-bancsis` (POST), `/banco/match-manual` (POST), `/banco/romper-match` (POST), `/banco/historial` (GET), `/banco/deshacer` (POST). |
| `modules/conciliacion/matcher_banco.py` | Agregar `crear_transaccion_desde_real(no_banco, real: MovBanco, usuario)` que: (1) llame a `bank_helpers.insert_movimiento_bancario` con el documento correcto según `tipo` C/D, (2) inserte en `banco_conciliacion_match` con el `id_transaccion` recién creado, estado `'matched'`. Atómico (`db.tx`). |
| `modules/conciliacion/matcher_banco.py` | `match_manual(no_banco, real, id_transaccion, usuario)` — fuerza un match sin pasar por el scorer. |
| `modules/conciliacion/matcher_banco.py` | `romper_match(id)` — DELETE de `banco_conciliacion_match`. |
| `modules/conciliacion/matcher_banco.py` | `historial(no_banco=None, desde=None, hasta=None, limit=200)` — lista paginada para `/historial`. |
| `modules/conciliacion/queries.py` | `bancos_disponibles()` — `SELECT no_banco, nombre FROM scintela.banco ORDER BY no_banco` para el selector. |
| `migrations/0047_banco_conciliacion_match_audit.sql` | Agregar columnas `metodo TEXT` (matched_auto / matched_manual / created_from_real) y `deshecho_en TIMESTAMP`. Backfill `metodo='matched_auto'`. |

### Templates

| Archivo | Cambio |
|---|---|
| `templates/conciliacion/banco_upload.html` | Agregar dropdown de bancos + listado de "últimos extractos". |
| `templates/conciliacion/banco_resultado.html` | Pasar de `<details>` a tabs. Agregar filtro de texto/tipo. Botones por fila. Modal de match manual (un solo modal reutilizable, JS minimal). |
| `templates/conciliacion/banco_historial.html` | **Nuevo.** Tabla con `[Deshacer]`. |
| `templates/conciliacion/hub.html` | Card grande arriba para `/banco`, las 2 viejas más chicas debajo. |

### Tests

| Test | Cubre |
|---|---|
| `test_crear_transaccion_desde_real_tipo_C` | Real-only Tipo C → `bank_helpers.insert_movimiento_bancario(doc='DE', ...)` + match persistido. |
| `test_crear_transaccion_desde_real_tipo_D` | Real-only Tipo D → `doc='CH'` + match persistido. |
| `test_match_manual_persiste_y_excluye_de_siguiente_upload` | Fuerza match → siguiente `matchear_extracto_banco()` no lo trae. |
| `test_romper_match_revierte_y_reaparece` | DELETE → vuelve a aparecer como real_only/bancsis_only. |
| `test_no_banco_selector_lee_form` | POST con `no_banco=3` matchea contra banco 3, no Pichincha. |
| `test_historial_paginado_y_filtrado` | `historial()` respeta `desde`/`hasta`. |
| `test_views_error_parser_no_devuelve_json` | El branch debug está borrado: cuando el parser rompe se redirige con flash. |

## 6. Fases (ordenadas por riesgo/dependencia)

**Fase A — Limpieza (1h, sin riesgo):**
1. Borrar el `return {..., traceback: ...}, 500` y poner `flash_exc + redirect`.
2. Reorganizar `hub.html` (banco arriba).
3. Test `test_views_error_parser_no_devuelve_json`.

**Fase B — Crear en BANCSIS (3-4h, alto impacto UX):**
1. `crear_transaccion_desde_real()` en matcher_banco.py.
2. Endpoint `/banco/crear-bancsis`.
3. Botón en `banco_resultado.html` por fila de real_only.
4. Tests `test_crear_transaccion_desde_real_*`.
5. **Verificar manualmente** con un real_only conocido que el `transacciones_bancarias.saldo` recompute funcione (`bank_helpers.recompute_saldos_desde` si insertamos al medio).

**Fase C — Selector de banco (1-2h):**
1. `bancos_disponibles()` + dropdown en `banco_upload.html`.
2. Aceptar `no_banco` desde el form en `hub()`.
3. Test `test_no_banco_selector_lee_form`.

**Fase D — Match manual + undo (3-4h):**
1. Migration 0047 (columnas `metodo`, `deshecho_en`).
2. `match_manual()` + `romper_match()` + `historial()`.
3. Vista `/banco/historial` + botón "Deshacer".
4. Modal de match manual en `banco_resultado.html`.
5. Tests.

**Fase E — UX polish (2-3h):**
1. Tabs en lugar de `<details>`.
2. Filtro de texto/tipo.
3. Lista de "últimos extractos" en upload.
4. Botón "Descargar reporte CSV" del resultado actual.

## 7. Invariantes que NO se tocan

- `scintela.banco_conciliacion_match.id_transaccion` sigue siendo UNIQUE — una tx BANCSIS, un match.
- La firma REAL (`fecha + documento + monto + tipo`) sigue siendo la clave del lado real — re-subir el mismo xlsx no duplica.
- `bank_helpers.signo_documento` no se toca. Real Tipo='C' → documento ∈ DOCS_ENTRADA. Real Tipo='D' → documento ∉ DOCS_ENTRADA (default 'CH').
- El filtro `cheques_depositados_rango` (stat='B') del flow de rebotes no se toca — sigue vivo en `/conciliacion/` (CSV) porque ese flow tiene side-effect propio (`cheques.reversar()` + STOP) que el flow nuevo NO replica.

## 8. Riesgos / cosas a confirmar antes de implementar

- **Saldo recompute al insertar al medio:** si `crear_transaccion_desde_real` inserta una fila con fecha pasada, `bank_helpers` necesita disparar `recompute_saldos_desde(ancla_id=...)`. Hay que verificar que ya lo hace o agregarlo. Si no, la fila queda con `saldo` mal computado y el próximo balance "no cuadra".
- **Permiso para insertar en `transacciones_bancarias`:** confirmar que `bancos.conciliar` permite escribir, no solo conciliar. Si no, agregar permiso o nuevo nombre.
- **Federico co-edita** ([feedback_git_workflow_federico](../../../Library/Application%20Support/Claude/local-agent-mode-sessions/...)): la conciliación no está en "áreas seguras". Avisar antes de tocar `informes/queries.py` si llega a hacer falta (probablemente no).
- **Multi-banco real:** los xlsx que pueda haber para otros bancos (Internacional, Produbanco) ¿tienen el mismo formato de columnas? Confirmar antes de habilitar el selector con bancos != Pichincha.

## 9. Definición de "terminado"

- ✅ Subo cualquier banco, no solo Pichincha.
- ✅ Cada fila tiene UNA acción obvia que la resuelve.
- ✅ Lo que resuelvo no me aparece mañana.
- ✅ Si me equivoco, puedo deshacer desde `/banco/historial`.
- ✅ El error de parseo me lleva al upload con un mensaje, no a un JSON.
- ✅ El hub destaca el flow correcto.
- ✅ 7 tests nuevos pasando.
