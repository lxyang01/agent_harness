from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TraceLogger:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "traces"
        self.root.mkdir(parents=True, exist_ok=True)

    def log(self, session_key: str, event: str, **data: Any) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        with (self.root / f"{session_key}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

