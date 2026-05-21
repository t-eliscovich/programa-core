# OVERNIGHT_TEST — 2026-05-20 noche → 21 madrugada

**Run:** sesión nocturna autónoma, secuencial.
**Status:** EN PROGRESO. Este archivo se actualiza por fase.

## Resumen ejecutivo

_(se completa al final)_

## Tabla de hallazgos — severidad C(rítica) / H(igh) / M(edium) / L(ow)

| # | Sev | Fase | Pantalla / módulo | Descripción | Acción tomada |
|---|---|---|---|---|---|
| _(empty so far)_ | | | | | |

## TODOs para Tamara

_(cosas que no pude hacer sola — credenciales, decisiones humanas, etc.)_

## Fixes aplicados (commit SHA)

_(se llena a medida que comito)_

## Pantallas testeadas en browser

| Pantalla | Status | Nota |
|---|---|---|
| `/` (home) | ✅ | Logo + bienvenida. Sin KPIs (intencional). |
| `/informes/balance` | ✅ | Resultados + Activo cuadran. Utilidad −941.618 (atención: pérdida acumulada del año) |
| `/cheques` | ✅ | $1.790.450 · 992 cartera. Tabs Cartera Z/Postergados/Daniela/Devueltos/Cartera total/Depositados/Eliminados |
| `/facturas` | ✅ | $5.051.001 · 4516 cartera, 4927 total. Sobrepagos en rojo. |
| `/posdat` | ✅ | $2.138.605 · 155 posdat + 11 YY = 166 partidas. Matchea Pasivos del Balance |
| `/caja` | ✅ | $25.973 · 523 movs. Hay "REVERSO id 522 — sin motivo" (L-3) |
| `/bancos` | ✅ | PICHINCHA $2.349.530 + INTERNACI $3.761,19 = $2.353.291. Matchea Balance ✓ |
| `/activos` | ✅ con drift | Valor libros $4.039.934 — Balance suma solo M/K/I vivos = $2.574.934 (L-4 explicado) |
| `/clientes` | ✅ | Listado paginable. Pago=C (contado/crédito). |
| `/compras` | ⚠ | Header dice "$2.138.606 · 166 partidas abiertas a proveedores" pero tabla muestra $2.864.093/395 filas (L-5 confusión KPI) |
| `/informes/balance/compras` | ✅ | 34 filas mes 05/2026 · $283.178 · 178.982 kg |
| `/informes/gastos` | ✅ | Matriz V1..V9. Total con amort $559.436. |
| `/informes/gastos/reclasificar` | ✅ | "No hay gastos sin clasificar" — 0 pendientes |
| `/informes/retiros` | ✅ | Mes 8 retiros $85.385,89 (matchea F&U) · Año $850.092 |
| `/informes/fuentes-y-usos` | ✅ post-fix | Total cuadra: 930.910 = 930.910. PASIVOS 23.246 USOS (corregido v7) |
| `/informes/historico-12m` | ✅ post-fix C-1 | Matriz mensual. Antes daba 404 — ver Fix |
| `/informes/historia/multianual` | ✅ | 2024/2025/2026 lado a lado, ΔKg + Δ% verde/rojo |
| `/informes/flujo` | ⚠ C-2 | **Bug crítico**: muestra fechas 2027/2028 con saldos negativos $-3.4M. Confuso |
| `/informes/retiros` (tab año) | ✅ | 64 retiros año $850.092 |
| `/capital` | ⚠ | KPI "$23.624 patrimonio al 20/02/2014" confuso. Saldo acum −$25.485.495 (L-2) |
| `/cobranzas/matriz-3-semanas` | ✅ | $940.755,87 total 3 sem · prom $62.717 |
| `/historial` | ✅ | 6628 activos · 5 reversos. Batch_id visible |
| `/conciliacion/hub` | ✅ | Dos cards (Depósitos / Cheques rebotados) |
| `/conciliacion/depositos` | ✅ | Wizard 3 pasos. Dropzone funcional |

## Drift dBase ↔ PC (Fase 1)

| Concepto | dBase (`HISTORIA.DBF` Abr 2026) | PC `/historico-12m` Abr 2026 | Δ |
|---|---:|---:|---:|
| BANCO | 2.374,1 | 2.374,1 | 0 |
| CARTERA | 6.902,1 | 6.902,1 | 0 |
| ANTICIPOS | 1.139,7 | 1.139,7 | 0 |
| STOCK MP+PROD | 8.097,9 | 8.097,9 | 0 |

Abril concuerda perfectamente — `HISTORIA.DBF` ya está cargado en `scintela.historia`.

Para mayo (parcial, al 20-may):

| Concepto | Balance live | F&U Δ (abr→may) | Snapshot mayo (proyecc.) |
|---|---:|---:|---:|
| Banco | 2.353.291 | -233.987 fuente | snap muestra 2.353.300 |
| Cartera | 6.841.450 | +822.278 uso (subió) | snap muestra 5.300.500 |
| Pasivos | 2.138.606 | +23.246 uso (bajó) | snap muestra 2.138.606 ✓ |

**Drift Cartera mayo:** Balance dice 6.841.450 (cheques 1.790 + facturas 5.051) pero snapshot histórico de mayo dice 5.300.500. Hipótesis: el snapshot del histórico no se actualiza dinámicamente; la última foto incluida es de inicio de mayo. El Balance es LIVE. Esto es por diseño — el histórico es snapshot, el balance es live.

## Drift F&U vs Balance (Fase 3)

Aplicado fix v7 que unificó las fórmulas. Estado actual:

| Línea | Balance LIVE | F&U Δ (abr→may, parcial) | ¿Coherente? |
|---|---:|---:|---|
| Caja+Bancos | $2.379.264 (Caja 25.973 + Bancos 2.353.291) | 233.987 fuente | ✓ Δ razonable, banco bajó |
| Cartera | $6.841.450 | 822.278 uso | ✓ Δ razonable, cartera subió |
| Anticipos USD | $1.118.797 | — | ⚠ F&U muestra — porque snapshot abril.anticipos=0 (excepción guard) |
| Stock MP+PROD | $8.016.204 | — | ⚠ Igual razón |
| Stock Quím | $296.839 | — | ⚠ Igual razón |
| Maquinaria | $972.685 | — | ⚠ Igual razón |
| Terr+Edif | $1.602.249 | — | ⚠ Igual razón |
| Pasivos | $2.138.606 | 23.246 uso (bajaron) | ✓ Δ razonable |
| Utilidad acum | −$941.618 (pérdida del año) | +642.842 fuente (mes) | ✓ Δ del mes corriente |
| Retiros mes | $85.385,89 | 85.386 uso | ✓ Matchea perfecto |

**Conclusión drift:** Pasivos / Banco / Cartera / Utilidad / Retiros — todos alineados ✓. Stocks y activos fijos quedan en `—` por el guard contra delta espurio (snapshot abril con esas columnas en 0). Eso es **esperado** y la solución es regenerar el snapshot de abril (fuera del scope de overnight).

---

## FASE 1 — Comparación dBase legacy ↔ Programa Core

### 1.a Inventario de DBFs disponibles

✅ **DBFs accesibles** en `/Users/tamaraeliscovich/Documents/INTELA copy/Files/`:
22 archivos `.DBF`. 15 manejados por `import_dbf.py`; 7 no manejados:
`ABOGCHEQ` (cheques abogados), `ABOGFAC`, `CLIENTES`, `ENTRADAS`,
`GGAST`, `GINT`, `GPICH`, `RETEN`, `UGCAJA`. La mayoría son tablas
auxiliares; CLIENTES es importante (cobertura faltante).

### 1.b Dry-run del sync

✅ Corrido. Conteos por DBF:

| DBF | Filas | Notas |
|---|---:|---|
| ACTIVOS | 62 | OK |
| CAJA | 512 | OK |
| CHEQUES | 1898 | 18 stat='W' (legacy no mapeado), 2 stat='V' (mapean a B), 38 stat='C' (cancelados), 18 stat='1' (depósito banco 1) |
| COMPRAS | 392 | OK |
| DOLARES | 2964 | OK |
| FACTURAS | 4923 | 396 stat='T' (canceladas — PC ya las maneja), 8 stat='X' (anuladas) |
| FLUJO | 239 | OK |
| HISTORIA | 205 | OK |
| INICIALE | 315 | OK |
| INTER | **1** | ⚠ Solo 1 fila — saldo inicial 3761.19 con stat='\*'. Las transacciones reales de Internacional vienen por otra vía. Mapper genérico mantiene stat='\*' tal cual. |
| PICHINCH | 650 | OK |
| POSDAT | 199 | 158 banc=0 (deuda viva), 41 banc=9 (instrumentado legacy) |
| RETIROS | 1077 | OK |
| TINTO | 122 | OK |
| XGAST | 177 | OK |

**Total ~13,737 filas listas para insertar.**

### 1.c Stats legacy detectados (importante para sync)

| Stat | Tabla | Count | Acción actual del script |
|---|---|---:|---|
| V | cheques | 2 | ✓ Remap a B |
| C | cheques | 38 | Pass-through (PC ya lo entiende: "cancelado") |
| W | cheques | 18 | ⚠ **No mapeado**. Solo aparece en `MODIFICA.PRG:800` como color visual "R/W" (warning). PC no lo lee — probablemente queden invisibles en la app. |
| 1 | cheques | 18 | Pass-through (banco propio depositado) |
| Y / \* | varios | 0 / 1 | ✓ Skip en remap |
| T | facturas | 396 | Pass-through (PC lo lee como "cancelada") |

**Hallazgo H-1:** 18 cheques con `stat='W'` se importarán con stat='W' y la app NO sabe interpretarlos. Acción recomendada: investigar qué hacen esos 18 (revisar uno) y agregarlo al remap o a `STAT_VIVOS_CARTERA`.

### 1.d Lectura cruzada de PRGs vs PC

Revisé `INFORMES.PRG`, `BANCOS.PRG`, `ALTAS.PRG`, `MODIFICA.PRG`:

- **PROCEDURE FUENTES** (`INFORMES.PRG:1654-1782`) ↔ `modules/informes/queries.py::fuentes_y_usos` — ya alineados (ver fix v7 de hoy). El PC tiene mejoras intencionales (filtro `importe>0` removido, fallback live).
- **PROCEDURE CHEQUE** (`ALTAS.PRG:773` IF STAT='T') — PC usa la misma lógica para factura.stat='T' canceladas.
- **PROCEDURE MENU** color logic (`MODIFICA.PRG:800`): PRG distinguía stat='W' con "R/W" color → bug latente en PC (no se renderiza warning para esos cheques).
- **PROCEDURE CHEQUERA** (`BANCOS.PRG`) ↔ `modules/bancos/queries.py::emitir_cheque` — coincide en los side-effects (proveedor/retiro/caja/gasto). PC tiene tipos extra (`anticipo_usd`, `otro`) — mejora intencional.
- **PROCEDURE FUENTES SUM USUTI/USRET** ↔ implementado ahora en v6 con `(patr_fin − patr_ini) + retiros_periodo` lo cual da el mismo resultado contable que el SUM legacy cuando los snapshots están bien.

No detecté ninguna fórmula PRG que el PC ignore por completo.
