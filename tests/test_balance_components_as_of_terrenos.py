"""`balance_components_as_of` (usado por `informe_balance_as_of`, la rama
que corre cuando un cierre se toma tarde) calculaba `uact` (realty) sumando
sólo tipo='I' (edificios), sin 'T' (terrenos) -- la rama LIVE equivalente
(`UACT FOR TIPO IN ('I','T')`, ~línea 681) sí los incluye desde 2026-XX-XX.

Tamara 2026-09-02, mientras se investigaba el incidente de agosto: no fue
lo que rompió agosto (esa vez no se llegó a usar esta rama para
maquinaria/realty), pero es un bug real e independiente -- sin 'T', esta
rama infravalora realty por el total de terrenos ($1.465.000 verificado
09/2026) cada vez que se usa para reconstruir un mes que ya pasó.

Sin Postgres en CI para este archivo: se verifica inspeccionando el
fuente, mismo patrón que el resto de la suite para SQL embebido.
"""
import inspect

from modules.informes import queries


def test_uact_incluye_terrenos_ademas_de_edificios():
    fuente = inspect.getsource(queries.balance_components_as_of)
    assert "tipo IN ('I','T')" in fuente, (
        "uact tiene que sumar tipo IN ('I','T') (edificios + terrenos), "
        "igual que la rama LIVE -- si sólo suma 'I' volvió el bug."
    )
    assert "tipo = 'I' " not in fuente and "tipo = 'I'\n" not in fuente, (
        "quedó un SELECT viejo sumando sólo tipo='I' sin terrenos"
    )
