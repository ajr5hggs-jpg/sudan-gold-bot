---
name: Telegram source accessibility
description: Check whether Telegram share links expose content before building a scraper.
---

Telegram links that resemble channel-post permalinks can resolve to generic contact pages instead of exposing message text. Verify the exact URL returns readable posts before relying on it as a scheduled data feed. If it does not, use an accessible source rather than guessing or fabricating prices.

**Why:** The initial price link returned a contact-only page, which could not support safe automated extraction.

**How to apply:** During future Telegram data-source work, inspect the actual page response and confirm the expected values appear before wiring up scheduled sends.
