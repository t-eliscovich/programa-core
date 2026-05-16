# Skill addendum — batch 19 (2026-04-29)

**Sidebar minimalista + gráfico de flujo arreglado + tablero con balance prominente.**

Continuación directa de batch 18. El batch 18 dejó el sidebar con 8 grupos
(Tablero / Flujo / Bases de datos / Operaciones / Cartera / Contabilidad /
Informes / Administración). Eso seguía siendo demasiado para la dueña, que
pidió "muchas menos pestañas" y aclaró el flujo real:
- **Dueño**: mira el balance (activos / pasivos / patrimonio neto) — no el flujo gráfico.
- **Equipo (Daniela y resto)**: cargan cheques, conciliación, facturas, compras.
- "No quiero un botón Cargar — en cheques cargo cheques, en facturas facturas, etc."

## Decisión final: sidebar con 4 top-level entity-centric

```
Tablero                  ← home con BALANCE prominente para el dueño
Cheques                  ← lista + tabs por estado (Z/B/devueltos/D/P/todos)
Facturas                 ← lista + tabs por estado (cartera/canceladas/eliminadas/todas)
Compras y producción     ← tabs todas/compras/producción/anticipos (ya estaba de batch 18)
─────────────────────────
Más…  ▾  (colapsable al fondo, agrupado por sub-secciones)
   ▸ Cartera y cobranzas    Saldos / Aging / Por grupo / Calendario / Cobros / Estado de cuenta
   ▸ Otras operaciones      Caja / Posdatados / Conciliación / Emitir cheque / Retenciones / Proformas
   ▸ Bases de datos         Clientes / Proveedores / Bancos / Contactos
   ▸ Informes               Resultados / Flujo gráfico / Ventas / Deudas / Historia / Metas
   ▸ Contabilidad           Provisiones / Capital / Activos / Retiros / Gastos
   ▸ Administración         Bitácora / Periodos / Usuarios / Cargar flujo legacy
```

**Por qué entity-centric en lugar de action-centric (ej: un botón "Cargar"):** el equipo
piensa "estoy trabajando con cheques" — no "tengo que cargar algo". Cuando entrás a Cheques,
hacés todo lo de cheques (ver, cargar, depositar, postergar, rebotar). El botón "+ Nuevo"
arriba a la derecha, los tabs por estado abajo. Mismo patrón en Facturas y Compras.

**El buscador global Ctrl+K queda igual** — sigue siendo la forma rápida de saltar a
cualquier cliente / factura / cheque / proveedor sin pasar por el menú.

## Gráfico de flujo arreglado (nunca mostraba nada)

**Causa raíz**: el gráfico leía de `scintela.flujo`, una tabla legacy que el dBase viejo
alimentaba pero que el nuevo app nunca pobló. En producción la tabla está vacía o stale,
así que el chart aparecía sin datos siempre.

**Fix (2026-04-29)**: nueva función `informes.queries.flujo_calculado(dias_atras, dias_adelante)`
que computa el flujo EN VIVO desde las fuentes de verdad transaccionales:

- **Saldo inicial** = SUM del último saldo registrado por cada banco (`scintela.banco` +
  último `scintela.transacciones_bancarias.saldo` por cuenta).
- **Por cada día futuro hasta hoy + N**:
    + cheques en cartera (stat IN ('Z','D','P')) con `fechad = ese día` → ingresos
    - posdat con `banc<>9` y `fechad = ese día` → egresos a proveedores
- **Saldo se acumula día a día** desde hoy.
- **Días pasados (hasta 14 atrás)**: se renderiza línea recta del saldo actual — no
  recalculamos historia (sería caro y requeriría replay del libro). Sirve sólo para dar
  contexto visual.

`flujo_proyeccion()` (la versión legacy que lee `scintela.flujo`) sigue disponible vía
`/informes/flujo/grafico?fuente=tabla` para casos donde alguien sí cargó datos en la
tabla legacy.

**Consumo en la view**: default es `flujo_calculado()`. Empty state actualizado para
no decir "tabla scintela.flujo vacía" sino "no hay saldo bancario, cheques en cartera
ni posdat con fecha futura — cargá movimientos para que aparezca la proyección".

## Tablero rediseñado: balance prominente

**Cambio clave**: arriba del todo del tablero del dueño aparece un **panel de balance**
grande (gradient slate, ocupa todo el ancho) con tres bloques:

```
┌──────────────────────────────────────────────────────────┐
│ BALANCE                              ver detalle →        │
│                                                            │
│ Activos              Pasivos          Patrimonio neto     │
│ $ 1.234.567          $ 456.789         $ 777.778          │
│ total                obligaciones      Utilidad mes: $X   │
└──────────────────────────────────────────────────────────┘
```

Los datos vienen de `informes.queries.informe_balance()` que ya existía — sólo se
agregó al `_parallel()` del dueño. El panel es clickeable y lleva a `/informes/balance`
para el detalle.

Las 4 KPIs operativas (Cartera / Deudas / Bancos / Facturado mes) quedan debajo, sin cambios.

## Pestañas internas — vocabulario canónico expuesto

### `/cheques`
Tabs por bucket de stat:

| Tab           | Stats                | Significado                          |
|---------------|----------------------|--------------------------------------|
| En cartera    | Z                    | Recibidos, sin movimiento            |
| Depositados   | B, A (legacy)        | En el banco                          |
| Devueltos     | 1, 2, 3, R (legacy)  | Rebotados                            |
| Daniela       | D                    | En gestión de cobranza               |
| Postergados   | P                    | Con fecha futura                     |
| Todos         | *                    | Sin filtro                           |

Conteos calculados via single GROUP BY query, mostrados como sufijo en cada tab.
El tab "Todos" no muestra conteo (se entiende).

### `/facturas`
Tabs por bucket de stat:

| Tab          | Stats                       | Significado                |
|--------------|-----------------------------|---------------------------|
| Cartera      | Z + A (con saldo > 0)       | Saldo vivo                 |
| Canceladas   | T                           | Cobradas total             |
| Eliminadas   | X + Y (legacy)              | Anuladas administrativas   |
| Todas        | *                           | Sin filtro                 |

`queries.conteos_por_vista()` agrega un solo GROUP BY que devuelve los 4 buckets
con n + total_saldo + total_importe.

### `/compras` (de batch 18)
Tabs todas / compras / producción / anticipos. Discriminación por `tipo + kg`:
- producción ⟺ tipo='K' AND kg > 0
- compras ⟺ tipo IN (H, Q, C) OR (tipo='K' AND kg=0)
- anticipos ⟺ tipo = 'A'

## Verificación final (cierre del batch)

- `ruff check .` (en módulos tocados) — **clean**.
- `pytest -q` — **390 passed, 9 skipped, 0 failed**.
- `tests/test_routes_smoke.py` — **69 GET routes, 0 failures**.

## Archivos tocados en este batch

```
templates/base.html                                     sidebar 4 top-level + Más colapsable
modules/informes/queries.py                             + flujo_calculado() (en vivo)
modules/informes/views.py                               flujo_grafico usa flujo_calculado por default
modules/informes/templates/informes/flujo_grafico.html  empty state con CTA a cheques
modules/cheques/views.py                                + conteos por bucket en lista
modules/cheques/templates/cheques/lista.html            tabs por estado (Z/B/devueltos/D/P/todos)
modules/facturas/queries.py                             + vista filter en buscar(), conteos_por_vista()
modules/facturas/views.py                               + vista param + conteos en context
modules/facturas/templates/facturas/lista.html          tabs (cartera/canceladas/eliminadas/todas)
modules/dashboard/views.py                              + balance en _parallel() del Dueño
modules/dashboard/templates/dashboard.html              panel de Balance prominente arriba
docs/SKILL_ADDENDUM_BATCH_19.md                         ESTE DOCUMENTO
```

## Reglas que NO se cambian (heredadas de batch 18)

- Vocabulario canónico de stats sigue siendo el de batch 18 (Z/B/V/1/2/3/D/P para cheques,
  Z/A/T/X para facturas, K/H/Q/C/A para compras).
- `xfactura` sigue read-only.
- `bitacora_acciones` sigue capturando todo write.
- Permisos no cambian.
- `posdat.banc=9` sigue significando cerrada.
- La integración SRI sigue siendo proyecto aparte.

## Pendientes

- **Migración 0013** del batch 18 todavía no se aplicó en RDS. Hacerlo antes del próximo
  ship para que la columna `cheque.fecha_recibido` exista en producción.
- **Verificar el panel de balance contra datos reales**. Hoy el dashboard fetcha
  `informe_balance()` que depende de `scintela.historia` (snapshot mensual) — si la tabla
  está vacía/desactualizada, los kg salen mal pero el balance monetario debería estar
  bien (todas sus fuentes son live).
- **Considerar mover el botón "Imprimible" del header**: hoy aparece sólo si rol=Dueño
  pero ahora con balance prominente, una vista imprimible del balance solo (sin las KPIs
  operativas) podría ser más útil. Punto para una sesión futura.
- **Buscador global Ctrl+K**: confirmar que sigue funcionando con el sidebar nuevo
  (debería, no toqué `app.js` ni el handler global).
