"""issue #12：眼镜 HUD 能力策略与显示回执分层（全部 simulated——无真机）。"""

from __future__ import annotations

import pytest

from whale_core.devices.glasses import (
    GlassesPolicyError,
    classify_receipt,
    validate_card,
)


def test_validate_card_normalizes_and_bounds():
    card = validate_card(card_id="c1", title="  会议提醒  ",
                         body="14:00 组会\x00", priority="high", ttl_s=600)
    assert card["title"] == "会议提醒" and "\x00" not in card["body"]
    with pytest.raises(GlassesPolicyError):
        validate_card(card_id="", title="t", body="b")
    with pytest.raises(GlassesPolicyError):
        validate_card(card_id="c", title="t", body="b", priority="urgent")
    with pytest.raises(GlassesPolicyError):
        validate_card(card_id="c", title="t", body="b", ttl_s=99999)
    with pytest.raises(GlassesPolicyError):
        validate_card(card_id="c", title="", body="")


def test_receipt_layering_never_fakes_shown():
    # 手机桥 executed ≠ 眼镜显示
    v = classify_receipt({"status": "executed", "receipt_id": "r1"})
    assert v.state == "bridge_sent"
    # 只有显示可证才报 shown
    v2 = classify_receipt({"status": "executed", "receipt_id": "r2",
                           "display_proof": True})
    assert v2.state == "shown"
    # 无回执/失败/未知：诚实标注
    assert classify_receipt(None).state == "unknown"
    assert classify_receipt({"status": "failed"}).state == "degraded"
    assert classify_receipt({"status": "mystery"}).state == "unknown"
