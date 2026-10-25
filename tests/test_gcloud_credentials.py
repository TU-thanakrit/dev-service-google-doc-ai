from datetime import UTC, datetime, timedelta

from app.gcloud_credentials import GcloudCliCredentials


def test_gcloud_credentials_refreshes_with_short_lived_cli_token(monkeypatch):
    calls = []

    def fake_check_output(command, text):
        calls.append((command, text))
        return "fresh-token\n"

    monkeypatch.setattr("app.gcloud_credentials.subprocess.check_output", fake_check_output)
    credentials = GcloudCliCredentials()

    credentials.refresh(request=None)

    assert credentials.token == "fresh-token"
    assert credentials.expiry is not None
    assert datetime.now(UTC) + timedelta(minutes=40) < credentials.expiry
    assert calls == [(["gcloud", "auth", "print-access-token"], True)]