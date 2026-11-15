"""The files on this branch are valid, and they describe ONE consistent server.

The dry run rebuilds a server from the files (Discord's own object shapes, made-up
ids), exports it the way the bot does, and plans the files against that export. An
empty plan means the files and the export format agree: what the bot exports is
exactly what these files say, so a fresh export of an unchanged server plans to
nothing.

    python -m unittest discover -s tools -p "test_*.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serverops  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TYPE_IDS = {v: k for k, v in serverops.CHANNEL_TYPES.items()}


def server_from_files(bundle: dict):
    """Discord role/channel/onboarding objects that the files describe."""
    lay = bundle[serverops.LAYOUT_FILE]
    guild = lay["guild_id"]
    roles = [{"id": guild, "name": "@everyone", "position": 0,
              "permissions": lay.get("everyone_permissions", "0")}]
    n = len(lay["roles"])
    for i, r in enumerate(lay["roles"]):
        roles.append({**r, "id": str(1000 + i), "position": n - i})
    # Roles Discord manages (the bot's own, boosters) are not in the file, but a
    # channel can still name them in an overwrite.
    named = {o["target"] for c in lay["channels"] for o in c["overwrites"]}
    for j, name in enumerate(sorted(named - {r["name"] for r in roles})):
        roles.append({"id": str(3000 + j), "name": name, "position": 0, "managed": True,
                      "permissions": "0", "tags": {"bot_id": str(j)}})
    rid = {r["name"]: r["id"] for r in roles}
    cats = {}
    chans = []
    for i, c in enumerate(lay["channels"]):
        cid = str(5000 + i)
        if c["type"] == "category":
            cats[c["name"]] = cid
        raw = {"id": cid, "name": c["name"], "type": TYPE_IDS[c["type"]],
               "position": c["position"], "topic": c.get("topic") or None,
               "parent_id": cats.get(c.get("category") or ""),
               "permission_overwrites": [
                   {"id": rid[o["target"]], "type": 0, "allow": o["allow"],
                    "deny": o["deny"]} for o in c["overwrites"]]}
        if c["type"] == "forum":
            raw["available_tags"] = [{"id": str(j), "name": t}
                                     for j, t in enumerate(c.get("tags") or [])]
        chans.append(raw)
    cid = {c["name"]: c["id"] for c in chans if c["type"] != 4}
    ob = bundle.get(serverops.ONBOARDING_FILE) or {}
    onboarding = {
        "enabled": ob.get("enabled", False),
        "mode": {v: k for k, v in serverops.ONBOARDING_MODES.items()}[
            ob.get("mode", "default")],
        "default_channel_ids": [cid[x] for x in ob.get("default_channels") or []],
        "prompts": [{**{k: v for k, v in p.items() if k != "options"}, "options": [
            {"title": o["title"], "description": o.get("description"),
             "channel_ids": [cid[x] for x in o.get("channels") or []],
             "role_ids": [rid[x] for x in o.get("roles") or []]}
            for o in p["options"]]} for p in ob.get("prompts") or []]}
    return guild, roles, chans, onboarding


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.bundle, problems = serverops.load_dir(ROOT)
        self.assertEqual(problems, [])

    def test_files_check(self):
        self.assertEqual(serverops.check_bundle(self.bundle), [])

    def test_dry_run_against_the_exported_server_is_empty(self):
        guild, roles, chans, onboarding = server_from_files(self.bundle)
        live = dict(self.bundle)
        live[serverops.LAYOUT_FILE] = serverops.export_layout(guild, roles, chans)
        live[serverops.ONBOARDING_FILE] = serverops.export_onboarding(
            onboarding, roles, chans)
        self.assertEqual(serverops.plan(self.bundle, live), [])

    def test_no_member_data(self):
        text = (ROOT / serverops.LAYOUT_FILE).read_text(encoding="utf-8")
        self.assertNotIn("member:", text)


if __name__ == "__main__":
    unittest.main()
