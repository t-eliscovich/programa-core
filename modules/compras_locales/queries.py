"""Compras LOCALES de hilo — el mapeo de proveedores Asinfo ↔ Programa Core.

Acá vivió el TARIFARIO $/kg (`scintela.hilo_local_tarifa`, TMT 2026-07-30)
hasta el 17/09/2026. Se retiró porque estaba MAL: una tarifa única por
proveedor (2,645 para HY) contra hilos que valen entre 2,30 y 3,80 $/kg dejaba
las deudas con HY $ 18.772 abajo de las facturas reales. Tamara: *"para
adelante sea sólo de Asinfo el total de la factura y no más tarifas"*. La
tabla queda en la base sin lectores (borrarla es una migración aparte).
"""
import db


def proveedores_por_ruc() -> dict[str, str]:
    """{RUC → codigo_prov} de scintela.proveedor.

    El mapeo Asinfo↔PC de los proveedores LOCALES sale del RUC, que las dos
    puntas ya tienen (Asinfo lo guarda en `empresa.codigo`): HY = Hiltexpoy =
    1791436210001, EP = El Peral = 1890153654001. **Sin tabla de alias tipeada
    a mano** — misma lección que los aliases de cliente (30/07): si hay un dato
    real que identifica, se usa ese.

    Los proveedores de importación (AC, AI, MH, KX…) tienen el RUC vacío y en
    Asinfo el código es la sigla, así que no entran acá y no molestan.
    """
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(codigo_prov)) AS cod, TRIM(COALESCE(ruc, '')) AS ruc
              FROM scintela.proveedor
             WHERE COALESCE(TRIM(ruc), '') <> ''
            """,
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    out: dict[str, str] = {}
    for r in rows:
        ruc = (r.get("ruc") or "").strip()
        cod = (r.get("cod") or "").strip()
        if ruc and cod:
            out[ruc] = cod
    return out
