from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.middleware.csrf import get_token
from django.utils.http import urlsafe_base64_decode
from ninja import Router, Schema, Status

from accounts import services
from accounts.forms import LoginForm, SignupForm

from ..deps import csrf_protect, session_auth
from ..errors import ApiError, field_errors_from_django
from ..schemas import StatusOut, UserOut

User = get_user_model()
router = Router()
INVALID = "Перевірте введені дані."
BAD_LINK = "Посилання недійсне або застаріло."


class SignupIn(Schema):
    username: str
    email: str
    password1: str
    password2: str


class TokenIn(Schema):
    token: str


class EmailIn(Schema):
    email: str


class LoginIn(Schema):
    username: str
    password: str


class CodeIn(Schema):
    code: str


class ResetConfirmIn(Schema):
    uid: str
    token: str
    new_password1: str
    new_password2: str


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
        "has_2fa": services.has_two_factor(user),
    }


@router.post("/logout", auth=session_auth, response=StatusOut, summary="End the session")
def logout_view(request):
    logout(request)
    return {"status": "ok"}


def _spa_link(path: str, token: str) -> str:
    return f"{settings.FRONTEND_URL}{path}{token}"


@router.post(
    "/signup",
    auth=csrf_protect,
    response={201: StatusOut},
    summary="Create an inactive account and mail a confirmation link",
)
def signup(request, data: SignupIn):
    form = SignupForm(data.dict())
    if not form.is_valid():
        raise ApiError(422, INVALID, field_errors_from_django(form.errors))
    services.register(
        form,
        lambda user: _spa_link("/confirm/", services.make_confirmation_token(user)),
        f"{settings.FRONTEND_URL}/forgot-password",
    )
    return Status(201, {"status": "confirmation_sent"})


@router.post("/confirm", auth=csrf_protect, response=StatusOut, summary="Activate an account from the emailed token")
def confirm(request, data: TokenIn):
    if services.confirm_token(data.token) is None:
        raise ApiError(400, BAD_LINK)
    return {"status": "confirmed"}


@router.post(
    "/resend", auth=csrf_protect, response=StatusOut, summary="Resend the confirmation mail (same answer for any email)"
)
def resend(request, data: EmailIn):
    services.resend_confirmation(data.email, lambda u: _spa_link("/confirm/", services.make_confirmation_token(u)))
    return {"status": "ok"}


@router.post(
    "/login", auth=csrf_protect, response=StatusOut, summary="Log in; 2fa_required means a code is still needed"
)
def login_view(request, data: LoginIn):
    request.session.pop("pre_2fa", None)  # a new attempt always replaces a pending challenge
    form = LoginForm(request, data=data.dict())
    if not form.is_valid():
        raise ApiError(422, INVALID, field_errors_from_django(form.errors))
    user = form.get_user()
    if services.has_two_factor(user):
        services.begin_two_factor(request, user, "")
        return {"status": "2fa_required"}
    login(request, user)
    return {"status": "ok"}


@router.post("/login/2fa", auth=csrf_protect, response=StatusOut, summary="Complete a pending 2FA login")
def login_2fa(request, data: CodeIn):
    result = services.finish_two_factor(request, data.code)
    if result == "ok":
        return {"status": "ok"}
    if result == "invalid":
        raise ApiError(422, INVALID, {"code": ["Невірний код."]})
    raise ApiError(401, "Сесія входу завершилась. Увійдіть знову.")


@router.post(
    "/password-reset", auth=csrf_protect, response=StatusOut, summary="Mail a reset link (same answer for any email)"
)
def password_reset(request, data: EmailIn):
    form = PasswordResetForm({"email": data.email})
    if not form.is_valid():
        raise ApiError(422, INVALID, field_errors_from_django(form.errors))
    form.save(
        request=request,
        email_template_name="registration/password_reset_spa_email.html",
        subject_template_name="registration/password_reset_subject.txt",
        extra_email_context={"frontend_url": settings.FRONTEND_URL},
    )
    return {"status": "ok"}


@router.post(
    "/password-reset/confirm", auth=csrf_protect, response=StatusOut, summary="Set a new password from a reset link"
)
def password_reset_confirm(request, data: ResetConfirmIn):
    try:
        user = User.objects.get(pk=urlsafe_base64_decode(data.uid).decode())
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        raise ApiError(400, BAD_LINK) from None
    if not default_token_generator.check_token(user, data.token):
        raise ApiError(400, BAD_LINK)
    form = SetPasswordForm(user, {"new_password1": data.new_password1, "new_password2": data.new_password2})
    if not form.is_valid():
        raise ApiError(422, INVALID, field_errors_from_django(form.errors))
    form.save()
    return {"status": "ok"}
