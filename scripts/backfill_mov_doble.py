"""Backfill retroactivo de mov_doble para movimientos legacy del DBF.

Las cajas importadas del DBF tienen conceptos como `PICH 1234` o `RR TMT`
que disparan un movimiento doble pero NO pasaron por `caja.crear()` (que
es la que registra `mov_doble`). Este script:

  1. Busca filas de scintela.caja con concepto matcheable (PICH/INTER/
     RR/IN.XX/INHB/PR <prov>) que NO tengan mov_doble.
  2. Para cada una, intenta matchear con la fila correspondiente en su
     tabla destino (transacciones_bancarias / retiros / dolares / compra)
     buscando por **misma fecha y mismo importe**.
  3. Si encuentra un match único, crea un mov_doble retroactivo enlazando
     ambas filas. Si hay 0 matches o >1 candidatos, lo lista como
     "huérfano" o "ambiguo" para revisión manual.

Uso:
    python scripts/backfill_mov_doble.py                  # DRY RUN
    python scripts/backfill_mov_doble.py --apply          # ejecuta
    python scripts/backfill_mov_doble.py --limit 50       # procesa solo 50

NO modifica las filas existentes — solo INSERTA nuevas filas en
scintela.mov_doble. Si algo sale mal, podés borrar las filas creadas
con `DELETE FROM scintela.mov_doble WHERE estado='activo' AND
metadata->>'backfill' = 'true'`.

NUNCA toca los saldos, importes ni nada que afecte el balance.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


# ─────────────────────────── Helpers ───────────────────────────


def cargar_contexto():
    """Devuelve dict con provs_validos + bancos_por_nombre."""
    provs = {
        (r.get("codigo_prov") or "").strip().upper()
        for r in (db.fetch_all("SELECT codigo_prov FROM scintela.proveedor") or [])
    }
    bancos: dict = {}
    for b in db.fetch_all(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco"
    ) or []:
        n = (b.get("nombre") or "").upper().strip()
        if "PICHINC" in n:
            bancos.setdefault("PICHINCHA", int(b["no_banco"]))
        if "INTER" in n and "ANTIC" not in n and "DEP" not in n:
            bancos.setdefault("INTERNACIONAL", int(b["no_banco"]))
    return {"provs_validos": provs, "bancos": bancos}


def buscar_match_banco(*, fecha, importe, no_banco, tipo_caja_origen) -> list[dict]:
    """Buscar tx_bancarias del mismo día e importe que matchea la caja.

    Si la caja egresa (S), el banco recibe (documento DE).
    Si la caja ingresa (E), el banco egresa (documento CH).
    """
    documento = "DE" if tipo_caja_origen == "S" else "CH"
    return db.fetch_all(
        """
        SELECT id_transaccion, no_banco, documento, importe, concepto, fecha
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
           AND fecha = %s
           AND documento = %s
           AND ABS(ABS(importe) - %s) < 0.51
           AND NOT EXISTS (SELECT 1 FROM scintela.mov_doble m
                           WHERE m.destino_table = 'transacciones_bancarias'
                             AND m.destino_id = id_transaccion)
        """,
        (no_banco, fecha, documento, float(importe)),
    ) or []


def buscar_match_retiro(*, fecha, importe, socio) -> list[dict]:
    """Buscar retiro del mismo día, socio e importe."""
    socio = (socio or "").strip().upper()
    return db.fetch_all(
        """
        SELECT id_retiro, fecha, ret, de, concepto
          FROM scintela.retiros
         WHERE fecha = %s
           AND ABS(COALESCE(ret, 0) - %s) < 0.51
           AND (%s = '' OR UPPER(COALESCE(de,'')) = %s)
           AND NOT EXISTS (SELECT 1 FROM scintela.mov_doble m
                           WHERE m.destino_table = 'retiros'
                             AND m.destino_id = id_retiro)
        """,
        (fecha, float(importe), socio, socio),
    ) or []


def buscar_match_dolares(*, fecha, importe, cta) -> list[dict]:
    """Buscar movimiento dolares del mismo día, cta e importe."""
    cta = (cta or "").strip().upper()
    return db.fetch_all(
        """
        SELECT id_dolares, fecha, cta, importe, concepto
          FROM scintela.dolares
         WHERE fecha = %s
           AND ABS(ABS(COALESCE(importe, 0)) - %s) < 0.51
           AND (%s = '' OR UPPER(COALESCE(cta,'')) = %s)
           AND NOT EXISTS (SELECT 1 FROM scintela.mov_doble m
                           WHERE m.destino_table = 'dolares'
                             AND m.destino_id = id_dolares)
        """,
        (fecha, float(importe), cta, cta),
    ) or []


def buscar_match_compra(*, fecha, importe, prov) -> list[dict]:
    """Buscar compra del mismo día, prov e importe."""
    prov = (prov or "").strip().upper()
    return db.fetch_all(
        """
        SELECT id_compra, fecha, codigo_prov, importe, concepto, numero
          FROM scintela.compra
         WHERE fecha = %s
           AND ABS(COALESCE(importe, 0) - %s) < 0.51
           AND UPPER(COALESCE(codigo_prov, '')) = %s
           AND NOT EXISTS (SELECT 1 FROM scintela.mov_doble m
                           WHERE m.destino_table = 'compra'
                             AND m.destino_id = id_compra)
        """,
        (fecha, float(importe), prov),
    ) or []


# ─────────────────────────── Backfill ──────────────────────────


def filas_a_procesar(limit: int) -> list[dict]:
    """Cajas con concepto matcheable y SIN mov_doble."""
    return db.fetch_all(
        """
        SELECT c.id_caja, c.fecha, c.tipo, c.importe, c.concepto, c.usuario_crea
          FROM scintela.caja c
         WHERE (UPPER(COALESCE(c.concepto,'')) LIKE 'PICH%%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'INTER%%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'RR %%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'IN.%%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'INHB%%'
             OR UPPER(COALESCE(c.concepto,'')) LIKE 'PR %%')
           AND NOT EXISTS (SELECT 1 FROM scintela.mov_doble m
                           WHERE (m.origen_table  = 'caja' AND m.origen_id  = c.id_caja)
                              OR (m.destino_table = 'caja' AND m.destino_id = c.id_caja))
         ORDER BY c.fecha DESC, c.id_caja DESC
         LIMIT %s
        """,
        (limit,),
    ) or []


def procesar_fila(caja: dict, ctx: dict) -> dict:
    """Intenta matchear esta fila de caja con su side effect.

    Devuelve {status, tipo, candidatos, mov_doble_data} donde status es:
      - 'match_unico'  → 1 fila destino encontrada
      - 'no_match'     → 0 filas destino encontradas
      - 'ambiguo'      → varias filas destino candidatas
      - 'no_parseado'  → el concepto no se pudo parsear
    """
    import concepto_parser
    parsed = concepto_parser.parse_concepto(caja.get("concepto") or "", ctx)
    tipo_parseado = parsed.get("tipo")
    if not tipo_parseado or tipo_parseado == "none":
        return {"status": "no_parseado", "parsed": parsed, "caja": caja}

    tipo_caja_origen = (caja.get("tipo") or "").strip().upper()
    importe = abs(float(caja.get("importe") or 0))
    fecha = caja.get("fecha")
    candidatos: list = []
    destino_table = None
    md_tipo = f"caja_{tipo_caja_origen.lower()}_to_{tipo_parseado}"

    if tipo_parseado == "transfer_banco":
        no_banco = parsed.get("no_banco")
        if not no_banco:
            return {"status": "no_parseado", "parsed": parsed,
                    "razon": "banco no resoluble", "caja": caja}
        candidatos = buscar_match_banco(
            fecha=fecha, importe=importe, no_banco=no_banco,
            tipo_caja_origen=tipo_caja_origen,
        )
        destino_table = "transacciones_bancarias"
    elif tipo_parseado == "retiro_socio":
        candidatos = buscar_match_retiro(
            fecha=fecha, importe=importe, socio=parsed.get("socio") or "",
        )
        destino_table = "retiros"
    elif tipo_parseado == "dolares":
        candidatos = buscar_match_dolares(
            fecha=fecha, importe=importe, cta=parsed.get("cuenta") or "",
        )
        destino_table = "dolares"
    elif tipo_parseado == "compra_proveedor":
        candidatos = buscar_match_compra(
            fecha=fecha, importe=importe, prov=parsed.get("prov") or "",
        )
        destino_table = "compra"
    else:
        # caja_inhb u otros — sin destino, no creamos mov_doble.
        return {"status": "no_parseado", "parsed": parsed,
                "razon": f"tipo {tipo_parseado} sin destino", "caja": caja}

    if len(candidatos) == 0:
        return {"status": "no_match", "parsed": parsed,
                "destino_table": destino_table, "caja": caja}
    if len(candidatos) > 1:
        return {"status": "ambiguo", "parsed": parsed,
                "destino_table": destino_table, "candidatos": candidatos,
                "caja": caja}

    dest = candidatos[0]
    dest_id_field = {
        "transacciones_bancarias": "id_transaccion",
        "retiros":                 "id_retiro",
        "dolares":                 "id_dolares",
        "compra":                  "id_compra",
    }[destino_table]
    return {
        "status": "match_unico",
        "parsed": parsed,
        "destino_table": destino_table,
        "destino_id": dest[dest_id_field],
        "md_tipo": md_tipo,
        "candidato": dest,
        "caja": caja,
    }


def crear_mov_doble(conn, resultado: dict) -> int | None:
    """Inserta el mov_doble retroactivo dentro del conn dado."""
    import mov_doble as _md
    caja = resultado["caja"]
    new_id = _md.registrar(
        conn=conn,
        tipo=resultado["md_tipo"],
        origen_table="caja",
        origen_id=caja["id_caja"],
        destino_table=resultado["destino_table"],
        destino_id=resultado["destino_id"],
        importe=float(caja.get("importe") or 0),
        fecha=caja.get("fecha"),
        concepto=caja.get("concepto") or "",
        usuario=caja.get("usuario_crea") or "backfill",
        metadata={
            "backfill": True,
            "parsed_tipo": resultado["parsed"].get("tipo"),
            "creado_por_script": "backfill_mov_doble.py",
        },
    )
    return new_id


# ─────────────────────────── Runner ────────────────────────────


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=500,
                    help="Máximo de filas de caja a procesar (default: 500)")
    ap.add_argument("--apply", action="store_true",
                    help="Ejecuta los INSERT. Sin esto, dry-run.")
    args = ap.parse_args()

    print()
    print(f"═══ Backfill mov_doble retroactivo ({'APPLY' if args.apply else 'DRY RUN'}) ═══")
    ctx = cargar_contexto()
    print(f"Contexto: {len(ctx['provs_validos'])} prov(s) válidos · "
          f"bancos: {ctx['bancos']}")
    print()

    filas = filas_a_procesar(args.limit)
    print(f"{len(filas)} filas de caja a evaluar.")
    print()

    counters = {"match_unico": 0, "no_match": 0, "ambiguo": 0, "no_parseado": 0}
    ejemplos: dict[str, list] = {k: [] for k in counters}

    if args.apply:
        conn_ctx = db.tx()
        conn = conn_ctx.__enter__()
    else:
        conn = None

    for caja in filas:
        res = procesar_fila(caja, ctx)
        status = res["status"]
        counters[status] += 1

        if status == "match_unico" and args.apply:
            try:
                new_id = crear_mov_doble(conn, res)
                res["id_mov_doble_nuevo"] = new_id
            except Exception as e:
                res["error_insert"] = str(e)
                counters["match_unico"] -= 1
                counters.setdefault("error_insert", 0)
                counters["error_insert"] = counters.get("error_insert", 0) + 1

        if len(ejemplos[status]) < 5:
            ejemplos[status].append(res)

    if args.apply:
        conn_ctx.__exit__(None, None, None)

    # Reportar
    print(f"  ✓ match_unico (mov_doble creado): {counters['match_unico']}")
    print(f"  ✗ no_match (sin candidato — huérfanos): {counters['no_match']}")
    print(f"  ⚠ ambiguo (varios candidatos — manual): {counters['ambiguo']}")
    print(f"  ⊝ no_parseado (concepto no decodificable): {counters['no_parseado']}")
    print()

    for status, lista in ejemplos.items():
        if not lista:
            continue
        print(f"── Ejemplos de '{status}' (máx 5):")
        for r in lista:
            c = r.get("caja") or {
                "id_caja": "—", "fecha": "—",
                "importe": 0, "concepto": "—", "tipo": "—",
            }
            cstr = (f"caja#{c.get('id_caja')} {c.get('fecha')} "
                    f"tipo={c.get('tipo')} imp=${float(c.get('importe') or 0):,.2f} "
                    f"concepto={c.get('concepto')!r}")
            extras = ""
            if r.get("destino_table"):
                extras += f" → {r['destino_table']}"
            if r.get("destino_id"):
                extras += f"#{r['destino_id']}"
            if r.get("candidatos"):
                extras += f" ({len(r['candidatos'])} candidatos)"
            if r.get("razon"):
                extras += f" — {r['razon']}"
            if r.get("id_mov_doble_nuevo"):
                extras += f"  [creó mov_doble #{r['id_mov_doble_nuevo']}]"
            if r.get("error_insert"):
                extras += f"  [ERROR: {r['error_insert']}]"
            print(f"    • {cstr}{extras}")
        print()

    if not args.apply:
        print("═══ DRY RUN — no se creó ningún mov_doble. ═══")
        print("Para ejecutar de verdad:")
        print(f"  python scripts/backfill_mov_doble.py --limit {args.limit} --apply")
    else:
        print(f"═══ ✓ Listo. {counters['match_unico']} mov_doble retroactivos creados. ═══")
        print("Refrescá /historial para verlos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
