from datetime import datetime

from django.conf import settings
from ninja import Router, Schema

from accounts.models import LoginEvent

from ..deps import session_auth

router = Router()


class LoginEventOut(Schema):
    id: int
    kind: str  # login | failed | two_factor_failed | two_factor_locked
    ip: str | None
    user_agent: str
    created_at: datetime


@router.get("/login-events", auth=session_auth, response=list[LoginEventOut], summary="Recent sign-in activity")
def login_events(request):
    return LoginEvent.objects.filter(user=request.auth)[: settings.LOGIN_EVENTS_SHOWN]
