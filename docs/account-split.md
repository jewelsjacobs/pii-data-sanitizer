# M7 — Account split runbook

> **Convention.** This doc uses `<user>` for your macOS username and
> `openclaw` as the agent account name. Substitute your real values
> when running the commands. (`whoami` will show your macOS username
> if you're unsure.)

The hard filesystem gate between raw and clean staging.

The sanitizer pipeline runs as the user (`<user>`) and produces
sanitized records in `clean-staging/`. The downstream Claude Code
agents that ingest those records into the Obsidian vault should run as
a **separate, less-privileged account** so that even if an agent goes
sideways, it cannot read raw mailboxes, the unredacted mapping file,
or anything else with original PII in it.

This document is the exact step-by-step for that split on macOS. Read
it end-to-end before running anything.

> All commands here are intended to be run **by the user** in a plain
> terminal. The Claude Code instance running the sanitizer should not
> be invoking `dscl`, `chown`, `chmod`, or `sudo` — these mutate
> system state and are out of its scope.

---

## Roles

| Role | Account | What it does | Should be able to read |
| --- | --- | --- | --- |
| User | `<user>` (your real macOS user) | Runs `make extract`, `make sanitize`, `make audit`, `make promote`. Reviews the promote staging folder. | Everything in this project, including `raw-staging/`, `mapping/`, the source mailboxes. |
| Agent | `openclaw` (new, non-admin) | Runs the Crew agents (Claude Code) that ingest from the vault. | `clean-staging/` (read-only) and the vault at `/Users/Shared/The Brain Vault/` (read+write). |

The agent account **must not** be able to read:

* `raw-staging/` — anywhere it lives, default `~/projects/pii-data-sanitizer/raw-staging/`
* `mapping/mapping.json` (and `mapping.enc`, `mapping.review.jsonl`)
* `~/Library/Mail/`, `~/Library/Messages/chat.db` (and any copies)
* `config/local.json` (contains your real self-identity)
* `.env` (IMAP credentials)

---

## 1. The `openclaw` account

Already done — set up via System Settings → Users & Groups as a
**Standard** (non-admin) account. Confirm with:

```sh
id openclaw
# uid=NNN(openclaw) gid=20(staff) groups=20(staff),12(everyone),61(localaccounts)
```

If you ever need to recreate it, the conservative path is System
Settings → Users & Groups → Add Account → Standard. The CLI
equivalent (`sudo dscl . -create /Users/openclaw …` plus
`sudo createhomedir -c -u openclaw`) works too but is easier to get
wrong. Either way, the rest of this runbook assumes the account
exists and has a home directory at `/Users/openclaw/`.

---

## 2. Lock down sensitive directories

`raw-staging/`, `mapping/`, and the source mailboxes must be
**unreadable** to any user except `<user>`. The default macOS home
directory permissions are `drwxr-xr-x`, which leaks readability to
"other" — fix that first.

```sh
cd ~/projects/pii-data-sanitizer

# Strip all "group" and "other" access from the sensitive trees.
chmod -R go-rwx raw-staging mapping
chmod go-rwx config/local.json .env 2>/dev/null || true

# Apple's mail and message stores are already mode 700, but enforce it.
chmod -R go-rwx ~/Library/Mail 2>/dev/null || true
chmod 700 ~/Library/Messages 2>/dev/null || true
```

Confirm:

```sh
stat -f "%Sp %u %g %N" raw-staging mapping
# drwx------  501 20 raw-staging
# drwx------  501 20 mapping
```

The leading `drwx------` means: owner=read+write+execute, nobody else
gets in. `openclaw` is "other" relative to your home, so this alone
denies it.

---

## 3. Grant `openclaw` read-only access to `clean-staging/`

POSIX modes alone won't help here — `clean-staging/` lives under your
home directory, which has no path-level access for non-owners. We
solve this with two ACLs: one on the parent path, one on the leaf.

```sh
# Path traversal — allow openclaw to descend through your home and
# project dir to reach clean-staging/. "execute" on a directory means
# "may resolve names inside"; it does NOT grant a list of contents.
chmod +a "user:openclaw allow execute,readattr,readextattr,readsecurity" ~
chmod +a "user:openclaw allow execute,readattr,readextattr,readsecurity" \
  ~/projects ~/projects/pii-data-sanitizer

# The actual read grant on clean-staging/ — recursive via inheritance.
chmod +a "user:openclaw allow read,readattr,readextattr,readsecurity,list,search,file_inherit,directory_inherit" \
  ~/projects/pii-data-sanitizer/clean-staging
```

These ACLs apply **in addition to** POSIX modes. `chmod +a` does not
disturb `drwx------` on the parent dirs — it surgically grants
`openclaw` and only `openclaw` enough access to traverse and read.

Verify the ACL:

```sh
ls -le ~/projects/pii-data-sanitizer/clean-staging
# drwx------+ 5 <user> staff 160 Apr 30 09:00 clean-staging
#  0: user:openclaw allow read,readattr,readextattr,readsecurity,list,search,file_inherit,directory_inherit
```

The trailing `+` after the mode means an ACL is present.

---

## 4. The vault stays in `/Users/Shared/`

`/Users/Shared/` is world-readable on macOS by default. The vault at
`/Users/Shared/The Brain Vault/` therefore needs no special setup —
both `<user>` (when you're moving promoted notes into
`00-Inbox/`) and `openclaw` (when an agent is reading or writing) can
already access it.

If you want the vault to be **writable** by both accounts (so an agent
can save notes back), `chmod -R 775` on the vault and add `openclaw`
to the same group as the directory's owner. This is the simplest path
and has no security cost — the vault content has already been
sanitized by the time it lands there.

```sh
sudo dseditgroup -o edit -a openclaw -t user staff
sudo chmod -R g+w "/Users/Shared/The Brain Vault"
```

(`staff` is the default group for human users on macOS — group ID 20.)

---

## 5. Run the Crew agents under `openclaw`

Two patterns. The first is the simplest; the second is the one to use
if you want the agent to feel like a separate session.

### Pattern A — `sudo -u openclaw` (recommended)

From your normal terminal as `<user>`:

```sh
sudo -u openclaw -i bash -lc 'cd "/Users/Shared/The Brain Vault" && claude'
```

This launches Claude Code as `openclaw`. The `-i` runs a login shell
so `~openclaw/.zshrc` and PATH are right; the inner `bash -lc` then
hands off to whatever you actually want to run.

You'll be prompted for **your** sudo password the first time.

### Pattern B — fast user switch

System Settings → Users & Groups → Login Options → Show fast user
switching menu. Use the menu bar to jump to the openclaw session.
Reserve this for when you want a clean Claude Code keychain / config
in the agent account.

---

## 6. Verify the boundary holds

After completing steps 1-5, run the verification script from each
account.

**As `<user>`:**

```sh
make verify-split
```

Expected: every "user" probe passes (raw-staging readable,
clean-staging readable, mapping readable, vault readable).

**As `openclaw`:**

```sh
sudo -u openclaw -i bash -lc 'cd ~<user>/projects/pii-data-sanitizer && uv run python -m scripts.verify_account_split --profile agent --user-home ~<user>'
```

Keep this on a single line. Splitting it across lines with `\`
continuations gets mangled by some shells when pasted (zsh in
particular) and you'll see `bash: : command not found`.

`--user-home ~<user>` is important. Without it, `Path.home()` resolves
to `/Users/openclaw`, and the script ends up probing openclaw's *own*
empty mailbox instead of the user's mailbox under
`/Users/<user>/Library/Mail/`. The script auto-detects the right home
when cwd is under `/Users/<user>/...`, but passing it explicitly is
the safe move.

Expected: every "agent" probe passes (raw-staging **denied**,
clean-staging readable, mapping **denied**,
`/Users/<user>/Library/Mail` **denied**,
`/Users/<user>/Library/Messages` **denied**, vault readable).

If any agent probe shows the wrong result, **stop**. Don't run the
agents until the boundary is right. The verification script's output
tells you which path is at fault.

---

## 7. Day-to-day workflow after the split

```sh
# As <user> — same as before
make extract-apple-mail
make sanitize-apple-mail
make audit-apple-mail
make promote-apple-mail

# Review the promote staging folder. Move approved notes into
# /Users/Shared/The Brain Vault/00-Inbox/ manually.

# As openclaw — the agent reads from the vault, never from this project
sudo -u openclaw -i bash -lc 'cd "/Users/Shared/The Brain Vault" && claude'
```

The mapping file, raw-staging, source mailboxes, and `.env` never
need to be touched by `openclaw`. The split makes that policy
**enforced** instead of just **assumed**.

---

## Troubleshooting

* **"`chmod +a`: Operation not permitted"** — you ran it on a path
  you don't own. Add `sudo` if it's a shared dir, or check that the
  path is actually under your home.
* **"`make verify-split` says clean-staging is denied for the agent"**
  — the ACL chain on the parent dirs is incomplete. Re-run the
  `chmod +a "user:openclaw allow execute,..."` lines from step 3 on
  every directory between `/` and `clean-staging/`.
* **"The agent can read raw-staging anyway"** — `chmod -R go-rwx`
  didn't apply, possibly because of an inherited ACL from a parent.
  Run `ls -le raw-staging` and look for any `allow` ACE that includes
  `openclaw`. Strip it with `chmod -a# 0 raw-staging`.
* **"sudo -u openclaw asks for openclaw's password, not mine"** —
  add a sudoers entry: `echo "<user> ALL=(openclaw) ALL" | sudo tee /etc/sudoers.d/openclaw-agent`.
  Confirm with `visudo -c`.
* **"My Claude Code session under openclaw can't see the vault"** —
  `/Users/Shared/The Brain Vault` may have inherited stricter perms.
  `ls -ld "/Users/Shared/The Brain Vault"` should show world-execute
  (`drwxr-xr-x` or better). Fix with `sudo chmod 755 "/Users/Shared/The Brain Vault"`.

---

## Removal / undo

If you decide to roll the split back:

```sh
# Remove the ACLs
chmod -N ~/projects/pii-data-sanitizer/clean-staging
chmod -N ~/projects/pii-data-sanitizer
chmod -N ~/projects
chmod -N ~

# Restore default home perms
chmod 755 ~

# Delete the openclaw account (keeps its home dir)
sudo dscl . -delete /Users/openclaw

# Or delete the home dir too
sudo rm -rf /Users/openclaw
```

The mapping and raw-staging POSIX modes (`drwx------`) are worth
keeping regardless — they cost nothing and protect against accidental
"share my home folder" mistakes.
