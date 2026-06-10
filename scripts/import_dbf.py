"""Importa los DBF legacy del dBase a Postgres scintela.* — proceso de sync.

Workflow oficial mientras corren dBase y Programa Core en paralelo:

    1. TMT copia los .DBF frescos del dBase a:
         /Users/tamaraeliscovich/Documents/INTELA copy/Files/

    2. Corre:
         python scripts/import_dbf.py
       (o `make sync-dbf` si está el target)

    3. Postgres queda al día con los números del dBase. La página
       /informes/balance refleja los nuevos totales sin más cambios.

Política:
  - Cada DBF presente reemplaza la tabla Postgres correspondiente
    (TRUNCATE + INSERT, transacción por tabla).
  - Cada DBF AUSENTE deja la tabla Postgres tal cual ("si no te lo
    pasé, usá los viejos" — TMT 2026-04-30).
  - Cada tabla en su propia transacción: si una falla, las otras siguen.
  - Idempotente: corrél dos veces, mismo resultado.
  - Salida en español, con conteos por tabla.

Para iterar sin riesgo:
    python scripts/import_dbf.py --dry-run
    python scripts/import_dbf.py --only=FACTURAS.DBF,CHEQUES.DBF
    python scripts/import_dbf.py --source-dir=/path/alternativo

Esta herramienta es de TRANSICIÓN. Cuando se retire el dBase, archivar
en scripts/_archive/.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Permite importar `db` desde la raíz del proyecto.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Cargar .env antes de importar db (que usa os.environ['DB_HOST'] al pool).
# Si python-dotenv no está, se asume que las variables ya están exportadas.
try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# Default: workspace folder donde TMT pega los DBFs.
DEFAULT_DBF_DIR = Path("/Users/tamaraeliscovich/Documents/INTELA copy/Files")

try:
    import dbfread
except ImportError:
    print(
        "ERROR: 'dbfread' no está instalado.\nCorrer: pip install dbfread",
        file=sys.stderr,
    )
    sys.exit(2)

import db  # noqa: E402

# ============================================================================
# Helpers de coerción — los DBFs traen tipos heterogéneos.
# ============================================================================


def _str(v, max_len=None):
    """Trim, devolver None si vacío. Trunca a max_len si se pasa."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if max_len:
        s = s[:max_len]
    return s


def _date(v):
    """dbfread devuelve datetime.date para campos D. Pass-through con guard."""
    if isinstance(v, date | datetime):
        return v if not isinstance(v, datetime) else v.date()
    return None


def _num(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=None):
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


# TMT 2026-05-20 v8 — coerciones extendidas para sync_dbase_actual.
# Reemplazan a las versiones simples cuando el DBF trae el campo como
# TEXT en vez de su tipo nativo (D / N). Pre-mortem 2c.
def _date_robusto(v):
    """Igual que _date() pero acepta también strings DD/MM/YYYY, YYYY-MM-DD,
    DD-MM-YYYY. Para DBFs en los que la columna FECHA está declarada C en
    vez de D (caso histórico)."""
    if v is None or v == "":
        return None
    d = _date(v)
    if d is not None:
        return d
    s = str(v).strip()
    if not s or s.startswith("#"):
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s[:19] if len(s) > 10 else s, fmt).date()
        except ValueError:
            continue
    return None


def _num_robusto(v, default=None):
    """Igual que _num() pero acepta '1.234,56' (es-EC) y '(123.45)' (paréntesis
    = negativo). Para campos numéricos venidos como TEXT del DBF."""
    if v is None or v == "":
        return default
    if isinstance(v, int | float):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if not s:
        return default
    # Paréntesis = negativo (contabilidad)
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    # 1.234,56 (es-EC)  vs  1234.56 (ISO)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


# ─── Stat legacy → moderno ────────────────────────────────────────────────
# Pre-mortem 2a. Mapea valores legacy del dBase a stats que la app entiende.
# `None` significa "skipear esa fila" (no importar).
_STAT_LEGACY_MAP_CHEQUE = {
    "V": "B",  # Cheque depositado en Internacional → "B" (banco moderno)
    # TMT 2026-05-21 confirmación dueña: 'W' en CHEQUES.DBF eran cheques
    # legacy ya depositados en banco (MODIFICA.PRG L800 los pintaba R/W
    # warning). Tamara confirmó "si ya están en el banco, entonces sí" →
    # mismo destino que V: stat='B'.
    "W": "B",
    "Y": None,  # Físicamente borrado en dBase
    "*": None,  # Sentinel reemplazo
}
_STAT_LEGACY_MAP_FACTURA = {
    "V": "A",  # Factura "vencida" legacy → "A" anulada parcial
    "Y": None,
    "*": None,
}
_STAT_LEGACY_MAP_GENERIC = {
    "Y": None,
    "*": None,
}


def _remap_stat(stat: str | None, mapping: dict) -> str | None:
    """Aplica el mapeo legacy si stat está en él, sino lo deja pasar.

    Retorna None si la fila debe ser skipeada (stat='Y' físicamente borrada).
    """
    if stat is None:
        return stat
    s = str(stat).strip().upper()
    if s in mapping:
        return mapping[s]
    return stat


# Mapeo nombre-de-mes (DBF) → número (Postgres requiere mesnum int).
# El DBF tiene los meses en inglés ('Apr', 'Aug', 'Dec'…) pero algunos archivos
# históricos quedaron en español. Aceptamos las dos formas — sino, los meses
# que NO coinciden en ambos idiomas (Apr/Abr, Aug/Ago, Dec/Dic, Jan/Ene)
# quedan con mesnum NULL y rompen las queries que filtran por mesnum.
_MES_NUM = {
    # Español
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "SET": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
    # Inglés (los DBFs actuales vienen así)
    "JAN": 1,
    "APR": 4,
    "AUG": 8,
    "DEC": 12,
    # (FEB, MAR, MAY, JUN, JUL, SEP, OCT, NOV ya coinciden con el español)
    # Correcciones de typos históricos en INICIALE.DBF:
    "JÔL": 7,  # 'Jôl' yy=2003 (en CP850), entre Jun y Aug → Jul. Encoding glitch.
    "JÂL": 7,  # Mismo row leído con CP1252 — por si el encoding cambia.
    "EDT": 3,  # 'edt' yy=2019, entre Feb y Apr → Mar (typo)
}


def _mes_a_num(mes_str):
    if not mes_str:
        return None
    return _MES_NUM.get(mes_str.strip().upper()[:3])


# ============================================================================
# Mappers: 1 función por tabla, devuelve dict con keys = columnas Postgres.
# ============================================================================


def _map_factura(r):
    stat_raw = _str(r.get("STAT"), 2)
    stat = _remap_stat(stat_raw, _STAT_LEGACY_MAP_FACTURA)
    if stat_raw and stat is None and stat_raw.upper() in _STAT_LEGACY_MAP_FACTURA:
        return None
    return {
        "numf": _int(r.get("NUMF"), 0) or 0,
        "fecha": _date_robusto(r.get("FECHA")) or date.today(),
        "codigo_cli": _str(r.get("CLIENTE"), 5) or "",
        "kg": _num_robusto(r.get("KG"), 0) or 0,
        "importe": _num_robusto(r.get("IMPORTE"), 0) or 0,
        "abono": _num_robusto(r.get("ABONO"), 0) or 0,
        "saldo": _num_robusto(r.get("SALDO"), 0) or 0,
        "stat": stat,
        "condic": _str(r.get("CONDIC"), 2),
        "vencimiento": _date_robusto(r.get("VENCIM")),
        "tipo": _str(r.get("TIPO"), 2),
        "clave": _str(r.get("CLAVE"), 2),
        "pase": _str(r.get("PASE"), 5),
        "usuario_crea": "dbf-import",
    }


def _map_cheque(r):
    stat_raw = _str(r.get("STAT"), 5)
    stat = _remap_stat(stat_raw, _STAT_LEGACY_MAP_CHEQUE)
    if stat_raw and stat is None and stat_raw.upper() in _STAT_LEGACY_MAP_CHEQUE:
        # Devuelve None → import_one filtrará esta fila.
        return None
    return {
        "fecha": _date_robusto(r.get("FECHA")) or date.today(),
        "fechad": _date_robusto(r.get("FECHAD")) or _date_robusto(r.get("FECHA")) or date.today(),
        "codigo_cli": _str(r.get("CLIENTE"), 5),
        "importe": _num_robusto(r.get("IMPORTE")),
        "no_banco": _int(r.get("NB")),
        "banco": _str(r.get("BANCO"), 30),
        "stat": stat,
        "fechaing": _date_robusto(r.get("FECHING")),
        "fechaout": _date_robusto(r.get("FECHOUT")),
        "prov": _str(r.get("PROV"), 5),
        "clave": _str(r.get("CLAVE"), 5),
        "usuario_crea": "dbf-import",
    }


def _map_posdat(r):
    return {
        "fecha": _date(r.get("FECHA")),
        "fechad": _date(r.get("FECHAD")),
        "prov": _str(r.get("PROV"), 3),
        "num": _int(r.get("NUM")),
        "importe": _num(r.get("IMPORTE")),
        "concepto": _str(r.get("CONCEPTO"), 100),
        "banc": _int(r.get("BANC")),
        "clave": _str(r.get("CLAVE"), 3),
        "usuario_crea": "dbf-import",
    }


def _map_caja(r):
    return {
        "fecha": _date(r.get("FECHA")),
        "tipo": _str(r.get("TIPO"), 3),
        "importe": _num(r.get("IMPORTE")),
        "concepto": _str(r.get("CONCEPTO"), 100),
        "saldo": _num(r.get("SALDO")),
        "clave": _str(r.get("CLAVE"), 3),
        "usuario_crea": "dbf-import",
    }


def _map_dolares(r):
    # En el DBF la columna de estado se llama 'ST T' (con espacio raro). dbfread
    # la expone como 'ST_T' o 'ST'. Probamos ambas.
    st = r.get("ST") or r.get("ST_T") or r.get("ST T")
    return {
        "fecha": _date(r.get("FECHA")),
        "cta": _str(r.get("CTA"), 3),
        "concepto": _str(r.get("CONCEPTO"), 100),
        "importe": _num(r.get("IMPORTE")),
        "st": _str(st, 3),
        "clave": _str(r.get("CLAVE"), 3),
        "usuario_crea": "dbf-import",
    }


def _map_retiros(r):
    """RETIROS.DBF — retiros del dueño / dividendos.

    Campos DBF: FECHA(D), NB(N4), RET(N10), DE(C2), CONCEPTO(C15), CLAVE(C1).
    El campo `de` es la sigla del socio/destino del retiro (MA/VI/PI/TA/AM/OP/FE...).
    `nb` puede ser None, 0, o el no_banco — lo dejamos pasar tal cual.
    """
    return {
        "fecha": _date(r.get("FECHA")),
        "nb": _int(r.get("NB")),
        "ret": _num(r.get("RET")),
        "de": _str(r.get("DE"), 5),
        "concepto": _str(r.get("CONCEPTO"), 100),
        "clave": _str(r.get("CLAVE"), 5),
        "usuario_crea": "dbf-import",
    }


def _map_tinto(r):
    """TINTO.DBF — fórmulas / batches de tintura del mes.

    Es el insumo de COL.QUI. y KR en el panel Resultados (vía
    `informes.queries.tinto_mes_corriente_resultado`). Sin sync, COL.QUI.
    queda en 0 aunque haya datos frescos en el DBF (bug TMT 2026-05-06).

    Campos DBF: FECHA(D), COD(C3), COLOR(C10), FRANELA…KIANA (N2 c/u, kg
    por tipo de fabric en el batch), TIPO(C1), KG(N7), KGN(N7),
    IMPORTE(N8), STAT(C1), CLAVE(C1).

    Postgres `scintela.tinto.tipo` es integer pero el DBF lo declara como
    char(1) y siempre lo deja vacío en la data observada. _int() lo
    coerciona a None cuando viene '' o no parseable, así no falla la
    inserción cuando el día de mañana metan un valor.
    """
    return {
        "fecha": _date(r.get("FECHA")),
        "cod": _str(r.get("COD"), 5),
        "color": _str(r.get("COLOR"), 30),
        "franela": _int(r.get("FRANELA")),
        "messi": _int(r.get("MESSI")),
        "james": _int(r.get("JAMES")),
        "jersey": _int(r.get("JERSEY")),
        "j3": _int(r.get("J3")),
        "toper": _int(r.get("TOPER")),
        "jlyc": _int(r.get("JLYC")),
        "pique": _int(r.get("PIQUE")),
        "flyc": _int(r.get("FLYC")),
        "falso": _int(r.get("FALSO")),
        "otros": _int(r.get("OTROS")),
        "kiana": _int(r.get("KIANA")),
        "tipo": _int(r.get("TIPO")),
        "kg": _num(r.get("KG")),
        "kgn": _num(r.get("KGN")),
        "importe": _num(r.get("IMPORTE")),
        "stat": _str(r.get("STAT"), 3),
        "clave": _str(r.get("CLAVE"), 3),
        "usuario_crea": "dbf-import",
    }


def _map_activos(r):
    valor = r.get("VALOR") or r.get("VALOR_R") or r.get("VALOR R")
    return {
        "fecha": _date(r.get("FECHA")),
        "concepto": _str(r.get("CONCEPTO"), 100),
        "tipo": _str(r.get("TIPO"), 3),
        "inicial": _num(r.get("INICIAL")),
        "amortizac": _num(r.get("AMORTIZAC")),
        "amortimes": _num(r.get("AMORTIMES")),
        "valor": _num(valor),
        "cuota": _num(r.get("CUOTA")),
        "usuario_crea": "dbf-import",
    }


def _map_historia(r):
    return {
        "fecha": _date(r.get("FECHA")),
        "stock": _num(r.get("STOCK")),
        "kcom": _num(r.get("KCOM")),
        "ktej": _num(r.get("KTEJ")),
        "ktin": _num(r.get("KTIN")),
        "ustock": _num(r.get("USTOCK")),
        "uqui": _num(r.get("UQUI")),
        "kvent": _num(r.get("KVENT")),
        "uvent": _num(r.get("UVENT")),
        "costo": _num(r.get("COSTO")),
        "ucom": _num(r.get("UCOM")),
        "utej": _num(r.get("UTEJ")),
        "utin": _num(r.get("UTIN")),
        "gasto": _num(r.get("GASTO")),
        "gstotal": _num(r.get("GSTOTAL")),
        "banco": _num(r.get("BANCO")),
        "cart": _num(r.get("CART")),
        "deuda": _num(r.get("DEUDA")),
        "retiro": _num(r.get("RETIRO")),
        "patrimonio": _num(r.get("PATRIMONIO")),
        "anticipos": _num(r.get("ANTICIPOS")),
        "dolar": _num(r.get("DOLAR")),
        "maquinaria": _num(r.get("MAQUINARIA")),
        "realty": _num(r.get("REALTY")),
        "usret": _num(r.get("USRET")),
        "usuti": _num(r.get("USUTI")),
        "usuario_crea": "dbf-import",
    }


def _map_iniciales(r):
    yy = r.get("YY") or r.get("YY_R") or r.get("YY R")
    return {
        "mesnum": _mes_a_num(_str(r.get("MES"))),
        "mesnom": _str(r.get("MES"), 10),
        "yy": _int(yy),
        "hilado": _num(r.get("HILADO")),
        "tejido": _num(r.get("TEJIDO")),
        "terminado": _num(r.get("TERMINADO")),
        "vq": _num(r.get("VQ")),
        "um": _num(r.get("UM")),
        "uk": _num(r.get("UK")),
        "uf": _num(r.get("UF")),
        "uq": _num(r.get("UQ") or r.get("UQ_EB") or r.get("UQ EB")),
        "pre": _num(r.get("PRE")),
        "kprog": _num(r.get("KPROG")),
        "gprog": _num(r.get("GPROG")),
        "numnot": _num(r.get("NUMNOT")),
        "dificil": _num(r.get("DIFICIL")),
        "pretej": _num(r.get("PRETEJ")),
        "pretin": _num(r.get("PRETIN")),
        "preadm": _num(r.get("PREADM")),
        "pretot": _num(r.get("PRETOT")),
        "usuario_crea": "dbf-import",
    }


def _map_compra(r):
    return {
        "fecha": _date(r.get("FECHA")),
        "codigo_prov": _str(r.get("PROV"), 3),
        "tipo": _str(r.get("TIPO"), 3),
        "comprobante": _str(r.get("COMPROBANT"), 100),
        "kg": _num(r.get("KG")),
        "importe": _num(r.get("IMPORTE")),
        "no_banco": _int(r.get("BANC")),
        "numero": _int(r.get("NUM")),
        "fecha_ing": _date(r.get("FECHING")),
        "fechad": _date(r.get("FECHAD")),
        "concepto": _str(r.get("CONCEPTO"), 200),
        "clave": _str(r.get("CLAVE"), 3),
        "usuario_crea": "dbf-import",
    }


def _map_flujo(r):
    inter = r.get("INTER") or r.get("INTER_R") or r.get("INTER R")
    return {
        "fecha": _date(r.get("FECHA")),
        "cheques": _num(r.get("CHEQUES")),
        "facturas": _num(r.get("FACTURAS")),
        "posdat1": _num(r.get("POSDAT1")),
        "posdat2": _num(r.get("POSDAT2")),
        "pichincha": _num(r.get("PICHINCHA")),
        "inter": _num(inter),
        "mprima": _num(r.get("MPRIMA")),
        "gastos": _num(r.get("GASTOS")),
        "saldo": _num(r.get("SALDO")),
        "pagos": _num(r.get("PAGOS")),
        "dolares": _num(r.get("DOLARES")),
        "usaldo": _num(r.get("USALDO")),
        "usuario_crea": "dbf-import",
    }


def _map_xgast(r):
    """XGAST.DBF → scintela.xgast — gastos varios V1..V9 del PRG.

    Las filas con NUM=1..9 alimentan los costos por área:
        V1+V2+V3 = gastos tejeduría     → GTEJ
        V4+V5+V6 = gastos tintorería    → GTIN
        V7+V8+V9 = gastos administración → GS
    """
    return {
        "fecha": _date(r.get("FECHA")),
        "doc": _str(r.get("DOC"), 5),
        "prov": _str(r.get("PROV"), 5),
        "concepto": _str(r.get("CONCEPTO"), 100),
        "num": _int(r.get("NUM")),
        "fechad": _date(r.get("FECHAD")),
        "importe": _num(r.get("IMPORTE")),
        "saldo": _num(r.get("SALDO")),
        "stat": _str(r.get("STAT"), 3),
        "clave": _str(r.get("CLAVE"), 3),
        "usuario_crea": "dbf-import",
    }


def _map_banco_trans(r):
    """DBF de movimientos bancarios → scintela.transacciones_bancarias.

    Compartido por PICHINCH.DBF y INTER.DBF. El no_banco se completa
    después en post_load según el lookup correspondiente. La estructura
    de columnas es idéntica entre los dos DBFs (ver _read_dbf comentario
    en INTELA copy/INTER.DBF).
    """
    return {
        "fecha": _date(r.get("FECHA")) or date.today(),
        "documento": _str(r.get("DOC"), 5) or "",
        "concepto": _str(r.get("CONCEPTO"), 50) or "",
        "fechad": _date(r.get("FECHAD")),
        "importe": _num(r.get("IMPORTE"), 0) or 0,
        "saldo": _num(r.get("SALDO")),
        "stat": _str(r.get("STAT"), 2),
        "no_banco": None,  # se completa después con lookup en banco
        "prov": _str(r.get("PROV"), 5),
        "numreferencia": _int(r.get("NUM")),
        "clave": _str(r.get("CLAVE"), 3),
        "usuario_crea": "dbf-import",
    }


# Alias retro-compat (los tests podrían referenciarlo todavía).
_map_pichincha_trans = _map_banco_trans


# ============================================================================
# TABLE_MAP — registro central. Si agregás un DBF, agregalo acá.
# ============================================================================

TABLE_MAP: dict[str, dict] = {
    "FACTURAS.DBF": {
        "pg_table": "scintela.factura",
        "mapper": _map_factura,
        "criticidad": "CRITICO",
        "descripcion": "Facturas — TOTF en balance, cartera, estado de cuenta",
        # TMT 2026-05-26: NO truncamos las facturas backfilleadas de Asinfo
        # (usuario_crea='asinfo-backfill'). Esas son históricas T/X que el
        # DBF purgó pero que recuperamos de Asinfo. El DBF NO debe pisarlas
        # en cada sync — son intocables. Sólo borramos las filas que vinieron
        # del DBF mismo (usuario_crea IS NULL o 'dbf-import' o cualquier
        # otro valor distinto al marcador 'asinfo-backfill').
        "delete_where": ("COALESCE(usuario_crea, '') NOT IN (%s, 'asinfo-carga')", "_lookup_asinfo_backfill_marker"),
    },
    "CHEQUES.DBF": {
        "pg_table": "scintela.cheque",
        "mapper": _map_cheque,
        "criticidad": "CRITICO",
        "descripcion": "Cheques — TOTC en balance",
        # Misma política: si en el futuro hacemos backfill de cheques de
        # Asinfo, los marcamos con usuario_crea='asinfo-backfill' y el sync
        # DBF los preserva. Por ahora no hay backfill de cheques, pero la
        # cláusula es no-op (no hay filas con ese marker) y deja la puerta
        # abierta sin tener que tocar el code path en el futuro.
        "delete_where": ("COALESCE(usuario_crea, '') NOT IN (%s, 'asinfo-carga')", "_lookup_asinfo_backfill_marker"),
    },
    "POSDAT.DBF": {
        "pg_table": "scintela.posdat",
        "mapper": _map_posdat,
        "criticidad": "SUPER",
        "descripcion": "Posdat — TOTP (PASIVOS) + POS1/POS2 a BANCOS",
    },
    "CAJA.DBF": {
        "pg_table": "scintela.caja",
        "mapper": _map_caja,
        "criticidad": "CRITICO",
        "descripcion": "Caja — SALCAJ del balance",
    },
    "DOLARES.DBF": {
        "pg_table": "scintela.dolares",
        "mapper": _map_dolares,
        "criticidad": "CRITICO",
        "descripcion": "Dólares — ANTICIPOS del balance",
    },
    "ACTIVOS.DBF": {
        "pg_table": "scintela.activos",
        "mapper": _map_activos,
        "criticidad": "CRITICO",
        "descripcion": "Activos fijos — UMAQ + UACT del balance",
    },
    "HISTORIA.DBF": {
        "pg_table": "scintela.historia",
        "mapper": _map_historia,
        "criticidad": "SUPER",
        "descripcion": "Snapshots mensuales — VSTO, VQX, PATANT, USUTI",
    },
    "INICIALE.DBF": {
        "pg_table": "scintela.iniciales",
        "mapper": _map_iniciales,
        "criticidad": "CRITICO",
        "descripcion": "Iniciales / proyecciones — KGPRO, PRETEJ, PRETIN, PREADM, PRETOT",
    },
    "COMPRAS.DBF": {
        "pg_table": "scintela.compra",
        "mapper": _map_compra,
        "criticidad": "utiles",
        "descripcion": "Compras — listado + alimenta posdat",
    },
    "FLUJO.DBF": {
        "pg_table": "scintela.flujo",
        "mapper": _map_flujo,
        "criticidad": "utiles",
        "descripcion": "Flujo — panel /informes/flujo/grafico",
    },
    "PICHINCH.DBF": {
        "pg_table": "scintela.transacciones_bancarias",
        "mapper": _map_banco_trans,
        "criticidad": "SUPER",
        "descripcion": "Banco Pichincha — saldo BANCOS del balance",
        "post_load": "asignar_no_banco_pichincha",
        # No truncamos toda la tabla — sólo las filas de este banco.
        # Permite que INTER.DBF coexista en transacciones_bancarias.
        "delete_where": ("no_banco = %s", "_lookup_no_banco_pichincha"),
    },
    "INTER.DBF": {
        "pg_table": "scintela.transacciones_bancarias",
        "mapper": _map_banco_trans,
        "criticidad": "SUPER",
        "descripcion": "Banco Internacional — segundo BANCOS del balance",
        "post_load": "asignar_no_banco_internacional",
        "delete_where": ("no_banco = %s", "_lookup_no_banco_internacional"),
    },
    "XGAST.DBF": {
        "pg_table": "scintela.xgast",
        "mapper": _map_xgast,
        "criticidad": "CRITICO",
        "descripcion": "Gastos varios V1..V9 — alimentan COSTOS (TEJIDO/GS.PROC/GASTOS) del panel Resultados",
    },
    "RETIROS.DBF": {
        "pg_table": "scintela.retiros",
        "mapper": _map_retiros,
        "criticidad": "CRITICO",
        "descripcion": "Retiros del dueño (dividendos / RR) — URET en balance",
    },
    "TINTO.DBF": {
        "pg_table": "scintela.tinto",
        "mapper": _map_tinto,
        "criticidad": "CRITICO",
        "descripcion": "Tintura del mes — alimenta COL.QUI. + KR del panel Resultados",
        # TMT 2026-06-09 dueña: planilla /informes/tinto-carga carga filas
        # directo en PC (usuario_crea='pc-carga'). El sync NO debe pisarlas.
        # PERO: scintela.tinto es "solo mes en curso" (el balance suma la
        # tabla ENTERA sin filtro de fecha, igual que el PRG) → las pc-carga
        # de meses anteriores SÍ se borran en cada sync, para no inflar el
        # balance del mes nuevo. Las filas de ajuste 'manual-kg-edit'
        # (editar KG en comparativa) son data de prueba: se borran siempre.
        "delete_where": (
            "COALESCE(usuario_crea, '') <> %s "
            "OR fecha < date_trunc('month', CURRENT_DATE)",
            "_lookup_pc_carga_marker",
        ),
    },
}


# Tablas que requieren manejo especial (multi-DBF, FK lookups, etc.)
# Por ahora solo PICHINCH necesita lookup de no_banco — más adelante
# podríamos agregar XGAST como segundo banco.


# ============================================================================
# Importer
# ============================================================================


_DEFAULT_ENCODING = "cp850"
_TRY_ENCODINGS = ("cp850", "cp1252", "latin-1", "utf-8")

# Override global desde --encoding (None = auto-detect).
FORCED_ENCODING: str | None = None


def _read_dbf(path: Path, encoding: str | None = None) -> list[dict]:
    """Lee un DBF y devuelve lista de dicts.

    TMT 2026-05-20 v8 — si `encoding` es None, prueba cp850 → cp1252 →
    latin-1 → utf-8 y se queda con el primero que no genere `?`
    (replacement char) en los primeros 200 caracteres de los string
    fields. Pre-mortem 2e.
    """
    if encoding:
        table = dbfread.DBF(
            str(path),
            encoding=encoding,
            load=True,
            ignore_missing_memofile=True,
            char_decode_errors="replace",
        )
        return [dict(rec) for rec in table]

    best_rows = None
    best_replacements = None
    best_enc = None
    for enc in _TRY_ENCODINGS:
        try:
            table = dbfread.DBF(
                str(path),
                encoding=enc,
                load=True,
                ignore_missing_memofile=True,
                char_decode_errors="replace",
            )
            rows = [dict(rec) for rec in table]
        except Exception:
            continue
        # Cuenta '?' en los primeros 50 rows como heurística
        sample = "".join(str(v) for r in rows[:50] for v in r.values() if isinstance(v, str))
        replacements = sample.count("?")
        if best_replacements is None or replacements < best_replacements:
            best_replacements = replacements
            best_rows = rows
            best_enc = enc
        if replacements == 0:
            break
    if best_enc and best_enc != _DEFAULT_ENCODING:
        import logging

        logging.getLogger("import_dbf").info(
            "Encoding detectado para %s: %s (replacements=%d)",
            path.name,
            best_enc,
            best_replacements or 0,
        )
    return best_rows or []


def _lookup_no_banco_pichincha() -> int | None:
    """Busca no_banco para Pichincha en scintela.banco. Si no hay, devuelve 1
    (convención del PRG legacy)."""
    row = db.fetch_one(
        "SELECT no_banco FROM scintela.banco WHERE UPPER(nombre) LIKE %s LIMIT 1",
        ("%PICHINCHA%",),
    )
    if row and row.get("no_banco"):
        return int(row["no_banco"])
    return 1  # default legacy


def _lookup_no_banco_internacional() -> int | None:
    """Busca no_banco para Internacional. Match parcial en 'nombre' porque el
    legacy lo guarda truncado a 9 chars: 'INTERNACI'."""
    row = db.fetch_one(
        "SELECT no_banco FROM scintela.banco WHERE UPPER(nombre) LIKE %s LIMIT 1",
        ("%INTERNAC%",),
    )
    if row and row.get("no_banco"):
        return int(row["no_banco"])
    return 32  # default por convención (se ve no_banco=32 en el dump del usuario)


def _post_load_pichincha(rows: list[dict]) -> list[dict]:
    """Asigna no_banco a cada fila de PICHINCH antes de insertar."""
    nb = _lookup_no_banco_pichincha()
    for r in rows:
        r["no_banco"] = nb
    return rows


def _post_load_internacional(rows: list[dict]) -> list[dict]:
    """Asigna no_banco=Internacional a cada fila de INTER.DBF."""
    nb = _lookup_no_banco_internacional()
    for r in rows:
        r["no_banco"] = nb
    return rows


_POST_LOAD_FNS = {
    "asignar_no_banco_pichincha": _post_load_pichincha,
    "asignar_no_banco_internacional": _post_load_internacional,
}


def _lookup_asinfo_backfill_marker() -> str:
    """Marker constante para `usuario_crea` de las filas backfilleadas
    desde Asinfo. Se usa con `delete_where` para que el sync DBF NO
    pise estas filas: el DELETE excluye `usuario_crea = 'asinfo-backfill'`.

    Si en el futuro hace falta otro marker (ej. 'asinfo-manual'), se
    puede expandir a una lista o cambiar la cláusula `delete_where` a
    `usuario_crea NOT IN (...)`. Por ahora un solo marker alcanza.
    """
    return "asinfo-backfill"


def _lookup_pc_carga_marker() -> str:
    """Marker de `usuario_crea` para filas de tintura cargadas en la
    planilla PC (/informes/tinto-carga). El sync preserva las del mes
    en curso — ver delete_where de TINTO.DBF."""
    return "pc-carga"


_DELETE_WHERE_FNS = {
    "_lookup_no_banco_pichincha": _lookup_no_banco_pichincha,
    "_lookup_no_banco_internacional": _lookup_no_banco_internacional,
    "_lookup_asinfo_backfill_marker": _lookup_asinfo_backfill_marker,
    "_lookup_pc_carga_marker": _lookup_pc_carga_marker,
}


def import_one(dbf_name: str, dbf_path: Path, dry_run: bool = False) -> dict:
    """Importa un DBF a su tabla destino.

    Devuelve {"ok": bool, "filas_leidas": int, "filas_insertadas": int, "msg": str}.
    """
    cfg = TABLE_MAP[dbf_name]
    pg_table = cfg["pg_table"]
    raw_rows = _read_dbf(dbf_path, encoding=FORCED_ENCODING)
    # TMT 2026-05-20 v8 — los mappers pueden devolver None para indicar
    # "skipear esta fila" (caso: stat legacy 'Y' borrada físicamente).
    mapped = [cfg["mapper"](r) for r in raw_rows]
    pg_rows = [r for r in mapped if r is not None]
    skipped_legacy = len(mapped) - len(pg_rows)

    # Post-load hooks (e.g. lookup no_banco for Pichincha) — sólo si NO es
    # dry-run, porque algunos hooks tocan la DB (pool no inicializado en dry-run).
    if cfg.get("post_load") and not dry_run:
        fn = _POST_LOAD_FNS.get(cfg["post_load"])
        if fn:
            pg_rows = fn(pg_rows)

    leidas = len(pg_rows)
    if dry_run:
        return {
            "ok": True,
            "filas_leidas": leidas,
            "filas_insertadas": 0,
            "msg": f"DRY-RUN: leí {leidas} filas, no inserté",
        }

    if leidas == 0:
        return {
            "ok": True,
            "filas_leidas": 0,
            "filas_insertadas": 0,
            "msg": "DBF vacío — saltado, Postgres conserva su data",
        }

    cols = list(pg_rows[0].keys())
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO {pg_table} ({', '.join(cols)}) VALUES ({placeholders})"

    insertadas = 0
    # db.tx() yielde una connection psycopg2; usamos cursor() + execute().
    with db.tx() as conn, conn.cursor() as cur:
        # TMT 2026-06-06: preservar las ediciones de PC de los GASTOS
        # PROYECTADOS (pretot/kprog/gprog) de scintela.iniciales. La dueña
        # los edita en /iniciales (Presupuesto) y el TRUNCATE+INSERT del DBF
        # los pisaría. Snapshot de las filas con usuario_modifica (= tocadas
        # en PC) ANTES del truncate; se restauran después del reload. Los
        # campos de STOCK (hilado/tejido/terminado/um/uk) sí vienen del DBF.
        # Ver memoria project_iniciales_editar_gastos_proyectados.
        _ini_overrides = []
        if pg_table == "scintela.iniciales":
            cur.execute(
                "SELECT yy, mesnum, pretot, kprog, gprog, usuario_modifica "
                "FROM scintela.iniciales WHERE usuario_modifica IS NOT NULL"
            )
            _ini_overrides = cur.fetchall()

        # Estrategia de limpieza pre-load:
        # - Default: TRUNCATE total (tabla 1:1 con DBF).
        # - delete_where: tabla compartida entre múltiples DBFs (caso
        #   transacciones_bancarias, donde PICHINCH.DBF y INTER.DBF
        #   van a la misma tabla diferenciados por no_banco). Sólo
        #   borra las filas correspondientes a este banco para no
        #   pisarse entre sí.
        delete_where = cfg.get("delete_where")
        if delete_where:
            where_sql, lookup_name = delete_where
            lookup_fn = _DELETE_WHERE_FNS.get(lookup_name)
            if lookup_fn is None:
                raise RuntimeError(f"delete_where lookup desconocido: {lookup_name!r}")
            param = lookup_fn()
            cur.execute(f"DELETE FROM {pg_table} WHERE {where_sql}", (param,))
        else:
            # TRUNCATE + RESTART IDENTITY para que id_* arranque de 1 con fresh data.
            cur.execute(f"TRUNCATE TABLE {pg_table} RESTART IDENTITY CASCADE")
        for r in pg_rows:
            cur.execute(sql, [r[c] for c in cols])
            insertadas += 1

        # Mismo criterio "dBase gana" para la TINTURA: la planilla se puede
        # cargar a mano en PC (/informes/tinto-carga, usuario_crea='pc-carga')
        # para no esperar el sync — pero cuando el DBF trae la misma partida
        # (misma fecha + cod + kg bruto), la copia PC se absorbe. Evita el
        # doble conteo de COL.QUI./KR/stock que advertía la pantalla de carga.
        # TMT 2026-06-10 (pedido dueña: proceso tintorería = dBase).
        if pg_table == "scintela.tinto":
            cur.execute(
                """
                DELETE FROM scintela.tinto a
                 WHERE a.usuario_crea = 'pc-carga'
                   AND EXISTS (
                        SELECT 1 FROM scintela.tinto b
                         WHERE COALESCE(b.usuario_crea, 'dbf-import') <> 'pc-carga'
                           AND b.fecha = a.fecha
                           AND UPPER(TRIM(COALESCE(b.cod,''))) =
                               UPPER(TRIM(COALESCE(a.cod,'')))
                           AND ABS(COALESCE(b.kg,0) - COALESCE(a.kg,0)) < 0.01
                   )
                """
            )
            if cur.rowcount:
                print(f"   [dBase gana] {cur.rowcount} partidas pc-carga absorbidas "
                      f"(el DBF trae la misma tintura)")

        # TMT 2026-06-10 (decisión dueña): "una carga de dBase GANA por sobre
        # todo". Si el DBF trae una factura que también existe como copia
        # asinfo (cargada con el botón o backfill), la copia asinfo se
        # absorbe — evita el doble conteo en cartera. Clave conservadora
        # (codigo_cli, numf, fecha) con numf>0; lo que no matchee exacto lo
        # muestra /admin/facturas-reconcile.
        if pg_table == "scintela.factura":
            cur.execute(
                """
                DELETE FROM scintela.factura a
                 WHERE a.usuario_crea IN ('asinfo-carga', 'asinfo-backfill')
                   AND COALESCE(a.numf, 0) > 0
                   AND EXISTS (
                        SELECT 1 FROM scintela.factura b
                         WHERE b.usuario_crea = 'dbf-import'
                           AND b.numf = a.numf
                           AND UPPER(TRIM(COALESCE(b.codigo_cli,''))) =
                               UPPER(TRIM(COALESCE(a.codigo_cli,'')))
                           AND b.fecha = a.fecha
                   )
                """
            )
            if cur.rowcount:
                print(f"   [dBase gana] {cur.rowcount} copias asinfo absorbidas "
                      f"(el DBF trae la misma factura)")

        # Restaurar las ediciones PC de gastos proyectados (ver snapshot arriba).
        if pg_table == "scintela.iniciales" and _ini_overrides:
            for yy, mesnum, pretot, kprog, gprog, usr in _ini_overrides:
                cur.execute(
                    "UPDATE scintela.iniciales "
                    "SET pretot = %s, kprog = %s, gprog = %s, usuario_modifica = %s "
                    "WHERE yy = %s AND mesnum = %s",
                    (pretot, kprog, gprog, usr, yy, mesnum),
                )

    msg = f"{insertadas} filas cargadas en {pg_table}"
    if skipped_legacy:
        msg += f" (+{skipped_legacy} skipeadas por stat legacy)"
    return {
        "ok": True,
        "filas_leidas": leidas,
        "filas_insertadas": insertadas,
        "filas_skipeadas_legacy": skipped_legacy,
        "msg": msg,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Sync DBF → Postgres scintela.* (proceso de transición)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--source-dir",
        default=str(DEFAULT_DBF_DIR),
        help=f"Carpeta donde están los .DBF (default: {DEFAULT_DBF_DIR})",
    )
    ap.add_argument("--dry-run", action="store_true", help="Lee los DBFs e informa, pero no toca Postgres.")
    ap.add_argument("--only", default="", help="Coma-separated DBF names (e.g. FACTURAS.DBF,CHEQUES.DBF)")
    ap.add_argument(
        "--list", action="store_true", help="Lista las tablas que el script sabe cargar y termina."
    )
    ap.add_argument(
        "--encoding",
        default=None,
        help=f"Forzar encoding (default: auto-detect entre {_TRY_ENCODINGS}). "
        "Si lo pasás, se usa para todos los DBFs.",
    )
    args = ap.parse_args()

    if args.list:
        print(f"{'DBF':<14} {'Tabla Postgres':<35} {'Crit':<8} Notas")
        print("-" * 100)
        for name in sorted(TABLE_MAP.keys()):
            c = TABLE_MAP[name]
            print(f"{name:<14} {c['pg_table']:<35} {c['criticidad']:<8} {c['descripcion']}")
        return

    if args.encoding:
        global FORCED_ENCODING
        FORCED_ENCODING = args.encoding

    src = Path(args.source_dir)
    if not src.exists():
        print(f"ERROR: source dir no existe: {src}", file=sys.stderr)
        sys.exit(2)

    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    if only:
        invalid = only - set(TABLE_MAP.keys())
        if invalid:
            print(f"ERROR: --only menciona DBFs desconocidos: {sorted(invalid)}", file=sys.stderr)
            print(f"DBFs válidos: {sorted(TABLE_MAP.keys())}", file=sys.stderr)
            sys.exit(2)

    targets = sorted(TABLE_MAP.keys())
    if only:
        targets = [t for t in targets if t in only]

    # En production / RDS, requerir confirmación explícita.
    db_host = os.environ.get("DB_HOST", "localhost")
    parece_prod = "rds.amazonaws.com" in db_host or os.environ.get("ENV", "").lower() == "production"
    if parece_prod and os.environ.get("I_KNOW_THIS_IS_PROD") != "1":
        print(f"ERROR: DB_HOST={db_host} parece producción.", file=sys.stderr)
        print("Si querés correr en prod, exportá I_KNOW_THIS_IS_PROD=1.", file=sys.stderr)
        sys.exit(2)

    if not args.dry_run:
        db.init_pool()
    print("Sync DBF → Postgres")
    print(f"  Source: {src}")
    print(f"  Target: {db_host}/{os.environ.get('DB_NAME', 'intela')}")
    if args.dry_run:
        print("  *** MODO DRY-RUN — no se modifica Postgres ***")
    if only:
        print(f"  Filtro: solo {sorted(only)}")
    print()

    resumen = {"ok": 0, "skip": 0, "fail": 0, "filas": 0}

    for name in targets:
        path = src / name
        cfg = TABLE_MAP[name]
        crit = cfg.get("criticidad", "?")

        if not path.exists():
            print(f"  - {name:<14} [{crit}]  no presente — Postgres conserva su data")
            resumen["skip"] += 1
            continue

        try:
            res = import_one(name, path, dry_run=args.dry_run)
            print(f"  ✓ {name:<14} [{crit}]  {res['msg']}")
            resumen["ok"] += 1
            resumen["filas"] += res["filas_insertadas"]
        except Exception as e:
            print(f"  ✗ {name:<14} [{crit}]  ERROR: {e}")
            resumen["fail"] += 1

    print()
    print(
        f"Resumen: {resumen['ok']} cargadas · {resumen['skip']} saltadas · "
        f"{resumen['fail']} con error · {resumen['filas']:,} filas totales"
    )

    if resumen["fail"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
