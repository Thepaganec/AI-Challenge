import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List


class ProjectMCPService:
    def __init__(self, project_root: str, logger: Any = None):
        self.project_root = Path(project_root).resolve()
        self.logger = logger

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    async def get_git_branch(self, trace_id: str = "") -> Dict[str, Any]:
        proc = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        branch = (proc.stdout or "").strip()
        if proc.returncode != 0 or not branch:
            return {
                "ok": False,
                "is_error": True,
                "tool": "get_git_branch",
                "trace_id": trace_id,
                "message": (proc.stderr or proc.stdout or "Failed to resolve git branch").strip(),
                "project_root": str(self.project_root),
            }
        return {
            "ok": True,
            "tool": "get_git_branch",
            "trace_id": trace_id,
            "branch": branch,
            "project_root": str(self.project_root),
            "is_git_repo": True,
        }

    async def list_project_files(self, limit: int = 60, max_depth: int = 3, trace_id: str = "") -> Dict[str, Any]:
        safe_limit = max(1, min(int(limit or 60), 200))
        safe_depth = max(1, min(int(max_depth or 3), 6))
        ignored_dirs = {".git", ".venv", "__pycache__", "logs", "RAG"}
        collected: List[str] = []

        for path in sorted(self.project_root.rglob("*")):
            if len(collected) >= safe_limit:
                break
            try:
                rel = path.relative_to(self.project_root)
            except Exception:
                continue
            parts = rel.parts
            if not parts:
                continue
            if any(part in ignored_dirs for part in parts):
                continue
            if len(parts) > safe_depth:
                continue
            if path.is_dir():
                continue
            collected.append(rel.as_posix())

        return {
            "ok": True,
            "tool": "list_project_files",
            "trace_id": trace_id,
            "project_root": str(self.project_root),
            "limit": safe_limit,
            "max_depth": safe_depth,
            "files": collected,
            "returned": len(collected),
        }
