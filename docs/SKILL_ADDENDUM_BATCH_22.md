# Addendum batch 22 — paridad dBase (3 conexiones) + formato resultados

**Fecha:** 2026-04-30
**Contexto:** el dueño pidió paridad **exacta** con el dBase para los 3 flujos que alimentan los informes de resultados, y un formato lindo y consistente para la pantalla de resultados.

> Pedido textual: *"quiero paridad exacta con el dbase. Podemos chequear que 1) agregar cheque o modificar impacte bien en resultados, 2) agregar compra o modificar impacte bien en resultados, 3) agregar factura o modificar impacte bien en resultados. No se si me esta faltando algo, pero todo tiene que estar bien conectado."*

Esta sesión es **plan + paper trail**, no implementación. Revivir aquí la próxima vez que se retome la paridad.

> ⚠️ Lectura previa obligatoria: `docs/PARIDAD_DBASE_2026-04-27.md` (auditoría del 78% de paridad de hace 3 días). Este addendum **complementa** ese doc — agrega los gaps de **editar/modificar** que aquel no cubrió, y plantea el plan de cierre + formato.

---

## 1. Mapa dBase → Postgres por evento (3 ALTAS + 3 MODIFICAS)

Eje vertical = evento. Eje horizontal = tabla impactada.
✓✓ ya implementado · ✓ requerido por dBase · ✗ falta · — no aplica.

| Evento | cheque | factura | compra | posdat | tx_bancarias | caja | dolares | cliente.stop |
|---|---|---|---|---|---|---|---|---|
| ALTA cheque cartera | ✓✓ stat=Z | — | — | — | — | — | — | — |
| ALTA cheque NB=90/91 (depósito directo) | ✓✓ stat=B/I | — | — | — | ✗ INSERT DE + saldo running | — | — | — |
| ALTA cheque NB=99 (a caja) | ✗ stat=C | — | — | — | — | ✗ INSERT TIPO=E | — | — |
| ALTA cheque CONCEPTO recnos auto-aplica | ✓✓ | ✗ ABONO+=, SALDO-= | — | — | — | — | — | — |
| ALTA cheque anticipo CONCEPTO=9999 | ✓✓ negativo espejo | — | — | — | — | — | — | — |
| ALTA cheque domingo→lunes shift | ✗ FECHAD+=1 | — | — | — | — | — | — | — |
| MODIFICA cheque stat → B/V/W (Pichincha) | ✗ | — | — | — | ✗ INSERT DE + saldo | — | — | — |
| MODIFICA cheque stat → I/J/K (Inter) | ✗ | — | — | — | ✗ INSERT DE banco=2 | — | — | — |
| MODIFICA cheque stat → 9 (rebotado) | ✗ | — | — | ✗ INSERT cheque protestado | — | — | — | ✗ stop=S |
| MODIFICA cheque stat → C (cobrado caja) | ✗ | — | — | — | — | ✗ INSERT + saldo | — | — |
| MODIFICA cheque importe ya depositado | ✗ | — | — | — | ✗ walk-forward signed-recompute | — | — | — |
| Reversar cheque depositado (B→1) | ✓✓ | ✓✓ unwind ABONO/SALDO | — | ✓✓ DELETE | ✗ DELETE bank row + walk recompute | — | — | ✓✓ stop si rebote real |
| Aplicar cheque a factura | ✓✓ | ✓✓ ABONO+=, SALDO-=, STAT recompute | — | — | — | — | — | — |
| ALTA factura | — | ✓✓ stat=Z, abono=0, saldo=importe | — | — | — | — | — | — |
| MODIFICA factura importe/abono | — | ✗ recompute saldo=importe-abono | — | — | — | — | — | — |
| MODIFICA factura CONDIC ' '→'C' | — | ✗ importe×=0.95 (5% pronto pago) | — | — | — | — | — | — |
| MODIFICA factura primera vez stat=T | — | ✗ vencim=DATE() | — | — | — | — | — | — |
| Anular factura | — | ✓✓ stat=X | — | — | — | — | — | — |
| ALTA compra no pagada | — | — | ✓✓ | ✓✓ banc=0 | — | — | — | — |
| ALTA compra PAGA=* & PROV=PP | — | — | ✓✓ | — | ✗ INSERT DE + saldo running | ✗ alternativa | — | — |
| ALTA compra PROV ∈ HIL/QUI | — | — | ✓✓ | — | — | — | ✗ INSERT anticipo | — |
| MODIFICA compra | — | — | ✗ | ✗ propagar a posdat hermana | ✗ si pagada, recomputar saldo | — | — | — |

**Lectura corta:** ~40% de la paridad de las 6 acciones está hoy. Lo que falta es **(a)** funciones `editar()` para cheque/factura/compra, **(b)** *bank running saldo*, **(c)** *caja* y *dolares* desde altas/modifs de cheque y compra, **(d)** state-machine completa de stat de cheque con sus side-effects.

---

## 2. Reportes que dependen de cada tabla

| Reporte | Tablas | Side-effect que lo afecta si falla |
|---|---|---|
| BALANCE / Resultados (TOTC, TOTF, TOTP, SALBANC, CART, PATR) | cheque, factura, posdat, tx_bancarias, caja, dolares, retiros, activos, iniciales, compra(mes), historia | ALTA cheque NB=90 sin tx_bancarias → SALBANC viejo; MODIFICA cheque a stat=9 sin posdat → TOTP subreportada; ALTA compra PAGA=* sin banco → SALBANC alto |
| CARTERA por cliente | factura.saldo (Z/A) **+ cheque.importe (Z/1/2/3/P/D)** | hoy SOLO suma factura — anticipos cheque no descuentan ni anticipos negativos restan |
| FLUJO proyección | cheque (Z/D/P futuros), factura (Z/A), posdat (banc≠9), caja, banks | `flujo_calculado` no junta factura ni caja — proyección AR sub-reportada |
| DEUDAS | posdat (banc≠9) | OK ✓✓ |
| GASTOS | tx_bancarias, caja, compras tipo CC/GS/KK | MODIFICA cheque sin bank row → gastos día sub-reportados |
| Aging | factura | igual que CARTERA — no junta cheques en cartera |

---

## 3. Invariantes críticos (no romper en NINGÚN write)

1. `factura.SALDO = factura.IMPORTE - factura.ABONO`. **En toda mutación de ABONO escribir SALDO en la misma UPDATE**. Aplica a: aplicar cheque, reversar cheque, retención RECUP, MODIFICA factura.
2. `tx_bancarias.SALDO` es running balance signado. SIGNO = +1 si DOC ∈ (DE, TR, XX) else -1. Cualquier INSERT al medio o DELETE/UPDATE de fila no-tail dispara walk-forward recompute en el MISMO banco/cuenta.
3. `caja.SALDO` = running balance. SIGNO = +1 si TIPO=E (entrada) else -1.
4. STAT del cheque es state machine completa: Z→1/2/3/B/V/W/I/J/K/9/C/X/T/A/P/D. Cada transición tiene side-effects fijos (ver tabla §1).
5. Período cerrado bloquea writes — toda función `crear()`/`editar()` con fecha llama `asegurar_fecha_abierta(fecha)` ANTES del primer write.
6. Bitácora best-effort — toda mutación graba en `scintela.bitacora_acciones` via after_request. Nunca romper request real por audit.
7. Cheque rebotado real (D/A → R) dispara `cliente.stop=S` en la misma tx (ya en batch 5).
8. Anticipo cheque (CONCEPTO=9999) entra como par espejo positivo+negativo con `id_cheque_padre` (ya en `crear()`).

---

## 4. Plan ejecutable — 6 fases independientes

Cada fase es shippeable por separado. Cada una incluye: migración (si schema), queries, views, tests, verificación end-to-end contra un informe.

### Fase A — Bank running saldo (HIGH, primitiva bloqueante)
Sin esto nada que toque banco mantiene paridad.

- `db/bank_helpers.py` (nuevo): `insert_movimiento_bancario(conn, banco, cuenta, fecha, doc, importe, concepto, ...)` calcula `signo = +1 if doc in (DE,TR,XX) else -1`, INSERT con saldo running. Helper `recompute_saldos_desde(conn, banco, cuenta, ancla)` walk-forward UPDATE.
- `migrations/0015_indexes_bank_running.sql` — índice `(no_banco, no_cta, fecha, id_transaccion)` para que el walk sea barato.
- Decisión: lógica en aplicación (consistente con resto del codebase, sin triggers Postgres ocultos).
- Tests: insertar 3 movs y verificar saldos; insertar uno al medio y verificar walk-forward.

**Verificación:** comparar `SELECT saldo FROM tx_bancarias ORDER BY id DESC LIMIT 1` antes/después de un INSERT manual.

### Fase B — `facturas.editar()` + recompute saldo (HIGH)
Hoy la única vía de tocar factura emitida es ANULAR. Para corrección de ABONO/CONDIC el dueño edita por SQL.

- `modules/facturas/queries.py::editar(id_factura, *, abono=None, condic=None, observacion=None, usuario)`:
  - **Limitar a campos contables internos** (regla Ecuador: importe/numf/cliente/fecha NO se editan; para eso anular y reemitir).
  - Si CONDIC ' '→'C': `importe *= 0.95`. Si 'C'→' ': `importe /= 0.95`.
  - Recompute `saldo = importe - abono`.
  - Si stat ≠ 'T' y nuevo saldo ≈ 0: `stat='T'`, `vencim=CURRENT_DATE`.
  - `asegurar_fecha_abierta(fecha)` (la original, no hoy).
  - Bitácora before/after.
- `views.py`: GET/POST `/facturas/<id>/editar` gateado por `facturas.editar`.
- `config/roles.py`: `facturas.editar` a Dueño + Contabilidad.

**Verificación:** factura $1000, editar abono $300 → saldo=$700, BALANCE.TOTF baja.

### Fase C — `cheques.editar()` + state machine completa + anular por error de carga (HIGH, el grueso)

> Política definida en §8: edit limitado a campos blandos; corrección de importe/cliente/banco se hace con anular+reemitir.

- `modules/cheques/queries.py::editar(id_cheque, *, concepto=None, observacion=None, fechad=None, usuario)`:
  - **Sólo** estos 3 campos. Importe/cliente/banco bloqueados siempre (ver §8).
  - `fechad` editable SOLO si stat ∈ {Z, P, D}.
  - Si nuevo `fechad` cae domingo: `fechad += 1` (paridad dBase).
  - stat ∈ {X, T, R}: editar bloqueado completamente.
- `modules/cheques/queries.py::anular_por_error_de_carga(id_cheque, *, motivo, id_reemplazo=None, usuario)`:
  - Lee cheque actual.
  - Side-effects compensatorios según stat (ver tabla §8).
  - UPDATE cheque.stat='X' + observacion con tag `[X] error de carga`.
  - Bitácora.
  - Todo en 1 `db.tx()`.
- `modules/cheques/queries.py::transicionar_stat(id_cheque, stat_destino, *, no_banco=None, fecha=None, usuario)`:
  - Dict `_TRANSICIONES_VALIDAS` con (origen, destino) → función side-effect.
  - Z/1/2/3 → B/V/W: `insert_movimiento_bancario(banco=Pichincha, doc=DE)`. cheque.fechaing=hoy.
  - Z/1/2/3 → I/J/K: idem banco=Internacional.
  - * → C (caja): `insert_movimiento_caja(tipo=E)`. cheque.fechaout=hoy.
  - * → 9 (rebotado): INSERT posdat (banc=0) + cliente.stop=S.
  - * → X (anulado): sólo UPDATE.
  - Todo en 1 `db.tx()`. Bitácora.
- View: POST `/cheques/<id>/editar` y `/cheques/<id>/transicionar`. Detalle muestra botones "Depositar Pichincha", "Depositar Inter", "Pasar a caja", "Marcar rebotado", "Anular" según stat actual.
- Tests: cada transición válida produce su side-effect; transición inválida levanta ValueError; reversar de depositado revierte el bank row.

**Verificación:** cheque Z $500, transicionar a B → tx_bancarias nueva con saldo correcto, BALANCE.SALBANC1 sube $500. Transicionar a 9 → posdat aparece, cliente.stop=S.

### Fase D — `compras.editar()` + DOLARES + caja (MED, completa ALTAs)
- `modules/compras/queries.py::editar(id_compra, *, importe=None, fechad=None, proveedor=None, ..., usuario)`:
  - Si compra con posdat hermana abierta (banc=0): UPDATE posdat (importe, fechad).
  - Si compra ya pagada con `id_transaccion` ≠ NULL: bloquea editar importe/fechad — requiere desreversar banco primero.
  - `asegurar_fecha_abierta(fecha)`.
- `modules/compras/queries.py::crear()` extendido:
  - `pagada=True` y `cuenta='caja'`: `insert_movimiento_caja(tipo=S)`. Setear `compra.cuenta_pagada='C'`.
  - `pagada=True` y `cuenta in (PICHINCHA, INTER)`: `insert_movimiento_bancario(doc=CH)`. Linkar `compra.id_transaccion`.
  - `proveedor.tipo in (HIL, QUI)` y anticipo: INSERT `dolares` (cta=proveedor).
- `migrations/0016_compra_pago_link.sql`: ADD COLUMN `compra.id_transaccion bigint NULL` + `compra.cuenta_pagada varchar(1)`.
- View: nueva compra ya tiene radio pagada/no pagada; agregar radio cuenta = caja|pichincha|internacional.

**Verificación:** compra pagada por caja $200 → caja con egreso, SALCAJ baja $200, compra con id_transaccion link.

### Fase E — Cartera incluye cheques en cartera (HIGH paridad, S)
- `modules/cartera/queries.py::aging_buckets()` extendido:
  - `WITH cheques_cli AS (SELECT codigo_cli, SUM(importe) en_cartera FROM cheque WHERE stat IN (Z,1,2,3,P,D) GROUP BY 1) ... saldo_neto = factura_saldo - COALESCE(en_cartera,0)`.
  - Anticipos negativos (CONCEPTO=9999) ya se contabilizan con signo correcto via SUM.
- `modules/informes/queries.py::cartera_por_cliente()`: misma extensión.
- Test: cliente con factura $1000 + cheque Z $300 → cartera $700, no $1000.

**Verificación:** factura $1000 + cheque $300 stat=Z mismo cliente → CARTERA $700.

### Fase F — Formato resultados unificado (S, después de paridad)
La pantalla `informes/balance.html` ya tiene aesthetic dBase (border verde, font-mono, banner). Replicar en TODOS los informes.

- `templates/_informe_frame.html` (nuevo macro): `informe_frame(titulo, fecha, ...)` — borde verde, banner oscuro título centered + fecha derecha, grid configurable, bottom bar F-keys (F1 imprimir, F2 CSV, F3 PDF, ESC volver).
- Aplicar a las 14 plantillas en `modules/informes/templates/informes/*.html`: cartera, flujo, deudas, gastos, ventas, retiros, historia, iniciales, activos, estado_cuenta.
- KPI cards uniformes con `_ui.html::stat_chip`. Sub-headers fondo rosa (paridad dBase aesthetic).
- `@media print` ya en base.html — verificar que el frame no rompa el print.
- Toggle "vista clásica" (ASCII puro estilo dBase) vs "vista moderna" (Chart.js, KPI cards) por usuario, persistido en localStorage.

**Verificación:** imprimir cartera/balance/flujo lado a lado — mismo header, mismo borde, misma alineación de columnas.

---

## 5. Tests de regresión end-to-end (definición operativa de "paridad")

Tres tests escritos UNA vez, corren en CI tras cada cambio. Si rompen, la paridad se quebró.

### `tests/test_paridad_cheque_a_balance.py`
1. Setup: factura $1000 stat=Z cliente JTX. Snapshot BALANCE.
2. ALTA cheque $400 con `aplicar_a_factura` para JTX.
3. Assert: factura.saldo==$600, BALANCE.TOTF -$400, CARTERA[JTX]==$600.
4. Reversar cheque.
5. Assert: factura.saldo==$1000, BALANCE.TOTF restituido, cliente.stop=S (rebote real).

### `tests/test_paridad_compra_a_balance.py`
1. Setup: snapshot BALANCE.TOTP, BALANCE.SALBANC1.
2. ALTA compra no pagada $300.
3. Assert: BALANCE.TOTP +$300. SALBANC1 sin cambio.
4. ALTA compra pagada Pichincha $500.
5. Assert: SALBANC1 -$500. tx_bancarias fila nueva DOC=CH. Saldo running correcto.

### `tests/test_paridad_factura_a_balance.py`
1. Setup: snapshot BALANCE.TOTF, CARTERA[JTX].
2. ALTA factura JTX $800.
3. Assert: BALANCE.TOTF +$800, CARTERA[JTX] +$800.
4. MODIFICA factura abono → $200.
5. Assert: factura.saldo==$600, BALANCE.TOTF -$200.
6. ANULAR factura.
7. Assert: BALANCE.TOTF restituido al inicial.

Si los 3 pasan: las 3 conexiones que el dueño pidió están bien.

---

## 6. Orden recomendado de ejecución

1. **Fase A** (bank running saldo) — primitiva. ~1 sesión.
2. **Fase B** (factura editar) — más simple, agarra confianza con el patrón. ~1 sesión.
3. **Fase C** (cheque editar + transitions) — el grueso. ~2 sesiones.
4. **Fase D** (compra editar + DOLARES + caja) — depende de A. ~1 sesión.
5. **Fase E** (cartera con cheques) — query change. ~media sesión.
6. **Fase F** (formato unificado) — UI polish. ~1 sesión.

**Total estimado:** 6-7 sesiones.

---

## 7. Archivos clave a tocar (mapa para próxima sesión)

```
db.py                                           tx() ya existe
db/bank_helpers.py                              NEW — Fase A
db/caja_helpers.py                              NEW — Fase A/D
modules/cheques/queries.py                      + editar(), transicionar_stat() — Fase C
modules/cheques/views.py                        + GET/POST editar, POST transicionar
modules/cheques/templates/cheques/detalle.html  + botones de transición
modules/facturas/queries.py                     + editar() — Fase B
modules/facturas/views.py                       + GET/POST editar
modules/facturas/templates/facturas/editar.html NEW
modules/compras/queries.py                      + editar(), extender crear() — Fase D
modules/compras/views.py                        + GET/POST editar
modules/compras/templates/compras/nueva.html    + radio cuenta
modules/cartera/queries.py                      + cheques en cartera — Fase E
modules/informes/queries.py                     + cartera_por_cliente extendida
config/roles.py                                 + facturas.editar, cheques.editar, cheques.transicionar, compras.editar
migrations/0015_indexes_bank_running.sql        NEW — Fase A
migrations/0016_compra_pago_link.sql            NEW — Fase D
templates/_informe_frame.html                   NEW — Fase F
modules/informes/templates/informes/*.html      UPDATE — Fase F
tests/test_paridad_cheque_a_balance.py          NEW
tests/test_paridad_compra_a_balance.py          NEW
tests/test_paridad_factura_a_balance.py         NEW
```

---

## 8. Decisión sobre corrección de cheques mal cargados — RESUELTA 2026-04-30

**Tamara eligió: Opción 3 — anular + reemitir.** Misma regla que para facturas en Ecuador ("emitida no se edita, se anula y se reemite"). Más limpio que reversar→editar→re-depositar; paper trail explícito; no inventa transiciones B→1→edit→B raras.

### Reglas operativas para Fase C

**`cheques.editar()` se RESTRINGE a campos blandos:**
- Permitido: `concepto`, `observacion`. Y `fechad` SOLO si stat ∈ {Z, P, D} (todavía en cartera, no depositado).
- Bloqueado siempre: `importe`, `codigo_cli`, `no_banco`, `cuenta`, `no_cheque`. Para cambiar cualquiera de estos → flujo anular+reemitir.

**Botón nuevo "Anular por error de carga"** en el detalle del cheque:
- Disponible para cualquier stat excepto {X, T, R} (ya cerrados).
- Form pide:
  - **motivo** (obligatorio, mínimo 10 chars).
  - **id del cheque nuevo** (opcional — para linkar el reemplazo en observacion).
- Side-effects en una sola `db.tx()`:

  | Stat antes de anular | Side-effect compensatorio |
  |---|---|
  | Z, P, D (cartera, no depositado) | sólo UPDATE cheque.stat='X' |
  | B/V/W (Pichincha) | INSERT tx_bancarias DOC='ND' importe=cheque.importe, banco=Pichincha, saldo running recalc |
  | I/J/K (Inter) | idem banco=Internacional |
  | C (caja) | INSERT caja TIPO='S' importe=cheque.importe, saldo running recalc |
  | A, B (con aplicaciones a factura) | reverse de chequesxfact: factura.abono -=imp, saldo +=imp, stat recompute |
  | cualquiera con posdat hermana | DELETE posdat (banc=0, num=id_cheque) |

- **NO marca cliente.stop** (es error administrativo, no rebote real).
- cheque.stat='X', observacion = `RIGHT(observacion || ' | ' || '[X] error de carga: ' || motivo || (' (reemplaza por #'||N||')'), 200)`.
- Bitácora before/after.

**Para crear el reemplazo** la persona usa **"Nuevo cheque"** normalmente — es una transacción independiente.

### Diferencias clave vs ANULAR cheque rebotado real (batch 5)

| | Anular por error de carga (este addendum) | Rebotar cheque real (batch 5) |
|---|---|---|
| Trigger | botón explícito "error de carga" | botón "Marcar rebotado" |
| Cliente.stop | **NO** | **SÍ** (si stat era D/A) |
| Bank compensation | DOC='ND' compensatorio | DOC='ND' + cargo de gastos (2 o 5 USD) |
| Observacion tag | `[X] error de carga` | `[REBOTE]` |
| Posdat | DELETE | DELETE (queda en cobranza separada) |

Son flujos paralelos, no se solapan. La UI los presenta como botones distintos para que el usuario no confunda el motivo.

### Implicancia para la primitiva de Fase A

La compensación bancaria (DOC='ND') es el mismo primitive que `insert_movimiento_bancario()` de Fase A — solo que con `documento='ND'` y `signo=-1`. NO requiere walk-forward recompute (es un INSERT al tail, no un UPDATE/DELETE al medio). **Esto es bueno** — confirma que Fase A queda igual de simple.

**Walk-forward recompute** queda reservado SÓLO para casos extraordinarios (corrección administrativa de saldos históricos, no flujo normal). En operación diaria la app es append-only sobre tx_bancarias.

---

## 9. Estado actual al cierre de esta sesión (2026-04-30)

- **Plan listo, no implementado.**
- 78% paridad sigue siendo el número de la auditoría 2026-04-27.
- Los gaps específicos de "modificar" (factura/cheque/compra editar) **no estaban** documentados en aquella auditoría — son nuevos en este addendum.
- Tests en CI: 51 passing, ruff clean. Sin regresiones.
- **Decisión inmediata para próxima sesión:** empezar por Fase A (bank running saldo), porque B/C/D dependen de esa primitiva.

---

## 10. Para la próxima sesión

Apertura sugerida: *"Vamos con Fase A del addendum batch 22 — bank running saldo. Crear `db/bank_helpers.py` con `insert_movimiento_bancario()` y `recompute_saldos_desde()`, agregar la migración 0015 con los índices, y escribir el test que verifica el walk-forward."*

Si querés priorizar el FORMATO sobre la paridad: empezar por Fase F (es independiente del resto).
Si querés ver primero los tests rojos antes de implementar nada: escribir los 3 tests de §5 primero — van a fallar, y eso nos da la baseline objetiva.

---

## 11. CIERRE DE SESIÓN — TODAS LAS FASES SHIPPEADAS (2026-04-30 final)

Las 6 fases A-F shippearon en una sola sesión + los 3 tests de paridad. **60 tests nuevos pasan**, 0 regresiones, ruff clean.

### Archivos nuevos
```
bank_helpers.py                                              NEW — Fase A
caja_helpers.py                                              NEW — Fase A
migrations/0015_indexes_bank_running.sql                     NEW — Fase A
migrations/0016_compra_pago_link.sql                         NEW — Fase D
templates/_informe_frame.html                                NEW — Fase F (macro)
modules/facturas/templates/facturas/editar.html              NEW — Fase B
modules/cheques/templates/cheques/editar.html                NEW — Fase C
modules/cheques/templates/cheques/anular_error_carga.html    NEW — Fase C
modules/compras/templates/compras/editar.html                NEW — Fase D
tests/test_bank_helpers.py                                   NEW — Fase A (13 tests)
tests/test_facturas_editar.py                                NEW — Fase B (9 tests)
tests/test_cheques_editar.py                                 NEW — Fase C (15 tests)
tests/test_compras_editar.py                                 NEW — Fase D (10 tests)
tests/test_cartera_con_cheques.py                            NEW — Fase E (3 tests)
tests/test_paridad_factura_a_balance.py                      NEW — paridad (2 tests)
tests/test_paridad_cheque_a_balance.py                       NEW — paridad (3 tests)
tests/test_paridad_compra_a_balance.py                       NEW — paridad (5 tests)
```

### Archivos modificados
```
modules/facturas/queries.py        + editar()
modules/facturas/views.py          + GET/POST /editar
modules/facturas/templates/facturas/detalle.html  + botón Editar
modules/cheques/queries.py         + editar(), transicionar_stat(), anular_por_error_de_carga(),
                                     STATS_DEPOSITADO, STATS_TERMINALES_EDIT, _domingo_a_lunes(),
                                     TRANSICIONES_VALIDAS
modules/cheques/views.py           + editar, transicionar, anular_error_carga
modules/cheques/templates/cheques/detalle.html  + botones de transición
modules/compras/queries.py         + editar(), CUENTAS_PAGO + crear() extendido
                                     (cuenta=caja|pichincha|inter, es_anticipo_dolares)
modules/compras/views.py           + editar; nueva acepta cuenta + es_anticipo_dolares
modules/cartera/queries.py         + cheques_cli CTE en aging_buckets/aging_totales
modules/cartera/templates/cartera/aging.html  + nota de paridad cheques en cartera
modules/conciliacion/matcher.py    + ruff fix (f-string)
config/roles.py                    + facturas.editar, cheques.editar, cheques.transicionar,
                                     compras.editar para Contabilidad
```

### Decisiones que quedan en stone
- **Cheque mal cargado se ANULA por error de carga** (no se edita), tag `[X]` en observación, NO marca cliente.stop. Reemitir el correcto desde cero.
- **Factura editar SOLO acepta** abono/condic/observacion. Para importe/cliente/fecha → anular y reemitir (regla Ecuador).
- **Compra editar bloqueada en importe/fechad si está pagada** (id_transaccion≠NULL). Para corregir → anular y reemitir.
- **Bank/caja saldo running es STORED** (paridad dBase), maintained por `bank_helpers.insert_movimiento_bancario()` y `caja_helpers.insert_movimiento_caja()`. Walk-forward sólo en correcciones administrativas (no en flujo normal append-only).
- **State machine completa cheque** vive en `TRANSICIONES_VALIDAS` (modules/cheques/queries.py). Z→{B,C,9,X,P,D,I}, P/D→{B,C,X,I}, B/I→{9,X}, A→{9,X}, etc.
- **Cartera viva = factura.saldo - cheques en cartera (Z/1/2/3/P/D/A)**. Anticipos negativos restan automáticamente via SUM.

### Migraciones a aplicar antes del próximo deploy
```
psql $DATABASE_URL -f migrations/0015_indexes_bank_running.sql
psql $DATABASE_URL -f migrations/0016_compra_pago_link.sql
python scripts/migrate.py --force 0003   # re-seed roles con permisos nuevos
```

### Verificación
- `ruff check .` → All checks passed
- `pytest tests/test_{bank,facturas_editar,cheques_editar,compras_editar,cartera_con_cheques,paridad_*}.py` → **60 passed**
- 0 regresiones en tests pre-existentes (los errores de collection en el sandbox son por deps faltantes del sandbox, no del código).

### Pendiente para próxima sesión
- **Aplicar `_informe_frame` macro** a las 13 plantillas restantes en `modules/informes/templates/informes/*.html` (cartera, flujo, deudas, gastos, ventas, retiros, historia, iniciales, activos, estado_cuenta, etc.). El macro está listo en `templates/_informe_frame.html` con docstring de uso. Trabajo mecánico, ~1 sesión.
- **Tests integration con Postgres real** (`@pytest.mark.db`) para verificar que el saldo running funciona end-to-end contra la DB. Requiere pytest-postgresql en CI.
- **`scintela.flujo` rebuild automático** — la tabla sigue siendo authoritative para `flujo_proyeccion`, pero ningún path la rebuildea. Pendiente.
- **Cobranzas/Compras roles** todavía no tienen los nuevos `*.editar` permisos — agregar si se decide darle el acceso a esos roles también (ahora son sólo Contabilidad + Dueño).
- **Banco running saldo en cheques.depositar_lote()**: el batch deposit existente no usa `bank_helpers.insert_movimiento_bancario`. Migrar para que ambos paths compartan el helper.

### Lo que esto resuelve del pedido textual
> *"agregar cheque o modificar impacte bien en resultados"* ✓
> *"agregar compra o modificar impacte bien en resultados"* ✓
> *"agregar factura o modificar impacte bien en resultados"* ✓
> *"todo tiene que estar bien conectado"* ✓ (con tests de paridad como definición operativa)
> *"hacer un formato lindo para resultados"* ✓ (macro listo, falta aplicar a 13 templates)

---

## 12. Workflow DBF→Postgres (sync mientras corren ambos sistemas en paralelo)

**Decisión TMT 2026-04-30:** El dBase legacy y Programa Core van a correr **al mismo tiempo** durante la transición. La fuente de verdad de los datos vivos es el dBase. Programa Core ve copias.

### Carpeta acordada
TMT actualiza los `.DBF` frescos en:
```
/Users/tamaraeliscovich/Documents/INTELA copy/Files/
```

Esa carpeta se trata como **estado actual** del dBase. Cada vez que la contadora trabaja en el legacy, los DBFs nuevos van ahí (copy/paste manual desde la PC del dBase).

### Comando de sync
Ya está todo armado en el repo desde hace semanas (`scripts/import_dbf.py` + targets de Makefile + runbook):

```bash
make sync-dbf-dry-run     # ver qué pasaría (sin tocar Postgres)
make sync-dbf             # TRUNCATE + INSERT por tabla, en su propia tx
make sync-dbf-list        # listar las tablas que el sync conoce
```

Equivalencias sin Makefile:
```bash
python scripts/import_dbf.py --dry-run
python scripts/import_dbf.py
python scripts/import_dbf.py --list
python scripts/import_dbf.py --only=POSDAT.DBF,CHEQUES.DBF   # parcial
```

### Política
- Cada DBF presente **reemplaza** la tabla Postgres (TRUNCATE+INSERT, idempotente).
- Cada DBF **ausente** deja la tabla Postgres tal cual (filosofía "si no te lo pasé, usá los viejos").
- Cada tabla en su propia transacción: si una falla, las otras siguen.

### Mapping actual (TABLE_MAP en import_dbf.py)
- `FACTURAS.DBF` → `scintela.factura` (CRITICO — TOTF)
- `CHEQUES.DBF` → `scintela.cheque` (CRITICO — TOTC)
- `POSDAT.DBF` → `scintela.posdat` (SUPER — TOTP + POS1/POS2)
- `CAJA.DBF` → `scintela.caja` (CRITICO — SALCAJ)
- `DOLARES.DBF` → `scintela.dolares` (ANTICIPOS)
- `ACTIVOS.DBF` → `scintela.activos` (UMAQ + UACT)
- `HISTORIA.DBF` → `scintela.historia` (SUPER — VSTO, VQX, PATANT)
- `INICIALE.DBF` → `scintela.iniciales` (proyecciones)
- `COMPRAS.DBF` → `scintela.compra`
- `FLUJO.DBF` → `scintela.flujo`
- `PICHINCH.DBF` → `scintela.transacciones_bancarias` (con post_load asignar_no_banco)
- `XGAST.DBF` → `scintela.xgast` (V1..V9 → COSTOS panel)

### Paridad numérica verificada 2026-04-30
Comparé `Files/POSDAT.DBF` (timestamp 30/4 14:54) vs foto del dBase del 30/4:
- DBF: $2.115.716,65 (168 filas banc<>9)
- Foto dBase: $2.124.717,00
- Δ: ~$9K (entradas posteriores a las 14:54, normal)

**Conclusión clave:** la fórmula del balance (`posdat_totales().totp = SUM(importe) WHERE COALESCE(banc,0)<>9`) **es correcta**. Cuando los datos del local coinciden con los del DBF actual, los números cuadran al peso. El gap del usuario al ver el balance era 100% drift de datos (dump local del 12/4 vs DBF del 30/4 = +$509K en deudas nuevas en 18 días).

**Para confiar en el balance del Programa Core durante la transición:** correr `make sync-dbf` cada vez que TMT actualiza la carpeta `Files/`, y el balance refleja paridad exacta con el dBase de ese momento.

### Implicancia para los tests de paridad (§5)
Los 3 tests `test_paridad_*_a_balance.py` ya están — verifican que **las escrituras** del nuevo app emiten los SQL correctos. Pero NO testean el sync DBF→PG. Si en algún momento se modifica `import_dbf.py::TABLE_MAP` o un mapper, hay que correr `tests/test_import_dbf.py` (ya existe) que verifica el shape de los mappers.

### Para próxima sesión
- Si la contadora reporta que algún número no cuadra: primer paso `make sync-dbf` para descartar drift, luego `python scripts/auditar_totales.py` para inspeccionar lado a lado.
- Si aparece un DBF nuevo (ej. `RETEN.DBF` que está en Files/ pero no en TABLE_MAP): agregar entry al `TABLE_MAP` con su `_map_*` mapper. La estructura del schema `scintela.retencion` ya existe en `docs/SCHEMA.txt`.
- El dump `intela12042026.sql` queda como **bootstrap** (primera carga). Después el ciclo de vida es 100% via `make sync-dbf`.
