"""Sucursales por dirección de Asinfo + recode de códigos duplicados.

TMT 2026-07-26 (dueña: "sucursal se transforma en cliente, no me pongas
sucursal, pero esa es la manera de llevar"; "hace vos el cambio").

CONTEXTO
--------
Asinfo trae TODO el volumen de una empresa bajo UN `nombre_comercial`
(= código de cliente). El dBase, en cambio, parte algunas empresas en
códigos distintos por DIRECCIÓN de entrega (sucursal). Caso vivo:
ALMACENES JOSE PUEBLA (empresa 22656) -> dirección 22656 = AJO,
dirección 22657 = AJO/AJ2. Como Asinfo manda todo como "AJO", PC cargaba
las de la sucursal bajo AJO y quedaban mal clasificadas.

Con la card 199 de Metabase ahora exponiendo `id_direccion_empresa`
(fix 2026-07-26), PC puede clasificar la sucursal SOLO por la dirección,
sin depender del dBase ni de un humano.

QUÉ HACE (idempotente)
----------------------
1. Crea `scintela.sucursal_direccion` (id_direccion -> codigo_cli) y la
   siembra con 22657->AJ2. La carga consulta esta tabla para clasificar.
2. Recode de lo EXISTENTE: las facturas hoy bajo AJO cuya dirección de
   Asinfo es 22657 pasan a AJ2 (factura + retención). Se autodetermina
   consultando Asinfo — no hay lista hardcodeada.
3. Typos confirmados (misma empresa, código mal escrito): fusiona al
   canónico — RRE->RRV, JUU->JUT, MR->MRD, KM->KAM (factura + retención).
4. Registra esos typos como alias Asinfo<->PC (para que la carga futura
   los codee bien sola).

REVERSIBLE: el detalle de qué se movió queda en usuario_modifica='mig-0132'.

GUARD to_regclass OBLIGATORIO: no-op en CI/test donde scintela.* no existe.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# (codigo_typo, codigo_canonico) — misma empresa, código mal escrito.
_TYPOS = [
    ("RRE", "RRV"),
    ("JUU", "JUT"),
    ("MR", "MRD"),
    ("KM", "KAM"),
]

# id_direccion de Asinfo -> código de sucursal en PC.
_SUCURSALES = [
    (22657, "AJ2", "ALMACENES JOSE PUEBLA — sucursal dir 22657 (empresa 22656=AJO)"),
]


def run(conn) -> None:
    cur = conn.cursor()

    # No-op en CI/test (scintela.* ausente).
    cur.execute("SELECT to_regclass('scintela.factura')")
    if cur.fetchone()[0] is None:
        print("0132: scintela.factura ausente (CI/test) — no-op")
        return

    # 1) Tabla sucursal_direccion + seed.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scintela.sucursal_direccion (
            id_direccion INTEGER PRIMARY KEY,
            codigo_cli   TEXT NOT NULL,
            nota         TEXT,
            fecha_alta   DATE NOT NULL DEFAULT CURRENT_DATE,
            usuario_crea TEXT
        )
        """
    )
    for id_dir, cod, nota in _SUCURSALES:
        cur.execute(
            """
            INSERT INTO scintela.sucursal_direccion (id_direccion, codigo_cli, nota, usuario_crea)
            VALUES (%s, %s, %s, 'mig-0132')
            ON CONFLICT (id_direccion) DO NOTHING
            """,
            (id_dir, cod, nota),
        )

    # AJ2 ahora es sucursal SEPARADA (clasificada por dirección 22657). El alias
    # viejo AJ2<->AJO la re-fusionaba en AJO — hay que sacarlo. Fail-soft si la
    # tabla de alias no existe todavía.
    try:
        cur.execute(
            "DELETE FROM scintela.cliente_alias "
            "WHERE UPPER(TRIM(codigo_asinfo))='AJ2' AND UPPER(TRIM(codigo_pc))='AJO'"
        )
        if cur.rowcount:
            print("0132: alias AJ2<->AJO eliminado (AJ2 ahora es sucursal separada)")
    except Exception as e:  # noqa: BLE001
        print(f"0132: no pude borrar alias AJ2<->AJO ({str(e)[:80]})")

    # 2) Recode AJO->AJ2 de lo existente, autodeterminado de Asinfo por dirección.
    #    Fail-soft: si Asinfo no responde, se saltea (la tabla + la carga futura
    #    igual clasifican bien de acá en adelante).
    try:
        from datetime import date

        from modules.asinfo import service as _asinfo
        filas = _asinfo.facturas_periodo(date(2026, 1, 1), date.today()) or []
        numeros_aj2 = set()
        for r in filas:
            try:
                if int(r.get("id_direccion_empresa") or 0) == 22657:
                    num = str(r.get("numero") or "").strip()
                    if num:
                        numeros_aj2.add(num)
            except (TypeError, ValueError):
                continue
        if numeros_aj2:
            nums = list(numeros_aj2)
            # match por numf_completo (N° SRI exacto). Solo las que hoy son AJO.
            cur.execute(
                """
                UPDATE scintela.factura
                   SET codigo_cli = 'AJ2', usuario_modifica = 'mig-0132'
                 WHERE numf_completo = ANY(%s)
                   AND UPPER(TRIM(codigo_cli)) = 'AJO'
                """,
                (nums,),
            )
            n_aj2 = cur.rowcount
            # retención de esas facturas también pasa a AJ2 (liga por codigo_cli+numf).
            cur.execute(
                """
                UPDATE scintela.retencion r
                   SET codigo_cli = 'AJ2'
                  FROM scintela.factura f
                 WHERE f.numf = r.numf
                   AND f.codigo_cli = 'AJ2'
                   AND UPPER(TRIM(r.codigo_cli)) = 'AJO'
                   AND f.numf_completo = ANY(%s)
                """,
                (nums,),
            )
            print(f"0132: AJO->AJ2 recodeadas {n_aj2} facturas (dir 22657, {len(nums)} N° de Asinfo)")
        else:
            print("0132: Asinfo no devolvió facturas dir 22657 — recode AJO->AJ2 salteado")
    except Exception as e:  # noqa: BLE001 — fail-soft, no rompe la migración
        print(f"0132: Asinfo no disponible ({str(e)[:120]}) — recode AJO->AJ2 salteado")

    # 3) Typos: fusionar al canónico (factura + retención).
    for typo, canon in _TYPOS:
        cur.execute(
            "UPDATE scintela.factura SET codigo_cli = %s, usuario_modifica = 'mig-0132' "
            "WHERE UPPER(TRIM(codigo_cli)) = %s",
            (canon, typo),
        )
        n = cur.rowcount
        cur.execute(
            "UPDATE scintela.retencion SET codigo_cli = %s "
            "WHERE UPPER(TRIM(codigo_cli)) = %s",
            (canon, typo),
        )
        if n:
            print(f"0132: typo {typo}->{canon}: {n} facturas recodeadas")

    # 4) Registrar los typos como alias Asinfo<->PC (carga futura). La tabla la
    #    crea aliases._bootstrap; si no existe todavía, la creamos igual.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scintela.cliente_alias (
            codigo_asinfo TEXT NOT NULL,
            codigo_pc     TEXT NOT NULL,
            nota          TEXT,
            fecha_alta    DATE NOT NULL DEFAULT CURRENT_DATE,
            usuario_crea  TEXT,
            PRIMARY KEY (codigo_asinfo, codigo_pc)
        )
        """
    )
    for typo, canon in _TYPOS:
        cur.execute(
            """
            INSERT INTO scintela.cliente_alias (codigo_asinfo, codigo_pc, nota, usuario_crea)
            VALUES (%s, %s, %s, 'mig-0132')
            ON CONFLICT (codigo_asinfo, codigo_pc) DO NOTHING
            """,
            (typo, canon, f"{typo} = mismo cliente que {canon} (typo) — barrido 2026-07-26"),
        )
