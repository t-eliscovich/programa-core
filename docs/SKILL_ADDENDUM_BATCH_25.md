# Skill addendum — Batch 25 (2026-05-23)

## Conciliación banco — plan de cierre

**Trigger:** "conciliar banco", "conciliación bancaria", "extracto Pichincha", "/conciliacion/banco", "/conciliacion/hub".

**Fuente de verdad del plan:** [`docs/PLAN_CONCILIACION_BANCO_2026_05_23.md`](PLAN_CONCILIACION_BANCO_2026_05_23.md). Ahí están las fases (A–E), los archivos a tocar y la definición de "terminado". Releer ese doc antes de tocar cualquier cosa del módulo `conciliacion/`.

## Estado al cierre de esta sesión

- Auditado todo el módulo `modules/conciliacion/` y la migration 0046.
- El flow `/conciliacion/hub` (o `/banco`) **funciona end-to-end** pero le faltan acciones críticas: crear-en-BANCSIS desde real_only, match manual, undo, selector de banco.
- Hay un branch de debug en `views.py:514-526` (retorna JSON con traceback inline cuando el matcher rompe) — quitar al empezar Fase A.

## Invariantes confirmados (no cambiar sin avisar)

- `_BANCO_PICHINCHA = 10` (no_banco=10 → `/bancos/10`). Confirmado por `/conciliacion/hub/diag`.
- Mapeo tipo banco ↔ documento BANCSIS:
  - Real `Tipo='C'` (crédito, entra plata) ↔ BANCSIS doc ∈ `('DE','TR','AC','NC')`.
  - Real `Tipo='D'` (débito, sale plata) ↔ BANCSIS doc ∈ `('CH','ND','DB')`.
- `scintela.banco_conciliacion_match` dedupea por:
  - `(no_banco, real_fecha, real_documento, real_monto, real_tipo)` — firma REAL.
  - `id_transaccion` UNIQUE — un tx BANCSIS, un match.
- Tolerancia default del matcher: ±5 días, ±$1, score = días + monto×10.
- El flow viejo `/conciliacion/` (CSV de cheques sospechosos de rebote) NO se borra — tiene side-effect propio (`cheques.reversar()` + STOP cliente) que el flow nuevo no replica.

## Lo NO obvio

- `bank_helpers.insert_movimiento_bancario` ya existe y es lo que tiene que usar el botón "Crear en BANCSIS". Atómico en `db.tx()`.
- Si se inserta una fila con fecha pasada, hay que verificar que el running `saldo` se recompute (`bank_helpers.recompute_saldos_desde`); si no, las filas posteriores quedan desincronizadas y el próximo balance no cuadra.
- `saldo_bancsis_final` del KPI viene de `transacciones_bancarias.saldo` del último mov por fecha — no es un SUM, es el running saldo stored. Si se rompe el recompute, este número miente.
- Cuando el matcher excluye filas ya conciliadas, lo hace en una ventana ±30 días del rango del extracto (`_ya_conciliadas`). Suficiente para uso normal; ojo si alguien sube un xlsx de hace 6 meses.

## Decisiones de UX validadas con Tamara (2026-05-23)

- Real-only se resuelve con **"Crear en BANCSIS"** desde la UI (acción primaria).
- Una sola pantalla, tabs en vez de `<details>`, acción obvia por fila, undo via `/banco/historial`.
- Hub: card grande para banco, las 2 utilidades viejas (depósitos pendientes, cheques rebotados) abajo y más chicas.

## Próxima sesión: por dónde empezar

Fase A del plan — limpieza sin riesgo, 1h, deja terreno listo para B (crear-en-BANCSIS).
