"""Parser del xlsx de carga masiva de cupos.

TMT 2026-08-05 — la dueña completó el Excel "clientes-sin-cupo" (423 filas)
con la columna **CUPO A CARGAR** y pidió subirlos todos: *"los que están
vacíos son clientes que no tienen crédito, pueden quedar en 0"*.

Cargar 423 fichas a mano por /clientes/<cod>/editar es inviable → falta UI
(regla operar-por-la-ui). Este módulo es la mitad pura (testeable sin app):
lee el xlsx y devuelve (codigo, cupo) por fila.

Contrato:
  * Se busca la fila de encabezados: la que tenga una celda "CÓDIGO" (o
    "CODIGO") y alguna celda que contenga "CUPO". No importa en qué fila
    esté ni cuántas columnas decorativas haya alrededor.
  * Columna de cupo: la que contenga "CARGAR" en el header si existe
    ("CUPO A CARGAR"); si no, la ÚLTIMA que contenga "CUPO" (para no
    agarrar un eventual "CUPO ACTUAL" exportado a la izquierda).
  * Celda de cupo vacía → **0** (cliente sin crédito, pedido explícito).
  * Cupo no numérico o negativo → la fila se SALTEA con aviso, nunca se
    inventa un número.
  * Código repetido en el archivo → vale la ÚLTIMA aparición, con aviso.
"""

from __future__ import annotations

import io
from dataclasses import dataclass


@dataclass(frozen=True)
class FilaCupo:
    codigo: str
    cupo: int
    fila: int  # número de fila en el xlsx (1-based), para los avisos


def _norm(celda) -> str:
    return str(celda or "").strip().upper()


def parse_cupos_xlsx(data: bytes) -> tuple[list[FilaCupo], list[str]]:
    """Devuelve (filas, avisos). Si no encuentra encabezados, filas=[]."""
    import openpyxl

    avisos: list[str] = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return [], ["No se pudo leer el archivo — ¿es un .xlsx válido?"]

    ws = wb.active
    col_codigo = col_cupo = None
    fila_header = 0
    filas_raw = ws.iter_rows(values_only=True)
    for i, row in enumerate(filas_raw, start=1):
        headers = [_norm(c) for c in row]
        if "CÓDIGO" in headers or "CODIGO" in headers:
            idx_cod = headers.index("CÓDIGO") if "CÓDIGO" in headers else headers.index("CODIGO")
            cupos = [j for j, h in enumerate(headers) if "CUPO" in h]
            if cupos:
                con_cargar = [j for j in cupos if "CARGAR" in headers[j]]
                col_codigo = idx_cod
                col_cupo = con_cargar[0] if con_cargar else cupos[-1]
                fila_header = i
                break
    if col_codigo is None:
        return [], [
            "No encontré los encabezados: hace falta una columna CÓDIGO y una "
            "columna de CUPO (ej. 'CUPO A CARGAR')."
        ]

    por_codigo: dict[str, FilaCupo] = {}
    for i, row in enumerate(filas_raw, start=fila_header + 1):
        codigo = _norm(row[col_codigo] if col_codigo < len(row) else "")
        if not codigo:
            continue
        crudo = row[col_cupo] if col_cupo < len(row) else None
        if crudo is None or (isinstance(crudo, str) and not crudo.strip()):
            cupo = 0  # vacío = sin crédito (pedido dueña 05/08)
        else:
            try:
                cupo = int(round(float(str(crudo).strip().replace(",", "."))))
            except (TypeError, ValueError):
                avisos.append(f"Fila {i} ({codigo}): cupo no numérico ({crudo!r}) — salteada.")
                continue
            if cupo < 0:
                avisos.append(f"Fila {i} ({codigo}): cupo negativo ({cupo}) — salteada.")
                continue
        if codigo in por_codigo:
            avisos.append(
                f"Fila {i}: código {codigo} repetido en el archivo — vale esta última."
            )
        por_codigo[codigo] = FilaCupo(codigo=codigo, cupo=cupo, fila=i)

    return list(por_codigo.values()), avisos
