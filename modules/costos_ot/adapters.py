"""Adaptadores intercambiables para costos de órdenes de tintura (OT).

Cada adaptador implementa el mismo contrato (`CostosOTAdapter`) y se elige en
tiempo de arranque según `COSTOS_OT_ADAPTER` en .env. El código del resto del
app NUNCA importa un adaptador concreto — siempre pasa por
`modules.costos_ot.service` que resuelve el adaptador activo.

Contrato:
    costos_por_cliente(codigo_cli: str) -> list[OTCosto]
    costos_por_factura(id_factura: int) -> list[OTCosto]
    disponible() -> bool

Todos los métodos tienen que ser SEGUROS: si el backend está caído, devolver
lista vacía + log a WARNING, nunca levantar. El panel de costos en el detalle
de factura NO puede romper el detalle.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Protocol

_LOG = logging.getLogger("programa_core.costos_ot")


# ---------------------------------------------------------------------------
# Contrato
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OTCosto:
    """Costo acumulado de una orden de tintura cerrada.

    Inmutable — los adaptadores devuelven snapshots. Si la OT re-abre y se
    vuelve a cerrar, se pide de nuevo.
    """

    n_orden: str
    fecha_cierre: date | None
    cliente_codigo: str
    descripcion: str
    kg: float
    costo_total: float
    costo_kg: float
    fuente: str  # "fake" | "metabase" | "postgres" — útil para debug en UI

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["fecha_cierre"]:
            d["fecha_cierre"] = d["fecha_cierre"].isoformat()
        return d


class CostosOTAdapter(Protocol):
    def costos_por_cliente(self, codigo_cli: str) -> list[OTCosto]: ...

    def costos_por_factura(self, id_factura: int) -> list[OTCosto]: ...

    def disponible(self) -> bool: ...


# ---------------------------------------------------------------------------
# FakeAdapter — datos sintéticos realistas
# ---------------------------------------------------------------------------

# Datos semilla — clientes conocidos del factory con OTs plausibles.
# Cuando se cambie al adapter real, esto queda como fixture de tests.
_FAKE_OTS: dict[str, list[dict]] = {
    "JTX": [
        {
            "n_orden": "24089",
            "fecha_cierre": date.today() - timedelta(days=3),
            "descripcion": "Jersey 30/1 · Azul marino",
            "kg": 185.5,
            "costo_kg": 1.78,
        },
        {
            "n_orden": "24112",
            "fecha_cierre": date.today() - timedelta(days=11),
            "descripcion": "Piqué 24/1 · Rojo Coral",
            "kg": 92.3,
            "costo_kg": 1.65,
        },
    ],
    "TEX": [
        {
            "n_orden": "24095",
            "fecha_cierre": date.today() - timedelta(days=7),
            "descripcion": "Ribb 1x1 24/1 · Negro",
            "kg": 320.0,
            "costo_kg": 1.92,
        },
    ],
    "MOD": [
        {
            "n_orden": "24102",
            "fecha_cierre": date.today() - timedelta(days=2),
            "descripcion": "Fleece 30/1 · Heather grey",
            "kg": 210.7,
            "costo_kg": 2.15,
        },
        {
            "n_orden": "24103",
            "fecha_cierre": date.today() - timedelta(days=2),
            "descripcion": "Jersey 30/1 · Blanco óptico",
            "kg": 145.2,
            "costo_kg": 1.55,
        },
    ],
}


@dataclass
class FakeAdapter:
    """Devuelve costos sintéticos. No hace I/O. Ideal para tests y demo."""

    fuente: str = "fake"
    _extra: dict[str, list[dict]] = field(default_factory=dict)

    def costos_por_cliente(self, codigo_cli: str) -> list[OTCosto]:
        codigo_cli = (codigo_cli or "").strip().upper()
        if not codigo_cli:
            return []
        raw = list(_FAKE_OTS.get(codigo_cli, [])) + list(self._extra.get(codigo_cli, []))
        return [self._to_costo(codigo_cli, r) for r in raw]

    def costos_por_factura(self, id_factura: int) -> list[OTCosto]:
        # Para el FakeAdapter no sabemos el mapping factura→OT; el caller debe
        # usar costos_por_cliente() en este adapter. Devolvemos vacío para que
        # el caller pueda tomar decisiones con seguridad.
        return []

    def disponible(self) -> bool:
        return True

    @staticmethod
    def _to_costo(codigo_cli: str, r: dict) -> OTCosto:
        kg = float(r["kg"])
        costo_kg = float(r["costo_kg"])
        return OTCosto(
            n_orden=str(r["n_orden"]),
            fecha_cierre=r.get("fecha_cierre"),
            cliente_codigo=codigo_cli,
            descripcion=str(r.get("descripcion", "")),
            kg=kg,
            costo_kg=costo_kg,
            costo_total=round(kg * costo_kg, 2),
            fuente="fake",
        )


# ---------------------------------------------------------------------------
# MetabaseAdapter — lee de una pregunta guardada en Metabase
# ---------------------------------------------------------------------------


@dataclass
class MetabaseAdapter:
    """Lee costos desde una Metabase question guardada.

    La lógica HTTP (login, refresh on 401, fetch_card) vive en el cliente
    compartido `modules._lib.metabase_client`. Este adapter solo proyecta
    los rows al dataclass OTCosto.

    Env vars esperadas:
        METABASE_URL                    — base URL (http://localhost:3000 en EC2)
        METABASE_USERNAME, METABASE_PASSWORD — para obtener sesión
        METABASE_QUESTION_ID_COSTOS_OT  — question_id que devuelve todas las OTs cerradas

    La question de Metabase tiene que devolver columnas con estos nombres
    (canonical en la infra de formulas_app):
        n_orden TEXT, fecha_cierre DATE, cliente_codigo TEXT, descripcion TEXT,
        kg NUMERIC, costo_kg NUMERIC
    """

    fuente: str = "metabase"

    def _question_id(self) -> str | None:
        return os.environ.get("METABASE_QUESTION_ID_COSTOS_OT") or None

    def disponible(self) -> bool:
        from modules._lib import metabase_client

        return metabase_client.disponible() and bool(self._question_id())

    def costos_por_cliente(self, codigo_cli: str) -> list[OTCosto]:
        if not self.disponible():
            _LOG.info("MetabaseAdapter no configurado; devolviendo lista vacía")
            return []
        from modules._lib import metabase_client

        rows = metabase_client.fetch_card(self._question_id())
        codigo_cli = (codigo_cli or "").strip().upper()
        return [
            self._row_to_costo(r)
            for r in rows
            if str(r.get("cliente_codigo", "")).upper() == codigo_cli
        ]

    def costos_por_factura(self, id_factura: int) -> list[OTCosto]:
        # Requiere mapping factura→OT que tiene que vivir en formulas_app
        # (todavía no existe ahí). Cuando exista, otra pregunta en Metabase
        # con parameter `id_factura`.
        return []

    @staticmethod
    def _row_to_costo(r: dict) -> OTCosto:
        kg = float(r.get("kg") or 0)
        costo_kg = float(r.get("costo_kg") or 0)
        fecha_cierre = r.get("fecha_cierre")
        if isinstance(fecha_cierre, str) and fecha_cierre:
            try:
                fecha_cierre = date.fromisoformat(fecha_cierre[:10])
            except ValueError:
                fecha_cierre = None
        return OTCosto(
            n_orden=str(r.get("n_orden", "")),
            fecha_cierre=fecha_cierre if isinstance(fecha_cierre, date) else None,
            cliente_codigo=str(r.get("cliente_codigo", "")).upper(),
            descripcion=str(r.get("descripcion", "")),
            kg=kg,
            costo_kg=costo_kg,
            costo_total=round(kg * costo_kg, 2),
            fuente="metabase",
        )


# ---------------------------------------------------------------------------
# PostgresAdapter — view cross-schema en el mismo RDS
# ---------------------------------------------------------------------------


@dataclass
class PostgresAdapter:
    """Adapter HISTÓRICO. NO IMPLEMENTABLE contra formulas_app real.

    Cuando se diseñó este módulo se asumió que `formulas_app.ordenes`
    tenía un mapping a cliente (`codigo_cli` / `fecha_cierre`). Validado
    contra el schema real (2026-05-21) NO lo tiene: formulas_app es una
    app de tintorería pura — sabe qué fórmula, qué jet, qué kilos, pero
    NO sabe a qué cliente pertenece cada orden. Esa relación vive en
    otro lado (presumiblemente el ERP Asinfo).

    Hasta que ese mapping exista (decisión de producto, no de código),
    este adapter queda permanentemente deshabilitado:
        - `disponible()` → False
        - `costos_por_cliente()` → []

    Se mantiene la clase para preservar la API del adapter pattern
    (build_adapter("postgres") devuelve algo válido) y los tests
    existentes. Si en el futuro se agrega la columna o se materializa
    el mapping en Programa Core, restaurar la implementación acá.

    Ver el SKILL programa-core-integraciones, sección "Cliente en
    formulas_app — no existe", para el contexto completo.
    """

    fuente: str = "postgres"

    def disponible(self) -> bool:
        # Permanentemente False — el bridge "por cliente" no es
        # implementable contra el schema real de formulas_app.
        return False

    def costos_por_cliente(self, codigo_cli: str) -> list[OTCosto]:
        _LOG.info(
            "PostgresAdapter.costos_por_cliente: no-op — formulas_app no tiene "
            "mapping orden→cliente. Ver SKILL programa-core-integraciones."
        )
        return []

    def costos_por_factura(self, id_factura: int) -> list[OTCosto]:
        # Cuando el puente bi-direccional exista: factura.id_factura →
        # formulas_app.ordenes.id_factura_destino. Por ahora vacío.
        return []

    @staticmethod
    def _row_to_costo(r: dict) -> OTCosto:
        kg = float(r.get("kg") or 0)
        costo_kg = float(r.get("costo_kg") or 0)
        return OTCosto(
            n_orden=str(r.get("n_orden", "")),
            fecha_cierre=r.get("fecha_cierre"),
            cliente_codigo=str(r.get("cliente_codigo", "")).upper(),
            descripcion=str(r.get("descripcion", "")),
            kg=kg,
            costo_kg=costo_kg,
            costo_total=round(kg * costo_kg, 2),
            fuente="postgres",
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_adapter(name: str | None = None) -> CostosOTAdapter:
    """Construye el adapter según env var o override explícito.

    Default: FakeAdapter. Sirve para dev y tests sin configuración adicional.
    """
    chosen = (name or os.environ.get("COSTOS_OT_ADAPTER", "fake")).strip().lower()
    if chosen == "metabase":
        return MetabaseAdapter()
    if chosen == "postgres":
        return PostgresAdapter()
    # Fallback silencioso a fake — prioridad: nunca romper la UI.
    if chosen != "fake":
        _LOG.warning("COSTOS_OT_ADAPTER=%r desconocido, usando fake", chosen)
    return FakeAdapter()
