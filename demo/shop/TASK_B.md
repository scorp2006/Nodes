# Task B (bob): loyalty points
1. Add a field `points: int = 0` to the `User` model in `src/shop/models.py`.
2. Add a function `award_points(user: User, order: Order) -> User` to `src/shop/models.py` that adds 1 point per whole dollar of `order_total(order)`, and export it in `__all__`.
3. Add `"points": user.points` to `user_summary` in `src/shop/api.py`.
4. Add `tests/test_points.py` covering it. Run `python -m pytest -q`. Commit with message "add loyalty points".
