"""DPS → the CTS Spring app's reads the statement reader needs, as the SERVICE identity: a CP's loan currency and
borrower profile, the country profiles, and the bank's FX rates. Everything else DPS asks Spring goes through the
Spring tools client (``integrations/spring_tools.py``: covenant library, formula validation, identifier lookup) or
the event push. DPS writes nothing to Spring's database and sends no data events (full review G9, G12).
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from datetime import date

import httpx

from dps.api.auth import ROLES_HEADER, SERVICE, USER_HEADER
from dps.integrations.service_auth import ClientCredentialsToken, TokenError
from dps.ops import observability as obs
from dps.settings import Settings

log = logging.getLogger(__name__)


class SpringUnavailable(Exception):
    """Spring could not be reached or answered 5xx; the caller retries later."""


# The correlation id of the job being processed (set by the worker for each job): every call to Spring carries it,
# so one upload can be followed across the bank app and DPS (CTS review, 23 Nov 2026: library, currency, profile,
# countries and FX calls had none).
CORRELATION: ContextVar[str | None] = ContextVar("dps_spring_correlation", default=None)


class SpringClient:
    """Sends a client-credentials token when ``oauth`` is given (production), else a fixed ``service_token``,
    else the SERVICE dev headers."""

    # An empty service_token means "no fixed token" (the dev headers or OAuth are used instead).
    def __init__(self, base_url: str, timeout: float, service_token: str = "",  # nosec B107
                 client: httpx.Client | None = None, oauth: ClientCredentialsToken | None = None) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)
        self._oauth = oauth
        self._countries: tuple[float, list[dict]] | None = None
        self._headers = ({"Authorization": f"Bearer {service_token}"} if service_token
                         else {USER_HEADER: "dps-service", ROLES_HEADER: SERVICE})

    @classmethod
    def from_settings(cls, settings: Settings) -> SpringClient:
        oauth = (ClientCredentialsToken(settings.core_token_url, settings.core_client_id,
                                        settings.core_client_secret, settings.core_scope)
                 if settings.core_token_url else None)
        return cls(settings.core_base_url, settings.core_timeout_seconds, settings.core_service_token, oauth=oauth)

    def _auth_headers(self) -> dict[str, str]:
        if self._oauth is None:
            return self._headers
        try:
            return {"Authorization": f"Bearer {self._oauth.token()}"}
        except TokenError as exc:
            raise SpringUnavailable(str(exc)) from exc  # the job is retried, as for any Spring outage

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {**(kw.pop("headers", None) or {}), **self._auth_headers(), **obs.trace_headers()}
        correlation = CORRELATION.get()
        if correlation and "X-Correlation-Id" not in headers:
            headers["X-Correlation-Id"] = correlation[:128]
        try:
            resp = self._client.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise SpringUnavailable(f"{method} {path}: {type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise SpringUnavailable(f"{method} {path}: HTTP {resp.status_code}")
        return resp

    def cp_currency(self, cp_id: str) -> str | None:
        """Money for any country: the CP's loan currency ("LKR"), or None when Spring does not answer."""
        try:
            resp = self._request("GET", f"/api/v1/credit-proposals/{cp_id}/currency")
        except SpringUnavailable:
            return None
        if resp.status_code != 200:
            return None
        code = (resp.json() or {}).get("currency")
        return code if isinstance(code, str) and len(code) == 3 else None

    def health(self, path: str) -> str:
        if not path:
            return "NOT_CONFIGURED"
        if not path.startswith("/") or path.startswith("//"):
            return "DOWN"
        try:
            response = self._request("GET", path, timeout=2.0)
            return "UP" if response.status_code == 200 and response.json().get("status") == "UP" else "DOWN"
        except (SpringUnavailable, ValueError, AttributeError):
            return "DOWN"

    def cp_profile(self, cp_id: str):
        """Phase 2: what applies to one borrower (dps.profiles.LoanProfile), or None when Spring does not answer."""
        from dps.profiles import LoanProfile

        try:
            resp = self._request("GET", f"/api/v1/credit-proposals/{cp_id}/profile")
        except SpringUnavailable:
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        return LoanProfile.from_core(body) if isinstance(body, dict) else None

    def country_profiles(self) -> list[dict]:
        """Phase 2: the bank's country profiles (Settings > Countries); kept for five minutes. Empty when Spring
        does not answer: the built-in currency words still apply."""
        now = time.monotonic()
        if self._countries is not None and now - self._countries[0] < 300:
            return self._countries[1]
        try:
            resp = self._request("GET", "/api/v1/settings/countries")
        except SpringUnavailable:
            return []
        body = resp.json() if resp.status_code == 200 else None
        profiles = [p for p in body if isinstance(p, dict)] if isinstance(body, list) else []
        if profiles:
            self._countries = (now, profiles)
        return profiles

    def fx_rate(self, from_ccy: str, to_ccy: str, on: date) -> float | None:
        """The bank's FX rate from one currency to another on a date (Settings, FX rates); None when it has none."""
        try:
            resp = self._request("GET", "/api/v1/fx/convert",
                                 params={"amount": "1", "from": from_ccy, "to": to_ccy, "date": on.isoformat()})
        except SpringUnavailable:
            return None
        if resp.status_code != 200:
            return None  # 422 FX_RATE_MISSING
        rate = (resp.json() or {}).get("rate")
        return float(rate) if rate is not None else None
