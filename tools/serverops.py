"""A Discord server as files: export it, check a change, plan it, gate it.

The same module runs in two places and must give the same answer in both:

* in the repository's CI, offline, where it checks a pull request's files and plans
  the change against the files on ``main``;
* in the bot, where it plans the merged files against the LIVE server and applies
  the part the reviewers' labels allow.

Files (paths relative to the repository root)::

    server/layout.json              categories, channels, topics, forum tags, and each
                                    channel's permission overwrites BY ROLE NAME
    server/onboarding.json          Discord onboarding: default channels and the
                                    questions a newcomer answers (options map to
                                    channels and self-assignable roles BY NAME)
    bot/plugins.json                which bot plugins are switched on or off here
    bot/self-assign.json            roles members may give themselves
    alerts/sentry.json              the scam/abuse watch: mode, alert channel, staff
    automations/orientation.json    the welcome post the bot repeats on a schedule
    automations/feedback-board.json the suggestion and project boards

Names are the identity, never ids: a role, a channel and a category are named the
way Discord shows them. No file holds a member, a token or a webhook URL.

Every planned change carries a risk:

    safe          applied after merge
    powerful      a grant of a moderation/management permission: applied only when a
                  code owner put the ``server:allow-powerful`` label on the PR
    destructive   a delete, a removed tag, a removed overwrite, a switched-off
                  plugin or alert: applied only with ``server:allow-destructive``
    forbidden     refused by the layout guard (administrator, manage server, and the
                  other powers a layout file must never hand out): never applied
    manual        a role's own permissions, colour or existence: a server admin does
                  it by hand in Discord, and the next export picks it up

    python serverops.py check  [--root DIR]               exit 1 on a bad file
    python serverops.py plan   --want DIR --have DIR      prints the plan (markdown)
    python serverops.py ci     --base DIR --head DIR      check + plan, for a PR

Exit codes: 0 clean, 1 a problem was found, 2 the check could not run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

try:  # the layout guard and the channel types come from the bot library itself
    from awdiscord import guildspec
except ImportError:  # pragma: no cover - CI installs it; see README
    guildspec = None  # type: ignore[assignment]

VERSION = "1"

LAYOUT_FILE = "server/layout.json"
ONBOARDING_FILE = "server/onboarding.json"
PLUGINS_FILE = "bot/plugins.json"
SELF_ASSIGN_FILE = "bot/self-assign.json"

LABEL_POWERFUL = "server:allow-powerful"
LABEL_DESTRUCTIVE = "server:allow-destructive"

SAFE, POWERFUL, DESTRUCTIVE, FORBIDDEN, MANUAL = (
    "safe", "powerful", "destructive", "forbidden", "manual")
RISK_ORDER = (FORBIDDEN, MANUAL, DESTRUCTIVE, POWERFUL, SAFE)

CHANNEL_TYPES = {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage",
                 15: "forum"}
TOPIC_TYPES = {"text", "announcement", "forum"}
MANAGED_ROLE_TAGS = ("bot_id", "integration_id", "premium_subscriber")

#: Permissions that let a role moderate or reshape the server. Granting any of them
#: in a channel overwrite or on a new role needs the ``server:allow-powerful`` label.
POWERFUL_BITS = {
    "KICK_MEMBERS": 1 << 1, "BAN_MEMBERS": 1 << 2, "ADMINISTRATOR": 1 << 3,
    "MANAGE_CHANNELS": 1 << 4, "MANAGE_GUILD": 1 << 5, "MANAGE_MESSAGES": 1 << 13,
    "MENTION_EVERYONE": 1 << 17, "MANAGE_NICKNAMES": 1 << 27, "MANAGE_ROLES": 1 << 28,
    "MANAGE_WEBHOOKS": 1 << 29, "MANAGE_GUILD_EXPRESSIONS": 1 << 30,
    "MANAGE_EVENTS": 1 << 33, "MANAGE_THREADS": 1 << 34, "MODERATE_MEMBERS": 1 << 40,
}

#: One settings file -> one stored record of one bot plugin. ``fields`` is the
#: allowlist: a field not named here is never exported and never written.
#: Field kinds: bool, int, str, text, channel, channels, role, roles, list, mode.
SETTINGS: list[dict[str, Any]] = [
    {"file": "alerts/sentry.json", "plugin": "aitherium-sentry", "key": "config",
     "fields": {"enabled": "bool", "mode": "mode", "alert_channel": "channel",
                "alert_role": "role", "staff_roles": "roles", "allow_rules": "list",
                "deny_rules": "list"},
     "modes": ["warn", "warn+alert", "alert-only"]},
    {"file": "automations/orientation.json", "plugin": "orientation",
     "key": "orientation",
     "fields": {"enabled": "bool", "channel": "channel", "every_hours": "int",
                "text": "text"}},
    {"file": "automations/feedback-board.json", "plugin": "aitherium_feedback",
     "key": "board:config",
     "fields": {"open": "bool", "suggest_channel": "channel",
                "projects_channel": "channel", "projects_channels": "channels"}},
]
SELF_ASSIGN = {"plugin": "collective-profile", "prefix": "allow:"}

MAX_TEXT = 4000
MAX_LIST = 50

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
ONBOARDING_MODES = {0: "default", 1: "advanced"}
ONBOARDING_MIN_DEFAULTS = 7
ONBOARDING_MIN_POSTABLE = 5
#: Advice from the last ``check_onboarding`` (shown, never failing a check: Discord
#: itself accepts these).
WARNINGS: list[str] = []
PROMPT_KEYS = {"title", "single_select", "required", "in_onboarding", "options"}
OPTION_KEYS = {"title", "description", "emoji", "channels", "roles"}


# --------------------------------------------------------------------------- export

def _bits(value: Any) -> int | None:
    try:
        return int(str(value if value is not None else "0"), 10)
    except (TypeError, ValueError):
        return None


def _managed(role: dict) -> bool:
    tags = role.get("tags") or {}
    return bool(role.get("managed")) or any(k in tags for k in MANAGED_ROLE_TAGS)


def export_layout(guild: str, roles: list[dict], channels: list[dict]) -> dict:
    """The server's layout file from Discord's own role and channel objects.

    Member overwrites are left out (a file names roles, never people), and so are
    channel kinds the layout does not manage (threads, media channels).
    """
    rname = {str(r["id"]): r["name"] for r in roles}
    rname[str(guild)] = "@everyone"
    cname = {str(c["id"]): c["name"] for c in channels}
    spec_roles = [
        {"name": r["name"], "color": int(r.get("color") or 0), "hoist": bool(r.get("hoist")),
         "mentionable": bool(r.get("mentionable")),
         "permissions": str(r.get("permissions", "0"))}
        for r in sorted(roles, key=lambda r: -int(r.get("position", 0)))
        if str(r["id"]) != str(guild) and not _managed(r)]
    out_channels = []
    for c in sorted(channels, key=lambda c: (int(c["type"]) != 4, int(c.get("position", 0)),
                                             str(c["id"]))):
        kind = CHANNEL_TYPES.get(int(c["type"]))
        if kind is None:
            continue
        ows = []
        for o in c.get("permission_overwrites") or []:
            if int(o.get("type", 0)) != 0 or str(o["id"]) not in rname:
                continue
            ows.append({"target": rname[str(o["id"])], "allow": str(o.get("allow", "0")),
                        "deny": str(o.get("deny", "0"))})
        entry: dict[str, Any] = {
            "name": c["name"], "type": kind,
            "category": cname.get(str(c.get("parent_id") or "")),
            "topic": c.get("topic") or "", "position": int(c.get("position", 0)),
            "overwrites": sorted(ows, key=lambda x: x["target"])}
        if kind == "forum":
            entry["tags"] = [str(t.get("name") or "") for t in c.get("available_tags") or []]
        out_channels.append(entry)
    everyone = next((r for r in roles if str(r["id"]) == str(guild)), None)
    out: dict[str, Any] = {"version": 1, "guild_id": str(guild), "roles": spec_roles,
                           "channels": out_channels}
    if everyone is not None:
        out["everyone_permissions"] = str(everyone.get("permissions", "0"))
    return out


def export_onboarding(raw: dict | None, roles: list[dict], channels: list[dict]) -> dict:
    """Discord's onboarding object with channel and role ids turned into names."""
    raw = raw if isinstance(raw, dict) else {}
    rname = {str(r["id"]): r["name"] for r in roles}
    cname = {str(c["id"]): c["name"] for c in channels}
    prompts = []
    for p in raw.get("prompts") or []:
        opts = []
        for o in p.get("options") or []:
            opt: dict[str, Any] = {"title": o.get("title") or ""}
            if o.get("description"):
                opt["description"] = o["description"]
            emoji = o.get("emoji") if isinstance(o.get("emoji"), dict) else {}
            if emoji.get("name") and not emoji.get("id"):
                opt["emoji"] = emoji["name"]
            if o.get("channel_ids"):
                opt["channels"] = [cname.get(str(i), f"id:{i}") for i in o["channel_ids"]]
            if o.get("role_ids"):
                opt["roles"] = [rname.get(str(i), f"id:{i}") for i in o["role_ids"]]
            opts.append(opt)
        prompts.append({"title": p.get("title") or "",
                        "single_select": bool(p.get("single_select")),
                        "required": bool(p.get("required")),
                        "in_onboarding": bool(p.get("in_onboarding", True)),
                        "options": opts})
    return {"enabled": bool(raw.get("enabled")),
            "mode": ONBOARDING_MODES.get(int(raw.get("mode") or 0), "default"),
            "default_channels": sorted(cname.get(str(i), f"id:{i}")
                                       for i in raw.get("default_channel_ids") or []),
            "prompts": prompts}


def _to_name(kind: str, value: Any, roles: dict, chans: dict) -> Any:
    table = roles if kind.startswith("role") else chans
    if kind in ("channel", "role"):
        v = str(value or "")
        return table.get(v, f"id:{v}") if v else ""
    return [table.get(str(v), f"id:{v}") for v in value or []]


def export_settings(store_get: Callable[[str, str], Any], store_keys: Callable[[str, str], list],
                    plugin_states: dict[str, bool], roles: list[dict],
                    channels: list[dict]) -> dict[str, Any]:
    """The bot settings files from the bot's per-server store (allowlisted fields only).

    ``store_get(plugin, key)`` and ``store_keys(plugin, prefix)`` read the store;
    ``plugin_states`` holds the plugins with an explicit on/off for this server.
    """
    rname = {str(r["id"]): r["name"] for r in roles}
    cname = {str(c["id"]): c["name"] for c in channels}
    out: dict[str, Any] = {
        PLUGINS_FILE: {"enabled": sorted(n for n, on in plugin_states.items() if on),
                       "disabled": sorted(n for n, on in plugin_states.items() if not on)}}
    for s in SETTINGS:
        rec = store_get(s["plugin"], s["key"])
        if not isinstance(rec, dict):
            continue
        data: dict[str, Any] = {}
        for field, kind in s["fields"].items():
            if field not in rec:
                continue
            value = rec[field]
            if kind in ("channel", "channels", "role", "roles"):
                value = _to_name(kind, value, rname, cname)
            data[field] = value
        out[s["file"]] = data
    names = []
    for key in store_keys(SELF_ASSIGN["plugin"], SELF_ASSIGN["prefix"]):
        rid = key[len(SELF_ASSIGN["prefix"]):]
        if store_get(SELF_ASSIGN["plugin"], key):
            names.append(rname.get(rid, f"id:{rid}"))
    out[SELF_ASSIGN_FILE] = {"roles": sorted(names)}
    return out


# --------------------------------------------------------------------------- files

def dump(data: Any) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def all_files() -> list[str]:
    return [LAYOUT_FILE, ONBOARDING_FILE, PLUGINS_FILE, SELF_ASSIGN_FILE] + [
        s["file"] for s in SETTINGS]


def load_dir(root: Path) -> tuple[dict[str, Any], list[str]]:
    """Every managed file under ``root`` (a missing one is absent, not empty)."""
    bundle: dict[str, Any] = {}
    problems: list[str] = []
    for rel in all_files():
        path = Path(root) / rel
        if not path.is_file():
            continue
        try:
            bundle[rel] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{rel}: not valid JSON ({exc})")
    return bundle, problems


def write_dir(root: Path, bundle: dict[str, Any]) -> list[str]:
    written = []
    for rel, data in bundle.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump(data), encoding="utf-8", newline="\n")
        written.append(rel)
    return written


def _check_value(where: str, kind: str, value: Any, spec: dict) -> list[str]:
    if kind == "bool" and not isinstance(value, bool):
        return [f"{where}: must be true or false"]
    if kind == "int" and (not isinstance(value, int) or isinstance(value, bool)
                          or not 1 <= value <= 24 * 30):
        return [f"{where}: must be a whole number from 1 to 720"]
    if kind in ("str", "text", "channel", "role") and not isinstance(value, str):
        return [f"{where}: must be text"]
    if kind == "text" and len(value) > MAX_TEXT:
        return [f"{where}: at most {MAX_TEXT} characters"]
    if kind == "mode" and value not in spec.get("modes", []):
        return [f"{where}: must be one of " + ", ".join(spec.get("modes", []))]
    if kind in ("channels", "roles", "list"):
        if not isinstance(value, list) or len(value) > MAX_LIST \
                or any(not isinstance(v, str) or len(v) > 200 for v in value):
            return [f"{where}: must be a list of at most {MAX_LIST} short texts"]
    return []


def check_bundle(bundle: dict[str, Any]) -> list[str]:
    """Problems that make the files unusable, before any plan."""
    problems: list[str] = []
    layout = bundle.get(LAYOUT_FILE)
    if layout is None:
        problems.append(f"{LAYOUT_FILE} is missing")
    elif not isinstance(layout, dict):
        problems.append(f"{LAYOUT_FILE}: not an object")
    else:
        for c in layout.get("channels") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") not in CHANNEL_TYPES.values():
                problems.append(f"channel {c.get('name')!r}: unknown type {c.get('type')!r}")
            if not isinstance(c.get("name"), str) or not 1 <= len(c.get("name") or "") <= 100:
                problems.append(f"a channel name must be 1-100 characters: {c.get('name')!r}")
            for o in c.get("overwrites") or []:
                if isinstance(o, dict) and str(o.get("target", "")).startswith("member:"):
                    problems.append(f"channel {c.get('name')!r}: overwrites name roles, "
                                    "never members")
            if c.get("tags") is not None and c.get("type") != "forum":
                problems.append(f"channel {c.get('name')!r}: only a forum has tags")
            if c.get("type") == "forum" and len(set(c.get("tags") or [])) != \
                    len(c.get("tags") or []):
                problems.append(f"forum {c.get('name')!r}: a tag is listed twice")
        names = [(c.get("category") or "", c.get("name"), c.get("type"))
                 for c in layout.get("channels") or [] if isinstance(c, dict)]
        dups = sorted({str(n) for n in names if names.count(n) > 1})
        if dups:
            problems.append("a channel is listed twice: " + ", ".join(dups))
        cats = {c.get("name") for c in layout.get("channels") or []
                if isinstance(c, dict) and c.get("type") == "category"}
        for c in layout.get("channels") or []:
            if isinstance(c, dict) and c.get("category") and c["category"] not in cats:
                problems.append(f"channel {c.get('name')!r}: category "
                                f"{c['category']!r} is not in the file")
    problems += check_onboarding(bundle)
    plugins = bundle.get(PLUGINS_FILE)
    if plugins is not None:
        if not isinstance(plugins, dict) or not all(
                isinstance(plugins.get(k, []), list) for k in ("enabled", "disabled")):
            problems.append(f"{PLUGINS_FILE}: needs lists 'enabled' and 'disabled'")
        elif set(plugins.get("enabled", [])) & set(plugins.get("disabled", [])):
            problems.append(f"{PLUGINS_FILE}: a plugin is both enabled and disabled")
    sa = bundle.get(SELF_ASSIGN_FILE)
    if sa is not None and (not isinstance(sa, dict) or not isinstance(sa.get("roles"), list)):
        problems.append(f"{SELF_ASSIGN_FILE}: needs a list 'roles'")
    for s in SETTINGS:
        data = bundle.get(s["file"])
        if data is None:
            continue
        if not isinstance(data, dict):
            problems.append(f"{s['file']}: not an object")
            continue
        for field in data:
            if field not in s["fields"]:
                problems.append(f"{s['file']}: unknown setting {field!r}")
                continue
            problems += _check_value(f"{s['file']} {field}", s["fields"][field],
                                     data[field], s)
    return problems


def _postable(layout: dict, name: str) -> bool:
    """Can @everyone see and post in channel ``name`` (by the file's overwrites)?"""
    base = _bits(layout.get("everyone_permissions"))
    perms = base if base is not None else VIEW_CHANNEL | SEND_MESSAGES
    hits = [c for c in layout.get("channels") or []
            if isinstance(c, dict) and c.get("name") == name and c.get("type") != "category"]
    if len(hits) != 1:
        return False
    for o in hits[0].get("overwrites") or []:
        if o.get("target") == "@everyone":
            perms = (perms & ~(_bits(o.get("deny")) or 0)) | (_bits(o.get("allow")) or 0)
    return bool(perms & VIEW_CHANNEL and perms & SEND_MESSAGES)


def _visible(layout: dict, name: str, roles: list) -> bool:
    """Can a member with only @everyone plus ``roles`` see channel ``name``?"""
    base = _bits(layout.get("everyone_permissions"))
    perms = base if base is not None else VIEW_CHANNEL
    hits = [c for c in layout.get("channels") or []
            if isinstance(c, dict) and c.get("name") == name and c.get("type") != "category"]
    if len(hits) != 1:
        return False
    ows = {o.get("target"): o for o in hits[0].get("overwrites") or []}
    if "@everyone" in ows:
        o = ows["@everyone"]
        perms = (perms & ~(_bits(o.get("deny")) or 0)) | (_bits(o.get("allow")) or 0)
    allow = deny = 0
    for r in roles:
        if r in ows:
            allow |= _bits(ows[r].get("allow")) or 0
            deny |= _bits(ows[r].get("deny")) or 0
    perms = (perms & ~deny) | allow
    return bool(perms & VIEW_CHANNEL)


def check_onboarding(bundle: dict[str, Any]) -> list[str]:
    """Discord's onboarding rules that a file can be checked against offline.

    Community and the bot's Manage Server permission are live facts: the bot checks
    them before it writes (``onboarding_blockers``).
    """
    ob = bundle.get(ONBOARDING_FILE)
    if ob is None:
        return []
    f = ONBOARDING_FILE
    if not isinstance(ob, dict):
        return [f"{f}: not an object"]
    problems: list[str] = []
    del WARNINGS[:]
    unknown = set(ob) - {"enabled", "mode", "default_channels", "prompts"}
    if unknown:
        problems.append(f"{f}: unknown key(s) " + ", ".join(sorted(unknown)))
    if not isinstance(ob.get("enabled", False), bool):
        problems.append(f"{f}: enabled must be true or false")
    if ob.get("mode", "default") not in ONBOARDING_MODES.values():
        problems.append(f"{f}: mode must be default or advanced")
    layout = bundle.get(LAYOUT_FILE) if isinstance(bundle.get(LAYOUT_FILE), dict) else {}
    chan_names = {c.get("name") for c in layout.get("channels") or []
                  if isinstance(c, dict) and c.get("type") != "category"}
    roles_by_name = {r.get("name"): r for r in layout.get("roles") or [] if isinstance(r, dict)}
    sa = bundle.get(SELF_ASSIGN_FILE)
    grantable = set(sa.get("roles") or []) if isinstance(sa, dict) else set()
    defaults = ob.get("default_channels") or []
    if not isinstance(defaults, list) or any(not isinstance(x, str) for x in defaults):
        return problems + [f"{f}: default_channels must be a list of channel names"]
    for name in defaults:
        if name not in chan_names and not name.startswith("id:"):
            problems.append(f"{f}: default channel #{name} is not in {LAYOUT_FILE}")
    if ob.get("enabled"):
        if len(set(defaults)) < ONBOARDING_MIN_DEFAULTS:
            problems.append(f"{f}: Discord needs at least {ONBOARDING_MIN_DEFAULTS} "
                            f"default channels (the file has {len(set(defaults))})")
        postable = [n for n in set(defaults) if _postable(layout, n)]
        if len(postable) < ONBOARDING_MIN_POSTABLE:
            problems.append(f"{f}: Discord needs {ONBOARDING_MIN_POSTABLE} default channels "
                            f"@everyone can post in; these {len(postable)} qualify: "
                            + (", ".join("#" + n for n in sorted(postable)) or "none"))
    prompts = ob.get("prompts") or []
    if not isinstance(prompts, list) or len(prompts) > 15:
        return problems + [f"{f}: prompts must be a list of at most 15"]
    for i, p in enumerate(prompts, 1):
        where = f"{f} question {i}"
        if not isinstance(p, dict):
            problems.append(f"{where}: not an object")
            continue
        extra = set(p) - PROMPT_KEYS
        if extra:
            problems.append(f"{where}: unknown key(s) {', '.join(sorted(extra))} (options "
                            "are static: an answer cannot depend on another answer)")
        if not isinstance(p.get("title"), str) or not 1 <= len(p.get("title") or "") <= 100:
            problems.append(f"{where}: title must be 1-100 characters")
        for k in ("single_select", "required", "in_onboarding"):
            if k in p and not isinstance(p[k], bool):
                problems.append(f"{where}: {k} must be true or false")
        opts = p.get("options") or []
        if not isinstance(opts, list) or not 1 <= len(opts) <= 50:
            problems.append(f"{where}: needs 1-50 options")
            continue
        for j, o in enumerate(opts, 1):
            ow = f"{where} option {j}"
            if not isinstance(o, dict):
                problems.append(f"{ow}: not an object")
                continue
            extra = set(o) - OPTION_KEYS
            if extra:
                problems.append(f"{ow}: unknown key(s) {', '.join(sorted(extra))} (options "
                                "are static: channels and roles only)")
            if not isinstance(o.get("title"), str) or not 1 <= len(o.get("title") or "") <= 50:
                problems.append(f"{ow}: title must be 1-50 characters")
            if len(str(o.get("description") or "")) > 100:
                problems.append(f"{ow}: description at most 100 characters")
            chans, roles = o.get("channels") or [], o.get("roles") or []
            if not chans and not roles:
                problems.append(f"{ow}: must give at least one channel or role")
            for c in chans:
                if c not in chan_names and not str(c).startswith("id:"):
                    problems.append(f"{ow}: channel #{c} is not in {LAYOUT_FILE}")
                elif c in chan_names and not _visible(layout, c, roles):
                    WARNINGS.append(f"{ow}: a newcomer who picks this may not see #{c} "
                                    "(hidden from @everyone, and the option gives no "
                                    "role with an overwrite there)")
            for r in roles:
                if r not in roles_by_name:
                    problems.append(f"{ow}: role {r!r} is not in {LAYOUT_FILE}")
                elif r not in grantable:
                    problems.append(f"{ow}: role {r!r} is not self-assignable "
                                    f"({SELF_ASSIGN_FILE}); onboarding never grants an "
                                    "earned role")
                elif _named(_bits(roles_by_name[r].get("permissions")) or 0):
                    problems.append(f"{ow}: role {r!r} carries powerful permissions")
    return problems


def onboarding_blockers(features: set | None, bot_perms: int | None) -> list[str]:
    """What a server admin must do before the bot can write onboarding."""
    out = []
    if features is None or "COMMUNITY" not in features:
        out.append("this server is not a Community server: a server admin enables "
                   "Community (Server Settings > Enable Community)")
    if bot_perms is None or not (bot_perms & POWERFUL_BITS["MANAGE_GUILD"]
                                 or bot_perms & POWERFUL_BITS["ADMINISTRATOR"]):
        out.append("the bot lacks Manage Server: a server admin gives the Aither role "
                   "Manage Server (Server Settings > Roles > Aither)")
    return out


# --------------------------------------------------------------------------- plan

def _op(kind: str, where: str, risk: str, detail: str = "", **args: Any) -> dict:
    return {"kind": kind, "where": where, "risk": risk, "detail": detail, "args": args}


def _named(bits: int) -> list[str]:
    return sorted(n for n, b in POWERFUL_BITS.items() if bits & b)


def _guard(item: dict, roles: bool = False) -> list[str]:
    """The layout guard's refusals for one role or channel (empty = allowed)."""
    if guildspec is None:
        return ["the layout guard (awdiscord) is not installed"]
    return guildspec.validate_spec({"roles": [item]} if roles else {"channels": [item]})


def _where(c: dict) -> str:
    cat = c.get("category") or ""
    return f"{cat + ' / ' if cat else ''}#{c['name']} ({c['type']})"


def _key(c: dict) -> tuple:
    return (c.get("category") or "", c["name"], c["type"])


def _plan_layout(want: dict, have: dict) -> list[dict]:
    ops: list[dict] = []
    have_roles = {r["name"]: r for r in have.get("roles") or []}
    want_roles = {r["name"]: r for r in want.get("roles") or []}
    for r in reversed(want.get("roles") or []):
        old = have_roles.get(r["name"])
        if old is None:
            bits = _bits(r.get("permissions")) or 0
            refused = _guard(r, roles=True)
            risk = FORBIDDEN if refused else (POWERFUL if _named(bits) else SAFE)
            ops.append(_op("create_role", f"role {r['name']}", risk,
                           "; ".join(refused) or ", ".join(_named(bits)), role=r))
        elif any(old.get(k) != r.get(k) for k in ("permissions", "color", "hoist",
                                                    "mentionable")):
            ops.append(_op("role_edit", f"role {r['name']}", MANUAL,
                           "a role's own settings are edited by hand in Discord"))
    if "everyone_permissions" in want and \
            str(want.get("everyone_permissions")) != str(have.get("everyone_permissions")):
        ops.append(_op("role_edit", "role @everyone", MANUAL,
                       "a role's own settings are edited by hand in Discord"))
    for name in sorted(set(have_roles) - set(want_roles)):
        ops.append(_op("role_delete", f"role {name}", MANUAL,
                       "roles are removed by hand in Discord"))

    have_ch = {_key(c): c for c in have.get("channels") or []}
    want_ch = {_key(c): c for c in want.get("channels") or []}
    moved: dict[tuple, tuple] = {}         # want key -> have key
    for k in want_ch:
        if k in have_ch:
            continue
        same = [h for h in have_ch if h[1:] == k[1:] and h not in want_ch]
        cands = [w for w in want_ch if w[1:] == k[1:] and w not in have_ch]
        if len(same) == 1 and len(cands) == 1:
            moved[k] = same[0]
    creates, updates, deletes = [], [], []
    for k, c in want_ch.items():
        old = have_ch.get(k) or (have_ch.get(moved[k]) if k in moved else None)
        if old is None:
            refused = _guard(c)
            grants = sorted({n for o in c.get("overwrites") or []
                             for n in _named(_bits(o.get("allow")) or 0)})
            risk = FORBIDDEN if refused else (POWERFUL if grants else SAFE)
            creates.append(_op("create_channel", _where(c), risk,
                               "; ".join(refused) or ", ".join(grants), channel=c))
            continue
        if k in moved:
            updates.append(_op("move_channel", _where(c), SAFE,
                               f"from {moved[k][0] or 'no category'}",
                               name=c["name"], type=c["type"], src=moved[k][0],
                               dst=c.get("category") or ""))
        if c["type"] in TOPIC_TYPES and (c.get("topic") or "") != (old.get("topic") or ""):
            updates.append(_op("set_topic", _where(c), SAFE, "", key=list(k),
                               topic=c.get("topic") or ""))
        if c["type"] == "forum":
            wt, ht = list(c.get("tags") or []), list(old.get("tags") or [])
            add = [t for t in wt if t not in ht]
            drop = [t for t in ht if t not in wt]
            if add:
                updates.append(_op("add_tags", _where(c), SAFE, ", ".join(add),
                                   key=list(k), tags=add))
            if drop:
                updates.append(_op("remove_tags", _where(c), DESTRUCTIVE, ", ".join(drop),
                                   key=list(k), tags=drop))
        wo = {o["target"]: o for o in c.get("overwrites") or []}
        ho = {o["target"]: o for o in old.get("overwrites") or []}
        for t in sorted(set(wo) | set(ho)):
            w, h = wo.get(t), ho.get(t)
            if w is None:
                updates.append(_op("remove_overwrite", _where(c), DESTRUCTIVE,
                                   f"{t}: back to the category/server default",
                                   key=list(k), target=t))
                continue
            if h is not None and str(h["allow"]) == str(w["allow"]) \
                    and str(h["deny"]) == str(w["deny"]):
                continue
            ha, hd = ((_bits(h["allow"]) or 0, _bits(h["deny"]) or 0) if h else (0, 0))
            wa, wd = _bits(w["allow"]) or 0, _bits(w["deny"]) or 0
            add, clear = wa & ~ha, hd & ~wd
            # MERGE when the change only widens access: OR the new allow bits in and
            # clear those (and any dropped) deny bits, against the LIVE overwrite at
            # apply time, so a hand-added bit is never lost. Anything that narrows
            # access (an allow bit dropped, a deny bit added) replaces the overwrite
            # and is destructive.
            narrows = bool(ha & ~wa or wd & ~hd)
            refused = _guard({**c, "overwrites": [w]})
            grants = _named(add)
            risk = FORBIDDEN if refused else DESTRUCTIVE if narrows else (
                POWERFUL if grants else SAFE)
            updates.append(_op("set_overwrite", _where(c), risk,
                               "; ".join(refused) or
                               (f"{t}: replace with allow {w['allow']} deny {w['deny']} "
                                "(narrows access)" if narrows else
                                f"{t}: add allow {add}" + (f", clear deny {clear}"
                                                           if clear else ""))
                               + (f" (grants {', '.join(grants)})" if grants else ""),
                               key=list(k), target=t, allow=str(wa), deny=str(wd),
                               mode="replace" if narrows else "merge", add=str(add),
                               clear=str(clear)))
    creates.sort(key=lambda o: o["args"]["channel"]["type"] != "category")
    gone = set(moved.values())
    for k, c in have_ch.items():
        if k in want_ch or k in gone:
            continue
        deletes.append(_op("delete_channel", _where(c), DESTRUCTIVE,
                           "deletes the channel and its messages", key=list(k)))
    # children before their category
    deletes.sort(key=lambda o: o["args"]["key"][2] == "category")
    return ops + creates + updates + deletes


def _plan_settings(want: dict[str, Any], have: dict[str, Any]) -> list[dict]:
    ops: list[dict] = []
    wp, hp = want.get(PLUGINS_FILE), have.get(PLUGINS_FILE) or {}
    if isinstance(wp, dict):
        h_on, h_off = set(hp.get("enabled") or []), set(hp.get("disabled") or [])
        for n in sorted(set(wp.get("enabled") or []) - h_on):
            ops.append(_op("enable_plugin", f"plugin {n}", SAFE, "switch on", plugin=n))
        for n in sorted(set(wp.get("disabled") or []) - h_off):
            ops.append(_op("disable_plugin", f"plugin {n}", DESTRUCTIVE, "switch off",
                           plugin=n))
    ws, hs = want.get(SELF_ASSIGN_FILE), have.get(SELF_ASSIGN_FILE) or {}
    if isinstance(ws, dict):
        w_r, h_r = set(ws.get("roles") or []), set(hs.get("roles") or [])
        for n in sorted(w_r - h_r):
            ops.append(_op("self_assign_add", f"self-assign {n}", SAFE,
                           "members may take this role", role=n))
        for n in sorted(h_r - w_r):
            ops.append(_op("self_assign_remove", f"self-assign {n}", DESTRUCTIVE,
                           "members may no longer take this role", role=n))
    for s in SETTINGS:
        w, h = want.get(s["file"]), have.get(s["file"]) or {}
        if not isinstance(w, dict):
            continue
        for field, kind in s["fields"].items():
            if field not in w or w[field] == h.get(field):
                continue
            old, new = h.get(field), w[field]
            weaker = (kind == "bool" and old is True and new is False) or (
                isinstance(old, list) and isinstance(new, list) and set(old) - set(new))
            shown = new if kind != "text" else f"{len(str(new))} characters"
            ops.append(_op("set_setting", f"{s['file']} {field}",
                           DESTRUCTIVE if weaker else SAFE, f"-> {json.dumps(shown)}",
                           file=s["file"], field=field, value=new))
    return ops


def plan(want: dict[str, Any], have: dict[str, Any]) -> list[dict]:
    """Every change that turns ``have`` into ``want``, each with its risk.

    An empty list means the two are the same server (export == live).
    """
    ops: list[dict] = []
    if isinstance(want.get(LAYOUT_FILE), dict):
        ops += _plan_layout(want[LAYOUT_FILE], have.get(LAYOUT_FILE) or {})
    ops += _plan_settings(want, have)
    return ops + _plan_onboarding(want, have)


def _plan_onboarding(want: dict[str, Any], have: dict[str, Any]) -> list[dict]:
    w, h = want.get(ONBOARDING_FILE), have.get(ONBOARDING_FILE) or {}
    if not isinstance(w, dict):
        return []
    norm = {"enabled": False, "mode": "default", "default_channels": [], "prompts": []}
    w, h = {**norm, **w}, {**norm, **h}
    # Discord does not keep the default channels in order: compare them as a set.
    w["default_channels"] = sorted(w["default_channels"] or [])
    h["default_channels"] = sorted(h["default_channels"] or [])
    if w == h:
        return []
    bits = []
    if w["enabled"] != h["enabled"]:
        bits.append("switch " + ("on" if w["enabled"] else "off"))
    if w["mode"] != h["mode"]:
        bits.append(f"mode {w['mode']}")
    if w["default_channels"] != h["default_channels"]:
        bits.append(f"{len(w['default_channels'])} default channel(s)")
    if w["prompts"] != h["prompts"]:
        bits.append(f"{len(w['prompts'])} question(s) with "
                    f"{sum(len(p.get('options') or []) for p in w['prompts'])} option(s)")
    have_titles = {p.get("title") for p in h["prompts"]}
    dropped = (h["enabled"] and not w["enabled"]) or bool(
        have_titles - {p.get("title") for p in w["prompts"]})
    return [_op("set_onboarding", "onboarding", DESTRUCTIVE if dropped else SAFE,
                ", ".join(bits), onboarding=w)]


def gate(ops: list[dict], labels: set[str]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split a plan into what may run now and what is refused, with the reason."""
    run, refused = [], []
    for o in ops:
        risk = o["risk"]
        if risk == SAFE:
            run.append(o)
        elif risk == POWERFUL and LABEL_POWERFUL in labels:
            run.append(o)
        elif risk == DESTRUCTIVE and LABEL_DESTRUCTIVE in labels:
            run.append(o)
        elif risk == POWERFUL:
            refused.append((o, f"grants a powerful permission: needs the "
                               f"`{LABEL_POWERFUL}` label from a code owner"))
        elif risk == DESTRUCTIVE:
            refused.append((o, f"removes something: needs the `{LABEL_DESTRUCTIVE}` "
                               "label from a code owner"))
        elif risk == FORBIDDEN:
            refused.append((o, "the layout guard never applies this; a server admin "
                               "does it by hand"))
        else:
            refused.append((o, "done by hand in Discord; the next export records it"))
    return run, refused


def render(ops: list[dict], title: str = "Plan", labels: set[str] | None = None) -> str:
    """The plan as markdown, grouped by risk; says so plainly when it is empty."""
    if not ops:
        return f"### {title}\n\nNo change: the files match the server.\n"
    run, refused = gate(ops, labels or set())
    lines = [f"### {title}", "",
             f"{len(ops)} change(s): {len(run)} will be applied after merge, "
             f"{len(refused)} will not.", ""]
    if run:
        lines += ["**Applied after merge**", ""]
        lines += [f"- `{o['risk']}` {o['kind'].replace('_', ' ')}: {o['where']}"
                  + (f" -- {o['detail']}" if o["detail"] else "") for o in run]
        lines.append("")
    if refused:
        lines += ["**Not applied**", ""]
        lines += [f"- `{o['risk']}` {o['kind'].replace('_', ' ')}: {o['where']} -- {why}"
                  for o, why in refused]
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI

def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="serverops.py")
    sub = ap.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("check")
    c.add_argument("--root", default=".")
    p = sub.add_parser("plan")
    p.add_argument("--want", required=True)
    p.add_argument("--have", required=True)
    i = sub.add_parser("ci")
    i.add_argument("--base", required=True)
    i.add_argument("--head", required=True)
    i.add_argument("--out", help="also write the plan markdown here")
    i.add_argument("--labels", default="", help="comma-separated PR labels")
    a = ap.parse_args(argv)
    if guildspec is None:
        print("could not judge: install awdiscord (see README)", file=sys.stderr)
        return 2
    if a.mode == "check":
        bundle, problems = load_dir(Path(a.root))
        problems += check_bundle(bundle)
        for line in problems:
            print(f"problem: {line}")
        print("files ok" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if a.mode == "plan":
        want, p1 = load_dir(Path(a.want))
        have, p2 = load_dir(Path(a.have))
        if p1 + p2:
            print("\n".join(p1 + p2), file=sys.stderr)
            return 2
        ops = plan(want, have)
        sys.stdout.write(render(ops))
        return 0 if not ops else 1
    head, p1 = load_dir(Path(a.head))
    base, p2 = load_dir(Path(a.base))
    problems = p1 + p2 + check_bundle(head)
    labels = {x.strip() for x in a.labels.split(",") if x.strip()}
    text = ""
    if problems:
        text = ("### Check failed: fix these before this can merge\n\n"
                + "\n".join(f"- {x}" for x in problems) + "\n\n")
    if WARNINGS:
        text += ("### Worth a look (not blocking)\n\n"
                 + "\n".join(f"- {x}" for x in WARNINGS) + "\n\n")
    if not p1 + p2 and isinstance(head.get(LAYOUT_FILE), dict):
        ops = plan(head, base)
        text += render(ops, "What the bot will do after merge", labels)
        bad = [o for o in ops if o["risk"] == FORBIDDEN]
        if bad:
            problems += [f"{o['where']}: {o['detail']}" for o in bad]
            text += ("\n**This PR cannot merge as is:** the layout guard refuses "
                     f"{len(bad)} change(s) above.\n")
    sys.stdout.write(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(_cli())
