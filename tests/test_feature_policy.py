"""Security policy for optional Windows features."""
from __future__ import annotations

import pytest

from winfirst.services.features_service import FeatureInfo, build_change_spec


def _feature(name: str, state: str = "Enabled", kind: str = "Feature") -> FeatureInfo:
    return FeatureInfo(name=name, display_name=name, state=state, kind=kind)


def test_defender_optional_features_cannot_be_disabled() -> None:
    feature = _feature("Windows-Defender-ApplicationGuard")
    with pytest.raises(ValueError, match="Defender"):
        build_change_spec(feature, False)


def test_smb1_cannot_be_enabled() -> None:
    feature = _feature("SMB1Protocol", state="Disabled")
    with pytest.raises(ValueError, match="SMB 1.0"):
        build_change_spec(feature, True)


def test_powershell_v2_cannot_be_enabled() -> None:
    feature = _feature("MicrosoftWindowsPowerShellV2", state="Disabled")
    with pytest.raises(ValueError, match="PowerShell 2.0"):
        build_change_spec(feature, True)


def test_ordinary_feature_change_is_allowed() -> None:
    feature = _feature("NetFx3", state="Disabled")
    spec = build_change_spec(feature, True)
    assert spec.operation_id == "feature.enable.NetFx3"
    assert spec.requires_admin is True
