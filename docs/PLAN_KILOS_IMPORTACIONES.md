# Plan — kilos de importación y tarifa del hilado

Escrito el 31/07/2026. **Reescrito el mismo día, después de medir.** Cinco de
las reglas que tenía este documento eran razonables y estaban mal; las mató el
dato, no el razonamiento. Queda todo anotado, porque el valor del documento es
justamente ése.

Regla de la casa: cada ítem trae **la evidencia que lo respalda** (dato medido o
línea de código) y **cómo se verifica**. Lo que no está verificado está marcado
como tal.

---

## 0. Estado al cierre — qué se hizo y qué NO

| | estado |
|---|---|
| Agrupar las importaciones partidas (`grupo_id` / `grupo_kg`) | **desplegado** |
| Alarma de banda 2,7–3,4 US$/kg por grupo | **desplegada y prendida** |
| `promedio_hilado_usd_kg` — el `continue` que perdía kilos | **arreglado** |
| `autobap` — la partida ya no se declara ambigua | **arreglado** |
| `/admin/debug-grupos-partidas` | **desplegado** |
| **Usar los kilos nuevos en el balance** | **NO, y ya no se puede: el interruptor se borró** |

### 03/08/2026 — el interruptor se sacó, y la razón da vuelta la sección 3

Existía `HILADO_KG_FUENTE` (+ `?hilado_kg=grupo`) para valuar con el kg del
GRUPO. **Ya no existe.** TMT: *"entonces no me dejes un interruptor que pueda
desalinear"*.

Tenía razón, y el dato le dio la razón — medido el 03/08 con AI 15 como único
ingreso del mes:

| | Hilado kg | $/kg | $ |
|---|---|---|---|
| Flujo producción | 1.897.270 | **3,044** | 5.774.704 |
| Balance HOY | 1.897.270 | **3,044** | 5.774.704 |
| Balance con el interruptor | 1.897.270 | **3,026** | 5.741.141 |

**Las dos pantallas YA coinciden al centavo. Prenderlo las desalineaba.**

Por qué: la tarifa que valúa el stock la da **Asinfo**
(`mov_hilado_valuacion`), que suma las dos mitades de una partida fila por fila
y por eso da bien. El `6,5455 US$/kg` que se ve en
`/admin/health/hilado-stock-debug` es `ucom ÷ kcom` de `compras_mes_corriente`
— un **diagnóstico**, donde `kcom` todavía cuenta media importación. Está mal y
hay que arreglarlo, pero **no es lo que valúa el stock**.

Mi error fue leer un número de diagnóstico como si fuera la tarifa, y de ahí
recomendar un cambio de −44.888 sobre un número que estaba bien. La dueña lo
frenó con *"me suena más que el hilo sea 3,2"* — y 3,273 es exactamente el
precio de compra de AI 15.

`kg_hilado_mes()` sigue vivo **como diagnóstico** (health + alarma de banda).
Lo que no puede es volver a decidir el kg que valúa.

---

## 1. Lo que la medición dio vuelta

### 1.1 La premisa del plan viejo era falsa

El plan decía: *"las compras de importaciones partidas llevan la mitad de los
kilos (AC 25 = 48.988 dBase / 24.494 PC)"* y *"los recargos repiten los kg:
70.354 kg contados dos veces"*, y de ahí salía la aritmética de cancelación
(+70.354 / −70.570 = −215).

**Contra la base de hoy, ninguna de las dos cosas es cierta.** Las 38 compras H
de julio (`/admin/health/hilado-stock-debug`):

- las 14 que traen kg lo traen **completo**: `AC 25 → 48.988`, `AC 19 → 22.354`;
- los recargos (`AC 25 2.241,70`, `AC 15 2.769,49`, `AC 16 15.441,06`) están
  todos con **kg = 0**. No hay nada repetido.

Lo que sí está partido son los **documentos de Asinfo**. Eso no toca las 14
compras sincronizadas, pero sí rompe la reconstrucción de las otras **24**, que
son BAP con `kg = 0` y sacan su kg de la importación — y se llevaban una mitad.

**Conclusión:** la cancelación a 215 kg no existe. Los dos "defectos" no eran
dos, era uno solo, y está del lado de Asinfo.

### 1.2 Las cinco reglas de agrupación que se cayeron

Cada una parecía sensata. Las tres primeras murieron en el escritorio; las dos
últimas las mató la medición sobre las 632 importaciones reales.

| # | regla | qué la mató |
|---|---|---|
| 1 | agrupar por (prov, nº, **año**) | **AC 58** — IM-0000622 (jul, "ACMT/EXP/2026-27/8368") e IM-0000527 (feb, "INV HY336-26-1") son dos campañas, no mitades. Sumarlas bajaba el $/kg de 3,0271 a 2,3428 y el stock 284.772 |
| 2 | exigir el sufijo `---N` | **AC 19** lleva el ordinal DENTRO del paréntesis: `"…/7914 (--1--)"` y `"(--2--)"`. 11.363,72 + 10.990,42 = **22.354,14**, exactamente el dBase. La regla perdía esos 11.000 kg |
| 3 | descalificar si la nota trae **rango** | mataba justo los casos buenos: **AC 25** son IM-0000548 e IM-0000549, las dos `"AC 25-26"` (24.492,24 + 24.494,24 = 48.986,48 ≈ 48.988 del dBase). El rango no es peligro: es el código que las dos mitades **comparten** |
| 4 | exigir **misma fecha exacta** | Asinfo fecha los dos documentos en días distintos: **AC 88** a 68 días (recibidos los dos el 28/03), AC 23-24 a 39. Y peor: **AI 62** tiene `---1/---2/---3` y sólo dos comparten fecha ⇒ se sumaba **una parte** del grupo, que es el error que el plan quería evitar |
| 5 | agrupar por (proveedor + nota base) a secas | **MH 68/69/70**: una sola factura `"INV HY3821-26"` cubre TRES importaciones con códigos distintos. Sumarlas daba 71.880 kg donde van 24.300. Ídem AI 23 con AI 28 bajo `"AYF02748"` |

### 1.3 La regla que quedó

> Se suman los kg de dos o más documentos de Asinfo **sólo** cuando comparten
> las tres cosas: mismo `prov_cod_asinfo`, misma **nota base** (el nº de factura
> del proveedor, sin paréntesis ni ordinal) y **mismo código del programa**
> (prov + nº + nº_hasta) — y las fechas caen dentro de **120 días**.

Qué aporta cada pieza: proveedor + nota base = **es la misma factura**; el
código del programa = **es la misma mercadería** (lo único que separa MH
68/69/70); la ventana = no pegar dos campañas que reusan un nº de factura (el
único caso a más de 68 días en toda la historia son 366, y es una nota sin
código).

**Descalificadores** (cualquiera ⇒ no se suma, queda el aviso): más de 3
miembros; a algún miembro le falta el kg (*grupo incompleto* — una suma parcial
que cambia en cada corrida es peor que el número de hoy); los miembros se
recibieron en **meses distintos** (el número que alimentan es mensual).

**Por defecto cada importación es su propio grupo.** Si algo no cierra, el
número no cambia respecto de hoy.

---

## 2. La prueba de que la agrupación está bien

No hace falta creerle al código. **MH 66-67**, julio:

| | kg | US$ | US$/kg |
|---|---|---|---|
| una mitad (lo de hoy) | 23.430 | 104.894,95 | **4,477** |
| el grupo (IM-0000608 + IM-0000609) | 47.730 | 104.894,95 | **2,198** |
| el grupo, con el concepto 67 cargado (~40.000) | 47.730 | ~144.895 | **~3,04** |

El 4,477 que apareció en pantalla —y que se leyó como *"qué caro salió este
hilo"*— **no era un precio: era media importación**. Con las dos mitades el
número pasa a decir la verdad, que es *"falta cargar plata"*, y coincide con lo
que ya sabíamos: falta el concepto 67.

Ese es el punto de toda esta parte. La agrupación no cambia lo que se compró:
cambia si el sistema puede decirte cuál es el problema.

---

## 3. ⚠️ SECCIÓN OBSOLETA — ver el bloque del 03/08 en la sección 0

Lo de abajo se escribió el 31/07 leyendo `usd_kg_ponderado_compras_mes` como si
fuera la tarifa del hilado. **No lo es.** La medición del 03/08 mostró que el
balance y el flujo ya coinciden y que el interruptor los desalineaba; el
interruptor se borró. Se deja el texto porque el razonamiento que lleva a la
conclusión equivocada vale más que borrarlo.

### (obsoleto) Por qué prender el interruptor mueve −104.843

Medido:

| | kcom (kg) | US$/kg compras | um_act | Utilidad |
|---|---|---|---|---|
| `legacy` (hoy) | 600.216,40 | 3,2321 | 3,5351 | **650.898,93** |
| `grupo` | 638.581,66 | 3,0379 | 2,9519 | **546.056,19** |

Los 38.365 kg de diferencia son **mercadería real que el sistema no estaba
contando** (las mitades que faltaban). El problema no son los kilos: es que
entran **sin su plata**.

La tarifa del hilado es `h_um = (h_ucom + (h_hilado − h_kcom) × um_anterior) /
h_hilado`. Subir `h_kcom` sin subir `h_ucom` **baja** la tarifa, y esa tarifa
revalúa 1,9 M de kg. O sea: sumar kilos correctos cuya plata todavía no está
cargada tira la utilidad para abajo por un motivo falso. Es el mismo defecto que
ya se arregló del lado de Asinfo con `kg_con_costo` (30/07), viviendo ahora en
el camino de fallback.

De dónde sale la plata que falta — lo que la alarma de banda ya nombra sola:

| grupo | US$/kg | qué le pasa |
|---|---|---|
| MH 66 | 2,198 | falta el concepto 67 (~40.000) — **se contesta con la carpeta** |
| AC 94 | 4,328 | falta kg o sobra plata |
| AC 53 | 3,643 | idem |
| AI 14 | 3,450 | apenas afuera |

**Regla que queda escrita:** el interruptor se prende **después** de que esas
cargas estén, no antes. Prenderlo hoy sería cambiar la utilidad por un dato que
falta, que es exactamente lo que estuvimos persiguiendo todo el día.

Y la verificación es barata: se prueba en vivo con `?hilado_kg=grupo`, sin
deploy y sin tocarle el número a nadie.

---

## 4. Lo que quedó andando sin tocar la utilidad

**Alarma de banda 2,7 – 3,4 US$/kg.** **No sale en rojo en Resultados**
(31/07, la dueña al verla: *"me sacás todo esto en rojo de Resultados"* — tres
carteles arriba del Informe todos los días tapan la pantalla con la que se
decide). El dato no se pierde: vive en `diagnostico.hilado_fuera_de_banda`, en
`/admin/health/hilado-stock-debug` y en `/admin/debug-grupos-partidas`. Se
calcula **por grupo** (Σ importe del
grupo ÷ kg del grupo), y sólo sobre importaciones **recibidas en el mes** — si
la mercadería llegó en junio y en julio sólo se cargó el CAE, el $/kg del mes da
0,79 y no significa nada. Una alarma que suena siempre no la mira nadie. Antes
el balance sólo avisaba si una compra tenía importe **sin** kg; con la **mitad**
de los kilos no saltaba nada, y las cinco de julio se encontraron ordenando la
lista a mano.

**`promedio_hilado_usd_kg`.** El `continue` salteaba la fila entera cuando
faltaba el importe. En una partida la mitad `---1` no lo tiene (el costo vive en
la `---2`), así que se perdían sus kilos mientras el $ del grupo se contaba
entero: **el $/kg salía casi al doble**. Ahora la falta de importe saltea sólo el
$. Afecta la valuación de la apertura en el Flujo de producción.

**`autobap`.** Contaba documentos, no grupos: una importación partida daba
siempre 2 candidatas ⇒ *"falta ponerle el año"*, que no arreglaba nada porque
las dos mitades son del mismo año. Ahora cuenta grupos, y la ambigüedad de
verdad (AC 58) sigue frenando.

**`/admin/debug-grupos-partidas`.** Muestra, importación por importación, la
nota cruda, la nota base, el grupo en que quedó y **por qué** — o por qué se
descartó. Es lo que permitió tirar abajo las reglas 2 a 5 en una tarde.

---

## 5. Lo que falta, y de quién depende

**Depende de la carpeta, no del sistema:**

- **MH 66-67** — ¿falta cargar el concepto 67 (~40.000)? El $/kg de 2,198 dice
  que sí. Con eso cargado el grupo queda en ~3,04, dentro de banda.
- **AC 32** a 2,394 — el dBase todavía no la tiene (se cargó el 31/07).
- **AC 94** (4,328) y **AC 53** (3,643).

**Depende de nosotros:**

- Prender `HILADO_KG_FUENTE=grupo` una vez cargado lo de arriba, y medir de
  nuevo antes y después. Es una línea.
- **Por qué el stock bajó 49.425 al reversar AC 22.** Sigue sin explicación: mi
  hipótesis (*"la compra se atribuyó a otra importación recibida"*) **quedó
  refutada** — 24/24 compras de AC bien atribuidas, $0 mal asignado. El caso
  concreto se perdió al reversar; hay que reproducirlo con otra importación no
  recibida, no reconstruirlo de memoria.
- El cupo de provisión de 33.300/día, con el contador.
- El modal ↺ del historial describe la acción equivocada.

---

## 6. Fragilidades que apareció la medición y no estábamos buscando

No bloquean nada, pero conviene que estén escritas:

- **Empates que se resuelven por orden de lista.** AC 25 tiene dos candidatas
  idénticas (IM-0000548 / IM-0000549, misma fecha, mismo rango). El desempate es
  por cercanía de fecha y **empatan**: gana la primera del arreglo. Son
  151.080,90 asignándose arbitrariamente. Ídem AC 19 (65.928,20). *La
  agrupación tapa el síntoma —las dos van al mismo grupo— pero el desempate
  sigue siendo arbitrario para todo lo demás.*
- **AC 25 y AC 27 no tienen importación propia**: sólo matchean por rango.
- **AC 53 zafó por 178 días**: tiene una segunda candidata a 478 (IM-0000372,
  marzo 2025) que quedó afuera sólo porque la ventana es de 300.
- **Las compras no reciben el desempate por año que sí reciben los anticipos**
  (`service.py:302` llama `_nearest_import` sin `anio=`; la 320 sí lo pasa). El
  arreglo del 29/07 se aplicó a un camino y no al otro.
- **`kg_hilado_mes` cuenta el grupo entero si CUALQUIER compra del mes matchea
  esa importación** — incluso un CAE suelto de mercadería que llegó otro mes
  (AC 16: 22.992 kg en julio por 18.254 de recargo). El camino viejo hacía lo
  mismo, así que no es una regresión, pero es la próxima piedra.

---

## 7. Cómo medir

`/admin/health/hilado-stock-debug` — anotar, antes y después: `kcom_base_sum_compra_kg`,
`kcom_total_usado_en_balance`, `kg_hilado_mes_NUEVO.kg`, `ucom_total_importe`,
`usd_kg_ponderado_compras_mes`, `balance_live.utilidad`, y por escenario `um_act`
+ `utilidad_proyectada`. Con `?hilado_kg=grupo` devuelve todo bajo la regla
nueva **sin cambiarle el número a nadie**.

`/admin/debug-grupos-partidas` — `grupos_armados` y `grupos_descartados` con el
motivo.

`/informes/traza` — graba solo. Si un cambio mueve la utilidad y no tendría que
haberlo hecho, queda la fila con la hora y el componente.

---

## 8. Tests

`tests/test_kilos_importaciones_partidas.py` — 27. Los que importan son los que
guardan las reglas caídas, para que nadie las vuelva a escribir:

- `test_ac_58_no_se_agrupa…` — mismo prov, mismo nº, mismo año, y NO son mitades
- `test_ac_19_agrupa_aunque_el_ordinal_este_en_el_parentesis` — 22.354,14
- `test_el_rango_en_la_nota_no_descalifica` — AC 25, 48.986,48
- `test_ac_88_agrupa_a_68_dias` — la fecha exacta no sirve
- `test_una_factura_con_tres_codigos_distintos_no_es_una_partida` — MH 68/69/70
- `test_dedup_por_grupo_no_por_documento` — el doble conteo que aparece justo
  cuando el arreglo empieza a funcionar
- `test_agrupar_dos_veces_da_el_mismo_kg` — idempotencia sobre filas cacheadas
- `test_los_agregados_mensuales_siguen_sumando_fila_por_fila` — las cuatro
  funciones que ya estaban bien no leen `grupo_kg`

Los dos que ya existían siguen **verdes sin tocarlos**:
`tests/test_hilado_kilos_sin_plata.py:55` y `tests/test_importaciones.py:204`.
Suite: **1859 pasando**.
