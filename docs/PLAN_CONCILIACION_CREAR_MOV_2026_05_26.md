# Plan — `+ Crear` rico en conciliación bancaria

**Fecha:** 2026-05-26
**Reporta:** Tamara — "está muy seco el crear, no me da nada".
**Objetivo:** Que crear una tx BANCSIS desde un movimiento real_only del extracto sea útil (no un pasaje en blanco), y que la dueña pueda agrupar comisiones/impuestos en una sola tx con desglose.

---

## Situación actual

Cuando el matcher detecta un mov en el extracto del banco que NO existe en BANCSIS (real_only), aparece la fila con un botón **`+ Crear`**. Ese botón hoy:

1. Toma del extracto: `fecha`, `concepto` (primeros 50 chars), `monto`, `tipo` (C/D).
2. Llama `crear_transaccion_desde_real()` → inserta tx BANCSIS con:
   - `documento` = `'DE'` (si es C) o `'CH'` (si es D), inferido automático.
   - `concepto` heredado tal cual del extracto, truncado.
   - `numreferencia` = parseo del documento.
   - **`prov` = NULL** ← acá está el "está muy seco".
3. Recompute saldos walk-forward.
4. Inserta el match conciliado en la misma db.tx().

**Lo que falta:**
- No queda registrado de qué cliente / proveedor viene la entrada o salida.
- Tampoco se categoriza (no se persiste si fue COMISION / IMPUESTO / COBRO_TRF).
- El concepto truncado a 50 chars muchas veces queda críptico ("PAGO BCO PICHIN…").
- Para comisiones/impuestos del mes (10-30 filas chicas), hay que hacer click 10-30 veces.

---

## Decisiones de Tamara (este turno)

1. **Crear individual:** el modal debe mostrar **cliente / proveedor de dónde proviene**, pre-llenado con lo que ya da el matcher heurístico (ej. si el concepto dice "BED HECTOR" lo levanta como `BED`). Concepto editable también, por si el del banco es críptico.
2. **Modo agrupado:** **una sola tx BANCSIS con la suma total** + desglose en `observaciones`. Concilia todos los real_only N:1 contra esa tx.

No pidió editar tipo de documento BANCSIS ni subcategoría (esos se infieren).

---

## Diseño

### Fase 1 — Crear individual rico

**UI (template `banco_resultado.html`):**

Reemplazar el botón `+ Crear` inline por un botón que abre un **modal compacto** (no inline collapse, no salir de la página) con:

```
┌─ Crear movimiento BANCSIS ─────────────────┐
│  Fecha           [26/05/2026]              │
│  Tipo            ⊙ Crédito (entra)         │
│                  ⊙ Débito (sale)           │
│  Monto           $ 1,234.56                │
│                                            │
│  Cliente / Prov  [BED ▼] HECTOR BEDON      │  ← datalist con sugerido
│  Concepto        [Pago factura #1234]      │  ← editable
│  Documento       [DE]                      │  ← read-only (inferido)
│                                            │
│  Del extracto:   "BED PAGO FACT 1234 ABO…" │  ← gris, contexto
│                                            │
│  [Cancelar]                    [Crear]     │
└────────────────────────────────────────────┘
```

**Pre-llenado del cliente/prov:**
- El matcher ya saca `categoria` y a veces `prov_sugerido` del concepto (`modules/conciliacion/categorizar.py`). Reusar esa lógica para pasarlo al template como `data-prov-sugerido`.
- Si la categoría es ENTRADA_COBRO_* → datalist de clientes (`clientes_para_datalist()`).
- Si la categoría es SALIDA_PROV_* → datalist de proveedores.
- Si es COMISION/IMPUESTO/OTRO → datalist vacío, campo libre (ej. "BANCO PICHINCHA", "SRI").

**Backend (`hub_crear_bancsis`):**
- Aceptar nuevos form fields: `prov`, `concepto_override`.
- Pasarlos a `crear_transaccion_desde_real(prov=..., concepto=...)` (extender la función).
- Validar: `prov` ≤ 10 chars, `concepto` ≤ 50.
- Mantener la respuesta JSON existente (la usa el front AJAX).

**Files a tocar:**
- `modules/conciliacion/matcher_banco.py`: extender `crear_transaccion_desde_real()` con `prov` y `concepto` opcionales.
- `modules/conciliacion/views.py:hub_crear_bancsis`: parsear nuevos form fields.
- `modules/conciliacion/templates/conciliacion/banco_resultado.html`: modal en lugar de botón inline; JS para abrir/cerrar/submit.
- `modules/conciliacion/categorizar.py`: ya tiene la heurística — solo exponer `prov_sugerido` en el dict que va al template.

**Tests:**
- `tests/test_conciliacion_banco_actions.py`: caso "crear con prov" + "crear con concepto custom".

**Estimación:** 1-2 horas.

---

### Fase 2 — Crear agrupado de impuestos/comisiones

**UI:**

En el bloque "Impuestos / Comisiones" del extracto (ya existe la sección, `categorizar.py` arma el grupo COMISION), agregar un botón sticky arriba:

```
┌─ Impuestos / Comisiones (12 filas — $ 234.56) ─────┐
│                                          [Crear 1 mov agrupado]  │
│  26/05  COMISION SERVICIO MENSUAL              $ 5.00          │
│  25/05  IVA COMISION                           $ 0.60          │
│  24/05  RETENCION FUENTE TRF                  $ 12.34          │
│  ...                                                              │
└──────────────────────────────────────────────────────────────────┘
```

**Modal al apretar `[Crear 1 mov agrupado]`:**

```
┌─ Crear movimiento BANCSIS — agrupado ──────────────┐
│  Fecha del mov   [31/05/2026 ▼]                    │  ← último día del extracto por default
│  Banco           PICHINCHA                          │  ← read-only
│  Concepto        [Comisiones e impuestos 01-31/05] │  ← editable
│  Prov            [BANCO PICHINCHA]                  │  ← editable (free text)
│  Monto total     $ 234.56                           │  ← read-only (suma)
│  Documento       NC (nota crédito si suma = débito) │  ← inferido
│                                                     │
│  Desglose (irá a observaciones):                   │
│  ────────────────────────────                       │
│  • 26/05 COMISION SERVICIO MENSUAL    $   5.00     │
│  • 25/05 IVA COMISION                 $   0.60     │
│  • 24/05 RETENCION FUENTE TRF         $  12.34     │
│  • ... (12 filas total)                             │
│                                                     │
│  [✓] Conciliar todas las filas con esta tx (N:1)   │
│                                                     │
│  [Cancelar]                     [Crear y conciliar]│
└─────────────────────────────────────────────────────┘
```

**Backend:**

Nueva ruta `POST /conciliacion/banco/crear-bancsis-agrupado`:
- Form: `no_banco`, `fecha`, `concepto`, `prov`, `desglose[]` (JSON con la lista de real_only originales).
- En una sola `db.tx()`:
  1. `bank_helpers.insert_movimiento_bancario(monto=suma, concepto=..., prov=..., documento='NC'|'NA', numreferencia=NULL)`
  2. Append observación = desglose formateado (líneas `fecha | concepto | monto`).
  3. Para cada `real` del desglose, llamar `confirmar_match(id_transaccion=new_id, metodo='created_from_real_grouped')` → todos quedan apuntando al mismo `id_transaccion` (N:1).
- Recompute saldos walk-forward.

**`bank_helpers.insert_movimiento_bancario`** ya acepta `observacion` (verificar; si no, agregarlo — es columna existente en `transacciones_bancarias`).

**`matcher_banco.confirmar_match`** ya soporta N matches contra el mismo `id_transaccion`; no necesita cambio de schema.

**Files a tocar:**
- `modules/conciliacion/matcher_banco.py`: nueva función `crear_transaccion_agrupada_desde_reals(no_banco, reals[], ...)`.
- `modules/conciliacion/views.py`: nueva ruta `hub_crear_bancsis_agrupado`.
- `modules/conciliacion/templates/conciliacion/banco_resultado.html`: botón sticky en la sección COMISION + modal.
- `tests/test_conciliacion_banco_actions.py`: caso "crear agrupado 5 comisiones → 1 tx BANCSIS + 5 matches".

**Edge cases:**
- Si el grupo tiene C y D mezclados (raro, pero pasa): por default crear con la suma signada; si la suma final es 0, refusar con flash "los movimientos se compensan, revisar".
- Si una fila del grupo ya está conciliada (otro turno): excluirla del subset; mostrar warn.
- Documento auto: `NC` si la suma neta es crédito, `ND` si débito.

**Estimación:** 2-3 horas (es la fase más nueva).

---

### Fase 3 — Polish UX

- Botones con icono + texto descriptivo (en vez de `+ Crear` pelado): **`+ Crear en BANCSIS`** / **`+ Agrupar 12 → 1`**.
- Toast/flash con el monto creado y el saldo nuevo del banco ("Creado #12345 en Pichincha: $1,234.56 — saldo nuevo $X").
- Después de crear, la fila desaparece de real_only con animación (ya está AJAX, mantener).
- Para agrupado: barra de progreso "creando 1 tx + conciliando 12 filas..." si tarda > 500ms.

**Estimación:** 0.5-1 hora.

---

### Fase 4 — Tests + deploy

- `pytest tests/test_conciliacion_banco_actions.py -v` con los nuevos casos.
- Smoke test en Chrome contra prod después del deploy.
- Probar con un extracto que tenga 10+ comisiones para validar el agrupado.

**Estimación:** 30 min.

---

## Total estimado: 4-6 horas

## Orden sugerido

1. **Fase 1 primero** (crear individual con cliente/prov) — es el que ya tira 80% del valor.
2. **Fase 2 después** (agrupado) — la mejora de "no clickear 30 veces".
3. **Fase 3 + 4 al final**.

Las dos fases son independientes — Fase 1 sin Fase 2 sigue siendo útil; Fase 2 sin Fase 1 también.

---

## Riesgos / dudas

- **El sugerido de cliente del matcher es heurístico, puede equivocarse.** Mitigación: el datalist permite tipear cualquier código, no es read-only.
- **Si el sugerido es `BED` pero existe `BEDA` también, hay que elegir.** Datalist resuelve esto (autocomplete).
- **El agrupado mete observaciones largas (hasta 200 chars).** Verificar que `transacciones_bancarias.observacion` es TEXT, no VARCHAR(N). Si es N, ajustar el truncado.
- **¿Qué pasa si la dueña agrupa y después quiere romper un match?** El undo existente (`/banco/romper-match`) sirve, pero solo rompe 1:1. Para N:1 hay que iterar — agregar nota: "Para romper un agrupado, romper cada match individual desde el historial".

---

## Próximo paso

Si Tamara aprueba, arranco con Fase 1.
