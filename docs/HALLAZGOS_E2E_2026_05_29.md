# Hallazgos E2E — turno nocturno 2026-05-29

Tests E2E ejecutados contra `programa.intela.com.ec` usando Chrome MCP
(Claude in Chrome) con el usuario `tamara · Accionista`. Sesión #6
abierta con el extracto `mov-27-05-202634XXXX6004 (1).xlsx` (264 movs).

## Baseline pre-test

- **Saldo a conciliar**: $2,557,969.47
- **Saldo banco programa**: $2,605,247.89
- **Pendientes programa**: 57 (+67,625.67 / −20,347.25 / neto +47,278.42)
- **Pendientes banco**: 173 (+203,645.90 / −4,995.56 / neto +198,650.34)
- **Saldo banco esperado**: $2,756,619.81
- Historial: 5 sesiones cerradas, Δ=0 en todas (sesiones de prueba previas)
- Deshacer: sin matches ni grupos activos (estado limpio)

## ✅ Validaciones que pasaron

### Pantalla de carga (`/conciliacion/banco`)

- Nombres nuevos aplicados: "Saldo banco programa", "Pendientes de
  conciliar en programa/banco", "= Saldo conciliado", "= Saldo banco
  esperado".
- Leyendas chiquitas eliminadas.
- Stack vertical de split funciona (+verde / −rojo / total semibold).
- Botones Historial + Deshacer arriba.
- Border line interna por fila (sin rayitas a media altura).

### Carga de extracto

- Upload del xlsx funcionó.
- Redirect inmediato a `/banco-v2?sesion_id=6`.
- Mensaje flash: "Sesión #6 abierta — 264 movimientos del extracto."
- Sesión grabada en `banco_conciliacion_sesion` con `abierta_en` + snapshot.

### Bucketización

- Tab Manual: 419 (banco 390 + programa 29)
- Tab Impuestos: 19
- Tab Transferencias: 0
- **Históricos cargados** ✓ (badge HISTO en filas del panel banco,
  ej "HISTO DEPOSITO $16,678.44"). El fix de `no_cheque` funciona.

### 🎯 CANARY del bug $67K — NO REGRESÓ

Test exacto del bug del idx que generó las txs de −$67,640 y −$67,162.

Procedimiento:
1. Marcar 4 items del tab Impuestos con idx altos (185, 188, 186, 189)
   — exactamente el rango donde el bug agarraba transferencias de $15K.
2. Conceptos: 2 COMISION-PAG (14.12 + 11.77) + 2 IVA (2.12 + 1.76).
3. Suma esperada: **$29.77** (NO $67K).

Resultado:
- Flash dice: *"Movimiento agrupado creado por **$-29.77**. 1 match(es)
  conciliados."*
- Saldo a conciliar se mantuvo en $2,557,969.47 (no hubo drift de $67K).
- Verificación en /banco-v2/deshacer: tx #14118 con doc ND, IMPORTE
  **−29.77**, concepto "Comisiones e impuestos 22/05".

**Confirmado: `res.real_only[i]` se usa correctamente. El bug no regresa.**

## 🐛 Bugs encontrados

### BUG #1 — crear_transaccion_agrupada_desde_reals solo graba 1 match de N

**Severidad: ALTA** (silenciosamente pierde matches, descalce contable).

**Síntoma**: en el test del CANARY se marcaron 4 reales, se creó la tx
BANCSIS agrupada por $-29.77 (suma correcta), pero solo 1 match se grabó
en `banco_conciliacion_match`. Los 3 reales restantes ($11.77 + $2.12 +
$1.76 = $15.65) quedaron sin conciliar:

- Tab Impuestos pasó de 19 → 18 movs (debería haber pasado a 15)
- /banco-v2/deshacer muestra "1/1 matches" (debería ser "4/4")
- La tx BANCSIS por −$29.77 existe pero solo apunta a 1 mov real

**Hipótesis causa**: en `matcher_banco.crear_transaccion_agrupada_desde_reals`:

```python
for real in reals:
    try:
        n = confirmar_match(..., conn=conn)  # MISMA conn de la tx
        if n: n_matches += 1
    except Exception:
        pass  # ⚠️ silencia el error
```

Si el primer `confirmar_match` lanza una excepción que aborta la
transacción psycopg2 (ej. el dual-write a `transacciones_bancarias` falla
por algún constraint o el `_tiene_migration_47` consulta rompe), los
siguientes 3 fallan con "current transaction is aborted, commands ignored
until end of transaction block" y caen al `except: pass` mudo.

**Fix propuesto**:

1. Quitar el `except: pass` ciego — loggear la excepción y propagarla,
   o al menos clasificar entre "ON CONFLICT DO NOTHING" (esperable) vs
   "transaction aborted" (síntoma de bug).
2. Si la conn está aborted, no insistir con los siguientes — abortar
   toda la operación y devolver `{"n_matches": 0, "error": ...}`.
3. Alternativa más simple: NO pasar `conn=conn` a `confirmar_match` —
   cada match usa su propia conexión y no se contaminan.

**Validación post-fix**: re-correr el canary. Con 4 reals seleccionados,
verificar:
- Flash dice "4 match(es) conciliados"
- Tab Impuestos pasa de 19 a 15
- /banco-v2/deshacer muestra "4/4 matches"

### Pendiente de investigar

- ¿Por qué el matcher P0 dio 0 transferencias por doc cuando el extracto
  trae muchas con número de comprobante? Posible: pocos `no_cheque` /
  `doc_banco` cargados en /cheques para este rango de fechas.
- ¿Cómo se comporta el flujo "anular grupo" cuando el grupo tiene
  matches que el bug #1 dejó sin grabar? El "Anular completo" debería
  hacer DELETE de la tx (que sí existe) y borrar el match grabado.
  Verificar.

## ✅ Estado al fin de la sesión de testing

Cleanup pendiente — voy a anular el grupo #14118 + borrar la sesión #6
abierta, dejando el saldo a conciliar en su valor original
$2,557,969.47.

## Próximos pasos

1. **Fixear BUG #1** (urgente — descalce contable real).
2. Continuar E2E:
   - Tab Manual con histórico (conciliar 1 par)
   - Anular grupo (verificar que borra tx + recompute saldos)
   - Terminar y guardar → XLSX
   - Historial muestra saldos
3. Cleanup completo + push del fix.
