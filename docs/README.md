# Programa Core — guía de docs

Esta carpeta es el **knowledge base persistente** del proyecto. Cuando
cualquier persona (humano o IA) abre una sesión nueva, debería leer
estos archivos en el orden que se indica abajo.

## Orden de lectura para una sesión nueva

1. **`ESTADO_AL_CIERRE.md`** — el índice maestro. Contiene snapshot del
   estado actual (tests, rutas, migraciones), invariantes consolidados
   del proyecto, y resumen de todos los batches.
2. **El último `SKILL_ADDENDUM_BATCH_*.md`** — detalle granular de la
   última sesión.
3. **`PARIDAD_DBASE_*.md`** — qué del legacy dBase está/no está implementado.
4. **`SCHEMA.txt`** — dump en texto plano de las 40 tablas de Postgres.

## Mapa de docs

### Estado actual
- **`ESTADO_AL_CIERRE.md`** — índice maestro, snapshot, invariantes.

### Sesiones cronológicas (`SKILL_ADDENDUM_BATCH_*.md`)
Cada batch documenta una sesión de trabajo. Las primeras 8 están
consolidadas en el SKILL.md del skill `programa-core`. Desde la 9 en
adelante están acá:

| Batch | Fecha | Tema |
|-------|-------|------|
| 1-8 | 2026-04-16 → 17 | (en SKILL.md) Scaffolding inicial + módulos CRUD + migraciones + tests + ops |
| 9 | 2026-04-17 | Cleanup post-deploy + live stock + 2FA + password policy |
| 10 | 2026-04-17 | SRI MVP (XML + clave acceso + persistencia) |
| 11 | 2026-04-17 | Costos OT + cheque rechazado conciliación + IP allowlist + Docker + integration tests |
| 12 | 2026-04-17 | `/healthz` + notas crédito SRI + runbook docker |
| 13 | 2026-04-17 | CSV upload inline (facturas/cheques/compras) |
| 14 | 2026-04-17 | UX quick wins (DD/MM/YYYY, autocomplete, Ctrl+K, sidebar mobile) |
| 15 | 2026-04-17 | UX tier 2 (errores en español, dashboard modo, undo 2-step, recientes) |
| 16 | 2026-04-24 | 4 quick wins + bulk actions cartera + dashboard fixes + módulos gastos/retiros/activos/iniciales/cobranzas |
| 17 | 2026-04-27 | Cierre de paridad dBase (snapshots historia + depósito lote + anticipos + wizard chequera + cleanstr + cartera grupos + difuntos) + 26 tests nuevos |

### Hotfixes
- **`HOTFIX_2026-04-17.md`** — 3 bugs del primer arranque post-batch-12

### Auditorías
- **`PARIDAD_DBASE_2026-04-27.md`** — comparación exhaustiva de los flujos
  legacy `.PRG` vs el nuevo app. Identifica gaps. Cerrados en batch 17.

### Runbooks operativos
- **`RUNBOOK_docker_local.md`** — docker compose para correr local
- **`RUNBOOK_rotar_credencial.md`** — procedimiento para rotar la
  contraseña vieja `1n7el4Pyth0n` en producción

### Referencia
- **`SCHEMA.txt`** — `pg_dump --schema-only` en texto plano (40 tablas)

## Cómo agregar una sesión nueva al knowledge base

Al cerrar una sesión:

1. **Crear `SKILL_ADDENDUM_BATCH_<N+1>.md`** con detalle granular —
   bloques temáticos, archivos tocados, invariantes nuevos, gotchas.
2. **Actualizar `ESTADO_AL_CIERRE.md`**:
   - Snapshot al inicio (tests, rutas, migraciones, ruff status)
   - Sección con resumen del batch nuevo
   - Apendear nuevos invariantes a la lista consolidada
3. **No editar batches viejos** — append-only paper trail.

## Estado al 2026-04-27 (fin batch 17 + hotfixes)

- **388 tests passed** (262 al inicio del proyecto)
- **69 GET routes** smoke
- **12 migraciones** (`0001` → `0012`)
- **32 módulos** en `modules/`
- **`ruff check .`** — clean en todo el proyecto
- **Paridad dBase**: ~95% (era 78% antes de batch 17)
