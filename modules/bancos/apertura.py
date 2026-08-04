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
