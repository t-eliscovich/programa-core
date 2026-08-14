"""La APERTURA de cada banco: el único número del saldo que se guarda.

⭐ POR QUÉ EXISTE (TMT 2026-08-04). Dueña, después de leer el diagnóstico:
*"arregla el bug para que no pase más"*.

El bug de fondo no era el ancla del walk ni el criterio de signos. Era esto:

    **El Balance publicaba un número que alguien escribió a mano en una
    fila, no un número que el sistema calcula.**

`transacciones_bancarias.saldo` es un saldo corrido GUARDADO: cada fila lleva
el acumulado hasta ahí. Y `saldo_bancos()` no sumaba nada — agarraba el
`saldo` de la última fila y eso era BANCOS → patrimonio → utilidad.

El problema es que las filas **nacen en orden de carga** y **se leen en orden
de fecha**. Cada vez que entra algo con fecha vieja —que es exactamente lo que
hace la conciliación al crear un `ND Comisiones` o un `DE AJUSTE`— esa fila
cae en el medio y hay que **re-estampar todas las que siguen**. Eso lo hace
código, que tiene que correr en todos los caminos, anclar bien y no dejarse
ninguna fila afuera. Falló tres veces:

  · 2026-05-12 — walk sin ancla: Pichincha quedó en −917.651,96.
  · 2026-06-11 — ancla en la misma fecha: la cadena corría un día por carga.
  · 2026-08-03 — ancla por id y walk por fecha: $2,96 movieron 155.187,31.

Eran **1.333 números mantenidos a mano** cuando el sistema necesita uno solo.
Éste. La plata que el banco tenía **antes de la primera fila cargada**.

Con la apertura guardada, `saldo_bancos()` calcula: apertura + suma firmada de
los movimientos. La columna `saldo` pasa a ser **decoración** de la pantalla de
banco: si se rompe, se ve fea una columna, pero el patrimonio no se mueve. La
familia entera de bugs se termina en vez de vigilarse.

Por qué no se hizo el 03/08: se creía que la suma firmada no era confiable
("`_signed_delta` no sabe leer los signos de las filas viejas del dBase").
Verificado el 04/08 sobre las 1.333 filas de Pichincha: `documento` predice el
signo en **1.326**, y las 7 excepciones son exactamente los 7 quiebres — o sea
eran el MISMO problema de orden, no un problema de signos. Cae la objeción.

🔒 TABLA PROPIA, NO UNA COLUMNA DE `scintela.banco`. `banco` la escribe el sync
del dBase; una columna nuestra ahí se borraría sin aviso en el próximo sync y
el patrimonio se iría a cero en silencio. [[feedback_dato_pc_only_no_colgar_de_lo_que_el_sync_pisa]]

🌱 SIEMBRA CONSERVADORA. La primera vez, la apertura de cada banco se calcula
como `lo que el Balance publica HOY − la suma de los movimientos`. O sea el
cambio de fuente es, al centavo, **un no-op**: ningún saldo se mueve el día que
esto entra. Incluido DEP.PICH., que arrastra −455,89 que sumados dan 0 — esa
decisión (una corrección de junio que caería en el mes equivocado) sigue siendo
de la dueña y no la toma un deploy. A partir de ahí la plata sólo se mueve
cuando alguien carga un movimiento, que es como tiene que ser.
"""
from __future__ import annotations

import logging

import db as _db
from bank_helpers import DOCS_ENTRADA

_LOG = logging.getLogger("programa_core.bancos.apertura")

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS scintela.banco_apertura (
    no_banco        INTEGER PRIMARY KEY,
    saldo_apertura  NUMERIC(14, 2) NOT NULL,
    origen          TEXT,
    nota            TEXT,
    usuario         TEXT,
    fijado_en       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_DOCS = ", ".join(f"'{d}'" for d in DOCS_ENTRADA)

# Suma firmada de los movimientos ya vigentes de un banco.
#
# `fecha <= CURRENT_DATE` replica lo que ya hacía la lectura del running
# guardado: un cheque postdatado al 30/06 no es plata que salió todavía.
# TMT 2026-06-26, dueña: "la utilidad está muy baja" — el Balance tomaba el
# saldo de una fila postdatada y Pichincha entraba 90.261 más bajo.
SUMA_FIRMADA_SQL = f"""
    COALESCE((
      SELECT SUM(CASE WHEN UPPER(TRIM(t.documento)) IN ({_DOCS})
                      THEN t.importe ELSE -t.importe END)
        FROM scintela.transacciones_bancarias t
       WHERE t.no_banco = {{banco}}
         AND t.fecha <= CURRENT_DATE
    ), 0)
"""

# Siembra: apertura = lo que el Balance publica HOY − la suma de movimientos.
# `ON CONFLICT DO NOTHING` la hace idempotente y, sobre todo, hace que nunca
# pise una apertura que una persona ya afirmó.
_SIEMBRA_SQL = f"""
INSERT INTO scintela.banco_apertura (no_banco, saldo_apertura, origen, nota)
SELECT b.no_banco,
       ROUND(COALESCE((
         SELECT t.saldo
           FROM scintela.transacciones_bancarias t
          WHERE t.no_banco = b.no_banco
            AND t.saldo IS NOT NULL
            AND ABS(t.saldo) > 0.5
            AND t.fecha <= CURRENT_DATE
          ORDER BY t.fecha DESC, t.id_transaccion DESC
          LIMIT 1
       ), 0) - {SUMA_FIRMADA_SQL.format(banco='b.no_banco')}, 2),
       'siembra',
       'Calculada del saldo que el Balance publicaba al migrar a saldo '
       'derivado, para que el cambio de fuente no moviera ningún número.'
  FROM scintela.banco b
 ON CONFLICT (no_banco) DO NOTHING
"""

_listo = False


def _bootstrap() -> None:
    """Crea la tabla y siembra las aperturas que falten. Fail-soft."""
    global _listo
    if _listo:
        return
    try:
        _db.execute(_BOOTSTRAP_SQL)
        _db.execute(_SIEMBRA_SQL)
        _listo = True
    except Exception as exc:  # noqa: BLE001
        # Si esto falla, `saldo_bancos()` cae al running guardado — el
        # comportamiento de siempre. Nunca dejamos el Balance sin número.
        _LOG.exception("bootstrap de banco_apertura falló: %s", exc)


def aperturas() -> dict[int, float]:
    """{no_banco: saldo_apertura}. Vacío si la tabla todavía no existe."""
    _bootstrap()
    try:
        filas = _db.fetch_all(
            "SELECT no_banco, saldo_apertura FROM scintela.banco_apertura"
        ) or []
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("no pude leer banco_apertura: %s", exc)
        return {}
    return {int(f["no_banco"]): float(f["saldo_apertura"] or 0) for f in filas}


# ⭐ TMT 2026-08-14 — QUÉ APERTURA PUEDE DESMENTIR A LA PRIMERA FILA DEL BANCO.
#
# `bank_helpers.contar_quiebres` arrancaba en la SEGUNDA fila (`saldo_prev IS
# NOT NULL`): un saldo mal estampado en la fila más VIEJA no lo veía nadie —
# ni el alta de un movimiento ni `/admin/health/all`— porque una fila sin
# predecesora no puede mostrar un salto. Nos costó una sesión: una ND huérfana
# como primera fila de Pichincha pasaba desapercibida en base virgen y sólo
# aparecía cuando dejaba de ser la primera.
#
# Contra qué contrastarla: la apertura guardada acá. Pero NO cualquiera —
# tiene que ser un número que NO salga de las transacciones, o el chequeo es
# circular y da OK siempre (un candado que no se puede activar se ve igual que
# uno que anda). [[feedback_el_dato_a_la_vista_mata_al_aviso]]
#
#   · `'afirmada'` — la puso una PERSONA por `/bancos/apertura` o confirmando
#     el re-encadenado hacia atrás contra el extracto. Es una afirmación
#     externa: sirve. En producción la de Pichincha (10) y la de DEP.PICH. (90).
#   · `'siembra'`  — la calculó `_SIEMBRA_SQL` como `saldo de la última fila −
#     suma de los movimientos`, o sea DESDE las transacciones. Con ella, la
#     cuenta de la primera fila queda implicada por los eslabones de adentro:
#     si todos encadenan, cierra sola (no mide nada); y si hay UN quiebre
#     interno de X, la resta lo devuelve como un −X en la primera fila, o sea
#     le echa la culpa a la fila más vieja de algo que pasó en el medio. Peor
#     que no chequear. En producción es el caso de INTERNACI (32).
#
# Un banco con apertura sembrada, entonces, SIGUE con el hueco abierto, y eso
# se dice en voz alta: `apertura_para_candado` devuelve el motivo y el health
# lo publica en `primera_fila_chequeada`. Es un hueco declarado, no heredado en
# silencio. Se cierra el día que alguien afirme esa apertura por la pantalla —
# no hace falta tocar código.
ORIGENES_QUE_AFIRMAN_LA_APERTURA: frozenset[str] = frozenset({"afirmada"})


def apertura_para_candado(no_banco: int, conn=None) -> tuple[float | None, str]:
    """La apertura contra la cual SÍ se puede chequear la primera fila.

    Devuelve `(apertura, motivo)`. Con `apertura=None` el candado se comporta
    como hasta el 14/08 (arranca en la segunda fila) y `motivo` dice por qué
    ese banco no se puede autochequear — para que quede escrito y visible.

    Deliberadamente NO llama a `_bootstrap()`: esto lo consulta el alta de un
    movimiento, adentro de su `db.tx()`. Una lectura que se pone a crear
    tablas y a sembrar filas dentro del write de otro no es una lectura. Si la
    tabla todavía no existe, no hay apertura y listo.

    El `to_regclass` de arriba no es adorno: si la tabla no existe, el SELECT
    falla y en Postgres una sentencia fallida **aborta la transacción entera**
    — el `try/except` atraparía el error pero el `db.tx()` del caller ya no
    podría commitear nada. Preguntar primero es lo que hace que este chequeo
    no pueda romper el alta que vino a cuidar. TMT 2026-08-14.
    """
    # ⭐ TMT 2026-08-14 (Tamara) — los códigos 90/91 NO son bancos: son medios de
    # cobranza. "DEP.PICH." no es otro banco, es que se depositó DIRECTO en
    # Pichincha; el movimiento real cae en el banco 10 (ver
    # `cheques.queries._banco_real_para_deposito`, paridad ALTAS.PRG L171
    # `BASE = IIF(NB=90,'PICHINCHA','INTER')`). La convención `no_banco < 90` ya
    # vive en cuatro lugares del código.
    #
    # Importa acá porque un rubro de cobranza no tiene saldo corriente que
    # cuidar: las 2 filas que hoy cuelgan de DEP.PICH. son residuo conocido
    # (−455,89 que el Balance nunca vio), no un ledger. Chequearles la "primera
    # fila" contra una apertura de 0 sería inventarle una cadena a algo que no
    # la tiene, y en el health saldría como si esa cuenta estuviera vigilada.
    if int(no_banco) >= 90:
        return None, (f"el código {no_banco} es un medio de cobranza, no una "
                      f"cuenta bancaria: no tiene saldo corriente que chequear")
    try:
        existe = _db.fetch_one(
            "SELECT to_regclass('scintela.banco_apertura') AS t", conn=conn)
        if not (existe and existe.get("t")):
            return None, "todavía no existe scintela.banco_apertura"
        fila = _db.fetch_one(
            "SELECT saldo_apertura, origen FROM scintela.banco_apertura "
            " WHERE no_banco = %s",
            (int(no_banco),), conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: nunca frenar un alta
        _LOG.exception("no pude leer la apertura del banco %s: %s", no_banco, exc)
        return None, "no pude leer banco_apertura"
    if not fila or fila.get("saldo_apertura") is None:
        return None, (f"el banco {no_banco} no tiene apertura declarada: su "
                      f"primera fila no se contrasta contra nada")
    origen = (fila.get("origen") or "").strip().lower()
    if origen not in ORIGENES_QUE_AFIRMAN_LA_APERTURA:
        return None, (f"la apertura del banco {no_banco} es de origen "
                      f"'{origen or 'sin origen'}', calculada desde las "
                      f"transacciones: chequear la primera fila contra ella "
                      f"sería circular")
    return round(float(fila["saldo_apertura"]), 2), "afirmada"


def fijar(no_banco: int, saldo_apertura: float, *, usuario: str = "",
          nota: str = "", origen: str = "afirmada", conn=None) -> None:
    """Deja asentado que una PERSONA afirma la apertura de este banco.

    Se llama desde el re-encadenado hacia atrás cuando alguien confirma la
    apertura contra el extracto: ese es exactamente el momento en que el
    número deja de ser una inferencia y pasa a ser una afirmación.
    """
    _bootstrap()
    _db.execute(
        """
        INSERT INTO scintela.banco_apertura
               (no_banco, saldo_apertura, origen, nota, usuario, fijado_en)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (no_banco) DO UPDATE
           SET saldo_apertura = EXCLUDED.saldo_apertura,
               origen         = EXCLUDED.origen,
               nota           = EXCLUDED.nota,
               usuario        = EXCLUDED.usuario,
               fijado_en      = NOW()
        """,
        (int(no_banco), round(float(saldo_apertura), 2), origen[:40],
         (nota or "")[:400], (usuario or "")[:50]),
        conn=conn,
    )


def panel() -> list[dict]:
    """Una fila por banco: qué dice la apertura y qué dicen los movimientos.

    ⭐ TMT 2026-08-04 — POR QUÉ HAY PANTALLA. La apertura es, desde hoy, el
    único número guardado del que cuelga el saldo de un banco. Un dato así no
    puede vivir sólo en la base: tiene que poder mirarse y corregirse por una
    pantalla, como todo lo demás. [[operar-por-la-ui]]

    El caso que la pidió: **DEP.PICH. arrastraba −455,89 que no eran plata.**
    Sus dos únicos movimientos son un `ND ANUL ch77436 err carga` del 23/06
    (Alex, posteado a DEP por error) y su `NC` reverso del 25/06 (Tamara). Se
    cancelan: el saldo de la última fila es **0,00**, que es la verdad. Pero
    `saldo_bancos()` saltea las filas con `ABS(saldo) <= 0.5` —una guarda vieja
    contra filas en cero por error— así que leía la ANTERIOR y publicaba
    −455,89. La siembra copió ese −455,89 para no mover nada al migrar.
    Con el saldo ya derivado la guarda sobra: una suma no tiene que adivinar
    si un cero es real. Dueña: *"ya podemos eliminar ese 455 no sé qué es"*.
    """
    _bootstrap()
    filas = _db.fetch_all(
        f"""
        SELECT b.no_banco, COALESCE(b.nombre,'') AS nombre,
               ap.saldo_apertura, ap.origen, ap.nota,
               ap.usuario, ap.fijado_en,
               {SUMA_FIRMADA_SQL.format(banco='b.no_banco')} AS suma,
               (SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco AND t.saldo IS NOT NULL
                   AND t.fecha <= CURRENT_DATE
                 ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1
               ) AS ultimo_saldo,
               (SELECT COUNT(*) FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco) AS n_mov
          FROM scintela.banco b
          LEFT JOIN scintela.banco_apertura ap ON ap.no_banco = b.no_banco
         ORDER BY b.no_banco
        """
    ) or []
    out = []
    for f in filas:
        ap = (None if f.get("saldo_apertura") is None
              else float(f["saldo_apertura"]))
        suma = float(f.get("suma") or 0)
        ult = (None if f.get("ultimo_saldo") is None
               else float(f["ultimo_saldo"]))
        # Lo que los MOVIMIENTOS dicen que debería ser la apertura, tomando
        # como bueno el saldo corrido de la última fila — SIN la guarda del
        # `ABS(saldo) > 0.5`, que es justo la que inventaba los −455,89.
        sugerida = (None if ult is None else round(ult - suma, 2))
        out.append({
            "no_banco": int(f["no_banco"]),
            "nombre": (f.get("nombre") or "").strip() or f"Banco {f['no_banco']}",
            "apertura": ap,
            "origen": (f.get("origen") or ""),
            "nota": (f.get("nota") or ""),
            "usuario": (f.get("usuario") or ""),
            "fijado_en": f.get("fijado_en"),
            "suma": suma,
            "ultimo_saldo": ult,
            "saldo_publicado": (None if ap is None else round(ap + suma, 2)),
            "sugerida": sugerida,
            "difiere": (ap is not None and sugerida is not None
                        and abs(ap - sugerida) > 0.02),
            "n_mov": int(f.get("n_mov") or 0),
        })
    return out
