from evals.live.presets import COMPANION_SMOKE, GLASSES_SMOKE, resolve_preset
from evals.live.scenarios import LIVE_SCENARIOS_BY_ID


def test_presets_resolve_known_ids():
    assert resolve_preset("companion") == COMPANION_SMOKE
    assert resolve_preset("glasses") == GLASSES_SMOKE
    assert resolve_preset("all") == ()
    assert resolve_preset("nope") is None
    for sid in COMPANION_SMOKE + GLASSES_SMOKE:
        assert sid in LIVE_SCENARIOS_BY_ID
