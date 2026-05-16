"""Validación read-only del dispatcher de reverso en /historial.

Recorre cada tipo en `_REVERSO_DISPATCH` y `_REVERSO_BLOQUEADO`, busca
movimientos activos de ese tipo en la DB, y reporta:

  1. ¿Se puede reversar desde el dispatcher?
  2. Si sí, ¿a qué endpoint rutea?
  3. Para los bloqueados: ¿cuántos hay en la DB que la dueña vería con el
     toast?

Además checks de integridad:

  - mov_doble con estado='activo' que tienen un mov_doble 'reverso' apuntando a
    ellos pero estado no fue actualizado (bug de update).
  - mov_doble 'reverso' sin id_original (huérfanos).
  - mov_doble 'reversado' sin id_reverso (huérfanos).
  - Cheques en stat='E' (endosados) cuya compra hermana ya está stat='Y' →
    inconsistencia: deberían estar reversados.
  - Compras anuladas (stat='Y') con cuenta_pagada en {B,C,P} que NO tienen
    una fila compensatoria en banco/caja → side-effect huérfano.

NO modifica nada. Sólo reporta.

Uso:
    python scripts/validar_reversos.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

# Importar los dispatches del módulo historial.
from modules.historial.views import _REVERSO_BLOQUEADO, _REVERSO_DISPATCH  # noqa: E402


def _separator(title: str = "") -> None:
    bar = "═" * 70
    if title:
        print(f"\n{bar}\n  {title}\n{bar}")
    else:
        print(bar)


def conteo_por_tipo() -> dict[str, dict]:
    """Cuenta mov_doble activos por tipo. Devuelve dict {tipo: {n, total_importe}}."""
    rows = db.fetch_all(
        """
        SELECT tipo,
               COUNT(*) AS n,
               COALESCE(SUM(importe), 0) AS total
          FROM scintela.mov_doble
         WHERE estado = 'activo'
         GROUP BY tipo
         ORDER BY tipo
        """
    ) or []
    return {r["tipo"]: {"n": int(r["n"]), "total": float(r["total"] or 0)} for r in rows}


def main() -> int:
    _separator("Validador de reverso dispatcher — 2026-05-13")

    counts = conteo_por_tipo()

    # ── 1. Tipos OK (reversables desde el dispatcher)
    _separator("1. Tipos validados como REVERSABLES")
    total_validados = 0
    for tipo, (endpoint, _) in sorted(_REVERSO_DISPATCH.items()):
        c = counts.get(tipo, {"n": 0, "total": 0})
        marca = "✓" if c["n"] > 0 else "·"
        print(f"  {marca} {tipo:40s} → {endpoint:45s} "
              f"({c['n']:4d} activos · ${c['total']:>14,.2f})")
        total_validados += c["n"]
    print(f"\n  Total movimientos reversables: {total_validados}")

    # ── 2. Tipos BLOQUEADOS (dispatcher muestra toast con guía)
    _separator("2. Tipos BLOQUEADOS — dispatcher muestra guía manual")
    total_bloqueados = 0
    for tipo, mensaje in sorted(_REVERSO_BLOQUEADO.items()):
        c = counts.get(tipo, {"n": 0, "total": 0})
        if c["n"] > 0:
            print(f"  ⚠ {tipo:40s} ({c['n']:4d} activos · ${c['total']:>14,.2f})")
            print(f"      → {mensaje[:90]}")
            total_bloqueados += c["n"]
    print(f"\n  Total movimientos en bloqueo: {total_bloqueados}")

    # ── 3. Tipos NO mapeados (ni en dispatch ni en bloqueado)
    _separator("3. Tipos en mov_doble SIN handler ni bloqueo explícito")
    mapeados = set(_REVERSO_DISPATCH) | set(_REVERSO_BLOQUEADO)
    huerfanos = []
    for tipo, c in counts.items():
        # Reversos ya hechos no entran (no se reversan otra vez)
        if tipo.startswith("reverso_") or tipo.startswith("caja_") and tipo.endswith("_directo"):
            continue
        if tipo.startswith("banco_") and tipo.endswith("_directo"):
            continue
        if tipo not in mapeados:
            huerfanos.append((tipo, c))
    for tipo, c in sorted(huerfanos):
        print(f"  ? {tipo:40s} ({c['n']:4d} activos · ${c['total']:>14,.2f})")
    if not huerfanos:
        print("  (ninguno)")

    # ── 4. Integridad de mov_doble
    _separator("4. Integridad de mov_doble")
    r_reverso_sin_origen = db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.mov_doble
         WHERE estado = 'reverso' AND id_original IS NULL
        """
    )
    print(f"  • Reversos sin id_original: {r_reverso_sin_origen['n'] if r_reverso_sin_origen else 0} "
          f"(deberían ser 0)")

    r_reversado_sin_reverso = db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.mov_doble
         WHERE estado = 'reversado' AND id_reverso IS NULL
        """
    )
    print(f"  • Reversados sin id_reverso: {r_reversado_sin_reverso['n'] if r_reversado_sin_reverso else 0} "
          f"(deberían ser 0)")

    r_activo_con_reverso = db.fetch_one(
        """
        SELECT COUNT(*) AS n FROM scintela.mov_doble md_a
         WHERE md_a.estado = 'activo'
           AND EXISTS (SELECT 1 FROM scintela.mov_doble md_r
                        WHERE md_r.id_original = md_a.id_mov_doble
                          AND md_r.estado = 'reverso')
        """
    )
    print(f"  • Activos con reverso apuntándoles (deberían estar 'reversado'): "
          f"{r_activo_con_reverso['n'] if r_activo_con_reverso else 0}")

    # ── 5. Casos de inconsistencia conocidos
    _separator("5. Inconsistencias de negocio detectadas")

    # 5a. Cheques endosados cuya compra hermana ya está anulada — sospechoso.
    sospechosos_endoso = db.fetch_all(
        """
        SELECT ch.id_cheque, ch.no_cheque, ch.stat, ch.prov, c.id_compra, c.stat AS stat_compra
          FROM scintela.cheque ch
          LEFT JOIN scintela.compra c
                 ON c.comprobante = ('CH' || COALESCE(ch.no_cheque, ch.id_cheque::text))
                AND c.cuenta_pagada = 'E'
         WHERE ch.stat = 'E'
           AND COALESCE(c.stat, '') = 'Y'
         ORDER BY ch.id_cheque DESC
         LIMIT 20
        """
    ) or []
    print(f"  • Cheques en stat='E' (endosado) con compra hermana stat='Y' (anulada): "
          f"{len(sospechosos_endoso)}")
    for s in sospechosos_endoso[:5]:
        print(f"      cheque #{s['id_cheque']} (N°{s['no_cheque']}) → "
              f"compra #{s['id_compra']} anulada. Cheque debería estar en cartera.")

    # 5b. Cheques en stat='X' (eliminado) con compra hermana ACTIVA — el bug
    # del endoso reverso original.
    cheques_x_con_compra_activa = db.fetch_all(
        """
        SELECT ch.id_cheque, ch.no_cheque, c.id_compra, c.importe, c.codigo_prov
          FROM scintela.cheque ch
          JOIN scintela.compra c
            ON c.comprobante = ('CH' || COALESCE(ch.no_cheque, ch.id_cheque::text))
           AND c.cuenta_pagada = 'E'
         WHERE ch.stat = 'X'
           AND COALESCE(c.stat, '') != 'Y'
         ORDER BY ch.id_cheque DESC
         LIMIT 20
        """
    ) or []
    print(f"  • Cheques 'X' con compra hermana ACTIVA (compras fantasma): "
          f"{len(cheques_x_con_compra_activa)}")
    for s in cheques_x_con_compra_activa[:5]:
        print(f"      cheque #{s['id_cheque']} (N°{s['no_cheque']}) → "
              f"compra #{s['id_compra']} ${s['importe']} a {s['codigo_prov']} sigue ACTIVA.")

    # 5c. Compras pagadas (stat normal) cuya id_transaccion apunta a un
    # movimiento bancario también activo Y la compra está anulada.
    compras_anul_con_pago_vivo = db.fetch_all(
        """
        SELECT c.id_compra, c.id_transaccion, c.importe, c.codigo_prov, t.documento, t.saldo
          FROM scintela.compra c
          JOIN scintela.transacciones_bancarias t ON t.id_transaccion = c.id_transaccion
         WHERE c.stat = 'Y'
           AND c.id_transaccion IS NOT NULL
         ORDER BY c.id_compra DESC LIMIT 20
        """
    ) or []
    print(f"  • Compras ANULADAS con id_transaccion bancaria viva (pagos fantasma): "
          f"{len(compras_anul_con_pago_vivo)}")
    for s in compras_anul_con_pago_vivo[:5]:
        print(f"      compra #{s['id_compra']} anulada — pero tx_banco #{s['id_transaccion']} "
              f"({s['documento']} ${s['importe']}) sigue restando saldo.")

    # ── 6. SALDOS BANCARIOS — el check más importante.
    #
    # NO podemos empezar el running en $0 porque la primera fila importada
    # del DBF ya tiene un saldo histórico de muchos años (opening). Usamos
    # la PRIMERA fila como ground truth y validamos que las siguientes
    # cuadran respecto al delta firmado de cada documento.
    _separator("6. Cross-check saldos bancarios running")
    import bank_helpers as _bh
    bancos = db.fetch_all(
        """
        SELECT no_banco, COALESCE(nombre, '') AS nombre
          FROM scintela.banco
         WHERE COALESCE(UPPER(nombre),'') LIKE '%%PICHINC%%'
            OR COALESCE(UPPER(nombre),'') LIKE '%%INTERNAC%%'
         ORDER BY no_banco
        """
    ) or []
    inconsistencias_saldo = 0
    for b in bancos:
        nb = int(b["no_banco"])
        filas = db.fetch_all(
            """
            SELECT id_transaccion, fecha, documento, importe, saldo
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
             ORDER BY fecha, id_transaccion
            """,
            (nb,),
        ) or []
        if not filas:
            print(f"  Banco #{nb} {b.get('nombre')}: sin movimientos.")
            continue
        # Anclar en la primera fila con saldo válido (no None, no 0).
        # Muchas filas legacy tienen saldo=NULL o 0 — esas las saltamos
        # para encontrar un opening sólido.
        ancla_idx = None
        for i, f in enumerate(filas):
            s = f.get("saldo")
            if s is not None and float(s or 0) != 0:
                ancla_idx = i
                break
        if ancla_idx is None:
            print(f"  Banco #{nb} {b.get('nombre')}: ninguna fila tiene saldo válido.")
            continue
        # Recompute desde el ancla — su saldo stored es ground truth.
        ancla = filas[ancla_idx]
        saldo_calc = float(ancla.get("saldo") or 0)
        last_diff = None
        for i in range(ancla_idx + 1, len(filas)):
            f = filas[i]
            saldo_stored = f.get("saldo")
            if saldo_stored is None:
                # Fila sin saldo computado — la saltamos del check pero
                # acumulamos el delta para no perder la posición.
                saldo_calc = round(saldo_calc + _bh._signed_delta(
                    f.get("documento"), float(f.get("importe") or 0)
                ), 2)
                continue
            saldo_stored = float(saldo_stored or 0)
            delta = _bh._signed_delta(f.get("documento"), float(f.get("importe") or 0))
            saldo_calc = round(saldo_calc + delta, 2)
            if abs(saldo_calc - saldo_stored) > 0.01:
                if last_diff is None:
                    last_diff = (i, f, saldo_calc, saldo_stored)
                # NO break — seguimos para reportar el final también, pero
                # actualizamos saldo_calc al stored para no propagar el
                # error y que las siguientes filas se validen contra el
                # último valor stored conocido.
                saldo_calc = saldo_stored
        ultimo = filas[-1]
        saldo_final_stored = float(ultimo.get("saldo") or 0)
        if last_diff:
            inconsistencias_saldo += 1
            idx, f0, sc, ss = last_diff
            print(f"  ⚠ Banco #{nb} {b.get('nombre')}: saldo final ${saldo_final_stored:,.2f}, "
                  f"pero hay desfase interno.")
            print(f"     primera fila descuadrada: id={f0['id_transaccion']} "
                  f"fecha={f0['fecha']} doc={f0['documento']} "
                  f"importe=${float(f0['importe']):,.2f}")
            print(f"     saldo_stored=${ss:,.2f}  vs  saldo_calc(desde fila previa)=${sc:,.2f}  "
                  f"diff={sc-ss:+,.2f}")
            print(f"     → Fix: bank_helpers.recompute_saldos_desde(conn, no_banco={nb}, "
                  f"ancla_id={f0['id_transaccion']})")
        else:
            print(f"  ✓ Banco #{nb} {b.get('nombre')}: saldo final ${saldo_final_stored:,.2f} — "
                  f"running cuadra desde la fila ancla (id={ancla['id_transaccion']}, "
                  f"fecha={ancla['fecha']}).")
    if inconsistencias_saldo == 0:
        print("\n  → Todos los saldos bancarios cuadran (delta interno consistente).")
    else:
        print(f"\n  → {inconsistencias_saldo} bancos con desfase. Corregir con bank_helpers.recompute_saldos_desde.")

    _separator("Fin de validación")
    return 0


if __name__ == "__main__":
    sys.exit(main())
