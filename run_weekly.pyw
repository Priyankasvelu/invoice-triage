# Started by the operating system's scheduler every Monday at 07:00.
# .pyw so that Windows runs it without opening a console window.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from invoice_triage.weekly import main  # noqa: E402

sys.exit(main())
