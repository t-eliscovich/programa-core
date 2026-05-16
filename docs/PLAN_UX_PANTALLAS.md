# Plan UX — todas las pantallas

**Fecha:** 2026-04-30
**Trigger:** la pantalla `/caja` muestra 4 KPIs contradictorios ("Saldo derivado SUM" vs "Saldo registrado"). Usuaria pidió revisar TODAS las pantallas con criterio de producto.

## Principios de producto que aplico

1. **Una pantalla = un trabajo principal.** Si vas a Caja, viniste a ver "cuánto tengo en efectivo HOY". Si viniste a auditar, ese es un secondary use case.
2. **Un número primario.** Lo más grande arriba. Nunca dos métricas con el mismo nombre que se contradicen.
3. **Vocabulario humano, no del código.** "Saldo derivado (SUM)" es jerga de developer. El usuario quiere "Saldo en caja".
4. **El segundo segundo cuenta.** Después del número grande, 2-3 datos de contexto. Después, la tabla. No 5 KPIs todos del mismo tamaño.
5. **Diferencias entre dos cifras se explican o se esconden.** Si el saldo running y la suma no cuadran, eso es un warning de auditoría — chico y al costado, no como KPI grande.
6. **Acciones primarias en un solo lugar (top-right).** Siempre.
7. **Pestañas con count + monto.** Para ver el universo sin cliquear.
8. **"Mostrando X de N"** cuando el listado está limitado. Nunca engañar con totales de las 500 visibles.

## Inventario priorizado

Clasifico por frecuencia de uso para priorizar el tiempo.

### 🔥 Críticas (uso diario, primera prioridad)
1. **`/`** — Dashboard del Dueño
2. **`/informes/balance`** — Balance de resultados
3. **`/caja`** — Libro de caja  ← problema actual
4. **`/cheques`** — Listado de cheques
5. **`/facturas`** — Listado de facturas
6. **`/bancos`** — Listado de bancos / detalle
7. **`/compras`** — Listado de compras
8. **`/informes/cartera`** + **`/cartera/aging`** — Cartera viva por cliente

### 📊 Frecuentes (uso semanal)
9. **`/posdat`** — Deudas con proveedores
10. **`/informes/flujo/grafico`** — Flujo de fondos
11. **`/informes/estado-cuenta`** — Estado de cuenta cliente
12. **`/retenciones`** — Retenciones emitidas
13. **`/conciliacion`** — Conciliación bancaria

### 📋 Esporádicas (mensuales)
14. **`/provisiones`** — Provisiones
15. **`/capital`** — Movimientos de capital
16. **`/retiros`** — Retiros del dueño
17. **`/activos`** — Activos fijos
18. **`/informes/gastos`** — Reporte de gastos
19. **`/informes/ventas`** — Reporte de ventas
20. **`/informes/historia`** — Histórico mensual
21. **`/informes/iniciales`** — Proyecciones mensuales

### 🛠 Master data
22. **`/clientes`** — Clientes
23. **`/proveedores`** — Proveedores

### ⚙️ Admin (uso ocasional)
24. **`/usuarios`** — Gestión de usuarios
25. **`/periodos`** — Cierre de períodos contables
26. **`/bitacora`** — Auditoría de cambios

### 🧾 SRI / facturación electrónica
27. **`/sri`** — Estado de comprobantes electrónicos

---

## Auditoría pantalla por pantalla

Para cada una identifico: **trabajo principal**, **número primario**, **acciones**, **issues actuales**, **fix propuesto**.

### 1. `/caja` — Libro de caja  🔥 ARREGLAR YA

**Trabajo principal:** ver cuánto efectivo hay AHORA + registrar entrada o salida.

**Número primario:** **SALDO EN CAJA $80.054** (al cierre del último movimiento).

**Issues actuales:**
- 4 KPIs todos del mismo tamaño compiten por atención.
- "Ingresos totales" $753.887 — es de TODA la historia, sin contexto temporal.
- "Egresos totales" $-9.496 con signo negativo en rojo — innecesario.
- "Saldo derivado (SUM)" vs "Saldo registrado" — dos cifras que se contradicen, el usuario no sabe cuál creer.
- "(SUM)" en el label — jerga de developer.

**Fix:**
- 1 KPI grande: **SALDO EN CAJA $80.054** + fecha del último movimiento abajo.
- Strip pequeño debajo: "Esta semana: +$X entradas · -$Y salidas · neto +$Z".
- Si saldo derivado ≠ registrado → warning ámbar pequeño "⚠ Hay diferencia de $X entre el saldo registrado y la suma. [Ver auditoría]". Click expande detalle.
- Botones de acción: "+ Entrada" verde, "− Salida" rojo, "CSV", "Imprimir" — como están.

### 2. `/` — Dashboard

**Trabajo principal:** abrir a la mañana y saber "¿cómo está la fábrica HOY?".

**Número primario:** depende del rol. Para el Dueño: **PATRIMONIO NETO** + **CARTERA por cobrar** + **DEUDA por pagar**.

**Issues típicos** (sin verlo, asumo según cómo está armado):
- Probablemente tiene demasiados widgets.
- "Mes en curso" puede competir con "Cartera total".

**Fix:**
- 3 KPIs grandes en top: Cartera viva · Deuda viva · Cash en mano (caja+bancos).
- Mes en curso: 1 strip pequeño debajo (facturado / cobrado / neto).
- Después: actividad reciente, alertas (cheques rebotados, facturas vencidas), cobranza de la semana.

### 3. `/informes/balance` — Balance ✅ EN PROGRESO

**Trabajo principal:** ver el resultado del mes y el patrimonio.

**Número primario:** **PATRIMONIO NETO**.

**Issues actuales:**
- Stock kg en 0 (arreglado en `iniciales_mes_actual()`).
- Proyección en 0 (mismo arreglo).
- Patrimonio ahora destacado pero pista del usuario: "todavía hay 4 sumas equivalentes que distraen".

**Fix:**
- Patrimonio Neto en font-size 1.5rem (hecho).
- Total Activo y Subtotal jerarquía visual clara (hecho).
- ✅ Reiniciar Flask para que carguen kg + proyección.

### 4. `/cheques` — Listado de cheques 🔥 EN PROGRESO

**Trabajo principal:** ver cheques en cartera + depositarlos.

**Número primario:** **TOTAL EN GESTIÓN** (= TOTC del balance).

**Issues actuales:** ya arreglado con tarjetas KPI grandes + tabs con $ al lado del count.

**Fix:** ✅ hecho.

### 5. `/facturas` — Listado de facturas 🔥 EN PROGRESO

**Trabajo principal:** ver cartera viva + crear factura.

**Número primario:** **CARTERA VIVA** (= TOTF del balance).

**Issues actuales:** ya arreglado con tarjetas KPI grandes + tabs con $.

**Fix:** ✅ hecho.

### 6. `/bancos` — Listado de bancos

**Trabajo principal:** ver el saldo de cada banco + total general.

**Número primario:** **TOTAL EN BANCOS** (= SALBANC del balance).

**Issues típicos:**
- Probablemente muestra muchos bancos con saldo 0.
- No hay "total" en font grande arriba.

**Fix:**
- KPI grande: **TOTAL EN BANCOS $X**.
- Strip de bancos individuales (Pichincha · Internacional · Caja USD · …) cada uno con su saldo.
- Toggle "Ver todos / Sólo activos".

### 7. `/compras` — Listado de compras

**Trabajo principal:** ver compras del mes + crear nueva + ver deuda con proveedores.

**Número primario:** **DEUDA VIVA** (= TOTP, viene de posdat).

**Issues típicos:**
- "Compras del mes" probablemente está mezclado con "Deuda histórica".
- No queda claro la diferencia entre /compras (histórico) y /posdat (deuda viva).

**Fix:**
- Tarjeta: **Deuda con proveedores $X** (= TOTP) con link a /posdat.
- Tarjeta: **Compras del mes $Y · Z compras**.
- Tabs: Todas / Compras (importadas) / Producción (K kg) / Anticipos.

### 8. `/posdat` — Deudas con proveedores

**Trabajo principal:** ver qué le debo a quién + marcar pagada.

**Número primario:** **TOTAL ADEUDADO** (= TOTP).

**Fix:**
- KPI: **DEUDA TOTAL $X · N obligaciones pendientes**.
- Strip: "Vencen esta semana · Vencidas · A futuro".
- Lista agrupada por proveedor (no fila por fila, sino "BP $805K · 20 obligaciones").

### 9. `/cartera/aging` y `/informes/cartera` — Cartera

**Trabajo principal:** ver quién me debe, cuánto, hace cuánto.

**Número primario:** **TOTAL CARTERA** + **TOP 3 DEUDORES**.

**Fix:**
- KPI grande: **CARTERA VIVA $X · N clientes**.
- Strip de aging: 0-30 / 31-60 / 61-90 / 90+ (ya está bien armado).
- Lista por cliente con bucket coloreado.

### 10. `/informes/flujo/grafico` — Flujo de fondos

**Trabajo principal:** ver proyección de cobros y pagos.

**Número primario:** **SALDO PROYECTADO HOY** + **MÍNIMO en próximos 30 días**.

**Fix:** chart está OK. Header: KPI grande del saldo + del mínimo proyectado con su fecha.

### 11. `/clientes` y `/proveedores`

**Trabajo principal:** buscar/editar uno + ver lista.

**Número primario:** **CARTERA TOTAL** (clientes) o **DEUDA TOTAL** (proveedores).

**Fix:** mismo patrón — KPI grande arriba + buscador + lista.

### 12. `/conciliacion` — Conciliación bancaria

**Trabajo principal:** matchear cheques con movimientos de banco.

**Número primario:** **N° de items SIN MATCH** (lo importante = lo problemático).

**Fix:** KPI rojo "23 sin match" si los hay, verde "Todo conciliado" si no.

### 13. `/retenciones`

**Trabajo principal:** emitir retención + ver histórico.

**Número primario:** **RETENCIONES DEL MES**.

### 14. `/capital`, `/retiros`, `/provisiones`, `/activos`

**Trabajo principal:** registrar movimientos del mes.

**Número primario por pantalla:**
- Capital: total aportado (acumulado) + del mes.
- Retiros: del mes (en US$ y kg).
- Provisiones: total provisionado del mes.
- Activos: valor neto contable.

### 15. `/usuarios`, `/periodos`, `/bitacora`

**Admin** — uso ocasional. Listas funcionales OK como están.

### 16. `/sri` — Comprobantes electrónicos

**Trabajo principal:** ver qué facturas se autorizaron / rechazaron.

**Número primario:** **N° pendientes de enviar** (rojo si > 0).

---

## Plan de ejecución

Prioridad por uso real diario × magnitud del problema visual:

### Sprint 1 — esta sesión (críticas)
- ✅ Balance (kg + proyección + patrimonio destacado)
- ✅ Cheques (KPIs grandes + tabs)
- ✅ Facturas (KPIs grandes + tabs)
- 🔥 **Caja** ← arreglar AHORA
- 🔥 **Bancos** ← KPI total grande
- 🔥 **Compras** ← KPI deuda + tabs
- 🔥 **Dashboard** ← simplificar a 3 KPIs

### Sprint 2 — próxima sesión (frecuentes)
- Posdat (KPI deuda total + agrupación por proveedor)
- Cartera/aging (mejorar header)
- Flujo gráfico (KPI saldo proyectado + mínimo)
- Conciliación (KPI sin-match)

### Sprint 3 — polish
- Clientes/Proveedores (KPI cartera/deuda)
- Retenciones (KPI mes)
- Capital/Retiros/Provisiones/Activos (KPI mes)
- SRI (KPI pendientes)

### Patrón estándar de KPI card

```html
<div class="rounded-lg border-2 border-{color}-300 bg-gradient-to-br from-{color}-50 to-{color}-100/40 px-5 py-3">
  <div class="text-xs uppercase tracking-wider text-{color}-700 font-semibold">{LABEL CORTO}</div>
  <div class="text-2xl font-bold text-{color}-900 tabular-nums mt-1">
    $ {{ valor | num_es(0) }}
  </div>
  <div class="text-xs text-{color}-700/80 mt-1">
    {contexto: count, fecha, comparación}
  </div>
</div>
```

Colores semánticos:
- **Verde (emerald)**: número positivo importante (cartera viva, ingresos, total bancos).
- **Azul**: cifra de patrimonio o cierre.
- **Rojo (rose)**: deuda, alerta, números negativos importantes.
- **Ámbar**: warning, drift, "revisar".
- **Slate**: contexto secundario.

### Anti-patrones a evitar (lo que está mal hoy)

1. **N KPIs del mismo tamaño** = 0 KPIs primarios. Siempre uno destaca.
2. **Sufijos técnicos** como "(SUM)", "(running)", "(stored)". Esa jerga vive en docstrings, no en UI.
3. **Dos números equivalentes con nombres distintos** ("Saldo derivado" vs "Saldo registrado"). O explicás la diferencia o mostrás uno solo.
4. **Negativos rojos para egresos esperados**. Los egresos son normales — verde si quedó saldo positivo. Rojo sólo para problemas.
5. **Totales de filas visibles** sin avisar. Si limitás a 500, "Mostrando X de N · Total filtro $T".
6. **Pestañas con sólo count, sin monto**. El usuario quiere saber dónde está la plata, no sólo cuántas filas.

---

## Próximos pasos

1. Arreglo **Caja** ahora (un solo KPI grande, strip de contexto, warning sutil de auditoría).
2. Arreglo **Bancos** y **Compras** con el mismo patrón.
3. Simplifico **Dashboard** a 3 KPIs primarios.
4. Después de eso, la usuaria revisa y vamos al Sprint 2.

Cada cambio: verificación visual antes de pasar al siguiente. No big-bang.
