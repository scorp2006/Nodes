"""A tiny API layer over the models."""
from shop.models import Order, User, order_total


def user_summary(user: User) -> dict:
    return {"id": user.id, "name": user.name}


def order_summary(order: Order) -> dict:
    return {"id": order.id, "total_cents": order_total(order)}
