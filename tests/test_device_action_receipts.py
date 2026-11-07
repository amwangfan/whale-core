"""设备层回执校验与旧生活层入口保持相同的行为。"""
import subprocess
import sys

import pytest

from whale_core.devices.actions import ActionReceiptError, validate_action_receipt
from whale_core.devices.agent_failures import UNKNOWN, classify_receipt
from whale_core.life.autonomy import AutonomyError
from whale_core.life.autonomy import validate_action_receipt as legacy_validate


@pytest.mark.parametrize("state", ["received", "accepted", "executed", "failed", "unknown"])
def test_both_entry_points_accept_existing_states(state):
    payload = {"state": state, "action_id": "action-1"}
    assert validate_action_receipt(payload, expect_action_id="action-1") == state
    assert legacy_validate(payload, expect_action_id="action-1") == state


@pytest.mark.parametrize("payload,expected_id", [
    (None, ""),
    ({"state": "invented", "action_id": "action-1"}, ""),
    ({"state": "executed"}, ""),
    ({"state": "executed", "action_id": "action-1"}, "action-2"),
])
def test_invalid_receipts_preserve_legacy_errors_and_unknown_result(payload, expected_id):
    with pytest.raises(ActionReceiptError) as device_error:
        validate_action_receipt(payload, expect_action_id=expected_id)
    with pytest.raises(AutonomyError) as legacy_error:
        legacy_validate(payload, expect_action_id=expected_id)
    assert legacy_error.value.code == device_error.value.code == "invalid-receipt"
    assert str(legacy_error.value) == str(device_error.value)
    assert classify_receipt(payload, expect_action_id=expected_id) == (UNKNOWN, "invalid-receipt")


def test_failure_classifier_import_does_not_load_life_autonomy():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import whale_core.devices.agent_failures; "
         "assert 'whale_core.life.autonomy' not in sys.modules"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
