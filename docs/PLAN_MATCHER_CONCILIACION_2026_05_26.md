# Plan — Matcher de conciliación bancaria, hecho con cabeza

**Fecha:** 2026-05-26
**Reporta:** Tamara — "podes meterle un poco de cabeza y hacerlo bien, solo sigo corrigiendo tonteras". Cada captura muestra un match cuestionable distinto: doc_banco como Rule #1 pero matchea $-0.49 con $2.00; comisiones individuales aparecen en Coinciden con diff de fecha 2 días y monto totalmente distinto; etc.

**Objetivo:** Reescribir las reglas del matcher con criterios claros, conservadores y trazables. **Cero matches dudosos en "Coinciden"**: si tengo duda, va a "Cruces sugeridos" para que la dueña decida.

---

## Dominio

**Extracto del banco** (Pichincha .xlsx — `MovBanco`):

| Campo | Significado |
|---|---|
| `fecha` | día contable según el banco |
| `documento` | N° de cheque, N° de comprobante de transferencia, o ID del banco para comisiones/impuestos |
| `concepto` | texto descriptivo. Ej "1 ch.LTM", "...EHP-INTELA C-PAG-ANTICIPO", "COMISION SERVICIO MENSUAL" |
| `tipo` | C (crédito = entrada) / D (débito = salida) |
| `monto` | absoluto |

**Programa** (`scintela.transacciones_bancarias` — `MovBancsis`):

| Campo | Significado |
|---|---|
| `fecha` | día contable según el programa |
| `documento` | DE, CH, NC, ND, TR, AC... |
| `concepto` | texto del programa |
| `importe` | absoluto (signo viene de `documento`) |
| `numreferencia` | debería = `doc_banco` del cheque o transferencia |
| `prov` | código cliente/proveedor |
| `chequextransaccion → cheque.no_cheque` | si la tx es DE de cheque, el N° del cheque depositado |

---

## Reglas de match — orden de prioridad

Cada match tiene un código `P0`, `P1`, `P1.5`, etc. que se muestra en la UI. **Solo van a "Coinciden" matches `P0` y `P1`** (los más seguros). El resto va a "Cruces sugeridos" para revisión humana.

### `P0` — doc ID exacto + tipo compatible

**Regla:** `_norm_doc(extracto.documento) ∈ {numreferencia, no_cheque_1, no_cheque_2, ...}` Y `_es_tipo_compatible(extracto.tipo, bancsis.documento)`.

**Sin filtro de fecha ni monto.**

**Por qué:** si la dueña cargó el doc_banco del cheque, ese es el ID universal. Si matchea, es match seguro.

**Excepción:** si el `documento` del extracto es vacío, "0", "00..." → skip P0.

### `P1` — monto EXACTO (±$0.01) + fecha ±3d + tipo compatible

**Regla:** `abs(extracto.monto - bancsis.importe) < 0.01` Y `|fecha_real - fecha_bk| ≤ ventana` Y tipo compatible.

**Si hay >1 candidato, ranking:**
1. Mismo `prov` que el cliente extraído del concepto del banco.
2. Fecha más cercana.
3. Primero por orden estable.

**Sin esto, va a Sugerencias.**

**Por qué:** monto exacto al centavo es la signal más fuerte después de doc ID. Tolerancia $0.01 elimina los matches absurdos como $0.49 vs $2.00.

### `P1.5` — Suma del día N-a-1

**Regla:** para cada real (tipo C) sin match: si hay subset de bancsis (tipo C, mismo día ±2d) cuya **suma sea EXACTA** (±$0.01) al monto del banco → match con N componentes.

**Algoritmo:**
1. Greedy: ordenar bancsis por importe desc, sumar sin pasar target.
2. Si greedy llega exacto → match.
3. Fallback: si la suma de TODOS los bancsis del mismo día == target → match (caso típico: el banco consolida los N cheques en 1 línea).

**Por qué:** patrón típico de Pichincha: la dueña deposita 5 cheques el lunes, el banco trae 1 línea con la suma. PROGRAMA tiene los 5 cheques individuales el lunes-martes.

### `P_GRUPO_COMISIONES` (no es un PASS de matching, es un FLOW separado)

**Comisiones/impuestos (grupo COMISION) NO entran a `Coinciden`.**

**Por qué:** son montos chicos (centavos), 10-30 por día. Matchearlos 1-a-1 contra BANCSIS es ruido. La dueña los crea como 1 NC/ND consolidado por día.

**Flow:**
1. Para cada día con ≥2 movs del grupo COMISION, generar un card pre-armado con suma + concepto default.
2. Antes de proponer crear, chequear si BANCSIS del mismo día ya tiene un NC/ND con monto = neto → marcar `ya_cargado` (no proponer).
3. Si NO está cargado: 1 click crea la tx BANCSIS + concilia las N filas del extracto N:1 contra esa tx.

**Las filas individuales de COMISIONES NO aparecen en el tab "Solo en banco"** (están en el card de agrupado del día).

### Eliminados / suavizados

- ❌ **P2 (cliente + monto exacto, fecha cualquiera)** — confiable solo si el código de cliente extraído es válido. Hoy genera ruido. Mantener pero solo si el código está en catálogo cliente/proveedor.
- ❌ **P3 (monto único exacto, sin filtros)** — riesgo de cruzar cosas no relacionadas (típico: 2 facturas distintas de $100). Solo confirmar si hay un solo candidato Y tipo compatible Y fecha ±15d.
- ❌ **P3.5 (cliente + monto cercano)** — Sacar de auto, ir a Sugerencias.
- ❌ **P4 (grupal N-a-N por suma+tipo)** — solo aceptar si N≤3 (sino es ruido masivo).

---

## Cambios concretos

### Pasada 1: limpieza inmediata (este turno)

1. **`monto_tolerancia=0.01`** default (era 5.0). Ya está aplicado. Verificar deploy.
2. **Excluir grupo COMISION de todos los PASS 1/2/3** — esas filas solo van al flow `P_GRUPO_COMISIONES`. Sin esto, las comisiones individuales aparecen en Coinciden con cruces falsos.
3. **PASS 1 con tolerancia 0.01** — si diff > 0.01, va a Sugerencias, no a Coinciden.

### Pasada 2: agrupado comisiones por día (este turno)

1. Agrupar comisiones por (fecha, grupo='COMISION'). 1 card por día (no un solo bloque).
2. Detectar `ya_cargado` mirando BANCSIS del mismo día con NC/ND y monto = neto.
3. Si `ya_cargado` → tono gris, badge "ya cargado", no proponer crear.
4. Si NO → card amarillo con botón "Crear y conciliar N filas".

### Pasada 3: UI cruces sugeridos (siguiente turno)

1. Mostrar en "Cruces sugeridos" todos los matches probables que NO llegaron a Coinciden.
2. Cada sugerencia: 1 real + lista de 1-3 candidatos del programa, con monto / fecha / diff.
3. Click para confirmar el match elegido.

### Pasada 4: tests + validación (siguiente turno)

1. Tests para cada PASS con casos sintéticos.
2. Smoke contra extracto real → verificar Coinciden ≥ 80% del extracto + ningún match con diff_monto > 0.01.

---

## Por qué este enfoque

**Conservador en "Coinciden":** la dueña confía en lo que el sistema marca como match seguro. Si hay falsos positivos, pierde confianza en toda la herramienta. Mejor "Coinciden 50, Sugerencias 80" con 100% precision que "Coinciden 130, Sugerencias 0" con 50% precision.

**Comisiones son su propio caso:** son chicas, repetitivas, y se cargan al programa como 1 NC consolidado por día. NO tiene sentido matchearlas 1-a-1.

**Doc ID > monto > fecha:** ese es el ranking que pidió la dueña. Hoy lo respetamos en PASS 0 (doc), PASS 1 (monto exacto). Si llegamos a PASS 2+, ya no hay garantía.

---

## Decisiones a confirmar antes de codear

1. **¿OK sacar las filas COMISION individuales del tab "Solo en banco"?** Van a aparecer SOLO en el card de agrupado.
2. **¿Eliminar PASS 3.5 (cliente + monto cercano) o mantener pero solo si va a Sugerencias?** Yo voto eliminar — genera ruido.
3. **¿Ventana fecha default ±3d ok o querés ±5?** Hoy default=3, ±5 si viernes/lunes.

---

## Próximo paso

Si aprobás el enfoque arranco con Pasada 1 + 2 en este turno. Pasada 3 (UI sugerencias) y Pasada 4 (tests) en el próximo.
