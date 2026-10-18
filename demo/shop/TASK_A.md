# Task A (alice): email verification
1. Add a field `email_verified: bool = False` to the `User` model in `src/shop/models.py`.
2. Add a function `verify_email(user: User) -> User` to `src/shop/models.py` that returns the user with `email_verified=True`, and export it in `__all__`.
3. Add `"email_verified": user.email_verified` to `user_summary` in `src/shop/api.py`.
4. Add `tests/test_verify.py` covering it. Run `python -m pytest -q`. Commit with message "add email verification".
