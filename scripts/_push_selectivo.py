"""Push selectivo: solo manda los archivos pasados por arg.

Uso:
    python scripts/_push_selectivo.py "mensaje" archivo1 archivo2 ...

Wrapping rápido sobre claude_push para evitar el walk completo del repo
cuando solo necesitamos pushear N archivos puntuales.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from claude_push import _api, _load_config, _load_pat  # noqa: E402


def main():
    if len(sys.argv) < 3:
        print("Uso: _push_selectivo.py 'mensaje' archivo1 [archivo2 ...]")
        sys.exit(2)
    message = sys.argv[1]
    rel_files = sys.argv[2:]

    cfg = _load_config()
    token = _load_pat()
    owner, repo, branch = cfg["owner"], cfg["repo"], cfg["branch"]

    ref = _api(token, "GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
    parent_sha = ref["object"]["sha"]
    print(f"→ Parent: {parent_sha[:8]}")

    new_blobs: list[dict] = []
    for rel in rel_files:
        full = REPO_ROOT / rel
        if not full.exists():
            print(f"  ✗ no existe: {rel}")
            continue
        content = full.read_bytes()
        blob = _api(
            token, "POST", f"/repos/{owner}/{repo}/git/blobs",
            {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        new_blobs.append({
            "path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"],
        })
        print(f"  ✓ {rel}  ({blob['sha'][:8]})")

    base_commit = _api(token, "GET", f"/repos/{owner}/{repo}/git/commits/{parent_sha}")
    tree = _api(
        token, "POST", f"/repos/{owner}/{repo}/git/trees",
        {"base_tree": base_commit["tree"]["sha"], "tree": new_blobs},
    )
    commit = _api(
        token, "POST", f"/repos/{owner}/{repo}/git/commits",
        {"message": message, "tree": tree["sha"], "parents": [parent_sha]},
    )
    _api(
        token, "PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
        {"sha": commit["sha"], "force": False},
    )
    print(f"✓ {commit['sha'][:8]} pusheado: {message[:60]}")
    print(f"  https://github.com/{owner}/{repo}/commit/{commit['sha']}")


if __name__ == "__main__":
    main()
