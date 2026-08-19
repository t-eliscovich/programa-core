"""Parser del xlsx de carga masiva de GRUPOS de clientes.

TMT 2026-08-19 (duena): los grupos viven hace anos en un cuaderno de la
oficina, una fila por grupo, el codigo del grupo primero y al lado los codigos
que van con el. Son ~45 grupos y ~130 clientes: cargarlos de a uno por el
lapicito es una tarde. Mismo remedio que los cupos de agosto (`cupos_xlsx`):
sube el Excel, ve el preview, confirma.

Contrato (a proposito el mismo espiritu que `cupos_xlsx`, para que la oficina
no tenga que aprender dos formatos):

  * Encabezados: se busca la primera fila que tenga una celda "GRUPO" y una
    celda "CODIGO" / "CODIGO CLIENTE". No importa en que fila este ni cuantas
    columnas decorativas haya al lado (la columna CLIENTE con el nombre, o una
    columna REVISAR con notas, se ignoran solas).
  * Una fila = un cliente. La columna GRUPO lleva el codigo del cliente CABEZA
    del grupo, repetido en todas las filas del grupo.
  * La fila donde GRUPO == CODIGO es la del padre: se ACEPTA y se ignora, no
    genera fila en la tabla (el padre no se apunta a si mismo, ver
    `grupos.py`). Esta permitida porque asi el Excel se lee como el cuaderno.
  * Fila sin grupo o sin codigo -> se saltea con aviso.
  * Codigo repetido en el archivo con DOS grupos distintos -> se saltea con
    aviso y no se elige ninguno. Es el unico caso donde adivinar hace dano de
    verdad: mete el saldo del cliente en el grupo equivocado y la impresion por
    grupo lo suma donde no va. Que lo resuelva la duena mirando el cuaderno.
  * Codigo repetido con el MISMO grupo -> vale una sola vez, sin aviso (una
    fila duplicada en el Excel no es una decision, es un copy/paste).
"""

from __future__ import annotations

import io
from dataclasses import dataclass


@dataclass(frozen=True)
class FilaGrupo:
    grupo: str
    codigo: str
    fila: int  # numero de fila en el xlsx (1-based), para los avisos


def _norm(celda) -> str:
    return str(celda or "").strip().upper()


def _buscar_headers(rows) -> tuple[int, int, int]:
    """(fila_header, col_grupo, col_codigo) o (0, -1, -1) si no aparecen."""
    for i, row in enumerate(rows, start=1):
        headers = [_norm(c) for c in row]
        col_grupo = next((j for j, h in enumerate(headers) if h == "GRUPO"), None)
        if col_grupo is None:
            col_grupo = next((j for j, h in enumerate(headers) if h.startswith("GRUPO")), None)
        col_cod = next(
            (j for j, h in enumerate(headers)
             if h in ("CODIGO", "CÓDIGO", "CODIGO CLIENTE", "CÓDIGO CLIENTE")),
            None,
        )
        if col_grupo is not None and col_cod is not None:
            return i, col_grupo, col_cod
    return 0, -1, -1


def parse_grupos_xlsx(data: bytes) -> tuple[list[FilaGrupo], list[str]]:
    """Devuelve (filas, avisos). Si no encuentra encabezados, filas=[]."""
    import openpyxl

    avisos: list[str] = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return [], ["No se pudo leer el archivo — ¿es un .xlsx válido?"]

    ws = wb.active
    todas = list(ws.iter_rows(values_only=True))
    fila_header, col_grupo, col_cod = _buscar_headers(todas)
    if col_grupo < 0:
        return [], [
            "No encontré los encabezados. El archivo necesita una fila con una "
            "celda GRUPO y otra CÓDIGO."
        ]

    vistos: dict[str, tuple[str, int]] = {}
    conflictivos: set[str] = set()
    orden: list[str] = []
    for i, row in enumerate(todas[fila_header:], start=fila_header + 1):
        grupo = _norm(row[col_grupo]) if col_grupo < len(row) else ""
        codigo = _norm(row[col_cod]) if col_cod < len(row) else ""
        if not grupo and not codigo:
            continue  # fila en blanco: separador visual, no es un error
        if not grupo or not codigo:
            avisos.append(f"Fila {i}: le falta el grupo o el código — la salteo.")
            continue
        if grupo == codigo:
            continue  # la fila del padre: se lee lindo en el Excel, no va a la tabla
        anterior = vistos.get(codigo)
        if anterior is None:
            vistos[codigo] = (grupo, i)
            orden.append(codigo)
        elif anterior[0] != grupo:
            conflictivos.add(codigo)
            avisos.append(
                f"Fila {i}: {codigo} aparece en el grupo {anterior[0]} (fila "
                f"{anterior[1]}) y en el grupo {grupo}. No cargo ninguno de los "
                f"dos — un cliente va en un solo grupo."
            )

    filas = [
        FilaGrupo(grupo=vistos[c][0], codigo=c, fila=vistos[c][1])
        for c in orden
        if c not in conflictivos
    ]
    if not filas and not avisos:
        avisos.append("El archivo no tiene ninguna fila con grupo y código.")
    return filas, avisos
