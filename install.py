# Copyright 2026 weewx-packages contributors
# See LICENSE.txt for your rights.
"""Install the manager under the station's user root without registering services."""

from weecfg.extension import ExtensionInstaller


def loader():
    return PackageManagerInstaller()


class PackageManagerInstaller(ExtensionInstaller):
    def __init__(self):
        # Installation succeeds independently of third-party Python dependencies.
        # The command needs packaging in the same environment as WeeWX.
        super().__init__(
            name='package-manager', version='0.1.0a1',
            description='Install and update WeeWX extensions from arbitrary repository indexes',
            author='weewx-packages contributors',
            files=[('bin/user/packages', [
                'bin/user/packages/__init__.py',
                'bin/user/packages/__main__.py',
                'bin/user/packages/cli.py',
                'bin/user/packages/engine.py',
                'bin/user/packages/extension_manager.py',
                'bin/user/packages/repository.py',
                'bin/user/packages/extension_state.py',
            ])])
