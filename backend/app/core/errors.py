class DomainError(Exception):
    """A safe, client-visible error; never include host paths or driver messages."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
