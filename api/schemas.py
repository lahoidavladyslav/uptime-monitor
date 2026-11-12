from ninja import Schema


class ErrorOut(Schema):
    message: str
    errors: dict[str, list[str]] = {}


class UserOut(Schema):
    id: int
    username: str
    email: str
    has_2fa: bool


class StatusOut(Schema):
    status: str
