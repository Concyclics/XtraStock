---
name: clawhub
description: Browse, search, and install skills from the CrawHub skill marketplace.
metadata:
  { "mmclaw": { "emoji": "🏪", "os": ["linux", "darwin", "win32"] } }
---
# CrawHub Skill Marketplace

Browse and install community skills from CrawHub.

## When to Use
✅ **USE this skill when:**
- User wants to find or install a new skill
- User asks what skills are available
- User wants to update or reinstall an existing skill

## Searching CrawHub

Use the **web-search skill** to search CrawHub for skills. Search queries should target `clawhub.ai`:

```
site:clawhub.ai <keyword>
```

As a fallback, construct these URLs to share with the user:

### Browse all skills (sorted by downloads)
```
https://clawhub.ai/skills?sort=downloads&nonSuspicious=true
```

### Search by keyword
```
https://clawhub.ai/skills?nonSuspicious=true&q=KEYWORD
```

> ⚠️ Do NOT curl/fetch CrawHub pages unless the user explicitly asks — the site has rate limits.

## Getting the Download URL (for Telegram/WhatsApp users)

Since users on mobile connectors can't run terminal commands directly:

1. Use the web-search skill to search `site:clawhub.ai <keyword>` and share the result URLs. Also mention the browse URL as a fallback if the search doesn't return the right result.
2. Ask them to open the skill page and click into it
3. On the skill page: **right-click the "Download Zip" button → "Copy Link"**
4. Ask them to paste the copied link back into the chat

Then install it (replace `<workspace>` per the OS note in Local Commands):
```
mmclaw -w <workspace> skill install <pasted-url>
```

If you get `already exists. Use --force to replace it.`, ask the user:
> "Skill X already exists. Replace it?"
- User says yes → run `mmclaw -w <workspace> skill install --force <pasted-url>`
- User says no → abort

## Local Commands

The workspace path comes from your `[MMCLAW_WORKSPACE]` context. Replace `<workspace>` with the actual path using the OS-appropriate variable:
- Linux/macOS: `$MMCLAW_WORKSPACE`
- Windows cmd: `%MMCLAW_WORKSPACE%`
- Windows PowerShell: `$env:MMCLAW_WORKSPACE`

```
# List installed skills
mmclaw -w <workspace> skill list

# Install from URL — always try without --force first
mmclaw -w <workspace> skill install <url>

# Install from local directory
mmclaw -w <workspace> skill install /path/to/skill-dir

# Uninstall
mmclaw -w <workspace> skill uninstall <skill-name>
```
