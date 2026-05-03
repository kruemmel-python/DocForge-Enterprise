def issue_token(user: str) -> str:
    if not user:
        raise ValueError("user is required")
    return f"token-for-{user}"
