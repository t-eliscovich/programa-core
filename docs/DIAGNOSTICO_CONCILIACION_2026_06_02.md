# Diagnóstico módulo conciliación — 2026-06-02

> Sesión "evaluar el proceso y redefinirlo si hace falta".
> Mirada de extremo a extremo: upload → matching → saldos → cierre → reapertura.

---

## TL;DR

El módulo está en estado **transicional avanzado**, no roto pero a medio camino.
La pantalla v2 (`/conciliacion/banco-v2` con sesiones persistentes y 3-4 tabs)
ya es lo que la dueña usa. El backbone (sesión, snapshots, balance,
matcher, cierre, reapertura) funciona. Lo que sobra es la v1 entera (≈ 70%
de `views.py` + `banco_resultado.html` con 3.300 líneas) que ya nadie
linkea pero sigue accesible por URL directa y duplica lógica.

El bug que viste hoy ("aprieto Conciliados y me manda al landing") es de
una sola línea — los tabs no preservan `sesion_id`. Lo arreglo abajo.

Veredicto: **no necesita rewrite, necesita Sprint 2 de limpieza** + dos
fixes puntuales (tab bug y vista detalle de sesión cerrada).

---

## 1. Bug del screenshot (tabs + sesión cerrada)

### Síntoma
Estás en una sesión cerrada y al clickear el tab "Conciliados" (o cualquier
tab) caés en la pantalla de upload con el banner azul *"Subí un extracto
para empezar la conciliación"*.

### Causa
`modules/conciliacion/templates/conciliacion/banco_v2.html:45` arma el
link del tab así:

```jinja
<a href="{{ url_for('conciliacion.banco_post_procesar', tab=slug) }}">
```

**No pasa `sesion_id`.**

Mientras la sesión está abierta no se nota porque
`banco_post_procesar` (línea 176) cae a `sesion_abierta(no_banco, usuario)`
y la encuentra. Pero apenas la sesión queda cerrada:

1. `sesion_abierta()` devuelve `None` (índice único parcial `WHERE cerrada_en IS NULL` no la trae).
2. Línea 178: `flash("Subí un extracto para empezar la conciliación.", "info")`.
3. Línea 179: `return redirect(url_for("conciliacion.hub"))` → landing.

### Fix mínimo (1 línea)
En `banco_v2.html:45`:

```jinja
<a href="{{ url_for('conciliacion.banco_post_procesar', sesion_id=sesion.id, tab=slug) }}">
```

Pero eso solo te resuelve a medias: con `sesion_id` y la sesión cerrada,
`banco_post_procesar` línea 181-183 te redirige a `/banco-v2/cerrada/<id>`,
que hoy es una pantalla anémica (matches hechos + botón "Excel"). No
muestra la lista de conciliados.

### Fix bueno (lo que en realidad querés)
Cuando la sesión está cerrada el tab "Conciliados" debería mostrar **lo
que se concilió en esa sesión** — read-only — sin redirigir.

Dos opciones, elegí:

**Opción A — cerrada con tabs (mínima)**
Hacer que `banco_post_procesar` acepte sesiones cerradas y renderice
`banco_v2.html` en modo `readonly=True`. Los tabs siguen mostrando lo
mismo, pero los checkboxes/botones desaparecen y "Conciliados" muestra
`matches_de_sesion(sesion)`. La pantalla `banco_v2_cerrada.html` queda
deprecada (o pasa a ser un sub-card).

**Opción B — cerrada con su propia pantalla**
Renombrar `banco_v2_cerrada.html` a algo tipo "resumen de sesión cerrada"
y agregarle 3 secciones (Manual / Impuestos / Transferencias) con los
matches que se hicieron en esa sesión. Botón "Reabrir" sigue. Sin tabs.

Recomiendo **A** porque ya tenés el partial de balance, el cuarto tab
Conciliados, y la query `matches_de_sesion`. Es agregar un flag `readonly`
al template y filtrar `if not readonly` en los formularios.

---

## 2. Endpoints — cuál es el vivo

### Pantalla landing
`GET /conciliacion/` y `/conciliacion/hub` renderizan `banco_upload.html`.
El form de upload (línea 200) **postea a `/banco-v2/crear-sesion`** —
o sea el flujo nuevo ya está enchufado desde el landing.

### Pantalla de trabajo activa
`GET /conciliacion/banco-v2?sesion_id=X&tab=manual|impuestos|transferencias|conciliados` →
`banco_v2.html` con 4 tabs y balance sticky arriba.

### Pantalla de sesión cerrada
`GET /conciliacion/banco-v2/cerrada/<id>` → `banco_v2_cerrada.html` (anémica).

### Pantalla de historial
`GET /conciliacion/banco-v2/historial` → tabla con todas las sesiones,
acciones: Excel / Reabrir / Borrar.

### Deshacer
`GET /conciliacion/banco-v2/deshacer` — lista matches activos +
txs BANCSIS creadas por conciliación (impuestos/agrupado). No te
manda al landing.

### Endpoints zombie (en código pero sin link UI)
- Todo el v1 hub POST → `banco_resultado.html` (3.300 líneas). Sigue
  accesible por URL directa como "rollback de emergencia".
- `/conciliacion/hub/selftest`, `/hub/kpi-debug`, `/hub/diag*` — diagnósticos.
- `/conciliacion/cambios` — el comentario en `app.py:346-347` dice que
  está "eliminado", pero `cambios_view.py` sigue en el repo. Probablemente
  no se importa → rutas no registradas → archivo muerto.
- `/conciliacion/banco/historico/marcar-conciliado` — solo accesible
  desde JS de `banco_resultado.html`.
- `/conciliacion/imprimir-banco` — hoja T-account imprimible sin link.

---

## 3. Qué pasa cuando subís un nuevo archivo

### Flow `POST /conciliacion/banco-v2/crear-sesion`

1. **Hash sha256** del archivo crudo.
2. **Cross-check duplicado**: `sesion_por_hash(no_banco, hash)`. Si ya
   existe sesión (abierta o cerrada) con ese hash y no marcaste
   `forzar=1` → flash con info de la sesión previa y redirect al landing,
   **sin crear nada**.
3. **Parse** con `parse_banco_xlsx(raw)` → `list[MovBanco]`. Headers
   esperados: `Fecha | Concepto | Documento | Monto | Saldo | Codigo | Tipo | Oficina`.
   Monto siempre positivo, signo viene de `Tipo` ∈ {C, D}.
4. **Crear sesión** (`crear_sesion`):
   - Si había otra sesión abierta tuya (mismo usuario + banco), la
     cierra con `cerrada_por='auto-replaced'`.
   - INSERT en `scintela.banco_conciliacion_sesion` con `extracto_payload`
     (jsonb con la lista entera), `extracto_hash`, `extracto_nombre`.
5. **Snapshot `'sesion_abierta'`** en `banco_saldo_conc_snapshot`.
6. **Redirect** a `/banco-v2?sesion_id=<id>`.

### Estado de la DB justo después del upload

| Tabla | Cambia? | Cómo |
|---|---|---|
| `banco_conciliacion_sesion` | sí | 1 fila nueva, `cerrada_en=NULL`, payload completo |
| `banco_saldo_conc_snapshot` | sí | 1 fila con `evento_tipo='sesion_abierta'` |
| `transacciones_bancarias` | **no** | el extracto NO se inserta acá hasta que confirmes matches |
| `banco_historicos_pendientes` | no | tampoco |
| `banco_conciliacion_match` | no | tampoco |

**Importante**: el matcher NO corre todavía. Corre lazy en cada GET de
`/banco-v2` (`estado_sesion` → `matchear_extracto_banco`). Es decir, el
upload es barato y reversible; ningún saldo cambia.

---

## 4. Cómo cambian los saldos en cada operación

La fórmula canónica vive en `modules/conciliacion/balance_pichincha.py:calcular()`.
La rinde el partial `_balance_pichincha.html`. Cinco saldos en juego:

```
  Saldo banco programa (libros, live)         ← último saldo de transacciones_bancarias
− Pendientes de conciliar en programa (N)
    + entradas (Σ créditos PC sin conciliar)
    − salidas  (Σ débitos PC sin conciliar)
    neto                                       ← pendientes_conciliar_neto
= Saldo conciliado                             ← saldo − neto pendientes PC
+ Pendientes de conciliar en banco (N)
    + entradas (Σ tipo='C' en histos)
    − salidas  (Σ tipo='D' en histos)
    neto                                       ← neto_pendientes
= Saldo banco esperado                         ← saldo conciliado + neto histos
```

### Qué define "conciliado" para una fila de programa
Doble OR (en `hoja_queries.py:_COND_CONCILIADO`):

```sql
TRIM(COALESCE(t.stat,'')) = '*'
   OR EXISTS (SELECT 1 FROM banco_conciliacion_match m
               WHERE m.id_transaccion = t.id_transaccion
                 AND m.deshecho_en IS NULL)
```

### Qué define "pendiente" para un histo banco
`banco_historicos_pendientes.conciliado_en IS NULL` (canónico).
*No* `conciliado_match_id IS NULL` — ya tuvimos esa lección.

### Tabla: cómo se mueven los saldos

| Operación | `saldo` libros | `pendientes PC` | `pendientes banco` | `saldo conciliado` | `saldo banco esperado` |
|---|---|---|---|---|---|
| Subir extracto | = | = | = | = | = |
| Confirmar match (1:1) | = | ↓ | ↓ si era histo | ±neto | = (cancela) |
| Crear tx desde real (entrada/salida nueva) | ± | = | ↓ si era histo | ± | = |
| Crear tx agrupada (impuestos N:1) | ± | = | ↓↓ | ± | = |
| Deshacer match | = | ↑ | ↑ si era histo | ∓ | = |
| Cerrar sesión | = | = | ↑ (real_only se promueven a histos) | = | ↑ |
| Reabrir sesión | = | = | ↓ (vuelven a sesión) | = | ↓ |
| Borrar sesión | = | =/↑ | ↑ (histos vuelven a pendientes) | ± | ± |
| Reset all | ↓ a baseline DBF | ↑ | ↑ | ↓ | ↓ |

### Tres flags conviven (y todos cuentan)

| Flag | Tabla | Qué significa | Quién lo toca |
|---|---|---|---|
| `stat='*'` | `transacciones_bancarias` | conciliado del lado DBF | sync DBF (one-way), `confirmar_match` (dual-write), `romper_match` (revierte) |
| `match.deshecho_en IS NULL` | `banco_conciliacion_match` | match activo de PC | `confirmar_match`, `romper_match` (soft-undo), `anular_grupo` (hard-delete) |
| `conciliado_en IS NOT NULL` | `banco_historicos_pendientes` | histo banco ya consumido | `confirmar_match` cuando el real era histo, `romper_match` resetea |

Cualquier mutación serias mantiene los tres sincronizados. Si quedan
desincronizados aparecen "stat fantasma" (mov con `stat='*'` sin match
activo) — endpoint `limpiar_stat_fantasma` los corrige.

---

## 5. Sesiones — ciclo de vida

### Estados
| Estado | Cómo se llega | Reversible? |
|---|---|---|
| Abierta | `crear-sesion` o `abrir-vacia` | sí, cerrando |
| Cerrada | `banco_terminar` | sí, `banco_reabrir_sesion` (decisión 2026-05-29) |
| Auto-reemplazada | otra sesión abierta del mismo usuario+banco fuerza el cierre | sí, reabriendo |
| Borrada | `banco_borrar_sesion` (reversibilidad limitada — borra matches dentro de la ventana) | no |
| Reset all | `banco_reset_all` con `confirm_text="BORRAR TODO"` | no |

### Único índice
`scintela.banco_conciliacion_sesion (no_banco, usuario) WHERE cerrada_en IS NULL`
→ **una sola sesión abierta por usuario y banco**.

### Qué persiste al cerrar
- `banco_conciliacion_sesion`: `cerrada_en`, `cerrada_por`, `pdf_path`.
- Snapshot `'sesion_cerrada'`.
- Todos los matches de la sesión en `banco_conciliacion_match` (con `creado_en` en la ventana).
- Nuevos `banco_historicos_pendientes` con `fuente=sesion:<id>` (los real_only del extracto que NO se conciliaron — quedan como pendientes futuros).
- Tx BANCSIS creadas por impuestos/agrupado en `transacciones_bancarias` con `metodo='created_from_real_grouped'`.
- Cambios de `stat='*'` en las txs PC matcheadas.
- Marcas `conciliado_en/conciliado_por/conciliado_match_id` en los histos consumidos.
- XLSX en disco (`data/conciliacion_pdfs/sesion_<id>.xlsx`).

---

## 6. Problemas detectados

### Críticos (afectan UX hoy)
1. **Tabs no preservan `sesion_id`** (el bug del screenshot). Fix de 1 línea.
2. **`banco_v2_cerrada.html` no muestra los conciliados** — la dueña tiene que reabrir la sesión para ver qué hizo. Fix = opción A o B arriba.

### Estructurales
3. **Tres fuentes de verdad para "conciliado"** (`stat`, `match.deshecho_en`, `conciliado_en`). Funcional pero frágil. Cada mutación tiene que mantener los tres. Hay endpoints para limpiar inconsistencias (`limpiar_stat_fantasma`, `deshacer_todos`), pero el split sigue.
4. **Coexistencia v1/v2**: `banco_resultado.html` (3.300 líneas), endpoints `hub_*` (≈ 1.700 líneas en `views.py`), partial `_balance_pichincha.html` con un `{% if false %}` envolviendo el render viejo. Todo eso es Sprint 2.
5. **`banco_saldo_conc_snapshot` se crea por bootstrap on-demand**, no por migration formal. Si el bootstrap falla silencioso, queda inexistente.
6. **`cambios_view.py` probablemente código muerto** (rutas no registradas).
7. **`saldo_snapshot.snapshot` es fail-soft** — si falla no avisa. Sesiones cerradas sin snapshot caen a fallback por timestamp.
8. **Fallback por idx en confirmaciones**: si el cliente manda checkboxes con índices viejos (no firmas), el matcher puede operar sobre filas mal alineadas. Flash "tu pantalla está vieja — recargá" mitiga pero no previene.

### Cosméticos
9. **`hub_historico_marcar_conciliado`** setea `conciliado_en` pero NO `conciliado_match_id` → audit trail incompleto.
10. **`crear_transaccion_agrupada_desde_reals`** tiene `except: pass` ciego (el bug N:1 ya fixeado vivía ahí).
11. Endpoint `/banco-v2/pdf/<id>` conserva el nombre "pdf" pero descarga `.xlsx`.

---

## 7. Propuesta de plan (Sprint 2)

### Tier 1 — Fixes UX (sesión actual)
- [ ] Fix tab links: agregar `sesion_id=sesion.id` en `banco_v2.html:45`.
- [ ] Rediseñar pantalla cerrada: render `banco_v2.html` en modo `readonly` para sesiones cerradas (Opción A). Mostrar los 4 tabs con datos read-only.
- [ ] El tab "Conciliados" en cerrada usa `matches_de_sesion(sesion)` que ya existe.

### Tier 2 — Limpieza (1 PR, bajo riesgo)
- [ ] Borrar `banco_resultado.html` (3.300 líneas) tras confirmar que ninguna URL externa lo apunta.
- [ ] Eliminar endpoints v1 deprecated:
  `hub_match_click`, `hub_match_manual`, `hub_crear_bancsis*`, `hub_aceptar_*`, `marcar_depositos_dia_conciliados`, `hub_confirmar_matches`, `hub_diag*`, `hub_selftest`, `hub_kpi_debug`.
- [ ] Eliminar el `{% if false %}` block (líneas 47-194) de `banco_upload.html` y el código backend que solo sirve a esas variables (`saldo_conc_split`, `saldo_dbase`, pendientes_conciliar_rows… si nadie más los usa).
- [ ] Verificar y borrar `cambios_view.py` si confirmás que las rutas no están registradas.
- [ ] Borrar `imprimir_banco` o engancharlo en la UI.

### Tier 3 — Refactor (1-2 sprints)
- [ ] Migration formal para `banco_saldo_conc_snapshot` y `conciliacion_upload` (mover de bootstrap on-demand).
- [ ] Helper `set_conciliado(tx_id, match_id)` que sincronice los tres flags en un lugar. Todo `UPDATE stat='*'` o `INSERT match` directo se reemplaza por la llamada.
- [ ] `pass_origen` en `MatchPropuesto` como campo dedicado (hoy es substring del `razon`).
- [ ] Forzar firmas en confirmaciones (rechazar idx-only): elimina el bug clase "pantalla vieja".

### Tier 4 — Opcional / abierto
- [ ] Sesión expira a N días (¿30?). Hoy no caduca.
- [ ] PDF "formato hoja FEB" (hoy es XLSX — la dueña aprobó el cambio).

---

## 8. Archivos clave para la próxima sesión

- `modules/conciliacion/banco_v2_view.py` (2.300 líneas — código vivo).
- `modules/conciliacion/sesion.py` (manejo de sesión, snapshot por hash).
- `modules/conciliacion/saldo_snapshot.py` (snapshots por evento).
- `modules/conciliacion/balance_pichincha.py` (helper canónico de saldos).
- `modules/conciliacion/matcher_banco.py` (PASS 0/1/1.5/2/3 + confirmar/romper match).
- `modules/conciliacion/templates/conciliacion/banco_v2.html` (← bug del tab).
- `modules/conciliacion/templates/conciliacion/banco_v2_cerrada.html` (← rediseñar).
- `modules/conciliacion/templates/conciliacion/_balance_pichincha.html` (partial reusable).
- `modules/conciliacion/views.py` (≈ 70% deprecated — Sprint 2 lo encoge a 500 líneas).
- `docs/PLAN_CONCILIACION_REFORM_2026_05_28.md` (fuente de verdad de Sprint 1).
- `docs/HALLAZGOS_E2E_2026_05_29.md` (bugs $43K y N:1 resueltos).
- Migraciones: `0046`, `0047`, `0053`, `0054`, `0056-0058`, `0060`, `0061`.
