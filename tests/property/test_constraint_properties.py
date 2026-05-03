"""
Property-based tests for nl2protocol.validation.constraints.

Each property here is a direct translation of an existing contract clause
(see the module's function docstrings) into a Hypothesis universal statement:
"for any input satisfying Pre, this Post property must hold."

Phase 1 covers helpers that don't need a generated `ProtocolSpec`:
  - get_pipette_range
  - get_pipette_for_volume
  - get_all_pipette_ranges
  - WellState.add
  - WellState.remove

Phase 2 (separate, future) will cover ConstraintChecker.check_all once
ProtocolSpec strategies are written.
"""

import string

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from nl2protocol.validation.constraints import (
    PIPETTE_SPECS,
    WellState,
    get_all_pipette_ranges,
    get_pipette_for_volume,
    get_pipette_range,
)


# Mutmut invokes each property test repeatedly (once per mutant). Hypothesis
# flags that as "differing executors" and aborts. Suppress only that one check
# so mutation testing works; other Hypothesis health checks remain on.
settings.register_profile(
    "mutmut_compatible",
    suppress_health_check=[HealthCheck.differing_executors],
)
settings.load_profile("mutmut_compatible")


# ============================================================================
# Strategy helpers
# ============================================================================

# Suffix segments for pipette load-names: lowercase letters + digits + underscore.
# Keep min_size >= 1 so we always end with non-empty content after "<spec>_".
_pipette_suffix = st.text(alphabet=string.ascii_lowercase + string.digits + "_", min_size=1)

# Arbitrary strings that may or may not match a pipette pattern.
_any_string = st.text(min_size=0, max_size=40)

# Substance labels for WellState properties.
_substance = st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=20)

# Non-negative volumes for WellState properties.
_volume = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)


def _starts_with_recognized_spec(s: str) -> bool:
    """True iff s.lower() starts with any "<spec>_" prefix from PIPETTE_SPECS."""
    s_low = s.lower()
    return any(s_low.startswith(spec + "_") for spec in PIPETTE_SPECS)


# ============================================================================
# get_pipette_range — properties
# ============================================================================

class TestGetPipetteRangeProperties:
    """Properties for get_pipette_range (translated from its Post: clause)."""

    # Post: returns PIPETTE_SPECS[spec] iff model.lower() starts with "<spec>_"
    @given(spec=st.sampled_from(list(PIPETTE_SPECS)), suffix=_pipette_suffix)
    def test_returns_correct_range_for_any_recognized_prefix(self, spec, suffix):
        model = f"{spec}_{suffix}"
        assert get_pipette_range(model) == PIPETTE_SPECS[spec]

    # Post: case-insensitive (uses model.lower())
    @given(spec=st.sampled_from(list(PIPETTE_SPECS)), suffix=_pipette_suffix)
    def test_uppercase_is_equivalent_to_lowercase(self, spec, suffix):
        assert get_pipette_range(f"{spec}_{suffix}".upper()) == PIPETTE_SPECS[spec]

    # Post: returns None for any string with no recognized prefix
    @given(s=_any_string)
    def test_returns_none_for_unrecognized_strings(self, s):
        assume(not _starts_with_recognized_spec(s))
        assert get_pipette_range(s) is None


# ============================================================================
# get_pipette_for_volume — properties
# ============================================================================

# A strategy for valid (mount, model) entries.
_pipette_entry = st.fixed_dictionaries({
    "model": st.sampled_from([f"{spec}_single_gen2" for spec in PIPETTE_SPECS])
})

# A strategy for valid configs with 1-2 pipettes.
_pipettes_config = st.dictionaries(
    keys=st.sampled_from(["left", "right"]),
    values=_pipette_entry,
    min_size=1,
    max_size=2,
)

_volume_for_lookup = st.floats(
    min_value=-1000.0, max_value=2000.0, allow_nan=False, allow_infinity=False
)


class TestGetPipetteForVolumeProperties:
    """Properties for get_pipette_for_volume (translated from its Post: clause)."""

    # Soundness: if returns mount m, then m's range covers volume.
    @given(volume=_volume_for_lookup, pipettes=_pipettes_config)
    def test_returned_mount_covers_volume(self, volume, pipettes):
        config = {"pipettes": pipettes}
        result = get_pipette_for_volume(volume, config)
        if result is not None:
            mount_range = get_pipette_range(pipettes[result]["model"])
            assert mount_range[0] <= volume <= mount_range[1]

    # Completeness: if at least one configured pipette covers volume,
    # the function returns a non-None mount (one whose range covers volume).
    @given(volume=_volume_for_lookup, pipettes=_pipettes_config)
    def test_returns_some_mount_when_any_covers(self, volume, pipettes):
        config = {"pipettes": pipettes}
        any_covers = any(
            (r := get_pipette_range(pip["model"])) is not None
            and r[0] <= volume <= r[1]
            for pip in pipettes.values()
        )
        result = get_pipette_for_volume(volume, config)
        if any_covers:
            assert result is not None
        else:
            assert result is None


# ============================================================================
# get_all_pipette_ranges — properties
# ============================================================================

class TestGetAllPipetteRangesProperties:
    """Properties for get_all_pipette_ranges (translated from its Post: clause)."""

    # Post: every result entry equals get_pipette_range(config[mount]["model"])
    @given(pipettes=_pipettes_config)
    def test_each_entry_matches_get_pipette_range(self, pipettes):
        config = {"pipettes": pipettes}
        result = get_all_pipette_ranges(config)
        for mount, rng in result.items():
            assert rng == get_pipette_range(pipettes[mount]["model"])

    # Post: every mount with a recognized model appears in result
    @given(pipettes=_pipettes_config)
    def test_every_recognized_mount_appears_in_result(self, pipettes):
        config = {"pipettes": pipettes}
        result = get_all_pipette_ranges(config)
        for mount, pip in pipettes.items():
            if get_pipette_range(pip["model"]) is not None:
                assert mount in result


# ============================================================================
# WellState.add — properties
# ============================================================================

class TestWellStateAddProperties:
    """Properties for WellState.add (translated from its Post: clause)."""

    # Post: After any sequence of add(v_i, _), volume_ul == sum(v_i)
    @given(volumes=st.lists(_volume, min_size=0, max_size=20))
    def test_cumulative_volume_equals_sum(self, volumes):
        ws = WellState()
        for v in volumes:
            ws.add(v, "test_substance")
        assert ws.volume_ul == pytest.approx(sum(volumes))

    # Post: substances == deduplicated non-empty list, preserving first-seen order
    @given(substances=st.lists(_substance, min_size=0, max_size=20))
    def test_substances_dedup_preserves_first_seen_order(self, substances):
        ws = WellState()
        for s in substances:
            ws.add(1.0, s)

        # Expected: walk substances, append each non-empty one not already seen.
        expected = []
        seen = set()
        for s in substances:
            if s and s not in seen:
                expected.append(s)
                seen.add(s)
        assert ws.substances == expected


# ============================================================================
# WellState.remove — properties
# ============================================================================

class TestWellStateRemoveProperties:
    """Properties for WellState.remove (translated from its Post: clause)."""

    # Post: for any v <= initial + 0.01, remove(v) returns True and
    # decreases volume_ul to max(0, initial - v)
    @given(initial=_volume, v=_volume)
    def test_within_tolerance_returns_true_and_clamps(self, initial, v):
        assume(v <= initial + 0.01)
        ws = WellState(volume_ul=initial)
        ok = ws.remove(v)
        assert ok is True
        assert ws.volume_ul == pytest.approx(max(0.0, initial - v))

    # Post: for any v > initial + 0.01, remove(v) returns False and leaves
    # volume_ul unchanged.
    @given(initial=_volume, v=_volume)
    def test_outside_tolerance_returns_false_and_unchanged(self, initial, v):
        assume(v > initial + 0.01)
        ws = WellState(volume_ul=initial)
        ok = ws.remove(v)
        assert ok is False
        assert ws.volume_ul == initial

    # Post: substances is never mutated by remove (success or failure)
    @given(initial=_volume, v=_volume, subs=st.lists(_substance, max_size=10))
    def test_substances_never_mutated_by_remove(self, initial, v, subs):
        ws = WellState(volume_ul=initial)
        for s in subs:
            ws.add(0.0, s)
        before = list(ws.substances)
        ws.remove(v)
        assert ws.substances == before

    # Derived (round-trip): add(delta) then remove(delta) restores volume_ul to
    # `initial` modulo float precision. Hypothesis found that strict equality
    # fails for large delta + small initial due to float drift in the add step
    # (e.g. (1e-5 + 131072) - 131072 ≠ 1e-5 exactly). The contract promises
    # mathematical correctness, which under float64 means ~1e-6 relative
    # tolerance — pinned here to make the property testable in practice.
    @given(initial=_volume, delta=_volume)
    def test_add_then_remove_round_trip(self, initial, delta):
        ws = WellState(volume_ul=initial)
        ws.add(delta, "")  # empty substance, doesn't get tracked
        ws.remove(delta)
        assert ws.volume_ul == pytest.approx(initial, rel=1e-6, abs=1e-6)
