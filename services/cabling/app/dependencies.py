from herd_common.auth import make_auth_dependencies

from app.config import settings

get_current_user_payload, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)
