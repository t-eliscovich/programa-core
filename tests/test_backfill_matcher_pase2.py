"""Tests para el matcher backfill, pase 2 (numf=0).

TMT 2026-05-28: agregamos un segundo pase al backfill que matchea
facturas PC con numf=0 por (cli + fecha ±1d + importe), porque antes
quedaban fuera del filtro `numf > 0` y nunca se intentaban matchear.
Ver task #25/#26.

Estos tests stubean db + asinfo_service + cli_aliases para no depender
de la DB real ni del servicio Metabase. Validamos:
    - Pase 2 caza un match cuando hay exactamente 1 AI libre con (cli,
      fecha exacta, importe ±0.01).
    - Pase 2 ignora AI ya asignados (numf_completo ya tomado por otra PC).
    - Pase 2 respeta el signo de kg (PC kg<0 no matchea AI kg>0).
    - Pase 2 con fecha ±1 día acepta drift de carga.
    - UniqueViolation al UPDATE se cuenta como uniq_conflict, no crashea.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import patch

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _FakeDB:
    """Stub db.* con respuestas pre-fabricadas para cada consulta."""

    def __init__(
        self,
        huerfanas: list[dict],
        ai_ya_usados: list[str] | None = None,
    ):
        self.huerfanas = list(huerfanas)
        self.ai_ya_usados = list(ai_ya_usados or [])
        self.updates: list[tuple] = []
        self._uniq_violation_for: set[int] = set()

    def fetch_all(self, sql, params=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.factura" in s and "numf_completo" in s and "stat is null" in s:
            # cargar_huerfanas_pc
            return self.huerfanas
        if "select distinct numf_completo" in s:
            # _ai_ya_asignados
            return [{"numf_completo": x} for x in self.ai_ya_usados]
        return []

    def fetch_one(self, sql, params=None):
        return None

    def execute(self, sql, params=None):
        # Detectar UPDATE de factura
        s = " ".join((sql or "").split()).lower()
        if "update scintela.factura" in s and "numf_completo" in s:
            numero, id_fact = params
            if id_fact in self._uniq_violation_for:
                import psycopg2
                raise psycopg2.errors.UniqueViolation(
                    f"duplicate key (numf_completo)={numero}"
                )
            self.updates.append((numero, id_fact))
        return 1

    def force_uniq_violation_for(self, *id_facturas):
        self._uniq_violation_for.update(id_facturas)


def _ai_row(numero, cli_asinfo, fecha, usd, kg, tipo="FACTURA"):
    return {
        "numero": numero,
        "cliente_codigo": cli_asinfo,
        "fecha": fecha,
        "usd": usd,
        "kg": kg,
        "tipo": tipo,
    }


def _pc_row(id_factura, fecha, cli, kg, importe, numf=0):
    return {
        "id_factura": id_factura,
        "numf": numf,
        "fecha": fecha,
        "codigo_cli": cli,
        "kg": kg,
        "importe": importe,
    }


@pytest.fixture
def stub(monkeypatch):
    """Stubs para db, asinfo_service y cli_aliases.

    `asinfo_service.facturas_periodo` y `cli_aliases.to_pc` se mockean
    desde dentro de cada test con monkeypatch para variar el dataset.
    """
    from scripts import backfill_numf_completo_from_asinfo as mod

    fake_db = _FakeDB(huerfanas=[])
    monkeypatch.setattr(mod, "db", fake_db)
    # cli_aliases.to_pc → identidad por default.
    monkeypatch.setattr(
        mod.cli_aliases, "to_pc", lambda c: (c or "").strip().upper()
    )
    return fake_db, mod


def test_pase2_matchea_numf_cero_por_fecha_e_importe(stub, monkeypatch):
    """Caso base: PC numf=0, AI tiene match perfecto (cli+fecha+imp)."""
    fake_db, mod = stub
    pc = _pc_row(4393, date(2026, 3, 13), "BED", 1200.50, 9054.78, numf=0)
    ai = _ai_row("NTEN-10152", "BED", date(2026, 3, 13), 9054.78, 1200.15, "NTEN")
    fake_db.huerfanas = [pc]
    monkeypatch.setattr(
        mod.asinfo_service, "facturas_periodo", lambda mn, mx: [ai]
    )

    stats = mod.backfill(dry_run=False)
    assert stats["pase1_matched"] == 0
    assert stats["pase2_matched"] == 1
    assert stats["updated"] == 1
    assert fake_db.updates == [("NTEN-10152", 4393)]


def test_pase2_ignora_ai_ya_asignado(stub, monkeypatch):
    """Si NTEN-10152 ya está como numf_completo en otra factura PC, no se
    debe re-asignar (es duplicado, requiere decisión humana)."""
    fake_db, mod = stub
    pc = _pc_row(4393, date(2026, 3, 13), "BED", 1200.50, 9054.78, numf=0)
    ai = _ai_row("NTEN-10152", "BED", date(2026, 3, 13), 9054.78, 1200.15, "NTEN")
    fake_db.huerfanas = [pc]
    fake_db.ai_ya_usados = ["NTEN-10152"]  # ya tomado
    monkeypatch.setattr(
        mod.asinfo_service, "facturas_periodo", lambda mn, mx: [ai]
    )

    stats = mod.backfill(dry_run=False)
    assert stats["pase2_matched"] == 0
    assert stats["no_match"] == 1
    assert fake_db.updates == []


def test_pase2_respeta_signo_kg(stub, monkeypatch):
    """PC kg negativo (devolución) NO debe matchear AI kg positivo aunque
    el importe coincida (sería el bug típico de #1066 TNZ)."""
    fake_db, mod = stub
    pc = _pc_row(1066, date(2026, 3, 23), "TNZ", -445.4, -3171.15, numf=0)
    ai = _ai_row(
        "NTEN-10428", "TNZ", date(2026, 3, 23), -3171.15, -445.4, "NTEN"
    )
    # AI positiva con mismo |kg| / |usd| — NO debe matchear PC negativa.
    ai_pos = _ai_row(
        "NTEN-99999", "TNZ", date(2026, 3, 23), 3171.15, 445.4, "FACTURA"
    )
    fake_db.huerfanas = [pc]
    monkeypatch.setattr(
        mod.asinfo_service, "facturas_periodo", lambda mn, mx: [ai, ai_pos]
    )

    stats = mod.backfill(dry_run=False)
    assert stats["pase2_matched"] == 1
    # Verifica que el match fue al negativo, no al positivo.
    assert fake_db.updates == [("NTEN-10428", 1066)]


def test_pase2_acepta_drift_un_dia(stub, monkeypatch):
    """Drift de carga de 1 día — PC fecha 13/03, AI fecha 14/03 → debe
    matchear. Drift > 1 día NO."""
    fake_db, mod = stub
    pc = _pc_row(5000, date(2026, 3, 13), "BED", 100.0, 1000.0, numf=0)
    ai_drift1 = _ai_row(
        "NTEN-11000", "BED", date(2026, 3, 14), 1000.0, 100.0, "FACTURA"
    )
    ai_drift3 = _ai_row(
        "NTEN-11001", "BED", date(2026, 3, 16), 1000.0, 100.0, "FACTURA"
    )
    fake_db.huerfanas = [pc]
    monkeypatch.setattr(
        mod.asinfo_service,
        "facturas_periodo",
        lambda mn, mx: [ai_drift1, ai_drift3],
    )

    stats = mod.backfill(dry_run=False)
    assert stats["pase2_matched"] == 1
    assert fake_db.updates == [("NTEN-11000", 5000)]


def test_pase2_match_unico_aun_cuando_otra_ai_lejana(stub, monkeypatch):
    """Si hay 2 AI candidatos pero uno tiene fecha exacta y el otro está a
    1 día, el de fecha exacta gana."""
    fake_db, mod = stub
    pc = _pc_row(5100, date(2026, 3, 13), "BED", 100.0, 1000.0, numf=0)
    ai_exact = _ai_row(
        "NTEN-22000", "BED", date(2026, 3, 13), 1000.0, 100.0
    )
    ai_d1 = _ai_row(
        "NTEN-22001", "BED", date(2026, 3, 14), 1000.0, 100.0
    )
    fake_db.huerfanas = [pc]
    monkeypatch.setattr(
        mod.asinfo_service,
        "facturas_periodo",
        lambda mn, mx: [ai_exact, ai_d1],
    )

    stats = mod.backfill(dry_run=False)
    assert stats["pase2_matched"] == 1
    assert fake_db.updates == [("NTEN-22000", 5100)]


def test_pase2_uniq_violation_no_crashea_y_cuenta(stub, monkeypatch):
    """Si el UPDATE choca con UNIQUE constraint (race entre snapshot y
    write), se cuenta como uniq_conflict pero el backfill no muere."""
    fake_db, mod = stub
    pc1 = _pc_row(6000, date(2026, 3, 13), "BED", 100.0, 1000.0, numf=0)
    pc2 = _pc_row(6001, date(2026, 3, 13), "EDU", 50.0, 500.0, numf=0)
    ai1 = _ai_row("NTEN-30000", "BED", date(2026, 3, 13), 1000.0, 100.0)
    ai2 = _ai_row("NTEN-30001", "EDU", date(2026, 3, 13), 500.0, 50.0)
    fake_db.huerfanas = [pc1, pc2]
    monkeypatch.setattr(
        mod.asinfo_service, "facturas_periodo", lambda mn, mx: [ai1, ai2]
    )
    # Forzar uniq violation en el UPDATE de la primera.
    fake_db.force_uniq_violation_for(6000)

    stats = mod.backfill(dry_run=False)
    # 2 matches detectados, 1 update OK, 1 uniq_conflict.
    assert stats["pase2_matched"] == 2
    assert stats["updated"] == 1
    assert stats["uniq_conflict"] == 1


def test_pase1_sigue_funcionando(stub, monkeypatch):
    """Pase 1 (numf>0, match por (numf, cli)) no debe romperse con la
    refactorización."""
    fake_db, mod = stub
    pc = _pc_row(7000, date(2026, 3, 13), "BED", 100.0, 1000.0, numf=11234)
    ai = _ai_row("NTEN-11234", "BED", date(2026, 3, 13), 1000.0, 100.0)
    fake_db.huerfanas = [pc]
    monkeypatch.setattr(
        mod.asinfo_service, "facturas_periodo", lambda mn, mx: [ai]
    )

    stats = mod.backfill(dry_run=False)
    assert stats["pase1_matched"] == 1
    assert stats["pase2_matched"] == 0
    assert stats["updated"] == 1


def test_backfill_dry_run_no_aplica_updates(stub, monkeypatch):
    """En dry_run, contamos matches pero no escribimos a DB."""
    fake_db, mod = stub
    pc = _pc_row(8000, date(2026, 3, 13), "BED", 100.0, 1000.0, numf=0)
    ai = _ai_row("NTEN-40000", "BED", date(2026, 3, 13), 1000.0, 100.0)
    fake_db.huerfanas = [pc]
    monkeypatch.setattr(
        mod.asinfo_service, "facturas_periodo", lambda mn, mx: [ai]
    )

    stats = mod.backfill(dry_run=True)
    assert stats["pase2_matched"] == 1
    assert stats["updated"] == 0
    assert fake_db.updates == []
