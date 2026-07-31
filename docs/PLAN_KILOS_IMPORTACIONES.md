# Plan — kilos de importación y tarifa del hilado

Escrito el 31/07/2026 al cierre de la jornada. Cada ítem trae **la evidencia que
lo respalda** (dato medido o línea de código) y **cómo se verifica que quedó
bien**. Lo que no está verificado está marcado como tal — hoy me equivoqué tres
veces por afirmar antes de medir, y este documento existe para que eso no se
repita.

---

## 0. Estado verificado hoy

Estas son las cosas que **medí**, no que supongo:

| hecho | cómo se verificó |
|---|---|
| Las recepciones de importación **no son parciales** | 18/18 en julio al 100 %, `indicador_tiene_recepcion_parcial=false`, creada y modificada a 1-3 s |
| Los kg recibidos **entran completos al stock** | 404.657 kg recibidos = 404.657 kg de Ingresos de HILADO |
| **No hay plata duplicada** en las compras de hilado | 33 compras de julio agrupadas por (proveedor, concepto): ningún importe repetido |
| El cruce de AC en julio **atribuye bien** | 24/24 compras a su propia importación, todas recibidas. $0 mal atribuido |
| Las compras de importaciones partidas llevan **la mitad de los kilos** | dBase: AC 25 = 48.988 kg / PC = 24.494 · AC 19 = 22.354 / 10.990 · MH 66 = 46.860 / 23.430 |
| Los **recargos repiten los kg** de la fila principal | AC 15, AC 16 y AC 25 aparecen dos veces con kg idénticos = **70.354 kg contados dos veces** |

---

## 1. El mapa: quién lee `compra.kg`

Esto es lo que hace que el problema importe, y es la parte que expliqué mal
antes. **Hay dos tarifas del hilado, no una.**

```
compras_mes_corriente()            ← SUM crudo de compra.kg / compra.importe
  modules/informes/queries.py:1663    SIN filtro de recepción
        │
        ├─ h_kcom, h_ucom                              queries.py:4456-4457
        │      │
        │      ├─ umx = h_ucom / h_kcom                queries.py:4511
        │      │     → el "$/kg comprado" que se mira en pantalla
        │      │
        │      ├─ KM / VM                              queries.py:4555-4556
        │      │     → Materia Prima del cuadro de resultados
        │      │
        │      └─ h_um = (h_ucom + (h_hilado − h_kcom) × um_anterior) / h_hilado
        │            queries.py:4772
        │            → LA TARIFA DEL HILADO, que valúa 1,9 M de kg
        │
        └─ (después) si _stock_fuente == "asinfo" y la valuación responde,
           h_um se PISA con mov_hilado_valuacion()      queries.py:4897-4905
```

**Consecuencia:** con Asinfo andando, `compra.kg` no toca la utilidad. **El día
que Asinfo no contesta, `h_um` de la línea 4772 queda en pie y sí la toca.**
Ese día ya pasó (29/07) y costó ~495.000 de utilidad falsa.

Por eso los kilos mal cargados no son cosmética: son la mecha.

---

## 2. El plan, en orden. El orden NO es opcional

### Paso 1 — Recargos (CAE / flete / seguro) con `kg = 0`

**Evidencia:** en julio, AC 15, AC 16 y AC 25 tienen dos filas cada una — la
compra principal y un recargo (2.241,70 / 2.769,49 / 2.813,87 / 15.441,06) — y
**el recargo repite los kg completos**. Total: 70.354 kg contados dos veces.

**Por qué va primero:** si se arregla el grupo antes que esto, AC 25 pasa de
3 × 24.494 a 3 × 48.988 = **146.964 kg** por 48.988 reales. Los dos defectos
viven en el mismo `SUM(compra.kg)` y se multiplican.

**Cómo se verifica:** `SUM(compra.kg)` de tipo H del mes tiene que bajar en
70.354 exactos, y el `$/kg comprado` de la pantalla tiene que subir en
consecuencia. `/admin/health/hilado-stock-debug` mide antes y después.

**Riesgo:** ninguno para la utilidad con Asinfo andando. Sí cambia el
`$/kg comprado` en pantalla — hacia el número correcto.

---

### Paso 2 — `promedio_hilado_usd_kg()` — **verificado hoy, es peor de lo que parecía**

**Evidencia (`modules/importaciones/service.py:586-588`):**

```python
if not kg or not imp:
    continue
total_kg += kg
```

El `continue` saltea la fila entera. En una importación partida, la mitad que
**no** tiene `importe_programa` (la `---1`, cuyo costo vive en la `---2`) se
descarta: **sus kilos no se cuentan**. Pero el $ del grupo sí se cuenta entero.

Resultado: **el $/kg sale casi al doble** para cada partida. Y el docstring de la
misma función dice *"los kg se suman de todas las filas recibidas"* — el
comentario y el código dicen cosas distintas.

**A qué afecta:** es el costo con que se valúa la **apertura** del stock en el
Flujo de producción.

**El arreglo:** sumar `kg` de todas las filas recibidas y el `$` una sola vez por
grupo — el mismo patrón que ya usan sus tres funciones hermanas
(`costo_hilado_recibido_mes`, `compras_hilado_recibidas_mes`,
`kg_stock_por_compra`). Es mover el `continue` para que sólo saltee el $.

**Cómo se verifica:** el $/kg tiene que caer dentro de la banda 2,7 – 3,4 y
coincidir con el que muestra el Flujo de producción.

---

### Paso 3 — Alarma de banda 2,7 – 3,4 US$/kg

**Evidencia:** hoy el balance sólo avisa si una compra tipo H tiene importe **sin
kg**. Con la **mitad** de los kilos no salta nada. Las cinco compras fuera de
banda (6,546 · 6,168 · 5,999 · 4,477 · 4,328) las encontré ordenando a mano.

**El arreglo:** al calcular el balance, ordenar las compras de hilado del mes por
`importe ÷ kg` y agregar a `diagnostico["advertencias"]` las que se salen de la
banda. Se muestra igual que los avisos `⚠ ASINFO` / `⚠ HILADO` que ya existen.

**Por qué va antes del paso 4:** porque es la red que avisa si el paso 4 sale
mal. Primero el detector, después la cirugía.

---

### Paso 4 — Los kilos de las importaciones partidas

**La regla final**, ya pasada por tres intentos de romperla:

> Se suman los kg de dos o más importaciones de Asinfo **sólo** cuando comparten
> las tres cosas: mismo `prov_cod_asinfo`, **misma `fp.fecha` exacta**, y misma
> nota base (`fp.descripcion` sin el sufijo `---N` ni el código entre
> paréntesis).

**Por qué NO alcanza el año** — el caso que mató la regla original:

| | fecha | nota |
|---|---|---|
| IM-0000622 | 2026-07-20 | ACMT/EXP/2026-27/8368 |
| IM-0000527 | 2026-02-11 | INV HY336-26-1 |

Las dos son AC 58, las dos son 2026, **no son mitades de nada**. Agrupar por año
les habría sumado los kilos de febrero a la compra de julio.

**Descalificadores — cualquiera ⇒ no agrupar y avisar:**

1. algún miembro trae rango (`numero_hasta`)
2. falta el sufijo `---N`, o los sufijos no son 1..N contiguos
3. el grupo tiene más de 3 miembros
4. algún miembro pudo quedar fuera del tope de 400 filas de Asinfo ⇒ **"grupo
   incompleto"**, no sumar (una suma parcial que cambia en cada corrida es peor
   que el número de hoy)
5. `importe ÷ kg_grupo` fuera de 2,7 – 3,4

**Dónde vive el cambio — esto es lo que evita romper lo que hoy anda:**

El `kg` de la fila **no se toca**. El grupo viaja en campos nuevos (`grupo_id`,
`grupo_kg`, `grupo_ims`, `grupo_completo`). Hay que pedirlo, nadie lo agarra por
accidente.

- **Usa `grupo_kg`:** `adjuntar_recepcion_asinfo`, `adjuntar_kg_asinfo_a_compras`,
  `kg_stock_por_compra`, `kg_hilado_faltantes_mes`, y el agrupador de `autobap`
  (que pasa a agrupar por `(cta, grupo_id)` en vez de `(cta, im_numero)`).
- **En esas cinco, el dedup pasa de `im_numero` a `grupo_id` en el mismo
  commit.** Usar `grupo_kg` con dedup por `im_numero` es la receta exacta del
  doble conteo.
- **NO lo usa:** `costo_hilado_recibido_mes`, `compras_hilado_recibidas_mes`,
  `promedio_hilado_usd_kg`, ni el KPI de `/importaciones`. **Esos cuatro ya suman
  bien fila por fila.**
- `grupo_kg` se calcula **siempre desde `importaciones_kg`**, nunca acumulando
  sobre `r["kg"]` — las filas de Asinfo están cacheadas 5 minutos y se comparten:
  una pasada que acumula se aplica dos veces en la segunda visita.

**El error que estuvimos a punto de cometer, con número:** si en vez de esto
escribíamos el total del grupo en el kg de **cada** fila, el $/kg de julio caía
de 3,0271 a **2,3428** y el stock valuado bajaba **284.772**. Ese sí movía la
Utilidad Real, para el lado equivocado.

---

### Paso 5 — `autobap` guard 9, y la UI colapsada

**Evidencia:** hoy una importación partida siempre da 2 candidatas plausibles ⇒
se declara ambigua ⇒ **no se convierte** y sale *"falta ponerle el año"* en la
campanita (`autobap.py:394-396`). Poner el año no arregla nada: el problema es
que las dos candidatas son el **mismo grupo**.

**El arreglo:** agrupar las candidatas por `grupo_id` antes de contarlas.

**Y en pantalla:** en `/importaciones` y en convertir-lote, las dos mitades se
muestran **colapsadas en una fila** con el kg del grupo. Nunca dos filas diciendo
cada una 48.988.

---

## 3. Los 16 tests

Los dos primeros **ya existen y tienen que quedar verdes sin tocarlos**. Si hay
que editarlos, el cambio está mal.

1. `tests/test_hilado_kilos_sin_plata.py:55` *(existente)* — AI 11 partida:
   `kg_con_costo` sigue siendo 22.564,78, no 45.129,56
2. `tests/test_importaciones.py:204` *(existente)* — AC 31: el $ una vez por
   grupo, los kg suman 25.494
3. **AC 58 no se agrupa** — mismo prov, mismo nº, mismo año, fechas y notas
   distintas ⇒ dos grupos. *El caso que mató la regla original*
4. **AC 5 no se agrupa** — dos embarques de la misma campaña a 10 meses
5. **AI 11 sí se agrupa** — misma fecha, nota base `AYF02649`, sufijos 1 y 2
6. **Rango vs suelto** — `"MH 66-67"` + `"MH 66"`: el kg del rango se cuenta una
   sola vez
7. **Sin sufijo no hay grupo** — se avisa, no se agrupa
8. **Sufijos no contiguos** (`---1` y `---3`) ⇒ grupo incompleto, no suma
9. **Grupo incompleto por borde de ventana** — un miembro sin kg ⇒ kg del
   documento y aviso, nunca una suma parcial
10. **Banda económica** — grupo con $/kg 2,1 o 6,5 ⇒ no se aplica, se avisa
11. **Dedup por grupo, no por `im_numero`** — el doble conteo que aparece justo
    cuando el arreglo empieza a funcionar
12. **`autobap` arma UN grupo** — dos mitades con anticipos propios ⇒ una compra
13. **`autobap` no frena por falsa ambigüedad**
14. **La igualdad del 31/07** — `costo_hilado_recibido_mes(2026,7)["kg"] ==
    hilado_recibido_mes(2026,7)` (404.657 = 404.657)
15. **Idempotencia sobre filas cacheadas** — dos llamadas dentro del TTL dan el
    mismo `grupo_kg`
16. **Los agregados mensuales no leen `grupo_kg`** — las cuatro funciones que hoy
    ya están bien siguen sumando `r["kg"]`

---

## 4. Lo que NO está verificado

Lo digo explícito para que nadie lo tome como hecho:

- **Por qué el stock bajó 49.425 al reversar AC 22.** Mi primera explicación
  (*"la compra se atribuyó a otra importación recibida"*) **quedó refutada**: el
  endpoint mostró 24/24 compras de AC bien atribuidas, $0 mal asignado. La
  sospecha actual es `compras_mes_corriente()` (sin filtro de recepción, línea
  4235 → 4772), pero **el caso concreto se perdió al reversar**. Hay que
  reproducirlo con otra importación no recibida, no reconstruirlo de memoria.
- **Si las dos mitades se reciben siempre el mismo día.** No es verificable
  leyendo código. Si alguna vez se recibieran en meses distintos, el mes que se
  queda con la mitad sin importe aportaría kilos con 0 dólares — y **eso sí
  movería la utilidad por el camino normal**. Es la única vía por la que este
  defecto podría tocarla hoy.
- **`fp.numero_factura`** sería la clave ideal (dato estructurado, sin texto
  libre). Existe en Asinfo y el repo ya lo lee para compras locales
  (`asinfo/service.py:2661`), pero `importaciones_asinfo()` no lo pide. **Hay que
  medir en vivo si viene poblado y si viene igual en las dos mitades.** Si sí,
  reemplaza a la nota normalizada y se acaba la dependencia del texto libre.
- **MH 66-67 a 2,2385 $/kg.** El dBase tiene lo mismo, así que no es un error de
  PC. Queda la duda de si falta cargar el concepto 67 (~40.000): **se contesta
  con la carpeta, no con el sistema**.
- **AC 32 a 2,394 $/kg.** El dBase todavía no la tiene (se cargó el 31/07). Mismo
  caso: carpeta.

---

## 5. Fragilidades que apareció la medición y no estábamos buscando

No bloquean el plan, pero conviene que estén escritas:

- **Empates que se resuelven por orden de lista.** AC 25 tiene dos candidatas
  idénticas (IM-0000549 e IM-0000548, **misma fecha, mismo rango "AC 25-26"**).
  El desempate es por cercanía de fecha y **empatan**: gana la primera del
  arreglo. Son 151.080,90 asignándose arbitrariamente. Lo mismo AC 19
  (IM-0000551 vs IM-0000550, 65.928,20).
- **AC 25 y AC 27 no tienen importación propia** — sólo matchean por rango. No
  existe una fila que diga "AC 25" a secas.
- **AC 53 zafó por 178 días.** Tiene una segunda candidata a 478 días
  (IM-0000372, marzo 2025) que quedó afuera sólo porque la ventana es de 300.
- **Las compras no reciben el desempate por año que sí reciben los anticipos.**
  `service.py:302` llama `_nearest_import(cands, c["fecha"])` sin `anio=`; la
  línea 320, para anticipos, sí lo pasa. El arreglo del 29/07 se aplicó a un
  camino y no al otro.

---

## 6. Cómo medir antes y después

`/admin/health/hilado-stock-debug` ya existe para esto: muestra `kcom`, `ucom`,
`usd_kg_compras`, `um_act` y la **utilidad proyectada** bajo cada escenario, sin
tocar nada. Correrlo antes de cada paso y después, y anotar los cuatro números.

Y `/informes/traza` graba solo. Si un paso mueve la utilidad y no tendría que
haberlo hecho, va a quedar la fila con la hora y el componente.
