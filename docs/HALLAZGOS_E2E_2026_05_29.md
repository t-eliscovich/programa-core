# Hallazgos E2E — turno nocturno 2026-05-29

> ## ⚠️ LEER PRIMERO — saldo a conciliar cambió durante el test
>
> El "Anular completo" del grupo creado por mi test hizo que el saldo
> a conciliar pasara de **$2,557,969.47** (baseline) a **$2,514,376.99**
> (−$43,592.48). Antes de seguir trabajando, **verificá contra el banco real**
> cuál de los dos valores es el correcto:
>
> - Si $2,514,376.99 es correcto → el "bug" en realidad fue una corrección
>   de un saldo que estaba mal desde antes (probablemente desde los tests
>   del bug $67K que hicimos juntos durante el día).
> - Si $2,557,969.47 es correcto → hay un bug en `bank_helpers.recompute_saldos_desde`
>   que corrompió $43K. NO usar "Anular completo" hasta investigar.
>
> Query SSM para diagnosticar (corre en CloudShell):
>
> ```bash
> aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
>   --document-name "AWS-RunPowerShellScript" \
>   --parameters '{"commands":["$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable(\"DATABASE_URL\",\"Machine\"); cd C:\\programa-core; & C:\\Python312\\python.exe -c \"import db; db.init_pool(); r = db.fetch_one(\\\"SELECT MAX(id_transaccion) AS ult_id, MAX(fecha) AS ult_fecha FROM scintela.transacciones_bancarias WHERE no_banco=10\\\"); print(r); r2 = db.fetch_one(\\\"SELECT saldo FROM scintela.transacciones_bancarias WHERE no_banco=10 AND saldo IS NOT NULL ORDER BY fecha DESC, id_transaccion DESC LIMIT 1\\\"); print(\\\"Último saldo:\\\", r2)\""]}'
> ```
>
> Compará el último saldo con lo que el banco te muestre. Esa es la fuente
> de verdad.
>
> ---

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

### BUG #2 — Anular grupo corrompe el saldo en $43K (debug urgente)

**Severidad: ALTA** (corrompe saldos contables al anular).

**Síntoma**: anular el grupo #14118 (ND −$29.77) bajó el saldo PC libros
en **$43,592.48** (NO en $29.77 como debería):

| Métrica | Antes del test | Después del Anular |
|---|---|---|
| Saldo a conciliar | $2,557,969.47 | $2,514,376.99 (−$43,592.48) |
| Saldo PC libros | $2,605,247.89 | $2,561,655.41 (−$43,592.48) |
| Saldo banco esperado | $2,756,619.81 | $2,713,027.33 (−$43,592.48) |

Lo esperado era que VOLVIERA al baseline ($2,557,969.47) porque la única
tx que existía era la ND -$29.77 (test impuestos). Al borrarla:
- Saldo libros debería SUBIR $29.77 (porque el ND restaba)
- Saldo a conciliar debería volver al baseline

En su lugar BAJÓ $43,592.48. Diferencia absurda.

**Hipótesis causa**: `bank_helpers.recompute_saldos_desde` tiene un bug
en el walk-forward que propaga un error acumulativo. Cuando la tx
agrupada se INSERTÓ al medio del histórico (fecha 22/05/2026 con muchas
filas posteriores), el walk-forward recalculó todas las posteriores. Al
DELETE de la tx + recompute_saldos_desde la siguiente, el cálculo no
revierte simétricamente — probablemente porque toma como ancla un saldo
PREVIO ya corrompido por el insert original.

**Posibles fuentes específicas**:
1. `_saldo_previo()` en bank_helpers retorna el saldo de la fila previa,
   pero si la cadena de saldos posteriores fue calculada con un offset,
   el ancla queda mal.
2. `recompute_saldos_desde` recalcula desde `ancla_id` pero NO incluye
   la fila ancla, así que la fila ancla mantiene su saldo viejo (que
   fue calculado CON la tx existente) y todas las posteriores quedan
   shifted.

**Validación necesaria** (SSM): correr
```sql
SELECT id_transaccion, fecha, documento, importe, saldo
  FROM scintela.transacciones_bancarias
 WHERE no_banco = 10
   AND fecha BETWEEN '2026-05-22' AND '2026-05-23'
 ORDER BY fecha, id_transaccion;
```
Comparar saldo running vs delta esperado en cada fila — si la cadena
está rota tras el anular, el bug está confirmado en
`recompute_saldos_desde`.

**Mitigación temporal**: no usar "Anular completo" en producción hasta
que esté fixeado. Para limpiar matches, usar Deshacer individual
(que no toca la tx BANCSIS, solo deshace el match).

### Pendiente de investigar

- ¿Por qué el matcher P0 dio 0 transferencias por doc cuando el extracto
  trae muchas con número de comprobante? Posible: pocos `no_cheque` /
  `doc_banco` cargados en /cheques para este rango de fechas.

## Validaciones adicionales que pasaron

- **Pantalla cerrada** `/banco-v2/cerrada/6`: render correcto, card verde,
  matches hechos, archivo origen, botones "Ver PDF de pendientes" +
  Historial + Nueva conciliación.
- **XLSX endpoint** `/banco-v2/pdf/6`: HTTP 200, 12,929 bytes, magic header
  `50 4B 03 04` (ZIP — formato xlsx correcto), Content-Type
  `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.
  Confirmado: el download funciona.
- **Anular grupo** elimina el grupo + matches de la pantalla Deshacer
  (después de anular, ambas secciones quedan vacías o limpias). Solo
  problema es el delta de $43K mencionado arriba en CRITICAL.
- **Historial** muestra balance inicial + final correctos para sesiones
  que sí tienen snapshots. Sesiones sin snapshot tienen fallback al
  snapshot más cercano por tiempo.

## Issues UX menores

- El botón "Ver PDF de pendientes" en la pantalla cerrada **dice PDF**
  pero descarga **XLSX**. Cambiar el texto a "📥 Descargar pendientes (Excel)".
- En `/banco-v2/historial`, las sesiones recién cerradas muestran
  `MATCHES: 1` pero el grupo asociado fue anulado → counter acumulativo,
  no real-time. Aclarar UX o decrementar al anular.

## Estado del sandbox al fin del E2E

- Sesión #6: **cerrada** (no afecta ya el flujo nuevo). XLSX generado en
  `data/conciliacion_pdfs/sesion_6.xlsx` (server). Stub histórico.
- Matches activos: **0** (el bug 67K canary fue anulado).
- Grupos BANCSIS activos creados por conciliación: **0**.
- Saldo a conciliar: **$2,514,376.99** (ver CRITICAL al inicio).

## Próximos pasos para mañana

1. **Resolver el delta de $43K** verificando contra el banco real (query
   SSM al pie del CRITICAL).
2. **Fixear BUG #1** (SAVEPOINT — ya pusheado en commit `004606e`, deploy
   en curso). Próxima ejecución de impuestos debería grabar N matches
   de N reales, no 1 de N. Test canary: marcar 4 IVAs/COMISIONES, el
   flash debe decir "4 match(es) conciliados" y `/banco-v2/deshacer`
   debe mostrar "4/4 matches" en el grupo.
3. **Decidir sobre el textstring** "Ver PDF" → "Descargar Excel".
4. **Continuar E2E** que no llegué a cubrir esta noche:
   - Tab Manual: conciliar 1 par real (banco + programa) y verificar
     que el match se graba contra el real correcto (no por el bug del
     idx en la rama de manual).
   - Tab Transferencias: el extracto que cargué dio 0 — porque pocas
     filas del banco tienen `numreferencia` que matchee con
     `no_cheque`/`doc_banco` en /cheques.
   - Subir un extracto con depósitos cheque y verificar que aparecen
     en Transferencias por doc.
