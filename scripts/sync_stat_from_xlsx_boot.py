"""Auto-sync stat='*' al boot del app.

Si existe `data/dbase_snapshots/PICHINCH.xlsx` con un hash distinto al que
ya sincamos (marker file `data/dbase_snapshots/.PICHINCH.synced`), corre
`sync_stat_from_xlsx.py` una sola vez y graba el hash al marker. Idempotente
entre reboots: el segundo boot no hace nada hasta que subamos un xlsx nuevo.

TMT 2026-05-28 dueña: 'hacelo vos, no quiero usar mi compu'. El sync via
admin endpoint (/admin/dbase-sync/stat-xlsx) sigue funcionando para
disparos manuales; éste corre automáticamente al deploy nuevo.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

_LOG = logging.getLogger("programa_core.boot.sync_stat")

# Bancos que tienen snapshot xlsx en el repo. Si agregás INTERNACIONAL más
# adelante, sumá la fila acá.
_SNAPSHOTS: tuple[tuple[str, int], ...] = (
    ("PICHINCH.xlsx", 10),
)


def _xlsx_hash(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _marker_for(xlsx: Path) -> Path:
    return xlsx.with_name("." + xlsx.stem + ".synced")


def maybe_run_once() -> None:
    """Corre el sync por cada snapshot cuya marca no coincide con el hash."""
    root = Path(__file__).resolve().parent.parent
    snap_dir = root / "data" / "dbase_snapshots"
    if not snap_dir.exists():
        _LOG.info("sync stat boot: %s no existe → no-op.", snap_dir)
        return

    for fname, no_banco in _SNAPSHOTS:
        xlsx = snap_dir / fname
        if not xlsx.exists():
            _LOG.info("sync stat boot: %s no existe → skip.", xlsx)
            continue
        h = _xlsx_hash(xlsx)
        marker = _marker_for(xlsx)
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == h:
            _LOG.info(
                "sync stat boot: %s ya sincado (hash=%s) → no-op.",
                fname, h[:8],
            )
            continue
        _LOG.warning(
            "sync stat boot: corriendo sync_stat_from_xlsx para %s "
            "(no_banco=%s, hash=%s)",
            fname, no_banco, h[:8],
        )
        try:
            from scripts.sync_stat_from_xlsx import main as _sync_main

            rc = _sync_main(["--xlsx", str(xlsx), "--no-banco", str(no_banco)])
            if rc == 0:
                marker.write_text(h, encoding="utf-8")
                _LOG.warning(
                    "sync stat boot: %s OK · marker actualizado.", fname
                )
            else:
                _LOG.error("sync stat boot: %s exit=%s", fname, rc)
        except Exception:
            _LOG.exception("sync stat boot: excepción en %s", fname)
