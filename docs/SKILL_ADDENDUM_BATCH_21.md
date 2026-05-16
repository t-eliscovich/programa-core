# SKILL ADDENDUM — Batch 21 (2026-04-30)

Debug de "patrimonio neto no cuadra con el dBase". TMT abrió `/informes/balance`,
vio PATR ≈ $13M cuando en el dBase rondaba $20M, y preguntó "¿qué bases me faltan?".
La respuesta no era una base — era un bug de una línea + un mapping de columnas
mal documentado en el skill.

Apilable en `programa-core/SKILL.md` como "Session notes — 2026-04-30 (batch 21)".

---

## Bug encontrado y arreglado: `historia.stock` ≠ `historia.ustock`

`modules/informes/queries.py::informe_balance()` línea 1072 leía la columna
equivocada para VSTO:

```python
# ANTES (mal — leía kg en vez de US$)
vsto = float(hist.get("stock") or 0)

# DESPUÉS
vsto = float(hist.get("ustock") or 0)
```

**Datos reales del incidente** (snapshot 2026-04-10):

- `historia.stock`  = `2,580,498.00` ← KG (interpretado como $ por el bug)
- `historia.ustock` = `8,387,238.00` ← VSTO real en US$
- Diferencia recuperada: **$5,806,740**

PATR live antes del fix: $12.9M. PATR live después del fix: $18.7M.
PATANT del último cierre: $19.86M. La brecha residual de ~$1.1M es churn
normal de los 20 días desde el cierre del 10/04 (ventas + cobranzas + compras
+ retiros del mes en curso). Eso **es lo correcto** mid-mes; el balance live
SIEMPRE difiere de PATANT entre cierres.

## Mapping correcto de columnas en `scintela.historia`

Esta tabla es la trampa del proyecto. Los nombres son del dBase de los '90 y
mezclan kg con US$ sin un sufijo evidente. Dejá esto a mano:

| Columna PG    | Significado                       | INFORMES.PRG (escritor)              |
|---------------|-----------------------------------|--------------------------------------|
| `stock`       | KG totales en stock (HI+TJ+PF)    | `STOK = HI+TJ+PF` (línea 349)        |
| `ustock`      | **VSTO en US$** (valor stock)     | `REPLA USTOCK WITH VSTO` (línea 1346)|
| `kcom/ucom`   | KG / US$ comprados en el mes      | línea 1345                           |
| `ktej/utej`   | KG / US$ tejeduría                | línea 1345                           |
| `ktin/utin`   | KG / US$ tintorería               | línea 1345                           |
| `kvent/uvent` | KG / US$ ventas                   | línea 1345                           |
| `uqui`        | VQX en US$ (colorantes/químicos)  | `REPLA UQUI WITH VQX`                |
| `patrimonio`  | **PATR-URET** (neto post retiros) | `REPLA PATRIMONIO WITH PATR-URET`    |
| `usret`       | URET (retiros del mes en US$)     | `REPLA USRET WITH URET`              |
| `usuti`       | UTILIDAD del mes (PATR-PATANT)    | `REPLA USUTI WITH UTILIDAD`          |
| `cart`        | TOTC + TOTF                       | `REPLA CART WITH TOTC+TOTF`          |
| `banco`       | SALBANC + SALCAJ                  | `REPLA BANCO WITH SALBANC+SALCAJ`    |
| `deuda`       | TOTP                              | `REPLA DEUDA WITH TOTP`              |
| `anticipos`   | ANTIC                             | `REPLA ANTICIPOS WITH ANTIC`         |
| `maquinaria`  | UMAQ                              | `REPLA MAQUINARIA WITH UMAQ`         |
| `realty`      | UACT                              | `REPLA REALTY WITH UACT`             |

**Regla mnemotécnica:** en `historia`, todo lo que empieza con `u` está en
**US$** (ustock, uqui, ucom, utej, utin, uvent, usret, usuti). Lo que **no**
empieza con `u` está en **kg** (stock, kcom, ktej, ktin, kvent). Excepción:
`patrimonio`, `banco`, `cart`, `deuda`, `anticipos`, `maquinaria`, `realty`,
`dolar`, `costo`, `gasto`, `gstotal`, `retiro` también son US$ aunque no
empiecen con u.

Los comentarios del header en `modules/informes/queries.py` (líneas 15-25)
dicen `historia.stock = VSTO` — eso es **falso**. Cambiarlo a `ustock`
queda en el backlog XS.

## Bug menor #2 (no fixeado): visualización del PATRIM.NETO

El dBase imprime en pantalla `PATR - URET` (INFORMES.PRG línea 459):

```
@16,56 SAY "PATRIM.NETO   "+ STR(PATR-URET,8)
@17,56 SAY "DIVID.        "+ STR(URET,8)
```

El nuevo app muestra `PATR` directo. Diferencia con datos del 30/04: $452.402.

URET aparece dos veces en la fórmula del dBase: suma a TOTL (entra al
patrimonio) y se resta en el display. Net effect: cancela. Lo que ves en
la pantalla es esencialmente:

```
PATRIM.NETO_dBase = SUBT + VSTO + VQX + UMAQ + UACT + ANTIC - TOTP
```

(URET no aparece en el resultado final, sólo en el "DIVID." separado.)

Para paridad exacta con el dBase: exponer `patr_neto = patr - uret` desde
`informe_balance()` y usarlo en `balance.html`. URET sigue saliendo como
"Retiros del mes" / "DIVID." separado. **TMT decide si quiere paridad o
prefiere "PATR antes de retiros" + "URET separado" como está hoy.**

## Bug #3 (no fixeado): `scintela.iniciales` con seed de 2000-2001

Las 12 filas de la tabla son todas de **agosto 2000 a julio 2001**. La
migración rellenó `mesnom` en texto (Aug, Sep, ..., Jul) pero `mesnum`
quedó NULL en todas. No hay filas del año en curso ni de los últimos 24
años.

**Implicación funcional:** la fórmula LIVE de VSTO/VQX del dBase
(INFORMES.PRG líneas 303-350) NO se puede recalcular en el nuevo app
porque depende de `iniciales` para HI0/TJ0/PF0/UM0/UK0/UQ0/UF0/VQ0 del
mes en curso. Hoy el balance usa el snapshot de `historia.ustock` y eso
es el comportamiento aceptado (≈ PATR del último cierre + churn).

**Para destrabar VSTO live (Fase 2.2 del backlog "live stock en kg"),
tres tareas:**

1. Re-importar `INTELA copy/Files/INICIALE.DBF` a `scintela.iniciales`
   con un script que rellene `mesnum` desde `mesnom` (mapping
   `Ene`→1, `Feb`→2, ..., `Dic`→12). El DBF en `Files/` es la versión
   de trabajo más reciente.
2. Implementar la fórmula del PRG líneas 303-350 en `vsto_live()` en
   `modules/informes/queries.py`. Inputs: fila de `iniciales` del mes
   actual (HI0, TJ0, PF0, UM0, UK0, UQ0, UF0, VQ0) + movimientos del
   mes (compras kg/$, kg a tejido, kg tejido, kg a tintura, kg
   tinturado, kg vendido).
3. `informe_balance()` llama a `vsto_live()` cuando hay datos de
   `iniciales` para el mes en curso, y cae al snapshot
   `historia.ustock` cuando no.

## Aclaración sobre el "files folder" que mencionó la usuaria

Es `/Users/tamaraeliscovich/Documents/INTELA copy/Files/` — la subcarpeta
donde están las **versiones más recientes** de los DBFs (la de la fábrica).
La carpeta raíz `INTELA copy/` tiene una segunda copia más vieja (probable
backup pre-migración).

Cuando TMT diga "te copio más archivos en mi carpeta files", es esa
subcarpeta. **No confundir** con `files/` bajo Programa Core (no existe).

DBFs en `INTELA copy/Files/`:
ACTIVOS, CAJA, CHEQUES, COMPRAS, DOLARES, ENTRADAS, FACTURAS, FLUJO,
**HISTORIA**, **INICIALE**, PICHINCH, POSDAT, RETEN, TINTO, XGAST.

DBFs solo en raíz (no en Files/):
ABOGCHEQ, ABOGFAC, GGAST, INTER, PASACLI, PROD, RETIROS, XVENT.

## Diagnóstico reusable: "el balance no cuadra"

Cuando alguien pregunte "el patrimonio neto da X y debería dar Y",
copy-paste esto:

```sql
-- 1) Componentes del balance en una pasada
SELECT 'TOTF' AS k, COALESCE(SUM(saldo),0)::numeric(14,2) AS v
  FROM scintela.factura WHERE COALESCE(saldo,0)>0 AND (stat IS NULL OR stat IN ('Z','A','',' '))
UNION ALL SELECT 'TOTC', COALESCE(SUM(importe),0)
  FROM scintela.cheque WHERE stat IN ('Z','1','2','3','P','D')
UNION ALL SELECT 'SALCAJ', COALESCE((SELECT saldo FROM scintela.caja
                          ORDER BY fecha DESC, id_caja DESC LIMIT 1),0)
UNION ALL SELECT 'UMAQ', COALESCE(SUM(valor),0)
  FROM scintela.activos WHERE tipo IN ('M','C','K')
UNION ALL SELECT 'UACT', COALESCE(SUM(valor),0)
  FROM scintela.activos WHERE tipo='I'
UNION ALL SELECT 'ANTIC', COALESCE(SUM(importe),0)
  FROM scintela.dolares WHERE st IS NULL OR st IN ('',' ')
UNION ALL SELECT 'URET', COALESCE(SUM(ret),0)
  FROM scintela.retiros WHERE fecha >= CURRENT_DATE - 63
UNION ALL SELECT 'TOTP', COALESCE(SUM(importe),0)
  FROM scintela.posdat WHERE COALESCE(banc,0)<>9;

-- 2) Snapshot historia (CUIDADO: ustock, NO stock)
SELECT fecha, ustock AS vsto, uqui AS vqx, patrimonio AS patant_minus_uret
  FROM scintela.historia ORDER BY fecha DESC LIMIT 1;

-- 3) Activos sin tipo (no entran a UMAQ ni UACT)
SELECT id_activo, descripcion, valor
  FROM scintela.activos
 WHERE tipo IS NULL OR TRIM(tipo)='' OR tipo NOT IN ('M','C','K','I');

-- 4) Bancos con movimientos
SELECT b.no_banco, b.nombre,
       (SELECT COUNT(*) FROM scintela.transacciones_bancarias t
         WHERE t.no_banco=b.no_banco) AS n_tx
  FROM scintela.banco b ORDER BY n_tx DESC NULLS LAST;
```

Si `historia.ustock` no aparece **muy** mayor a `historia.stock`, el bug
está en otro lado. Si los activos sin tipo suman > 1% del balance, fix
de tipos. Si solo Pichincha tiene transacciones, está bien — la fábrica
opera con un solo banco principal (TMT confirmó 2026-04-30).

## Anti-pattern documentado: nombres ambiguos en `historia`

Las columnas con nombre de moneda implícita (`stock` vs `ustock`,
`kcom` vs `ucom`) son una trampa heredada del dBase de los '90 (nombres
de 8 caracteres). **Hoy no las renombramos** porque hay 25 años de filas
históricas que dependen del schema.

**Pero la regla "u-prefix = US$, sin u = kg" tiene que estar en el primer
párrafo de cualquier documentación nueva sobre `historia`.** La omisión
de esta regla en el skill (hasta esta sesión) costó 30 minutos de debug
hoy y resultó en un app que reportaba 35% menos patrimonio del real.

## Backlog actualizado

- **[XS, nuevo]** Corregir el docstring en el header de
  `modules/informes/queries.py` líneas 15-25 — dice
  `historia.stock = VSTO`, debe decir `historia.ustock = VSTO`. La
  función `informe_balance()` ya está fixeada; falta sólo el comment.
- **[XS, nuevo]** Decidir si `PATRIM.NETO` muestra `PATR` o `PATR-URET`
  para paridad con dBase. Si sí: exponer `patr_neto = patr - uret`
  desde `informe_balance()` y reemplazar la binding en `balance.html`.
- **[S, promovido — ya estaba en Fase 2.2]** Re-importar `INICIALE.DBF`
  desde `INTELA copy/Files/` con script que rellene `mesnum` numérico.
  Sin esto no hay VSTO live ni proyecciones del mes (PROYEC en
  INFORMES.PRG).

## Files touched

```
modules/informes/queries.py    line 1072: hist.get("stock") → hist.get("ustock")
                                + comentario explicando el porqué
docs/SKILL_ADDENDUM_BATCH_21.md  NEW (este archivo)
```
