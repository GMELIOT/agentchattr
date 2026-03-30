"""Regression tests for pair-aware loop guard.

Tests two core scenarios:
1. Legitimate delegation chains across different agent pairs do NOT trigger the guard
2. Actual ping-pong loops between the same pair DO trigger the guard
"""

import pytest
from router import Router


@pytest.fixture
def router():
    return Router(agent_names=["claude", "codex", "gemini"], max_hops=4)


class TestLegitimateChainDoesNotFire:
    """A delegation chain touching multiple agents should never trigger."""

    def test_chain_across_three_agents(self, router):
        """claude->codex->claude->gemini->claude->codex — 6 hops, 3 pairs, no loop."""
        ch = "ops"
        # claude delegates to codex
        assert router.get_targets("claude", "@codex do task A", ch) == ["codex"]
        # codex reports back
        assert router.get_targets("codex", "@claude done", ch) == ["claude"]
        # claude delegates to gemini (new pair — resets)
        assert router.get_targets("claude", "@gemini do task B", ch) == ["gemini"]
        # gemini reports back
        assert router.get_targets("gemini", "@claude done", ch) == ["claude"]
        # claude delegates to codex again (new pair — resets)
        assert router.get_targets("claude", "@codex do task C", ch) == ["codex"]
        # codex reports back
        assert router.get_targets("codex", "@claude done", ch) == ["claude"]
        # Still not paused — all legitimate coordination
        assert not router.is_paused(ch)

    def test_alternating_pairs_never_accumulate(self, router):
        """Alternating between two different pairs resets each time."""
        ch = "ops"
        for _ in range(20):
            # claude->codex pair
            router.get_targets("claude", "@codex task", ch)
            # codex->gemini pair (different!)
            router.get_targets("codex", "@gemini task", ch)
        assert not router.is_paused(ch)


class TestPingPongLoopFires:
    """Same two agents bouncing messages should trigger the guard."""

    def test_same_pair_exceeds_max_hops(self, router):
        """claude<->codex bouncing 5 times with max_hops=4 should fire."""
        ch = "ops"
        # Hops 1-4: under the limit
        for i in range(2):
            assert router.get_targets("claude", "@codex ping", ch) != []
            assert router.get_targets("codex", "@claude pong", ch) != []
        # Hop 5: should be blocked
        result = router.get_targets("claude", "@codex ping again", ch)
        assert result == []
        assert router.is_paused(ch)

    def test_guard_fires_at_exact_threshold(self, router):
        """With max_hops=4, hop 4 succeeds, hop 5 is blocked."""
        ch = "ops"
        results = []
        for i in range(5):
            r = router.get_targets("claude", "@codex msg", ch)
            results.append(r)
        # First 4 succeed
        for r in results[:4]:
            assert r == ["codex"]
        # 5th is blocked
        assert results[4] == []
        assert router.is_paused(ch)

    def test_blocked_messages_stay_blocked(self, router):
        """Once paused, further agent messages are blocked."""
        ch = "ops"
        for _ in range(5):
            router.get_targets("claude", "@codex msg", ch)
        assert router.is_paused(ch)
        # Further messages blocked
        assert router.get_targets("codex", "@claude reply", ch) == []
        assert router.get_targets("claude", "@gemini help", ch) == []


class TestHumanResets:
    """Human messages should always reset the guard."""

    def test_human_message_resets_counter(self, router):
        """Human message mid-loop resets the pair count."""
        ch = "ops"
        # Build up 3 hops
        router.get_targets("claude", "@codex msg", ch)
        router.get_targets("codex", "@claude msg", ch)
        router.get_targets("claude", "@codex msg", ch)
        # Human intervenes
        router.get_targets("guillaume", "@claude continue", ch)
        # Counter is reset — 4 more hops allowed
        for _ in range(4):
            assert router.get_targets("claude", "@codex msg", ch) == ["codex"]
        assert not router.is_paused(ch)

    def test_human_message_unpauses(self, router):
        """Human message after guard fires unpauses the channel."""
        ch = "ops"
        for _ in range(5):
            router.get_targets("claude", "@codex msg", ch)
        assert router.is_paused(ch)
        # Human message unpauses
        router.get_targets("guillaume", "@claude fix it", ch)
        assert not router.is_paused(ch)


class TestContinueCommand:
    """The /continue mechanism should reset all state."""

    def test_continue_resets_everything(self, router):
        """continue_routing clears pair state, count, and pause."""
        ch = "ops"
        for _ in range(5):
            router.get_targets("claude", "@codex msg", ch)
        assert router.is_paused(ch)
        router.continue_routing(ch)
        assert not router.is_paused(ch)
        # Fresh counter — can route again
        assert router.get_targets("claude", "@codex msg", ch) == ["codex"]


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_no_mention_no_routing(self, router):
        """Agent message without @mention doesn't route or count."""
        ch = "ops"
        assert router.get_targets("claude", "just thinking aloud", ch) == []
        assert router._get_ch(ch)["pair_count"] == 0

    def test_self_mention_filtered(self, router):
        """Agent mentioning itself doesn't create a valid target."""
        ch = "ops"
        assert router.get_targets("claude", "@claude note to self", ch) == []

    def test_per_channel_isolation(self, router):
        """Loop in one channel doesn't affect another."""
        for _ in range(5):
            router.get_targets("claude", "@codex msg", "ops")
        assert router.is_paused("ops")
        # Different channel is fine
        assert router.get_targets("claude", "@codex msg", "general") == ["codex"]
        assert not router.is_paused("general")

    def test_guard_emitted_flag(self, router):
        """Guard emitted flag prevents duplicate system messages."""
        ch = "ops"
        for _ in range(5):
            router.get_targets("claude", "@codex msg", ch)
        assert router.is_paused(ch)
        assert not router.is_guard_emitted(ch)
        router.set_guard_emitted(ch)
        assert router.is_guard_emitted(ch)

    def test_max_hops_1(self):
        """With max_hops=1, first agent-to-agent message succeeds, second is blocked."""
        r = Router(agent_names=["claude", "codex"], max_hops=1)
        assert r.get_targets("claude", "@codex msg", "ops") == ["codex"]
        assert r.get_targets("codex", "@claude msg", "ops") == []
        assert r.is_paused("ops")
