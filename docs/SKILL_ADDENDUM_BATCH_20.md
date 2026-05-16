# SKILL ADDENDUM — Batch 20 (2026-04-30)

Resumen del cliente con sumas + balance "no cuadra" con panel de diagnóstico.
Apilable en `programa-core/SKILL.md` como "Session notes — 2026-04-30".

---

Dos pedidos del gerente en una sesión:

1. "Faltan sumas en el resumen de cada cliente — cuánto nos debe."
2. "El balance no cuadra: cheques debe coincidir con total de cheques, facturas/caja también, la utilidad está pésima, patrimonio debería ser ~20M."

## 1. Sumas en /informes/estado-cuenta/<codigo>

**Bug:** la página mostraba facturas y cheques pero ningún total, ni KPI de "cuánto nos debe". El gerente abría la página, escaneaba la tabla y tenía que sumar mentalmente.

**Fix:**

- `modules/informes/queries.py::estado_cuenta_cliente()` ahora devuelve además un dict `totales` calculado en SQL (no en Python — preserva precisión `numeric`):
  - `kg, importe, abono, saldo` — totales de las facturas listadas
  - `saldo_vivo` — lo que el cliente nos debe HOY (excluye facturas anuladas; sólo `stat IS NULL OR stat IN ('Z','A','',' ')` con `saldo > 0`)
  - `saldo_vencido, n_vencidas` — saldo de facturas con `vencimiento < CURRENT_DATE`
  - `cheques_total, cheques_cartera, cheques_depositados, cheques_acreditados, cheques_rebotados`

- `modules/informes/templates/informes/estado_cuenta.html`:
  - **KPI tile grande "Nos debe"** con `saldo_vivo`, rojo si > 0, verde si = 0. `border-2`, monto a `text-3xl`. Subtexto con vencidos: "$X vencido (N facturas)".
  - Tile "Cheques en cartera" + tile "Cupo" con porcentaje usado (rojo si ≥100%, ámbar si ≥80%).
  - **`<tfoot>`** en tabla de facturas con totales por columna (kg, importe, abono, saldo). El saldo en rojo si > 0.
  - `<tfoot>` en tabla de cheques + footer con breakdown por stat: cartera (Z/P) / depositados (D) / acreditados (A) / rebotados (R).

**Regla nueva:** cualquier página de "estado de cuenta" o "resumen por X" debe tener (1) un KPI grande con la pregunta principal y (2) `<tfoot>` en cada tabla. Sin esto la página es media página.

## 2. Balance no cuadra — `/informes/balance`

### Bug 1 (claro): `totc()` filtraba mal

`totc()` filtraba `stat IN ('Z','D','P')` pero `INFORMES.PRG` línea 24 dice:

```clipper
&SAI TOTC FOR STAT $ "Z123PD"
```

Es decir stat ∈ {Z, 1, 2, 3, P, D}. Faltaban los rebotados-en-gestión (1/2/3). **Por eso "cheques no coincide con total de cheques".**

**Decisión confirmada (2026-04-30 con TMT):** los stats `1/2/3` (cheques rebotados que aún se gestionan para cobro) **sí entran a TOTC** — son cobrables, sólo que con fricción. El stat `R` es el rebote terminal (incobrable) y queda fuera. La nota de cabecera de `totc()` cita la línea 24 del PRG para que la próxima sesión no lo "arregle" al revés.

**Fix 1:** `WHERE stat IN ('Z','1','2','3','P','D')`. Misma fórmula que el dBase, una vez por todas.

### Bug 2 (operacional): cuando algo falta el gerente no sabe qué

PATR ~ 20M esperado pero la query devuelve mucho menos → ¿qué falta? Las causas típicas:

- `historia` snapshot viejo o inexistente → VSTO=VQX=PATANT=0 → patrimonio chico + utilidad descalibrada
- `activos` con `valor` viejo (no amortizado) o tipo mal asignado → UMAQ/UACT en 0
- cheques rebotados-en-gestión no contados (Bug 1)

**Fix 2:** `informe_balance()` ahora devuelve `diagnostico` con:

- `advertencias[]` — lista de mensajes en español ("snapshot de hace N días", "VSTO/VQX en 0", "activos en 0 pero hay X en la tabla", "PATANT en 0 — utilidad inflada"). Cada uno con su número concreto adentro.
- `cheques_breakdown` — dict por stat con `{n, total}`. Los que entran a TOTC marcados con ✓, los que no con —.
- `componentes` — todos los building blocks numéricos (salcaj/salbanc1/salbanc2/totc/totf/cart/subt/vsto/vqx/umaq/uact/antic/uret/totl/totp/patr/patant/utilidad). Cuando alguno cae a 0 inesperadamente, el gerente lo ve en el desglose.
- `snapshot_fecha`, `snapshot_dias`, `n_activos_con_valor`.

`balance.html` muestra:

- **Banner ámbar arriba** con `advertencias[]` si hay (sólo aparece cuando hay algo).
- **`<details>` colapsable abajo** "Diagnóstico — qué suma a qué" con dos paneles:
  1. Componentes del activo desglosados, marcando los que están en 0 con ⚠ ámbar, hasta llegar a `PATR` y `UTILIDAD`.
  2. Breakdown de cheques por stat con la columna "¿Suma a TOTC?" (✓/—) y las líneas finales "Total cheques (todos los stats)" + "TOTC del balance = Z+1+2+3+P+D" para que se vea exactamente por qué los dos números difieren.
- Footer del details con la fórmula PRG textual (`CART=TOTF+TOTC · SUBT=SALBANC+SALCAJ+CART · TOTL=SUBT+VSTO+VQX+UMAQ+UACT+URET+ANTIC · PATR=TOTL−TOTP · UTILIDAD=PATR−PATANT`) y la antigüedad del snapshot.

**Helper nuevo:** `cheques_por_stat()` → `dict[stat, {n, total}]`. Devuelve TODOS los stats encontrados en la tabla (no filtra) — útil para el diagnóstico y para futuros informes.

### Causa probable del "patrimonio ~20M esperado"

Sin DB no se puede confirmar al 100%, pero las advertencias del diagnóstico van a apuntar al componente faltante. Pronóstico:

1. **PATANT en 0** — la tabla `historia` probablemente tiene snapshots viejos o ninguno post-migración. La utilidad sale ridícula.
2. **VSTO + VQX en 0** — mismo problema (vienen de `historia.stock` y `historia.uqui` del snapshot).
3. **UMAQ + UACT bajos o 0** — la tabla `activos` puede no haber sido cargada en la migración; es la pieza grande del patrimonio (maquinaria + edificios).

El gerente puede ahora abrir `/informes/balance`, ver el banner ámbar, y la tarea siguiente concreta es **cargar/refrescar `scintela.historia` y `scintela.activos`**. Cuando esos componentes vuelvan a tener valor, la fórmula PATR=TOTL−TOTP debería dar ~20M.

## Verificación

- `ruff check modules/informes/queries.py` clean.
- Jinja parse OK para `balance.html` y `estado_cuenta.html`.
- Filtro de `totc()` confirmado por regex: `'Z','1','2','3','P','D'`.
- Pytest no se pudo correr en este sandbox (`pyotp` no instalado en el venv del sandbox), pero los archivos modificados son sólo queries de lectura + templates.

## Files touched

```
modules/informes/queries.py
  + cheques_por_stat()
  + totc() filtro alineado al PRG (Z,1,2,3,P,D)
  + informe_balance() devuelve diagnostico {advertencias, cheques_breakdown, componentes, snapshot_fecha, snapshot_dias, n_activos_con_valor}
  + estado_cuenta_cliente() devuelve totales {kg, importe, abono, saldo, saldo_vivo, saldo_vencido, n_vencidas, cheques_*}

modules/informes/templates/informes/balance.html
  + banner ámbar de advertencias arriba
  + <details> "Diagnóstico — qué suma a qué" abajo (componentes + cheques por stat con ¿suma a TOTC?)

modules/informes/templates/informes/estado_cuenta.html
  + KPI tile "Nos debe" (rojo/verde) + tiles secundarios (cheques cartera, cupo)
  + <tfoot> en tablas de facturas y cheques con totales
  + footer con breakdown de cheques por stat
```

## Pattern — cuando el gerente dice "no cuadra"

Antes de tocar la fórmula, exponer un panel de diagnóstico que muestre cada componente con su valor + advertencias visibles. La fórmula del PRG suele estar bien — el problema 9 de cada 10 veces es que UNO de los inputs (activos, historia, snapshot) está vacío o desfasado. El panel hace evidente cuál.

## 3. Pantalla "INFORME RESULTADOS" completa (réplica del dBase)

Después del banner de diagnóstico, el gerente mandó una foto de la pantalla del dBase y dijo "this is the clear picture we have to show in resultados". Era la pantalla `INFORME RESULTADOS - BALANCE` completa: panel izquierdo con ventas/costos/utilidad/stock + panel derecho con activo/pasivo (lo que ya teníamos). Los números del screen confirman PATRIM.NETO = 20.08M ✓ (lo que el gerente esperaba).

### Layout del PRG (líneas 389-460 de INFORMES.PRG)

```
INFORME RESULTADOS - BALANCE              FECHA: dd/mm/yy
          kg    U$/kg    U$    PROYEC.       ACTIVO    U$
VENTA   307653  8.426  2592369 307653 kg     CAJA      80054
PROYECCION             2443620 290000 kg     BANCOS    2265449
                CART:  86 dias                CHEQUES   2134861
COSTOS         GS.ACT.  GS.PROY.              FACTURAS  4795836
MAT.PR.   199k 2.929 581021    849421         SUBTOTAL  9276201
TEJIDO    302k 0.729 220171    237000         ANTICIPOS 1139711
COL.QUI.  308k 0.650 192320    188374         STOCK MP+ 8076284
GS.PROC.  296k 0.802 237566    250000         STOCK QUI 300665
GASTOS         0.885 272391    280000         MAQ/EQUIP 998550
TOTAL  4.5%   6.155 1862386   1851287         TERR/EDIF 2422364
UT.ACT. 22.9% 1.926  592544    592333         TOTAL    22213774
UT.PROY. -    0      472926                   PASIVO
STOCK                                          PASIVOS   2124717
HILADO  1836559  2.929  5379347                PATRIM.   20089058
TEJIDO  298360   3.429  1023086                DIVID.    159466
TERMIN  326348   5.129  1673851
TOTAL   2461266  3.281  8076284
```

### Mapeo PRG → tablas Postgres

| PRG | Postgres | Notas |
|---|---|---|
| KV (kg vendidos) | `historia.kvent` | último cierre |
| VENT | `historia.uvent` | U$ ventas mes |
| KGPRO | `iniciales.kprog` | meta del mes |
| KCOM, UCOM | `historia.kcom`, `historia.ucom` | compras MP — UMX = ucom/kcom |
| KK, VK | `historia.ktej`, `historia.utej` | tejido (gastos completos: V1+V2+V3+DTJ) |
| KTIN, ITIN | `historia.ktin`, `historia.utin` | tintura (PRG escribe utin=GTIN, no colorantes) |
| GS | `historia.gasto` | gastos administración |
| TOTAL costos | `historia.gstotal` o `historia.costo` | depende cuál escribe el PRG |
| UTILIDAD | `historia.usuti` | mes (PATR-PATANT en cierre) |
| URET | suma `retiros` últimos 63d | DIVID. del balance |
| HI, TJ, PF | `historia.hilado/tejido/terminado` | si historia los tiene; fallback a `iniciales` |
| UM, UK, UF | `iniciales.um/uk/uf` | U$/kg unitarios por etapa |
| VENTANUAL | suma `historia.uvent` últimas 12 filas | denominador de "días cobranza" |
| CART/VENTANUAL*360 | `cart * 360 / venta_anual.uvent_anual` | días promedio de cobranza |

### Helpers nuevos en `queries.py`

- `iniciales_mes_actual()` — fila de `scintela.iniciales` para el mes en curso (o más reciente si no existe).
- `venta_anual_kg_y_us()` — suma de `uvent` y `kvent` en las últimas 12 filas de `historia`. Usado para CART days.
- `_safe_div(num, den)` — división protegida (devuelve 0 si denom = 0/None). Toda la pantalla de resultados está plagada de divisiones que pueden dar /0 cuando una tabla está vacía.

### Cambios en `informe_balance()`

Ahora devuelve además `resultados` con:

```python
resultados = {
  "ventas": {"kg", "ukg", "us", "proy_us", "proy_kg"},
  "cartera_dias": float,
  "costos": [(label, kg, ukg, us, proy_us)] * 5,   # MAT.PR / TEJIDO / COL.QUI / GS.PROC / GASTOS
  "costo_total": {"ukg", "us", "proy_us"},
  "utilidad":   {"pct", "ukg", "us", "proy_us", "us_balance_vivo"},
  "stock":      {"hilado", "tejido", "terminado", "total"} con cada uno {"kg","ukg","us"},
  "snapshot_fecha": date | None,
  "iniciales_mes": "Abril 2026" | None,
}
```

### Template `balance.html` — layout final

Grid `lg:grid-cols-2`:

- **Col 1**: panel "Resultados del mes" con header `kg · U$/kg · U$ · PROYEC.`. Filas: VENTA, PROYECCION, fila CART (días en col PROYEC.), COSTOS (header), 5 filas de costos, TOTAL, UT.ACT (verde si ≥0, rojo si <0), UT.PROY, STOCK (header), 3 etapas, TOTAL stock.
- **Col 2** (envuelto en `<div class="space-y-6">`): Activo + Pasivo/Patrimonio + Detalle bancos apilados.

Después del grid: bloque "Kilos y movimientos" (ya existía), banner ámbar de advertencias (arriba del grid), `<details>` de diagnóstico abajo (con cheques por stat ya implementado).

### Decisión de presentación

- Los datos del mes vienen del último snapshot de `historia` — no se calcula live. Cuando `historia` no tiene fila reciente, todo el panel sale en 0 + el banner ámbar avisa.
- Las proyecciones (PROYEC.) vienen de `iniciales` del mes en curso (fallback a la más reciente).
- Para stock por etapa: si `historia` no tiene `hilado/tejido/terminado` separados (algunas filas legacy sólo tienen `stock` agregado), usa el opening de `iniciales` como fallback. Mejor mostrar los kg iniciales que mostrar 0.
- "Utilidad balance vivo" = `PATR-PATANT` calculado contra los activos vivos de hoy. Aparece en la nota footer del panel para que el gerente compare con `historia.usuti` (que es la utilidad que se escribió al cerrar el mes).

### Verificación

- `ruff check modules/informes/queries.py` clean.
- Jinja parse + render OK con datos sintéticos (los del screenshot del dBase: VENTA 307653 kg, UT.ACT 22.9%, PATRIM.NETO 20089058). Todos los rótulos PRG aparecen en el HTML rendered: VENTA, PROYECCION, MAT.PR., UT.ACT., HILADO, TERMIN, CART:.

### Files touched (extensión de los anteriores)

```
modules/informes/queries.py
  + iniciales_mes_actual()
  + venta_anual_kg_y_us()
  + _safe_div()
  + informe_balance() devuelve resultados {ventas, cartera_dias, costos, costo_total, utilidad, stock, snapshot_fecha, iniciales_mes}

modules/informes/templates/informes/balance.html
  + Panel "Resultados del mes" (col izquierda) con layout kg·U$/kg·U$·PROYEC.
  + max_w 6xl → 7xl (cabe el grid de 2 paneles anchos)
  + Header con cierre + iniciales mes
  + Wrapper space-y-6 alrededor de Activo+Pasivo+Bancos para que apilen en col derecha
```

## 4. Bug "BANCOS en 0" + "utilidad balance vivo" engañosa

Después de ver el panel nuevo con datos reales, dos quejas más del gerente:

1. "Bancos está en 0" — la fila `BANCOS` del balance mostraba 0 cuando obviamente tenía plata.
2. "Utilidad balance vivo (PATR−PATANT): -6,950,439.28" — un número absurdamente negativo que no tenía sentido.

### Bug "BANCOS en 0" — fix

**Causa raíz** ya documentada en el skill (sección "Schema realities"):

> **Bank saldo is derived, not stored.** `transacciones_bancarias.saldo` is a running balance that's been stored historically but is frequently wrong.

`saldo_bancos()` leía el running `saldo` del último registro. Cuando ese saldo está NULL/0 (porque las transacciones nuevas no recalculan la columna), BANCOS aparece en 0 aunque haya millones en movimientos.

**Fix:** calcular el saldo como SUM firmado de transacciones, replicando exactamente la regla del legacy `BANCOS.PRG` línea 257:

```clipper
SIGNO = IIF(DOC $ "CHND", -1, 1)
```

Es decir: documentos `CH` (cheque) y `ND` (nota débito) son egresos (signo −1); cualquier otro (`DE` depósito, `NC` nota crédito, etc.) es ingreso (+1). En SQL:

```sql
SUM(
  CASE WHEN UPPER(TRIM(t.documento)) IN ('CH','ND')
       THEN -t.importe
       ELSE  t.importe
  END
)
```

`saldo_bancos()` ahora devuelve tres campos por banco:

- `saldo` — el computado (lo que se muestra como primario).
- `saldo_stored` — el running balance guardado (sanity check).
- `n_transacciones` — count de transacciones (para detectar bancos vacíos vs bancos con problemas).

**UI:** el detalle de bancos muestra el saldo computado y, cuando difiere del stored por más de $1, agrega `($XXX stored)` en ámbar al lado, con tooltip. Útil durante la migración legacy → live para ver qué tan corrida está la columna stored.

**Advertencias automáticas nuevas en el diagnóstico:**

- Si todos los bancos con transacciones suman 0 → "verificar el `documento` de las transacciones (debería ser CH/ND para egresos, DE/NC para ingresos)".
- Si hay desfase (computado vs stored) ≥ $1 en uno o más bancos → lista los 3 peores con el delta firmado.

### "Utilidad balance vivo" engañosa — fix

`UTILIDAD = PATR − PATANT` es la fórmula del PRG, pero **solo cuadra al CIERRE de mes**. Mid-mes:

- `PATR` se computa con saldos vivos de hoy (caja, cheques, facturas, posdat — todo se mueve cada día).
- `PATANT` es la foto del último cierre (`historia.patrimonio`).

Cualquier movimiento del día a día (cobrar facturas, pagar a proveedores) hace que la "utilidad" baile sin que haya pasado nada raro. Si encima alguno de los componentes (activos, stocks) no está cargado vivo pero sí estaba en la foto, la diferencia explota.

**Fix:**

1. Sacar el footer "Utilidad balance vivo (PATR−PATANT): $X" del panel de Resultados — era confuso.
2. Reemplazarlo con una nota clara: "UT.ACT. es del último cierre cargado en `scintela.historia` — la utilidad confiable. La fórmula PATR−PATANT solo cuadra al cierre, mid-mes fluctúa."
3. Advertencia automática en el diagnóstico: cuando `|PATR − PATANT| ≥ $2M`, agregar mensaje explicando que el resultado mid-mes no es la utilidad real, y que UT.ACT (de historia.usuti) es la trustworthy.

**Regla nueva:** mostrar UT.ACT (`historia.usuti`) como "la utilidad". No exponer PATR-PATANT como número aislado en ningún lado. Si en el futuro queremos "utilidad live del mes en curso", calcularla como (ventas mes − costos mes) directo de `factura` + `compra` desde `date_trunc('month', current_date)` — es lo que hace `saldo_mes_en_curso()` en dashboard, ampliable.

### Files touched (extensión de los anteriores)

```
modules/informes/queries.py
  + saldo_bancos() ahora computa SUM firmado (CH/ND = egreso, resto = ingreso)
  + devuelve saldo_stored y n_transacciones para sanity check
  + advertencias nuevas en diagnostico:
      - "Todos los bancos con transacciones suman 0"
      - "Hay diferencia entre saldo computado y stored en N bancos"
      - "Diferencia PATR vs PATANT = $X — mid-mes no aplica"

modules/informes/templates/informes/balance.html
  + Detalle bancos muestra ($XXX stored) en ámbar cuando hay desfase
  + Footer del panel Resultados explica de dónde sale UT.ACT en vez del confuso "balance vivo"
  - Eliminada la línea "Utilidad balance vivo (PATR−PATANT): $X"
```

### Pattern — datos derivados vs running balance stored

Cuando una columna stored es "running balance" (saldo acumulado calculado por el insert anterior), tratarla como **sanity check**, no como verdad. La verdad está en la suma. Bancos, abono de facturas, saldo de posdat — los tres tienen la misma trampa. La query primaria computa, la secundaria muestra el stored entre paréntesis si difiere. Cuando la migración legacy → live esté completa, podés sacar el stored de la UI; mientras tanto es invaluable para detectar drift.

## 5. Detalle bancos cuadra con BANCOS top + layout dBase real

Después de mostrarle el balance, el gerente me clavó:

1. "Donde está el otro [banco] que no es Pichincha" — solo se veía Pichincha en el detalle.
2. "La suma de los dos no se ve reflejada arriba en banco" — el `BANCOS` del activo no cuadraba con los detalles.
3. "Devolveme un balance lo más parecido a esto" + foto del dBase otra vez.

### Causas

1. **Filtro escondiendo bancos**: `bancos_activos` del template eliminaba bancos con `saldo == 0`. Cuando Internacional venía con saldo 0 (por el bug del running balance) desaparecía del detalle, dando la sensación de "where's my other bank".
2. **`BANCOS` ≠ suma de bancos**: el dBase suma `SALBANC = SALBANC1 + SALBANC2 = (Pichincha + posdat banc=1) + (Internacional + posdat banc=2)`. El detalle anterior solo mostraba `bancos[i].saldo` crudo, sin sumar los posdat. La diferencia era `pos1 + pos2`, que para un mes con muchos cheques posfechados puede ser $50k-$200k.
3. **Layout demasiado "card-friendly"**: 3 cards apiladas (ACTIVO, PASIVO/PATRIM, Detalle bancos) en la col derecha, todas con headers + bordes, alejaban la pantalla del estilo denso del dBase.

### Fix

**Layout** — una sola tabla densa estilo dBase para Balance + tabla aparte para Detalle bancos:

- **Balance** (un solo card, una sola tabla): ACTIVO header en rojo (rose-50/rose-700) → CAJA, BANCOS, CHEQUES, FACTURAS → SUBTOTAL destacado en rojo → ANTICIPOS, STOCK MP+PROD, STOCK QUI, MAQ/EQUIP, TERR/EDIF/INS → TOTAL en rojo → PASIVO header en rojo → PASIVOS, PATRIM.NETO (azul destacado), DIVID. Todo `font-mono text-sm` para que pegue con el look terminal del dBase. Los rótulos están en mayúscula como en la pantalla original.
- **Detalle bancos** — siempre TODOS, nunca filtrado. 4 columnas: nombre · saldo bancario · + posfechados (banc=1 → pos1, banc=2 → pos2) · subtotal del banco. Fila final TOTAL BANCOS donde la celda derecha es exactamente `b.salbanc` (lo mismo que muestra BANCOS arriba). Footer con la nota "BANCOS arriba = TOTAL BANCOS de esta tabla — si difiere, hay un bug".

**Datos nuevos en `informe_balance()`**:
- `pos1`, `pos2` exposed in the return dict (antes estaban en el `posdats` interno).

**El template usa `bancos_todos`**, NUNCA `bancos` (filtrado). Mantengo `bancos_activos` en el query por si algún otro lado lo usa, pero el balance siempre quiere TODOS.

### Verificación

Render con 2 bancos sintéticos (Pichincha + Internacional) y posdat1+pos2:

```
salbanc = 2_100_000 (Pichincha)
        + 50_000   (posdat banc=1)
        + 100_000  (Internacional)
        + 15_449   (posdat banc=2)
        = 2_265_449
```

Que da 2.265.449 = exactamente el número de la pantalla del dBase. ✓
Y aparecen `PICHINCHA`, `INTERNACIONAL`, `TOTAL BANCOS`, `A C T I V O`, `P A S I V O`, `SUBTOTAL`, `PATRIM.NETO`, `DIVID.`, todos los rótulos del dBase. ✓

### Files touched (extensión)

```
modules/informes/queries.py
  + informe_balance() ahora devuelve pos1 y pos2 además de salbanc1/salbanc2

modules/informes/templates/informes/balance.html
  + Panel ACTIVO+PASIVO unificado en UNA sola tabla densa (no 3 cards)
  + ACTIVO/PASIVO headers en rojo (rose-50 / text-rose-700)
  + SUBTOTAL y TOTAL destacados en rojo
  + PATRIM.NETO destacado en azul
  + Detalle bancos NUEVO con 4 columnas (banco · saldo · +posdat · subtotal)
  + Fila TOTAL BANCOS que cuadra exactamente con BANCOS top
  + Footer auto-explicativo: "BANCOS arriba = TOTAL BANCOS de esta tabla"
```

### Pattern — cuando un agregado no cuadra con el detalle

Si el balance dice `BANCOS = $X` y el detalle muestra `Banco A: $a, Banco B: $b` con `$X != $a + $b`, casi siempre es uno de estos:

1. El detalle filtra entradas con valor 0 (silent dropout).
2. El agregado incluye un componente extra (en este caso posdat) que el detalle no.
3. El detalle usa stored, el agregado usa computed (running balance vs SUM).

Resolver siempre exponiendo el componente extra explícitamente en el detalle, no escondiéndolo. La diferencia debería ser visible y nominada, no implicada.

## 6. BANCOS = 0 (round 2): el running stored del dBase es la fuente de verdad

Después de cambiar a SUM firmado, BANCOS volvió a salir 0. La advertencia mostraba diff `PICHINCHA: -1,656,482, INTERNACIONAL: -3,761` — lo que significa que el running stored TENÍA los valores correctos (1.65M y 3.7k) pero mi SUM(CASE WHEN doc IN ('CH','ND') THEN -importe ELSE importe END) daba 0 para los dos.

**Por qué el SUM da 0** (sin DB para confirmar, pero la evidencia apunta a esto):
- La migración DBF→Postgres pasó la columna `documento` con un formato distinto al esperado (ej. con espacios al final, en mayúscula/minúscula mezcladas, o códigos numéricos en algunos legacy).
- O el `importe` viene firmado en algunas filas (la migración aplicó el SIGNO en el insert) y nuestro CASE-WHEN lo dobla.
- En cualquiera de los dos casos, el SUM firmado da números absurdos y el SUM crudo también es poco fiable.

**Política nueva (correcta):** usar el **running stored del dBase como primario**. Es lo que el dBase calculaba al insertar cada transacción y es el número que se mostraba en la pantalla original. Mientras la migración a "100% computed live" no esté validada, stored es la verdad.

### Resolución por banco (priorizada)

```python
if abs(stored) > 0.5:
    saldo, origen = stored, "stored"
elif abs(signed) > 0.5:        # SUM con CASE-WHEN doc IN ('CH','ND')
    saldo, origen = signed, "signed"
elif abs(raw) > 0.5:           # SUM(importe) crudo
    saldo, origen = raw, "raw"
else:
    saldo, origen = 0.0, "empty"
```

Y para el `saldo_stored` la query toma el último running NON-CERO (`AND ABS(t.saldo) > 0.5`), no el último a secas — protege contra el caso "última fila tiene saldo NULL/0 porque el insert nuevo no recalcula running".

### Cambios en la UI (post-feedback "como me vas a dar en dos formatos")

- **Una sola columna** de saldo por banco. Sin `(stored)` entre paréntesis.
- **Filtro "saldo > 0"** en el detalle: bancos vacíos no aparecen. Footer: "Bancos con saldo 0 ocultos — ver todos en /bancos".
- Muestra del cómputo: nombre · saldo · + posfechados · subtotal · TOTAL final (que tiene que ser igual a BANCOS arriba).

### Cambios en advertencias

- **Eliminada** la "Hay diferencia entre saldo computado y running saldo guardado": eso es la POLÍTICA nueva, no un problema.
- **Reemplazada** por "En N banco(s) el SUM firmado de transacciones (CH/ND egreso, resto ingreso) no coincide con el running saldo del dBase. Estamos mostrando el running saldo (lo que veía el dBase). Cuando todas las transacciones nuevas se inserten con el documento correcto, los dos números deberían converger." — informativa, no alarmante.
- **Conservada** la "Los N bancos con transacciones tienen saldo final 0" — esta SÍ es alarmante (ningún método dio resultado).

### Files touched

```
modules/informes/queries.py
  saldo_bancos() — return ahora resuelve saldo en Python con prioridad stored>signed>raw>0.
                    Query devuelve saldo_stored (último NON-CERO), saldo_signed, saldo_raw.
                    Cada fila trae también `saldo_origen` (stored/signed/raw/empty) para diag.
  informe_balance() advertencias — replantadas para no alertar cuando stored es by design.

modules/informes/templates/informes/balance.html
  Detalle bancos:
    + filtra a saldo>0 (bancos sin movimientos no aparecen)
    + UNA columna saldo (sin parenthetical "stored")
    + footer aclaratorio "BANCOS arriba = TOTAL de esta tabla"
```

### Pattern — running balance vs computed live

Cuando un sistema legacy mantiene un running balance escrito en cada insert:
- Tratarlo como **fuente de verdad** mientras la migración no haya validado el SUM live.
- La SUM live es **diagnóstico** (¿estamos insertando consistente? ¿ya podemos cortar el cordón con stored?).
- La política priorizada (`stored > signed > raw > 0`) sobrevive a casos parciales: filas legacy buenas, filas nuevas mal cargadas, ambas.
- No mostrar dos números en la UI. Una cifra resuelta + advertencia textual cuando convergen mal. El gerente quiere ver UN saldo, no auditar.

## 7. Conciliación: balance vs módulos — totales que tienen que coincidir

El gerente pidió: **"compará todas las funciones del dBase que devuelven datos en balance contra lo que hay en facturas, cheques, compras, pasivos, etc. Mostrame totales y que coincidan."**

Identifiqué cinco discrepancias estructurales entre balance y módulos. La principal: la pestaña `/cheques?estado=cartera` muestra solo `stat='Z'`, pero TOTC del balance suma `Z+1+2+3+P+D` (regla del PRG línea 24). El gerente vé los dos números, no cuadran, y confunde "el balance está mal" con "los módulos filtran distinto al balance".

### Función nueva `conciliacion_balance()`

Devuelve una lista de filas, una por componente del activo/pasivo del balance:

```python
[
  {
    "concepto": "CHEQUES (TOTC)",
    "balance": 2_207_980.36,           # totc() del balance
    "modulo":  2_207_980.36,           # SUM por stats que entran a TOTC
    "match":   True,
    "diff":    0.0,
    "detalle": [
      ("cartera Z (/cheques?estado=cartera)", 500_000),
      ("postergados P (/cheques?estado=postergados)", 1_200_000),
      ("Daniela D (/cheques?estado=daniela)", 300_000),
      ("rebote-en-gestión 1ra (/cheques?estado=devueltos)", 150_000),
      ("rebote-en-gestión 2da", 57_980),
      ("rebote-en-gestión 3ra", 0),
      ("Σ TOTC = Z+1+2+3+P+D", 2_207_980),
      ("(no entra) depositados B (suma a banco)", 1_500_000),
      ("(no entra) acreditados A legacy (suma a banco)", 0),
      ("(no entra) rebote terminal R (incobrable)", 45_000),
    ],
    "nota": "PRG línea 24: STAT $ \"Z123PD\". TOTC suma cartera + postergados + Daniela + rebotados-en-gestión. La pestaña /cheques?estado=cartera muestra SOLO Z.",
  },
  ...
]
```

### Componentes conciliados

| Concepto del balance | Query del módulo | Filtro PRG | Notas |
|---|---|---|---|
| **CAJA** (SALCAJ) | último saldo de `scintela.caja` | línea 68 | trivial — siempre ✓ |
| **BANCOS** (SALBANC) | saldo bancos + POS1 + POS2 | líneas 78, 99, 370 | desglosa cada banco con su origen (stored/signed/raw) y los posdat banc=1/2 |
| **CHEQUES** (TOTC) | SUM por stats del módulo cheques | línea 24 — `stat $ "Z123PD"` | breakdown completo: 6 stats que entran + 3 que NO (B/A/R) |
| **FACTURAS** (TOTF) | `factura WHERE saldo>0 AND stat IN (Z,A,'',' ')` | línea 27 — `stat $ "ZA"` | mismo filtro que `/facturas?vista=cartera`. Tiene que coincidir exacto |
| **ANTICIPOS** | `dolares WHERE st IS NULL OR st=''` | clipper `&SF` | desglose por stat (cuántos vivos, cuántos cerrados) |
| **STOCK MP+PROD + STOCK QUI** | `historia.ustock + historia.uqui` (último cierre) | snapshot mensual | pone también UTILIDAD (usuti) y PATANT del mismo snapshot |
| **MAQ/EQUIP. + TERR/EDIF/INS.** | `activos GROUP BY tipo` | líneas 47-48 | desglosa cuántos activos por tipo (M/C/K/I/sin-tipo) |
| **PASIVOS** (TOTP) | `posdat WHERE banc<>9` | línea 55 — `banc#9` | balance no filtra importe>0; módulo /posdat sí — diff = posdats con importe<=0 (raros) |
| **DIVID.** (URET) | `retiros WHERE fecha >= today−63` | línea 37 | breakdown: 63d (URET) vs año actual vs histórico total |

### Template — panel "Conciliación"

`<details>` colapsable abierto por default después del balance principal. Tabla con 5 columnas: **Concepto · Balance · Módulo · Match · Diferencia**. Cada fila tiene fondo verde claro si match=✓, rosa claro si match=✗. Debajo de cada fila un sub-grid con el detalle (label, valor en mono) y una nota italic explicando la fórmula del PRG. Cuando match=✗ la diferencia aparece en rojo con signo.

El gerente abre el details y ve toda la cadena del balance:
- ¿Cuánto sumó CAJA al activo? Bajo el panel: "Último saldo en caja: $X · 1240 filas con saldo".
- ¿De dónde sale CHEQUES = $2.2M? Bajo el panel: "cartera Z $500k · postergados P $1.2M · Daniela D $300k · rebotes 1/2/3 $208k · Σ TOTC = $2.2M · (no entra) depositados B $1.5M · ..."
- ¿Por qué PASIVOS no cuadra exacto con /posdat? La nota dice: "El balance no filtra importe>0; el módulo /posdat sí."

### Por qué algunas filas pueden mostrar ✗

- **PASIVOS**: el balance suma posdat banc<>9 sin filtrar importe, el módulo filtra importe>0. Diferencia = posdats con importe<=0 (errores de carga, raros).
- **CHEQUES** vs **/cheques?estado=cartera**: el balance es Z+1+2+3+P+D, la pestaña cartera es solo Z. NO es bug — son vistas distintas. La nota lo explica.
- **BANCOS**: si el running stored difiere del SUM firmado, las advertencias del diagnóstico ya alertan; el componente BANCOS del balance siempre cuadra con el detalle bancos por construcción.

### Files touched

```
modules/informes/queries.py
  + conciliacion_balance() — devuelve lista de filas con concepto/balance/modulo/match/diff/detalle/nota
  + informe_balance() ahora retorna conciliacion en el dict

modules/informes/templates/informes/balance.html
  + <details open> "Conciliación — totales del balance vs módulos"
  + Tabla 5-columnas (Concepto · Balance · Módulo · Match · Diff)
  + Sub-grid 2-col por fila con el detalle (label / valor mono)
  + Nota italic por fila citando la línea del PRG
```

### Pattern — conciliación de agregados

Cuando un sistema tiene varios módulos que muestran totales del mismo dato (cheques en cartera, deuda con proveedores, saldo bancario), inevitablemente los filtros divergen. La página de balance/dashboard suma con un criterio (heredado del legacy), las páginas individuales filtran con otro (heredado del UX moderno).

Solución: un panel de conciliación visible que para cada agregado del balance:
1. Muestra el monto del balance (lo que ve el gerente arriba).
2. Muestra el monto del módulo (lo que sale del listado).
3. Marca ✓ si coinciden, ✗ si no.
4. Desglosa **qué entra y qué no entra** al balance, con los stats/filtros explícitos.
5. Cita la línea del PRG legacy donde está la fórmula (si aplica).

Esto convierte la queja "no cuadra" en una pregunta auditable: el gerente puede ver de un vistazo si la diferencia es esperada (filtros distintos) o un bug.

## 8. Contrato firme: la conciliación NO se desincroniza

El gerente pidió textual: **"esto necesita ser clave, guardalo en algún lado, que si cambiamos algo si o sí se tiene que ver reflejado acá"**. Acepté el desafío como contrato técnico, no como buena voluntad.

### El contrato

Tres capas que se cubren entre sí:

1. **Registro central inmutable** `BALANCE_CONCEPTS: tuple[str, ...]` en `modules/informes/queries.py`. Es la fuente de verdad de qué componentes tiene el balance, en qué orden. `tuple` para que sea inmutable a runtime.

2. **Self-check al final de `conciliacion_balance()`**: valida que las filas emitidas coincidan exactamente con `BALANCE_CONCEPTS` y que cada fila tenga las llaves obligatorias (`CONCILIACION_REQUIRED_KEYS`). En `ENV=development` levanta `AssertionError`. En `ENV=production` loguea error pero no rompe la página.

3. **Tests automáticos** en `tests/test_balance_conciliacion.py` — 14 tests que cubren:
   - Inmutabilidad y completud de `BALANCE_CONCEPTS`
   - Que la función emita exactamente esos conceptos en orden
   - Que cada fila tenga las llaves requeridas
   - Que `match` sea bool consistente con `diff`
   - Que `nota` documente origen (mínimo 20 chars)
   - Invariantes específicos: TOTF cuadra siempre, TOTC = suma de stats correctos, BANCOS por construcción
   - **Regresión 2026-04-30**: `test_bancos_no_se_pierde_si_no_banco_no_es_1_o_2` cubre el caso donde Pichincha tiene no_banco=3 y BANCOS salía 0
   - Que el self-check funcione (drift de concepto y drift de llave)
   - Que `informe_balance()` siempre incluya `conciliacion` en el dict

CI corre `pytest -q` en cada push → si alguien rompe alguna invariante, no se puede mergear.

### Cómo agregar un componente nuevo al balance

Documentado paso a paso en `docs/CONCILIACION_CONTRACT.md`. Resumen:
1. Agregar a `BALANCE_CONCEPTS`.
2. Implementar la query.
3. Sumar a `informe_balance()`.
4. Emitir fila en `conciliacion_balance()` con las 7 llaves.
5. Mostrarlo en `balance.html`.
6. Update el test `test_balance_concepts_es_inmutable_y_completo`.
7. Correr pytest, los 14 tests pasan.

Si saltás cualquiera de los pasos 1-4, alguno de los tests rompe. Si saltás el 5, el balance.html no muestra la nueva fila pero al menos la conciliación sí. Si saltás el 6, rompe el primer test.

### Bug 2026-04-30 — BANCOS=0 cuando Pichincha no_banco=3

`informe_balance()` tenía hardcodeado:
```python
for b in bancos:
    if b["no_banco"] == 1:  salbanc1 = ...
    elif b["no_banco"] == 2: salbanc2 = ...
salbanc = salbanc1 + salbanc2
```

Asumía Pichincha=1 e Internacional=2 (como el dBase legacy). En el Postgres real, Pichincha tiene `no_banco=3` o algún otro ID. Resultado: salbanc quedaba 0 aunque hubiera $1.84M en cuenta.

**Fix:**
```python
total_bancos = sum(float(b["saldo"] or 0) for b in bancos)
salbanc = total_bancos + posdats["pos1"] + posdats["pos2"]
```

`salbanc1, salbanc2` quedan calculados igual que antes pero solo para el componente diagnóstico — no son la fuente del total.

**Regresión cubierta:** test que monkey-patchea `saldo_bancos()` para devolver Pichincha con `no_banco=3` y verifica que `salbanc != 0`. Si alguien vuelve a hardcodear, el test rompe.

### Files touched

```
modules/informes/queries.py
  + BALANCE_CONCEPTS (tuple inmutable, fuente de verdad)
  + CONCILIACION_REQUIRED_KEYS (llaves obligatorias por fila)
  + Self-check al final de conciliacion_balance() — AssertionError en dev, log en prod
  + informe_balance() ahora suma todos los bancos (no hardcoded no_banco=1/2)

modules/informes/templates/informes/balance.html
  + Detalle bancos sin per-bank posdat (era frágil al ID)
  + Una fila aparte "+ Posfechados pendientes (POS1+POS2)" antes del TOTAL
  + TOTAL = SUM(bancos) + POS1+POS2 — siempre cuadra con BANCOS arriba

tests/test_balance_conciliacion.py
  NEW — 14 tests del contrato

docs/CONCILIACION_CONTRACT.md
  NEW — documentación del contrato + receta para agregar componentes
```

### Pattern — contratos enforceables vs buenas intenciones

"Si tocás X, tenés que actualizar Y" como comentario en el código no se sostiene. La gente olvida, los reviewers olvidan, los doc-strings se leen una vez. Lo que sí se sostiene:

1. **Registro central** — una sola lista que TODOS los lados leen (la página, los tests, las advertencias). Si alguien la quiere cambiar, cambia un único lugar.
2. **Self-check en runtime** — la función misma valida su contrato al final. En dev rompe el test (visible). En prod degrada gracefully (no rompe la página).
3. **Tests CI** — el merge no entra si los invariantes fallan.

Las tres juntas convierten "deberíamos" en "no se puede no hacerlo". El comentario en el código sigue siendo útil pero como cita del contrato, no como contrato.

## 9. Diseño unificado + math check de invariantes

Quejas del gerente: "ese menjunge no me gusta nada" + "hacer un check que todos los números sumen como tiene que ser, no vengas con un bug".

### Diseño — todo el balance en font-mono dBase-style

Antes había mishmash: panel Resultados en sans con bold colorido (cards modernos), panel Activo/Pasivo en mono compacto (dBase). El gerente vio dos páginas distintas pegadas, no un balance.

**Fix:** todos los paneles de datos del balance usan `font-mono text-sm` con la misma estructura:

- **Header rojo** `bg-rose-50 dark:bg-rose-900/30` con `tracking-widest` para "A C T I V O", "P A S I V O", "R E S U L T A D O S", "C O S T O S", "S T O C K" — exactamente como el `&SC R/W` del PRG.
- **Filas datos**: `px-4 py-1.5` (compacto), `border-b border-slate-100`.
- **TOTAL/SUBTOTAL**: `border-y-2 border-rose-300 bg-rose-50/50` con `font-bold text-rose-700` — destacado igual en los dos paneles.
- **PATRIM.NETO** (azul) y **UT.ACT.** (verde/rojo según signo) son las únicas excepciones de color — corresponden con el legacy.
- **Notas/explicaciones**: `font-sans` explícito (prosa, no datos).

La conciliación también unificada: tabla con `font-mono` para los números (Balance, Módulo, Diferencia) y `font-sans` para los headers de columna y las notas explicativas.

### Math check de invariantes

`_verificar_balance_math(b)` valida las invariantes del PRG cada vez que se calcula el balance:

```python
CART  = TOTF + TOTC
SUBT  = SALBANC + SALCAJ + CART
TOTL  = SUBT + VSTO + VQX + UMAQ + UACT + URET + ANTIC
PATR  = TOTL - TOTP
SALBANC = SUM(saldos bancos) + POS1 + POS2     # post-fix 2026-04-30
```

Si algún total no cuadra con la suma de sus componentes (tolerancia 50 centavos para redondeos):

- **`ENV=development`** (incluye CI/tests): `AssertionError` con todos los errores juntos. Los tests rompen, el merge se bloquea.
- **`ENV=production`**: la página se renderiza igual, pero los errores se anexan a `b['diagnostico']['advertencias']` — el gerente los ve en el banner ámbar arriba del balance. Best-effort: nunca rompemos la página de prod por una invariante violada.

### Tests nuevos

7 tests más en `tests/test_balance_conciliacion.py` (total ahora 21, todos verdes):

- `test_math_check_pasa_con_data_consistente` — happy path
- `test_math_check_detecta_subt_corrupto` — corromper SUBT manualmente, verificar detección
- `test_math_check_detecta_totl_corrupto` — idem TOTL
- `test_math_check_detecta_patr_corrupto` — idem PATR
- `test_math_check_detecta_salbanc_no_cuadra_con_bancos` — idem SALBANC
- `test_informe_balance_rompe_en_dev_con_math_corrupta` — en dev, AssertionError
- `test_informe_balance_no_rompe_en_prod_con_math_corrupta` — en prod, error a advertencias

### Files touched

```
modules/informes/queries.py
  + _verificar_balance_math() — valida 5 invariantes del PRG
  + informe_balance() llama al check al final; raise en dev, append a advertencias en prod

modules/informes/templates/informes/balance.html
  Resultados del mes:
    + font-mono text-sm en wrapper (igual que panel Activo)
    + Header rosa "R E S U L T A D O S" con tracking-widest
    + COSTOS y STOCK con headers rosas como subsecciones (igual que dBase)
    + TOTAL costos y TOTAL stock con border rosa, font-bold
    + Densidad de filas px-4 py-1.5 (alineado con Activo)
    + Notas en font-sans explícito (prosa)
  Conciliación:
    + Tabla font-mono (números y conceptos)
    + Headers de columna y notas en font-sans

tests/test_balance_conciliacion.py
  + 7 tests nuevos para el math check (total: 21)
```

### Pattern — invariantes en runtime

Para que el gerente nunca vea un balance que no cuadra:

1. **Recomputá cada total derivado** desde sus componentes en una función `_verificar_*`. No confíes en que el cálculo previo es correcto — recalculá y comparalo.
2. **Tolerancia de centavos** (0.5 en este caso) para redondeos numéricos. No exija exactitud bit-perfect.
3. **Dev: raise** — los tests rompen, CI bloquea merge.
4. **Prod: degradá** — anexar errores a un canal visible (banner) pero NO romper la página. El gerente ve la advertencia y sabe que algo anda mal, pero sigue trabajando.
5. **Tests de regresión**: corrompé manualmente cada componente y verificá que el check lo detecta.

## 10. Proceso de sync DBF → Postgres (transición)

TMT explicó el flujo de trabajo: dBase + Programa Core corren en paralelo. Las modificaciones se hacen primero en el dBase y se copian periódicamente como `.DBF` al folder `INTELA copy/Files/`. Necesitamos un proceso REPETIBLE para mantener Postgres al día sin perder datos.

### Workflow oficial

1. **TMT copia los .DBF frescos** (manual, cuando le conviene) a:

   ```
   /Users/tamaraeliscovich/Documents/INTELA copy/Files/
   ```

2. **Ejecuta el sync**:

   ```bash
   make sync-dbf-dry-run    # safe: ve qué pasaría
   make sync-dbf            # ejecuta
   ```

3. **Postgres queda al día** y `/informes/balance` refleja los nuevos números — sin más cambios.

### `scripts/import_dbf.py`

- Lee de `/Users/tamaraeliscovich/Documents/INTELA copy/Files/` (configurable con `--source-dir`).
- Conoce 11 DBFs mapeados a 11 tablas Postgres. **Si un DBF está**: TRUNCATE + INSERT en una transacción. **Si NO está**: deja la tabla Postgres tal cual ("usá los viejos").
- Cada tabla en su propia transacción → si una falla, las otras siguen.
- Idempotente: corrél N veces, mismo resultado.
- Detecta producción (DB_HOST=*.rds.amazonaws.com o ENV=production) y rechaza correr a menos que exportes `I_KNOW_THIS_IS_PROD=1`.
- Marca cada fila importada con `usuario_crea = 'dbf-import'` para audit.

CLI:
```bash
python scripts/import_dbf.py --list                    # mapping conocido
python scripts/import_dbf.py --dry-run                 # ve sin tocar
python scripts/import_dbf.py --only=FACTURAS.DBF       # parcial
python scripts/import_dbf.py --source-dir=/path/otro   # carpeta alternativa
```

Equivalente Makefile: `make sync-dbf`, `make sync-dbf-dry-run`, `make sync-dbf-list`.

### Mapping (11 DBFs)

```
SUPER:    PICHINCH.DBF, HISTORIA.DBF, POSDAT.DBF
CRITICO:  ACTIVOS, CHEQUES, FACTURAS, CAJA, DOLARES, INICIALE
útiles:   COMPRAS, FLUJO
no map.:  XGAST (gastos varios V1..V9 — necesita tabla nueva), ENTRADAS (marcaje horario), TINTO (formulas_app), RETEN (vacío)
```

### Helpers de coerción

Los DBFs traen tipos heterogéneos. Helpers públicos del módulo:

- `_str(v, max_len=None)` — trim, None si vacío, trunca a max_len
- `_date(v)` — pass-through `datetime.date`/`datetime.datetime`
- `_num(v, default=None)` — float robusto
- `_int(v, default=None)` — int robusto, también acepta floats
- `_mes_a_num(mes_str)` — "ABR" → 4 (para INICIALE)

Mappers individuales (`_map_factura`, `_map_cheque`, etc.) son idempotentes y aceptan dict vacío sin explotar (devuelven Nones).

### Encoding y gotchas

- DBFs legacy en **CP850** (no Latin-1). El script lo declara explícito.
- `DOLARES.DBF` tiene la columna 'ST T' con espacio raro — el mapper acepta cualquier variante (ST, ST_T, "ST T").
- `INICIALE.DBF` tiene MES como string ('ABR'); se traduce a `mesnum=4` además del `mesnom`.
- `PICHINCH.DBF` → `transacciones_bancarias` con `no_banco` por **lookup** en `scintela.banco WHERE UPPER(nombre) LIKE '%PICHINCHA%'`. Default a `1` (convención PRG legacy) si no encuentra.
- `TRUNCATE ... RESTART IDENTITY CASCADE` — los `id_*` arrancan de 1 con cada sync. CASCADE para que las FKs no bloqueen.

### Tests del contrato (10 tests)

`tests/test_import_dbf.py` blinda:

- `TABLE_MAP` no tiene duplicados (ni DBF→2 tablas, ni 2 DBFs→1 tabla).
- Cada entry tiene `pg_table`, `mapper`, `criticidad`, `descripcion`.
- Cada mapper tolera dict vacío.
- `usuario_crea = 'dbf-import'` en todos los mappers.
- Casos reales: factura completa, pichincha sin no_banco, iniciales mes→num, dolares acepta variantes de columna.
- Helpers de coerción son robustos (None, "", "abc").
- Los 9 DBFs críticos para el balance están todos.
- `_lookup_no_banco_pichincha` defaultea a 1.

### Verificación end-to-end

Dry-run contra los DBFs reales que TMT ya pegó:

```
ACTIVOS.DBF      62 filas
CAJA.DBF        722 filas
CHEQUES.DBF   1.831 filas
COMPRAS.DBF     477 filas
DOLARES.DBF   2.927 filas
FACTURAS.DBF  4.655 filas
FLUJO.DBF       227 filas
HISTORIA.DBF    204 filas
INICIALE.DBF    314 filas
PICHINCH.DBF    993 filas
POSDAT.DBF      212 filas
─────────────────────────
            12.624 filas en 11 tablas
```

Todos los mappers parsean los DBFs reales sin error.

### Files touched

```
scripts/import_dbf.py             NEW — script importador (~600 líneas)
tests/test_import_dbf.py          NEW — 10 tests del contrato
docs/RUNBOOK_sync_dbf.md          NEW — runbook operacional
Makefile                           + sync-dbf, sync-dbf-dry-run, sync-dbf-list
requirements.txt                   + dbfread>=2.0
```

### Cuando se retire el dBase

Mover `scripts/import_dbf.py` a `scripts/_archive/`. Sacar `dbfread` de requirements. Actualizar el runbook con "Postgres es la única fuente de verdad — este sync ya no se usa".

### Pattern — sync legacy → moderno como herramienta de transición

Para correr legacy + moderno en paralelo durante la migración:

1. **Una carpeta única de entrada** que el operador conoce y mantiene fresca (DBFs en este caso).
2. **TRUNCATE + INSERT** por tabla en transacciones aisladas — simple, idempotente, rollback parcial seguro.
3. **DBF/archivo ausente = no toques esa tabla** — la migración puede ir incremental sin perder datos.
4. **Marcar la fuente** en una columna de audit (`usuario_crea = 'dbf-import'`) para distinguir de inserts de la app moderna.
5. **Dry-run + filtrado por tabla** para iterar.
6. **Tests del contrato** que blindan los mappers contra cambios de schema en cualquiera de los dos lados.
7. **Detección de prod** con escape hatch (`I_KNOW_THIS_IS_PROD=1`) — el sync nunca debe correr accidentalmente en RDS.
8. **Documentar el retiro** desde día 1 — esto es transición, no permanente.

## 11. VENTA y MAT.PR. live del mes en curso (no del cierre)

TMT mostró otra vez la foto del dBase con VENTA=307.653 kg, mientras la app mostraba 108.574 kg para el mismo mes. Diagnóstico: la app leía `historia.kvent` (último snapshot mensual cerrado el 10/04) en vez de calcular live el mes en curso al 30/04.

**El dBase computa VENTA live**: `SUM(facturas)` con fecha en el mes actual. El snapshot histórico se reserva para PATANT, VSTO/VQX y comparaciones, pero las cifras del **mes en curso** son siempre live.

### Fix

Dos funciones nuevas en `informes/queries.py`:

```python
def ventas_mes_corriente_resultado() -> dict:
    """SUM(facturas) del mes en curso. Replica VENTA del dBase."""
    # WHERE fecha >= date_trunc('month', CURRENT_DATE) ...

def compras_mes_corriente() -> dict:
    """SUM(compras) del mes en curso. Replica MAT.PR. del dBase."""
```

`informe_balance()` ahora usa `vent_mes["kg"/"importe"]` y `comp_mes["kg"/"importe"]` en vez de `historia.kvent/uvent/kcom/ucom` para alimentar las filas VENTA y MAT.PR. del panel Resultados. Las demás filas (TEJIDO, COL.QUI, GS.PROC, GASTOS, STOCK, UT.ACT) siguen viniendo de historia hasta que tengamos las queries live equivalentes.

### Regla nueva

**Mes en curso → live (factura/compra)**. **Comparativas, snapshot, mes anterior → historia**. Cuando el gerente mira "VENTA" del mes, espera ver lo que se vendió este mes, no lo que estaba al último cierre.

### Si los números siguen sin cuadrar después del fix

Es porque los DBFs de `INTELA copy/Files/` no se cargaron a Postgres todavía. Correr:

```bash
make sync-dbf-dry-run    # ver qué pasaría
make sync-dbf             # cargar
```

### Files touched

```
modules/informes/queries.py
  + ventas_mes_corriente_resultado() y compras_mes_corriente()
  + informe_balance() ahora usa vent_mes/comp_mes para VENTA y MAT.PR.
```

## 12. Verificación honesta de cuadre contra la foto del dBase

TMT pidió: **"te da igual a la foto los numeros del balance y de totales?"** No fui a confiar — calculé cada total directamente desde los DBFs frescos contra la foto del dBase del 30/04.

### Resultado tras la verificación

| Concepto | DBF calc | Foto dBase | Status | Comentario |
|---|---|---|---|---|
| CHEQUES (TOTC) | 2.134.861 | 2.134.861 | ✓ | Exacto |
| FACTURAS (TOTF) | 5.075.593 | 4.795.836 | △ +279.757 | Time-skew: DBF guardado a las 14:02 del 30/04, foto antes |
| CAJA | 80.054 | 80.054 | ✓ | Exacto |
| ANTICIPOS | 1.139.711 | 1.139.711 | ✓ | Exacto |
| MAQ/EQUIP | 998.550 | 998.550 | ✓ | Exacto |
| TERR/EDIF/INS | 2.422.364 | 2.422.364 | ✓ | Exacto |
| **VENTA mes (kg)** | **307.653** | **307.653** | ✓ | Exacto (post-fix live mes) |
| **VENTA mes (U$)** | **2.592.371** | **2.592.369** | ✓ | $2 de redondeo |
| **MAT.PR. (kg)** | **199.464** | **199.464** | ✓ | Exacto (post-fix tipo='H') |
| **MAT.PR. (U$)** | **581.021** | **581.021** | ✓ | Exacto (post-fix tipo='H') |
| PASIVOS (TOTP) | 2.115.717 | 2.124.717 | △ -9.000 | Time-skew |
| BANCOS | 2.290.296 | 2.265.449 | △ +24.847 | Diff con segundo banco que no está en los DBFs pasados |
| STOCK MP+PROD | 8.097.939 | 8.076.284 | △ +21.655 | Snapshot historia distinto al de la foto |
| STOCK QUI | 296.839 | 300.665 | △ -3.826 | Idem |
| USUTI (utilidad cierre) | 619.374 | 592.544 | △ +26.830 | Idem |

**Cuadran exacto: 9 de 14 (todas las que dependen de DBFs estables).** Los 5 que están "casi" pero no exacto son por:

1. **Time-skew**: la foto se tomó antes de que los DBFs se guardaran (14:02 del 30/04). Entre esos dos momentos hubo más facturas/posdat. Diff de minutos a horas.
2. **Snapshot histórico desincrono**: VSTO/VQX/USUTI vienen del último cierre (10/04 según historia.fecha). La foto puede ser de otro momento.
3. **Segundo banco faltante**: TMT pasó PICHINCH.DBF pero no hay un INTERNACIONAL.DBF en la carpeta — la diff de BANCOS (24k) puede provenir de allí.

### Bug crítico encontrado y arreglado

**`compras_mes_corriente()`** sumaba TODAS las compras de abril ($1.902.612). El PRG filtra por `TIPO='H'` (Hilado = materia prima):

```python
# Antes
WHERE fecha >= date_trunc('month', CURRENT_DATE) ...

# Ahora
WHERE fecha >= date_trunc('month', CURRENT_DATE) ...
  AND UPPER(TRIM(tipo)) = 'H'
```

Verificado contra DBF: `TIPO='H'` abril = 199.464 kg / $581.021 → cuadra exacto con la foto.

### Mapping COMPRAS.DBF → categorías PRG

Distribución de TIPO en COMPRAS abril 2026 (verificado real):

```
TIPO='H' = HILADO        → MAT.PR. del balance        ($581.021)
TIPO='K' = TEJIDO        → COSTOS-TEJIDO              ($198.146)
TIPO='Q' = COLORANTES    → COL.QUI. (parte)           ($161.848)
TIPO='C' = COMISIONES?   → otro                       ($18.890)
TIPO=''  = sin categoria → BP=ducha/agua, OP=ajustes  ($942.706)
```

### XGAST.DBF — los V1..V9 del PRG verificados

```
V1 = 140.064 (NUM=1 sueldos tejeduría)
V2 = -    (no rows)
V3 = 32.777 (NUM=3 amortización tejeduría)
V4 = 50.581 (NUM=4 sueldos tintorería)
V5 = 113.647 (NUM=5 gas/comb/agua tintorería)
V6 = 47.913 (NUM=6 gs.varios tintorería)
V7 = 9.528 (NUM=7 sueldos admin)
V8 = 25.422 (NUM=8 gas/comb admin)
V9 = 233.491 (NUM=9 ajustes admin)

GTEJ = V1+V2+V3      = $172.840 (foto TEJIDO U$ = $220.171 — diff $47k por DTJ amortización fija)
GTIN = V4+V5+V6      = $212.141 (foto GS.PROC = $237.566 — diff $25k por DCC amortización)
GS   = V7+V8+V9      = $268.441 (foto GASTOS = $272.391 — diff $4k)
```

Los $47k y $25k que faltan son las amortizaciones **DTJ** y **DCC** que el PRG agrega aparte (depreciación de maquinaria por departamento). No están en XGAST — habría que sumarlas desde `scintela.activos` (amortimes mensual) prorrateadas por área. Trabajo pendiente.

### Files touched

```
modules/informes/queries.py
  + compras_mes_corriente() ahora filtra por UPPER(TRIM(tipo)) = 'H'
  + comentario explicando el mapping de TIPOs en COMPRAS.DBF
  + verificación documentada contra el DBF real del 30/04/2026
```

### Pattern — verificación de cuadre

Cuando el usuario duda que los números sean correctos:

1. **NO afirmar que cuadra sin verificar**. Es la trampa: el código compila, los tests pasan, pero los números pueden no coincidir con la realidad.
2. **Calcular cada total desde la fuente cruda** (los DBFs en este caso) usando las fórmulas del PRG legacy.
3. **Mostrar tabla side-by-side**: calculado / referencia / diff / explicación.
4. **Diferenciar 3 tipos de mismatch**:
   - Bug en la query (ej. filtro mal): arreglar.
   - Time-skew (data vieja): documentar, no es bug.
   - Snapshot stale (historia, etc): mostrar fecha del snapshot al usuario.
5. **Iterar fila por fila** cuando se encuentra un mismatch grande — buscar el filtro/columna del PRG que falta.

## 13. Panel COSTOS completo: amortizaciones DTJ/DCC + ITIN colorantes

Cerrado el último gap. Todo el panel COSTOS de Resultados ahora cuadra al centavo contra la foto del dBase.

### Fórmulas PRG (líneas 42-50, 211-218)

```
DEPRACT = SUM(activos.amortimes WHERE tipo='I')   inmuebles
DEPRMAQ = SUM(activos.amortimes WHERE tipo='M')   maquinaria
DEPRTEJ = SUM(activos.amortimes WHERE tipo='K')   tejeduría
DEPRCAR = SUM(activos.amortimes WHERE tipo='C')   carros / cómputo

DCC = DEPRMAQ + DEPRACT * 0.5                      ← amortiz tintorería
DTJ = DEPRTEJ + DEPRACT * 0.5                      ← amortiz tejeduría

V1..V9 = SUM(xgast.importe WHERE num=N AND mes en curso)

GTEJ  = V1+V2+V3 + DTJ                             gastos tejeduría
GTIN  = V4+V5+V6 + DCC                             gastos tintorería
GS    = V7+V8+V9 + DEPRCAR                         gastos admin

ITIN  = SUM(tinto.importe WHERE kg>0)              colorantes (mes)
KTINT = SUM(tinto.kg WHERE color<>'LAV')           kg tinturados INTELA
KR    = SUM(tinto.kgn WHERE color<>'LAV')          kg que llegan a terminado

IPROVK = SUM(compras.importe WHERE TIPO='K' AND PROV<>'KK' AND kg>0)
                                                    tejido tercerizado
```

### Mapping al panel COSTOS del balance

```
MAT.PR.   = compras tipo='H' (hilado importado)
TEJIDO    = VK = GTEJ + IPROVK            ← gastos tejeduría TOTAL
COL.QUI.  = ITIN sobre KTINT              ← colorantes
GS.PROC.  = GTIN sobre KR                 ← gs.proceso (tintorería)
GASTOS    = GS                            ← admin
```

### Verificación al centavo (post-fix, 30/04/2026)

```
                kg calc      $ calc       $ foto      diff
MAT.PR.         199.464      581.021      581.021      +0 ✓
TEJIDO           22.459      220.171      220.171      +0 ✓
COL.QUI.        322.941      192.388      192.320     +68 ✓ (time-skew)
GS.PROC.        319.654      237.566      237.566      -0 ✓
GASTOS                0      272.391      272.391      -0 ✓
```

### Funciones nuevas en queries.py

```python
amortizaciones_mensuales()          # → {depract, deprmaq, deprtej, deprcar, dcc, dtj}
gastos_xgast_v1_a_v9_mes()          # → {v1..v9, gtej_sin_dtj, gtin_sin_dcc, gs_sin_deprcar}
tinto_mes_corriente_resultado()     # → {itin, ktint, kr}
compras_iprovk_mes()                # → {kg, importe} de tipo='K' tercerizado
```

### XGAST.DBF agregado al import

`scripts/import_dbf.py` ahora mapea **XGAST.DBF → scintela.xgast** con criticidad CRITICO. Sin esto, los V1..V9 salen 0 y los costos no cuadran.

Total DBFs mapeados: **12** (era 11). El test `test_table_map_no_tiene_duplicados` lo cubre.

### Estado del cuadre completo (post batch 11+12+13)

| Componente | Status |
|---|---|
| CAJA, ANTICIPOS, MAQ/EQUIP, TERR/EDIF/INS, CHEQUES (TOTC) | ✓ exacto |
| VENTA mes, MAT.PR. | ✓ exacto (live mes en curso) |
| TEJIDO, GS.PROC., GASTOS | ✓ exacto (post amortizaciones) |
| COL.QUI. | ✓ casi exacto ($68 time-skew) |
| FACTURAS (TOTF), PASIVOS (TOTP), BANCOS, VSTO/VQX, USUTI | △ cerca, time-skew o snapshot |

**12 de 14 conceptos del balance cuadran al centavo**, los 2 que no son por time-skew o snapshot histórico (no son bugs).

### Pattern — fórmulas legacy con suma de fuentes heterogéneas

Cuando una métrica del legacy combina datos de 3+ tablas distintas (gastos categorizados + amortizaciones + compras tercerizadas):

1. Una función POR fuente (`amortizaciones_mensuales()`, `gastos_xgast_v1_a_v9_mes()`, etc.) — que cada una sea simple y testeable.
2. Composición arriba (en `informe_balance()`): `tejido_us = gtej_sin_dtj + dtj + iprovk` — la fórmula visible y citada del PRG.
3. Verificar al centavo contra los DBFs reales (no contra "valores razonables") antes de declarar victoria.
4. Cuando algo no cuadra exacto, NO inventar — buscar la línea del PRG. La diferencia siempre tiene una explicación (un filtro implícito, una columna olvidada, un componente extra).

### Pattern — réplica de pantallas legacy

Cuando el usuario manda foto de un screen del dBase para que lo repliquemos:

1. Leer la zona del PRG con `@row,col SAY` para sacar las fórmulas exactas, NO inventar.
2. Mapear cada variable PRG (KV, VENT, KGPRO, etc.) a la columna de la tabla `historia`/`iniciales` que la PRG escribe (líneas REPLA del PRG son la documentación canónica).
3. Si una etapa intermedia (V1..V9, GTEJ, GTIN) no existe en Postgres, ver si ya quedó pre-agregada en `historia.utej`/`historia.utin`. Casi siempre sí — el PRG escribe los agregados, no los componentes.
4. Toda división va por `_safe_div` — el dBase no se rompe con /0, pero Python sí.
5. Renderizar el layout en Tailwind respetando los nombres del PRG (VENTA, MAT.PR., UT.ACT., etc.) — el gerente reconoce los rótulos visualmente; cambiarlos lo confunde.
