def rep(p, old, nue):
    s = open(p, encoding="utf-8").read()
    assert old in s, ("NO ESTA: " + old[:80])
    assert s.count(old) == 1, ("AMBIGUO: " + old[:80])
    open(p, "w", encoding="utf-8").write(s.replace(old, nue, 1))

CQ = "modules/cheques/queries.py"

rep(CQ, '''        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        stat_prev = (ch.get("stat") or "").upper()''',
'''        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        # Antes de tocar nada: si un TOTALIZAR se llevó los vínculos, esta
        # anulación compensaría el banco y dejaría el abono puesto.
        _freno_si_el_totalizar_se_llevo_los_vinculos(id_cheque, conn=conn)
        stat_prev = (ch.get("stat") or "").upper()''')

rep(CQ, '''        stat_nuevo, es_rebote_real = _stat_destino_reversa(stat_prev)''',
'''        stat_nuevo, es_rebote_real = _stat_destino_reversa(stat_prev)
        # Sólo la anulación administrativa revierte las facturas; el rebote
        # real no las toca, así que el vínculo borrado no le cambia nada.
        if not es_rebote_real:
            _freno_si_el_totalizar_se_llevo_los_vinculos(id_cheque, conn=conn)''')
print("ok")
