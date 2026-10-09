"""Regression checks for delayed OBS scheduler state and current source errors."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from obs_release import wait


REVISION = {"project": "example", "package": "vrcs", "repository": "windows",
            "arch": "x86_64", "srcmd5": "current-source"}


class SchedulerClient:
    def __init__(self, error=False):
        self.round = 0
        self.error = error

    def request(self, url):
        if "_result" in url:
            code = "disabled" if self.round == 0 else "succeeded"
            self.round += 1
            return (f'<resultlist><result><status package="vrcs:standard" code="{code}"/>'
                    f'<status package="vrcs:cuda" code="{code}"/></result></resultlist>').encode()
        if "_buildinfo" in url:
            error = '<error>missing build dependency</error>' if self.error else ''
            return f'<buildinfo><srcmd5>current-source</srcmd5>{error}</buildinfo>'.encode()
        if "_log" in url:
            return b'Building source current-source\n'
        raise AssertionError(url)


class ObsWaitTests(unittest.TestCase):
    def test_old_disabled_status_after_five_minutes_does_not_fail_new_build(self):
        with tempfile.TemporaryDirectory() as directory, patch('obs_release.time.sleep'), \
                patch('obs_release.time.monotonic', side_effect=[0, 301, 302]):
            client = SchedulerClient()
            wait(client, REVISION, Path(directory), 600)
            self.assertEqual(client.round, 2)

    def test_build_information_error_for_current_source_fails_immediately(self):
        with tempfile.TemporaryDirectory() as directory, patch('obs_release.time.sleep'), \
                patch('obs_release.time.monotonic', side_effect=[0, 1]):
            with self.assertRaisesRegex(RuntimeError, 'missing build dependency'):
                wait(SchedulerClient(error=True), REVISION, Path(directory), 600)


if __name__ == '__main__':
    unittest.main()
