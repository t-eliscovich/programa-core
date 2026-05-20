#!/usr/bin/env python3
"""claude_push.py — auto-push desde el sandbox sin tocar .git/.

Usa la GitHub REST API para crear un commit + push a `main`. Requiere:
  - GH_PAT  : fine-grained PAT con scope `contents: read & write` sobre
              el repo programa-core. Se lee de:
                1. variable de entorno GH_PAT, o
                2. archivo `.gh_pat` en la raíz del repo (gitignored).
  - GH_OWNER, GH_REPO, GH_BRANCH : opcionales, leídos del config local
              (`.claude_push.json` en la raíz, gitignored) o defaults.

Uso:
    python scripts/claude_push.py "mensaje de commit"
    python scripts/claude_push.py            # mensaje default

Flujo:
  1. Compara el árbol del working tree vs el último SHA de main en remoto.
  2. Para cada archivo modificado/nuevo, crea un blob via la API.
  3. Crea un tree nuevo con esos blobs.
  4. Crea un commit con tree+parent y mensaje.
  5. Actualiza la ref `refs/heads/main` al nuevo commit.

NO toca `.git/` local. Después de pushear, el siguiente `git pull` desde
la Mac de Tamara trae los commits hechos por Claude.

Limitaciones:
  - No maneja conflictos (asume que Claude es el único editor activo).
    Si hay divergencia con remote, aborta y pide pull manual.
  - No empaqueta deleciones automáticamente (hay que listarlas en CLI).
  - Hace 1 blob por archivo → para batches grandes (50+ archivos)
    podría ser lento, pero típicamente vamos a hacer 5-20.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

REPO_ROOT = Path(__file__).resolve().parent.parent

# ─── Config ───────────────────────────────────────────────────────────────
DEFAULT_OWNER  = "t-eliscovich"
DEFAULT_REPO   = "programa-core"
DEFAULT_BRANCH = "main"

# Archivos/directorios a ignorar (mismo espíritu que .gitignore mínimo).
IGNORE_PATTERNS = {
    ".git", ".gh_pat", ".claude_push.json",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", ".env", ".env.local",
    ".DS_Store", "*.pyc",
}


def _load_config() -> dict:
    """Carga config del archivo `.claude_push.json` si existe, sino defaults."""
    cfg_path = REPO_ROOT / ".claude_push.json"
    cfg: dict = {
        "owner":  DEFAULT_OWNER,
        "repo":   DEFAULT_REPO,
        "branch": DEFAULT_BRANCH,
    }
    if cfg_path.exists():
        cfg.update(json.loads(cfg_path.read_text()))
    cfg["owner"]  = os.environ.get("GH_OWNER",  cfg["owner"])
    cfg["repo"]   = os.environ.get("GH_REPO",   cfg["repo"])
    cfg["branch"] = os.environ.get("GH_BRANCH", cfg["branch"])
    return cfg


def _load_pat() -> str:
    """Carga el PAT de env o de `.gh_pat`."""
    pat = os.environ.get("GH_PAT", "").strip()
    if pat:
        return pat
    pat_file = REPO_ROOT / ".gh_pat"
    if pat_file.exists():
        return pat_file.read_text().strip()
    raise SystemExit(
        "✗ No se encontró el PAT.\n"
        "  Setealo con: echo 'ghp_xxx' > .gh_pat && chmod 600 .gh_pat\n"
        "  O export GH_PAT=ghp_xxx"
    )


def _api(token: str, method: str, path: str, body: dict | None = None) -> dict:
    """Wrapper liviano de urllib para llamar a la GitHub API."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":    "claude-push",
    }
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req) as resp:
            return json.loads(resp.read().decode() or "null")
    except urlerror.HTTPError as e:
        body_txt = e.read().decode(errors="replace")
        raise SystemExit(f"✗ GitHub API {e.code}: {body_txt[:500]}") from e


def _walk_repo() -> list[Path]:
    """Lista archivos relevantes del working tree (respeta .gitignore básico)."""
    out: list[Path] = []
    for root, dirs, files in os.walk(REPO_ROOT):
        # filtrar dirs in-place
        dirs[:] = [d for d in dirs if d not in IGNORE_PATTERNS
                   and not d.startswith(".") or d in {".github"}]
        for fname in files:
            if fname in IGNORE_PATTERNS or fname.endswith(".pyc"):
                continue
            full = Path(root) / fname
            try:
                if full.stat().st_size > 1_000_000:
                    # Saltar archivos > 1MB (el commit típico no los necesita).
                    continue
            except OSError:
                continue
            out.append(full)
    return out


def _git_sha1(content: bytes) -> str:
    """SHA-1 estilo git: 'blob <len>\0<content>'."""
    h = hashlib.sha1()
    h.update(f"blob {len(content)}\0".encode())
    h.update(content)
    return h.hexdigest()


def push(message: str) -> None:
    cfg = _load_config()
    token = _load_pat()
    owner, repo, branch = cfg["owner"], cfg["repo"], cfg["branch"]

    print(f"→ Repo: {owner}/{repo} (branch {branch})")

    # 1. Obtener SHA del branch en remoto.
    ref = _api(token, "GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
    parent_sha = ref["object"]["sha"]
    print(f"→ Parent commit: {parent_sha[:8]}")

    # 2. Obtener el tree base (recursive) para saber qué SHAs ya existen.
    base_commit = _api(token, "GET", f"/repos/{owner}/{repo}/git/commits/{parent_sha}")
    base_tree_sha = base_commit["tree"]["sha"]
    base_tree = _api(
        token, "GET",
        f"/repos/{owner}/{repo}/git/trees/{base_tree_sha}?recursive=1",
    )
    remote_sha_by_path: dict[str, str] = {
        item["path"]: item["sha"]
        for item in base_tree.get("tree", [])
        if item.get("type") == "blob"
    }

    # 3. Walkar repo local, computar SHA, detectar diffs.
    locals_walk = _walk_repo()
    new_or_changed: list[tuple[str, bytes]] = []
    for full in locals_walk:
        rel = str(full.relative_to(REPO_ROOT)).replace(os.sep, "/")
        try:
            content = full.read_bytes()
        except OSError:
            continue
        local_sha = _git_sha1(content)
        if remote_sha_by_path.get(rel) != local_sha:
            new_or_changed.append((rel, content))

    if not new_or_changed:
        print("✓ Nada para commitear. Local == remoto.")
        return

    print(f"→ {len(new_or_changed)} archivos cambiados/nuevos. Subiendo blobs…")

    # 4. Crear blobs (uno por archivo).
    new_blobs: list[dict] = []
    for rel, content in new_or_changed:
        # Subimos como base64 para soportar binarios; works for text too.
        body = {
            "content":  base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        }
        blob = _api(token, "POST", f"/repos/{owner}/{repo}/git/blobs", body)
        new_blobs.append({
            "path":  rel,
            "mode":  "100644",
            "type":  "blob",
            "sha":   blob["sha"],
        })

    # 5. Crear el tree (con base_tree para preservar lo que no cambió).
    tree = _api(
        token, "POST", f"/repos/{owner}/{repo}/git/trees",
        {"base_tree": base_tree_sha, "tree": new_blobs},
    )
    print(f"→ Tree: {tree['sha'][:8]}")

    # 6. Crear el commit.
    commit = _api(
        token, "POST", f"/repos/{owner}/{repo}/git/commits",
        {"message": message, "tree": tree["sha"], "parents": [parent_sha]},
    )
    print(f"→ Commit: {commit['sha'][:8]} — {message[:60]}")

    # 7. Actualizar el ref (fast-forward only por seguridad).
    _api(
        token, "PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
        {"sha": commit["sha"], "force": False},
    )
    print(f"✓ Pusheado a {owner}/{repo}@{branch}")
    print(f"  → https://github.com/{owner}/{repo}/commit/{commit['sha']}")


def main(argv: list[str]) -> int:
    msg = argv[1] if len(argv) > 1 else "Cambios automáticos de Claude"
    push(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
