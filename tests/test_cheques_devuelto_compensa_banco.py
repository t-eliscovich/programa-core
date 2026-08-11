"""Un cheque DEPOSITADO que se marca devuelto (1/2/3) debe descontar su
importe del banco con una nota de débito (ND), sino queda contado doble
(banco + cartera viva). TMT 2026-07-23 (dueña: "la devolución no aparece en
banco"). Ver queries.compensar_deposito_devuelto.
"""

import contextlib
from datetime import date


class _Rec:
    """Stub de db.* para el helper: simula el link a un depósito 'DE' y captura
    las NDs insertadas y los DELETE de link.

    Idempotencia (2026-07-26): el helper ya NO pregunta "¿existe alguna ND?"
    (booleano) sino "¿la ND más nueva es POSTERIOR al 'DE' vivo?" (compara
    id_transaccion). El stub modela eso: `nd_id` es el id de la ND más reciente
    para el cheque (None si no hay). El 'DE' vivo tiene id_transaccion=777, así
    que una ND con nd_id>777 = ya compensado (no-op); nd_id<777 o None = hay que
    compensar.
    """

    _DE_ID = 777

    def __init__(self, *, tiene_link: bool, ya_nd: bool, nd_id: int | None = None):
        self.tiene_link = tiene_link
        # Compat: ya_nd=True sin nd_id explícito ⇒ ND posterior al 'DE' (doble
        # click / ND manual ya asentada) ⇒ no-op.
        self.nd_id = nd_id if nd_id is not None else (self._DE_ID + 23 if ya_nd else None)
        self.executes: list[tuple[str, tuple]] = []
        self.nds: list[dict] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "numreferencia = %s" in s and "'nd'" in s:
            return {"m": self.nd_id}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "chequextransaccion cxt" in s and "'de'" in s:
            return [{"id_transaccion": self._DE_ID, "no_banco": 10}] if self.tiene_link else []
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _apply(monkeypatch, rec):
    import db

    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "fetch_all", rec.fetch_all)
    monkeypatch.setattr(db, "execute", rec.execute)

    import bank_helpers

    def _fake_insert(conn, **kw):
        rec.nds.append(kw)
        return {"id_transaccion": 12345, "saldo_nuevo": 0.0}

    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario", _fake_insert)


def _tiene_delete_link(executes):
    return any(
        "delete from scintela.chequextransaccion" in " ".join(sql.split()).lower()
        for sql, _ in executes
    )


def _nd_del_cheque(rec):
    """La ND que compensa el depósito, sin contar la del gasto bancario.

    TMT 2026-08-11: desde que Programa Core emite también el `GS. cheq.` del
    protesto (paridad `MODIFICA.PRG` L314-318), el banco recibe DOS 'ND' por
    un cheque devuelto: la del importe y la de la comisión. Estos tests
    contaban "las ND" como proxy de "la compensación", así que hay que
    separarlas — si no, el test no distingue una compensación duplicada (el
    bug que protege) de la comisión legítima.
    """
    from modules.cheques.queries import CONCEPTO_GS_PROTESTO

    return [
        n for n in rec.nds
        if not str(n.get("concepto", "")).startswith(CONCEPTO_GS_PROTESTO)
    ]


def _gs_del_protesto(rec):
    from modules.cheques.queries import CONCEPTO_GS_PROTESTO

    return [
        n for n in rec.nds
        if str(n.get("concepto", "")).startswith(CONCEPTO_GS_PROTESTO)
    ]


def test_compensa_genera_nd_y_desagrupa(monkeypatch):
    """Con depósito 'DE' vivo y sin ND previa → inserta ND por el importe y
    borra el link al depósito."""
    from modules.cheques import queries

    rec = _Rec(tiene_link=True, ya_nd=False)
    _apply(monkeypatch, rec)

    monto = queries.compensar_deposito_devuelto(
        object(),
        id_cheque=100688,
        importe=21508.62,
        codigo_cli="BYG",
        no_cheque="14778",
        fecha=date(2026, 7, 23),
        usuario="tmt",
    )

    assert monto == 21508.62
    assert len(_nd_del_cheque(rec)) == 1, "una sola compensación del depósito"
    assert len(_gs_del_protesto(rec)) == 1, "y su comisión bancaria"
    nd = _nd_del_cheque(rec)[0]
    assert nd["documento"] == "ND"
    assert nd["importe"] == 21508.62
    assert nd["no_banco"] == 10
    assert nd["numreferencia"] == 100688
    assert _tiene_delete_link(rec.executes)


def test_compensa_idempotente_si_ya_hay_nd(monkeypatch):
    """Si ya existe una ND posterior al depósito → NO inserta otra (evita el
    doble descuento). Igual desagrupa: ver el test de abajo."""
    from modules.cheques import queries

    rec = _Rec(tiene_link=True, ya_nd=True)
    _apply(monkeypatch, rec)

    monto = queries.compensar_deposito_devuelto(
        object(), id_cheque=100688, importe=21508.62, codigo_cli="BYG",
        no_cheque="14778", fecha=date(2026, 7, 23), usuario="tmt",
    )

    assert monto == 0.0
    assert rec.nds == []


def test_compensa_desagrupa_aunque_la_nd_ya_estuviera(monkeypatch):
    """TMT 2026-08-03 (bug A). Cuando la ND del protesto ya estaba cargada a
    mano, el helper salía SIN borrar el link. Un link vivo significa "este
    cheque sigue adentro de ese depósito": el cheque quedaba en stat '1'
    (re-depositable, STATS_DEPOSITABLES) y al re-depositarlo terminaba colgando
    de DOS 'DE' a la vez. Después "Volver a cartera" le resta el importe a CADA
    'DE' del que cuelga (`deshacer_deposito_cheque`, `for lk in links`) → el
    banco perdía el importe del cheque una vez de más.

    Escenario: lote A+B+C de 50 (DE=150) · ND de A tipeada en /bancos (−50) ·
    A → devuelto (no duplica la ND) · A se re-deposita (DE#2 = 50) · volver a
    cartera A → el libro terminaba en 50 contra 100 de banco real.
    """
    from modules.cheques import queries

    rec = _Rec(tiene_link=True, ya_nd=True)
    _apply(monkeypatch, rec)

    queries.compensar_deposito_devuelto(
        object(), id_cheque=100688, importe=21508.62, codigo_cli="BYG",
        no_cheque="14778", fecha=date(2026, 7, 23), usuario="tmt",
    )

    assert _tiene_delete_link(rec.executes), (
        "salió sin desagrupar: el cheque queda dentro de un depósito ya "
        "compensado y puede terminar colgando de dos 'DE'"
    )


def test_compensa_segundo_rebote_con_nd_vieja(monkeypatch):
    """2° rebote: hay un 'DE' vivo NUEVO (re-depósito) y una ND VIEJA del rebote
    anterior (nd_id < id del 'DE'). Debe compensar OTRA vez (ND #2) — la ND vieja
    NO puede bloquearlo. Este es el caso del cheque de 21k contado doble."""
    from modules.cheques import queries

    rec = _Rec(tiene_link=True, ya_nd=True, nd_id=_Rec._DE_ID - 100)  # ND anterior al DE
    _apply(monkeypatch, rec)

    monto = queries.compensar_deposito_devuelto(
        object(), id_cheque=100688, importe=21508.62, codigo_cli="BYG",
        no_cheque="14778", fecha=date(2026, 7, 23), usuario="tmt",
    )

    assert monto == 21508.62
    assert len(_nd_del_cheque(rec)) == 1  # se crea la ND #2
    assert len(_gs_del_protesto(rec)) == 1  # con su comisión
    assert _tiene_delete_link(rec.executes)


def test_compensa_noop_sin_deposito(monkeypatch):
    """Cheque que nunca se depositó (sin link 'DE' vivo) → no toca el banco."""
    from modules.cheques import queries

    rec = _Rec(tiene_link=False, ya_nd=False)
    _apply(monkeypatch, rec)

    monto = queries.compensar_deposito_devuelto(
        object(), id_cheque=999, importe=500.0, codigo_cli="ZZZ",
        no_cheque="1", fecha=date(2026, 7, 23), usuario="tmt",
    )

    assert monto == 0.0
    assert rec.nds == []
    assert not _tiene_delete_link(rec.executes)


def test_no_duplica_la_nd_tipeada_a_mano(monkeypatch):
    """TMT 2026-07-30 (dueña, caso CJE 99698). El guard miraba SÓLO
    `numreferencia = id_cheque`, y una ND tipeada a mano en /bancos nunca la
    lleva (`crear_movimiento_simple` no la setea). La ND del protesto ya estaba
    cargada —y conciliada contra el extracto— y marcar el cheque devuelto
    metía una SEGUNDA: Pichincha −3.384,32 por un solo rebote.

    Este test clava que el match por (banco + cliente + importe) también
    cuenta: la query tiene que preguntar por `prov` e `importe`, no sólo por
    numreferencia.
    """
    import inspect

    from modules.cheques import queries as q
    src = inspect.getsource(q.compensar_deposito_devuelto)
    i = src.find("nd_post = db.fetch_one")
    assert i > 0, "cambió el guard — revisar este test"
    bloque = src[i:i + 900]
    assert "numreferencia = %s" in bloque, "el match por referencia se mantiene"
    assert "prov" in bloque and "no_banco = %s" in bloque, (
        "el guard volvió a mirar sólo numreferencia: una ND cargada a mano se "
        "vuelve a duplicar"
    )
    # La ND real del caso CJE se cargó con prov='PROT' y el cliente sólo
    # aparece en el concepto ("ND ch. prot. CJE 1") — sin este OR, el guard
    # no la ve y la duplica igual (fue exactamente lo que pasó el 30/07).
    assert "concepto" in bloque.lower() and "LIKE %s" in bloque, (
        "el cliente puede venir en el concepto y no en prov"
    )
    assert "ABS(COALESCE(importe, 0) - %s) <= 0.01" in bloque, (
        "sin el match por importe exacto, cualquier ND del cliente cancelaría "
        "la compensación"
    )
