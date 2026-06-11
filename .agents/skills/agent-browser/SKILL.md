---
name: agent-browser
description: Browser automation CLI for AI agents to interact with the Programa Core web app and other websites. Use to navigate pages, fill forms, click buttons, take screenshots, extract data, test web flows, automate login, QA features, and hunt for UI bugs.
user-invocable: true
subagent-type: general-purpose
---

# Agent Browser

`agent-browser` is a fast native Rust CLI that lets AI agents drive Chrome or
Chromium via CDP — no Playwright or Puppeteer required.

## Installation

```bash
npm i -g agent-browser && agent-browser install
```

## Load The Right Skill Before Running

Always load the appropriate skill first:

```bash
agent-browser skills get agent-browser   # core browser automation
agent-browser skills get electron        # Electron desktop apps
agent-browser skills get slack           # Slack workspace automation
```

## Why agent-browser

- Fast native Rust CLI — works with any AI agent
- Chrome/Chromium via CDP
- Accessibility-tree snapshots (no fragile selectors)
- Session management and video recording
- Works in Vercel Sandbox microVMs and AWS Bedrock AgentCore cloud browsers

## Programa Core Notes

- The app runs at `http://localhost:5000` in development (`make run`)
- The UI is in Spanish — form labels, button text, and page titles are all in Spanish
- Authentication uses a login form at `/login`; test credentials are in `.env`
- Dark mode is enabled by default; test in both light and dark if visual correctness matters
- All financial data entry forms use Spanish field names (importe, fecha, concepto, etc.)

## Available Skills

| Skill | Use |
|---|---|
| `agent-browser` | Core browser, navigation, forms, screenshots |
| `dogfood` | Test agent-browser itself |
| `electron` | Electron desktop apps (VS Code, Slack, Figma, etc.) |
| `slack` | Slack workspace automation |
| `vercel-sandbox` | Browser automation in Vercel Sandbox microVMs |
| `agentcore` | AWS Bedrock AgentCore cloud browsers |

## Common Patterns

```bash
# Take a screenshot of the current state
agent-browser screenshot

# Navigate to a page
agent-browser navigate http://localhost:5000/cobranzas

# Fill a form field
agent-browser fill "#importe" "1500.00"

# Click a button
agent-browser click "Guardar"

# Get the accessibility tree (preferred over CSS selectors)
agent-browser snapshot
```
