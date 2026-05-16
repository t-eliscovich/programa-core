# Skill addendum — batch 16 (2026-04-24)

Sesión muy larga, varios sub-batches. Tarde se reportó el dashboard mal
y arrancó la sesión hot-fixing y agregando módulos faltantes.

## Snapshot al cierre de batch 16

- 362 tests passed
- 57+ GET routes (a lo largo de la sesión subió a ~66)
- 11 migraciones (`0001` → `0011`)

## Lo que se hizo

### Bloque 1 — 4 "quick wins" iniciales

1. **`compras.anular` con 2-step**
   - Migración `0011_compra_stat.sql` — agrega `stat char(1)` y
     `observacion varchar(200)` a `scintela.compra`
   - `compras.queries.anular()` con `db.tx()` — UPDATE compra +
     DELETE posdat hermana en una transacción
   - Patrón `_confirmar_accion.html` para 2-step + motivo obligatorio
   - Toggle "ver/ocultar anuladas" en `/compras`

2. **Tipos `posdat` y `retencion` en recientes**
   - `recientes/queries.py::_TIPOS_VALIDOS` extendido a 6 tipos
   - Wireado en `posdat/views.py::editar` (GET) y
     `retenciones/views.py::confirmar_anulacion`
   - Dots naranja (posdat) y púrpura (retención) en sidebar

3. **`flash_exc()` helper** aplicado a 25 sites
   - Helper en `error_messages.py` que humanize la excepción
   - Replace en 13 archivos: periodos, compras, conciliacion, cartera,
     posdat, provisiones, sri, usuarios, cheques, proveedores, facturas,
     clientes, retenciones
   - Gotcha: el regex de batch-replace insertaba el import dentro del
     multi-line `from flask import (...)`, hubo que escribir un script
     de reparación

4. **Kbd hint ⌘K más visible**
   - Botón ancho con placeholder "Buscar cliente, factura, cheque…"
   - Platform detection JS: ⌘K en Mac, Ctrl+K en Windows
   - Pulse one-shot de 2.2s × 3 ciclos para usuarios nuevos
   - localStorage `__gs_seen` recuerda el dismiss

### Bloque 2 — Bulk actions cartera

5. **Multi-select en `/cartera/aging`**
   - Checkboxes por fila + master checkbox + contador vivo
   - Acciones: Copiar emails / Copiar tel. / Exportar CSV / Ver solo
     seleccionados / Limpiar
   - `clientes.correo` agregado al SELECT del aging

### Bloque 3 — Dashboard inicial fix

6. **Cards gigantes con números cortados** — clamp() inline puro,
   después grid `md:grid-cols-2 lg:grid-cols-4` (xl no estaba compilado)
7. **Vista Simple llena** — chips de la semana (cobrar/pagar/rebotados)
8. **Saludo + fecha** en header (`Buenos días, tamara · Lunes 27 de abril`)
9. **Bancos con saldo negativo** — fila roja con badge "SOBREGIRO"
10. **Top 10 deudores con barra %** del total
11. **Trend "flat"** → guion `—` en vez de `0%`

### Bloque 4 — Quick filters cartera + chart evolución + calendario

12. **Quick filters** en aging: Todos / Críticos 90+ / Con email / Con tel
    / En STOP / Sin contacto. JS hide rows + auto-deselect.
13. **Chart "Evolución cartera vs deuda 12 meses"** en dashboard Dueño
    (vista completa) usando Chart.js + `historia.cart`/`deuda` + último
    punto on-the-fly
14. **`/cobranzas/calendario`** — agenda visual cheques posfechados +
    facturas que vencen, agrupado por día con `<details>` colapsables

### Bloque 5 — Módulos nuevos

15. **`/gastos`** — listado de `scintela.xgast` con KPIs (total, saldo
    pendiente, ticket promedio)
16. **`/retiros`** — listado de `scintela.retiros` con resumen por código
17. **`/activos`** — activos fijos con valor en libros, % depreciado en
    chips de color, botón "Correr amortización del mes"
18. **`/iniciales` + `/iniciales/comparativo`** — metas mensuales (kg,
    ventas$, gastos) + comparativo Real vs Meta con cumplimiento %
19. **`/clientes/contactos`** — directorio con tel/email + botoncitos
    WhatsApp / Llamar / Email directos
20. **`/cobros-efectivo`** — cobros recibidos últimos 7 días, agrupado
    por día (reemplaza opción "E" del menú dBase legacy)

### Bloque 6 — Provisiones bug fixes

21. **Bug truncate**: concepto era `[:80]` pero schema es varchar(50).
    Fix: `[:50]`. Lo mismo periodo_aplica `[:20]` → `[:30]`.
22. **Falta botón "+ Nueva provisión"** prominente
23. **Falta acciones por fila** — Editar + Eliminar gateado en `provisiones.editar`
24. **Falta buscador**
25. **`total_mensual` engañoso** — solo contaba "MENSUAL" exacto. Ahora
    `resumen()` calcula prorrateado (anuales÷12, bimestrales÷2, etc).

### Bloque 7 — Bugs reportados durante demo

26. **Retiros — modelo confundido** — la columna `de` no es socio sino
    código legacy de 2 letras. Quitamos KPI "Personas". Renombramos
    "Por socio" → "Por código". Columna "De" → "Cód." con tooltip.
27. **Chart de flujo no funciona** — Chart.js timing fragility. Reescribí
    con `window._dashCharts.push(fn)` + `<script ... onload="...">`.
28. **"None" string en N° cheques** — fix con check explícito en template
    para `c.no_cheque in (None, '', 'None', 'NONE', 'none')`.

## Archivos nuevos en batch 16
- `migrations/0011_compra_stat.sql`
- `modules/gastos/` (módulo completo)
- `modules/retiros/` (módulo completo)
- `modules/activos/` (módulo completo)
- `modules/iniciales/` (módulo completo)
- `modules/cobranzas/` (módulo completo — calendario + cobros recibidos)
- `modules/clientes/templates/clientes/contactos.html`
- `modules/cartera/templates/cartera/aging.html` (rewrite con bulk + filters)

## Permiso nuevos en `config/roles.py`
- `compras.anular`, `activos.amortizar`, `iniciales.editar`
