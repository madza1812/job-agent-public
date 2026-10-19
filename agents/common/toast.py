"""A Windows toast notification, with no dependencies: Windows PowerShell's WinRT access.

Best effort only. Off Windows, without a logged-in desktop (e.g. a task set to run
whether the user is logged on or not), or on any error, nothing is shown and nothing
is raised.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

TIMEOUT_S = 20
# Toasts need a registered app id; Windows PowerShell's is always there.
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_SCRIPT = """\
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('{xml}')
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def script(title: str, message: str) -> str:
    """The PowerShell that shows the toast. Text is XML-escaped, then quoted for a
    single-quoted PowerShell string."""
    xml = ("<toast><visual><binding template='ToastGeneric'>"
           f"<text>{escape(title)}</text><text>{escape(message)}</text>"
           "</binding></visual></toast>")
    return _SCRIPT.format(xml=xml.replace("'", "''"), app_id=APP_ID)


def _powershell() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def notify(title: str, message: str, runner=subprocess.run) -> bool:
    """Show a toast; returns whether PowerShell reported success. Never raises."""
    if sys.platform != "win32":
        return False
    encoded = base64.b64encode(script(title, message).encode("utf-16-le")).decode("ascii")
    try:
        proc = runner(
            [str(_powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # best effort: a missing toast must never fail the run
        log.info("Toast not shown: %s", exc)
        return False
    if proc.returncode != 0:
        log.info("Toast not shown: PowerShell exit %s", proc.returncode)
        return False
    return True
