from shop.api import order_summary, user_summary
from shop.models import Order, User


def test_base():
    u = User(1, "Ada", "ada@example.com")
    assert user_summary(u) == {"id": 1, "name": "Ada"}
    assert order_summary(Order(1, 1, [("a", 250), ("b", 100)])) == {"id": 1, "total_cents": 350}
