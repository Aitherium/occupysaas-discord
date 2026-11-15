# Contributing

Changes to the server go through pull requests to `main`. Nobody pushes to `main`
directly; the branch is protected.

## For everyone

1. Small pull requests: one idea each. "Add #design-feedback" and "rename the AI
   forum's tags" are two pull requests.
2. Say **why** in the description. Reviewers approve the reason, not only the diff.
3. Keep names exactly as Discord shows them (capitals, emoji, spaces included).
4. Never add a member, a user id, a token, or a webhook link to any file. The check
   refuses member entries.
5. Wait for both comments: the check and the bot's plan. Fix what the check names.

## For reviewers (code owners)

Before approving, read the bot's plan comment, not only the diff:

- Every line marked `safe` will be applied on merge.
- `destructive` lines (deletes, removed tags or access, switching a feature off) run
  only if you add the `server:allow-destructive` label **before** merging. Adding it
  after the merge does nothing.
- `powerful` lines (manage messages/channels/roles/threads/events, moderate, kick,
  ban, mention everyone) run only with `server:allow-powerful`, added before merging.
- `forbidden` and `manual` lines are never applied by the bot. If they are wanted, a
  server admin does them by hand, then a fresh export lands here.
- The label counts only when a code owner added it. A label added by anyone else is
  ignored and the bot says so.

After the merge the bot posts what it applied, and what it refused and why, in the
moderators' channel and on the pull request.

## Becoming a reviewer

Reviewers are the people who hold the server's staff roles. A reviewer's GitHub
account is added to `.github/CODEOWNERS` (through the reviewers team) only after it
was proven in Discord:

1. In the server, run `/enlist` and give your GitHub username.
2. Run `/verify-github` and follow the steps (a short code in your GitHub bio).
3. Ask a server admin to add you to the reviewers team. Nobody's GitHub name is ever
   guessed or typed in from memory.

## The check

`tools/serverops.py` is the same code the bot runs. The workflow in
`.github/workflows/check.yml` installs the bot library at a pinned version, checks
every file and posts the plan. A change to `tools/serverops.py` itself is reviewed
like any other, and the bot warns on a pull request when its own copy differs.
