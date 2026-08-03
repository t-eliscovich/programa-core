"""La ✕ sobre un pendiente histórico deja de ser irreversible. TMT 2026-08-03.

Borrar un pendiente del backlog (`banco_eliminar_pendiente`) es un `DELETE`:
la fila se va y no vuelve. "↩ Restaurar borradas" recupera SÓLO filas del
extracto de la sesión, no históricos.

Y borrar uno **baja el "Saldo banco esperado" por su importe exacto**, o sea
mueve la diferencia de la conciliación. Era la única acción de la app que toca
plata y no se podía deshacer.

El rastro que había no alcanzaba: `banco_saldo_conc_snapshot` guarda el **id**
del pendiente y el saldo resultante, pero **ni el importe ni el concepto** — y
la fila ya no existe para ir a buscarlos. El día que la dueña borró 6 filas de
basura ($8.304.132,19 de resumen contable cargado como pendientes) hubo que
deducir qué se había sacado de los Excel que ella tenía en la compu.

⚠ Por qué papelera y NO soft-delete: `banco_historicos_pendientes` se lee
desde **18 SELECT en 8 archivos**. Marcar la fila como borrada obliga a
filtrar en las 18; si se escapa una, la fila vuelve a la suma de pendientes y
la conciliación se mueve. Una tabla aparte tiene cero superficie.
"""
from __future__ import annotations

import inspect
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakePapeleraDB:
    """Fake mínimo: papelera + backlog en memoria."""

    def __init__(self, papelera=None, backlog_firmas=None):
        self.papelera = list(papelera or [])
        self.backlog_firmas = set(backlog_firmas or [])
        self.next_id = max((f["id"] for f in self.papelera), default=0) + 1
        self.ejecutados: list[str] = []

    # --- API de db ---
    def execute(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        self.ejecutados.append(s)
        if "insert into scintela.banco_historicos_pendientes" in s:
            firma = (params[1], params[3], float(params[4]))  # fecha, doc, monto
            if firma in self.backlog_firmas:
                return 0                      # ON CONFLICT DO NOTHING
            self.backlog_firmas.add(firma)
            return 1
        if "update scintela.banco_pendientes_papelera" in s:
            for f in self.papelera:
                if f["id"] == params[1]:
                    f["restaurado_en"] = "2026-08-03 12:00"
                    f["restaurado_por"] = params[0]
            return 1
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        self.ejecutados.append(s)
        if "insert into scintela.banco_pendientes_papelera" in s:
            fid = self.next_id
            self.next_id += 1
            self.papelera.append({"id": fid, **(params or {})})
            return {"id": fid}
        return None

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.banco_pendientes_papelera" in s:
            for f in self.papelera:
                if f["id"] == params[0]:
                    return dict(f)
            return None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return [dict(f) for f in self.papelera]

    def tx(self):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield object()
        return _cm()

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "tx", self.tx)


FILA = {
    "id": 4321, "fecha": date(2026, 7, 31), "concepto": "DEPOSITO CHEQUE",
    "documento": "34535095", "monto": 3000.00, "tipo": "C",
    "oficina": "AG. NORTE", "detalle": "", "codigo": "001045",
    "fuente": "xlsx:FEB2023:88", "creado_en": "2026-07-31 10:00",
}


def _mod(monkeypatch, fake):
    import db as db_mod

    from modules.conciliacion import papelera_pendientes as pap
    fake.apply_to(monkeypatch, db_mod)
    pap._bootstrapped = True          # no correr el CREATE TABLE en el fake
    return pap


def test_guardar_copia_la_fila_ENTERA(monkeypatch):
    """El importe y el concepto son justo lo que el rastro viejo NO guardaba."""
    fake = _FakePapeleraDB()
    pap = _mod(monkeypatch, fake)

    fid = pap.guardar(FILA, no_banco=10, usuario="tamara", sesion_id=60)
    assert fid
    g = fake.papelera[0]
    assert g["monto"] == 3000.00
    assert g["concepto"] == "DEPOSITO CHEQUE"
    assert g["documento"] == "34535095"
    assert g["hist_id"] == 4321
    assert g["usuario"] == "tamara"
    assert g["sesion_id"] == 60


def test_guardar_NO_puede_romper_el_borrado(monkeypatch):
    """Si la papelera falla, la ✕ tiene que seguir funcionando."""
    import db as db_mod

    from modules.conciliacion import papelera_pendientes as pap
    pap._bootstrapped = True

    def _explota(*a, **k):
        raise RuntimeError("papelera caída")

    monkeypatch.setattr(db_mod, "execute_returning", _explota)
    assert pap.guardar(FILA, no_banco=10, usuario="tamara") is None


def test_restaurar_devuelve_la_fila_al_backlog(monkeypatch):
    fake = _FakePapeleraDB(papelera=[{
        "id": 1, "hist_id": 4321, "no_banco": 10, "fecha": date(2026, 7, 31),
        "concepto": "DEPOSITO CHEQUE", "documento": "34535095",
        "monto": 3000.00, "tipo": "C", "oficina": "AG. NORTE",
        "detalle": "", "codigo": "001045", "restaurado_en": None,
    }])
    pap = _mod(monkeypatch, fake)

    r = pap.restaurar(1, no_banco=10, usuario="tamara")
    assert r["ok"] is True
    assert (date(2026, 7, 31), "34535095", 3000.00) in fake.backlog_firmas
    assert fake.papelera[0]["restaurado_en"]


def test_restaurar_dos_veces_no_duplica(monkeypatch):
    """`ux_bhp_firma` lo frena; la pantalla tiene que decir por qué."""
    fake = _FakePapeleraDB(
        papelera=[{
            "id": 1, "hist_id": 4321, "no_banco": 10, "fecha": date(2026, 7, 31),
            "concepto": "DEPOSITO CHEQUE", "documento": "34535095",
            "monto": 3000.00, "tipo": "C", "restaurado_en": None,
        }],
        backlog_firmas={(date(2026, 7, 31), "34535095", 3000.00)},
    )
    pap = _mod(monkeypatch, fake)

    r = pap.restaurar(1, no_banco=10, usuario="tamara")
    assert r["ok"] is False
    assert "ya estaba en el backlog" in r["motivo"]


def test_restaurar_lo_ya_restaurado_avisa(monkeypatch):
    fake = _FakePapeleraDB(papelera=[{
        "id": 1, "no_banco": 10, "monto": 3000.00, "concepto": "X",
        "restaurado_en": "2026-08-03 11:00",
    }])
    pap = _mod(monkeypatch, fake)
    r = pap.restaurar(1, no_banco=10, usuario="tamara")
    assert r["ok"] is False and "ya se había restaurado" in r["motivo"]


# --- el invariante: no se borra sin copiar antes -------------------------


def test_el_borrado_copia_a_la_papelera_ANTES_del_delete():
    """Invariante de estructura: si esto se rompe, volvemos al borrado ciego."""
    from modules.conciliacion.banco_v2_view import banco_eliminar_pendiente

    src = inspect.getsource(banco_eliminar_pendiente)
    assert "papelera_pendientes" in src, (
        "la ✕ volvió a borrar un histórico sin dejar copia"
    )
    i_guardar = src.index("_pap.guardar(")
    i_delete = src.index("DELETE FROM scintela.banco_historicos_pendientes")
    assert i_guardar < i_delete, (
        "la copia a la papelera tiene que ir ANTES del DELETE"
    )


def test_el_select_previo_trae_los_campos_que_la_papelera_necesita():
    """Sin importe/concepto/fecha la papelera no sirve de nada."""
    from modules.conciliacion.banco_v2_view import banco_eliminar_pendiente

    src = inspect.getsource(banco_eliminar_pendiente)
    bloque = src[:src.index("_pap.guardar(")]
    for campo in ("fecha", "concepto", "documento", "monto", "tipo"):
        assert campo in bloque, f"el SELECT previo al borrado no trae `{campo}`"


def test_la_papelera_no_toca_ninguna_query_de_pendientes():
    """El motivo de que sea tabla aparte y no un soft-delete.

    `banco_historicos_pendientes` se lee desde 18 lugares. Si el módulo de la
    papelera empezara a filtrar o a escribir ahí fuera del restaurar, esa
    superficie volvería.
    """
    from modules.conciliacion import papelera_pendientes as pap

    import re

    src = inspect.getsource(pap)
    # Sólo SQL de verdad: FROM / INTO / UPDATE / JOIN sobre la tabla.
    sentencias = re.findall(
        r"(?:FROM|INTO|UPDATE|JOIN)\s+scintela\.banco_historicos_pendientes",
        src, re.IGNORECASE,
    )
    assert len(sentencias) == 1, (
        f"la papelera toca `banco_historicos_pendientes` en {len(sentencias)} "
        f"sentencias SQL ({sentencias}); sólo puede hacerlo en el INSERT de "
        f"`restaurar` — si empieza a leer o a filtrar ahí, vuelve la "
        f"superficie de 18 queries que justamente evitamos"
    )
    assert "INSERT INTO scintela.banco_historicos_pendientes" in src
    assert "DELETE FROM scintela.banco_historicos_pendientes" not in src
    assert "UPDATE scintela.banco_historicos_pendientes" not in src
