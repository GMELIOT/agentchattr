# Permission Prompt Matrix

All known permission prompt patterns across supported CLI tools.

This file is the maintained source of truth for permission prompt detection in
`wrapper_unix.py`. Every wrapper/parser change must update this matrix.

## Claude Code (Anthropic)

| Pattern (regex) | Category | Option Format | Keys | Confidence | Last Verified |
|-----------------|----------|---------------|------|------------|---------------|
| `Do you want to [\s\S]+?\?` | Tool use | Numbered options | `1`/`2`/`3` + Enter | Live capture | 2026-03-28 |
| `Allow .+ to run tool .+\?` | MCP tool permission | Numbered options | `1`/`2`/`3`/`4` + Enter | Live capture | 2026-03-28 |

Notes:
- Claude prompts often include tool context above the question, such as `Bash command`,
  `Edit file`, or `Write file`.
- Anthropic docs confirm the permission families and MCP approval flow, but do not
  publish a canonical list of every literal prompt string.

## Codex CLI (OpenAI)

| Pattern (regex) | Category | Option Format | Keys | Confidence | Last Verified |
|-----------------|----------|---------------|------|------------|---------------|
| `Would you like to make the following edits\?` | File edit | Inline options | `y`/`a`/`esc` | Live capture | 2026-03-27 |
| `Would you like to run the following command\?` | Command execution | Numbered options with inline key hints | `y`/`esc` | Live capture | 2026-03-28 |
| `Allow command\?` | Command execution | Unknown | Unknown | Upstream issues | Unverified |

Notes:
- OpenAI docs confirm approval modes, but do not publish a canonical prompt-string
  catalog.
- Exact phrasings above come from live captures and upstream issue examples.

## Gemini CLI (Google)

| Pattern (regex) | Category | Option Format | Keys | Confidence | Last Verified |
|-----------------|----------|---------------|------|------------|---------------|
| `Action Required` | Tool approval | Provider-specific prompt block | Varies by prompt | Live capture | 2026-03-25 |
| `Approve\? \(y/n/always\)` | Tool approval | Inline options | `y`/`n`/`always` | Upstream issues | Unverified |

Notes:
- The current detector uses `Action Required` as the umbrella trigger for Gemini prompts.
- Exact Gemini approval controls may vary by version and terminal rendering.

## Qwen / Kilo / Kimi

Not yet active on Isaac. Patterns are TBD when those providers are enabled.

## Maintenance Rules

- Every `wrapper_unix.py` pattern change must update this file.
- New patterns require a regression fixture in `tests/test_permission_interceptor.py`.
- Confidence levels:
  - `Live capture`
  - `Upstream docs`
  - `Upstream issues`
  - `Inferred`
