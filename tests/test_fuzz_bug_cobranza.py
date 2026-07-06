"""Fuzz del POST /cheques/nuevo paso=ejecutar — busca la excepcion generica."""
from __future__ import annotations

import random
import re
import sys

_REPO_ROOT = "/tmp/pc0706"
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.test_repro_bug_cobranza import Store, _login  # noqa: E402

CAPTURADAS = []


def _wire(monkeypatch, fake_db, facturas):
    import db
    import mov_doble
    import error_messages
    from modules.cheques import queries as cq

    store = Store(facturas, fake_db)
    for name in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, name, getattr(store, name))
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **k: None)
    monkeypatch.setattr(mov_doble, "registrar", lambda **k: None)

    _orig = error_messages.humanize

    def _spy(exc):
        CAPTURADAS.append(exc)
        return _orig(exc)

    monkeypatch.setattr(error_messages, "humanize", _spy)
    return store


def _mk_facturas(saldos):
    out = {}
    for i, s in enumerate(saldos, start=1):
        out[i] = {"id_factura": i, "numf": 170000 + i, "importe": float(s),
                  "abono": 0.0, "saldo": float(s), "stat": "Z"}
    return out


IMPORTES = ["1353.69", "881.73", "2726.73"]
TOTAL = 4962.15


def _post(client, aplicar, extra=None):
    data = {
        "paso": "ejecutar",
        "codigo_cli": "MSS",
        "no_cheque[]": ["", "", ""],
        "importe[]": IMPORTES,
        "fechad[]": ["", "", ""],
        "stat[]": ["B", "B", "B"],
        "doc_banco[]": ["36969717", "35993449", ""],
        "no_banco[]": ["90", "90", "90"],
    }
    for idf, monto in aplicar.items():
        data[f"aplicar[{idf}]"] = monto
    if extra:
        data.update(extra)
    return client.post("/cheques/nuevo", data=data, follow_redirects=False)


def _err_txt(resp):
    html = resp.get_data(as_text=True)
    m = re.search(r"Revis[^<]*estos datos[^<]*</p>\s*<ul[^>]*>(.*?)</ul>", html, re.S)
    if not m:
        return ""
    return " | ".join(
        re.sub(r"\s+", " ", t).strip()
        for t in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.S)
    )


def test_fuzz(client, fake_db, monkeypatch):
    rng = random.Random(42)
    _login(client, fake_db)
    fallos = []

    def run(tag, saldos, aplicar_vals=None, extra=None):
        CAPTURADAS.clear()
        facturas = _mk_facturas(saldos)
        store = _wire(monkeypatch, fake_db, facturas)
        aplicar = aplicar_vals or {i + 1: str(s) for i, s in enumerate(saldos)}
        resp = _post(client, aplicar, extra=extra)
        genericas = [e for e in CAPTURADAS if not isinstance(e, ValueError)]
        if resp.status_code != 302 or CAPTURADAS:
            fallos.append((tag, resp.status_code,
                           [f"{type(e).__name__}: {e}" for e in CAPTURADAS],
                           _err_txt(resp)[:220]))
        if store.unmatched:
            fallos.append((tag + " UNMATCHED", 0, store.unmatched[:3], ""))
        return genericas

    # 1) sumas desviadas del total por centavos y sub-centavos
    for delta in ("-0.02", "-0.01", "-0.004", "0.000", "0.004", "0.006",
                  "0.01", "0.02", "0.03", "0.04", "0.05", "0.99", "1.00", "1.01"):
        base = [1353.69, 881.73, 1000.00, 900.00, 500.00]
        resto = round(TOTAL - sum(base) + float(delta), 3)
        saldos = base + [resto]
        run(f"delta={delta}", [f"{s:.3f}".rstrip("0").rstrip(".") if isinstance(s, float) else s for s in saldos])

    # 2) con una NC en el medio (saldo negativo) que cierra la cuenta
    for delta in (0.0, 0.01, 0.03):
        saldos = [2000.00, 1500.00, 1500.00, round(TOTAL + delta - 5000.00 - (-37.85) , 2), -37.85]
        run(f"nc delta={delta}", [str(s) for s in saldos])

    # 3) factura grande partida entre los 3 cheques + centavos
    for delta in (0.0, 0.01, 0.02, 0.04):
        saldos = [str(round(TOTAL + delta - 300.0, 2)), "300.00"]
        run(f"split delta={delta}", saldos)

    # 4) montos editados a mano con 3 decimales (coma EU)
    run("3dec", ["1353.69", "881.735", "2726.725"],
        aplicar_vals={1: "1353.69", 2: "881,735", 3: "2726,725"})

    # 5) random sets
    for k in range(60):
        n = rng.randint(3, 9)
        cuts = sorted(rng.uniform(0.01, TOTAL - 0.01) for _ in range(n - 1))
        vals, prev = [], 0.0
        for c in cuts + [TOTAL]:
            vals.append(round(c - prev, 2))
            prev = c
        # corregir el ultimo para que la suma sea exacta +- delta aleatoria
        delta = rng.choice([0.0, 0.0, 0.01, -0.01, 0.02, 0.03])
        vals[-1] = round(TOTAL + delta - sum(vals[:-1]), 2)
        if any(v <= 0 for v in vals):
            continue
        run(f"rand{k} d={delta}", [f"{v:.2f}" for v in vals])

    # 6) sobrante -> anticipo (aplicado < cheques) con confirmacion
    saldos = ["1353.69", "881.73", "2000.00"]
    run("sobrante-anticipo", saldos,
        extra={"sobrante_modo": "anticipo", "confirmar_anticipo": "1"})
    run("sobrante-factura-unica", ["4000.00"],
        aplicar_vals={1: "4000.00"},
        extra={"sobrante_modo": "factura", "sobrante_a_factura": "1"})

    print("\n===== RESULTADOS NO-FELICES =====")
    for tag, st, caps, err in fallos:
        print(f"[{tag}] status={st} caps={caps} err={err!r}")
    print(f"total no-felices: {len(fallos)}")
    genericos = [f for f in fallos if any("ValueError" not in c for c in (f[2] or []) if isinstance(c, str))]
    print(f"con excepcion NO-ValueError (mensaje generico): {len(genericos)}")
    for g in genericos:
        print("GENERICO:", g)
