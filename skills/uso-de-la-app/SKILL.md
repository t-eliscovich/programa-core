---
name: "uso-de-la-app"
description: "La pantalla USO DE LA APP de Programa Core (/uso, mig 0232, viva desde el 26/08/2026) — cómo se lee, qué significa exactamente cada columna (días, veces que entró con el corte de 30 minutos, pantallas, clientes, impresiones, cambios, % del teléfono), EN QUÉ FIJARSE cuando se la mira (constancia antes que volumen, clientes distintos antes que pantallas, los clientes de la cartera que nunca abrió) y los seis números que ENGAÑAN. Usar cuando se mencione uso de la app, /uso, cuánto usa un vendedor la aplicación, quién no entra, quién no la abre, medir el uso, visitas, uso_pantalla, qué mira o qué movimientos hace un vendedor, a qué hora trabaja, desde el celular o la computadora, o haya que agregarle una pantalla o una columna a ese informe. Pair con portal-vendedores, comisiones-vendedores y programa-core-datos."
---

# Uso de la app — /uso

Nació de una pregunta de la dueña, 26/08/2026: *"¿podríamos medir cuánto usa
cada vendedor la aplicación? ¿y qué movimientos hace?"*.

**Dónde está**: menú del usuario → Administración → **Uso de la app**
(`/uso`). El detalle de uno es `/uso/<usuario>`. Permiso: `bitacora.ver`
(Accionista y Administrador). Un vendedor nunca la ve — el
`scope_vendedor` le cierra todo lo que no sea `/mi-cartera`.

## Lo primero: qué mide y qué no

Son **dos fuentes distintas** pegadas en una pantalla, y confundirlas es el
error que más caro sale al interpretar:

| | De dónde sale | Desde cuándo hay datos |
|---|---|---|
| Lo que **mira** (visitas) | `scintela.uso_pantalla`, mig 0232 | **26/08/2026**. Antes no hay nada |
| Lo que **cambia** (movimientos) | `scintela.bitacora_acciones`, mig 0004 | 2026, desde el arranque |

Cuatro límites que hay que decir en voz alta antes de sacar una conclusión:

1. **Las visitas arrancan el 26/08/2026.** No se puede comparar contra julio.
   La columna «Cambios» sí tiene historia vieja: es la única.
2. **Sólo se registra a los VENDEDORES** — los usuarios con `vend` cargado en
   `seguridad.usuario`. La oficina no se mide. Es lo que se preguntó, y acota
   el volumen a seis personas.
3. **El preview de la dueña no cuenta.** Abrir `/mi-cartera?vend=PPR` con
   permiso wildcard no le suma nada a Patricio: quien mira no tiene `vend`
   propio. Los números del vendedor son del vendedor.
4. **Vender no pasa por la app.** Un vendedor puede cerrar el mes entero por
   WhatsApp y por teléfono sin abrir una pantalla. El uso explica un
   resultado; no lo reemplaza.

## Las columnas, exactas

Vale la pena saber cómo se cuenta cada una, porque cuando alguien discute el
número la discusión se termina acá:

| Columna | Qué es exactamente |
|---|---|
| **Días** | Días distintos —de Ecuador— en que abrió al menos una pantalla |
| **Veces que entró** | Cada visita que llega después de **más de 30 minutos** sin tocar nada. Abrir la app, mirar tres pantallas y cerrarla = 1 |
| **Pantallas** | Todas las pantallas que abrió. Refrescar suma |
| **Clientes** | «12 de 43»: a cuántos de su cartera les abrió la ficha, sobre los que tiene asignados. Cuentan sólo los que HOY son suyos, así los dos números cierran con la lista de «los que no abrió» |
| **Impresiones** | Las pantallas que terminan en papel: imprimir, PDF, foto, Excel. **Ya están contadas también en Pantallas** — no se suman aparte |
| **Cambios** | Lo que guardó, de la bitácora. En un vendedor es poco por diseño: casi todo lo que hace es mirar |
| **Del teléfono** | Qué parte de sus pantallas abrió desde un celular |
| **Última vez** | La última pantalla que abrió, en hora de Ecuador |

El rango por defecto son los **últimos 30 días**; se cambia con Desde/Hasta y
se baja con el botón **CSV**. Salen **todos** los vendedores, también los que
no entraron nunca: esa fila que dice «no entró» es a propósito, es media
pantalla del valor.

## En qué fijarse

En orden de utilidad. Cada una viene con su falso positivo, porque leer mal
este informe es fácil y caro:

**1. El que no entró.** Es lo primero que hay que mirar y lo único que salta
solo. Antes de ir a preguntarle nada: chequear que el usuario esté **activo**
y que tenga su `vend` cargado (`/usuarios`) — un vendedor sin `vend` no se
registra y se ve idéntico a uno que no trabaja. Y mirar la fecha: si nadie
tiene datos, lo que falla es el registro, no la gente.

**2. Constancia antes que volumen.** *Días* manda sobre *Pantallas*. Entrar
todos los días un rato es trabajar la cartera; 200 pantallas en dos días es
alguien preparando una reunión o cerrando el mes. Las dos cosas pueden estar
bien, pero no son lo mismo y la columna que las separa es Días.

**3. Clientes, contra su cartera.** El número que dice si mira la cartera o
mira siempre a los mismos cinco. Por eso viene como **«12 de 43»** y no como
un 12 suelto: 12 sobre 43 es una cosa, 12 sobre 15 es otra.

Y la mitad que se usa: en el detalle del vendedor está **«Los que no abrió»**,
con lo que cada uno debe y lo vencido primero. Ahí la pantalla deja de
describir y empieza a servir — es la conversación más útil que sale de este
informe. Ojo con el tono: no abrir una ficha no es no haber hablado con el
cliente.

**4. Impresiones ≈ trabajo de campo.** Cada estado de cuenta impreso, PDF o
foto es un papel que se le deja a un cliente o un mensaje que se le manda.
Es lo más cercano a "salió a cobrar" que tiene el sistema.

**5. A qué hora trabaja** (en el detalle, columnas Desde/Hasta del día por
día). Sirve para entender, no para vigilar: el que arranca 7:30 en el
teléfono está en la calle; el que abre todo a las 18:00 está cargando el día
de golpe.

**6. El movimiento después de un cambio.** Si se rediseñó una pantalla del
portal y su uso se cae, la pantalla molesta. Es la única forma que hay de
saber si lo que se construye para los vendedores se usa. Mirar la tabla «Las
pantallas más abiertas»: una pantalla nueva que a la semana sigue en cero no
la encontró nadie.

**7. Cruzarlo con el resultado, siempre.** El uso solo no dice nada. Vale
contra `/comisiones` (la cobranza del mes), contra las metas en kilos de
`/mi-cartera/comision` y contra `/cartera/aging`. Las dos combinaciones que
importan: **usa mucho y cobra poco** (mira, pero no cierra) y **usa poco y
cobra bien** (funciona con su libreta; no romperle el método, ver si algo de
la app le estorba).

Y una advertencia de tono, porque la pantalla se presta: **no es un reloj de
fichar**. Sirve para saber si la herramienta le sirve a la gente y para
encontrar al que se quedó afuera, no para contar minutos. Si una conclusión
sólo se sostiene mirando esta pantalla, no se sostiene.

## Los números que engañan

- **Refrescar suma.** Un teléfono con la pantalla abierta y el dedo pesado
  infla *Pantallas*. Nunca infla *Días* ni *Clientes*: por eso esos dos son
  los que se miran.
- **Impresiones está adentro de Pantallas.** Sumarlas da un total que no
  existe.
- **«Cambios» juega otro campeonato.** Trae historia vieja y cuenta
  escrituras, no visitas. En la misma fila que columnas que arrancan de cero
  el 26/08, se lee como si el vendedor hubiera hecho mucho más de lo que hizo
  ese mes.
- **El % del teléfono con pocas visitas es ruido.** Con 4 pantallas en el
  rango, un 25% no significa nada.
- **Todo está en hora de Ecuador**, convertido en la consulta. El servidor
  corre en UTC: cualquier consulta nueva que se escriba a mano contra
  `uso_pantalla` tiene que hacer `AT TIME ZONE 'America/Guayaquil'` o los días
  salen corridos cinco horas (ver `programa-core-datos`).
- **Un cliente aparece en «Clientes» aunque el vendedor sólo haya pasado de
  largo.** La ficha se cuenta al abrirse, no al hacer algo con ella.

## El detalle de un vendedor — `/uso/<usuario>`

Cuatro bloques, y el orden en que conviene leerlos:

1. **Día por día**, con la primera y la última pantalla de cada día.
2. **Los clientes que abrió**, ordenados por veces.
3. **Los que no abrió** — su cartera menos lo que miró, con el saldo y lo
   vencido. Los que deben plata vencida arriba.
4. **Sus pantallas**, las más abiertas primero.
5. **Todo lo que hizo**: visitas y cambios mezclados en una sola línea de
   tiempo, los cambios pintados aparte. Es el bloque para reconstruir un día
   puntual —"¿qué pasó el martes?"—, y también con CSV.

## Consultas para preguntas que la pantalla no contesta

Consola SQL de sólo lectura (`/admin/sql`, ver `programa-core-datos`).

**Los clientes de su cartera que NO abrió en 30 días.** Esto ya está EN la
pantalla (bloque «Los que no abrió» del detalle); la consulta queda para
cruzarlo con algo que la pantalla no trae:

```sql
SELECT c.codigo_cli, c.nombre
  FROM scintela.cliente c
 WHERE UPPER(TRIM(c.vend)) = 'PPR'
   AND UPPER(TRIM(c.codigo_cli)) NOT IN (
        SELECT codigo_cli
          FROM scintela.uso_pantalla
         WHERE usuario = 'ppr'
           AND codigo_cli IS NOT NULL
           AND ts > now() - INTERVAL '30 days')
 ORDER BY 1;
```

**A qué hora del día se usa la app** (todos juntos):

```sql
SELECT EXTRACT(hour FROM ts AT TIME ZONE 'America/Guayaquil')::int AS hora,
       count(*) AS pantallas
  FROM scintela.uso_pantalla
 WHERE ts > now() - INTERVAL '30 days'
 GROUP BY 1 ORDER BY 1;
```

**¿Está registrando?** (después de pedirle a alguien que abra la app):

```sql
SELECT usuario, count(*), max(ts AT TIME ZONE 'America/Guayaquil')
  FROM scintela.uso_pantalla
 WHERE ts > now() - INTERVAL '1 hour'
 GROUP BY 1;
```

Si eso da vacío y el vendedor jura que entró: mirar que el usuario tenga
`vend` en `seguridad.usuario`, y que el deploy sea del 26/08/2026 o posterior.

## Cómo está hecho, para el que lo tenga que tocar

| Archivo | Qué hay |
|---|---|
| `migrations/0232_uso_de_la_app.sql` | La tabla `scintela.uso_pantalla` |
| `modules/uso/registro.py` | El hook `after_request` que anota la visita, el mapa de nombres de pantalla y las «pantallas de papel» |
| `modules/uso/queries.py` | Las consultas, con la explicación de las dos zonas horarias |
| `modules/uso/views.py` | Las dos pantallas y los CSV |
| `tests/test_uso_de_los_vendedores.py` | Qué se registra, qué no, y que medir no rompa nada |

Decisiones que ya se tomaron, para no volver a discutirlas:

- **Tabla propia y no la bitácora**: la bitácora audita ESCRITURAS a propósito
  (`auth._should_audit`). Meterle las visitas de todos la vuelve inútil como
  auditoría.
- **El INSERT es best-effort**: si falla, el vendedor no se entera. Medir no
  puede tumbar la pantalla con la que trabaja todo el día.
- **`pantalla` guarda el endpoint** (`mi_cartera.cliente`), no el título. El
  nombre lindo se resuelve al leer, así cambiarle el texto a una pantalla
  arregla también todo lo ya registrado.

**Al agregar una pantalla nueva al portal del vendedor**: ponerle su nombre en
`registro.NOMBRES` y, si termina en papel, en `registro.PAPELES`. Sin eso la
fila aparece con el nombre técnico. Hay un test que valida que todo endpoint
nombrado exista de verdad en la app — es la misma lección de los links
hardcodeados del historial (ver `programa-core`).

**Para medir también a la oficina**: es una sola condición en
`registro.hay_que_registrar()` (hoy exige `vendedor_de(g.user)`). Antes de
hacerlo, pensar el volumen: son ~20 personas con pantallas mucho más pesadas,
y la pantalla `/uso` hoy lista sólo vendedores (`WHERE u.vend IS NOT NULL`).
