"""Permission policy engine for auto-allow and always-ask decisions."""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


class PermissionPolicy:
    """Evaluate permission prompts against configured policy rules."""

    def __init__(
        self,
        auto_allow: list[str],
        always_ask: list[str],
        *,
        dry_run: bool = True,
        config_path: Path | None = None,
    ):
        self.dry_run = dry_run
        self.config_path = Path(config_path) if config_path else None
        self._auto_allow_patterns = list(auto_allow)
        self._always_ask_patterns = list(always_ask)
        self._compiled_auto_allow = self._compile_patterns(auto_allow, "auto_allow")
        self._compiled_always_ask = self._compile_patterns(always_ask, "always_ask")

    def _compile_patterns(
        self, patterns: list[str], bucket: str
    ) -> list[tuple[str, re.Pattern[str]]]:
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for pattern in patterns:
            try:
                compiled.append((pattern, re.compile(pattern, re.IGNORECASE)))
            except re.error as exc:
                log.error("[policy] invalid %s regex %r: %s", bucket, pattern, exc)
        return compiled

    def _match(
        self, action: str, compiled: list[tuple[str, re.Pattern[str]]]
    ) -> str | None:
        for pattern, regex in compiled:
            if regex.fullmatch(action):
                return pattern
        return None

    def evaluate(self, action: str) -> dict:
        """Return the policy decision for an action."""
        always_ask_rule = self._match(action, self._compiled_always_ask)
        if always_ask_rule:
            log.info('[policy] always_ask: "%s" matched rule "%s"', action, always_ask_rule)
            return {"decision": "always_ask", "matched_rule": always_ask_rule}

        auto_allow_rule = self._match(action, self._compiled_auto_allow)
        if auto_allow_rule:
            if self.dry_run:
                log.info(
                    '[policy] dry_run auto_allow: "%s" matched rule "%s" -> ask_human',
                    action,
                    auto_allow_rule,
                )
                return {"decision": "ask_human", "matched_rule": auto_allow_rule}
            log.info('[policy] auto_allow: "%s" matched rule "%s"', action, auto_allow_rule)
            return {"decision": "auto_allow", "matched_rule": auto_allow_rule}

        log.info('[policy] ask_human: "%s" matched no rule', action)
        return {"decision": "ask_human", "matched_rule": None}

    def add_auto_allow(self, pattern: str) -> None:
        """Persist a new auto-allow pattern and activate it immediately."""
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("pattern required")
        if pattern in self._auto_allow_patterns:
            return
        self._persist_auto_allow(pattern)
        self._auto_allow_patterns.append(pattern)
        try:
            self._compiled_auto_allow.append((pattern, re.compile(pattern, re.IGNORECASE)))
        except re.error as exc:
            self._auto_allow_patterns.pop()
            raise ValueError(f"invalid regex: {exc}") from exc

    def _persist_auto_allow(self, pattern: str) -> None:
        if not self.config_path:
            raise RuntimeError("config_path required for persistence")
        if self.config_path.exists():
            content = self.config_path.read_text("utf-8")
        else:
            content = ""

        if pattern in content:
            return

        permissions_idx = content.find("[permissions]")
        if permissions_idx == -1:
            addition = (
                "\n[permissions]\n"
                "dry_run = true\n"
                "auto_expire_seconds = 300\n"
                "auto_allow = [\n"
                f'    "{pattern}",\n'
                "]\n"
                "always_ask = []\n"
            )
            self.config_path.write_text(content.rstrip() + addition, "utf-8")
            return

        auto_allow_match = re.search(
            r"(?ms)^auto_allow\s*=\s*\[\n(?P<body>.*?)^\]",
            content[permissions_idx:],
        )
        if auto_allow_match:
            start = permissions_idx + auto_allow_match.start("body")
            end = permissions_idx + auto_allow_match.end("body")
            body = content[start:end]
            insertion = f'    "{pattern}",\n'
            updated = content[:end] + insertion + content[end:]
            self.config_path.write_text(updated, "utf-8")
            return

        section_start = permissions_idx + len("[permissions]\n")
        insertion = 'auto_allow = [\n    "%s",\n]\n' % pattern
        updated = content[:section_start] + insertion + content[section_start:]
        self.config_path.write_text(updated, "utf-8")

    def get_rules(self) -> dict:
        """Return current configured rules."""
        return {
            "dry_run": self.dry_run,
            "auto_allow": list(self._auto_allow_patterns),
            "always_ask": list(self._always_ask_patterns),
        }
