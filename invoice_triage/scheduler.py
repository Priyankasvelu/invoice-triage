"""Turn the Monday 07:00 run on or off with the operating system's own scheduler.

Windows: Task Scheduler (XML task: working directory + "run as soon as possible
after a missed start"). macOS: a launchd LaunchAgent. Linux: a systemd user timer
with Persistent=true. None of them wakes a sleeping computer; a missed Monday run
starts the next time the computer is on.

The builders are pure functions (unit-tested on any OS); apply/remove/status do
the system calls.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "Invoice Triage weekly"
LAUNCHD_LABEL = "com.invoicetriage.weekly"
SYSTEMD_NAME = "invoice-triage"
HOUR, MINUTE = 7, 0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def next_run(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    candidate = now.replace(hour=HOUR, minute=MINUTE, second=0, microsecond=0)
    candidate += timedelta(days=(0 - candidate.weekday()) % 7)        # Monday = 0
    return candidate if candidate > now else candidate + timedelta(days=7)


def _gui_python(executable: str, platform: str) -> str:
    """On Windows use pythonw.exe so no console window opens on Monday morning."""
    exe = Path(executable)
    if platform == "win32" and exe.name.lower() == "python.exe":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(exe)


# ---------- pure builders ----------

def windows_task_xml(python: str, script: Path, workdir: Path, start: datetime) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Checks new vendor invoices every Monday.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start.strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek><DaysOfWeek><Monday /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python)}</Command>
      <Arguments>{escape(f'"{script}"')}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def launchd_plist(python: str, script: Path, workdir: Path, log: Path) -> str:
    def s(v) -> str:
        return f"<string>{escape(str(v))}</string>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>{s(LAUNCHD_LABEL)}
  <key>ProgramArguments</key><array>{s(python)}{s(script)}</array>
  <key>WorkingDirectory</key>{s(workdir)}
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>{HOUR}</integer><key>Minute</key><integer>{MINUTE}</integer></dict>
  <key>StandardOutPath</key>{s(log)}
  <key>StandardErrorPath</key>{s(log)}
</dict>
</plist>
"""


def systemd_units(python: str, script: Path, workdir: Path) -> tuple[str, str]:
    service = f"""[Unit]
Description=Weekly vendor invoice check

[Service]
Type=oneshot
WorkingDirectory={workdir}
ExecStart="{python}" "{script}"
"""
    timer = f"""[Unit]
Description=Run the invoice check every Monday at {HOUR:02d}:{MINUTE:02d}

[Timer]
OnCalendar=Mon *-*-* {HOUR:02d}:{MINUTE:02d}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


# ---------- system calls ----------

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                          timeout=30, creationflags=_NO_WINDOW)


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def enable(app_dir: Path, platform: str = sys.platform) -> None:
    python = _gui_python(sys.executable, platform)
    script = app_dir / "run_weekly.pyw"
    if platform == "win32":
        xml = app_dir / "state" / "weekly-task.xml"
        xml.parent.mkdir(parents=True, exist_ok=True)
        xml.write_text(windows_task_xml(python, script, app_dir, next_run()), encoding="utf-16")
        result = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"])
    elif platform == "darwin":
        plist = _launchd_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        _run(["launchctl", "unload", str(plist)])
        plist.write_text(launchd_plist(python, script, app_dir, app_dir / "state" / "weekly.log"),
                         encoding="utf-8")
        result = _run(["launchctl", "load", "-w", str(plist)])
    else:
        unit_dir = _systemd_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        service, timer = systemd_units(python, script, app_dir)
        (unit_dir / f"{SYSTEMD_NAME}.service").write_text(service, encoding="utf-8")
        (unit_dir / f"{SYSTEMD_NAME}.timer").write_text(timer, encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        result = _run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_NAME}.timer"])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "the scheduler refused the job")


def disable(platform: str = sys.platform) -> None:
    if platform == "win32":
        _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    elif platform == "darwin":
        _run(["launchctl", "unload", "-w", str(_launchd_path())])
        _launchd_path().unlink(missing_ok=True)
    else:
        _run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_NAME}.timer"])
        for suffix in ("service", "timer"):
            (_systemd_dir() / f"{SYSTEMD_NAME}.{suffix}").unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])


def is_enabled(app_dir: Path, platform: str = sys.platform) -> bool:
    """True only if the job exists AND still points at this folder (it breaks if moved)."""
    try:
        if platform == "win32":
            out = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
            # The output's code page varies; match only ASCII parts and confirm the folder exists.
            return out.returncode == 0 and "run_weekly.pyw" in out.stdout and app_dir.is_dir()
        if platform == "darwin":
            p = _launchd_path()
            return p.exists() and str(app_dir) in p.read_text(encoding="utf-8")
        unit = _systemd_dir() / f"{SYSTEMD_NAME}.service"
        enabled = _run(["systemctl", "--user", "is-enabled", f"{SYSTEMD_NAME}.timer"]).stdout.strip()
        return enabled == "enabled" and unit.exists() and str(app_dir) in unit.read_text(encoding="utf-8")
    except (OSError, subprocess.SubprocessError):
        return False
