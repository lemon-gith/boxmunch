"""boxmunge add-git-project — create a project from a git repo."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

from boxmunge.fileutil import LockError, project_lock
from boxmunge.identity import check_project_identity, register_project_identity
from boxmunge.log import log_operation
from boxmunge.manifest import load_manifest, validate_manifest, ManifestError
from boxmunge.paths import validate_project_name

if TYPE_CHECKING:
    from boxmunge.paths import BoxPaths

# Transports git is allowed to use for the clone. Belt-and-braces with
# validate_repo_url: even if a URL form slipped past validation, git itself
# refuses file://, git://, ext:: and other remote helpers.
_GIT_ALLOW_PROTOCOL = "https:ssh"

# scp-like syntax: user@host:path (e.g. git@github.com:org/repo.git).
_SCP_LIKE_URL = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s:][^\s]*$")
_URL_SCHEMES = ("https://", "ssh://")


def validate_repo_url(repo_url: str) -> str | None:
    """Return an error message if repo_url is not an acceptable remote URL.

    Accepts https://, ssh:// and scp-like user@host:path remotes. Rejects
    anything git could interpret as an option (leading '-'), a local path or
    file:// URL (which would let a caller clone another project's repo off
    this host's disk), and remote-helper transports such as ext::.
    """
    if not repo_url:
        return "repository URL is empty"
    if repo_url.startswith("-"):
        return "repository URL must not start with '-'"
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in repo_url):
        return "repository URL must not contain whitespace or control characters"
    if "::" in repo_url:
        return "repository URL must not use a remote-helper transport"
    if repo_url.lower().startswith(_URL_SCHEMES) or _SCP_LIKE_URL.match(repo_url):
        return None
    return ("repository URL must be https://, ssh:// or user@host:path "
            "(local paths and other schemes are not accepted)")


def validate_ref(ref: str) -> str | None:
    """Return an error message if ref is not an acceptable branch/tag name."""
    if not ref or ref.startswith("-") or any(c.isspace() for c in ref):
        return "ref must be non-empty, must not start with '-' and must not contain whitespace"
    return None


def run_add_git_project(
    name: str, repo_url: str, paths: BoxPaths, ref: str | None = None,
) -> int:
    """Create a new project from a git repository. Returns 0 on success."""
    # Validate everything caller-supplied before touching the filesystem.
    try:
        validate_project_name(name)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    url_error = validate_repo_url(repo_url)
    if url_error:
        print(f"ERROR: {url_error}", file=sys.stderr)
        return 2
    if ref is not None:
        ref_error = validate_ref(ref)
        if ref_error:
            print(f"ERROR: {ref_error}", file=sys.stderr)
            return 2

    try:
        with project_lock(name, paths):
            return _run_add_git_project_locked(name, repo_url, paths, ref)
    except LockError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _run_add_git_project_locked(
    name: str, repo_url: str, paths: BoxPaths, ref: str | None,
) -> int:
    project_dir = paths.project_dir(name)

    if project_dir.exists() and (project_dir / "manifest.yml").exists():
        print(f"ERROR: Project '{name}' already exists.", file=sys.stderr)
        return 1

    repo_dir = project_dir / "repo"
    print(f"Cloning {repo_url}...")
    try:
        clone_cmd = ["git", "clone"]
        if ref:
            clone_cmd.extend(["--branch", ref])
        # '--' ends option parsing so the URL can never be read as a flag.
        clone_cmd.extend(["--", repo_url, str(repo_dir)])
        subprocess.run(
            clone_cmd, check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_ALLOW_PROTOCOL": _GIT_ALLOW_PROTOCOL},
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Git clone failed: {e.stderr}", file=sys.stderr)
        return 1

    manifest_path = repo_dir / "manifest.yml"
    if not manifest_path.exists():
        print(f"ERROR: Repository has no manifest.yml at root.", file=sys.stderr)
        return 1

    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    errors, warnings = validate_manifest(manifest, name)
    if errors:
        print(f"ERROR: Manifest validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  {e}")
        return 1
    for w in warnings:
        print(f"  WARN: {w}")

    manifest_id = manifest.get("id", "")
    try:
        check_project_identity(name, manifest_id, paths)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "backups").mkdir(exist_ok=True)
    (project_dir / "data").mkdir(exist_ok=True)

    # Symlink compose.yml and manifest.yml from repo to project root
    compose_src = repo_dir / "compose.yml"
    compose_dst = project_dir / "compose.yml"
    if compose_src.exists() and not compose_dst.exists():
        compose_dst.symlink_to(compose_src)

    manifest_dst = project_dir / "manifest.yml"
    if not manifest_dst.exists():
        manifest_dst.symlink_to(manifest_path)

    register_project_identity(name, manifest_id, paths)

    log_operation("add-git-project", f"Added git project from {repo_url}", paths, project=name)
    print(f"Project '{name}' created from {repo_url}.")
    print(f"  Run 'stage {name}' or 'deploy {name}' to start it.")
    return 0


def cmd_add_git_project(args: list[str]) -> None:
    """CLI entry point for add-git-project command."""
    from boxmunge.commands.argv import parse_args
    from boxmunge.paths import BoxPaths

    parsed = parse_args(
        "add-git-project", args, options={"--ref"},
        min_positional=2, max_positional=2,
    )
    name, repo_url = parsed.positional
    paths = BoxPaths()
    sys.exit(run_add_git_project(name, repo_url, paths, ref=parsed.get("--ref")))
