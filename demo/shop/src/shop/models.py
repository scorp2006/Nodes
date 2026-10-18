"""Shop domain models."""
from dataclasses import dataclass, field

__all__ = ["User", "Order", "order_total"]

CURRENCY = "USD"


@dataclass
class User:
    id: int
    name: str
    email: str


@dataclass
class Order:
    id: int
    user_id: int
    items: list = field(default_factory=list)  # list of (sku, price_cents)


def order_total(order: Order) -> int:
    return sum(price for _, price in order.items)
