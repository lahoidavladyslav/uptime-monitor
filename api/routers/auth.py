from django.contrib.auth import logout
from django.middleware.csrf import get_token
from ninja import Router

from accounts.services import has_two_factor

from ..deps import session_auth
from ..schemas import StatusOut, UserOut

router = Router()


@router.get("/csrf", response=StatusOut, summary="Set the CSRF cookie")
def csrf(request):
    get_token(request)  # makes CsrfViewMiddleware put the cookie on this response
    return {"status": "ok"}


@router.get("/me", auth=session_auth, response=UserOut, summary="Current user")
def me(request):
    user = request.auth
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "has_2fa": has_two_factor(user),
    }


@router.post("/logout", auth=session_auth, response=StatusOut, summary="End the session")
def logout_view(request):
    logout(request)
    return {"status": "ok"}
