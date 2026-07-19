from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class InstallationState:
    schema: int = 1
    phase: str = "absent"
    bridge_version: str = "0.1.0"
    bedolaga_version: str | None = None
    bedolaga_commit: str | None = None
    backup_id: str | None = None
    candidate_image: str | None = None
    original_image: str | None = None
    activation_percent: int = 0
    paused_from_phase: str | None = None
    paused_activation_percent: int = 0
    canary_uuid_hash: str | None = None
    last_error: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def load(cls, path: Path) -> InstallationState:
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        known = {key: value for key, value in payload.items() if key in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path) -> None:
        self.updated_at = datetime.now(UTC).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)

    def update(self, **changes: Any) -> InstallationState:
        for key, value in changes.items():
            if key not in self.__dataclass_fields__:
                raise KeyError(key)
            setattr(self, key, value)
        return self
