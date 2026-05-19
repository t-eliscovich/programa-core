"""Sanity tests over the canonical role map.

These guard against silly mistakes like duplicate permisos, empty roles,
or accidentally giving a non-owner the `*` wildcard.
"""
from config.roles import ROLES


def test_roles_are_non_empty_and_unique():
    nombres = [r[0] for r in ROLES]
    assert len(nombres) == len(set(nombres)), "nombres de rol duplicados"
    # TMT 2026-05-19 v8 — "Dueño" renombrado a "Accionista" (pedido dueña).
    assert "Accionista" in nombres


def test_only_accionista_has_wildcard():
    for nombre, permisos in ROLES:
        has_wild = "*" in permisos
        if nombre == "Accionista":
            assert has_wild, "Accionista debe tener '*'"
        else:
            assert not has_wild, f"El rol {nombre!r} no debe tener '*'"


def test_permiso_format_is_dotted():
    for nombre, permisos in ROLES:
        for p in permisos:
            if p == "*":
                continue
            assert "." in p and p == p.lower(), (
                f"permiso {p!r} en {nombre!r} debe ser 'modulo.accion' en minúsculas"
            )


def test_no_duplicate_permisos_per_role():
    for nombre, permisos in ROLES:
        assert len(permisos) == len(set(permisos)), f"permisos duplicados en {nombre!r}"


def test_lectura_is_view_only():
    permisos = dict(ROLES).get("Lectura")
    assert permisos is not None
    for p in permisos:
        assert p.endswith(".ver"), f"Lectura debería ser solo-lectura, encontré {p!r}"
