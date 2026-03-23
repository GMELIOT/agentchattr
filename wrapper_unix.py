"""Mac/Linux agent injection — uses tmux send-keys to type into the agent CLI.

Called by wrapper.py on Mac and Linux. Requires tmux to be installed.
  - Mac:   brew install tmux
  - Linux: apt install tmux  (or yum, pacman, etc.)

How it works:
  1. Creates a tmux session running the agent CLI
  2. Queue watcher sends keystrokes via 'tmux send-keys'
  3. Wrapper attaches to the session so you see the full TUI
  4. Ctrl+B, D to detach (agent keeps running in background)
"""

import re
import shlex
import shutil
import subprocess
import sys
import time


def _session_exists(session_name: str) -> bool:
    """Return True while the tmux session is still alive."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def _check_tmux():
    """Verify tmux is installed, exit with helpful message if not."""
    if shutil.which("tmux"):
        return
    print("\n  Error: tmux is required for auto-trigger on Mac/Linux.")
    if sys.platform == "darwin":
        print("  Install: brew install tmux")
    else:
        print("  Install: apt install tmux  (or yum/pacman equivalent)")
    sys.exit(1)


def inject(text: str, *, tmux_session: str, delay: float = 0.3):
    """Send text + Enter to a tmux session via send-keys."""
    # Use -l to send text literally (avoids misinterpreting as key names),
    # then send Enter as a separate key press
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "-l", text],
        capture_output=True,
    )
    # Scale delay with text length so longer prompts get more processing time
    time.sleep(max(delay, len(text) * 0.001))
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "Enter"],
        capture_output=True,
    )


def get_activity_checker(session_name, trigger_flag=None):
    """Return a callable that detects tmux pane output by hashing content."""
    last_hash = [None]

    def check():
        # External trigger: queue watcher injected a message
        if trigger_flag is not None and trigger_flag[0]:
            trigger_flag[0] = False
            return True
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p"],
                capture_output=True, timeout=2,
            )
            h = hash(result.stdout)
            changed = last_hash[0] is not None and h != last_hash[0]
            last_hash[0] = h
            return changed
        except Exception:
            return False

    return check


# ---------------------------------------------------------------------------
# Permission prompt detection
# ---------------------------------------------------------------------------

# Patterns that indicate a CLI is waiting for user approval.
# Each entry: (regex_pattern, agent_hint, option_extractor)
PERMISSION_PATTERNS = [
    # Claude Code: "Do you want to ..." with numbered options
    (r"Do you want to (.+\?)", "claude"),
    # Codex: "Would you like to make the following edits?"
    (r"Would you like to make the following edits\?", "codex"),
    # Gemini: "Action Required" followed by "Apply this change?"
    (r"Action Required", "gemini"),
]

# Map of key labels to the actual keystroke to send
KEYSTROKE_MAP = {
    # Claude Code
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    # Codex uses y/a/Escape
    "y": "y",
    "a": "a",
    # Gemini uses numbers
}


def capture_pane(session_name: str) -> str:
    """Capture the current tmux pane content."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def detect_permission_prompt(pane_content: str) -> dict | None:
    """Scan pane content for a permission prompt.

    Returns a dict with prompt details if found, None otherwise:
        {
            "action": "Do you want to create test-permission.txt?",
            "agent_hint": "claude",
            "options": [{"key": "1", "label": "Yes"}, ...],
            "raw_block": "<full prompt text>"
        }
    """
    if not pane_content.strip():
        return None

    for pattern, agent_hint in PERMISSION_PATTERNS:
        match = re.search(pattern, pane_content)
        if not match:
            continue

        # Extract the prompt block starting from the match
        block_start = pane_content[match.start():]
        lines = block_start.split("\n")

        action = lines[0].strip()
        options = []

        for line in lines[1:]:
            stripped = line.strip()
            # Match numbered options: "1. Yes" or "> 1. Yes" or "● 1. Allow once"
            opt_match = re.match(r"[>●]?\s*(\d+)\.\s+(.+)", stripped)
            if opt_match:
                options.append({
                    "key": opt_match.group(1),
                    "label": opt_match.group(2).strip(),
                })
            # Codex uses (y) (a) (esc) format
            elif re.match(r".*\(([yYaA])\)\s*$", stripped):
                key_match = re.search(r"\(([yYaA])\)", stripped)
                if key_match:
                    key = key_match.group(1).lower()
                    label = re.sub(r"\s*\([yYaA]\)\s*$", "", stripped)
                    label = re.sub(r"^[>●]?\s*\d+\.\s*", "", label)
                    options.append({"key": key, "label": label.strip()})
            elif stripped == "" and options:
                # Blank line after options = end of option block
                break

        if not options:
            continue

        return {
            "action": action,
            "agent_hint": agent_hint,
            "options": options,
            "raw_block": "\n".join(lines[:20]),  # cap at 20 lines
        }

    return None


def inject_keystroke(session_name: str, key: str):
    """Send a single keystroke to the tmux session to respond to a permission prompt."""
    # For Escape key
    if key.lower() in ("esc", "escape"):
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Escape"],
            capture_output=True,
        )
        return

    # For regular keys (1, 2, 3, y, a, etc.)
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, key],
        capture_output=True,
    )


def run_agent(
    command,
    extra_args,
    cwd,
    env,
    queue_file,
    agent,
    no_restart,
    start_watcher,
    strip_env=None,
    pid_holder=None,
    session_name=None,
    inject_env=None,
    inject_delay: float = 0.3,
):
    """Run agent inside a tmux session, inject via tmux send-keys."""
    _check_tmux()

    session_name = session_name or f"agentchattr-{agent}"
    agent_cmd = " ".join(
        [shlex.quote(command)] + [shlex.quote(a) for a in extra_args]
    )

    # Build env(1) prefix for the command INSIDE the tmux session.
    # subprocess.run(env=...) only affects the tmux client binary — the
    # session shell inherits from the tmux server instead.  Use env(1)
    # to set (-u to unset, VAR=val to inject) vars in the actual session.
    env_parts = []
    if strip_env:
        env_parts.extend(f"-u {shlex.quote(v)}" for v in strip_env)
    if inject_env:
        env_parts.extend(
            f"{shlex.quote(k)}={shlex.quote(v)}"
            for k, v in inject_env.items()
        )
    if env_parts:
        agent_cmd = f"env {' '.join(env_parts)} {agent_cmd}"

    # Resolve cwd to absolute path (tmux -c needs it)
    from pathlib import Path
    abs_cwd = str(Path(cwd).resolve())

    # Wire up injection with the tmux session name
    inject_fn = lambda text: inject(text, tmux_session=session_name, delay=inject_delay)
    start_watcher(inject_fn)

    print(f"  Using tmux session: {session_name}")
    print(f"  Detach: Ctrl+B, D  (agent keeps running)")
    print(f"  Reattach: tmux attach -t {session_name}\n")

    while True:
        try:
            # Clean up stale session from a previous crash
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )

            # Create tmux session running the agent CLI
            result = subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name,
                 "-c", abs_cwd, agent_cmd],
                env=env,
            )
            if result.returncode != 0:
                print(f"  Error: failed to create tmux session (exit {result.returncode})")
                break

            # Attach — blocks until agent exits or user detaches (Ctrl+B, D)
            subprocess.run(["tmux", "attach-session", "-t", session_name])

            # Check: did the agent exit, or did the user just detach?
            if _session_exists(session_name):
                # Session still alive — user detached, agent running in background.
                # Keep the wrapper alive so the local proxy and heartbeats survive.
                print(f"\n  Detached. {agent.capitalize()} still running in tmux.")
                print(f"  Reattach: tmux attach -t {session_name}")
                while _session_exists(session_name):
                    time.sleep(1)
                break

            # Session gone — agent exited
            if no_restart:
                break

            print(f"\n  {agent.capitalize()} exited.")
            print(f"  Restarting in 3s... (Ctrl+C to quit)")
            time.sleep(3)
        except KeyboardInterrupt:
            # Kill the tmux session on Ctrl+C
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
            break
