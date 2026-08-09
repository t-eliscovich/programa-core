"""La lista de rutas ACEPTADAS no se puede podrir.

TMT 2026-08-09: el bloque rojo de `/usuarios/accesos` tenía 26 rutas y la
mitad eran mentira. Se limpiaron y las que quedaron se revisaron a mano: unas
son redirects, otras están abiertas a propósito, y siete son escrituras que la
dueña decidió dejar así ("todas las cuentas son de confianza"). Todas viven en
`accesos.ACEPTADAS` con su motivo, y por eso dejan de salir en rojo.

⚠ El peligro de una lista de excepciones es que sobreviva a lo que excusaba.
Si mañana alguien borra `/cheques/<id>/deshacer-deposito` y en otro módulo
nace una ruta con el mismo path, la excepción vieja la tapa sola y en
silencio. Estos tests son el freno:

  · toda ruta aceptada tiene que EXISTIR todavía;
  · y tiene que seguir sin candado — el día que alguien le ponga
    `@requiere_permiso`, la excepción sobra y hay que borrarla.

El segundo es el que evita que la lista crezca para siempre.
"""
from __future__ import annotations

import pytest

from modules.usuarios import accesos


def _rutas_sin_candado(app) -> set[str]:
    """Las que hoy no llevan `@requiere_permiso` y sí son pantallas."""
    return {
        str(r.rule)
        for r in app.url_map.iter_rules()
        if accesos._es_pantalla(r)
        and not getattr(app.view_functions.get(r.endpoint), "_permiso", None)
    }


@pytest.mark.parametrize("ruta", sorted(accesos.ACEPTADAS))
def test_la_ruta_aceptada_todavia_existe(app, ruta):
    rutas = {str(r.rule) for r in app.url_map.iter_rules()}
    assert ruta in rutas, (
        f"{ruta} está en ACEPTADAS pero ya no existe. Borrala de la lista: una "
        f"excepción huérfana tapa en silencio a la próxima ruta que se llame igual."
    )


@pytest.mark.parametrize("ruta", sorted(accesos.ACEPTADAS))
def test_la_ruta_aceptada_sigue_sin_candado(app, ruta):
    assert ruta in _rutas_sin_candado(app), (
        f"{ruta} ya tiene permiso: sacala de ACEPTADAS. La lista es de lo que "
        f"se decidió DEJAR abierto, no un registro histórico."
    )


def test_ninguna_aceptada_sobra(app):
    """Cada línea de ACEPTADAS tiene que estar haciendo algo.

    Si una ruta deja de detectarse como "sin candado" —porque le pusieron un
    decorador, o porque el detector aprendió a ver su chequeo— la línea queda
    de adorno y encima cuenta una historia falsa. Pasó el mismo día que se
    escribió la lista: `/informes/factura/cambio-stat/.../deshacer` entró como
    "escritura sin candado" y el detector nuevo mostró que sí chequea.
    """
    m = accesos.mapa(app)
    en_pantalla = {a["ruta"] for a in m["aceptadas"]}
    sobran = set(accesos.ACEPTADAS) - en_pantalla
    assert not sobran, f"sobran en ACEPTADAS (ya no hacen falta): {sorted(sobran)}"


def test_las_aceptadas_no_aparecen_en_rojo(app):
    m = accesos.mapa(app)
    assert not (set(m["sin_permiso"]) & set(accesos.ACEPTADAS))


def test_iniciales_no_se_acusa_de_no_chequear(app):
    """`/iniciales` chequea con un helper, no con un `if` adentro.

    Es el caso que motivó mirar un nivel de indirección: el detector viejo
    premiaba copiar y pegar `tiene_permiso` en cada vista, y acusaba de agujero
    a la que factorizó el chequeo en `_puede_ver_iniciales()`.
    """
    m = accesos.mapa(app)
    assert "/iniciales" not in m["sin_permiso"]
    assert any(c["ruta"] == "/iniciales" for c in m["control_propio"])


def test_un_redirect_puro_no_cuenta_como_agujero(app):
    """`/anticipos/` rebota a `/dolares`, que sí tiene candado."""
    m = accesos.mapa(app)
    assert "/anticipos/" not in m["sin_permiso"]
    assert any(a["ruta"] == "/anticipos/" for a in m["aceptadas"])


@pytest.mark.parametrize(
    "ruta,clase",
    [
        ("/cheques/_api/anticipos-vivos/<codigo_cli>", "api"),
        ("/costos-ot/cliente/<codigo_cli>/fragment", "api"),
        ("/admin/debug-yy/", "diagnostico"),
        ("/admin/health/all", "diagnostico"),
        ("/comisiones/<codigo>/excel", "descarga"),
        ("/informes/flujo/grafico/export.xlsx", "descarga"),
        ("/cheques", None),
        ("/informes/balance", None),
        ("/compras/<int:id_compra>", None),
        ("/cheques/resumen-dia", None),
    ],
)
def test_clase_tecnica(ruta, clase):
    assert accesos.clase_tecnica(ruta) == clase


def test_las_tecnicas_no_se_pierden(app):
    """Ocultarlas está bien; perderlas, no: rutas + tecnicas = todo."""
    m = accesos.mapa(app)
    de_la_tabla = {
        r
        for g in m["grupos"]
        for p in g["permisos"]
        for r in (*p["rutas"], *p["tecnicas"])
    }
    con_permiso = {
        str(r.rule)
        for r in app.url_map.iter_rules()
        if accesos._es_pantalla(r)
        and getattr(app.view_functions.get(r.endpoint), "_permiso", None)
    }
    assert de_la_tabla == con_permiso


# ---------------------------------------------------------------------------
# Re-encadenar y Apertura: sólo Accionista y Administrador (dueña 2026-08-09)
# ---------------------------------------------------------------------------
# *"reencadenar solo accionistas"*. `/bancos/apertura` cuelga del mismo
# permiso; preguntada, eligió cerrarla también — y es la más peligrosa de las
# dos: el saldo del banco es `apertura + movimientos`, así que corregir una
# apertura mueve el patrimonio sin que entre ni salga plata.


def test_ningun_rol_conserva_bancos_editar():
    from config.roles import ROLES

    for nombre, permisos in ROLES:
        if "*" in permisos:
            continue
        assert "bancos.editar" not in permisos, f"{nombre} todavía lo tiene"


def test_hay_migracion_0182_para_la_base():
    """`config/roles.py` no manda en runtime: los permisos vivos están en
    `seguridad.permiso`. Sin la migración el cambio existe sólo en el repo y
    producción queda idéntica, con los tests en verde (migs 0164/0165)."""
    from pathlib import Path

    mig = Path("migrations/0182_bancos_editar_solo_duenos.py").read_text()
    assert "DELETE FROM seguridad.permiso" in mig
    assert "bancos.editar" in mig


def test_las_dos_pantallas_siguen_pidiendo_el_permiso(app):
    """El permiso se cierra por la RUTA, no por el link.

    Si mañana alguien le saca el decorador a una de las dos, esconder el botón
    no alcanza: la URL a mano sigue entrando.
    """
    for endpoint in ("bancos.reencadenar_saldos", "bancos.apertura_bancos"):
        vista = app.view_functions[endpoint]
        assert getattr(vista, "_permiso", None) == "bancos.editar", endpoint


def test_los_botones_de_bancos_van_detras_del_permiso():
    """Y el link también, porque un botón que da 404 es peor que no tenerlo.

    A INT le desaparecen los dos botones en vez de recibir un 404 en la cara.
    """
    from pathlib import Path

    html = Path("modules/bancos/templates/bancos/lista.html").read_text()
    i = html.index("tiene_permiso('bancos.editar')")
    j = html.index("{% endif %}", i)
    bloque = html[i:j]
    assert "bancos.reencadenar_saldos" in bloque
    assert "bancos.apertura_bancos" in bloque
