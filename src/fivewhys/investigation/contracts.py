"""Wire contract shared by the producer documentation and callback receiver."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class TerminalJob(BaseModel):
    id: UUID
    kind: Literal["analysis", "index"]
    team: Literal["orders", "platform"]
    status: Literal["completed", "failed"]
    result: dict | None
    error: str | None
    error_code: str | None


class Receipt(BaseModel):
    id: UUID
    attempt: int
    status: Literal["retry", "accepted"]
