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

_(se llena en Fase 2)_

## Drift dBase ↔ PC

_(se llena en Fase 1)_

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
