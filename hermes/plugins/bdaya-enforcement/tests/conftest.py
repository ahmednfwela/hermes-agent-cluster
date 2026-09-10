"""conftest for bdaya-enforcement tests.

The plugin directory name contains a dash, so the tests inject the plugin root
into ``sys.path`` and import ``hooks`` directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
