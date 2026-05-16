"""Helper compartido para upload de CSV en los módulos transaccionales.

Parser tolerante:
    - encoding UTF-8 (con BOM) o CP1252.
    - separador ';' o ','.
    - fechas DD/MM/YYYY, ISO, DD-MM-YYYY.
    - montos '1234.56' (ISO) o '1.234,56' (es-EC).
    - celdas vacías se devuelven como None (no string "").

Contrato del caller:
    1. Definir `COLS` (list[tuple[str, str, bool]]) = (campo, header, required).
    2. Definir `CONVERTERS` (dict[str, callable]) opcional para casos especiales.
    3. Llamar `procesar_csv(raw_bytes, cols, crear_fn, converters)`.
    4. Devuelve `ResultadoUpload(ok=N, error=M, detalles=[...], plantilla_csv=str)`.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass
class ResultadoUpload:
    ok: int = 0
    error: int = 0
    detalles: list[dict] = field(default_factory=list)  # [{linea, ok|error, mensaje, datos}]

    @property
    def total(self) -> int:
        return self.ok + self.error


_FECHA_PATS = [
    re.compile(r"^(\d{2})/(\d{2})/(\d{4})$"),
    re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),
    re.compile(r"^(\d{2})-(\d{2})-(\d{4})$"),
]


def parse_fecha(s: str | None) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    for i, pat in enumerate(_FECHA_PATS):
        m = pat.match(s)
        if not m:
            continue
        if i == 1:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def parse_monto(s: str | None) -> Decimal | None:
    """Acepta '1234.56', '1.234,56', '1234,56'. None si vacío, error si basura."""
    if s is None:
        return None
    s = str(s).strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"monto inválido: {s!r}") from e


def parse_int(s: str | None) -> int | None:
    if s is None or str(s).strip() == "":
        return None
    try:
        return int(str(s).strip())
    except ValueError as e:
        raise ValueError(f"entero inválido: {s!r}") from e


def parse_bool(s: str | None) -> bool:
    if s is None:
        return False
    v = str(s).strip().lower()
    return v in ("1", "true", "sí", "si", "s", "yes", "y", "t")


def _normalizar_bytes(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _detectar_separador(texto: str) -> str:
    primera = texto.split("\n", 1)[0]
    return ";" if primera.count(";") > primera.count(",") else ","


def plantilla_csv(cols: list[tuple[str, str, bool]]) -> str:
    """Devuelve un CSV con sólo la fila de header + 1 fila de ejemplo vacía.

    El header es el segundo elemento de cada tuple (el nombre legible).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([h for _c, h, _r in cols])
    writer.writerow(["" for _ in cols])
    return buf.getvalue()


# Tipos implícitos por prefijo del nombre de campo.
_INT_FIELDS = {"numf", "no_banco", "numero"}
_FECHA_FIELDS = {"fecha", "fechad", "vencimiento", "fecha_emision", "fecha_cierre"}
_MONTO_FIELDS = {"importe", "kg", "valor", "saldo"}
_BOOL_FIELDS = {"pagada"}


def _coerce(name: str, raw: str, converters: dict | None = None):
    """Convierte el string del CSV al tipo correcto inferido del nombre del campo.

    Se puede override via `converters[nombre] = callable`.
    """
    if converters and name in converters:
        return converters[name](raw)
    if name in _FECHA_FIELDS:
        return parse_fecha(raw)
    if name in _INT_FIELDS:
        return parse_int(raw)
    if name in _MONTO_FIELDS:
        m = parse_monto(raw)
        return m
    if name in _BOOL_FIELDS:
        return parse_bool(raw)
    # default: string, trim
    s = (raw or "").strip()
    return s or None


def procesar_csv(
    raw: bytes,
    cols: list[tuple[str, str, bool]],
    crear_fn,
    converters: dict | None = None,
    usuario: str = "web",
) -> ResultadoUpload:
    """Parsea `raw` según `cols` y llama `crear_fn(**row)` por cada fila.

    Args:
        raw: bytes del archivo subido.
        cols: lista de (campo, header, required).
        crear_fn: función con firma `crear_fn(usuario=..., **campos)`.
        converters: opcional {campo: callable} para override de tipos.
        usuario: para `usuario_crea`.

    Returns:
        `ResultadoUpload` con ok/error counts y detalles por fila.
    """
    result = ResultadoUpload()
    if not raw:
        return result

    texto = _normalizar_bytes(raw)
    sep = _detectar_separador(texto)
    reader = csv.DictReader(io.StringIO(texto), delimiter=sep)

    # Mapeo tolerante: headers del CSV → campos de cols.
    # Case-insensitive + accent-insensitive + trim (la user puede tipear "Codigo"
    # sin tilde y el CSV original tenía "Código").
    import unicodedata
    def _norm(s: str) -> str:
        s = (s or "").strip().lower()
        # Descomponer acentos y descartarlos: "código" → "codigo"
        return "".join(
            c for c in unicodedata.normalize("NFKD", s)
            if not unicodedata.combining(c)
        )

    headers = reader.fieldnames or []
    header_map = {}
    for campo, header, _req in cols:
        for h in headers:
            if h and _norm(h) == _norm(header):
                header_map[campo] = h
                break

    for i, row in enumerate(reader, start=2):
        datos: dict = {}
        fila_error = None
        for campo, header, required in cols:
            h = header_map.get(campo)
            raw_val = row.get(h, "") if h else ""
            try:
                val = _coerce(campo, raw_val, converters)
            except ValueError as e:
                fila_error = str(e)
                break
            if required and val is None:
                fila_error = f"Falta campo obligatorio: {header}"
                break
            datos[campo] = val

        if fila_error:
            result.error += 1
            result.detalles.append({
                "linea": i, "ok": False, "mensaje": fila_error, "datos": row,
            })
            continue

        try:
            res = crear_fn(usuario=usuario, **datos)
            result.ok += 1
            result.detalles.append({
                "linea": i, "ok": True,
                "mensaje": f"creado · {res}" if res else "creado",
                "datos": row,
            })
        except ValueError as e:
            # Reglas de negocio (período cerrado, cliente inexistente, etc).
            result.error += 1
            result.detalles.append({
                "linea": i, "ok": False, "mensaje": str(e), "datos": row,
            })
        except Exception as e:
            # FK violation, constraint, etc.
            result.error += 1
            result.detalles.append({
                "linea": i, "ok": False,
                "mensaje": f"{type(e).__name__}: {e}", "datos": row,
            })

    return result
