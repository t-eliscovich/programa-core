"""Tests for modules.cheques.queries.reversar() — la máquina de estados nueva.

Vocabulario canónico (2026-04-29 — ver docs/SKILL_ADDENDUM_BATCH_18.md):

Reversar() ahora usa una máquina de estados con 3 destinos posibles
según stat origen:

    Stat origen → Stat destino    Es rebote real?
    -----------------------------------------------
    B (Pichincha)  → 1            sí (stop)
    1 (devuelto #1) → 3           sí (stop)
    2 (devuelto #2) → 3           sí (stop)
    A (legacy acred.) → 1         sí (stop)
    Z (cartera)    → X            no (administrativo)
    D (Daniela)    → X            no (administrativo)
    P (postergado) → X            no (administrativo)
    V (legacy Inter) → X          no (administrativo)
    X, R, 3        → ValueError   terminal

Estos tests NO tocan Postgres. Monkeypatchean db.fetch_one/fetch_all/execute/tx
para capturar las llamadas y verificar los efectos en memoria.
"""

from __future__ import annotations

import contextlib
from datetime import date

import pytest

# --- helpers ---------------------------------------------------------------


class _DBRecorder:
    """Stub para db.* que registra cada llamada y devuelve filas pre-cargadas."""

    def __init__(
        self,
        *,
        cheque: dict,
        aplic: list[dict] | None = None,
        factura: dict | None = None,
        rowcount_cliente_update: int = 1,
    ):
        self.cheque = cheque
        self.aplic = aplic or []
        self.factura = factura
        self.rowcount_cliente_update = rowcount_cliente_update
        self.executes: list[tuple[str, tuple]] = []

    # --- SELECTs
    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque where id_cheque" in s:
            return dict(self.cheque) if self.cheque else None
        if "from scintela.factura where id_factura" in s:
            return dict(self.factura) if self.factura else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.chequesxfact where id_cheque" in s:
            return [dict(a) for a in self.aplic]
        return []

    # --- writes
    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        # Simular el guard WHERE stop!='S': si el cliente ya estaba en stop,
        # el rowcount es 0 aunque el UPDATE se emita.
        if "update scintela.cliente" in s and "stop='s'" in s:
            return self.rowcount_cliente_update
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        """Stub para mov_doble.registrar() — devuelve un id sintético.

        TMT 2026-05-21: mov_doble.registrar usa execute_returning para
        obtener el id_mov_doble nuevo. Antes este método NO estaba
        monkeypatched y _DBRecorder.tx() devolvía un object() pelado,
        lo que rompía cuando registrar() intentaba conn.cursor().
        """
        self.executes.append((sql, tuple(params or ())))
        # mov_doble.registrar espera un dict con id_mov_doble.
        return {"id_mov_doble": 9999}

    # --- tx context manager
    @contextlib.contextmanager
    def tx(self):
        yield object()  # un sentinel — ningún código de reversar() lo inspecciona

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)
        monkeypatch.setattr(db_mod, "tx", self.tx)


def _contains_cliente_stop_update(executes: list[tuple[str, tuple]]) -> bool:
    """Hay al menos un execute() que sea el UPDATE scintela.cliente SET stop='S'?

    TMT 2026-05-21 dueña: el STOP es SOLO MANUAL — ya no se aplica al rebote.
    Este helper queda para tests legacy y para asegurar que NO se aplica.
    """
    for sql, _params in executes:
        s = " ".join(sql.split()).lower()
        if "update scintela.cliente" in s and "stop='s'" in s:
            return True
    return False


def _contains_cliente_observacion_update(executes: list[tuple[str, tuple]]) -> bool:
    """Hay al menos un execute() que sea el UPDATE scintela.cliente SET observacion?

    Es el camino nuevo (TMT 2026-05-21): cuando un cheque rebota, se anota
    [REBOTE] en la observación del cliente — el STOP lo decide la dueña aparte.
    """
    for sql, _params in executes:
        s = " ".join(sql.split()).lower()
        if "update scintela.cliente" in s and "observacion" in s:
            return True
    return False


def _cheque_update_stat(executes: list[tuple[str, tuple]]) -> str | None:
    """Retorna el primer parámetro del UPDATE scintela.cheque SET stat=%s."""
    for sql, params in executes:
        s = " ".join(sql.split()).lower()
        if s.startswith("update scintela.cheque set stat=%s"):
            return params[0] if params else None
    return None


# --- tests -----------------------------------------------------------------


def test_reversar_B_to_1_anota_observacion_pero_no_aplica_stop(monkeypatch):
    """Cheque depositado en Pichincha (B) que rebota → stat=1.

    TMT 2026-05-21 dueña: el STOP es SOLO MANUAL. Antes este flow ponía
    el cliente en STOP automáticamente; ahora solo anota [REBOTE] en la
    observación del cliente. stop_aplicado siempre False.
    """
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 42,
            "no_cheque": "12345678",
            "stat": "B",
            "codigo_cli": "JTX",
        },
        aplic=[],
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

    out = queries.reversar(id_cheque=42, motivo="fondos insuficientes", usuario="tmt")

    assert out["es_rebote_real"] is True
    assert out["stop_aplicado"] is False  # STOP solo manual (dueña 2026-05-21)
    assert out["stat_previo"] == "B"
    assert out["stat_nuevo"] == "1"
    assert out["codigo_cli"] == "JTX"
    assert _cheque_update_stat(rec.executes) == "1"
    assert not _contains_cliente_stop_update(rec.executes), (
        "El UPDATE SET stop='S' NO debe emitirse — el STOP es solo manual."
    )
    assert _contains_cliente_observacion_update(rec.executes), (
        "Debe anotar [REBOTE] en la observación del cliente."
    )


def test_reversar_1_to_3_segundo_rebote(monkeypatch):
    """Cheque ya rebotado (1) que se redepositó y volvió rebotado → stat=3."""
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 43,
            "no_cheque": "99999",
            "stat": "1",
            "codigo_cli": "ACM",
        },
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

    out = queries.reversar(id_cheque=43, motivo="segundo rechazo", usuario="tmt")

    assert out["es_rebote_real"] is True
    assert out["stop_aplicado"] is False  # STOP solo manual (2026-05-21)
    assert out["stat_previo"] == "1"
    assert out["stat_nuevo"] == "3"
    assert _cheque_update_stat(rec.executes) == "3"
    assert not _contains_cliente_stop_update(rec.executes)
    assert _contains_cliente_observacion_update(rec.executes)


def test_reversar_A_legacy_to_1_flips_stop(monkeypatch):
    """Cheque legacy 'A' (acreditado) que rebota tardío → stat=1 + STOP."""
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 44,
            "no_cheque": "LEGACY",
            "stat": "A",
            "codigo_cli": "OLD",
        },
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

    out = queries.reversar(id_cheque=44, motivo="rebote tardío", usuario="tmt")

    assert out["es_rebote_real"] is True
    assert out["stop_aplicado"] is False  # STOP solo manual (2026-05-21)
    assert out["stat_nuevo"] == "1"
    assert _cheque_update_stat(rec.executes) == "1"
    assert not _contains_cliente_stop_update(rec.executes)
    assert _contains_cliente_observacion_update(rec.executes)


def test_reversar_Z_to_X_no_stop(monkeypatch):
    """Cheque en cartera (Z) — eliminado por error. Stat=X. Cliente intacto."""
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 45,
            "no_cheque": "7777",
            "stat": "Z",
            "codigo_cli": "FOO",
        },
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

    out = queries.reversar(id_cheque=45, motivo="cargado por error", usuario="tmt")

    assert out["es_rebote_real"] is False
    assert out["stop_aplicado"] is False
    assert out["stat_previo"] == "Z"
    assert out["stat_nuevo"] == "X"
    assert _cheque_update_stat(rec.executes) == "X"
    assert not _contains_cliente_stop_update(rec.executes), (
        "Z→X no debe tocar el cliente — es eliminación administrativa."
    )


def test_reversar_P_to_X_no_stop(monkeypatch):
    """Cheque postergado (P) — anulación administrativa. Stat=X."""
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 46,
            "no_cheque": "8888",
            "stat": "P",
            "codigo_cli": "BAR",
        },
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

    out = queries.reversar(id_cheque=46, motivo="postdate cancelado", usuario="tmt")

    assert out["es_rebote_real"] is False
    assert out["stop_aplicado"] is False
    assert out["stat_nuevo"] == "X"
    assert not _contains_cliente_stop_update(rec.executes)


def test_reversar_D_to_X_no_stop(monkeypatch):
    """Cheque en gestión Daniela (D) — devuelto por Daniela. Stat=X, sin stop.

    Bajo el vocabulario nuevo, D ya NO es 'depositado' — es 'Daniela'.
    Cancelar un cheque desde Daniela no es un rebote del banco, es una
    decisión administrativa (Daniela devuelve el cheque al cliente).
    """
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 47,
            "no_cheque": "DANIELA",
            "stat": "D",
            "codigo_cli": "FOO",
        },
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

    out = queries.reversar(id_cheque=47, motivo="Daniela devuelve", usuario="tmt")

    assert out["es_rebote_real"] is False
    assert out["stop_aplicado"] is False
    assert out["stat_nuevo"] == "X"
    assert not _contains_cliente_stop_update(rec.executes)


def test_reversar_idempotent_when_cliente_already_stopped(monkeypatch):
    """Cliente ya en STOP: ya no se intenta tocar el flag stop, solo observación.

    TMT 2026-05-21: el STOP es SOLO MANUAL. Este test ya no valida idempotencia
    del WHERE stop!='S' (porque no existe el UPDATE de stop) — verifica que
    aun con el cliente en STOP, la observación se anota igual y stop_aplicado
    es False.
    """
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 48,
            "no_cheque": "1234",
            "stat": "B",
            "codigo_cli": "JTX",
        },
        rowcount_cliente_update=0,  # ya no relevante, dejado por simetría
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

    out = queries.reversar(id_cheque=48, motivo="segundo rebote", usuario="tmt")

    assert out["es_rebote_real"] is True
    assert out["stop_aplicado"] is False
    # Nunca debe emitirse un UPDATE SET stop='S' — ese flag es solo manual.
    assert not _contains_cliente_stop_update(rec.executes)
    # Sí se anota [REBOTE] en la observación.
    assert _contains_cliente_observacion_update(rec.executes)


def test_reversar_terminal_levanta(monkeypatch):
    """Stats terminales (X, R, 3) levantan ValueError."""
    import db
    from modules.cheques import queries

    for terminal in ("X", "R", "3"):
        rec = _DBRecorder(
            cheque={
                "id_cheque": 49,
                "no_cheque": "x",
                "stat": terminal,
                "codigo_cli": "JTX",
            },
        )
        rec.apply_to(monkeypatch, db)
        monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

        with pytest.raises(ValueError, match="terminal"):
            queries.reversar(id_cheque=49, motivo="x", usuario="tmt")


def test_reversar_bloquea_periodo_cerrado(monkeypatch):
    """Si el período contable de hoy está cerrado, levanta antes de abrir tx."""
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 50,
            "no_cheque": "x",
            "stat": "B",
            "codigo_cli": "JTX",
        },
    )
    rec.apply_to(monkeypatch, db)

    def _raise(_f):
        raise ValueError("Período contable cerrado.")

    monkeypatch.setattr(queries, "asegurar_fecha_abierta", _raise)

    with pytest.raises(ValueError, match="Per.odo contable cerrado"):
        queries.reversar(id_cheque=50, motivo="x", usuario="tmt")

    # Y no se hizo NINGÚN execute — la guarda corta antes de abrir la tx.
    assert rec.executes == [], "El guard de período debe cortar antes de escribir."


def test_reversar_observacion_capada_a_200_chars_en_sql(monkeypatch):
    """El UPDATE de cliente usa RIGHT(..., 200) para no desbordar varchar(200)."""
    import db
    from modules.cheques import queries

    rec = _DBRecorder(
        cheque={
            "id_cheque": 51,
            "no_cheque": "CAPTEST",
            "stat": "B",
            "codigo_cli": "JTX",
        },
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda _f: None)

    queries.reversar(id_cheque=51, motivo="test cap", usuario="tmt")

    # TMT 2026-05-21: el UPDATE ya no toca el flag stop, sólo la observación.
    # Encontrar el UPDATE cliente y asegurar que pasa por RIGHT(..., 200).
    cliente_update = None
    for sql, params in rec.executes:
        s = " ".join(sql.split()).lower()
        if "update scintela.cliente" in s and "observacion" in s:
            cliente_update = (s, params)
            break
    assert cliente_update is not None
    assert "right(" in cliente_update[0], (
        "El append a observacion debe estar envuelto en RIGHT(...) para capar a 200."
    )
    assert queries._OBS_CAP in cliente_update[1], "El cap (200) se pasa como parámetro al RIGHT()."


def test_reversar_fecha_de_hoy(monkeypatch):
    """El guard de período usa date.today() (hoy), no la fecha del cheque."""
    import db
    from modules.cheques import queries

    captured: dict = {}

    def _capture(fecha):
        captured["fecha"] = fecha

    rec = _DBRecorder(
        cheque={
            "id_cheque": 52,
            "no_cheque": "FECHA",
            "stat": "Z",
            "codigo_cli": "JTX",
        },
    )
    rec.apply_to(monkeypatch, db)
    monkeypatch.setattr(queries, "asegurar_fecha_abierta", _capture)

    queries.reversar(id_cheque=52, motivo="x", usuario="tmt")

    assert captured["fecha"] == date.today()
