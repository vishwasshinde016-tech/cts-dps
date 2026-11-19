"""Authentication: Entra ID JWT (production) or trusted headers (dev/test only)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import jwt
from fastapi import Request

from dps.errors import ApiError
from dps.settings import AuthMode, Settings

USER_HEADER = "X-User"
ROLES_HEADER = "X-Roles"

log = logging.getLogger(__name__)

ADMIN, APPROVER, AUDITOR, AUTHOR, RM, SERVICE, SUPPORT = (
    "ADMIN", "APPROVER", "AUDITOR", "AUTHOR", "RM", "SERVICE", "SUPPORT")


@dataclass(frozen=True)
class Principal:
    user: str
    roles: frozenset[str] = field(default_factory=frozenset)
    token: str | None = None  # raw bearer token, forwarded to Core for ownership checks

    def has_any(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)


def _unauthorized(message: str = "Authentication required") -> ApiError:
    return ApiError(401, "UNAUTHORIZED", message)


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks: jwt.PyJWKClient | None = None
        if settings.auth_mode == AuthMode.DEV_HEADER and settings.env.lower() == "prod":
            raise RuntimeError("DPS_AUTH_MODE=DEV_HEADER is not allowed with DPS_ENV=prod; use JWT")
        # Entra ID signs every tenant's tokens with the same keys: without issuer and audience checks a
        # token Microsoft issued for any other tenant or app would be accepted.
        if settings.auth_mode == AuthMode.JWT and not (settings.jwt_issuer and settings.jwt_audience):
            raise RuntimeError("DPS_AUTH_MODE=JWT needs DPS_JWT_ISSUER and DPS_JWT_AUDIENCE")
        if settings.auth_mode == AuthMode.SERVICE_TOKEN and not (settings.service_token_sha256
                                                                and settings.service_allowed_networks):
            raise RuntimeError("DPS_AUTH_MODE=SERVICE_TOKEN needs DPS_SERVICE_TOKEN_SHA256 and "
                               "DPS_SERVICE_ALLOWED_NETWORKS")
        if settings.auth_mode == AuthMode.JWT and not settings.jwt_public_key_pem:
            if not settings.jwt_jwks_url:
                raise RuntimeError("DPS_AUTH_MODE=JWT needs DPS_JWT_JWKS_URL or DPS_JWT_PUBLIC_KEY_PEM")
            self._jwks = jwt.PyJWKClient(settings.jwt_jwks_url)

    def authenticate(self, request: Request) -> Principal:
        if self.settings.auth_mode == AuthMode.DEV_HEADER:
            return self._from_headers(request)
        if self.settings.auth_mode == AuthMode.SERVICE_TOKEN:
            return self._from_service_token(request)
        return self._from_jwt(request)

    def _from_service_token(self, request: Request) -> Principal:
        """G11: the caller is the CTS Spring app (role SERVICE) when it shows the vault token from an allowed network.
        The acting user in X-User is recorded as such; it never grants rights by itself."""
        import hashlib
        import hmac
        import ipaddress

        host = request.client.host if request.client else ""
        networks = []
        for n in self.settings.service_allowed_networks.split(","):
            try:
                networks.append(ipaddress.ip_network(n.strip(), strict=False))
            except ValueError:
                continue  # a malformed entry allows nothing
        try:
            address = ipaddress.ip_address(host)
            allowed = any(address in net for net in networks)
        except ValueError:
            allowed = False
        if not allowed:
            log.warning("Sign-in refused: caller outside the allowed networks")
            raise _unauthorized("Not allowed from this network")
        header = request.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not token or not hmac.compare_digest(digest, self.settings.service_token_sha256.lower()):
            raise _unauthorized("Invalid token")
        acting = (request.headers.get(USER_HEADER) or "").strip()
        return Principal(user=acting or "cts-spring", roles=frozenset({SERVICE}), token=None)

    @staticmethod
    def _from_headers(request: Request) -> Principal:
        user = (request.headers.get(USER_HEADER) or "").strip()
        if not user:
            raise _unauthorized()
        raw = request.headers.get(ROLES_HEADER) or RM
        roles = frozenset(r.strip().upper() for r in raw.split(",") if r.strip())
        return Principal(user=user, roles=roles or frozenset({RM}))

    def _from_jwt(self, request: Request) -> Principal:
        header = request.headers.get("Authorization") or ""
        if not header.lower().startswith("bearer "):
            raise _unauthorized()
        token = header[7:].strip()
        s = self.settings
        try:
            key = (s.jwt_public_key_pem if s.jwt_public_key_pem
                   else self._jwks.get_signing_key_from_jwt(token).key)  # type: ignore[union-attr]
            claims = jwt.decode(
                token, key, algorithms=["RS256"],
                audience=s.jwt_audience or None,
                issuer=s.jwt_issuer or None,
                leeway=s.jwt_leeway_seconds,
                options={"require": ["exp", "iat"], "verify_aud": bool(s.jwt_audience)},
            )
        except jwt.PyJWTError as exc:
            # The reason only (expired, wrong audience, key fetch failed...), never the token itself.
            log.warning("Sign-in refused: %s", type(exc).__name__)
            raise _unauthorized("Invalid token") from exc
        user = claims.get(s.jwt_principal_claim) or claims.get("sub")
        if not user:
            raise _unauthorized("Token has no user")
        roles = claims.get(s.jwt_roles_claim) or []
        if isinstance(roles, str):
            roles = [roles]
        return Principal(user=str(user), roles=frozenset(str(r).strip().upper() for r in roles if r),
                         token=token)


def require_roles(principal: Principal, *roles: str) -> None:
    if not principal.has_any(*roles):
        raise ApiError(403, "FORBIDDEN", "Missing required role")
