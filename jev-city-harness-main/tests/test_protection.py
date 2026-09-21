import json
import math
import tempfile
import time
import unittest
from pathlib import Path

from jev_city.protection import formation_report


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = []
        self.leader = {"x": 0, "z": 0, "gangId": "crew"}
        for i in range(9):
            p = Path(self.tmp.name) / str(i)
            p.mkdir()
            self.paths.append(str(p))
            a = 2 * math.pi * i / 9
            (p / "identity.json").write_text(json.dumps({"agent": {"id": str(i)}}))
            (p / "status.json").write_text(
                json.dumps(
                    {
                        "at": time.time(),
                        "running": True,
                        "self": {
                            "id": str(i),
                            "x": 10 * math.cos(a),
                            "z": 10 * math.sin(a),
                            "hp": 100,
                            "equipped": "carbine",
                            "gangId": "crew",
                        },
                    }
                )
            )

    def test_surrounded_leader_can_move(self):
        self.assertTrue(formation_report(self.leader, self.paths)["covered"])

    def test_guards_all_behind_do_not_count_as_surrounding(self):
        for path in self.paths:
            p = Path(path) / "status.json"
            d = json.loads(p.read_text())
            d["self"].update(x=-10, z=0)
            p.write_text(json.dumps(d))
        self.assertFalse(formation_report(self.leader, self.paths)["covered"])

    def test_stale_guards_do_not_count(self):
        for path in self.paths[:4]:
            p = Path(path) / "status.json"
            d = json.loads(p.read_text())
            d["at"] = 0
            p.write_text(json.dumps(d))
        r = formation_report(self.leader, self.paths)
        self.assertEqual(r["nearby"], 5)
        self.assertFalse(r["covered"])

    def test_replacement_cannot_use_previous_character_position(self):
        for path in self.paths:
            (Path(path) / "identity.json").write_text(json.dumps({"agent": {"id": "replacement"}}))
        self.assertEqual(formation_report(self.leader, self.paths)["nearby"], 0)

    def test_brief_formation_gap_does_not_stop_travel(self):
        from unittest.mock import Mock, patch

        from jev_city.protection import wait_for_formation
        from jev_city.storage import Store

        store = Store(Path(self.tmp.name) / "leader")
        store.save("protection.json", {"enabled": True, "guardStates": self.paths})
        store.save("formation-status.json", {"waiting": False, "resumedAt": 90})
        h = Mock(store=store, obs={"self": dict(self.leader, motion={"gait": "walk"})})
        bad = {"nearby": 5, "required": 6, "centerOffset": 8, "largestGap": 3.5, "covered": False}
        with (
            patch("jev_city.protection.formation_report", return_value=bad),
            patch("jev_city.protection.time.time", return_value=100),
        ):
            self.assertFalse(wait_for_formation(h))
        h.action.assert_not_called()

    def test_sustained_gap_stops_after_grace_period(self):
        from unittest.mock import Mock, patch

        from jev_city.protection import wait_for_formation
        from jev_city.storage import Store

        store = Store(Path(self.tmp.name) / "leader")
        store.save("protection.json", {"enabled": True, "guardStates": self.paths})
        store.save("formation-status.json", {"waiting": False, "resumedAt": 80, "badSince": 90})
        h = Mock(store=store, obs={"self": dict(self.leader, motion={"gait": "walk"})})
        bad = {"nearby": 5, "required": 6, "centerOffset": 8, "largestGap": 3.5, "covered": False}
        with (
            patch("jev_city.protection.formation_report", return_value=bad),
            patch("jev_city.protection.time.time", return_value=100),
        ):
            self.assertTrue(wait_for_formation(h))
        h.action.assert_called_once_with({"action": "stop"})

    def test_severe_loss_overrides_minimum_cruise_time(self):
        from unittest.mock import Mock, patch

        from jev_city.protection import wait_for_formation
        from jev_city.storage import Store

        store = Store(Path(self.tmp.name) / "leader")
        store.save("protection.json", {"enabled": True, "guardStates": self.paths})
        store.save("formation-status.json", {"waiting": False, "resumedAt": 97, "badSince": 97})
        h = Mock(store=store, obs={"self": dict(self.leader, motion={"gait": "walk"})})
        bad = {"nearby": 1, "required": 6, "centerOffset": 8, "largestGap": 6, "covered": False}
        with (
            patch("jev_city.protection.formation_report", return_value=bad),
            patch("jev_city.protection.time.time", return_value=100),
        ):
            self.assertTrue(wait_for_formation(h))
