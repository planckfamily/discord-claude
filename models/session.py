from dataclasses import dataclass


@dataclass
class StreamEvent:
    type: str  # "text", "tool_use", "error", "result"
    content: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    context_window: int | None = None
    model: str | None = None
