"""Append-only, hash-chained audit log with secret redaction."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key|api[_-]?secret|secret|token|password|passphrase|seed[_-]?phrase|authorization)\s*[=:]\s*\S+", re.I),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"^\s*Bearer\s+\S+", re.I),
)
GENERIC_KEYS = ("api_key", "api_secret", "secret", "token", "password", "passphrase", "private_key", "seed")

LAST_HASH_KEY = "cat_audit_head"


def redact(value: Any) -> Any:
    """Remove anything that could look like a credential before it hits disk."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(k in str(key).lower() for k in GENERIC_KEYS):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = value
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(lambda m: m.group(0).split("=")[0].split(":")[0].strip() + "= [REDACTED]", text)
        return text
    return value


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head = self._recover_head()

    def _recover_head(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = ""
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        if not last:
            return "0" * 64
        try:
            return json.loads(last).get("hash", "0" * 64)
        except json.JSONDecodeError:
            return "0" * 64

    def append(self, event: str, payload: dict[str, Any], mode: str = "PAPER") -> dict[str, Any]:
        record = {
            "seq": self._count() + 1,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "mode": mode,
            "prev": self._head,
            "data": redact(payload),
        }
        body = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256((self._head + body).encode("utf-8")).hexdigest()
        record["hash"] = digest
        self._head = digest
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os_sync(fh)
        return record

    def _count(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def verify(self) -> tuple[bool, str]:
        """Recompute the chain; any edit or removal breaks integrity."""
        if not self.path.exists():
            return True, "empty"
        head = "0" * 64
        count = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False, f"line {lineno}: not json"
                claimed = record.pop("hash", None)
                body = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                expect = hashlib.sha256((head + body).encode("utf-8")).hexdigest()
                if claimed != expect or record.get("prev") != head:
                    return False, f"line {lineno}: chain broken"
                head = claimed
                count += 1
        return True, f"{count} records intact"


def os_sync(fh) -> None:
    try:
        fh.flush()
        import os

        os.fsync(fh.fileno())
    except (OSError, ValueError):
        pass
