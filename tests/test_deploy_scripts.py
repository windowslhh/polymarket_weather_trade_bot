"""FIX-2P-7: deploy scripts must chown both .env AND data/ to UID 1000.

Pre-fix the scripts only chowned data/ — fresh deploys left .env owned
by root, and the container (running as appuser UID 1000) succeeded only
because chmod 600 + accidental ownership alignment let it through.  The
runbook prescribed a manual chown step; this test pins the scripts so
that step is automated and a future refactor can't silently drop it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script_name", [
    "full_reset_and_deploy.sh",
    "backup_and_reset.sh",
])
def test_deploy_script_chowns_env_and_data_to_1000(script_name: str) -> None:
    body = (ROOT / "scripts" / script_name).read_text()
    assert 'chown 1000:1000 "$BOT_DIR/.env"' in body, (
        f"{script_name}: missing chown 1000:1000 of .env (FIX-2P-7)."
    )
    assert 'chown -R 1000:1000 "$BOT_DIR/data"' in body, (
        f"{script_name}: missing chown -R 1000:1000 of data/ (FIX-2P-7)."
    )
