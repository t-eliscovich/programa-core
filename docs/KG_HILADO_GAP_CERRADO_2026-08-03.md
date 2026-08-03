# El gap de kilos del hilado — CASO CERRADO, no lo vuelvan a diagnosticar

**Si llegaste acá porque viste un gap de kilos contra el dBase (+97.673, +20.396
o parecido): ya está medido. Leé esto antes de proponer nada.**

Fecha: 03/08/2026. Instrumento: `/admin/debug-kg-por-mes` (sólo lectura, se
puede volver a correr cuando quieras).

---

## La hipótesis que circulaba

> *"La conversión repite los kilos por diseño, la haga quien la haga. Por eso la
> guarda tiene que estar adentro de `convertir_a_compra`. Y explica el gap de
> hilado que veníamos arrastrando en los compares contra el dBase (+97.673 kg,
> después +20.396). Era esto."*

Y de ahí salía la propuesta: **"una importación = una compra POR MES"**, uniendo
los 9 casos ya cargados.

## Qué dice el dato

### 1. La duplicación NO existe. 14 meses, cero casos.

`/admin/debug-kg-por-mes` mira, por cada `(prov, nº)` con más de una compra, a
qué importación fue a parar cada una. La reconstrucción (`kg_hilado_faltantes_mes`)
deduplica por importación, así que la ÚNICA vía por la que podría sumar dos
veces es que dos compras del mismo código caigan en **grupos distintos**.

Los 9 casos que se citaban — son exactamente éstos:

| mes | código | compras | importación | grupos | ¿duplica? |
|---|---|---|---|---|---|
| 2026-08 | AI 15 | 2 | IM-0000572 | 1 | **no** |
| 2026-07 | AC 25 | 2 | IM-0000549 | 1 | **no** |
| 2026-07 | AC 15 | 2 | IM-0000562 | 1 | **no** |
| 2026-07 | AC 16 | 2 | IM-0000545 | 1 | **no** |
| 2026-07 | MH 66 | 2 | IM-0000609 | 1 | **no** |
| 2026-07 | AC 92 | 2 | IM-0000576 | 1 | **no** |
| 2026-07 | AC 28 | 2 | IM-0000567 | 1 | **no** |
| 2026-07 | AI 14 | 2 | IM-0000564 | 1 | **no** |
| 2026-07 | AC 32 | 2 | IM-0000565 | 1 | **no** |

**Los nueve resuelven a UNA sola importación.** `meses_con_duplicacion` viene
vacío en los 14 meses.

Y el `kg` tampoco entra por la compra: `convertir_a_compra` recibe `kg=None`
tanto del botón manual (`modules/dolares/views.py:252`) como del automático
(`modules/importaciones/autobap.py:520`). Es la regla del 10/07 — *el kg vive en
el stock, no en la compra*. Las dos compras de AI 15 tienen `kg = 0` las dos.

### 2. Pero el número SÍ existe. Es éste:

| mes | kcom (balance) | Asinfo recibido | gap |
|---|---|---|---|
| 2026-08 | 11.289 | 22.579 | −11.289 |
| **2026-07** | 600.216 | 393.836 | **+206.381** |
| **2026-06** | 259.866 | 369.878 | **−110.011** |
| 2026-05 | 173.823 | 137.101 | +36.722 |
| 2026-04 | 0 | 162.991 | −162.991 |
| 2026-03 | 0 | 125.468 | −125.468 |
| 2026-02 | 0 | 144.828 | −144.828 |
| 2026-01 | 0 | 104.510 | −104.510 |
| 2025-12 | 0 | 970.178 | −970.178 |

**junio + julio = +96.369.** Contra los +97.673 citados difieren en 1.304: es el
mismo número.

### 3. La causa es otra: desfase de FECHA, no duplicación

`kcom` mide por **fecha de la compra**. `hilado_recibido_mes` mide por **fecha de
recepción**. Son dos cosas distintas y por eso no tienen por qué coincidir.

Y hay un motor que las separa a propósito: `autobap` convierte *"todo anticipo
cuya importación ya esté recibida"*, incluidos los viejos — su propio docstring
cita AC 77, **recibida el 22/12/25 con el anticipo cargado el 18/06/26**. Así que
julio absorbe kilos de mercadería que entró en abril, mayo y junio.

Se ve limpio en la tabla: de abril para atrás `kcom = 0` con recepciones de 100 a
970 mil kg. Esos kilos no desaparecieron — reaparecen en julio.

---

## Conclusiones

| afirmación | veredicto |
|---|---|
| "La conversión repite los kilos" | **refutado** — 14 meses, 0 duplicaciones |
| "Explica el gap contra el dBase" | **el gap existe, pero no por eso** |
| "Una importación = una compra por mes ARREGLA el gap" | **no** — no hay nada que deduplicar |
| "La guarda va adentro de `convertir_a_compra`" | **bien** — el principio es correcto |
| "El 'por mes' importa" | **bien, y por una razón mejor que la que dieron** |

**Lo que sí queda en pie del razonamiento ajeno:** el instinto del *"por mes"*
apunta a lo correcto desde otro ángulo. El problema de fondo no es duplicación,
es **a qué mes se le imputan los kilos** — que es justo lo que muestra la tabla.
Ellos lo intuyeron; el mecanismo que le pusieron encima no es el que hay.

**Un cuidado, para no fabricar el próximo diagnóstico equivocado:** que `kcom` no
coincida con Asinfo **no es por sí solo un defecto**. Miden dos cosas distintas
por dos fechas distintas. Si es un problema o no depende de para qué se usa
`kcom`, y ésa es una conversación aparte — **no** una excusa para unir compras.

---

## Antes de proponer un arreglo acá, corré esto

```
/admin/debug-kg-por-mes/?meses=14
```

Devuelve, por mes: `sum_compra_kg`, `reconstruido`, `kcom_que_usa_el_balance`,
`asinfo_recibido`, `gap_kcom_menos_asinfo` (con signo) y
`refs_con_varias_compras` con el flag `duplica_kilos`.

**Si `meses_con_duplicacion` viene vacío, no hay kilos duplicados.** Cualquier
propuesta que empiece por "los kilos se están contando dos veces" tiene que
mostrar primero un mes donde ese flag dé `true`.
