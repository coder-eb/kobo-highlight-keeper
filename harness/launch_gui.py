"""Start a calibre GUI that is fully isolated from the user's running calibre.

Run via harness/run_gui.py (which sets the environment), never directly:
  calibre-debug -e harness/launch_gui.py

calibre normally allows one GUI per macOS user: a second launch would hand
its arguments (our sandbox library!) to the running instance. The lock and
the GUI socket are per-user, not per-config-dir, so both are redirected into
sandbox/run before any GUI module is imported.
"""

import os
import sys

sandbox = os.environ['KHK_SANDBOX']
for var in ('CALIBRE_CONFIG_DIRECTORY', 'CALIBRE_CACHE_DIRECTORY', 'CALIBRE_TEMP_DIR'):
    if not os.environ.get(var, '').startswith(sandbox):
        sys.exit(f'refusing to start: {var} is not inside the sandbox')

import calibre.utils.ipc as ipc  # noqa: E402
import calibre.utils.lock as lock  # noqa: E402

sock = os.path.join(sandbox, 'run', 'gui.sock')
ipc.gui_socket_address = lambda: sock
lock.create_single_instance_mutex = lambda name, per_user=True: (lambda: None)

from calibre.gui2 import listener  # noqa: E402  (binds gui_socket_address by name)

if listener.gui_socket_address() != sock:
    sys.exit('refusing to start: GUI socket isolation failed')

# Never auto-detect USB/MTP devices: the sandbox must not connect to a real
# Kobo that happens to be plugged in. Folder-as-device mounts (the simulated
# Kobo) are handled separately by the device thread and keep working.
from calibre.gui2 import device as gui_device  # noqa: E402

_detect = gui_device.DeviceManager.detect_device


def _sandbox_detect_device(self):
    if self.is_device_connected and self.connected_device_kind in {'folder', 'folder-as-device'}:
        return _detect(self)


gui_device.DeviceManager.detect_device = _sandbox_detect_device

from calibre.gui2 import gprefs  # noqa: E402
from calibre.utils.config import config_dir  # noqa: E402

assert config_dir.startswith(sandbox), config_dir
for key in ('action-layout-toolbar', 'action-layout-toolbar-device'):
    layout = list(gprefs.get(key) or gprefs.defaults.get(key) or ())
    if 'Kobo Highlight Keeper' not in layout:
        layout.append('Kobo Highlight Keeper')
        gprefs[key] = layout

from calibre.gui2.main import main  # noqa: E402

sys.exit(main(['calibre', '--with-library', os.path.join(sandbox, 'library')]))
