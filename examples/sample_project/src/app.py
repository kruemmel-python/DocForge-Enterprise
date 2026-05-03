from .auth import issue_token

def main(user: str) -> str:
    return issue_token(user)
