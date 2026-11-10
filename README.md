# Diya OTT Radar

A cloud-scheduled Telegram assistant that discovers Indian OTT release news mentioning Telugu movies, including original Telugu and Telugu-dubbed titles.

## What this first version does

- Runs every Friday at 08:00 India Standard Time using GitHub Actions.
- Also supports manual runs and a Telegram connection-test mode.
- Reads Google News RSS searches for recent Telugu OTT release announcements.
- Deduplicates links and filters for Telugu + OTT/streaming mentions.
- Sends source links and conservative language labels to your Telegram chat.

## Accuracy limitation you should understand

This version is a **news lead finder**, not a verified OTT catalogue API. News feeds do not reliably expose platform audio-track metadata. A story saying a title is Telugu or dubbed does not prove that a Telugu audio track is currently available in the streaming app. Reports therefore label audio as unverified and include the source link. Before watching, check the title's Audio / Languages section inside the OTT app.

A later upgrade can add platform-specific catalogue checks where an official, reliable data source is available. Do not change the labels to "confirmed" unless the relevant platform or an authoritative listing explicitly confirms Telugu audio.

## 1. Add GitHub Actions secrets

In the repository, open **Settings → Secrets and variables → Actions → New repository secret** and create:

- `TELEGRAM_BOT_TOKEN`: your newly regenerated token from BotFather.
- `TELEGRAM_CHAT_ID`: your private Telegram chat ID.

Never put either value in source code, README files, commits, issues, or screenshots. If a token has been exposed, revoke it with BotFather and replace the GitHub secret.

## 2. Run a Telegram test

1. Open the repository's **Actions** tab.
2. Select **Diya OTT Radar**.
3. Click **Run workflow**.
4. Set **Send a Telegram connection test only** to `true`.
5. Run the workflow and check your Telegram chat.

For the first run, test Telegram delivery before expecting discovery results.

## 3. Schedule

The workflow uses `30 2 * * 5` UTC, which is Friday at 08:00 in India (UTC+05:30). GitHub may start scheduled workflows a little after the exact minute. Scheduled workflows also require the repository's Actions workflows to be enabled.

## 4. Optional configuration

You can add repository variables under **Settings → Secrets and variables → Actions → Variables**:

- `MAX_ITEMS`: number of items to include (default 20).
- `OTT_FEEDS`: comma-separated RSS feed URLs. If omitted, the built-in Google News RSS searches are used.

## Local run

```bash
python -m pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="your-chat-id"
python main.py --test
python main.py
```

## Project structure

```text
.
├── .github/workflows/friday.yml
├── main.py
├── requirements.txt
└── README.md
```
