#!/usr/bin/env python
"""Entry point that works under ``python -I`` (isolated mode).

Under -I neither the current directory nor PYTHONPATH is searched, so the
repository root is inserted explicitly here.  Untrusted case files are only
ever passed as path arguments; nothing is imported from their directories.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from counterweir.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
