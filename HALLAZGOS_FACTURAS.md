# Hallazgos edge cases facturas — 2026-05-16

## ⚠️ Issues de datos legacy

### 1. Factura duplicada AJ2 numf=169945
Dos facturas idénticas mismo cliente, mismo número, mismo importe, fechas distintas:
- `id=1029` fecha 02/03/2026 $196.15
- `id=1794` fecha 22/03/2026 $196.15

Probable doble carga al importar DBF. **Acción sugerida:** revisar manual y anular una.

### 2. Factura $/kg anómalo (sub-precio)
`id=1190 numf=170226 cli=ADG`: 41,35 kg por $18,84 = **$0,46/kg**. Normal es $5-10/kg. Probable typo de entrada.

### 3. Aplicación cheque negativa (-$37.64)
`chequesxfact id=484`: cheque #1920 (cartera) aplicado a factura #2 (totalizada) con importe -$37.64. Es un mecanismo legítimo del legacy DBF para anticipos / sobre-pagos. **No es bug.**

## 📊 Business intel — exposición de cobranza

### Top 5 clientes con MAYOR riesgo (factura abierta >> cheques en cartera)

| Cliente | Facturas abiertas | Cheques cartera | **Gap descubierto** |
|---|---|---|---|
| **BED** | $328.182 | $24.377 | **-$303.805** |
| **TNZ** | $157.811 | $43.000 | -$114.811 |
| **LAL** | $105.256 | $4.230 | -$101.026 |
| **BAN** | $77.573 | $498 | -$77.075 |
| **JOH** | $77.779 | $1.700 | -$76.079 |

### Clientes BIEN cubiertos (cheques ≥ deuda)

| Cliente | Facturas | Cheques | Cobertura |
|---|---|---|---|
| EEU | $128.227 | $138.931 | +108% ✅ |
| RRV | $114.255 | $122.451 | +107% ✅ |
| CLR | $101.753 | $112.461 | +110% ✅ |

### 50 facturas vencidas hace +1 año (cobranza vieja)
Top deudores:
- **CVH numf=101719**: $9.178 desde agosto 2022 (4 años) 🔴
- ALT numf=110543: $4.357 desde feb 2023
- CVH numf=101859: $2.005 desde ago 2022
- OMM (4 facturas legacy 2025): ~$5.000 total
- MJM numf=0: $1.334 desde nov 2021 (5 años!) 🔴

## 📈 Distribución por antigüedad

| Antigüedad | Facturas vivas |
|---|---|
| Últimos 30 días | 1.659 (35%) |
| 30-90 días | 2.295 (47%) |
| 90-365 días | 599 (12%) |
| Más de 1 año | 56 (1%) |

## ✅ Validaciones que pasan

- 0 facturas con fecha futura
- 0 facturas con vencimiento < fecha
- 0 facturas con vencimiento NULL
- 0 aplicaciones over-apply (importe > saldo factura)
- 0 aplicaciones huérfanas cliente
- 374 facturas con kg=0 (servicios, legítimas)
- 253 facturas kg<0 (devoluciones, legítimas)

## 💡 Recomendaciones operativas

1. **Acción inmediata**: revisar AJ2 169945 — anular la duplicada
2. **Decidir**: ¿qué hacer con las 50 facturas vencidas hace +1 año? Pasar a incobrable o intentar cobrar
3. **Priorizar cobranza**: BED $304k descubierto = mayor riesgo
4. **Consolidar clientes duplicados**: 45 clientes con mismo nombre, distinto código (ya identificado en audits previos)
