"""Tests for the production identity adapter: ID-token verification and team mapping.

Offline: google-auth's network signature check is replaced by a stub, so these
tests prove the adapter's own rules — header handling, fail-closed verification,
and mapping — not that a real Google-signed token verifies against Google's keys.
That is deployment evidence (`docs/ocr-v1-contracts.md`, B2). No cloud call is made.
"""

import pytest
from fastapi import Request

from app import ocr_auth
from app.ocr_auth import GoogleIdTokenVerifier
from app.ocr_tracer import MappingTeamDirectory, Principal

AUDIENCE = "https://dev-ocr-service-api-example.asia-southeast1.run.app"
SERVICE_ACCOUNT = "n8n-ocr-caller@thaiunion-ai-platform.iam.gserviceaccount.com"
# The service account's own `sub` claim: an allowlist entry binds the pair of
# verified claims, never the email alone (§7.3).
SUBJECT = "1029384756"
TEAM_KEY = f"{SERVICE_ACCOUNT}|{SUBJECT}"


def make_request(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/ocr/jobs",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        }
    )


def stub_verification(monkeypatch, claims=None, error=None) -> dict:
    """Replace google-auth's network verification and record how it was called."""
    seen: dict = {}

    def fake_verify(token, request, audience=None, clock_skew_in_seconds=0):
        seen.update(token=token, audience=audience, clock_skew=clock_skew_in_seconds)
        if error is not None:
            raise error
        return claims

    monkeypatch.setattr(ocr_auth.id_token, "verify_oauth2_token", fake_verify)
    return seen


def test_verified_token_yields_the_principal_and_pins_the_audience(monkeypatch):
    seen = stub_verification(
        monkeypatch,
        claims={
            "iss": "https://accounts.google.com",
            "email": SERVICE_ACCOUNT,
            "sub": "1029384756",
        },
    )

    principal = GoogleIdTokenVerifier(audience=AUDIENCE).verify(
        make_request(Authorization="Bearer signed-id-token")
    )

    assert principal == Principal(email=SERVICE_ACCOUNT, subject="1029384756")
    # Signature, issuer, audience and expiry are google-auth's checks (D12): the
    # adapter passes the pinned audience and the contract's 60 s skew.
    assert seen == {"token": "signed-id-token", "audience": AUDIENCE, "clock_skew": 60}


def test_failed_verification_is_unauthenticated_rather_than_an_error(monkeypatch):
    stub_verification(monkeypatch, error=ValueError("Token expired"))

    assert (
        GoogleIdTokenVerifier(audience=AUDIENCE).verify(
            make_request(Authorization="Bearer expired-token")
        )
        is None
    )


def test_only_an_authorization_bearer_header_is_read(monkeypatch):
    seen = stub_verification(monkeypatch, claims={})

    verifier = GoogleIdTokenVerifier(audience=AUDIENCE)
    assert verifier.verify(make_request()) is None
    assert verifier.verify(make_request(Authorization="Basic dXNlcjpwYXNz")) is None
    # Cloud Run strips this header's signature, so it must never be verified here.
    assert (
        verifier.verify(
            make_request(**{"X-Serverless-Authorization": "Bearer signed-id-token"})
        )
        is None
    )
    # Identity headers are not credentials, whatever they claim.
    assert (
        verifier.verify(
            make_request(
                **{
                    "X-Goog-Authenticated-User-Email": f"accounts.google.com:{SERVICE_ACCOUNT}",
                    "X-Forwarded-User": "someone-else@example.com",
                }
            )
        )
        is None
    )
    assert seen == {}


def test_verified_identity_maps_to_a_stable_internal_team(monkeypatch):
    stub_verification(
        monkeypatch,
        claims={"iss": "accounts.google.com", "email": SERVICE_ACCOUNT, "sub": SUBJECT},
    )
    directory = MappingTeamDirectory({TEAM_KEY: "team-ops"})
    verified = GoogleIdTokenVerifier(audience=AUDIENCE).verify(
        make_request(Authorization="Bearer signed-id-token")
    )

    assert directory.team_id(verified) == "team-ops"
    # Ownership never comes from anything but the verified principal.
    assert directory.team_id(Principal(email="other@example.com", subject=SUBJECT)) is None


def test_a_reused_email_with_another_subject_is_not_authorized(monkeypatch):
    # The same service-account email with a different `sub`: a forged subject
    # cannot borrow an allowlisted email's team, so the lookup is unauthorized.
    stub_verification(
        monkeypatch,
        claims={"iss": "accounts.google.com", "email": SERVICE_ACCOUNT, "sub": "999999999"},
    )
    directory = MappingTeamDirectory({TEAM_KEY: "team-ops"})
    verified = GoogleIdTokenVerifier(audience=AUDIENCE).verify(
        make_request(Authorization="Bearer signed-id-token")
    )

    assert verified == Principal(email=SERVICE_ACCOUNT, subject="999999999")
    assert directory.team_id(Principal(email=SERVICE_ACCOUNT, subject="999999999")) is None


def test_an_allowlist_key_that_is_not_an_email_and_subject_pair_is_refused():
    # Strict parsing: a binding must carry both verified claims, so a mapping
    # written as an email alone cannot silently authorize that email.
    malformed = (
        {SERVICE_ACCOUNT: "team-ops"},  # the email alone carries no subject
        {"|x": "team-ops"},  # no email
        {f"{SERVICE_ACCOUNT}|": "team-ops"},  # empty subject
    )
    for binding in malformed:
        with pytest.raises(ValueError):
            MappingTeamDirectory(binding)
