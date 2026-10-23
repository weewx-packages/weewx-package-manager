#
#    Copyright (c) 2009-2026 Tom Keffer <tkeffer@gmail.com> and Matthew Wall
#
#    See the file LICENSE.txt for your rights.
#
# Modified 2026-10-23 by weewx-packages: isolate managed installer hooks from the core.
"""Run the WeeWX installer sequence with overridable writes on an unmodified core.

The method body follows WeeWX's ExtensionEngine. The separate class lets the
package manager journal file and configuration writes without replacing core files.
"""

import os
import shutil

import weecfg
import weecfg.extension
import weeutil.weeutil
import weewx


class InstallerEngine(weecfg.extension.ExtensionEngine):
    """Expose write hooks used by managed transactions."""

    def install_from_installer(self, extension_dir, installer_path, installer, extra_args=None):
        """Install files, services, and settings using an already loaded installer.

        Args:
            extension_dir (str): Directory containing the extension payload.
            installer_path (str): Path to install.py, which will be cached for removal.
            installer (weecfg.extension.ExtensionInstaller): Loaded installer with files, defaults,
                service registrations, and optional custom hooks.
            extra_args (list[str]|None): Arguments to pass to process_args().

        Returns:
            str: Installed extension name.
        """
        extension_name = installer.get('name', 'Unknown')
        self.printer.out(f"Found extension with name '{extension_name}'", level=2)

        # Install any files:
        if 'files' in installer:
            self._install_files(installer['files'], extension_dir)

        save_config = False

        # Go through all the possible service groups and see if the extension
        # includes any services that belong in any of them.
        self.printer.out("Adding services to service lists.", level=2)
        for service_group in weewx.all_service_groups:
            if service_group in installer:
                extension_svcs = weeutil.weeutil.option_as_list(installer[service_group])
                # Check to make sure the service group is in the configuration dictionary
                if service_group not in self.config_dict['Engine']['Services']:
                    self.config_dict['Engine']['Services'][service_group] = []
                # Be sure it's actually a list
                svc_list = weeutil.weeutil.option_as_list(
                    self.config_dict['Engine']['Services'][service_group])
                for svc in extension_svcs:
                    # See if this service is already in the service group
                    if svc not in svc_list:
                        if not self.dry_run:
                            # Add the new service into the appropriate service group
                            svc_list.append(svc)
                            self.config_dict['Engine']['Services'][service_group] = svc_list
                            save_config = True
                        self.printer.out(f"Added new service {svc} to {service_group}.", level=3)
        # Pass any extra arguments on to the installer
        if extra_args is not None:
            installer.process_args(extra_args)

        # Give the installer a chance to do any customized configuration
        save_config |= installer.configure(self)

        # Look for options that have to be injected into the configuration file
        if 'config' in installer:
            save_config |= self._inject_config(installer['config'], extension_name)

        # Save the extension's install.py file in the extension's installer
        # directory for later use enumerating and uninstalling
        extension_installer_dir = os.path.join(self.root_dict['EXT_DIR'], extension_name)
        self.printer.out(f"Saving installer file to {extension_installer_dir}")
        if not self.dry_run:
            self._cache_installer(installer_path, extension_installer_dir)

        if save_config:
            self._save_configuration()

        return extension_name

    def _cache_installer(self, installer_path, extension_installer_dir):
        """Save the installer used to enumerate and uninstall this extension later."""
        os.makedirs(extension_installer_dir, exist_ok=True)
        shutil.copy2(installer_path, extension_installer_dir)

    def _save_configuration(self):
        """Save engine changes with the usual backup; managed installs defer this step."""
        backup_path = weecfg.save_with_backup(self.config_dict, self.config_path)
        self.printer.out(f"Saved copy of configuration as {backup_path}")

