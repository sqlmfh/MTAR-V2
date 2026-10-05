from __future__ import annotations

import threading
import types
import unittest
from unittest.mock import patch

import mtar_services


class _Configured:
    def configured(self):
        return True


def _old_style_checker(module_globals: dict, release: threading.Event) -> threading.Thread:
    """A checker as the code before the stop switch started it."""
    code = compile("def loop():\n    while not release.is_set():\n        gmail_client.configured()\n        release.wait(0.01)\n", "old_mtar_services", "exec")
    exec(code, module_globals)
    thread = threading.Thread(target=module_globals["loop"], name=mtar_services.POLLER_NAME, daemon=True)
    thread.start()
    return thread


class BackgroundCheckTests(unittest.TestCase):
    def setUp(self):
        self._saved = (mtar_services._poller, mtar_services._stop_poller)
        mtar_services._poller = None
        mtar_services._stop_poller = threading.Event()

    def tearDown(self):
        mtar_services._stop_poller.set()
        if mtar_services._poller is not None:
            mtar_services._poller.join(timeout=5)
        mtar_services._poller, mtar_services._stop_poller = self._saved

    def test_one_checker_per_server(self):
        with (
            patch.object(mtar_services, "gmail_client", _Configured()),
            patch.object(mtar_services, "check_gmail_now"),
            patch.object(mtar_services, "drive_client", types.SimpleNamespace(configured=lambda: False)),
        ):
            first = mtar_services.start_background_checks()
            self.assertIs(mtar_services.start_background_checks(), first)
            self.assertTrue(first.is_alive())
            mtar_services._stop_poller.set()
            first.join(timeout=5)
            self.assertFalse(first.is_alive())

    def test_checker_from_code_before_an_update_is_switched_off(self):
        release = threading.Event()
        old_globals = {"gmail_client": _Configured(), "drive_client": _Configured(), "release": release}
        old = _old_style_checker(old_globals, release)
        try:
            with patch.object(mtar_services, "gmail_client", types.SimpleNamespace(configured=lambda: False)), \
                    patch.object(mtar_services, "drive_client", types.SimpleNamespace(configured=lambda: False)):
                self.assertIsNone(mtar_services.start_background_checks())
            self.assertFalse(old_globals["gmail_client"].configured())
            self.assertFalse(old_globals["drive_client"].configured())
        finally:
            release.set()
            old.join(timeout=5)

    def test_checker_with_a_stop_switch_is_stopped(self):
        stop = threading.Event()
        old = threading.Thread(target=stop.wait, name=mtar_services.POLLER_NAME, daemon=True)
        old.mtar_stop = stop
        old.start()
        with patch.object(mtar_services, "gmail_client", types.SimpleNamespace(configured=lambda: False)), \
                patch.object(mtar_services, "drive_client", types.SimpleNamespace(configured=lambda: False)):
            mtar_services.start_background_checks()
        old.join(timeout=5)
        self.assertFalse(old.is_alive())


if __name__ == "__main__":
    unittest.main()
