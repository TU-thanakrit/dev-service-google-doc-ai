import subprocess
from datetime import UTC, datetime, timedelta

from google.auth.credentials import Credentials


class GcloudCliCredentials(Credentials):
    """Refreshable credentials backed by the active gcloud CLI account."""

    def refresh(self, request) -> None:
        del request
        self.token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"],
            text=True,
        ).strip()
        self.expiry = datetime.now(UTC) + timedelta(minutes=50)
