import unittest

from scripts.Network.Ciena_RLS_Upgrade import RLSUpgradeScript


class TestRLSUpgradeRegressions(unittest.TestCase):
    def test_poll_until_done_accepts_idle_transition(self):
        script = RLSUpgradeScript(
            ip_address="127.0.0.1",
            username="user",
            password="pass",
            server_url="http://example.invalid/test.tgz",
            poll_interval=0,
            timeout=5,
        )
        script._log = lambda _msg: None

        responses = iter([
            "software:\n  upgrade-operational-state   : load-in-progress\nrls#",
            "software:\n  upgrade-operational-state   : idle\nrls#",
        ])
        script._send = lambda _session, _cmd, timeout=20: next(responses)

        self.assertTrue(script._poll_until_done(object()))


if __name__ == "__main__":
    unittest.main()
