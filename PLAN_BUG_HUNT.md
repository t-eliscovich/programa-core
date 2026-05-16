# Plan bug hunt exhaustivo — sesión autónoma 2026-05-16

Voy a ejecutar 10 fases buscando bugs en TODA la app. Cada bug que encuentre lo arreglo, lo testeo, y sigo. Reporto al final con: bugs encontrados, fixes aplicados, áreas auditadas, áreas restantes.

## Phase 1 — Edge cases de inputs en flujos críticos (~30 min)

- [ ] Cheque importe = 0 → debe rechazar
- [ ] Cheque importe negativo → debe rechazar (excepto modo anticipo)
- [ ] Cheque importe gigante ($999.999.999) → debe pasar o rechazar coherente
- [ ] Cheque sin N° (vacío) → debe rechazar
- [ ] Cheque con fechad domingo → debe shiftear a lunes (paridad ALTAS.PRG)
- [ ] Multi-cheque con N° duplicados → debe fallar
- [ ] Multi-cheque con clientes mixtos → debe rechazar (un cheque por cliente)
- [ ] Cobranza aplicada a factura ya T → debe rechazar o no aparecer
- [ ] Aplicar más plata que el saldo del cheque → debe rechazar
- [ ] Aplicar más plata que el saldo de la factura → debe permitir crédito (sobrepago)

## Phase 2 — Reportes vs DB raw (~45 min, alto valor)

Para cada reporte: comparo la cifra que muestra la UI contra SUM directo desde SQL. Si difieren, ahí hay un bug.

- [ ] `/informes/balance` — TOTC del balance vs SUM(factura.saldo WHERE stat IN Z/A)
- [ ] `/informes/deudas` — total deudas vs SUM(posdat.importe WHERE banc=0)
- [ ] `/informes/flujo` — chequeo proyección 30 días
- [ ] `/informes/ventas` — total ventas vs SUM(factura.importe del mes, no anuladas)
- [ ] `/informes/gastos` — matriz V1..V9 + amortización
- [ ] `/informes/cartera` — totales por cliente
- [ ] `/informes/estado_cuenta` cliente con muchos movs — saldo final consistente
- [ ] `/informes/resultados` — utilidad anual
- [ ] Bancos saldos = walk-forward correcto
- [ ] Stock por etapa = histórico

## Phase 3 — Anulaciones cross-table (~30 min)

- [ ] Anular factura CON cheque aplicado activo → debe rechazar
- [ ] Anular factura con cheque ya reversado → debe permitir
- [ ] Anular cheque que pagó factura → factura vuelve a Z saldo
- [ ] Anular compra con posdat → posdat se borra
- [ ] Anular compra ya pagada → debe rechazar
- [ ] Anular gasto pendiente (P) con posdat hermana → posdat se borra
- [ ] Reversar transferencia → ambos bancos vuelven a saldo original
- [ ] Reversar depósito → cheque vuelve a Z + banco baja
- [ ] Anular cheque endosado → compra creada se borra/anula

## Phase 4 — Validaciones de entrada y seguridad (~20 min)

- [ ] SQL injection: `'; DROP TABLE--` en buscadores → debe escapar
- [ ] XSS: `<script>alert(1)</script>` en conceptos → debe escapar
- [ ] Importes con coma `1.234,56` vs punto `1234.56`
- [ ] Fechas mal formateadas
- [ ] Strings unicode (ñ, acentos)
- [ ] Strings vacíos en required fields
- [ ] Strings demasiado largos (>200 chars)
- [ ] CSRF token reutilizado entre forms

## Phase 5 — Búsquedas y filtros (~20 min)

- [ ] Búsqueda global ⌘K con varios términos
- [ ] Filtros de fecha out-of-range (1900, 2099)
- [ ] Combinación múltiple de filtros
- [ ] Sort por cada columna en cada listado
- [ ] Pagination si existe

## Phase 6 — Stock + compras (~20 min)

- [ ] Compra que afecta stock (kg)
- [ ] Devolución de factura (kg negativo)
- [ ] Stock consistency: sum xfactura por tipo = stock por etapa?
- [ ] Compras tipo H,K,Q,C,A → afectan distinto

## Phase 7 — Cheques edge (~30 min)

- [ ] Postergación cheque
- [ ] Rebote chain 1→2→3→R con stop cliente automático
- [ ] Cheque depositado luego anulado
- [ ] Cheque a Daniela
- [ ] Endoso y reverso del endoso

## Phase 8 — Capital/retiros edge (~15 min)

- [ ] Aporte $0 / negativo
- [ ] Retiro $ > caja disponible
- [ ] Movimiento en mes cerrado (período)

## Phase 9 — Caja edge (~15 min)

- [ ] Caja saldo < 0 (debe rechazar)
- [ ] Egresos sin clasificar
- [ ] Concept parser con casos raros ("CC PINTURA" prov vs gasto)

## Phase 10 — Sistema check (~15 min)

- [ ] Re-correr `check_salud_dia.py`
- [ ] Re-correr audit C mov_doble
- [ ] Saldos invariantes (caja, bancos, stock, capital)
- [ ] Verificar que mis tests no rompieron nada

---

Cada bug encontrado lo agrego como task. Cuando termine: BUG_HUNT_RESULTADO.md con todo.
