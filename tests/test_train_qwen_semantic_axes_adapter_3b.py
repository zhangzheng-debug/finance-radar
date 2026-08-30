from __future__ import annotations

from scripts import train_qwen_semantic_axes_adapter as driver
from scripts import train_qwen_semantic_axes_adapter_3b as wrapper


def test_configures_only_the_pinned_3b_base_profile(monkeypatch) -> None:
    monkeypatch.setattr(driver, "EXPECTED_BASE_MODEL_ID", "old")
    monkeypatch.setattr(driver, "EXPECTED_QWEN_PROFILE", {"old": True})
    wrapper.configure_driver()
    assert driver.EXPECTED_BASE_MODEL_ID == "Qwen/Qwen2.5-3B-Instruct"
    assert driver.EXPECTED_QWEN_PROFILE == wrapper.MODEL_PROFILE
