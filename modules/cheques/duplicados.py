"""¿La misma cobranza se cargó dos veces?

⭐ POR QUÉ EXISTE (TMT 2026-08-04). Cerrar la comisión de julio contra el dBase
costó un día entero. De las diferencias que aparecieron, la más cara fue la más
tonta: **Alex cargó la cobranza de ELF dos veces**, el 13/07 a las 21:46:53 y
otra vez a las 21:47:07 — catorce segundos después. Nadie lo vio hasta que el
total del vendedor no cerró, tres semanas más tarde.

Un cobro cargado dos veces miente en tres lugares a la vez: baja la deuda del
cliente el doble, sube la cobranza del mes el doble, y le paga al vendedor una
comisión que no ganó. Y no hay ninguna pantalla donde salte: las dos filas se
ven perfectamente normales por separado.

## Las dos capas

**Capa 1 — la sospecha.** Cheques que se parecen demasiado: mismo cliente,
mismo importe, misma `fechad`, mismo banco. Es barato de calcular pero por sí
solo **no prueba nada**: un cliente que paga 500 por día tiene una fila así
todos los días, y son cobros distintos y legítimos (IIA, medido: 11 días de
julio).

**Capa 2 — el veredicto.** Contra el extracto: si el banco acreditó UN depósito
de 500 y PC tiene DOS, uno de los dos no existe. Es la única prueba real, y por
eso vive acá al lado y no en otra pantalla.

## ⚠️ Lo que NO es duplicado (la dueña, 04/08)

> *"pera y no tienen que haber sido ingresados en la misma cobranza. onda si
> pongo 100 y otros cheque de 100 en la misma cobranza claramente no va a ser
> error"*

Dos cheques iguales **de la misma cobranza** son dos cheques de verdad: el
cliente entregó dos papeles de 100 y el cajero los tipeó juntos, a propósito.
El error es la cobranza REPETIDA, no el importe repetido.

Distinguir una cosa de la otra necesita saber qué cheques entraron juntos, y
`scintela.cheque` **no tiene columna de cobranza**. La que sí la tiene es
`mov_doble.batch_id`: el alta le pone el mismo `batch_id` a todos los
`cheque_creado` que generó. Fallback para lo viejo (1.535 de 2.273
`cheque_creado` tienen batch): dos altas de la misma cobranza ocurren dentro de
la misma transacción — medido, **los 316 batches multi-cheque cierran en menos
de 2 segundos**, sin una sola excepción. Por eso la ventana es de 2 s y no de
un minuto: ELF fueron 14 segundos y eran DOS cobranzas.

## Por qué el filtro es tan angosto

Medido sobre la base viva (grupos con ≥2 cheques desde el 1/6): **57 grupos,
138 cheques**. Casi todo ruido, y el ruido tiene dos formas:

* **100 de los 138 son `dbf-import`.** Cheques posdatados en cuotas: MMM tiene
  12 cheques de 3.100 con la misma `fechad`, JHM 4 de 2.381,83, RRV 18 pares.
  Son planes de pago, no errores — y encima los trajo el import de una sola
  vez, así que ni siquiera hubo alguien cargando dos veces.
* **Los que siguen en cartera (`Z`/`P`) con `fechad` futura** son justamente
  esas cuotas.

Lo que duele es la plata que YA entró. Por eso capa 1 mira sólo cheques en
estado cobrado y con `fechad` que ya pasó, y exige que las dos cargas las haya
hecho **una persona** (no el import) **el mismo día**. Con eso, los casos que
sobreviven son del tipo ELF: EEG 500 el 15/07 y MTM 300 el 03/08, los dos
cargados por `alex`.

Es a propósito que esto **deje pasar** duplicados del dBase: ahí manda el
dBase, y una alarma que grita 50 veces por día no la mira nadie a la semana.
"""
from __future__ import annotations

from datetime import date, timedelta

import db
from modules.comisiones.queries import STATS_COBRADO

# Dos altas de la MISMA cobranza pasan por una sola transacción. Medido sobre
# los 316 batches multi-cheque de la base: el 100 % cierra en <= 2 s. ELF —el
# caso que motivó todo esto— fueron 14 s y eran dos cobranzas distintas, así
# que la ventana tiene que quedar bien por debajo de eso.
VENTANA_MISMA_COBRANZA_SEG = 2

# Quién cargó. Estos dos no son personas: son el import del dBase. Un
# "duplicado" traído por el import es un duplicado DEL DBASE, y ahí manda el
# dBase — no es algo que se arregle en esta pantalla.
USUARIOS_IMPORT = ("dbf-import", "reconcile-dbf")


def _dias_atras(dias: int, hoy: date | None = None) -> date:
    return (hoy or date.today()) - timedelta(days=dias)


# ---------------------------------------------------------------------------
# Capa 1 — la sospecha
# ---------------------------------------------------------------------------
# Notas de lectura del SQL:
#
#   · `creado` sale de `mov_doble` y NO de `cheque.fecha_creacion` (que no
#     existe): el alta registra un `cheque_creado` por cheque, y ahí está el
#     `batch_id` que agrupa la cobranza.
#   · El `LAG(...) > 2 segundos` arma los clusters: cada corte empieza una
#     "cobranza" nueva. Se ordena por `creado` con `NULLS FIRST` para que los
#     cheques sin mov_doble (los del import) queden cada uno en su propio
#     cluster en vez de mezclarse con los de una carga real.
#   · `COALESCE(batch_id::text, 'W'||k)`: si hay batch manda el batch (exacto);
#     si no, manda el cluster por tiempo (aproximado pero medido).
_SQL_SOSPECHOSOS = """
WITH vivos AS (
    SELECT c.id_cheque,
           UPPER(TRIM(COALESCE(c.codigo_cli,'')))      AS cli,
           ROUND(c.importe, 2)                         AS imp,
           c.fechad,
           c.no_banco,
           UPPER(TRIM(COALESCE(c.stat,'')))            AS stat,
           COALESCE(c.usuario_crea,'')                 AS usuario,
           NULLIF(TRIM(COALESCE(c.no_cheque,'')), '')  AS no_cheque,
           md.batch_id,
           md.fecha_creacion                           AS creado
      FROM scintela.cheque c
      LEFT JOIN LATERAL (
              SELECT m.batch_id, m.fecha_creacion
                FROM scintela.mov_doble m
               WHERE m.tipo = 'cheque_creado'
                 AND m.origen_table = 'cheque'
                 AND m.origen_id = c.id_cheque
               ORDER BY m.id_mov_doble
               LIMIT 1) md ON TRUE
     WHERE UPPER(TRIM(COALESCE(c.stat,''))) = ANY(%(stats)s)
       AND c.fechad BETWEEN %(desde)s AND %(hasta)s
       AND COALESCE(c.importe, 0) > 0
),
grupos AS (
    SELECT cli, imp, fechad, no_banco
      FROM vivos
     GROUP BY cli, imp, fechad, no_banco
    HAVING COUNT(*) > 1
),
cand AS (
    SELECT v.* FROM vivos v JOIN grupos g USING (cli, imp, fechad, no_banco)
),
marca AS (
    SELECT cand.*,
           CASE WHEN creado IS NULL                    THEN 1
                WHEN LAG(creado) OVER w IS NULL        THEN 1
                WHEN creado - LAG(creado) OVER w
                     > (%(ventana_seg)s * interval '1 second') THEN 1
                ELSE 0 END AS corte
      FROM cand
    WINDOW w AS (PARTITION BY cli, imp, fechad, no_banco
                 ORDER BY creado NULLS FIRST, id_cheque)
),
clus AS (
    SELECT marca.*,
           SUM(corte) OVER (PARTITION BY cli, imp, fechad, no_banco
                            ORDER BY creado NULLS FIRST, id_cheque) AS k
      FROM marca
),
cob AS (
    SELECT clus.*, COALESCE(batch_id::text, 'W' || k) AS cobranza FROM clus
)
SELECT cli, imp, fechad, no_banco,
       COUNT(*)                                          AS n_cheques,
       COUNT(DISTINCT cobranza)                          AS n_cobranzas,
       COUNT(*) FILTER (WHERE usuario <> ALL(%(import)s)) AS n_a_mano,
       COUNT(DISTINCT usuario) FILTER (WHERE usuario <> ALL(%(import)s))
                                                         AS n_usuarios,
       COUNT(DISTINCT creado::date) FILTER (WHERE usuario <> ALL(%(import)s))
                                                         AS n_dias_carga,
       ARRAY_AGG(id_cheque ORDER BY id_cheque)           AS ids,
       ARRAY_AGG(COALESCE(no_cheque,'—') ORDER BY id_cheque) AS numeros,
       ARRAY_AGG(usuario ORDER BY id_cheque)             AS usuarios,
       ARRAY_AGG(creado ORDER BY id_cheque)              AS creados,
       ARRAY_AGG(stat ORDER BY id_cheque)                AS stats
  FROM cob
 GROUP BY cli, imp, fechad, no_banco
HAVING COUNT(DISTINCT cobranza) > 1
   -- ⭐ la regla de la dueña: dos cheques iguales de la MISMA cobranza no son
   -- error, son dos papeles de verdad tipeados juntos. Lo que se denuncia es
   -- la cobranza REPETIDA.
   --
   -- Y al menos una de las copias la tiene que haber cargado una PERSONA. Si
   -- las dos las trajo el import, el duplicado está en el dBase y ahí manda el
   -- dBase — son 30 grupos medidos, y una alarma que grita 30 veces el primer
   -- día no se lee nunca más. Alcanza con UNA a mano: import + carga a mano
   -- del mismo cobro es justamente doble conteo (el import lo trajo y alguien
   -- además lo tipeó).
   AND COUNT(*) FILTER (WHERE usuario <> ALL(%(import)s)) >= 1
 ORDER BY imp DESC, fechad DESC
"""


def sospechosos(desde: date, hasta: date, *, ventana_seg: int | None = None) -> list[dict]:
    """Grupos de cheques que parecen la misma cobranza cargada dos veces.

    NO es un veredicto — es la lista corta que vale la pena mirar. El veredicto
    lo da `veredicto_extracto()`.
    """
    return db.fetch_all(
        _SQL_SOSPECHOSOS,
        {
            "stats": list(STATS_COBRADO),
            "desde": desde,
            "hasta": hasta,
            "ventana_seg": int(
                VENTANA_MISMA_COBRANZA_SEG if ventana_seg is None else ventana_seg
            ),
            "import": list(USUARIOS_IMPORT),
        },
    ) or []


# ---------------------------------------------------------------------------
# Capa 2 — el veredicto: ¿el banco lo acreditó?
# ---------------------------------------------------------------------------
# ⚠️ NO se puede preguntar "¿tiene los `real_*` en NULL en
# `banco_conciliacion_match`?". Ese era el plan y está mal: el flujo v2
# (`conciliacion/banco_v2_view.py`) inserta matches con TODOS los `real_*` en
# NULL — identifica la fila del extracto por `tx_firma`, no copiándola. Con ese
# criterio, todo lo conciliado por la pantalla nueva saldría como "no está en
# el banco", que es exactamente al revés.
#
# Lo que sí quiere decir "el banco nunca lo acreditó":
#   · el cheque tiene su movimiento `DE` en `transacciones_bancarias`, y
#   · ese movimiento NO tiene match vivo en `banco_conciliacion_match`
#     (`deshecho_en IS NULL`), y
#   · tampoco viene marcado conciliado desde el dBase (`stat = '*'`).
#
# ⚠️⚠️ Y **"sin conciliar" no es "no está en el banco"**. Medido el 04/08
# probando este mismo detector contra MTM: sus dos depósitos del 03/08 estaban
# los dos sin conciliar, y el veredicto ingenuo los daba a los dos por
# inexistentes. No faltaban: el extracto de agosto **todavía no se había
# subido**. Ese día el banco 10 tenía 44 depósitos y 2 conciliados; el 31/07,
# 52 de 54. El salto entre un día cerrado y uno abierto es de 96 % contra 5 %,
# así que el umbral de abajo puede estar en cualquier lado del medio.
#
# Resultado: hay CUATRO respuestas, y las dos de "no sé" no se mezclan —
# "todavía no conciliamos ese día" y "este cheque no tiene depósito" se
# arreglan de maneras distintas.
_SQL_VEREDICTO = """
WITH movs AS (
    SELECT c.id_cheque, tb.id_transaccion, tb.no_banco, tb.fecha,
           (TRIM(COALESCE(tb.stat,'')) = '*'
            OR EXISTS (SELECT 1 FROM scintela.banco_conciliacion_match m
                        WHERE m.id_transaccion = tb.id_transaccion
                          AND m.deshecho_en IS NULL)) AS conciliado
      FROM scintela.cheque c
      JOIN scintela.chequextransaccion cxt ON cxt.id_cheque = c.id_cheque
      JOIN scintela.transacciones_bancarias tb
        ON tb.id_transaccion = cxt.id_transaccion
       AND UPPER(COALESCE(tb.documento,'')) = 'DE'
     WHERE c.id_cheque = ANY(%(ids)s)
),
cobertura AS (
    SELECT d.no_banco, d.fecha,
           COUNT(*)                                        AS n_dia,
           COUNT(*) FILTER (
               WHERE TRIM(COALESCE(t.stat,'')) = '*'
                  OR EXISTS (SELECT 1 FROM scintela.banco_conciliacion_match m
                              WHERE m.id_transaccion = t.id_transaccion
                                AND m.deshecho_en IS NULL))  AS n_dia_conc
      FROM (SELECT DISTINCT no_banco, fecha FROM movs) d
      JOIN scintela.transacciones_bancarias t
        ON t.no_banco = d.no_banco
       AND t.fecha = d.fecha
       AND UPPER(COALESCE(t.documento,'')) = 'DE'
     GROUP BY d.no_banco, d.fecha
)
SELECT m.id_cheque,
       COUNT(*)                             AS n_movs,
       COUNT(*) FILTER (WHERE m.conciliado) AS n_conciliados,
       COUNT(*) FILTER (WHERE NOT m.conciliado
                          AND COALESCE(cb.n_dia_conc, 0) * 100
                              >= COALESCE(cb.n_dia, 0) * %(umbral)s
                          AND COALESCE(cb.n_dia, 0) > 0)
                                            AS n_faltan_con_dia_cerrado
  FROM movs m
  -- LEFT y no INNER: un día sin cobertura calculable es un día ABIERTO, no un
  -- cheque que desaparece del reporte. Con INNER, la fila se caía entera y el
  -- cheque volvía como "sin depósito" — una respuesta distinta y equivocada.
  LEFT JOIN cobertura cb ON cb.no_banco = m.no_banco AND cb.fecha = m.fecha
 GROUP BY m.id_cheque
"""

#: Un día se considera CONCILIADO si al menos este % de sus depósitos lo está.
#: Medido: los días cerrados van 95-100 %, los abiertos 0-5 %. No hay zona
#: gris, así que el número exacto no es delicado — lo que importa es que
#: exista el corte.
UMBRAL_DIA_CONCILIADO_PCT = 50

SIN_DEPOSITO = "sin_deposito"        # no hay `DE` que conciliar → no sé
EN_EL_BANCO = "en_el_banco"          # el extracto lo acreditó
PERIODO_ABIERTO = "periodo_abierto"  # ese día todavía no se concilió → no sé
NO_EN_EL_BANCO = "no_en_el_banco"    # el día está cerrado y esto no aparece


def clasificar_veredicto(
    n_movs: int, n_conciliados: int, n_faltan_con_dia_cerrado: int = 0
) -> str:
    """Traduce los conteos a una de las CUATRO respuestas posibles.

    Pura a propósito: es la regla que decide si a la dueña le sale "sobra
    éste" o "no puedo saberlo", y esa distinción tiene que poder probarse sin
    base de datos.
    """
    if not n_movs:
        return SIN_DEPOSITO
    if n_conciliados >= n_movs:
        return EN_EL_BANCO
    if n_faltan_con_dia_cerrado > 0:
        return NO_EN_EL_BANCO
    return PERIODO_ABIERTO


def veredicto_extracto(ids: list[int], *, umbral_pct: int | None = None) -> dict[int, str]:
    """Por cada cheque: si el banco acreditó su depósito, si no, o si no se sabe."""
    if not ids:
        return {}
    filas = db.fetch_all(
        _SQL_VEREDICTO,
        {
            "ids": [int(i) for i in ids],
            "umbral": int(
                UMBRAL_DIA_CONCILIADO_PCT if umbral_pct is None else umbral_pct
            ),
        },
    ) or []
    # El SQL joinea contra el `DE`, así que un cheque SIN depósito no vuelve en
    # ninguna fila. Se completa acá y no se deja afuera: quien llama tiene que
    # poder distinguir "no sé" de "no lo pregunté", y un `.get()` que devuelve
    # None invita a tratarlo como falso.
    veredictos = {int(i): SIN_DEPOSITO for i in ids}
    veredictos.update({
        int(f["id_cheque"]): clasificar_veredicto(
            int(f["n_movs"] or 0),
            int(f["n_conciliados"] or 0),
            int(f["n_faltan_con_dia_cerrado"] or 0),
        )
        for f in filas
    })
    return veredictos


# ---------------------------------------------------------------------------
# Las dos capas juntas
# ---------------------------------------------------------------------------

def resumir(grupo: dict, veredictos: dict[int, str]) -> dict:
    """Agrega a un grupo de capa 1 lo que dice el extracto, y una frase.

    La frase importa tanto como el número: el reporte lo lee alguien que no
    escribió esta query y tiene que poder decidir sin abrir el código.
    """
    ids = [int(i) for i in (grupo.get("ids") or [])]
    faltan = [i for i in ids if veredictos.get(i) == NO_EN_EL_BANCO]
    abiertos = [i for i in ids if veredictos.get(i) == PERIODO_ABIERTO]
    nose = [i for i in ids if veredictos.get(i, SIN_DEPOSITO) == SIN_DEPOSITO]
    if faltan:
        frase = (
            f"{len(faltan)} de {len(ids)} no aparecen en el extracto — "
            f"probablemente sobra{'n' if len(faltan) > 1 else ''}"
        )
    elif abiertos:
        frase = "ese día del banco todavía no se concilió — mirar cuando cierre"
    elif len(nose) == len(ids):
        frase = "sin depósito conciliable: hay que mirarlo a mano"
    else:
        frase = "el banco acreditó todos — parecen cobros distintos de verdad"
    return {
        **grupo,
        "ids_sin_respaldo": faltan,
        "ids_periodo_abierto": abiertos,
        "ids_sin_dato": nose,
        "veredicto": frase,
        # El monto EN RIESGO es lo que sobra, no el total del grupo: si hay
        # tres cheques de 500 y uno sobra, el problema son 500, no 1.500.
        "monto_en_riesgo": round(float(grupo.get("imp") or 0) * len(faltan), 2),
    }


def revisar(desde: date, hasta: date) -> list[dict]:
    """Capa 1 + capa 2, listo para mostrar."""
    grupos = sospechosos(desde, hasta)
    if not grupos:
        return []
    ids: list[int] = []
    for g in grupos:
        ids.extend(int(i) for i in (g.get("ids") or []))
    veredictos = veredicto_extracto(ids)
    return [resumir(g, veredictos) for g in grupos]
