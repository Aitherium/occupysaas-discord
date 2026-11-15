# Occupy SaaS Discord, as files

This repository **is** the Occupy SaaS Discord server's setup: its categories and
channels, channel topics, forum tags, which roles can see and post where, the
onboarding questions newcomers answer, and the settings of the server's bot
(Aither): its scam/abuse alerts, the welcome post it repeats, and the suggestion
boards.

Anyone can suggest a change. Tom and the moderators review it. When a change is
merged, the bot applies it to the server and posts exactly what it did in the
moderators' channel.

## How to suggest a change (no coding needed)

**From Discord.** Type `/server propose` followed by your idea, for example:

> /server propose Add a #design-feedback channel under OccupySaas Development so
> designers can post mockups.

The bot opens an issue here for you and replies with the link. A reviewer turns it
into a change or asks you a question on the issue.

**On GitHub, editing a file yourself.**

1. Open the file you want to change (see the table below) and press the pencil icon.
2. Make your edit. GitHub offers to put it on a new branch and open a pull request
   for you; say yes.
3. In the pull request, a bot comment shows **exactly** what would change on the
   server. If the check fails, the comment says why in plain words.
4. A reviewer approves it, or asks for a change.
5. After the merge, the bot applies it and posts the result.

| to change | edit |
|---|---|
| a channel, category, topic, forum tag, or who can see a channel | `server/layout.json` |
| what newcomers are asked when they join, and their starting channels | `server/onboarding.json` |
| the bot's scam/abuse alerts | `alerts/sentry.json` |
| the welcome post and how often it repeats | `automations/orientation.json` |
| the suggestion and project boards | `automations/feedback-board.json` |
| which bot features are on | `bot/plugins.json` |
| which roles members can give themselves | `bot/self-assign.json` |

Everything is named the way Discord shows it: `"general"`, `"Peace Officer"`,
`"OccupySaas Development"`. You never need an id.

## What the bot will and will not do

- **Adds and edits are applied after merge**: a new channel or category, a topic,
  a new forum tag, a channel moved to another category, a role's access to a
  channel, onboarding, bot settings.
- **Removing things needs a reviewer's label.** Deleting a channel, removing a forum
  tag or a role's access, switching a bot feature or alert off: the bot refuses
  unless a code owner put the `server:allow-destructive` label on the pull request
  before it merged. Without it, the bot posts what it skipped and why.
- **Powerful permissions need a reviewer's label.** Letting a role manage messages,
  channels, roles, threads, events or members needs `server:allow-powerful`.
- **Never applied by the bot**: Administrator and Manage Server grants, and a role's
  own settings (its permissions, colour, or deleting it). A server admin does those
  by hand in Discord; the next export records them here.
- **Onboarding** needs the server to be a Community server and the bot to hold
  Manage Server. Until both are true the bot refuses an onboarding change and says
  which one is missing.
- Nothing here ever holds a member, a token, or a webhook link.

## Reading a check result

| the comment says | meaning | what to do |
|---|---|---|
| `No change: the files match the server.` | your edit changes nothing on the server | check you saved the edit |
| `Check failed: fix these before this can merge` | a file breaks a rule (Discord's or ours); each line names the file and the problem | fix the named line, push again |
| `safe` | applied after merge | nothing |
| `destructive` | removes something; needs `server:allow-destructive` from a code owner | ask a reviewer |
| `powerful` | grants a strong permission; needs `server:allow-powerful` | ask a reviewer |
| `forbidden` | the bot never applies this | ask a server admin to do it by hand |
| `manual` | role settings are edited by hand in Discord | ask a server admin |

Two comments may appear on a pull request: the **check** (run here, against the
files on `main`) and the **bot's plan** (run by the bot against the live server).
If they disagree, the bot's plan is the one that runs; the difference means
somebody changed the server by hand since the last export.

## For reviewers

See [CONTRIBUTING.md](CONTRIBUTING.md). Reviewers are the code owners in
`.github/CODEOWNERS`. A reviewer's GitHub account is added there only after it was
verified with `/verify-github` in the server.

## Running the check yourself

```bash
pip install "awdiscord @ git+https://github.com/Aitherium/awdiscord@8ee70365c93ffa0697c0ddf979efc1b660d57897"
python tools/serverops.py check
python tools/serverops.py plan --want . --have <a copy of main>
```

Apache-2.0. See [LICENSE](LICENSE).
