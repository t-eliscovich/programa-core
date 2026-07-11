#!/usr/bin/env python3
"""Audit de facturas PC sin match en Asinfo.

Imprime las facturas registradas en Programa Core que:
- Tienen fecha >= 2025-01-01 (post-cutoff de Asinfo)
- Tienen kg != 0 (no son NC financieras)
- NO matchean ni por numero completo ni por sufijo numérico
  ni en el índice FACTURA ni en el índice DEVOLUCION de Asinfo

Reusa la lógica exacta de modules/facturas/views.py::lista() — pero standalone,
para correr antes de que el deploy esté en producción.

Uso:
    cd /Users/tamaraeliscovich/Documents/Claude/Projects/Programa Core
    source .env.prod  # o exportar manualmente las envs
    .venv/bin/python scripts/audit_huerfanas.py

Requisitos (env vars):
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD    — RDS prod
    METABASE_URL, METABASE_USERNAME, METABASE_PASSWORD — Metabase
    ASINFO_CARD_FACTURAS                                — card id (199 al 2026-05-21)
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import date

# Asegurar que podemos importar los modules del proyecto
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Cargar .env automáticamente (busca primero .env.prod, luego .env)
for _env_name in (".env.prod", ".env"):
    _env_path = os.path.join(_ROOT, _env_name)
    if os.path.isfile(_env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(_env_path, override=False)
            print(f"[env] Cargado {_env_name}", file=sys.stderr)
            break
        except ImportError:
            # Fallback manual si python-dotenv no está instalado
            with open(_env_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip('"').strip("'")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
            print(f"[env] Cargado {_env_name} (manual)", file=sys.stderr)
            break

import psycopg2
import psycopg2.extras

ASINFO_CUTOFF = date(2025, 1, 1)


def cargar_facturas_pc() -> list[dict]:
    """Lista TODAS las facturas vivas de PC desde el cutoff."""
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=10,
    )
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT f.id_factura, f.numf, f.numf_completo, f.fecha,
                   f.codigo_cli, COALESCE(c.nombre, '') AS cliente,
                   f.kg, f.importe, f.saldo, f.stat
              FROM scintela.factura f
              LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
             WHERE f.fecha >= %s
               AND f.kg <> 0
               AND COALESCE(f.stat, '') IN ('', ' ', 'Z', 'A', 'T')
             ORDER BY f.fecha DESC
            """,
            (ASINFO_CUTOFF,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def cargar_asinfo_periodo(desde: date, hasta: date) -> list[dict]:
    """Trae las facturas de Asinfo via Metabase card."""
    from modules.asinfo import service as asinfo_service
    return asinfo_service.facturas_periodo(desde, hasta)


def detectar_huerfanas(filas_pc: list[dict], filas_asinfo: list[dict]) -> list[dict]:
    """Match por numero completo y sufijo, doble índice por TIPO."""
    idx_factura_completo: dict[str, dict] = {}
    idx_factura_numf: dict[int, dict] = {}
    idx_devolucion_completo: dict[str, dict] = {}
    idx_devolucion_numf: dict[int, dict] = {}

    for r in filas_asinfo:
        tipo = r.get("tipo")
        numero = r.get("numero")
        if not numero:
            continue
        if tipo == "FACTURA":
            c_idx, n_idx = idx_factura_completo, idx_factura_numf
        elif tipo == "DEVOLUCION":
            c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
        else:
            continue
        c_idx[numero] = r
        sufijo = numero.split("-")[-1] if "-" in numero else numero
        try:
            n_idx[int(sufijo)] = r
        except (ValueError, TypeError):
            pass

    huerfanas = []
    for f in filas_pc:
        pc_kg = float(f.get("kg") or 0)
        if pc_kg > 0:
            c_idx, n_idx = idx_factura_completo, idx_factura_numf
        elif pc_kg < 0:
            c_idx, n_idx = idx_devolucion_completo, idx_devolucion_numf
        else:
            continue  # kg=0 ya filtrado en SQL

        r_ai = None
        numero = (f.get("numf_completo") or "").strip()
        if numero:
            r_ai = c_idx.get(numero)
        if r_ai is None and f.get("numf"):
            try:
                r_ai = n_idx.get(int(f["numf"]))
            except (ValueError, TypeError):
                pass

        if r_ai is None:
            f["_motivo"] = (
                "No existe en FACTURA" if pc_kg > 0
                else "No existe en DEVOLUCION"
            )
            huerfanas.append(f)

    return huerfanas


def main():
    print("Cargando facturas PC desde cutoff 2025-01-01...", file=sys.stderr)
    pc = cargar_facturas_pc()
    print(f"  {len(pc)} facturas PC vivas con kg!=0", file=sys.stderr)
    if not pc:
        print("Nada para auditar.", file=sys.stderr)
        return

    fechas = [f["fecha"] for f in pc if f.get("fecha")]
    desde = max(min(fechas), ASINFO_CUTOFF)
    hasta = max(fechas)
    print(f"Pidiendo Asinfo en rango {desde} → {hasta}...", file=sys.stderr)
    asinfo = cargar_asinfo_periodo(desde, hasta)
    print(f"  {len(asinfo)} documentos Asinfo en el rango", file=sys.stderr)

    huerfanas = detectar_huerfanas(pc, asinfo)
    print(f"\nHUÉRFANAS DETECTADAS: {len(huerfanas)}", file=sys.stderr)
    print("=" * 100, file=sys.stderr)

    # CSV a stdout
    w = csv.writer(sys.stdout)
    w.writerow(["fecha", "numf", "numf_completo", "codigo_cli", "cliente",
                "kg", "importe", "saldo", "stat", "motivo"])
    total_importe = 0.0
    for h in huerfanas:
        w.writerow([
            h["fecha"].isoformat() if h.get("fecha") else "",
            h.get("numf") or "",
            h.get("numf_completo") or "",
            h.get("codigo_cli") or "",
            (h.get("cliente") or "")[:40],
            f"{float(h.get('kg') or 0):.2f}",
            f"{float(h.get('importe') or 0):.2f}",
            f"{float(h.get('saldo') or 0):.2f}",
            h.get("stat") or "",
            h.get("_motivo") or "",
        ])
        total_importe += float(h.get("importe") or 0)

    print(f"\nTotal importe huérfano: $ {total_importe:,.2f}", file=sys.stderr)


if __name__ == "__main__":
    main()
