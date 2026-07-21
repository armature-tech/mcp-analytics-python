"""Customer-style tool functions defined under ``from __future__ import annotations``.

The future import turns every annotation into a string that only resolves in
THIS module's namespace — the shape of real customer servers such as
armature-v1-qa-mcp-python. Schema parity between instrumented and
uninstrumented registrations must hold for these tools (QA-03: customer-owned
result shapes remain unchanged).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Customer(BaseModel):
    customer_id: str
    name: str


def get_customer(customer_id: str) -> dict[str, Any]:
    """Get one synthetic customer."""

    return {"customer_id": customer_id, "name": "Acme"}


def get_customer_model(customer_id: str) -> Customer:
    """Get one synthetic customer as a model."""

    return Customer(customer_id=customer_id, name="Acme")
