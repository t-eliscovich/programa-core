"""Parser tolerante para estados de cuenta en CSV de bancos ecuatorianos.

Los bancos emiten CSVs con quirks distintos. No tratamos de ser un parser
universal — detectamos qué formato es y parseamos conforme.

Formatos soportados hoy:
    - Pichincha "EstadoCuenta.csv" — 8 columnas, encoding CP1252, sep ';'.
    - Internacional "Movimientos.csv" — 7 columnas, UTF-8, sep ','.
    - Genérico — heurístico por nombres de columnas.

Devuelve siempre la misma estructura (`BancoLinea` dataclass) para que el
matcher downstream no tenga que preocuparse por el origen.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

_LOG = logging.getLogger("programa_core.conciliacion.parser")


@dataclass(frozen=True)
class BancoLinea:
    """Una línea de estado de cuenta, normalizada.

    - fecha: cuando el banco registra el movimiento
    - concepto: descripción / motivo (el texto libre del banco)
    - referencia: número de transacción / cheque / etc (string — puede tener ceros a la izquierda)
    - debito: monto egreso (positivo). Sólo uno de debito/credito tiene valor.
    - credito: monto ingreso (positivo). Para depósitos de cheques, va acá.
    - banco: etiqueta del CSV de origen (pichincha / internacional / otro).
    """
    fecha: date
    concepto: str
    referencia: str
    debito: Decimal
    credito: Decimal
    banco: str


_FECHA_PATTERNS = [
    re.compile(r"^(\d{2})/(\d{2})/(\d{4})$"),
    re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),
    re.compile(r"^(\d{2})-(\d{2})-(\d{4})$"),
]


def _parse_fecha(s: str) -> date | None:
    s = (s or "").strip()
    for i, pat in enumerate(_FECHA_PATTERNS):
        m = pat.match(s)
        if m:
            if i == 1:  # YYYY-MM-DD
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return date(y, mo, d)
            except ValueError:
                return None
    return None


def _parse_monto(s: str) -> Decimal:
    """Acepta '1.234,56' (es-EC) y '1234.56' (ISO). Devuelve Decimal, 0 si vacío."""
    s = (s or "").strip().replace(" ", "")
    if not s:
        return Decimal(0)
    # Detectar formato: si tiene coma Y punto, asumir punto-miles/coma-decimales.
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # Puede ser '1234,56' (es) o '1,234' (en sin decimales) — ambiguo,
        # preferimos interpretar como decimal es.
        s = s.replace(",", ".")
    # Signo negativo puede venir como "(123.45)" en bancos conservadores
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal(0)


def _normalizar_bytes(raw: bytes) -> str:
    """Devuelve string UTF-8. Maneja BOM y CP1252 gracefully."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    # Probá UTF-8 primero; si falla, asumí CP1252 (Windows Ecuador default)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _detectar_separador(texto: str) -> str:
    """La primera línea típicamente tiene N semicolons o N commas. El que
    aparezca más veces es el separador."""
    primera = texto.split("\n", 1)[0]
    return ";" if primera.count(";") > primera.count(",") else ","


def _detectar_banco(headers: list[str]) -> str:
    lower = {h.lower().strip() for h in headers}
    if any("oficina" in h or "pichincha" in h for h in lower):
        return "pichincha"
    if any("internacional" in h for h in lower):
        return "internacional"
    return "otro"


def parse_csv(raw: bytes) -> list[BancoLinea]:
    """Punto de entrada único — parsea los bytes del upload a `list[BancoLinea]`.

    Tolera:
        - BOM UTF-8
        - encoding CP1252 (fallback si UTF-8 falla)
        - separador `;` o `,`
        - headers con capitalización arbitraria
        - columnas opcionales ausentes

    Líneas inválidas se ignoran (log a WARNING) — no rompen el batch.
    """
    if not raw:
        return []

    texto = _normalizar_bytes(raw)
    sep = _detectar_separador(texto)

    reader = csv.DictReader(io.StringIO(texto), delimiter=sep)
    headers = reader.fieldnames or []
    banco = _detectar_banco(headers)

    # Mapeo flexible de columnas. Acepta variaciones obvias.
    def _col(row: dict, *candidates: str) -> str:
        for c in candidates:
            for k in row:
                if k and k.strip().lower() == c.lower():
                    return (row[k] or "").strip()
        return ""

    salida: list[BancoLinea] = []
    for i, row in enumerate(reader, start=2):
        fecha_s = _col(row, "fecha", "fecha movimiento", "fecha de transaccion", "fecha_mov")
        fecha = _parse_fecha(fecha_s)
        if not fecha:
            _LOG.debug("fila %d: fecha inválida %r, ignorando", i, fecha_s)
            continue
        concepto = _col(row, "concepto", "descripcion", "detalle", "observacion")
        referencia = _col(row, "referencia", "numero", "nro cheque", "documento", "n_documento")
        debito = _parse_monto(_col(row, "debito", "debe", "egreso", "cargos"))
        credito = _parse_monto(_col(row, "credito", "haber", "ingreso", "abonos"))
        # Si sólo hay "monto", usar signo para desambiguar.
        if not debito and not credito:
            monto = _parse_monto(_col(row, "monto", "importe", "valor"))
            if monto < 0:
                debito = -monto
            else:
                credito = monto

        salida.append(BancoLinea(
            fecha=fecha,
            concepto=concepto,
            referencia=referencia,
            debito=debito,
            credito=credito,
            banco=banco,
        ))
    return salida
