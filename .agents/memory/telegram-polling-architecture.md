---
name: Telegram polling architecture
description: Keep Telegram commands and scheduled notifications on one shared bot poller.
---

Use one `python-telegram-bot` application for all Telegram features in this project. Add commands and periodic work to that application; do not start a second `getUpdates` poller with the same bot token.

**Why:** Two polling processes using the same token can conflict and interrupt the existing Sudan gold-price subscriber.

**How to apply:** Extend the existing application and its background workers when adding Telegram features, rather than creating another polling process.
