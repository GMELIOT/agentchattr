"""Slack bridge for Imladris-Engineering inter-team communication.

Polls configured Slack channels, parses structured tags ([REQUEST], [RESPONSE],
[UPDATE], [BLOCKER], [FYI]), tracks threads, logs events, and relays [BLOCKER]
messages to Telegram.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("slack_bridge")

# ---------------------------------------------------------------------------
# Structured tag protocol
# ---------------------------------------------------------------------------

VALID_TAGS = {"REQUEST", "RESPONSE", "UPDATE", "BLOCKER", "FYI"}

_TAG_RE = re.compile(
    r"^\[(" + "|".join(VALID_TAGS) + r")\]\s*(.+?)(?:\s*[-\u2014\u2013]\s*(.+?))?$",
    re.MULTILINE,
)


@dataclass
class ParsedMessage:
    """A parsed inter-team protocol message."""
    tag: str
    title: str
    sender: str
    body: str
    ref: str
    raw: str
    thread_ts: str
    channel: str
    ts: str

    def to_dict(self) -> dict:
        return {
            "tag": self.tag,
            "title": self.title,
            "sender": self.sender,
            "body": self.body,
            "ref": self.ref,
            "thread_ts": self.thread_ts,
            "channel": self.channel,
            "ts": self.ts,
        }


def parse_protocol_message(text: str, channel: str = "", ts: str = "", thread_ts: str = "") -> ParsedMessage | None:
    """Parse a message for structured protocol tags.

    Expected format:
        [TAG] Short title -- Agent name @ timestamp
        Body: 1-5 sentences
        REF: optional reference

    Returns None if the message doesn't match the protocol format.
    """
    if not text:
        return None

    lines = text.strip().split("\n")
    first_line = lines[0].strip()

    # Match [TAG] title -- sender @ timestamp  OR  [TAG] title
    tag_match = re.match(
        r"^\[(" + "|".join(VALID_TAGS) + r")\]\s+(.+)$",
        first_line,
    )
    if not tag_match:
        return None

    tag = tag_match.group(1)
    remainder = tag_match.group(2).strip()

    # Try to split "title -- sender @ timestamp"
    sender = ""
    title = remainder
    dash_match = re.match(r"^(.+?)\s*[-\u2014\u2013]+\s*(.+)$", remainder)
    if dash_match:
        title = dash_match.group(1).strip()
        sender = dash_match.group(2).strip()

    # Extract body and REF from remaining lines
    body_lines: list[str] = []
    ref = ""
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.upper().startswith("REF:"):
            ref = stripped[4:].strip()
        elif stripped:
            body_lines.append(stripped)

    return ParsedMessage(
        tag=tag,
        title=title,
        sender=sender,
        body="\n".join(body_lines),
        ref=ref,
        raw=text,
        thread_ts=thread_ts or ts,
        channel=channel,
        ts=ts,
    )


# ---------------------------------------------------------------------------
# Thread tracker
# ---------------------------------------------------------------------------

class ThreadTracker:
    """Track Slack threads by request title for correct reply threading."""

    def __init__(self) -> None:
        self._threads: dict[str, str] = {}  # normalised title -> thread_ts
        self._lock = threading.Lock()

    def track(self, title: str, thread_ts: str) -> None:
        key = title.strip().lower()
        with self._lock:
            self._threads[key] = thread_ts

    def get_thread(self, title: str) -> str | None:
        key = title.strip().lower()
        with self._lock:
            return self._threads.get(key)

    def to_dict(self) -> dict[str, str]:
        with self._lock:
            return dict(self._threads)


# ---------------------------------------------------------------------------
# Local event logger
# ---------------------------------------------------------------------------

class InterTeamLogger:
    """Append inter-team events to a local markdown log."""

    def __init__(self, log_path: str | Path) -> None:
        self._path = Path(log_path)
        self._lock = threading.Lock()

    def log_event(
        self,
        direction: str,
        tag: str,
        title: str,
        note: str = "",
    ) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M")
        line = f"| {ts} | {direction} | [{tag}] | {title} | {note} |\n"
        with self._lock:
            if not self._path.exists():
                header = "| Timestamp | Direction | Tag | Title | Note |\n|---|---|---|---|---|\n"
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(header)
            with open(self._path, "a") as f:
                f.write(line)


# ---------------------------------------------------------------------------
# Slack API client
# ---------------------------------------------------------------------------

class SlackClient:
    """Minimal Slack Web API client using urllib (no dependencies)."""

    API_BASE = "https://slack.com/api"

    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token.strip()

    def _call(self, method: str, params: dict | None = None, json_body: dict | None = None) -> dict:
        url = f"{self.API_BASE}/{method}"
        headers = {"Authorization": f"Bearer {self.bot_token}"}

        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body).encode("utf-8")
        elif params:
            data = urllib.parse.urlencode(params).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = None

        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read())

        if not result.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {result.get('error', result)}")
        return result

    def resolve_channel_id(self, channel_name: str) -> str | None:
        """Resolve a channel name (without #) to its ID."""
        clean = channel_name.lstrip("#")
        cursor = ""
        while True:
            params: dict[str, str] = {"types": "public_channel,private_channel", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            result = self._call("conversations.list", params=params)
            for ch in result.get("channels", []):
                if ch.get("name") == clean:
                    return ch["id"]
            cursor = result.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        return None

    def fetch_history(self, channel_id: str, oldest: str = "0", limit: int = 50) -> list[dict]:
        """Fetch channel messages newer than oldest timestamp."""
        params = {"channel": channel_id, "oldest": oldest, "limit": str(limit)}
        result = self._call("conversations.history", params=params)
        return result.get("messages", [])

    def post_message(self, channel_id: str, text: str, thread_ts: str = "") -> dict:
        """Post a message to a channel, optionally in a thread."""
        payload: dict = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        result = self._call("chat.postMessage", json_body=payload)
        return result.get("message", {})

    def get_user_info(self, user_id: str) -> dict:
        """Get user display name from user ID."""
        result = self._call("users.info", params={"user": user_id})
        return result.get("user", {})


# ---------------------------------------------------------------------------
# Slack bridge (poller + dispatcher)
# ---------------------------------------------------------------------------

class SlackBridge:
    """Polls Slack channels, parses protocol messages, dispatches events."""

    def __init__(
        self,
        bot_token: str,
        channels: list[str],
        poll_interval: int = 30,
        state_path: str = "data/slack_bridge_state.json",
        log_path: str = "data/inter-team-log.md",
        on_message: Callable[[ParsedMessage], None] | None = None,
        on_blocker: Callable[[ParsedMessage], None] | None = None,
    ) -> None:
        self.client = SlackClient(bot_token)
        self.channel_names = channels
        self.poll_interval = poll_interval
        self.state_path = Path(state_path)
        self.logger = InterTeamLogger(log_path)
        self.threads = ThreadTracker()
        self.on_message = on_message
        self.on_blocker = on_blocker

        self._channel_ids: dict[str, str] = {}  # name -> id
        self._last_ts: dict[str, str] = {}  # channel_id -> last_read_ts
        self._running = False
        self._thread: threading.Thread | None = None

        self._load_state()

    def _load_state(self) -> None:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                self._last_ts = data.get("last_ts", {})
                threads = data.get("threads", {})
                for title, ts in threads.items():
                    self.threads.track(title, ts)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Failed to load slack bridge state: %s", exc)

    def _save_state(self) -> None:
        data = {
            "last_ts": self._last_ts,
            "threads": self.threads.to_dict(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=2))

    def _resolve_channels(self) -> None:
        for name in self.channel_names:
            clean = name.lstrip("#")
            try:
                channel_id = self.client.resolve_channel_id(clean)
                if channel_id:
                    self._channel_ids[clean] = channel_id
                    log.info("Resolved Slack channel #%s -> %s", clean, channel_id)
                else:
                    log.warning("Could not resolve Slack channel #%s", clean)
            except Exception as exc:
                log.warning("Error resolving channel #%s: %s", clean, exc)

    def _poll_channel(self, channel_name: str, channel_id: str) -> list[ParsedMessage]:
        oldest = self._last_ts.get(channel_id, "0")
        try:
            messages = self.client.fetch_history(channel_id, oldest=oldest)
        except Exception as exc:
            log.warning("Error polling #%s: %s", channel_name, exc)
            return []

        if not messages:
            return []

        # Messages come newest-first; reverse for chronological processing
        messages.sort(key=lambda m: float(m.get("ts", "0")))

        parsed: list[ParsedMessage] = []
        for msg in messages:
            ts = msg.get("ts", "")
            # Skip if we've already processed this timestamp
            if ts and float(ts) <= float(oldest):
                continue

            text = msg.get("text", "")
            thread_ts = msg.get("thread_ts", "")
            result = parse_protocol_message(text, channel=channel_name, ts=ts, thread_ts=thread_ts)
            if result:
                parsed.append(result)

            # Update high-water mark
            if ts and (not self._last_ts.get(channel_id) or float(ts) > float(self._last_ts.get(channel_id, "0"))):
                self._last_ts[channel_id] = ts

        return parsed

    def _handle_parsed(self, msg: ParsedMessage) -> None:
        # Track threads for REQUEST messages
        if msg.tag == "REQUEST":
            self.threads.track(msg.title, msg.thread_ts)

        # Log the event
        self.logger.log_event("RECEIVED", msg.tag, msg.title, msg.sender)

        # Dispatch callbacks
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception as exc:
                log.warning("on_message callback failed: %s", exc)

        if msg.tag == "BLOCKER" and self.on_blocker:
            try:
                self.on_blocker(msg)
            except Exception as exc:
                log.warning("on_blocker callback failed: %s", exc)

    def poll_once(self) -> list[ParsedMessage]:
        """Run a single poll cycle across all channels. Returns parsed messages."""
        if not self._channel_ids:
            self._resolve_channels()

        all_parsed: list[ParsedMessage] = []
        for name, channel_id in self._channel_ids.items():
            parsed = self._poll_channel(name, channel_id)
            for msg in parsed:
                self._handle_parsed(msg)
            all_parsed.extend(parsed)

        if all_parsed:
            self._save_state()

        return all_parsed

    def send_message(
        self,
        channel_name: str,
        tag: str,
        title: str,
        sender: str,
        body: str,
        ref: str = "",
        thread_title: str = "",
    ) -> dict:
        """Send a structured protocol message to a Slack channel."""
        clean = channel_name.lstrip("#")
        channel_id = self._channel_ids.get(clean)
        if not channel_id:
            channel_id = self.client.resolve_channel_id(clean)
            if channel_id:
                self._channel_ids[clean] = channel_id

        if not channel_id:
            raise ValueError(f"Cannot resolve channel #{clean}")

        # Build protocol-formatted message
        ts_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        header = f"[{tag}] {title} -- {sender} @ {ts_str}"
        parts = [header, body]
        if ref:
            parts.append(f"REF: {ref}")
        text = "\n".join(parts)

        # Thread if replying to an existing request
        thread_ts = ""
        if thread_title:
            thread_ts = self.threads.get_thread(thread_title) or ""

        result = self.client.post_message(channel_id, text, thread_ts=thread_ts)

        # Track new threads
        if tag == "REQUEST" and result.get("ts"):
            self.threads.track(title, result["ts"])

        # Log the event
        self.logger.log_event("SENT", tag, title, sender)
        self._save_state()

        return result

    def _poll_loop(self) -> None:
        log.info("Slack bridge poller started (interval: %ds)", self.poll_interval)
        while self._running:
            try:
                self.poll_once()
            except Exception as exc:
                log.warning("Slack bridge poll error: %s", exc)
            time.sleep(self.poll_interval)

    def start(self) -> None:
        """Start the background polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="slack-bridge")
        self._thread.start()

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


# ---------------------------------------------------------------------------
# Standalone daemon entry point
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config from config.toml (or config.local.toml override)."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    config_path = Path(__file__).parent / "config.toml"
    local_path = Path(__file__).parent / "config.local.toml"

    cfg: dict = {}
    if config_path.exists():
        cfg = tomllib.loads(config_path.read_text())
    if local_path.exists():
        local = tomllib.loads(local_path.read_text())
        cfg.update(local)
    return cfg


def _build_telegram_relay(cfg: dict) -> Callable[[ParsedMessage], None] | None:
    """Build a Telegram relay callback for BLOCKER and REQUEST messages."""
    perms = cfg.get("permissions", {})
    tg_token = perms.get("telegram_bot_token", "")
    tg_chat_id = perms.get("telegram_chat_id", "")
    if not tg_token or not tg_chat_id:
        return None

    from telegram_notify import TelegramNotifier
    notifier = TelegramNotifier(tg_token, tg_chat_id)

    def relay(msg: ParsedMessage) -> None:
        prefix = "BLOCKER" if msg.tag == "BLOCKER" else "Slack"
        text = (
            f"[{prefix}] {msg.title}\n"
            f"From: {msg.sender or 'unknown'}\n"
            f"Channel: #{msg.channel}\n"
        )
        if msg.body:
            text += f"\n{msg.body[:500]}"
        payload = {"chat_id": tg_chat_id, "text": text}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            log.warning("Telegram relay failed: %s", exc)

    return relay


def main() -> None:
    """Run the Slack bridge as a standalone daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    cfg = _load_config()
    bridge_cfg = cfg.get("slack_bridge", {})

    # Resolve bot token from env var or direct value
    token_env = bridge_cfg.get("bot_token_env", "SLACK_BOT_TOKEN")
    bot_token = os.environ.get(token_env, bridge_cfg.get("bot_token", ""))
    if not bot_token:
        log.error("No Slack bot token found. Set %s env var or bot_token in config.", token_env)
        return

    channels = bridge_cfg.get("channels", ["imladris-engineering"])
    poll_interval = bridge_cfg.get("poll_interval_seconds", 30)

    data_dir = Path(cfg.get("server", {}).get("data_dir", "./data"))

    # Build callbacks
    telegram_relay = _build_telegram_relay(cfg)

    def on_message(msg: ParsedMessage) -> None:
        log.info("Received [%s] %s from %s in #%s", msg.tag, msg.title, msg.sender, msg.channel)

    def on_blocker(msg: ParsedMessage) -> None:
        log.warning("BLOCKER: %s -- %s", msg.title, msg.body[:200])
        if telegram_relay:
            telegram_relay(msg)

    def on_request(msg: ParsedMessage) -> None:
        if msg.tag == "REQUEST" and telegram_relay:
            telegram_relay(msg)

    def on_any(msg: ParsedMessage) -> None:
        on_message(msg)
        if msg.tag == "REQUEST":
            on_request(msg)

    bridge = SlackBridge(
        bot_token=bot_token,
        channels=channels,
        poll_interval=poll_interval,
        state_path=str(data_dir / "slack_bridge_state.json"),
        log_path=str(data_dir / "inter-team-log.md"),
        on_message=on_any,
        on_blocker=on_blocker,
    )

    log.info("Slack bridge starting -- channels: %s, interval: %ds", channels, poll_interval)
    log.info("Telegram relay: %s", "enabled" if telegram_relay else "disabled")

    bridge._resolve_channels()
    if not bridge._channel_ids:
        log.error("No channels resolved. Check bot token permissions and channel names.")
        return

    log.info("Resolved %d channel(s). Entering poll loop.", len(bridge._channel_ids))

    try:
        bridge._running = True
        bridge._poll_loop()
    except KeyboardInterrupt:
        log.info("Slack bridge stopped by user.")
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
