#!/usr/bin/env python3
"""Para las 18 huérfanas restantes (post fix card 199), buscar EN ASINFO directo
por cliente+fecha+total (importe PC) — sin asumir cliente_codigo exacto.

Estrategia:
  1) Sacar las 18 PC reales (igual que /facturas?solo_huerfanas=1)
  2) Para cada una, query Asinfo factura_cliente por:
     a) total exacto, fecha exacta -> ver qué cliente_codigo tiene Asinfo
     b) fecha exacta + cliente.codigo o cliente.nombre LIKE prefix PC
  3) Reportar coincidencias y diferencias"""
from __future__ import annotations
import os, sys
from collections import defaultdict
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
for _env in (".env.prod", ".env"):
    _p = os.path.join(_ROOT, _env)
    if os.path.isfile(_p):
        try:
            from dotenv import load_dotenv; load_dotenv(_p, override=False)
        except ImportError: pass
        break

import db; db.init_pool()
from modules.asinfo import service as asinfo_service
from modules._lib import metabase_client as mc


def _norm(s): return (s or "").strip().upper()


def matcher_view_que_queda():
    """Replica view + extrae las que quedan como huérfanas."""
    filas = db.fetch_all("""
        SELECT id_factura, numf, numf_completo, codigo_cli, fecha, kg, importe, saldo, stat
          FROM scintela.factura
         WHERE stat IN ('Z','A')
           AND saldo > 0
           AND fecha >= '2025-01-01'
           AND kg <> 0
         ORDER BY fecha DESC, id_factura DESC
    """)
    asinfo_rows = asinfo_service.facturas_periodo(date(2025,1,1), date(2026,5,28))
    _TIPOS_POS = ("FACTURA","NTEN","NC_FINANCIERA")
    _TIPOS_NEG = ("DEVOLUCION","NCNT")
    idx_completo = {}
    idx_sufijo = {}
    idx_compuesto = defaultdict(list)
    for r in asinfo_rows:
        tipo = r.get("tipo")
        numero = r.get("numero")
        if not numero: continue
        if tipo in _TIPOS_POS or tipo in _TIPOS_NEG:
            if numero not in idx_completo:
                idx_completo[numero] = r
            sfx = numero.split("-")[-1] if "-" in numero else numero
            try:
                sfx_int = int(sfx)
                if sfx_int not in idx_sufijo:
                    idx_sufijo[sfx_int] = r
            except: pass
            cli = _norm(r.get("cliente_codigo"))
            fai = str(r.get("fecha"))[:10] if r.get("fecha") else None
            try: kg_ai = float(r.get("kg") or 0)
            except: kg_ai = 0
            if cli and fai:
                idx_compuesto[(cli, fai, round(kg_ai, 2))].append(r)
    huerfanas = []
    for f in filas:
        try: pck = float(f.get("kg") or 0)
        except: pck = 0
        if pck == 0: continue
        if (f.get("numf_completo") or "").startswith("#"): continue
        nc = (f.get("numf_completo") or "").strip()
        r_ai = idx_completo.get(nc) if nc else None
        if r_ai is None and f.get("numf"):
            try: r_ai = idx_sufijo.get(int(f["numf"]))
            except: pass
        if r_ai is None:
            cli = _norm(f.get("codigo_cli"))
            fp = str(f.get("fecha"))[:10] if f.get("fecha") else None
            if cli and fp:
                cands = idx_compuesto.get((cli, fp, round(pck, 2)), [])
                pci = float(f.get("importe") or 0)
                ok = [c for c in cands if abs(float(c.get("usd") or 0) - pci) < 0.5]
                if len(ok) == 1: r_ai = ok[0]
        if r_ai is None: huerfanas.append(f)
    return huerfanas


def main():
    huerfanas = matcher_view_que_queda()
    print(f"Huerfanas reales (post fix): {len(huerfanas)}")
    for h in huerfanas[:20]:
        print(f"  id={h['id_factura']} numf={h.get('numf')} cli={_norm(h['codigo_cli'])} "
              f"fecha={str(h['fecha'])[:10]} kg={float(h['kg'])} imp={float(h['importe'] or 0)}")
    print()

    for h in huerfanas[:20]:
        pci = float(h["importe"] or 0)
        fecha_pc = str(h["fecha"])[:10]
        cli_pc = _norm(h["codigo_cli"])
        print("=" * 100)
        print(f"PC id={h['id_factura']} cli={cli_pc} fecha={fecha_pc} kg={float(h['kg'])} imp={pci}")

        # Buscar en Asinfo por fecha + total exacto (sin filtrar cliente)
        sql = f"""
            SELECT TOP 10
                   fc.id_factura_cliente,
                   fc.numero,
                   fc.fecha,
                   fc.estado,
                   fc.id_documento,
                   cl.codigo  AS cliente_codigo,
                   cl.nombre  AS cliente_nombre
              FROM dbo.factura_cliente fc
              JOIN dbo.cliente cl ON cl.id_cliente = fc.id_cliente
             WHERE fc.fecha = '{fecha_pc}'
               AND ABS(fc.total - {pci}) < 1
             ORDER BY fc.id_factura_cliente
        """
        rows = mc.fetch_dataset(2, sql)
        if not rows:
            # Probar con +/-1 dia
            sql_b = f"""
                SELECT TOP 10
                       fc.id_factura_cliente, fc.numero, fc.fecha, fc.estado,
                       cl.codigo AS cliente_codigo, cl.nombre AS cliente_nombre
                  FROM dbo.factura_cliente fc
                  JOIN dbo.cliente cl ON cl.id_cliente = fc.id_cliente
                 WHERE fc.fecha BETWEEN DATEADD(day,-2,'{fecha_pc}') AND DATEADD(day,2,'{fecha_pc}')
                   AND ABS(fc.total - {pci}) < 1
                """
            rows = mc.fetch_dataset(2, sql_b)
            if rows:
                print(f"  match con +/-2 dias:")
        if not rows:
            # Probar por cliente (codigo o nombre LIKE) + fecha sola
            sql_c = f"""
                SELECT TOP 10
                       fc.id_factura_cliente, fc.numero, fc.fecha, fc.estado, fc.total,
                       cl.codigo AS cliente_codigo, cl.nombre AS cliente_nombre
                  FROM dbo.factura_cliente fc
                  JOIN dbo.cliente cl ON cl.id_cliente = fc.id_cliente
                 WHERE fc.fecha = '{fecha_pc}'
                   AND (cl.codigo LIKE '{cli_pc}%' OR cl.nombre LIKE '{cli_pc}%')
                """
            rows = mc.fetch_dataset(2, sql_c)
            if rows:
                print(f"  match SOLO por cliente+fecha (total distinto):")
        if not rows:
            print(f"  >>> NADA encontrado en Asinfo <<<")
        else:
            for r in rows[:5]:
                print(f"    -> Asinfo num={r.get('numero')} fecha={str(r.get('fecha'))[:10]} "
                      f"estado={r.get('estado')} total={r.get('total','?')} "
                      f"cli={r.get('cliente_codigo')} nombre={r.get('cliente_nombre','')[:30]}")


if __name__ == "__main__":
    main()
