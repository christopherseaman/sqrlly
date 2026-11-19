"""Share dependencies and initialize isolated worktrees."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import subprocess
from pathlib import Path


def materialize_shares(base: str, dest: str, shares: list[str]) -> None:
    """Link each configured base path inside the engine-owned tree."""
    for share in shares:
        rel = Path(share)
        if not rel.parts or rel.is_absolute() or ".." in rel.parts or ".git" in rel.parts:
            raise RuntimeError(f"worktree_share requires a repository-relative path: {share!r}")
        src = Path(base) / rel
        link = Path(dest) / rel
        if not link.parent.resolve().is_relative_to(Path(dest).resolve()):
            raise RuntimeError(f"worktree_share escapes worktree: {share!r}")
        if not src.exists():
            raise RuntimeError(
                f"worktree_share path {share!r} does not exist in base "
                f"{base!r} — install/build it before the run."
            )
        target = os.path.relpath(src, link.parent)
        if link.is_symlink():
            if os.readlink(link) != target:
                raise RuntimeError(f"Unexpected existing symlink at {link}")
        elif not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)



def setup_fingerprint(base: str, commands: list[str]) -> str:
    """Sentinel content: hash of the setup commands + the base HEAD commit.
    Re-runs setup when commands change OR the base advances (a lockfile/schema
    change is a base commit in a git workflow). PM-agnostic — no knowledge of
    pnpm/Prisma file paths (decision: hash base state, not the worktree schema)."""
    try:
        head = subprocess.run(
            ["git", "-C", base, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        head = ""
    blob = "\n".join(commands) + "\x00" + head
    return hashlib.sha256(blob.encode()).hexdigest()


async def ensure_setup(
    *, base: str, dest: str, commands: list[str],
    retries: int = 1,
) -> None:
    """Run setup once per command list and base commit."""
    if not commands:
        return
    marker = Path(dest) / ".sqrlly" / "setup-ok"
    if marker.resolve() != marker.absolute():
        raise RuntimeError(f"Unexpected worktree setup marker symlink at {marker}")
    fp = setup_fingerprint(base, commands)
    if marker.exists() and marker.read_text().strip() == fp:
        return
    env = dict(os.environ)
    for cmd in commands:
        argv = shlex.split(cmd)
        attempt = 0
        while True:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=dest, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _out, errb = await proc.communicate()
            if proc.returncode == 0:
                break
            if attempt >= retries:
                raise RuntimeError(
                    f"worktree setup failed: {cmd!r} exit {proc.returncode} "
                    f"in {dest}: {errb.decode().strip()}"
                )
            attempt += 1
            await asyncio.sleep(0.5 * attempt)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(fp)
