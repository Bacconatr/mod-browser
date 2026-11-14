"""Approve or deny exactly the request PVE checked: the optional fingerprint, at the HTTP API.

PVE (Homelab's pve-mod-queue.py, #1759) posts a request to Discord with the
fingerprint of its feed entry, and sends it back with the owner's reaction. The
site refuses (409) when the request changed in between, so the last few seconds
between PVE's check and the call cannot approve something the owner never saw.
"""

from __future__ import annotations

import hashlib
import json

from tests.test_requests import CONNECTED, RequestsCase


def pve_fingerprint(raw):
    """Copied verbatim from pve-mod-queue.py as merged (Homelab #1759), the other side of the contract."""
    decided = {k: raw.get(k) for k in ("requester", "ownerAdded", "mod", "dependencies", "versionId", "added", "kind")}
    blob = json.dumps(decided, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


class FingerprintTest(RequestsCase):
    def setUp(self):
        self.start()
        self.rid = self.ask()[1]["request"]["id"]

    def entry(self):
        _, body, _ = self.call("GET", self.local + "/admin-api/v1/requests")
        return next(r for r in body["requests"] if r["id"] == self.rid)

    def change_behind_pves_back(self):
        """What the post did not show changed: here a dependency set, as an override would leave it."""
        added = self.queue.get(self.rid).added + [{"name": "Kaleidoscope Lib", "source": "curseforge", "projectId": "1"}]
        self.queue._db.execute("UPDATE requests SET added = ? WHERE id = ?", (json.dumps(added), self.rid))

    def test_pve_and_the_site_compute_the_same_fingerprint_from_the_feed(self):
        entry = self.entry()
        self.assertEqual(entry["fingerprint"], pve_fingerprint(entry))

    def test_approve_with_the_checked_fingerprint_goes_through(self):
        code, body, _ = self.act(self.rid, "approve", {"fingerprint": pve_fingerprint(self.entry())}, base="local")
        self.assertEqual((code, body["request"]["state"]), (200, "queued"))

    def test_a_request_that_changed_since_the_check_is_refused_and_left_alone(self):
        checked = pve_fingerprint(self.entry())
        self.change_behind_pves_back()
        for action, extra in (("approve", {}), ("deny", {"reason": "not this one"})):
            code, body, _ = self.act(self.rid, action, {"fingerprint": checked, **extra}, base="local")
            self.assertEqual(code, 409, action)
            self.assertIn("changed since it was checked", body["error"])
            self.assertEqual(body["fingerprint"], pve_fingerprint(self.entry()))
        self.assertEqual(self.state_of(self.rid), "pending")

    def test_deny_with_the_checked_fingerprint_goes_through(self):
        code, body, _ = self.act(self.rid, "deny", {"fingerprint": pve_fingerprint(self.entry()), "reason": "no"}, base="local")
        self.assertEqual((code, body["request"]["state"]), (200, "denied"))

    def test_without_a_fingerprint_nothing_changes(self):
        self.change_behind_pves_back()
        self.assertEqual(self.act(self.rid, "approve", base="local")[0], 200)

    def test_a_malformed_fingerprint_or_one_on_another_action_is_a_400(self):
        good = pve_fingerprint(self.entry())
        for body, action in (({"fingerprint": "ABC"}, "approve"), ({"fingerprint": good.upper()}, "approve"),
                             ({"fingerprint": 7}, "deny"), ({"fingerprint": good}, "remove")):
            self.assertEqual(self.act(self.rid, action, body, base="local")[0], 400, (body, action))
        self.assertEqual(self.state_of(self.rid), "pending")

    def test_the_owners_page_gets_the_same_check(self):
        checked = pve_fingerprint(self.entry())
        self.change_behind_pves_back()
        self.assertEqual(self.act(self.rid, "approve", {"fingerprint": checked})[0], 409)

    def test_the_public_feed_does_not_carry_it(self):
        _, body, _ = self.call("GET", self.public + "/api/requests")
        self.assertNotIn("fingerprint", body["requests"][0])
