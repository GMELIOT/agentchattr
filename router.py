"""Message routing based on @mentions with per-channel pair-aware loop guard."""

import re


class Router:
    def __init__(self, agent_names: list[str], default_mention: str = "both",
                 max_hops: int = 4, online_checker=None):
        self.agent_names = set(n.lower() for n in agent_names)
        self.default_mention = default_mention
        self.max_hops = max_hops
        self._online_checker = online_checker  # callable() -> set of online agent names
        # Per-channel state: { channel: { pair_count, paused, guard_emitted, last_pair } }
        self._channels: dict[str, dict] = {}
        self._build_pattern()

    def _get_ch(self, channel: str) -> dict:
        if channel not in self._channels:
            self._channels[channel] = {
                "pair_count": 0,
                "paused": False,
                "guard_emitted": False,
                "last_pair": None,  # frozenset of the two agents in the current exchange
            }
        return self._channels[channel]

    def _build_pattern(self):
        # Sort longest-first so "gemini-2" is tried before "gemini"
        names = "|".join(re.escape(n) for n in sorted(self.agent_names, key=len, reverse=True))
        self._mention_re = re.compile(
            rf"@({names}|both|all)\b", re.IGNORECASE
        )

    def parse_mentions(self, text: str) -> list[str]:
        mentions = set()
        for match in self._mention_re.finditer(text):
            name = match.group(1).lower()
            if name in ("both", "all"):
                # Only tag online agents when using @all
                if self._online_checker:
                    online = self._online_checker()
                    mentions.update(n for n in self.agent_names if n in online)
                else:
                    mentions.update(self.agent_names)
            else:
                mentions.add(name)
        return list(mentions)

    def _is_agent(self, sender: str) -> bool:
        return sender.lower() in self.agent_names

    def get_targets(self, sender: str, text: str, channel: str = "general") -> list[str]:
        """Determine which agents should receive this message."""
        ch = self._get_ch(channel)
        mentions = self.parse_mentions(text)

        if not self._is_agent(sender):
            # Human message resets state and unpauses
            ch["pair_count"] = 0
            ch["paused"] = False
            ch["guard_emitted"] = False
            ch["last_pair"] = None
            if not mentions:
                if self.default_mention in ("both", "all"):
                    return list(self.agent_names)
                elif self.default_mention == "none":
                    return []
                return [self.default_mention]
            return mentions
        else:
            # Agent message: blocked while loop guard is active
            if ch["paused"]:
                return []
            # Only route if explicit @mention
            if not mentions:
                return []
            # Filter self-mentions to get actual targets
            targets = [m for m in mentions if m != sender]
            if not targets:
                return []
            # Build the pair for this exchange (sender + primary target)
            current_pair = frozenset({sender.lower(), targets[0].lower()})
            if current_pair == ch["last_pair"]:
                # Same pair bouncing — increment
                ch["pair_count"] += 1
            else:
                # New pair entered — reset counter
                ch["pair_count"] = 1
                ch["last_pair"] = current_pair
            if ch["pair_count"] > self.max_hops:
                ch["paused"] = True
                return []
            return targets

    def continue_routing(self, channel: str = "general"):
        """Resume after loop guard pause."""
        ch = self._get_ch(channel)
        ch["pair_count"] = 0
        ch["paused"] = False
        ch["guard_emitted"] = False
        ch["last_pair"] = None

    def is_paused(self, channel: str = "general") -> bool:
        return self._get_ch(channel)["paused"]

    def is_guard_emitted(self, channel: str = "general") -> bool:
        return self._get_ch(channel)["guard_emitted"]

    def set_guard_emitted(self, channel: str = "general"):
        self._get_ch(channel)["guard_emitted"] = True

    def update_agents(self, names: list[str]):
        """Replace the agent name set and rebuild the mention regex."""
        self.agent_names = set(n.lower() for n in names)
        self._build_pattern()
