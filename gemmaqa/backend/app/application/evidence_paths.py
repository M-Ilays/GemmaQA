"""Safe evidence path helpers for reports and APIs."""

from __future__ import annotations

from pathlib import Path


def to_relative_evidence_path(path: str | Path, run_root: Path) -> str:
    """Convert absolute path to run-relative POSIX path; never leak drive letters."""
    try:
        p = Path(path).resolve()
        root = run_root.resolve()
        rel = p.relative_to(root)
        return rel.as_posix()
    except Exception:
        text = str(path).replace("\\", "/")
        # Strip anything before /evidence/<run_id>/ or common suffixes
        for marker in ("/screenshots/", "/traces/", "/reports/", "/network/", "/console/"):
            if marker in text:
                idx = text.rfind(marker)
                # include parent folder name
                parts = text[idx:].lstrip("/").split("/")
                if parts:
                    return "/".join(parts)
        return Path(text).name


def public_evidence_url(run_id: str, relative_path: str) -> str:
    rel = relative_path.replace("\\", "/").lstrip("/")
    return f"/api/runs/{run_id}/evidence/file/{rel}"


def sanitize_evidence_index_entry(
    *,
    run_id: str,
    evidence_id: str,
    kind: str,
    path: str,
    run_root: Path,
    description: str = "",
) -> dict:
    rel = to_relative_evidence_path(path, run_root)
    return {
        "id": evidence_id,
        "kind": kind,
        "relative_path": rel,
        "public_url": public_evidence_url(run_id, rel),
        "description": description,
    }
