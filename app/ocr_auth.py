"""Production caller authentication for OCR v1: Google ID token -> internal team.

`GoogleIdTokenVerifier` is the real authentication adapter (contract §7, D12): the
token's signature, issuer, audience and `exp`/`iat` are verified in-process by
google-auth, so no identity header is ever trusted. The team allowlist
(`MappingTeamDirectory`) lives with the tracer's seam vocabulary because it is
independent of both GCP and the eventual Entra identity.

This adapter has no deployed composition yet: the tracer is still unmounted, and
the n8n token path plus Cloud Run IAM remain deployment evidence (§7, B2).
"""

from fastapi import Request
from google.auth.transport.requests import Request as GoogleTokenRequest
from google.oauth2 import id_token

from app.ocr_tracer import Principal

# Contract §7: `exp` and `iat` are accepted with 60 s of clock skew.
CLOCK_SKEW_SECONDS = 60


class GoogleIdTokenVerifier:
    """Verify `Authorization: Bearer <id_token>` in-process; fail closed on any doubt."""

    def __init__(
        self, *, audience: str, clock_skew_seconds: int = CLOCK_SKEW_SECONDS
    ) -> None:
        if not audience:
            # Without a pinned audience google-auth would skip the `aud` check.
            raise ValueError("an audience is required to verify ID tokens")
        self._audience = audience
        self._clock_skew_seconds = clock_skew_seconds
        self._google_request = GoogleTokenRequest()

    def verify(self, request: Request) -> Principal | None:
        # Only the presented token counts. `X-Serverless-Authorization` is not
        # read because Cloud Run strips its signature (§7.1).
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        try:
            claims = id_token.verify_oauth2_token(
                token,
                self._google_request,
                self._audience,
                clock_skew_in_seconds=self._clock_skew_seconds,
            )
        except Exception:
            # Fail closed, log nothing: an exception text can carry the token (D20).
            return None
        email, subject = claims.get("email"), claims.get("sub")
        if not email or not subject:
            return None
        return Principal(email=email, subject=subject)
