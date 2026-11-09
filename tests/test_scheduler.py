import unittest
import xml.dom.minidom
from datetime import datetime
from pathlib import Path

from invoice_triage import scheduler

APP = Path("/Users/ana/Invoice Triage")      # a path with a space, as people actually have
SCRIPT = APP / "run_weekly.pyw"


class NextRun(unittest.TestCase):
    def test_sunday_evening_runs_next_morning(self):
        self.assertEqual(scheduler.next_run(datetime(2026, 11, 8, 21, 0)), datetime(2026, 11, 9, 7, 0))

    def test_monday_after_seven_waits_a_week(self):
        self.assertEqual(scheduler.next_run(datetime(2026, 11, 9, 7, 1)), datetime(2026, 11, 16, 7, 0))

    def test_monday_before_seven_is_today(self):
        self.assertEqual(scheduler.next_run(datetime(2026, 11, 9, 6, 59)), datetime(2026, 11, 9, 7, 0))


class Builders(unittest.TestCase):
    def test_windows_task_is_valid_xml_with_catch_up_and_workdir(self):
        text = scheduler.windows_task_xml(r"C:\Python312\pythonw.exe", Path(r"C:\Users\ana\Invoice & Co\run_weekly.pyw"),
                                          Path(r"C:\Users\ana\Invoice & Co"), datetime(2026, 11, 9, 7))
        dom = xml.dom.minidom.parseString(text.replace('encoding="UTF-16"', 'encoding="UTF-8"'))
        get = lambda tag: dom.getElementsByTagName(tag)[0].firstChild.data
        self.assertEqual(get("StartWhenAvailable"), "true")
        self.assertEqual(get("WorkingDirectory"), r"C:\Users\ana\Invoice & Co")
        self.assertEqual(get("Arguments"), r'"C:\Users\ana\Invoice & Co\run_weekly.pyw"')
        self.assertEqual(get("StartBoundary"), "2026-11-09T07:00:00")
        self.assertTrue(dom.getElementsByTagName("Monday"))

    def test_launchd_plist_is_valid_and_monday_7(self):
        text = scheduler.launchd_plist("/usr/local/bin/python3", SCRIPT, APP, APP / "state/weekly.log")
        xml.dom.minidom.parseString(text)
        self.assertIn("<key>Weekday</key><integer>1</integer>", text)
        self.assertIn(f"<string>{APP}</string>", text)

    def test_systemd_quotes_paths_and_catches_up(self):
        service, timer = scheduler.systemd_units("/usr/bin/python3", SCRIPT, APP)
        self.assertIn(f'ExecStart="/usr/bin/python3" "{SCRIPT}"', service)
        self.assertIn(f"WorkingDirectory={APP}", service)
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnCalendar=Mon *-*-* 07:00:00", timer)

    def test_windows_prefers_windowless_python(self):
        self.assertEqual(scheduler._gui_python("/usr/bin/python3", "linux"), "/usr/bin/python3")


if __name__ == "__main__":
    unittest.main()
