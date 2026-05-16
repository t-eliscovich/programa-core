# Runbook — Sync DBF → Postgres

**Status:** activo desde 2026-04-30. Herramienta de TRANSICIÓN — se usa mientras corren dBase y Programa Core en paralelo. Cuando se retire el dBase, archivar.

## Contrato del proceso

Mientras el dBase legacy siga en producción y Programa Core esté en modo "shadow" (lectura), los datos vivos viven en los `.DBF`. Para que el balance/resultados/conciliación reflejen los números actuales, hay que **importar periódicamente** los DBFs frescos a Postgres.

**Vos hacés (ya):** copiar los `.DBF` actualizados desde la PC del dBase a:

    /Users/tamaraeliscovich/Documents/INTELA copy/Files/

**El script hace (cuando lo ejecutás):**

1. Lee cada `.DBF` que esté presente en esa carpeta.
2. **Si el DBF está**: TRUNCATE de la tabla Postgres correspondiente + INSERT de todas las filas (en una transacción, idempotente).
3. **Si el DBF NO está**: Postgres conserva su data ("usá los viejos").
4. Cada tabla en su propia transacción: si una falla, las otras siguen.

## Flujo cotidiano

```bash
# 1. Copias los DBFs frescos al folder (a mano, ya lo hacés).
#    /Users/tamaraeliscovich/Documents/INTELA copy/Files/*.DBF

# 2. Antes de tocar nada, ver qué pasaría:
make sync-dbf-dry-run
# → te lista cuántas filas tiene cada DBF, sin escribir.

# 3. Si pinta bien, sincronizás:
make sync-dbf

# 4. Refrescás /informes/balance en el navegador y los números cuadran.
```

Sin Makefile (mismo resultado):

```bash
python scripts/import_dbf.py --dry-run         # safe inspection
python scripts/import_dbf.py                    # ejecutar
python scripts/import_dbf.py --list             # lista mapping
python scripts/import_dbf.py --only=FACTURAS.DBF,CHEQUES.DBF   # parcial
python scripts/import_dbf.py --source-dir=/path/otro            # carpeta alternativa
```

## Tabla de mapping

| DBF | Tabla Postgres | Criticidad | Para qué |
|---|---|---|---|
| **PICHINCH.DBF** | `scintela.transacciones_bancarias` | SUPER | BANCOS Pichincha |
| **HISTORIA.DBF** | `scintela.historia` | SUPER | VSTO, VQX, PATANT, USUTI |
| **POSDAT.DBF** | `scintela.posdat` | SUPER | PASIVOS + POS1/POS2 |
| **ACTIVOS.DBF** | `scintela.activos` | CRITICO | UMAQ + UACT |
| **CHEQUES.DBF** | `scintela.cheque` | CRITICO | TOTC, estado de cuenta |
| **FACTURAS.DBF** | `scintela.factura` | CRITICO | TOTF, cartera, estado de cuenta |
| **CAJA.DBF** | `scintela.caja` | CRITICO | SALCAJ |
| **DOLARES.DBF** | `scintela.dolares` | CRITICO | ANTICIPOS |
| **INICIALE.DBF** | `scintela.iniciales` | CRITICO | KGPRO, PRETEJ/PRETIN/PREADM/PRETOT |
| **COMPRAS.DBF** | `scintela.compra` | útiles | Listado compras |
| **FLUJO.DBF** | `scintela.flujo` | útiles | Panel /informes/flujo/grafico |
| **RETIROS.DBF** | `scintela.retiros` | CRITICO | URET del balance + dividendos por socio |
| **TINTO.DBF** | `scintela.tinto` | CRITICO | COL.QUI. + KR del panel Resultados (batches de tintura) |

**No mapeados (intencional):** ENTRADAS.DBF (marcaje horario de empleados — alcance de `formulas_app`, no del módulo financiero), RETEN.DBF (0 registros — sólo header dBase, retenciones se manejan vía `scintela.retencion` desde compras).

## Por qué es seguro

- **Cada tabla en su propia transacción** (`BEGIN; TRUNCATE; INSERT...; COMMIT`). Si la inserción de una factura falla, esa transacción se rollbackea y las otras tablas siguen.
- **Idempotente**: corrél N veces, mismo resultado.
- **Si un DBF falta**, no se toca esa tabla en Postgres ("si no te lo pasé, usá los viejos").
- **Audit trail**: cada fila insertada queda con `usuario_crea = 'dbf-import'` para que se sepa de dónde vino.
- **No corre en producción accidentalmente**: si `DB_HOST` apunta a RDS o `ENV=production`, el script aborta a menos que exportes `I_KNOW_THIS_IS_PROD=1`.

## Encoding y gotchas

- Los DBFs vienen en **CP850** (encoding histórico del dBase español). El script lo declara explícitamente. Si ves caracteres raros (ñ, á, etc.), reportar.
- Los DBFs traen el campo `ST T` con espacio raro en DOLARES — el mapper acepta cualquier variante (ST, ST_T, "ST T").
- INICIALE.DBF guarda mes como string ('ABR'); el mapper lo traduce a `mesnum` int (4) además de mantener `mesnom`.
- PICHINCH (banco Pichincha) tiene `no_banco` asignado por **lookup en `scintela.banco`** — si no encuentra "PICHINCHA" en ningún banco, default a `no_banco=1` (convención del PRG legacy).
- TRUNCATE con `RESTART IDENTITY CASCADE` resetea las secuencias de los `id_*`. Las FKs se respetan vía CASCADE.

## Después de cada sync, verificar:

```bash
# 1. Math check del balance — falla si las cuentas no cuadran
python -m pytest tests/test_balance_conciliacion.py -q

# 2. Smoke test en /informes/balance (browser):
#    - El banner ámbar de advertencias (si aparece) no es nuevo
#    - La conciliación: todas las filas en ✓ verde
#    - BANCOS arriba == TOTAL BANCOS del detalle
```

## Si una tabla falla en el sync

El script imprime `✗ NOMBRE.DBF [crit] ERROR: <razón>`. Las otras tablas siguen cargándose. Para diagnosticar:

```bash
# Aislar el fallo
python scripts/import_dbf.py --only=NOMBRE.DBF

# Ver el DBF crudo
python -c "import dbfread; t = dbfread.DBF('/path/a/NOMBRE.DBF', encoding='cp850'); print(t.field_names); [print(r) for r in list(t)[:3]]"
```

## Tests que blindan el flujo

`tests/test_import_dbf.py` — 10 tests, garantizan:

- `BALANCE_CONCEPTS` y `TABLE_MAP` no tienen duplicados.
- Cada entry de `TABLE_MAP` tiene `pg_table`, `mapper`, `criticidad`, `descripcion`.
- Cada mapper tolera dict vacío sin explotar.
- `usuario_crea = 'dbf-import'` siempre presente (audit obligatorio).
- Mappers individuales contra casos reales (factura, pichincha, iniciales, dolares).
- Los 9 DBFs críticos para el balance están todos mapeados.
- `_lookup_no_banco_pichincha` defaultea a 1 cuando no hay banco con ese nombre.

## Cuando se retire el dBase

Mover `scripts/import_dbf.py` a `scripts/_archive/`. Eliminar dependencia `dbfread` del `requirements.txt`. Actualizar este runbook con el "from-now-on, Postgres es la única fuente de verdad".

## Files

- `scripts/import_dbf.py` — el script.
- `tests/test_import_dbf.py` — los 10 tests del contrato.
- `Makefile` — targets `sync-dbf` y `sync-dbf-dry-run`.
- Este runbook.
