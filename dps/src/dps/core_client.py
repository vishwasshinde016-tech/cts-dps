"""DPS → Covenant Core calls made by the flows and workers as the SERVICE identity.

C4 reads: covenant library (``GET /api/v1/covenants``) and formula validation
(``POST /api/v1/formulas/validate``). C5: ``POST /api/v1/data-events``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

import httpx

from dps.auth import ROLES_HEADER, SERVICE, USER_HEADER
from dps.service_auth import ClientCredentialsToken, TokenError
from dps.settings import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoreMaster:
    id: str
    name: str
    category: str
    assessment_type: str
    referenced_measures: tuple[str, ...]


class CoreUnavailable(Exception):
    """Core could not be reached or answered 5xx; the caller retries later."""


class CoreClient:
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
    def from_settings(cls, settings: Settings) -> CoreClient:
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
            raise CoreUnavailable(str(exc)) from exc  # the job is retried, as for any Core outage

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {**(kw.pop("headers", None) or {}), **self._auth_headers()}
        try:
            resp = self._client.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise CoreUnavailable(f"{method} {path}: {type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise CoreUnavailable(f"{method} {path}: HTTP {resp.status_code}")
        return resp

    def active_masters(self) -> list[CoreMaster]:
        """All active library covenants (paged read of Core's search endpoint)."""
        out: list[CoreMaster] = []
        page = 0
        while True:
            resp = self._request("GET", "/api/v1/covenants", params={"active": "true", "size": 200, "page": page})
            resp.raise_for_status()
            body = resp.json()
            for c in body.get("content", []):
                if c.get("custom"):
                    continue  # CP-scoped custom covenants are not library entries
                out.append(CoreMaster(str(c["id"]), c.get("name") or "", c.get("category") or "",
                                      c.get("assessmentType") or "AUTO", tuple(c.get("referencedMeasures") or ())))
            if body.get("last", True):
                return out
            page += 1

    def validate_formula(self, expression: str) -> list[str]:
        """Core's error messages for the expression; empty when valid."""
        resp = self._request("POST", "/api/v1/formulas/validate",
                             json={"expression": expression, "requireBoolean": True})
        resp.raise_for_status()
        body = resp.json()
        if body.get("valid"):
            return []
        return [e.get("message") or e.get("code") or "invalid" for e in body.get("errors") or []] or ["invalid"]

    def cp_currency(self, cp_id: str) -> str | None:
        """Money for any country: the CP's loan currency ("LKR"), or None when Core does not answer."""
        try:
            resp = self._request("GET", f"/api/v1/credit-proposals/{cp_id}/currency")
        except CoreUnavailable:
            return None
        if resp.status_code != 200:
            return None
        code = (resp.json() or {}).get("currency")
        return code if isinstance(code, str) and len(code) == 3 else None

    def cp_profile(self, cp_id: str):
        """Phase 2: what applies to one borrower (dps.profiles.LoanProfile), or None when Core does not answer."""
        from dps.profiles import LoanProfile

        try:
            resp = self._request("GET", f"/api/v1/credit-proposals/{cp_id}/profile")
        except CoreUnavailable:
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        return LoanProfile.from_core(body) if isinstance(body, dict) else None

    def country_profiles(self) -> list[dict]:
        """Phase 2: the bank's country profiles (Settings > Countries); kept for five minutes. Empty when Core
        does not answer: the built-in currency words still apply."""
        now = time.monotonic()
        if self._countries is not None and now - self._countries[0] < 300:
            return self._countries[1]
        try:
            resp = self._request("GET", "/api/v1/settings/countries")
        except CoreUnavailable:
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
        except CoreUnavailable:
            return None
        if resp.status_code != 200:
            return None  # 422 FX_RATE_MISSING
        rate = (resp.json() or {}).get("rate")
        return float(rate) if rate is not None else None

    def send_data_event(self, payload: dict) -> dict:
        """C5. 202 (or 200 for a duplicate) is success; 4xx is permanent and raised as HTTPStatusError."""
        resp = self._request("POST", "/api/v1/data-events", json=payload)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
