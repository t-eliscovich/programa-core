# Bug Hunt Exhaustivo — Resultado 2026-05-16

Sesión autónoma de búsqueda de bugs por toda la app. **3 bugs nuevos encontrados y arreglados, 8 issues de data quality legacy reportados.**

## Resumen

| Categoría | Cantidad |
|---|---|
| **Bugs reales de código arreglados** | 3 (G, H, I) |
| **Data quality (legacy DBF, no es bug código)** | 8 grupos |
| **Tablas escaneadas** | 44 |
| **Tests de integridad ejecutados** | ~30 |

---

## 🔥 Bugs nuevos arreglados

### Bug G — Reverso cheque no limpia `chequesxfact`
**Síntoma:** Al reversar un cheque, las aplicaciones en `chequesxfact` quedaban vivas apuntando a un cheque con stat='X'. Consecuencia: el detalle de factura mostraba "Cheque XXX aplicado $YY" aunque el cheque ya estaba anulado.

**Detección:** Mi mega-scan encontró 2 chequesxfact huérfanos (TEST001, TESTA01 — mis tests de hoy).

**Fix:** `modules/cheques/queries.py:reversar()` ahora hace `DELETE FROM chequesxfact WHERE id_cheque=%s` al final del reverso. Limpieza manual de los 2 huérfanos ya ejecutada.

---

### Bug H — Cheques nuevos Z se crean con `fechaing=today`
**Síntoma:** Al crear un cheque nuevo via `/cheques/nuevo`, el INSERT incluye `fechaing=CURRENT_DATE`. Pero la convención canónica dice que `fechaing` debe estar NULL hasta que el cheque pase por banco (stat B/A/1/2/3/R/D). Para cheques Z (cartera) NUNCA debió tener fechaing.

**Detección:** Mi scan encontró 985 cheques Z con `fechaing` no NULL. De esos, 979 son DBF legacy y 6 son nuevos creados por este bug (incluyendo mis tests CH-T1A/B/C).

**Fix:** `modules/cheques/queries.py:1301` cambia `CURRENT_DATE` → `NULL` en el INSERT default.

**Impacto:** Para datos nuevos a partir de ahora, sólo cheques que efectivamente pasaron por banco van a tener fechaing. La columna "Depositado" de `/cheques` (que filtra por stat ∈ B/A/1/2/3/R) seguirá funcionando como antes.

---

### Bug I — `fechad` domingo no se shiftea a lunes en alta
**Síntoma:** Paridad con ALTAS.PRG L119 dice "si fechad cae domingo, shift a lunes". Esta regla se aplicaba en EDIT (línea 115 de queries.py) pero NO en ALTA.

**Detección:** Mi scan encontró 3 cheques en cartera con fechad domingo:
- id=1641 fechad 17/05/2026 (domingo)
- id=1648 fechad 24/05/2026 (domingo)
- id=1900 fechad 14/06/2026 (sábado en realidad? — verificar)

**Fix:** `modules/cheques/queries.py` (alta): ahora `fechad = _domingo_a_lunes(fechad)` antes del INSERT.

---

## 📊 Data quality (NO es bug de código)

### 1. 237 facturas legacy en stat='T' con saldo>0 (suma $348.043)
Todas son `usuario_crea='dbf-import'`. Daniela las marcó como T (cobradas) en el DBF pero el saldo no se redujo correctamente. **Decisión necesaria:** ¿el balance las cuenta como cartera o como cobradas?

### 2. 781 facturas con saldo<0 (sobre-pago, suma -$534k)
Distribuidas: 586 Z, 122 A, 73 T. Probable que sean facturas con anticipos legacy aplicados.

### 3. 812 facturas con abono>importe (matemáticamente imposible)
Mismo grupo que las anteriores. Inconsistencia legacy DBF.

### 4. 86 cheques con importe<=0 no anulados
- 12 stat B (depositados) suma -$21k
- 72 stat Z (cartera) suma -$83k → cartera con importes negativos
- 2 stat C (legacy)

Probable que sean cheques-espejo de anticipos (CONCEPTO=9999 del DBase).

### 5. 56 cheques con stat fuera del set canónico
- 18 stat='W' (legacy)
- 38 stat='C' (legacy)

Deberían documentarse o normalizarse.

### 6. 45 clientes duplicados por nombre
Ya identificado en audit anterior. Ej: AIDA GUANUCHI (AIG/AI7), AJP ACP (CL3/CLR/CL2), etc.

### 7. 27 mov_doble reversado sin id_reverso (Bug A residual)
Movs reversados ANTES del fix de Bug A. Quedaron así. Bug A está fixed pero los 27 viejos no se backfillean automáticamente. Opción: script de backfill que linkee originales→reversos buscando por origen/destino/importe matching.

### 8. 7 caja saldo<0 históricos (abril 2026)
Caja arrancó negativa en algún punto antes del check de validación. Concepts: "KK SU CAJA", "SU ADM CAJA", "CH.VDP", etc. Probablemente pre-fix de validación. No afecta operación actual.

### Otros minor:
- 1 trans bancaria con documento NULL (id 5038, banco 32, concepto "INICIAL" — saldo inicial banco)
- 985 cheques Z con fechaing legacy (979 dbf-import + 6 pre-fix H)
- 3 cheques fechad domingo (pre-fix I)
- 1 factura A con saldo~0 (debería ser T, inconsistencia leve)
- 2 caja egresos no clasificados este mes (PICH transferencia + REVERSO id 522)

---

## ✅ Áreas SIN issues

- Cheques B/A con fechaing NULL: **0** ✅
- Cheques duplicados (banco + no_cheque): **0** ✅
- Cheques con stat NULL: **0** ✅
- Facturas con importe=0 activas: **0** ✅
- Facturas sin cliente: **0** ✅
- xgast con importe<=0 vivos: **0** ✅
- xgast P con saldo=0: **0** ✅
- xgast A con saldo>0: **0** ✅
- trans bancarias con saldo NULL: **0** ✅ (Bug viejo 2026-05-11 sigue arreglado)
- Mov_doble huérfanos origen: **0** ✅
- Mov_doble id_original inexistente: **0** ✅
- Compra ANULADA con id_transaccion vivo: **0** ✅
- Compra con importe<=0 no anulada: **0** ✅
- Compras con id_transaccion huérfana: **0** ✅
- Clientes con stop='S' inexplicado: **0** ✅
- Caja con tipo fuera E/S/CB: **0** ✅
- Caja con saldo NULL: **0** ✅

---

## Áreas no auditadas (próxima sesión)

1. **Provisiones** (12 filas existentes) — no testeado el flujo de mensual auto
2. **Activos fijos** — esquema usa `valor_inicial`/`amortizacion`, no escaneé
3. **Dolares** (anticipos USD) — no testeado
4. **Retenciones emitidas/recibidas** — tabla `retencion` vacía
5. **SRI / Facturación electrónica** — no testeado
6. **Cobranzas** — calendario, flujo gráfico
7. **Imprimir / PDF** generación
8. **Búsqueda global ⌘K** funcional
9. **Cierre de período** + bloqueos por mes
10. **Bitácora** — registros auditables

---

## Estado actual de la DB

- Pichincha: $2,302,806.11
- Internacional: $3,861.19
- Caja: $28,078.19
- Cartera viva: $5,038,915.57
- Cheques en proceso: $1,834,657.25
- Posdat abierto: $2,028,672.52
- Capital acumulado: -$1,019,665.00

---

## Para commit + push de la sesión

```bash
cd "/Users/tamaraeliscovich/Documents/Claude/Projects/Programa Core" && \
git add -A && \
git commit -m "Bug hunt exhaustivo: fix G, H, I + audit completo

- G: reverso cheque ahora limpia chequesxfact
- H: cheques nuevos Z se crean con fechaing=NULL (no CURRENT_DATE)
- I: alta de cheque shiftea fechad domingo a lunes (paridad ALTAS.PRG)

Mega-scan de 44 tablas, ~30 checks de integridad. Findings:
- 8 grupos de data quality legacy (dbf-import) documentados
- 17 áreas con 0 issues ✅

Reporte: BUG_HUNT_RESULTADO.md" && \
git push
```
