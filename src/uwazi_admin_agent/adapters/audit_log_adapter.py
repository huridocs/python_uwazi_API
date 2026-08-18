"""JSON Lines audit log on disk (§5 Phase 6).

Layout::

    <root>/<run_id>/audit.jsonl

One JSON record per line, appended in order. JSONL is the audit-log convention
(append-only, tail-able, no rewrite-on-each op). Pure filesystem — no network.
A missing file loads as an empty list (a run that wrote nothing has no audit
log, which is fine for read-only steps).
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from loguru import logger

from uwazi_admin_agent.domain.audit_record import AuditRecord
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort

AUDIT_FILENAME = "audit.jsonl"


class JsonlAuditLog(AuditLogPort):
    """On-disk JSONL audit log, one record per line."""

    def __init__(self, root: Path) -> None:
        self._root: Path = Path(root)

    @override
    def append(self, run_id: str, record: AuditRecord) -> None:
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / AUDIT_FILENAME
        with path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json())
            fh.write("\n")
        logger.debug("audit appended run={} op={} outcome={}", run_id, record.op_kind, record.outcome.value)

    @override
    def load(self, run_id: str) -> list[AuditRecord]:
        path = self._run_dir(run_id) / AUDIT_FILENAME
        if not path.exists():
            return []
        records: list[AuditRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(AuditRecord.model_validate_json(line))
        return records

    def _run_dir(self, run_id: str) -> Path:
        return self._root / run_id
