"""Reconstruye el detalle de las fotos anteriores a la grabadora, con nombre.

TMT 2026-08-06: *"sin detalle — foto vieja, esto quiero intentar recomponer"*.

Las ~200 fotos previas a la mig 0171 guardan los once totales y nada más. Se
puede hacer bastante mejor que adivinar, y sin inventar un centavo, combinando
dos cosas que YA están:

  · **cuánta plata se movió**: el Δ exacto de cada componente sale de restar
    las dos filas de `traza_utilidad`. Cierra al centavo por construcción.
  · **qué pasó**: `scintela.mov_doble` tiene `fecha_creacion` como `timestamptz`
    real y 20.405 filas con nombre —cobranzas, depósitos, devoluciones,
    anticipos—. Es NATIVA de Programa Core, así que ninguno de los TRUNCATE del
    sync del dBase la toca (ese fue el error de la 0175, ver su docstring).

Así, una ventana vieja no dice "sin detalle" sino, por ejemplo:

    Cheques   −1.251,87   Depósito de cheque (Z → B)
    Facturas    −907,97   Factura: devolución

⚠ Sigue siendo una RECONSTRUCCIÓN y se marca `tipo = 'reconstruido'`: el Δ del
componente es exacto, pero el nombre es el del evento más plausible de esa
ventana, no una atribución documento por documento. La pantalla lo dice en
ámbar y el aviso de "ventanas que no cierran" las ignora.

Idempotente: borra lo reconstruido antes de rehacerlo, así que se puede correr
las veces que haga falta (y limpia lo que hubiera dejado la 0175).
"""

from __future__ import annotations

import os

#: Δ mínimo para que un componente merezca un renglón.
UMBRAL = 1.0

#: Los 11 componentes y su signo hacia la utilidad. Igual que en `foto.py`.
COMPONENTES = (
    ("caja", 1), ("bancos", 1), ("cheques", 1), ("facturas", 1),
    ("antic", 1), ("vsto", 1), ("vqx", 1), ("umaq", 1), ("uact", 1),
    ("totp", -1), ("uret", 1),
)

ETIQUETAS = {
    "caja": "Caja", "bancos": "Bancos", "cheques": "Cheques",
    "facturas": "Facturas", "antic": "Anticipos", "vsto": "Stock MP+Prod.",
    "vqx": "Stock Químicos", "umaq": "Maquinaria", "uact": "Terrenos/Edif.",
    "totp": "Pasivos", "uret": "Dividendos",
}

#: Tabla de `mov_doble` → componente que mueve. `compra` queda afuera a
#: propósito: una compra toca posdat, caja o bancos según cómo se pague, y el
#: mov_doble de la compra en sí no dice cuál.
TABLA_COMP = {
    "factura": "facturas", "cheque": "cheques", "caja": "caja",
    "dolares": "antic", "posdat": "totp", "retiros": "uret",
    "transacciones_bancarias": "bancos",
}


def run(conn) -> None:
    cur = conn.cursor()

    # Lo reconstruido se rehace entero: es derivado, no es historia.
    cur.execute("DELETE FROM scintela.dia_movimiento WHERE tipo = 'reconstruido'")

    # Las fotos sin detalle propio, con su ventana y sus once componentes.
    cols = ", ".join(c for c, _s in COMPONENTES)
    lags = ", ".join(f"LAG({c}) OVER w AS {c}_ant" for c, _s in COMPONENTES)
    cur.execute(
        f"""
        WITH v AS (
            SELECT id_traza, creado_en, utilidad, {cols},
                   LAG(creado_en) OVER w AS desde,
                   LAG(utilidad)  OVER w AS utilidad_ant, {lags}
              FROM scintela.traza_utilidad
            WINDOW w AS (ORDER BY creado_en, id_traza)
        )
        SELECT * FROM v
         WHERE desde IS NOT NULL
           -- 🚨 Sólo las ANTERIORES a que arrancara la grabadora. Una foto
           -- nueva en la que legítimamente no se movió nada tampoco tiene
           -- filas, y reconstruirla la marcaría "puede faltar lo cobrado"
           -- sobre una ventana que sí tiene registro y está completa.
           AND v.id_traza < COALESCE(
                 (SELECT MIN(id_traza) FROM scintela.dia_movimiento
                   WHERE id_traza IS NOT NULL AND tipo <> 'reconstruido'),
                 9223372036854775807)
           AND NOT EXISTS (
                 SELECT 1 FROM scintela.dia_movimiento m
                  WHERE m.id_traza = v.id_traza)
         ORDER BY id_traza
        """
    )
    campos = [d[0] for d in cur.description]
    fotos = [dict(zip(campos, r, strict=False)) for r in cur.fetchall()]
    if not fotos:
        cur.close()
        return

    # Los eventos de todo el rango, de una sola vez.
    cur.execute(
        """
        SELECT fecha_creacion, tipo, origen_table, destino_table
          FROM scintela.mov_doble
         WHERE fecha_creacion >= %s AND fecha_creacion <= %s
         ORDER BY fecha_creacion
        """,
        (min(f["desde"] for f in fotos), max(f["creado_en"] for f in fotos)),
    )
    eventos = cur.fetchall()

    try:
        from modules.historial.queries import TIPOS_LABEL
    except Exception:  # noqa: BLE001 -- la migración no puede depender de la app
        TIPOS_LABEL = {}

    filas, ventanas = [], 0
    for f in fotos:
        # Qué tipos de evento tocaron cada componente en esta ventana.
        por_comp: dict[str, list[str]] = {}
        for fecha, tipo, orig, dest in eventos:
            if not (f["desde"] < fecha <= f["creado_en"]):
                continue
            for tabla in (orig, dest):
                comp = TABLA_COMP.get((tabla or "").strip())
                if comp:
                    nombre = TIPOS_LABEL.get(tipo, (tipo or "").replace("_", " "))
                    if nombre not in por_comp.setdefault(comp, []):
                        por_comp[comp].append(nombre)

        hubo = False
        for comp, signo in COMPONENTES:
            d = float(f.get(comp) or 0) - float(f.get(f"{comp}_ant") or 0)
            if abs(d) < UMBRAL:
                continue
            nombres = por_comp.get(comp) or []
            if len(nombres) == 1:
                etiqueta = nombres[0]
            elif nombres:
                etiqueta = f"{nombres[0]} y {len(nombres) - 1} tipo(s) más"
            else:
                etiqueta = f"{ETIQUETAS[comp]}: sin detalle registrado"
            filas.append((f["id_traza"], comp, "reconstruido",
                          f"#recon:{comp}", etiqueta,
                          round(d, 2), round(d * signo, 2),
                          ETIQUETAS[comp], "utilidad"))
            hubo = True
        ventanas += 1 if hubo else 0

    for i in range(0, len(filas), 500):
        trozo = filas[i:i + 500]
        marcas = ", ".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(trozo))
        cur.execute(
            "INSERT INTO scintela.dia_movimiento (id_traza, componente, tipo,"
            " doc_id, etiqueta, delta, aporte, regla, familia) VALUES " + marcas,
            [v for fila in trozo for v in fila])
    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        print(f"    {len(filas)} renglón(es) reconstruido(s) en {ventanas} ventana(s)")
