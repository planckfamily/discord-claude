from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Feature:
    name: str
    session_id: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "active"
    subdir: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    prompt_count: int = 0
    sessions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "status": self.status,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": self.total_cost_usd,
            "prompt_count": self.prompt_count,
        }
        if self.subdir:
            d["subdir"] = self.subdir
        if self.sessions:
            d["sessions"] = self.sessions
        return d

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Feature":
        return cls(
            name=name,
            session_id=data["session_id"],
            started_at=data.get("started_at", ""),
            status=data.get("status", "active"),
            subdir=data.get("subdir"),
            total_input_tokens=data.get("total_input_tokens", 0),
            total_output_tokens=data.get("total_output_tokens", 0),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            prompt_count=data.get("prompt_count", 0),
            sessions=data.get("sessions", []),
        )
