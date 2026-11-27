"""Kodi-only custom-window entry point."""

import os
import sys

import xbmcaddon

ADDON = xbmcaddon.Addon()
sys.path.insert(0, os.path.join(ADDON.getAddonInfo("path"), "resources", "lib"))

from channels_dvr.kodi_ui import open_window  # noqa: E402
from channels_dvr.ui_routes import launch_view  # noqa: E402

if __name__ == "__main__":
    # Kodi script arguments are closed view names, never URLs or commands.
    view = None if len(sys.argv) == 1 else launch_view(sys.argv[1] if len(sys.argv) == 2 else 'Dashboard')
    open_window(ADDON.getAddonInfo("path"), ADDON, start_view=view)
