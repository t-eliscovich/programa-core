# Contrato de la Conciliación del Balance

**Status:** activo desde 2026-04-30
**Owner:** quien toque el balance es responsable de mantenerlo

## Por qué existe este contrato

El balance (`/informes/balance`) muestra totales (CHEQUES, FACTURAS, PASIVOS, BANCOS, etc.) que el gerente compara contra los listados de los módulos individuales (`/cheques`, `/facturas`, `/posdat`, etc.). Si los totales no cuadran con lo que ve en los listados, el balance pierde credibilidad. Y los filtros divergen sin que nadie se entere — porque el balance suma con un criterio (heredado del legacy `INFORMES.PRG`) y los módulos individuales filtran con otro (UX moderno).

La **conciliación** es un panel en `/informes/balance` (abierto por default) que muestra cada componente del balance al lado del total que sale del módulo correspondiente, con ✓ si cuadran, ✗ + diff si no. Más un desglose visible y una nota explicando la fórmula. Más documentación de qué entra y qué no.

Este contrato garantiza que la conciliación se mantenga viva. Si alguien cambia una fórmula del balance sin actualizar la conciliación, **CI bloquea el merge**.

## Cómo funciona

### 1. Registro central — `BALANCE_CONCEPTS`

En `modules/informes/queries.py`:

```python
BALANCE_CONCEPTS: tuple[str, ...] = (
    "CAJA",
    "BANCOS",
    "CHEQUES (TOTC)",
    "FACTURAS (TOTF)",
    "ANTICIPOS",
    "MAQ/EQUIP. + TERR/EDIF/INS.",
    "STOCK MP+PROD. + STOCK QUI. + UTILIDAD",
    "PASIVOS (TOTP)",
    "DIVID. (URET)",
)
```

Esta lista es **la fuente de verdad** de qué componentes tiene el balance. El orden importa — replica el flujo del dBase (Activo arriba, Pasivo abajo).

### 2. Estructura obligatoria de cada fila — `CONCILIACION_REQUIRED_KEYS`

```python
CONCILIACION_REQUIRED_KEYS = frozenset([
    "concepto",  # str — nombre exacto en BALANCE_CONCEPTS
    "balance",   # float — monto que el balance muestra
    "modulo",    # float — monto que sale del módulo correspondiente
    "match",     # bool — True si abs(diff) ≤ 0.5
    "diff",      # float — balance - modulo (firmado)
    "detalle",   # list[tuple[str, value]] — desglose para auditar
    "nota",      # str — explicación, mínimo 20 chars; idealmente cita PRG
])
```

### 3. Self-check en runtime

`conciliacion_balance()` valida al final que las filas emitidas coinciden con `BALANCE_CONCEPTS` y que cada fila tiene todas las llaves requeridas. En `ENV=development` levanta `AssertionError`. En `ENV=production` loguea error pero no rompe la página (best-effort).

### 4. Tests automáticos — `tests/test_balance_conciliacion.py`

14 tests que cubren:

- `test_balance_concepts_es_inmutable_y_completo` — valida la constante.
- `test_conciliacion_emite_exactamente_los_conceptos_de_balance` — emite uno por concepto, en orden.
- `test_cada_fila_tiene_llaves_requeridas` — estructura.
- `test_balance_y_modulo_son_numericos` — tipos.
- `test_match_es_bool_y_consistente_con_diff` — invariante interno.
- `test_detalle_es_lista_de_tuplas` — estructura del desglose.
- `test_nota_documenta_origen_o_diferencia` — nota mínimo 20 chars.
- `test_facturas_balance_y_modulo_cuadran_por_construccion` — TOTF debe matchear siempre (mismo filtro).
- `test_cheques_balance_es_suma_de_stats_que_entran_a_totc` — invariante TOTC.
- `test_bancos_cuadra_por_construccion` — invariante BANCOS.
- `test_bancos_no_se_pierde_si_no_banco_no_es_1_o_2` — regresión: no hardcodear IDs.
- `test_self_check_detecta_drift` — el self-check funciona.
- `test_self_check_detecta_falta_de_llave` — el self-check funciona.
- `test_informe_balance_incluye_conciliacion` — el dict del balance trae el panel.

CI corre `pytest -q` en cada push. Si cualquiera falla, **no podés mergear**.

## Cómo agregar un componente nuevo al balance

Por ejemplo, si querés agregar "INVERSIONES FINANCIERAS" como nuevo activo:

1. **Agregar a `BALANCE_CONCEPTS`** (orden: donde corresponda visualmente):

   ```python
   BALANCE_CONCEPTS = (
       ...
       "INVERSIONES FINANCIERAS",
       ...
   )
   ```

2. **Implementar la query** en `informes/queries.py`:

   ```python
   def inversiones_financieras() -> float:
       row = db.fetch_one("SELECT COALESCE(SUM(valor), 0) AS total FROM scintela.inversiones WHERE activo=true")
       return float(row["total"] or 0)
   ```

3. **Sumarlo a `informe_balance()`**:

   ```python
   inv = inversiones_financieras()
   totl = subt + vsto + vqx + activos["umaq"] + activos["uact"] + _uret + _antic + inv
   ```

4. **Emitir la fila en `conciliacion_balance()`**, en el orden correcto:

   ```python
   out.append({
       "concepto": "INVERSIONES FINANCIERAS",
       "balance": inv,
       "modulo":  inv,  # o el total del módulo correspondiente
       "match":   True,
       "diff":    0.0,
       "detalle": [
           ("Activas", inv),
           ("Histórico de cancelaciones", ...),
       ],
       "nota": "PRG NO TENÍA esto. Fuente: scintela.inversiones, filtro activo=true.",
   })
   ```

5. **Mostrarlo en `balance.html`** — agregar la fila al panel del activo.

6. **Actualizar el test** `test_balance_concepts_es_inmutable_y_completo` con el nuevo concepto en `esperados`.

7. **Correr `pytest tests/test_balance_conciliacion.py`** — los 14 tests deben pasar.

8. **Update `docs/SKILL_ADDENDUM_BATCH_NN.md`** documentando el cambio.

## Qué NO hacer

- **No** hardcodear `if no_banco == 1 then ... elif no_banco == 2 then ...` para sumar bancos. Tu DB puede tener IDs distintos al dBase. Sumá todos. La regresión 2026-04-30 (BANCOS = 0 cuando Pichincha tenía no_banco=3) ya quedó cubierta por test.
- **No** silenciar el self-check. Si rompe, fixealo correctamente — no lo "evites" agregando un try/except.
- **No** mostrar "stored vs computed" en dos columnas paralelas en la UI. Una cifra resuelta + advertencia textual cuando convergen mal. El gerente quiere ver UN saldo, no auditar.
- **No** filtrar bancos con saldo 0 en el detalle SI tienen movimientos — eso fue otro bug (el detalle desaparecía y la cifra del activo no cuadraba con la suma visible). Hoy se ocultan solo los que tienen `saldo == 0`, no los que tienen `n_transacciones == 0`.

## Estado actual (2026-04-30)

| Concepto | Filtro PRG | Coincide con módulo? | Notas |
|---|---|---|---|
| CAJA | último saldo en caja | ✓ | trivial |
| BANCOS | SUM(todos los bancos) + POS1 + POS2 | ✓ por construcción | post-fix sin hardcoded ID |
| CHEQUES (TOTC) | stat ∈ {Z,1,2,3,P,D} | ✓ con la suma de stats; ✗ con `/cheques?estado=cartera` (que muestra solo Z) | esperado, ver nota |
| FACTURAS (TOTF) | stat ∈ {Z,A} ∧ saldo>0 | ✓ con `/facturas?vista=cartera` | filtro idéntico |
| ANTICIPOS | st NULL OR vacío en dolares | ✓ | trivial |
| MAQ/EQUIP. + TERR/EDIF/INS. | activos por tipo M/C/K vs I | ✓ | trivial |
| STOCK MP+PROD. + STOCK QUI. + UTILIDAD | historia (último cierre) | ✓ | snapshot mensual |
| PASIVOS (TOTP) | banc ≠ 9 | ✗ esperable | balance no filtra importe>0; módulo /posdat sí |
| DIVID. (URET) | retiros últimos 63d | ✓ | trivial |
