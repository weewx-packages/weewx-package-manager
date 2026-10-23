#
#    See the file LICENSE.txt for your rights.
#    Modified 2026-10-23 by weewx-packages: standalone extension namespace.
#
"""Local extension ownership and recovery of engine-managed files.

StateStore reads the inventory beside each cached installer and coordinates writers
for one station. Transaction saves file backups and a journal under manager_root.
Callers hold StateStore.locked() while creating, applying, or restoring transactions.
Custom installer hooks can have external effects that these file backups cannot undo.
"""

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid

import weecfg
from user.packages import repository
import weeutil.startup


def fingerprint(path):
    """Describe a file's contents or a symbolic link without following the link.

    Args:
        path (str): File to inspect. The file need not exist.

    Returns:
        dict: A kind of 'absent', 'file', or 'symlink', with a SHA-256 digest for
            files or the link text for symbolic links.
    """
    if os.path.islink(path):
        return {'kind': 'symlink', 'link': os.readlink(path)}
    if not os.path.lexists(path):
        return {'kind': 'absent'}
    if not os.path.isfile(path):
        raise repository.RepositoryError('Expected a regular file: ' + path, 3)
    return {'kind': 'file', 'sha256': repository.digest(path)}


def _relative(path):
    """Return whether path is a canonical relative name suitable for a file record."""
    return (isinstance(path, str) and bool(path) and not path.startswith('/') and
            '\\' not in path and ':' not in path and
            not any(ord(character) < 32 or ord(character) == 127 for character in path) and
            all(part not in ('', '.', '..') for part in path.split('/')))


class StateStore:
    """Read installation records and journals belonging to one station.

    Records contain their original configuration path and installation roots. A
    mismatch is an error because the recorded files may belong to another station.
    """

    def __init__(self, config):
        """Resolve the station's physical roots from its WeeWX configuration.

        Args:
            config (dict): Configuration including config_path and installation roots.
        """
        self.config = config
        self.options = repository.settings(config)
        self.root_dict = weeutil.startup.extract_roots(config)
        self.config_path = os.path.realpath(config['config_path'])
        self.extension_root = os.path.realpath(self.root_dict['EXT_DIR'])
        self.roots = {'user': os.path.realpath(self.root_dict['USER_DIR']),
                      'skins': os.path.realpath(self.root_dict['SKIN_DIR'])
                      if 'SKIN_DIR' in self.root_dict else None}

    def state_path(self, name):
        """Return the state.json path for a validated installed extension name."""
        if not isinstance(name, str) or not repository.INSTALLER_NAME.fullmatch(name):
            raise repository.RepositoryError('Invalid installed extension name.', 3)
        return os.path.join(self.extension_root, name, 'state.json')

    def validate(self, state, name):
        """Check an inventory's structure, identity, and file boundaries.

        Args:
            state (dict): Decoded installation record to check.
            name (str): Installed name of the extension that owns the record.

        Returns:
            dict: The supplied state, unchanged.

        Raises:
            repository.RepositoryError: The record is invalid or belongs
                to a different configuration or set of roots.
        """
        try:
            repository._object(state, (
                'schema_version', 'transaction_id', 'config_path', 'roots', 'source_id',
                'package_id', 'installer_name', 'version', 'archive_sha256', 'installed_at',
                'files', 'config_entries', 'service_entries'), (), 'installation state')
            if type(state['schema_version']) is not int or state['schema_version'] != 1:
                raise ValueError('Unsupported state schema_version.')
            for field in ('transaction_id', 'source_id'):
                value = state[field]
                if field == 'source_id' and value is None:
                    continue
                repository._text(value, field, 36, repository.UUID)
            if (state['source_id'] is None) != (state['package_id'] is None):
                raise ValueError('Incomplete source binding.')
            if state['package_id'] is not None:
                repository._text(state['package_id'], 'package ID', 64,
                                         repository.SLUG)
            if (state['installer_name'] != name or state['config_path'] != self.config_path or
                    state['roots'] != self.roots):
                raise ValueError('Configuration or installation roots changed.')
            repository._text(state['version'], 'installed version', 128)
            if state['archive_sha256'] is not None:
                repository._text(state['archive_sha256'], 'archive digest', 64,
                                         repository.DIGEST)
            if not isinstance(state['installed_at'], str):
                raise ValueError('Invalid installation timestamp.')
            seen = set()
            for item in state['files']:
                repository._object(item, ('root', 'path', 'sha256', 'preserved'),
                                           ('replacement',), 'file record')
                target = self.target(item)
                if target in seen or type(item['preserved']) is not bool:
                    raise ValueError('Duplicate file target or invalid preservation flag.')
                seen.add(target)
                if item['sha256'] is not None:
                    repository._text(item['sha256'], 'file digest', 64,
                                             repository.DIGEST)
                replacement = item.get('replacement')
                if replacement is not None:
                    if not item['preserved']:
                        raise ValueError('Replacement belongs to an unpreserved file.')
                    repository._object(replacement, ('transaction_id', 'path', 'sha256'),
                                               (), 'replacement')
                    repository._text(replacement['transaction_id'], 'transaction UUID',
                                             36, repository.UUID)
                    repository._text(replacement['sha256'], 'replacement digest', 64,
                                             repository.DIGEST)
                    if not _relative(replacement['path']):
                        raise ValueError('Invalid replacement backup path.')
            seen_config = set()
            # Compare settings in the form written by ConfigObj. Installer defaults
            # may use booleans or numbers, which would not equal their saved strings.
            for item in state['config_entries']:
                repository._object(item, ('section', 'key', 'value'), (), 'config record')
                if (not isinstance(item['section'], list) or
                        any(not isinstance(part, str) or not part for part in item['section']) or
                        not isinstance(item['key'], str) or not item['key']):
                    raise ValueError('Invalid configuration path.')
                value = item['value']
                if not isinstance(value, str) and not (
                        isinstance(value, list) and all(isinstance(part, str) for part in value)):
                    raise ValueError('Configuration values must use their saved string form.')
                key = tuple(item['section']), item['key']
                if key in seen_config:
                    raise ValueError('Duplicate configuration record.')
                seen_config.add(key)
            seen_services = set()
            for item in state['service_entries']:
                repository._object(item, ('group', 'service', 'introduced'), (),
                                           'service record')
                if (not isinstance(item['group'], str) or not isinstance(item['service'], str)
                        or type(item['introduced']) is not bool):
                    raise ValueError('Invalid service record.')
                service_key = item['group'], item['service']
                if service_key in seen_services:
                    raise ValueError('Duplicate service record.')
                seen_services.add(service_key)
        except (KeyError, TypeError, ValueError, repository.RepositoryError) as error:
            raise repository.RepositoryError('Invalid state for %s: %s' % (name, error), 3)
        return state

    def read(self, name):
        """Read and validate one extension's state.

        Args:
            name (str): Installed extension name.

        Returns:
            dict|None: Its validated inventory, or None when no record exists.
                A damaged record raises an error; it is not a legacy installation.
        """
        path = self.state_path(name)
        if not os.path.lexists(path):
            return None
        if os.path.islink(path):
            raise repository.RepositoryError('State must not be a symbolic link.', 3)
        try:
            return self.validate(repository.read_json(path), name)
        except repository.RepositoryError as error:
            raise repository.RepositoryError(str(error), 3)

    def all(self):
        """Return inventories by installed name, rejecting duplicate package bindings.

        Legacy installer directories without state.json are omitted. Invalid state
        stops the operation so its files cannot be mistaken for unowned files.
        """
        states = {}
        if os.path.isdir(self.extension_root):
            for name in sorted(os.listdir(self.extension_root)):
                if not os.path.isdir(os.path.join(self.extension_root, name)):
                    continue
                if not os.path.lexists(os.path.join(self.extension_root, name, 'state.json')):
                    continue
                state = self.read(name)
                if state is not None:
                    states[name] = state
        package_ids = [state['package_id'] for state in states.values()
                       if state['package_id'] is not None]
        if len(package_ids) != len(set(package_ids)):
            raise repository.RepositoryError('Duplicate installed package bindings.', 3)
        return states

    def target(self, record):
        """Resolve a file record inside its configured root, excluding manager data.

        Args:
            record (dict): File record containing root and relative path.

        Returns:
            str: Absolute target path with its parent checked for symlink escapes.
        """
        root = self.roots.get(record['root'])
        if not root or not _relative(record['path']):
            raise repository.RepositoryError('Invalid file record or missing target root.', 3)
        target = os.path.abspath(os.path.join(root, record['path']))
        # A target symlink is itself a replaceable object. Its parent directories
        # must stay inside the root so replacement cannot write into another tree.
        if not repository.within(os.path.dirname(target), root):
            raise repository.RepositoryError('Installation path escapes its root.', 3)
        for reserved in (self.extension_root, self.options['manager_root'],
                         self.options['cache_root']):
            if repository.within(target, reserved):
                raise repository.RepositoryError('Package targets reserved manager data.', 3)
        return target

    def locate(self, path):
        """Express a target path as a user/skin root and a relative path.

        Args:
            path (str): Absolute destination of an engine-managed file.

        Returns:
            dict: A root/path record, without digests or ownership decisions.
        """
        # User and skin roots can be nested. Use the more specific root so each
        # physical target has a consistent representation in the inventory.
        candidates = sorted(((name, root) for name, root in self.roots.items() if root),
                            key=lambda item: len(item[1]), reverse=True)
        for name, root in candidates:
            if repository.within(os.path.dirname(path), root):
                record = {'root': name, 'path': os.path.relpath(path, root)}
                self.target(record)
                return record
        raise repository.RepositoryError('Target lies outside configured user/skin roots: '
                                               + path)

    @contextlib.contextmanager
    def locked(self, recovery=False):
        """Exclude other writers and normally refuse unfinished transactions.

        Args:
            recovery (bool): True to allow access to unfinished transactions.

        Yields:
            None: Control while both physical writer locks are held.
        """
        paths = []
        for location in (self.config_path, self.extension_root):
            paths.append(os.path.join(os.path.dirname(location),
                                      '.' + os.path.basename(location) + '.extensions.lock'))
        with contextlib.ExitStack() as stack:
            # Lock files live beside the paths being protected. Atomic replacement
            # of weewx.conf must not replace the inode on which a writer is locking.
            # A fixed order also lets configurations sharing EXT_DIR wait safely.
            for path in sorted(set(paths)):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                stream = stack.enter_context(os.fdopen(descriptor, 'a+b'))
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            if not recovery and self.pending():
                raise repository.RepositoryError(
                    'An unfinished extension operation requires python -m user.packages recover.', 3)
            yield

    def journals(self):
        """Return journals after checking their station identity and transaction status."""
        root = self.options['transaction_root']
        if not os.path.isdir(root):
            return []
        result = []
        for name in sorted(os.listdir(root)):
            if not repository.UUID.fullmatch(name):
                continue
            path = os.path.join(root, name, 'journal.json')
            if not os.path.isfile(path):
                continue
            try:
                journal = repository.read_json(path)
                if (journal['id'] != name or journal['config_path'] != self.config_path or
                        journal['roots'] != self.roots or journal['status'] not in (
                            'prepared', 'applying', 'committed', 'rolled_back', 'needs_attention')):
                    raise ValueError('Journal identity or status mismatch.')
                result.append(journal)
            except (KeyError, ValueError, TypeError, repository.RepositoryError) as error:
                raise repository.RepositoryError('Invalid transaction journal: %s' % error, 3)
        return result

    def pending(self):
        """Return transactions that block further changes until recovery completes."""
        return [item for item in self.journals()
                if item['status'] in ('prepared', 'applying', 'needs_attention')]

    def prune(self):
        """Remove completed transactions no longer needed by state or retained files."""
        states = self.all()
        keep = {state['transaction_id'] for state in states.values()}
        for state in states.values():
            for item in state['files']:
                if item.get('replacement'):
                    keep.add(item['replacement']['transaction_id'])
        journals = self.journals()
        newest = {}
        for item in journals:
            if item['status'] == 'committed':
                prior = newest.get(item['name'])
                if not prior or (item['created_at'], item['id']) > (prior['created_at'], prior['id']):
                    newest[item['name']] = item
        keep.update(item['id'] for item in newest.values())
        # An uninstall removes state.json, but its last committed backup must still
        # be available. Retained replacement files may also refer to older journals.
        for item in journals:
            if item['status'] in ('committed', 'rolled_back') and item['id'] not in keep:
                shutil.rmtree(os.path.join(self.options['transaction_root'], item['id']))


class Transaction:
    """Back up a known set of paths, then journal each replacement or deletion.

    Normal context exit requires an explicit commit(). Otherwise, managed changes
    are restored. Restore compares before/after fingerprints and refuses to destroy
    a subsequent edit. Callers must hold the store's writer locks throughout.
    """

    def __init__(self, store, name, operation, paths):
        """Create a journal and back up all destinations before the first write.

        Args:
            store (StateStore): Locked station state store.
            name (str): Installed extension name.
            operation (str): Operation label recorded for recovery, e.g., 'install'.
            paths (Iterable[str]): Absolute paths this transaction is allowed to change.
        """
        self.store = store
        self.directory = os.path.join(store.options['transaction_root'], str(uuid.uuid4()))
        os.makedirs(self.directory, mode=0o700)
        self.journal = {'id': os.path.basename(self.directory), 'name': name,
                        'operation': operation, 'config_path': store.config_path,
                        'roots': store.roots, 'created_at': repository.utc_now(),
                        'status': 'prepared', 'targets': [], 'directories': [],
                        'hook_started': False, 'external_reviewed': False}
        self.save()
        for path in sorted(set(paths)):
            self.snapshot(path)

    @classmethod
    def load(cls, store, journal):
        """Open an existing journal for recovery without taking new backups.

        Args:
            store (StateStore): Locked station state store.
            journal (dict): Existing journal returned by StateStore.journals().

        Returns:
            Transaction: Transaction using the original backups and fingerprints.
        """
        transaction = cls.__new__(cls)
        transaction.store = store
        transaction.journal = journal
        transaction.directory = os.path.join(store.options['transaction_root'], journal['id'])
        return transaction

    def save(self):
        """Persist the current journal atomically before the next filesystem change."""
        repository.atomic_json(os.path.join(self.directory, 'journal.json'), self.journal)

    def _check_target(self, path):
        """Reject journal paths outside the configuration, installer, and payload roots."""
        if not os.path.isabs(path) or os.path.normpath(path) != path:
            raise repository.RepositoryError('Invalid journal target.', 3)
        if path == self.store.config_path:
            return
        cache = os.path.join(self.store.extension_root, self.journal['name'])
        if (path in (os.path.join(cache, 'install.py'), os.path.join(cache, 'state.json')) and
                repository.within(os.path.dirname(path), self.store.extension_root)):
            return
        self.store.locate(path)

    def snapshot(self, path):
        """Record a destination's original contents, metadata, and missing parents.

        Args:
            path (str): Destination to back up before it can be changed.
        """
        path = os.path.abspath(path)
        self._check_target(path)
        before = fingerprint(path)
        entry = {'path': path, 'before': before, 'after': None, 'backup': None}
        if before['kind'] != 'absent':
            information = os.lstat(path)
            entry['metadata'] = {'mode': stat.S_IMODE(information.st_mode),
                                 'uid': information.st_uid, 'gid': information.st_gid,
                                 'atime_ns': information.st_atime_ns,
                                 'mtime_ns': information.st_mtime_ns}
            if before['kind'] == 'file':
                entry['backup'] = 'backup-%d' % len(self.journal['targets'])
                backup = os.path.join(self.directory, entry['backup'])
                repository.copy_file(path, backup)
                os.chmod(backup, 0o600)
                # An editor does not take our process locks. Both copies must still
                # match the fingerprint, or recovery would have a false baseline.
                if fingerprint(backup) != before or fingerprint(path) != before:
                    raise repository.RepositoryError('File changed during backup: ' + path, 4)
        parent = os.path.dirname(path)
        while not os.path.exists(parent):
            if parent not in self.journal['directories']:
                self.journal['directories'].append(parent)
            parent = os.path.dirname(parent)
        self.journal['targets'].append(entry)
        self.save()

    def _entry(self, path):
        """Find a planned target and refuse edits since its last recorded contents."""
        self._check_target(path)
        for item in self.journal['targets']:
            if item['path'] == path:
                expected = item['after'] or item['before']
                if fingerprint(path) != expected:
                    raise repository.RepositoryError('File changed during operation: ' + path, 4)
                return item
        raise repository.RepositoryError('Installer attempted an unplanned write: ' + path, 4)

    def check_observations(self, observations):
        """Reject edits made between planning and taking the transaction's backups.

        Args:
            observations (dict): Absolute paths mapped to fingerprints seen while
                planning. Only paths included in this transaction are checked.
        """
        for item in self.journal['targets']:
            if item['path'] in observations and item['before'] != observations[item['path']]:
                raise repository.RepositoryError('File changed after planning: ' + item['path'], 4)

    def write(self, path, source=None, data=None, mode=0o644):
        """Replace a planned target with a source file or byte string.

        Supply source or data. When source is None, data must contain bytes.

        Args:
            path (str): Destination already included in the transaction.
            source (str|None): Source file to copy, or None to use data instead.
            data (bytes|None): Bytes to write when source is None.
            mode (int): Permissions for a new target written from data. Source
                copies use source permissions; existing targets keep theirs.
        """
        entry = self._entry(path)
        if source is not None:
            mode = stat.S_IMODE(os.stat(source).st_mode) & 0o777
            checksum = repository.digest(source)
        else:
            checksum = hashlib.sha256(data).hexdigest()
        entry['after'] = {'kind': 'file', 'sha256': checksum}
        self.journal['status'] = 'applying'
        # Recovery must know both possible contents before the destination changes.
        self.save()
        if source is not None:
            repository.copy_file(source, path)
        else:
            repository.atomic_write(path, data, mode)

    def write_json(self, path, value):
        """Write a JSON record through the transaction, using private file permissions."""
        self.write(path, data=(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
                               + '\n').encode('utf-8'), mode=0o600)

    def write_configuration(self, config):
        """Save the station configuration using WeeWX's existing serialization rules.

        Args:
            config (dict): Updated WeeWX configuration, including its internal keys.
        """
        descriptor, temporary = tempfile.mkstemp()
        os.close(descriptor)
        try:
            weecfg.save(config, temporary)
            self.write(self.store.config_path, source=temporary)
        finally:
            os.unlink(temporary)

    def remove(self, path):
        """Journal and unlink a planned target, retaining its original backup."""
        entry = self._entry(path)
        entry['after'] = {'kind': 'absent'}
        self.journal['status'] = 'applying'
        self.save()
        if os.path.lexists(path):
            os.unlink(path)
            repository.sync_directory(os.path.dirname(path))

    def candidate(self, source, record):
        """Keep a publisher's replacement when the user preserves a local file.

        Args:
            source (str): Proposed package file to retain under this transaction.
            record (dict): Target inventory entry containing root and path.

        Returns:
            dict: Transaction UUID, relative backup path, and replacement digest
                to save in the preserved file's inventory record.
        """
        relative = os.path.join('candidates', record['root'], record['path'])
        destination = os.path.join(self.directory, relative)
        repository.copy_file(source, destination)
        return {'transaction_id': self.journal['id'], 'path': relative,
                'sha256': repository.digest(destination)}

    def hook_started(self):
        """Record that recovery may need a user's review of external hook effects."""
        self.journal['hook_started'] = True
        self.save()

    def commit(self):
        """Accept all managed changes; call only after files and state have been saved."""
        self.journal['status'] = 'committed'
        self.save()

    def restore(self):
        """Restore original managed files without overwriting subsequent edits.

        Restoration can be repeated after an interruption. Custom hook effects
        remain the caller's responsibility even when all files were restored.

        Returns:
            bool: True if rollback is complete. False if custom hook effects still
                require acknowledgement before the station can accept more changes.
        """
        for entry in reversed(self.journal['targets']):
            path = entry['path']
            self._check_target(path)
            current = fingerprint(path)
            # A previous recovery may already have restored this target. Otherwise,
            # only the recorded result of our own write is safe to undo.
            if current == entry['before']:
                continue
            if not entry['after'] or current != entry['after']:
                raise repository.RepositoryError('Recovery found a subsequent edit: ' + path, 3)
            before = entry['before']
            if before['kind'] == 'absent':
                if os.path.lexists(path):
                    os.unlink(path)
            elif before['kind'] == 'symlink':
                temporary = path + '.restore-' + self.journal['id']
                os.symlink(before['link'], temporary)
                os.replace(temporary, path)
            else:
                backup = entry['backup']
                if backup != 'backup-%d' % self.journal['targets'].index(entry):
                    raise repository.RepositoryError('Invalid backup reference.', 3)
                saved = os.path.join(self.directory, backup)
                if fingerprint(saved) != before:
                    raise repository.RepositoryError('Recovery backup is damaged: ' + saved, 3)
                repository.copy_file(saved, path)
                information = entry['metadata']
                os.chmod(path, information['mode'])
                try:
                    os.chown(path, information['uid'], information['gid'])
                except PermissionError:
                    pass
                os.utime(path, ns=(information['atime_ns'], information['mtime_ns']))
            repository.sync_directory(os.path.dirname(path))
        for directory in sorted(self.journal['directories'], key=len, reverse=True):
            if not any(repository.within(directory, root)
                       for root in (self.store.extension_root,) + tuple(
                           root for root in self.store.roots.values() if root)):
                raise repository.RepositoryError('Invalid recovery directory.', 3)
            try:
                os.rmdir(directory)
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                    raise
        attention = self.journal['hook_started'] and not self.journal['external_reviewed']
        self.journal['status'] = 'needs_attention' if attention else 'rolled_back'
        self.save()
        return not attention

    def __enter__(self):
        """Return this transaction; its destinations were backed up at construction."""
        return self

    def __exit__(self, error_type, error, traceback):
        """Roll back an uncommitted operation and leave uncertain recovery visible."""
        if self.journal['status'] == 'committed':
            return False
        try:
            self.restore()
        except (OSError, repository.RepositoryError) as recovery_error:
            self.journal['status'] = 'needs_attention'
            self.save()
            raise repository.RepositoryError(
                'Operation failed; run python -m user.packages recover: %s' % recovery_error, 3)
        if self.journal['status'] == 'needs_attention':
            raise repository.RepositoryError(
                'Installer failed (%s); managed files restored. External effects require '
                'python -m user.packages recover.' % error, 3)
        return False
