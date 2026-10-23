#
#    See the file LICENSE.txt for your rights.
#    Modified 2026-10-23 by weewx-packages: standalone extension namespace.
#
"""Resolve extension sources and apply manual installations and updates.

The manager selects packages and plans ownership decisions before invoking the
existing ExtensionEngine. _ManagedEngine keeps its hook order and journals the
engine's writes. The inventory records which files and defaults may be removed by
later updates; it does not claim ownership of all settings named by an installer.
"""

import contextlib
import copy
import difflib
import os
import tempfile
import zipfile

import packaging.version

import weecfg
import weecfg.extension
from user.packages.engine import InstallerEngine
from user.packages import extension_state
from user.packages import repository
import weeutil.weeutil
from weeutil.printer import Printer
import weewx


def flatten(section, prefix=()):
    """Collect configuration leaves by their complete section/key paths.

    Args:
        section (dict): Configuration section to traverse.
        prefix (tuple[str, ...]): Path of the section within its parent configuration.

    Returns:
        dict: Tuple paths mapped to independent copies of their values, suitable
            for comparison after an installer modifies the configuration.
    """
    result = {}
    for name, value in section.items():
        key = prefix + (name,)
        if isinstance(value, dict):
            result.update(flatten(value, key))
        else:
            result[key] = copy.deepcopy(value)
    return result


def leaf(config, section, key):
    """Read a configuration value without creating missing sections.

    Args:
        config (dict): Configuration containing the setting.
        section (Iterable[str]): Section path; empty for a top-level setting.
        key (str): Name of the scalar setting.

    Returns:
        Any: Setting value, or None when the section or key is absent.
    """
    current = config
    for name in section:
        if name not in current or not isinstance(current[name], dict):
            return None
        current = current[name]
    return current.get(key)


def remove_leaf(config, section, key):
    """Remove one setting and any parent sections left empty by its removal.

    Args:
        config (dict): Configuration to change in memory.
        section (Iterable[str]): Section path; empty for a top-level setting.
        key (str): Setting to remove if present.
    """
    current = config
    parents = []
    for name in section:
        if name not in current or not isinstance(current[name], dict):
            return
        parents.append((current, name))
        current = current[name]
    current.pop(key, None)
    for parent, name in reversed(parents):
        if parent[name]:
            break
        del parent[name]


class _ManagedEngine(InstallerEngine):
    """Keep the legacy hook order while routing the engine's writes through a journal."""

    def __init__(self, manager, transaction, plan):
        """Use the station configuration and the file choices already approved.

        Args:
            manager (ExtensionManager): Manager coordinating this operation.
            transaction (extension_state.Transaction): Active file transaction.
            plan (list[dict]): File operations returned by _file_plan().
        """
        super().__init__(manager.config_path, manager.config, printer=manager.printer)
        self.manager = manager
        self.transaction = transaction
        self.plan = plan
        self.added_configuration = {}

    def _install_files(self, file_list, extension_dir):
        """Apply only planned file replacements, checking their source contents again."""
        paths = self.manager.file_paths(file_list, extension_dir)
        if set(paths) != set(item['target'] for item in self.plan if item.get('source')):
            raise repository.RepositoryError('Installer file list changed after planning.', 4)
        for item in self.plan:
            if item.get('source') and not item['keep']:
                if repository.digest(item['source']) != item['source_sha256']:
                    raise repository.RepositoryError('Package file changed after planning.', 4)
                self.transaction.write(item['target'], source=item['source'])
        return len(paths)

    def _cache_installer(self, installer_path, extension_installer_dir):
        """Include the cached installer in the same rollback as its package files."""
        self.transaction.write(os.path.join(extension_installer_dir, 'install.py'),
                               source=installer_path)

    def _save_configuration(self):
        """Defer the engine's save until the manager finishes ownership changes."""
        # The manager saves once, after obsolete owned settings have also been handled.
        pass

    def _inject_config(self, extension_config, extension_name):
        """Apply normal defaults and remember only the settings newly introduced."""
        before = flatten(self.config_dict)
        result = super()._inject_config(extension_config, extension_name)
        after = flatten(self.config_dict)
        self.added_configuration = {key: value for key, value in after.items() if key not in before}
        return result


class ExtensionManager:
    """Coordinate source selection, installer execution, ownership, and recovery."""

    def __init__(self, config, dry_run=False, printer=None):
        """Create a manager for one station configuration.

        Args:
            config (dict): Parsed WeeWX configuration, including config_path.
            dry_run (bool): Preview changes without writing package or manager state.
            printer (Printer|None): Output destination respecting WeeWX verbosity levels.
        """
        self.config = config
        self.config_path = os.path.realpath(config['config_path'])
        self.dry_run = dry_run
        self.printer = printer or Printer()
        self.registry = repository.Registry(config)
        self.store = extension_state.StateStore(config)
        self.configuration_fingerprint = extension_state.fingerprint(self.config_path)

    def reload(self):
        """Reload configuration and rebuild its source and installation-state readers."""
        _, self.config = weecfg.read_config(self.config_path)
        self.registry = repository.Registry(self.config)
        self.store = extension_state.StateStore(self.config)

    @contextlib.contextmanager
    def operation(self, recovery=False):
        """Validate local state and coordinate access for one station operation.

        Args:
            recovery (bool): True to permit recovery of unfinished transactions.

        Yields:
            None: Control with refreshed state and writer locks held. Dry runs
                validate the current state without taking locks or refreshing it.
        """
        if self.dry_run:
            if not recovery and self.store.pending():
                raise repository.RepositoryError('Run python -m user.packages recover first.', 3)
            self.store.all()
            yield
            return
        locked_root = self.store.extension_root
        with self.store.locked(recovery=recovery):
            # Another command may have committed while this one waited for locks.
            # Reload after locking, then ensure we still protect the same EXT_DIR.
            before = extension_state.fingerprint(self.config_path)
            self.reload()
            self.configuration_fingerprint = extension_state.fingerprint(self.config_path)
            if before != self.configuration_fingerprint:
                raise repository.RepositoryError('Configuration changed while reading it.', 4)
            if self.store.extension_root != locked_root:
                raise repository.RepositoryError('Installation roots changed while waiting.', 3)
            self.store.all()
            yield

    def _gen_file_paths(self, file_list, extension_dir=''):
        """Map legacy archive destinations onto the station's configured roots.

        Args:
            file_list (list[tuple]): Installer pairs of destination and source names.
            extension_dir (str): Extracted package root. An empty string is enough
                when reading only destinations from a cached legacy installer.

        Yields:
            tuple[str, str]: A two-way tuple (source_path, destination_path), with
                an absolute destination and a source relative to extension_dir.
        """
        station = self.store.root_dict['WEEWX_ROOT']
        for source, destination in weecfg.extension.ExtensionEngine._gen_file_paths(
                station, extension_dir, file_list):
            # Archive paths use the traditional layout even when the station's roots differ.
            for logical, actual in (('bin/user', self.store.roots['user']),
                                    ('skins', self.store.roots['skins'])):
                prefix = os.path.join(station, logical)
                if os.path.commonpath([os.path.abspath(destination), prefix]) == prefix:
                    if actual is None:
                        raise repository.RepositoryError('Configure a skin root first.', 3)
                    destination = os.path.join(actual, os.path.relpath(destination, prefix))
                    break
            yield source, os.path.abspath(destination)

    def file_paths(self, file_list, extension_dir):
        """Validate a file list before it becomes an installation plan.

        Args:
            file_list (list[tuple]): Destination/source pairs from the installer.
            extension_dir (str): Extracted package root containing regular source files.

        Returns:
            dict: Absolute destination paths mapped to absolute source paths.
                Sources must remain inside the package and destinations inside
                the managed roots. Duplicate destinations are errors.
        """
        paths = {}
        for source, destination in self._gen_file_paths(file_list, extension_dir):
            source = os.path.abspath(source)
            if (not repository.within(source, extension_dir) or
                    not os.path.isfile(source) or os.path.islink(source)):
                raise repository.RepositoryError('Invalid package source file: ' + source)
            self.store.locate(destination)
            if destination in paths:
                raise repository.RepositoryError('Duplicate target file: ' + destination)
            paths[destination] = source
        return paths

    def _package(self, alias, package_id, refresh=True):
        """Return an exact package from one source, without cross-source fallback."""
        entry = self.registry.get(alias, refresh=refresh, persist=not self.dry_run)
        for package in entry['catalog']['packages']:
            if package['id'] == package_id:
                return package
        raise repository.RepositoryError('Package %s is absent from repository %s.' %
                                               (package_id, alias))

    def _recorded_digest(self, state, package):
        """Refuse a changed installed-release archive even after the index cache is lost."""
        if state and state['archive_sha256']:
            for release in package['releases']:
                if packaging.version.Version(release['version']) == packaging.version.Version(
                        state['version']) and release['archive']['sha256'] != state['archive_sha256']:
                    raise repository.RepositoryError(
                        'Installed release archive changed without a new version.', 4)

    def install(self, source, alias=None, version=None, pre=False, yes=False, extra_args=None):
        """Resolve a package ID or direct source, then request its installation.

        Args:
            source (str): Exact package ID, local path, or archive URL.
            alias (str|None): Repository to use for an unbound package. An existing
                binding must be changed explicitly through switch_source().
            version (str|None): Exact repository release, or None for normal selection.
            pre (bool): True to allow pre-release repository versions.
            yes (bool): Confirm the operation without input. Source and conflict
                choices still require explicit terminal selections.
            extra_args (list[str]|None): Arguments to pass to the selected installer.
        """
        explicit = (source.startswith(('http:', 'https:', 'file:', '/', './', '../', '~')) or
                    '/' in source or source.endswith(('.zip', '.tgz', '.tar', '.tar.gz',
                                                      '.tar.bz2', '.tar.xz')))
        if explicit:
            if alias or version is not None or pre:
                raise repository.RepositoryError('Repository options require a package ID.')
            return self.install_direct(source, yes, extra_args)
        local = os.path.lexists(source)
        if not repository.SLUG.fullmatch(source):
            if local and not alias:
                return self.install_direct(source, yes, extra_args)
            raise repository.RepositoryError('Invalid package ID; use a path or search.')
        states = self.store.all()
        bound = next((item for item in states.values() if item['package_id'] == source), None)
        # The saved UUID survives alias changes. Once installed, a package never
        # switches source just because another repository advertises a newer release.
        if bound:
            saved_alias = self.registry.alias_for_id(bound['source_id'])
            if alias and alias != saved_alias:
                raise repository.RepositoryError('Use switch-source to change an installed source.')
            aliases = [saved_alias]
        else:
            aliases = [alias] if alias else [name for name in sorted(self.registry.sources)
                                            if self.registry.sources[name]['enabled']]
        matches = []
        failures = []
        for name in aliases:
            try:
                entry = self.registry.get(name, refresh=True, persist=not self.dry_run)
                for package in entry['catalog']['packages']:
                    if package['id'] == source:
                        release = repository.select_release(package, version, pre)
                        matches.append((name, package, release))
            except repository.RepositoryError as error:
                failures.append((name, error))
        if failures:
            # Choosing from a partial lookup could hide the user's intended source.
            # An explicit --repo limits the lookup when another source is unavailable.
            for name, failure in failures:
                print('Repository %s: %s' % (name, failure))
            raise repository.RepositoryError(
                'Incomplete lookup. Use --repo to select a reachable source or ./ for a local path.',
                max(failure.code for _, failure in failures))
        if local and not alias:
            matches.append((None, None, None))
        if not matches:
            raise repository.RepositoryError('No exact package %s. Try python -m user.packages search %s.'
                                                   % (source, source))
        if len(matches) == 1:
            index = 0
        else:
            labels = []
            for name, package, release in matches:
                if name is None:
                    labels.append('Local path: ' + os.path.abspath(source))
                    continue
                latest = max(package['releases'],
                             key=lambda item: packaging.version.Version(item['version']))
                compatible = release['version'] if release else 'unavailable: ' + '; '.join(
                    repository.incompatibility(item) for item in package['releases'])
                labels.append('%s | %s | %s | latest %s | compatible %s | %s' % (
                    name, self.registry.sources[name]['url'], package['name'], latest['version'],
                    compatible, package['description']))
            while True:
                index = repository.choose('Choose source', labels)
                if matches[index][0] is None or matches[index][2] is not None:
                    break
                print('That source has no compatible release.')
        name, package, release = matches[index]
        if name is None:
            return self.install_direct(source, yes, extra_args)
        if release is None:
            raise repository.RepositoryError('No compatible release for %s: %s' % (
                source, '; '.join(repository.incompatibility(item)
                                  for item in package['releases'])))
        return self.install_package(name, package, release, yes, extra_args, version is not None)

    def install_package(self, alias, package, release, yes=False, extra_args=None,
                        downgrade=False, switching=False):
        """Acquire a selected release and install it after verifying its identity.

        Args:
            alias (str): Source from which the package was selected.
            package (dict): Validated catalog package, including installer_name.
            release (dict): Selected compatible release from that package.
            yes (bool): Confirm a specified operation without input.
            extra_args (list[str]|None): Arguments for the installer's process_args().
            downgrade (bool): True when an exact version was explicitly requested.
            switching (bool): True for an explicit source switch. This permits a
                new binding and runs installation even for an unchanged version.
        """
        # Download outside the writer locks, then check that neither the chosen
        # source nor the installed inventory changed while the download was running.
        snapshot = copy.deepcopy(self.registry.source(alias))
        state_snapshot = extension_state.fingerprint(
            self.store.state_path(package['installer_name']))
        with tempfile.TemporaryDirectory() as temporary:
            archive = os.path.join(temporary, 'package')
            self.registry.downloader.fetch(release['archive']['url'], archive,
                                            self.registry.options['archive_max_bytes'])
            if repository.digest(archive) != release['archive']['sha256']:
                raise repository.RepositoryError('Archive SHA-256 mismatch.', 4)
            directory = self.registry.downloader.extract(archive, release['archive']['format'],
                                                          os.path.join(temporary, 'unpacked'))
            with self.operation():
                if self.registry.source(alias) != snapshot:
                    raise repository.RepositoryError('Source changed during acquisition.', 3)
                if extension_state.fingerprint(self.store.state_path(
                        package['installer_name'])) != state_snapshot:
                    raise repository.RepositoryError('Installation changed during acquisition.', 3)
                state = self.store.read(package['installer_name'])
                if state and not switching:
                    if state['source_id'] != snapshot['source_id'] or state['package_id'] != package['id']:
                        raise repository.RepositoryError('Installed target has another source binding.')
                    self._recorded_digest(state, package)
                    current = packaging.version.Version(state['version'])
                    selected = packaging.version.Version(release['version'])
                    if selected < current and not downgrade:
                        print('%s is ahead of the catalog; no downgrade.' % package['installer_name'])
                        return
                    if selected == current and state['archive_sha256'] == release['archive']['sha256']:
                        print('%s %s is already installed.' % (package['id'], state['version']))
                        return
                for name, other in self.store.all().items():
                    if other['package_id'] == package['id'] and name != package['installer_name']:
                        raise repository.RepositoryError('Package ID already has an installed binding.')
                # Loading install.py executes publisher code. The archive digest
                # and extraction boundaries have already been checked at this point.
                installer_path, installer = weecfg.get_extension_installer(directory)
                if (installer.get('name') != package['installer_name'] or
                        packaging.version.Version(installer.get('version', '')) !=
                        packaging.version.Version(release['version'])):
                    raise repository.RepositoryError('Installer name/version differs from catalog.')
                if not state and os.path.exists(os.path.dirname(self.store.state_path(installer['name']))):
                    raise repository.RepositoryError('Existing legacy extension requires adopt first.')
                self._apply(directory, installer_path, installer, state, snapshot['source_id'],
                            package['id'], release['archive']['sha256'], yes, extra_args,
                            release['restart_required'])

    def install_direct(self, source, yes=False, extra_args=None):
        """Install a directory or archive without creating a repository binding.

        Args:
            source (str): Package directory, local archive path, or archive URL.
            yes (bool): Confirm a specified operation without input.
            extra_args (list[str]|None): Arguments for the installer.

        Installs confined to managed roots retain ownership records. Untracked
        legacy packages targeting other directories use the existing engine
        without transaction recovery. A bound package must be detached first.
        """
        with tempfile.TemporaryDirectory() as temporary:
            location = os.path.expanduser(source)
            checksum = None
            if os.path.isdir(location):
                directory = os.path.abspath(location)
            else:
                archive = os.path.join(temporary, 'package')
                self.registry.downloader.fetch(repository.url(location), archive,
                                                self.registry.options['archive_max_bytes'])
                checksum = repository.digest(archive)
                file_format = 'zip' if zipfile.is_zipfile(archive) else 'tar'
                directory = self.registry.downloader.extract(archive, file_format,
                                                              os.path.join(temporary, 'unpacked'))
            with self.operation():
                installer_path, installer = weecfg.get_extension_installer(directory)
                state = self.store.read(installer['name'])
                if state and state['source_id'] is not None:
                    raise repository.RepositoryError('Detach the bound extension before direct replacement.')
                outside = any(not any(repository.within(os.path.dirname(target), root)
                                      for root in self.store.roots.values() if root)
                              for _, target in self._gen_file_paths(installer.get('files', [])))
                if outside:
                    if state:
                        raise repository.RepositoryError(
                            'Tracked extensions must target configured user/skin roots.')
                    print('Targets outside user/skin roots require the legacy installer; '
                          'repository tracking and recovery are unavailable.')
                    if not self.dry_run:
                        repository.confirm('Install this legacy extension?', yes)
                    engine = InstallerEngine(self.config_path, self.config,
                                                              self.dry_run, self.printer)
                    return engine.install_from_installer(directory, installer_path, installer, extra_args)
                self._apply(directory, installer_path, installer, state, None, None, checksum,
                            yes, extra_args, True)

    def _other_owners(self, own_name):
        """Find targets reserved by other tracked or legacy extensions.

        Args:
            own_name (str): Installed extension to exclude from the ownership lookup.

        Returns:
            dict: Absolute target paths mapped to other installed names. Legacy
                inventories require loading their cached Python installers; an
                unreadable inventory prevents a safe ownership decision.
        """
        owners = {}
        states = self.store.all()
        for name, state in states.items():
            if name == own_name:
                continue
            for item in state['files']:
                owners[self.store.target(item)] = name
        extension_root = self.store.extension_root
        if os.path.isdir(extension_root):
            for name in os.listdir(extension_root):
                if name == own_name or name in states:
                    continue
                directory = os.path.join(extension_root, name)
                if not os.path.isdir(directory):
                    continue
                try:
                    _, installer = weecfg.get_extension_installer(directory)
                    for _, target in self._gen_file_paths(installer.get('files', [])):
                        owners[os.path.abspath(target)] = name
                except Exception as error:
                    raise repository.RepositoryError('Cannot establish legacy ownership for %s: %s'
                                                           % (name, error), 3)
        return owners

    def _file_plan(self, paths, state, name):
        """Plan replacement, retention, and removal without changing package files.

        Args:
            paths (dict): New absolute destinations mapped to package source files.
            state (dict|None): Previous inventory, or None for an untracked install.
            name (str): Installed extension whose files are being planned.

        Returns:
            list[dict]: Actions containing target, optional source, keep decision,
                updated inventory record, and observed target fingerprint. New
                sources also have a digest to check before copying. Real runs
                ask about conflicts; dry runs report them and plan to keep them.
        """
        owners = self._other_owners(name)
        old = {self.store.target(item): item for item in state['files']} if state else {}
        plan = []
        for target, source in sorted(paths.items()):
            if target in owners:
                raise repository.RepositoryError('%s is owned by %s.' % (target, owners[target]))
            record = self.store.locate(target)
            prior = old.pop(target, None)
            current = extension_state.fingerprint(target)
            # A preserved file stays a conflict even if it still matches its saved
            # digest. That digest describes local bytes, not a publisher baseline.
            conflict = (prior is not None and (prior['preserved'] or prior['sha256'] is None or
                        current != {'kind': 'file', 'sha256': prior['sha256']}))
            conflict = conflict or (prior is None and current['kind'] != 'absent')
            keep = False
            if conflict:
                self._show_diff(target, source)
                if self.dry_run:
                    print('Requires an explicit keep/replace decision: ' + target)
                    keep = True
                else:
                    keep = repository.choose('Resolve ' + target,
                                                    ['Keep local file', 'Replace after backup']) == 0
            record.update(sha256=current.get('sha256') if keep else repository.digest(source),
                          preserved=keep, replacement=prior.get('replacement') if prior else None)
            if not keep:
                record['replacement'] = None
            plan.append({'target': target, 'source': source, 'keep': keep, 'record': record,
                         'source_sha256': repository.digest(source), 'observed': current})
        for target, prior in sorted(old.items()):
            # Entries left in the old inventory are no longer shipped. Remove
            # unchanged owned files, but ask before discarding a local modification.
            current = extension_state.fingerprint(target)
            if current['kind'] == 'absent':
                continue
            keep = False
            if prior['preserved'] or current != {'kind': 'file', 'sha256': prior['sha256']}:
                if self.dry_run:
                    print('Requires an explicit retain/remove decision: ' + target)
                    keep = True
                else:
                    keep = repository.choose('Obsolete file ' + target,
                                                    ['Retain local file', 'Remove after backup']) == 0
            record = copy.deepcopy(prior)
            record.update(sha256=current.get('sha256'), preserved=keep)
            plan.append({'target': target, 'source': None, 'keep': keep, 'record': record,
                         'observed': current})
        return plan

    def _show_diff(self, target, source):
        """Show a bounded, escaped text diff, or explain a non-text file conflict."""
        if not os.path.isfile(target) or os.path.islink(target):
            print('Local path differs from its recorded baseline: ' + target)
            return
        try:
            with open(target, encoding='utf-8') as stream:
                old = stream.read(262144).splitlines(True)
            with open(source, encoding='utf-8') as stream:
                new = stream.read(262144).splitlines(True)
            for line in difflib.unified_diff(old, new, fromfile=target, tofile='proposed release'):
                print(repository.display(line.rstrip('\n')))
        except UnicodeError:
            print('Binary file changed: ' + target)

    def _obsolete_settings(self, state, requested_config, requested_services):
        """Decide which formerly owned settings and services may be removed.

        Args:
            state (dict|None): Previous inventory, or None for a new installation.
            requested_config (dict): Flattened defaults still requested by the installer.
            requested_services (set[tuple[str, str]]): Requested (group, service) pairs.

        Returns:
            tuple[list, list]: Configuration and service decisions. Each list holds
                (inventory_entry, remove) pairs. Modified settings require a
                choice; services are removed only if introduced and not shared.
        """
        configuration = []
        services = []
        if not state:
            return configuration, services
        for item in state['config_entries']:
            key = tuple(item['section']) + (item['key'],)
            if key in requested_config:
                continue
            current = leaf(self.config, item['section'], item['key'])
            if current is None:
                configuration.append((item, True))
                continue
            remove = current == item['value']
            if not remove and not self.dry_run:
                remove = repository.choose('Obsolete modified setting ' + '.'.join(key),
                                                  ['Keep setting', 'Remove setting']) == 1
            print('%s setting %s' % ('Remove' if remove else 'Keep', '.'.join(key)))
            configuration.append((item, remove))
        other_services = {(item['group'], item['service'])
                          for name, other in self.store.all().items()
                          if name != state['installer_name'] for item in other['service_entries']}
        for item in state['service_entries']:
            key = item['group'], item['service']
            if key not in requested_services:
                services.append((item, item['introduced'] and key not in other_services))
        return configuration, services

    def _remove_settings(self, configuration, services):
        """Apply approved setting and service removals to the in-memory configuration."""
        for item, remove in configuration:
            if remove:
                remove_leaf(self.config, item['section'], item['key'])
        groups = self.config.get('Engine', {}).get('Services', {})
        for item, remove in services:
            if remove:
                groups[item['group']] = [value for value in weeutil.weeutil.option_as_list(
                    groups.get(item['group'], [])) if value != item['service']]

    def _observations(self, plan):
        """Collect planning fingerprints for the transaction's final edit check."""
        observations = {item['target']: item['observed'] for item in plan}
        observations[self.config_path] = self.configuration_fingerprint
        return observations

    def _apply(self, directory, installer_path, installer, state, source_id, package_id,
               archive_sha256, yes, extra_args, restart):
        """Plan and commit one install while holding the station writer locks.

        Args:
            directory (str): Package directory containing the payload files.
            installer_path (str): Already loaded install.py to cache on success.
            installer (weecfg.extension.ExtensionInstaller): Loaded package installer.
            state (dict|None): Previous inventory, or None for an untracked install.
            source_id (str|None): Bound source UUID; None for direct installations.
            package_id (str|None): Bound package ID; None for direct installations.
            archive_sha256 (str|None): Acquired archive digest; None for directories.
            yes (bool): Confirm the specified operation without input.
            extra_args (list[str]|None): Arguments for the installer's process_args().
            restart (bool): True to print a restart notice after a successful install.

        Dry runs stop after planning. Real runs save payload, configuration, cached
        installer, and inventory in one recoverable transaction. Custom hooks can
        have additional effects outside that transaction's file records.
        """
        name = installer['name']
        self.store.state_path(name)
        paths = self.file_paths(installer.get('files', []), directory)
        plan = self._file_plan(paths, state, name)
        requested_config = flatten(installer.get('config', {}))
        requested_services = {(group, service) for group in weewx.all_service_groups
                              for service in weeutil.weeutil.option_as_list(installer.get(group, []))}
        obsolete_config, obsolete_services = self._obsolete_settings(
            state, requested_config, requested_services)
        old_version = state['version'] if state else 'not tracked'
        print('%s: %s -> %s' % (name, old_version, installer.get('version', 'unknown')))
        for item in plan:
            print('%s %s' % ('Keep' if item['keep'] else 'Install' if item['source'] else 'Remove',
                             item['target']))
        custom_process = type(installer).process_args is not weecfg.extension.ExtensionInstaller.process_args
        custom_configure = type(installer).configure is not weecfg.extension.ExtensionInstaller.configure
        if custom_process or custom_configure:
            print('Custom installer hooks can change more than the engine-managed plan.')
        if self.dry_run:
            print('Dry run: custom hooks were not invoked; no files or state changed.')
            return
        repository.confirm('Apply this extension operation?', yes)
        state_path = self.store.state_path(name)
        installer_target = os.path.join(os.path.dirname(state_path), 'install.py')
        targets = [item['target'] for item in plan if not item['keep']]
        targets += [self.config_path, state_path, installer_target]
        before_services = {(group, service) for group in weewx.all_service_groups
                           for service in weeutil.weeutil.option_as_list(
                               self.config.get('Engine', {}).get('Services', {}).get(group, []))}
        with extension_state.Transaction(self.store, name, 'install', targets) as transaction:
            transaction.check_observations(self._observations(plan))
            engine = _ManagedEngine(self, transaction, plan)
            original_process = installer.process_args
            original_configure = installer.configure

            # Mark custom hooks before invoking them. A hook can fail or the process
            # can die after an external change but before control returns here.
            def process_args(arguments):
                if custom_process:
                    transaction.hook_started()
                return original_process(arguments)

            def configure(active_engine):
                if custom_configure:
                    transaction.hook_started()
                return original_configure(active_engine)

            installer.process_args = process_args
            installer.configure = configure
            engine.install_from_installer(directory, installer_path, installer, extra_args)
            # Hooks can add defaults to the installer. Do not remove a formerly
            # owned setting that the final injection still requests.
            requested_config = flatten(installer.get('config', {}))
            obsolete_config = [(item, remove) for item, remove in obsolete_config
                               if tuple(item['section']) + (item['key'],) not in requested_config]
            for item in plan:
                if not item['source'] and not item['keep']:
                    transaction.remove(item['target'])
                if item['keep'] and item['source']:
                    item['record']['replacement'] = transaction.candidate(item['source'], item['record'])
            self._remove_settings(obsolete_config, obsolete_services)
            transaction.write_configuration(self.config)
            _, saved_config = weecfg.read_config(self.config_path)
            # Only newly injected settings become owned. Reread ConfigObj's saved
            # values so future comparisons use the serialized strings and lists.
            config_entries = {(tuple(item['section']), item['key']): copy.deepcopy(item)
                              for item in state['config_entries']} if state else {}
            for item, remove in obsolete_config:
                if remove:
                    config_entries.pop((tuple(item['section']), item['key']), None)
            for key in engine.added_configuration:
                if key[:2] == ('Engine', 'Services'):
                    continue
                config_entries[(key[:-1], key[-1])] = {
                    'section': list(key[:-1]), 'key': key[-1],
                    'value': leaf(saved_config, key[:-1], key[-1])}
            previous_services = {(item['group'], item['service']): item for item in
                                 state['service_entries']} if state else {}
            service_entries = []
            for group, service in sorted(requested_services):
                prior = previous_services.get((group, service))
                service_entries.append({'group': group, 'service': service,
                                        'introduced': prior['introduced'] if prior else
                                        (group, service) not in before_services})
            service_entries.extend(copy.deepcopy(item) for item, remove in obsolete_services if not remove)
            files = []
            for item in plan:
                if item['source'] or item['keep']:
                    record = item['record']
                    current = extension_state.fingerprint(item['target'])
                    record['sha256'] = current.get('sha256')
                    files.append(record)
            new_state = {'schema_version': 1, 'transaction_id': transaction.journal['id'],
                         'config_path': self.config_path, 'roots': self.store.roots,
                         'source_id': source_id, 'package_id': package_id,
                         'installer_name': name, 'version': str(installer.get('version', 'unknown')),
                         'archive_sha256': archive_sha256,
                         'installed_at': repository.utc_now(), 'files': files,
                         'config_entries': list(config_entries.values()),
                         'service_entries': service_entries}
            self.store.validate(new_state, name)
            # Inventory must describe the files already written. Commit comes last
            # so interruption before this point leaves a recoverable operation.
            transaction.write_json(state_path, new_state)
            transaction.commit()
        self.store.prune()
        print('Installed %s %s.' % (name, installer.get('version')))
        if restart:
            print('Restart WeeWX to use the updated extension.')

    def updates(self, names=(), version=None, pre=False, check=False, yes=False, extra_args=None):
        """Check or apply independent updates using each extension's bound source.

        Args:
            names (list[str]|tuple[str, ...]): Installed names to process, or empty for all.
            version (str|None): Exact target version, requiring one installed name.
            pre (bool): True to include pre-release versions.
            check (bool): True to report candidates without acquiring their archives.
            yes (bool): Confirm specified update operations without input.
            extra_args (list[str]|None): Installer arguments, requiring one name.

        Legacy and detached packages are skipped. Package failures do not undo
        earlier commits; an unfinished transaction stops subsequent mutations.
        A failed batch raises RepositoryError with the highest encountered status.
        """
        states = self.store.all()
        if version is not None and len(names) != 1:
            raise repository.RepositoryError('--version requires exactly one installed name.')
        if extra_args and len(names) != 1:
            raise repository.RepositoryError('Installer arguments require one installed name.')
        legacy = set(os.listdir(self.store.extension_root)) - set(states) if os.path.isdir(
            self.store.extension_root) else set()
        legacy = {name for name in legacy if os.path.isfile(
            os.path.join(self.store.extension_root, name, 'install.py'))}
        if names:
            for name in names:
                if name not in states and name not in legacy:
                    raise repository.RepositoryError('No tracked extension: ' + name, 3)
        selected = sorted(names or set(states) | legacy)
        failures = []
        outcomes = {'completed': [], 'skipped': [], 'cancelled': [], 'failed': []}
        if not check and not self.dry_run and selected:
            repository.confirm('Check and apply updates to %s?' % ', '.join(selected), yes)
        for name in selected:
            try:
                state = states.get(name)
                if state is None or state['source_id'] is None:
                    print('Skipped %s extension: %s' % ('legacy' if state is None else 'detached', name))
                    outcomes['skipped'].append(name)
                    continue
                alias = self.registry.alias_for_id(state['source_id'])
                if not self.registry.sources[alias]['enabled'] and not names:
                    print('Skipped disabled source: ' + name)
                    outcomes['skipped'].append(name)
                    continue
                package = self._package(alias, state['package_id'])
                self._recorded_digest(state, package)
                release = repository.select_release(package, version, pre)
                if release is None:
                    raise repository.RepositoryError('No compatible release for ' + name)
                print('%s [%s]: installed %s, compatible %s' %
                      (name, alias, state['version'], release['version']))
                current = packaging.version.Version(state['version'])
                installed = next((item for item in package['releases']
                                  if packaging.version.Version(item['version']) == current), None)
                if installed and repository.incompatibility(installed):
                    print('Installed release: ' + repository.incompatibility(installed))
                    if check or (packaging.version.Version(release['version']) <= current
                                 and version is None):
                        raise repository.RepositoryError('Installed release requires attention.')
                if not check:
                    self.install_package(alias, package, release, yes, extra_args,
                                         downgrade=version is not None)
                outcomes['completed'].append(name)
            except Exception as error:
                if isinstance(error, repository.RepositoryError):
                    failure = error
                else:
                    failure = repository.RepositoryError(str(error), 4)
                print('Failed %s: %s' % (name, failure))
                outcomes['cancelled' if failure.code == 1 else 'failed'].append(name)
                failures.append(failure)
                if self.store.pending():
                    # An ordinary package failure can be isolated. Unresolved
                    # recovery affects the whole station, so stop the batch here.
                    outcomes['skipped'].extend(selected[selected.index(name) + 1:])
                    break
        for outcome, packages in outcomes.items():
            print('%s: %s' % (outcome.capitalize(), ', '.join(packages) or '-'))
        if failures:
            raise repository.RepositoryError('One or more extension operations failed.',
                                                   max(error.code for error in failures))

    def adopt(self, name, alias, package_id, yes=False):
        """Bind an existing installation to a matching published name and version.

        Args:
            name (str): Installed extension name, currently unbound or legacy.
            alias (str): Source whose published identity should be recorded.
            package_id (str): Exact package ID in that source.
            yes (bool): Confirm the proposed binding without input.

        Known inventories are retained. Legacy files get unknown baselines and
        no inferred configuration ownership because matching names and versions
        do not establish which bytes or defaults the old installer wrote.
        """
        package = self._package(alias, package_id)
        source = copy.deepcopy(self.registry.source(alias))
        with self.operation():
            if self.registry.source(alias) != source:
                raise repository.RepositoryError('Source changed during acquisition.', 3)
            state = self.store.read(name)
            if state and state['source_id'] is not None:
                raise repository.RepositoryError('Extension already has a binding; use switch-source.')
            for other in self.store.all().values():
                if other['package_id'] == package_id:
                    raise repository.RepositoryError('Package ID already has an installed binding.')
            _, installer = weecfg.get_extension_installer(os.path.dirname(self.store.state_path(name)))
            if installer['name'] != package['installer_name']:
                raise repository.RepositoryError('Installer name does not match repository package.')
            version = str(installer['version'])
            repository.select_release(package, version, pre=True)
            files = state['files'] if state else []
            if not state:
                for _, target in self._gen_file_paths(installer.get('files', [])):
                    record = self.store.locate(target)
                    record.update(sha256=None, preserved=False, replacement=None)
                    files.append(record)
            owners = self._other_owners(name)
            for item in files:
                target = self.store.target(item)
                if target in owners:
                    raise repository.RepositoryError('%s is owned by %s.' % (target, owners[target]))
            if self.dry_run:
                print('Would bind %s %s to %s/%s; local files are unverified.' %
                      (name, version, alias, package_id))
                return
            repository.confirm('Bind %s %s to %s/%s without verifying local files?' %
                                       (name, version, alias, package_id), yes)
            path = self.store.state_path(name)
            with extension_state.Transaction(self.store, name, 'adopt', [path]) as transaction:
                if not state:
                    state = {'schema_version': 1, 'config_path': self.config_path,
                             'roots': self.store.roots, 'installer_name': name, 'version': version,
                             'archive_sha256': None, 'installed_at': repository.utc_now(),
                             'files': files, 'config_entries': [], 'service_entries': []}
                state.update(transaction_id=transaction.journal['id'],
                             source_id=source['source_id'], package_id=package_id)
                self.store.validate(state, name)
                transaction.write_json(path, state)
                transaction.commit()
            self.store.prune()

    def detach(self, name, yes=False):
        """Clear a source binding while retaining the local installation inventory.

        Args:
            name (str): Tracked installed extension name.
            yes (bool): Confirm detachment without input.
        """
        with self.operation():
            state = self.store.read(name)
            if state is None:
                raise repository.RepositoryError('No tracked extension: ' + name, 3)
            if self.dry_run:
                print('Would detach ' + name)
                return
            repository.confirm('Detach %s from repository updates?' % name, yes)
            path = self.store.state_path(name)
            with extension_state.Transaction(self.store, name, 'detach', [path]) as transaction:
                state.update(source_id=None, package_id=None, transaction_id=transaction.journal['id'])
                transaction.write_json(path, state)
                transaction.commit()
            self.store.prune()

    def switch_source(self, name, alias, package_id, version=None, pre=False, yes=False,
                      extra_args=None):
        """Explicitly reinstall an extension from another source or package binding.

        Args:
            name (str): Tracked installed name, which the new package must preserve.
            alias (str): New repository alias.
            package_id (str): Exact package ID in that source.
            version (str|None): Exact version; required to allow a downgrade.
            pre (bool): True to include pre-release versions in selection.
            yes (bool): Confirm the specified switch without input.
            extra_args (list[str]|None): Arguments for the new installer.
        """
        state = self.store.read(name)
        if state is None:
            raise repository.RepositoryError('Adopt the legacy extension before switching.', 3)
        package = self._package(alias, package_id)
        if package['installer_name'] != name:
            raise repository.RepositoryError('Source switch must preserve installer name.')
        release = repository.select_release(package, version, pre)
        if release is None:
            raise repository.RepositoryError('No compatible release.')
        if (packaging.version.Version(release['version']) < packaging.version.Version(state['version'])
                and version is None):
            raise repository.RepositoryError('A source-switch downgrade requires --version.')
        print('Switch %s: %s -> %s/%s' % (name, state['source_id'], alias, package_id))
        self.install_package(alias, package, release, yes, extra_args, version is not None, True)

    def uninstall(self, name, yes=False):
        """Remove an installed extension, retaining local changes when requested.

        Args:
            name (str): Installed extension name.
            yes (bool): Confirm removal without input; file and setting conflicts
                still require explicit decisions.

        Tracked removals use ownership and transaction recovery. Legacy removals
        keep the existing engine behavior after checking for other file owners.
        """
        with self.operation():
            state = self.store.read(name)
            if state is None:
                _, installer = weecfg.get_extension_installer(os.path.dirname(self.store.state_path(name)))
                owners = self._other_owners(name)
                for _, target in self._gen_file_paths(installer.get('files', [])):
                    if target in owners:
                        raise repository.RepositoryError('%s is owned by %s.' % (target, owners[target]))
                engine = InstallerEngine(self.config_path, self.config,
                                                          self.dry_run, self.printer)
                engine.uninstall_extension(name, no_confirm=yes)
                return
            plan = self._file_plan({}, state, name)
            configuration, services = self._obsolete_settings(state, {}, set())
            if self.dry_run:
                print('Would uninstall ' + name)
                return
            repository.confirm('Uninstall %s?' % name, yes)
            state_path = self.store.state_path(name)
            installer_path = os.path.join(os.path.dirname(state_path), 'install.py')
            targets = [item['target'] for item in plan if not item['keep']]
            targets += [self.config_path, state_path, installer_path]
            with extension_state.Transaction(self.store, name, 'uninstall', targets) as transaction:
                transaction.check_observations(self._observations(plan))
                for item in plan:
                    if not item['keep']:
                        transaction.remove(item['target'])
                    else:
                        print('Retained local file: ' + item['target'])
                self._remove_settings(configuration, services)
                transaction.write_configuration(self.config)
                transaction.remove(installer_path)
                transaction.remove(state_path)
                transaction.commit()
            try:
                os.rmdir(os.path.dirname(state_path))
            except OSError:
                pass
            self.store.prune()
            print('Uninstalled ' + name)

    def recover(self, yes=False):
        """Restore unfinished managed operations and report any external review needed.

        Args:
            yes (bool): Confirm restoration of managed files without input. This
                does not acknowledge custom hook effects, which require a
                separate terminal confirmation after the user has inspected them.
        """
        with self.operation(recovery=True):
            pending = self.store.pending()
            for journal in pending:
                print('%s: %s %s [%s]' % (journal['id'], journal['operation'],
                                          journal['name'], journal['status']))
                if self.dry_run:
                    continue
                repository.confirm('Restore engine-managed files?', yes)
                transaction = extension_state.Transaction.load(self.store, journal)
                restored = transaction.restore()
                if not restored:
                    repository.confirm('Custom hooks ran. Have you inspected their external effects?')
                    transaction.journal['external_reviewed'] = True
                    transaction.restore()
                print('Recovered transaction ' + journal['id'])
            if not pending:
                print('No unfinished extension operations.')
