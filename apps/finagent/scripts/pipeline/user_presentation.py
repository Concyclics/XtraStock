"""Share the chat renderer with standalone pipeline entry points."""
import sys

from common import ROOT

_package_root = str(ROOT / "MMClaw")
if _package_root not in sys.path:
    sys.path.insert(0, _package_root)

from mmclaw.presentation import publish_readable_report, render_report_file  # noqa: E402,F401

from mmclaw.reader_language import READER_STYLE_PROMPT, strip_boilerplate  # noqa: E402,F401
