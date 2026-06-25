"""Queries de iniciales/metas mensuales (scintela.iniciales).

Schema:
    mesnum (1-12), mesnom ("Enero"..."Diciembre"), yy (año)
    kprog   - kg programados / meta de producción
    gprog   - gastos programados / meta de gastos
    pretot  - precio total objetivo
    hilado/tejido/terminado - stock objetivo en cada etapa
    pre, pretej, pretin, preadm - precios intermedios
    + auxiliares (vq, um/uk/uf/uq, numnot, dificil)

Comparativo real vs meta lee de scintela.historia:
    historia.kvent  - kg vendidos
    historia.uvent  - importe vendido (USD)
    historia.gasto  - gastos del mes
"""
from __future__ import annotations

from datetime import date

import db
from filters import today_ec

MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


def por_id(id_iniciales: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_iniciales, mesnum, mesnom, yy,
               hilado, tejido, terminado,
               kprog, gprog, pretot,
               pre, pretej, pretin, preadm,
               vq, um, uk, uf, uq,
               numnot, dificil,
               fecha_crea, fecha_modifica, usuario_crea, usuario_modifica
        FROM scintela.iniciales
        WHERE id_iniciales = %s
        """,
        (id_iniciales,),
    )


def por_mes(yy: int, mesnum: int) -> dict | None:
    # Tie-breaker `id_iniciales DESC`: el DBF tiene 4 (mes, yy) duplicados
    # históricos (Apr 2020, Oct 2021, Jul 2022, Apr 2025). Sin ORDER el
    # resultado no es determinista. La fila con id más alto = la última
    # insertada por TRUNCATE+INSERT = la versión más reciente del DBF.
    return db.fetch_one(
        """
        SELECT id_iniciales, mesnum, mesnom, yy, kprog, gprog, pretot
        FROM scintela.iniciales
        WHERE yy = %s AND mesnum = %s
        ORDER BY id_iniciales DESC
        LIMIT 1
        """,
        (yy, mesnum),
    )


def lista_anio(yy: int) -> list[dict]:
    """Trae los 12 meses del año (con campos NULL para los que no existan).

    Usa DISTINCT ON para colapsar duplicados (mes, yy) — el DBF tiene 4
    combos repetidos históricamente. Sin DISTINCT, el LEFT JOIN expande
    y la lista tiene >12 filas para esos años.
    """
    return db.fetch_all(
        """
        WITH meses(mesnum) AS (SELECT generate_series(1, 12)),
             iniciales_dedupe AS (
                 SELECT DISTINCT ON (mesnum)
                        mesnum, mesnom, id_iniciales,
                        kprog, gprog, pretot, hilado, tejido, terminado
                 FROM scintela.iniciales
                 WHERE yy = %s
                 ORDER BY mesnum, id_iniciales DESC
             )
        SELECT m.mesnum,
               COALESCE(i.mesnom, '')                     AS mesnom,
               i.id_iniciales,
               COALESCE(i.kprog, 0)                       AS kprog,
               COALESCE(i.gprog, 0)                       AS gprog,
               COALESCE(i.pretot, 0)                      AS pretot,
               COALESCE(i.hilado, 0)                      AS hilado,
               COALESCE(i.tejido, 0)                      AS tejido,
               COALESCE(i.terminado, 0)                   AS terminado
        FROM meses m
        LEFT JOIN iniciales_dedupe i
               ON i.mesnum = m.mesnum
        ORDER BY m.mesnum
        """,
        (yy,),
    ) or []


def comparativo_anio(yy: int) -> list[dict]:
    """Real vs Meta por mes para un año.

    Real: historia.kvent (kg), uvent (importe USD), gasto.
    Meta: iniciales.kprog, gprog, pretot.

    Cumplimiento: real/meta * 100 (cuando meta > 0).
    """
    # DISTINCT ON colapsa duplicados (mes, yy) en iniciales — ver lista_anio.
    return db.fetch_all(
        """
        WITH meses(mesnum) AS (SELECT generate_series(1, 12)),
             reales AS (
                 SELECT EXTRACT(MONTH FROM fecha)::int AS mesnum,
                        COALESCE(SUM(kvent), 0) AS kvent_real,
                        COALESCE(SUM(uvent), 0) AS uvent_real,
                        COALESCE(SUM(gasto), 0) AS gasto_real
                 FROM scintela.historia
                 WHERE EXTRACT(YEAR FROM fecha)::int = %(yy)s
                 GROUP BY 1
             ),
             iniciales_dedupe AS (
                 SELECT DISTINCT ON (mesnum)
                        mesnum, id_iniciales, kprog, gprog, pretot
                 FROM scintela.iniciales
                 WHERE yy = %(yy)s
                 ORDER BY mesnum, id_iniciales DESC
             )
        SELECT m.mesnum,
               i.id_iniciales,
               COALESCE(i.kprog, 0)        AS kg_meta,
               COALESCE(r.kvent_real, 0)   AS kg_real,
               COALESCE(i.pretot, 0)       AS importe_meta,
               COALESCE(r.uvent_real, 0)   AS importe_real,
               COALESCE(i.gprog, 0)        AS gasto_meta,
               COALESCE(r.gasto_real, 0)   AS gasto_real
        FROM meses m
        LEFT JOIN iniciales_dedupe i
               ON i.mesnum = m.mesnum
        LEFT JOIN reales r
               ON r.mesnum = m.mesnum
        ORDER BY m.mesnum
        """,
        {"yy": yy},
    ) or []


def anios_disponibles() -> list[int]:
    """Lista de años con metas cargadas — para el selector."""
    rows = db.fetch_all(
        """
        SELECT DISTINCT yy
        FROM scintela.iniciales
        WHERE yy IS NOT NULL
        ORDER BY yy DESC
        """
    ) or []
    return [int(r["yy"]) for r in rows]


def crear(*, mesnum: int, yy: int, kprog=None, gprog=None, pretot=None,
          pretej=None, pretin=None, preadm=None,
          hilado=None, tejido=None, terminado=None,
          usuario: str = "web") -> dict:
    """Crear meta de un mes. mesnom se setea desde MESES_ES por consistencia.

    TMT 2026-06-23 (dueña, "PC es el futuro"): el presupuesto se carga por área
    (pretej/pretin/preadm) y pretot = su suma (igual que el dBase, INFORMES.PRG
    L12: PRETOT = PRETEJ+PRETIN+PREADM)."""
    if not (1 <= int(mesnum) <= 12):
        raise ValueError("mesnum debe estar entre 1 y 12")
    if int(yy) < 2000 or int(yy) > 2100:
        raise ValueError("Año fuera de rango razonable")
    # Idempotencia: si ya existe la fila para (yy, mesnum), no re-insertar.
    existing = por_mes(yy, mesnum)
    if existing:
        raise ValueError(f"Ya existe meta para {MESES_ES[mesnum-1]} {yy} (id #{existing['id_iniciales']})")
    mesnom = MESES_ES[mesnum - 1]
    return db.execute_returning(
        """
        INSERT INTO scintela.iniciales
            (mesnum, mesnom, yy, kprog, gprog, pretot,
             pretej, pretin, preadm,
             hilado, tejido, terminado, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id_iniciales, mesnum, yy
        """,
        (mesnum, mesnom, yy, kprog, gprog, pretot,
         pretej, pretin, preadm,
         hilado, tejido, terminado, usuario),
    ) or {}


def editar(id_iniciales: int, *, kprog=None, gprog=None, pretot=None,
           pretej=None, pretin=None, preadm=None,
           hilado=None, tejido=None, terminado=None,
           usuario: str = "web") -> int:
    campos = []
    params: list = []
    for col, val in [
        ("kprog", kprog), ("gprog", gprog), ("pretot", pretot),
        ("pretej", pretej), ("pretin", pretin), ("preadm", preadm),
        ("hilado", hilado), ("tejido", tejido), ("terminado", terminado),
    ]:
        if val is not None:
            campos.append(f"{col} = %s")
            params.append(val)
    if not campos:
        return 0
    campos.append("fecha_modifica = CURRENT_TIMESTAMP")
    campos.append("usuario_modifica = %s")
    params.append(usuario[:50])
    params.append(id_iniciales)
    return db.execute(
        f"UPDATE scintela.iniciales SET {', '.join(campos)} WHERE id_iniciales = %s",
        tuple(params),
    )


def anio_actual() -> int:
    """Año en curso, helper para el default del selector."""
    return today_ec().year


# ---------------------------------------------------------------------------
# ITEM #5 — Auto-cierre de stock mensual (MENU.PRG L246-263)
# ---------------------------------------------------------------------------

def cerrar_mes_auto(fecha_cierre: date | None = None,
                    usuario: str = "auto") -> dict:
    """ITEM #5 — Copia el stock + precios del mes actual al mes siguiente.

    Mirror exacto de MENU.PRG L246-263:

        USE INICIALES
        GO BOTT
        MPN=HILADO; KPN=TEJIDO; PFN=TERMINADO; VQN=VQ
        UMN=UM; UKN=UK; UQN=UQ; UFN=UF
        KK=KPROG; GG=GPROG; ULTNOT=NUMNOT
        &AB                             && APPEND BLANK
        REPLA MES WITH CMONTH(DATE()), YY WITH YEAR(DATE())
        REPLA HILADO WITH MPN, TEJIDO WITH KPN, TERMINADO WITH PFN, VQ WITH VQN
        REPLA UM WITH UMN, UK WITH UKN, UQ WITH UQN, UF WITH UFN
        REPLA KPROG WITH KK, GPROG WITH GG, NUMNOT WITH ULTNOT

    Es decir: lee la ÚLTIMA fila de scintela.iniciales (mes en curso) y
    crea una nueva con esos mismos valores pero con (mesnum, yy) del mes
    siguiente. Idempotente: si ya hay fila para el mes siguiente → no
    hace nada.

    Lock pessimista sobre `scintela.sistema_meta.clave='cierre_mes_ult_fecha'`
    para evitar dos calls concurrentes que dupliquen la fila.

    `fecha_cierre`: fecha de referencia (default=HOY). El mes objetivo
    será MES+1 de esa fecha.

    Devuelve `{aplicado: bool, mes_origen: 'YYYY-MM', mes_destino: 'YYYY-MM',
    id_iniciales_nuevo: int | None, razon: str}`.
    """
    fecha_cierre = fecha_cierre or today_ec()
    mes_origen_num = fecha_cierre.month
    anio_origen = fecha_cierre.year

    # Mes destino = mes siguiente (rollover de diciembre→enero del año siguiente)
    if mes_origen_num == 12:
        mes_dest_num = 1
        anio_dest = anio_origen + 1
    else:
        mes_dest_num = mes_origen_num + 1
        anio_dest = anio_origen

    mes_origen_clave = f"{anio_origen:04d}-{mes_origen_num:02d}"
    mes_dest_clave = f"{anio_dest:04d}-{mes_dest_num:02d}"

    with db.tx() as conn:
        # TMT 2026-05-15 (re-audit H2): advisory lock obligatorio. El
        # `SELECT ... FOR UPDATE` original NO bloquea si la fila no existe
        # (primer-ever-run); dos workers podían leer NULL y ambos pasar
        # el gate idempotente, duplicando filas en `iniciales`. El advisory
        # lock se mantiene incluso cuando la fila aún no existe.
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext('cerrar_mes_auto'))",
            conn=conn,
        )
        lock = db.fetch_one(
            "SELECT valor FROM scintela.sistema_meta "
            " WHERE clave = %s FOR UPDATE",
            ("cierre_mes_ult_fecha",),
            conn=conn,
        )
        ult_clave = (lock or {}).get("valor")

        # Inicializar si nunca corrió.
        if not ult_clave:
            db.execute(
                """INSERT INTO scintela.sistema_meta (clave, valor)
                   VALUES (%s, %s)
                   ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor""",
                ("cierre_mes_ult_fecha", "1900-01"),
                conn=conn,
            )
            ult_clave = "1900-01"

        # Si ya cerramos hasta el mes destino, no hacer nada.
        if ult_clave >= mes_dest_clave:
            return {
                "aplicado": False,
                "mes_origen": mes_origen_clave,
                "mes_destino": mes_dest_clave,
                "id_iniciales_nuevo": None,
                "razon": (f"Ya se cerró hasta {ult_clave} ≥ destino "
                          f"{mes_dest_clave}. Nada que hacer."),
            }

        # Idempotencia adicional: si ya hay fila para (yy_dest, mes_dest_num)
        # no insertar. Sólo avanzamos el marker.
        ya_existe = db.fetch_one(
            """
            SELECT id_iniciales
              FROM scintela.iniciales
             WHERE yy = %s AND mesnum = %s
             ORDER BY id_iniciales DESC
             LIMIT 1
            """,
            (anio_dest, mes_dest_num),
            conn=conn,
        )
        if ya_existe:
            db.execute(
                "UPDATE scintela.sistema_meta SET valor = %s, "
                "actualizado = CURRENT_TIMESTAMP WHERE clave = %s",
                (mes_dest_clave, "cierre_mes_ult_fecha"),
                conn=conn,
            )
            return {
                "aplicado": False,
                "mes_origen": mes_origen_clave,
                "mes_destino": mes_dest_clave,
                "id_iniciales_nuevo": ya_existe.get("id_iniciales"),
                "razon": (f"Ya existe fila iniciales para {mes_dest_clave} "
                          f"(id={ya_existe.get('id_iniciales')}). Marker avanzado."),
            }

        # Leer la ÚLTIMA fila de iniciales — refleja el "GO BOTT" del PRG.
        # Tomamos por id_iniciales DESC porque el DBF tiene (mes, yy)
        # duplicados históricos y queremos la más reciente (ver por_mes()).
        ult = db.fetch_one(
            """
            SELECT hilado, tejido, terminado, vq,
                   um, uk, uq, uf,
                   kprog, gprog, numnot, pretot
              FROM scintela.iniciales
             ORDER BY id_iniciales DESC
             LIMIT 1
            """,
            conn=conn,
        )
        if not ult:
            return {
                "aplicado": False,
                "mes_origen": mes_origen_clave,
                "mes_destino": mes_dest_clave,
                "id_iniciales_nuevo": None,
                "razon": "scintela.iniciales está vacía — no hay nada que copiar.",
            }

        mesnom = MESES_ES[mes_dest_num - 1]

        res = db.execute_returning(
            """
            INSERT INTO scintela.iniciales
                (mesnum, mesnom, yy,
                 hilado, tejido, terminado, vq,
                 um, uk, uq, uf,
                 kprog, gprog, numnot, pretot,
                 usuario_crea)
            VALUES (%s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s)
            RETURNING id_iniciales
            """,
            (
                mes_dest_num, mesnom, anio_dest,
                ult.get("hilado"), ult.get("tejido"), ult.get("terminado"),
                ult.get("vq"),
                ult.get("um"), ult.get("uk"), ult.get("uq"), ult.get("uf"),
                ult.get("kprog"), ult.get("gprog"), ult.get("numnot"),
                ult.get("pretot"),
                (usuario or "auto")[:50],
            ),
            conn=conn,
        ) or {}

        # Avanzar marker.
        db.execute(
            "UPDATE scintela.sistema_meta SET valor = %s, "
            "actualizado = CURRENT_TIMESTAMP WHERE clave = %s",
            (mes_dest_clave, "cierre_mes_ult_fecha"),
            conn=conn,
        )

        return {
            "aplicado": True,
            "mes_origen": mes_origen_clave,
            "mes_destino": mes_dest_clave,
            "id_iniciales_nuevo": res.get("id_iniciales"),
            "razon": (f"Cierre {mes_origen_clave}→{mes_dest_clave} aplicado: "
                      f"HI={ult.get('hilado')} TJ={ult.get('tejido')} "
                      f"PF={ult.get('terminado')}"),
        }
