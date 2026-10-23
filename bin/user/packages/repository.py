#
#    See the file LICENSE.txt for your rights.
#    Modified 2026-10-23 by weewx-packages: standalone extension namespace.
#
"""Read extension catalogs and select releases without importing package installers.

A source is a locally registered index URL with a stable UUID. The publisher's
repository ID checks that a URL still serves the expected catalog; it is not an
authentication credential. Catalogs and HTTP validators are disposable cache data.
Installed bindings and verified digests belong to extension_state instead.
"""

import contextlib
import copy
import datetime
import errno
import gzip
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

import packaging.specifiers
import packaging.version

import weecfg
import weeutil.startup
import weewx

SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
INSTALLER_NAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z')
REPOSITORY_ID = re.compile(r'[A-Za-z0-9_.-]+\Z')
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
DIGEST = re.compile(r'[0-9a-fA-F]{64}\Z')
TYPES = ('skin', 'driver', 'service', 'search-list', 'generator', 'other')
FORMATS = ('zip', 'tar', 'tar.gz', 'tar.bz2', 'tar.xz')
OPTIONS = {'manager_root', 'cache_root', 'timeout', 'redirect_limit', 'index_max_bytes',
           'archive_max_bytes', 'unpacked_max_bytes'}
DEFAULTS = {'timeout': 30, 'redirect_limit': 5, 'index_max_bytes': 4194304,
            'archive_max_bytes': 268435456, 'unpacked_max_bytes': 1073741824}


class RepositoryError(Exception):
    """An extension operation failed with a command, configuration, or I/O exit code."""

    def __init__(self, message, code=2):
        """Attach the command's exit status to its user-facing error message.

        Args:
            message (str): Explanation suitable for console output.
            code (int): Exit status: 1 for cancellation, 2 for command/catalog
                errors, 3 for local state errors, or 4 for operational failures.
        """
        super().__init__(message)
        self.code = code


def utc_now():
    """Return the current UTC time in the timestamp format used by local state."""
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def display(value):
    """Quote terminal control characters in publisher-controlled text."""
    return ''.join(character if character.isprintable() else repr(character)[1:-1]
                   for character in str(value))


def confirm(message, yes=False):
    """Require confirmation of a specified operation, or raise on cancellation.

    Args:
        message (str): Prompt describing the operation.
        yes (bool): True to acknowledge it without input. This does not resolve
            source choices or file conflicts, which must use choose().
    """
    if yes:
        return
    if not sys.stdin.isatty():
        raise RepositoryError(message + ' Confirmation requires a terminal or --yes.')
    if input(message + ' [y/N] ').strip().lower() not in ('y', 'yes'):
        raise RepositoryError('Cancelled.', 1)


def choose(message, options):
    """Ask for an explicit terminal choice, with cancellation as the default.

    Args:
        message (str): Selection prompt.
        options (list[str]): Labels displayed in numbered order.

    Returns:
        int: Zero-based index of the selected option. Cancellation or absence
            of a terminal raises RepositoryError instead of selecting a default.
    """
    for number, label in enumerate(options, 1):
        print('%d) %s' % (number, display(label)))
    print('0) Cancel')
    if not sys.stdin.isatty():
        raise RepositoryError(message + ' An explicit choice requires a terminal.')
    while True:
        answer = input(message + ' [0]: ').strip()
        if not answer or answer == '0':
            raise RepositoryError('Cancelled.', 1)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1


def _unique_pairs(pairs):
    """Build a decoded JSON object while rejecting duplicate keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise RepositoryError("Duplicate JSON key '%s'." % display(key))
        result[key] = value
    return result


def _invalid_constant(value):
    """Reject the NaN and infinity literals accepted by Python's JSON decoder."""
    raise RepositoryError('Non-finite JSON number: ' + value)


def _finite_float(value):
    """Decode a JSON float, rejecting finite-looking literals that overflow."""
    number = float(value)
    if not math.isfinite(number):
        raise RepositoryError('Non-finite JSON number: ' + value)
    return number


def decode_json(data):
    """Decode UTF-8 JSON, rejecting duplicate keys and non-finite numbers.

    Args:
        data (str|bytes): JSON text, or its UTF-8 encoding.

    Returns:
        Any: Decoded JSON value. Catalog and state validation check its structure.
    """
    try:
        return json.loads(data.decode('utf-8') if isinstance(data, bytes) else data,
                          object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant,
                          parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise RepositoryError('Invalid JSON: %s' % error)


def read_json(path):
    """Read a file using the same strict JSON rules as downloaded catalogs."""
    try:
        with open(path, 'rb') as source:
            return decode_json(source.read())
    except OSError as error:
        raise RepositoryError('Cannot read %s: %s' % (path, error), 4)


def digest(path):
    """Return a regular file's SHA-256 digest as a hexadecimal string."""
    checksum = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def sync_directory(path):
    """Flush directory changes where the filesystem supports directory fsync."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
                raise
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _atomic_target(path, mode):
    """Provide a temporary output file, replacing path only on normal exit.

    Args:
        path (str): Destination whose existing permissions and owner are retained.
        mode (int): Permissions to use if the destination does not exist.

    Yields:
        typing.BinaryIO: Temporary stream to fill before the atomic replacement.
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    old_stat = os.stat(path) if os.path.exists(path) else None
    descriptor, temporary = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', dir=parent)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            yield target
            os.fchmod(target.fileno(), stat.S_IMODE(old_stat.st_mode) if old_stat else mode)
            if old_stat:
                try:
                    os.fchown(target.fileno(), old_stat.st_uid, old_stat.st_gid)
                except PermissionError:
                    pass
            target.flush()
            os.fsync(target.fileno())
        # The temporary file must be on the same filesystem for atomic replacement.
        # Flush the parent as well so recovery can rely on the new directory entry.
        os.replace(temporary, path)
        sync_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write(path, data, mode=0o600):
    """Replace a file durably, keeping its existing mode and ownership.

    Args:
        path (str): Destination, with parent directories created as necessary.
        data (bytes): Complete replacement contents.
        mode (int): Permissions for a new file. Default is private to its owner.
    """
    with _atomic_target(path, mode) as target:
        target.write(data)


def atomic_json(path, value):
    """Save a UTF-8 JSON record atomically, refusing non-finite values."""
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
                        + '\n').encode('utf-8'))


def copy_file(source, target):
    """Copy a file atomically, retaining target metadata or using source permissions."""
    with open(source, 'rb') as stream, _atomic_target(
            target, stat.S_IMODE(os.stat(source).st_mode) & 0o777) as destination:
        shutil.copyfileobj(stream, destination, 65536)


def save_configuration(config, path, expected=None):
    """Serialize and atomically save repository changes to the configuration.

    Args:
        config (dict): WeeWX configuration to serialize using weecfg.save().
        path (str): Actual configuration file to replace.
        expected (str|None): Previous SHA-256 digest to check before replacing
            the file, or None to omit that check. The caller holds writer locks.
    """
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    os.close(descriptor)
    try:
        weecfg.save(config, temporary)
        if expected is not None and digest(path) != expected:
            raise RepositoryError('Configuration changed during the operation.', 4)
        copy_file(temporary, path)
    finally:
        os.unlink(temporary)


def settings(config):
    """Resolve manager directories and validate configured acquisition limits.

    Args:
        config (dict): WeeWX configuration, optionally with ExtensionRepositories.

    Returns:
        dict: Absolute manager/cache/transaction roots and integer download limits.
    """
    roots = weeutil.startup.extract_roots(config)
    section = config.get('ExtensionRepositories', {})
    if not isinstance(section, dict):
        raise RepositoryError('ExtensionRepositories must be a section.', 3)
    result = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        try:
            value = int(section.get(key, default))
        except (TypeError, ValueError):
            raise RepositoryError('Invalid repository option %s.' % key, 3)
        if value < (0 if key == 'redirect_limit' else 1):
            raise RepositoryError('Invalid repository option %s.' % key, 3)
        result[key] = value
    manager = section.get('manager_root', os.path.join(roots['USER_DIR'], 'extension-manager'))
    if not isinstance(manager, str) or not manager.strip():
        raise RepositoryError('Invalid manager_root.', 3)
    manager_root = os.path.realpath(os.path.join(roots['WEEWX_ROOT'], manager))
    cache = section.get('cache_root', os.path.join(manager_root, 'cache'))
    if not isinstance(cache, str) or not cache.strip():
        raise RepositoryError('Invalid cache_root.', 3)
    cache_root = os.path.realpath(os.path.join(roots['WEEWX_ROOT'], cache))
    transaction_root = os.path.join(manager_root, 'transactions')
    # Cache can be discarded, while transactions contain the only recovery copies.
    # Neither may live in EXT_DIR, whose subdirectories enumerate extensions.
    for first, second in ((cache_root, transaction_root), (manager_root, roots['EXT_DIR']),
                          (cache_root, roots['EXT_DIR'])):
        if within(first, second) or within(second, first):
            raise RepositoryError('Repository cache, manager, and installer directories overlap.', 3)
    return dict(result, manager_root=manager_root, cache_root=cache_root,
                transaction_root=transaction_root)


def within(path, root):
    """Return whether path resolves inside root, accounting for symbolic links."""
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)


def url(value, base=None, information=False):
    """Resolve and normalize an allowed local file or HTTP(S) reference.

    Args:
        value (str): URL, or a local path when no base is supplied.
        base (str|None): Final index or previous redirect URL for relative references.
        information (bool): True to allow fragments in informational links.
            Retrieval URLs must have no fragment.

    Returns:
        str: Normalized URL without credentials. Remote references cannot resolve
            to local files or downgrade HTTPS to HTTP.
    """
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise RepositoryError('Invalid source URL.')
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127
           for character in value):
        raise RepositoryError('Whitespace or control character in URL.')
    raw = urllib.parse.urlsplit(value)
    if not raw.scheme and not base:
        return urllib.parse.urljoin('file:', urllib.request.pathname2url(os.path.realpath(
            os.path.expanduser(value))))
    if raw.scheme in ('http', 'https') and not raw.netloc:
        raise RepositoryError('HTTP URL requires a host.')
    resolved = urllib.parse.urlsplit(urllib.parse.urljoin(base, value) if base else value)
    if resolved.scheme not in ('http', 'https', 'file'):
        raise RepositoryError('Unsupported URL scheme: ' + display(resolved.scheme))
    if resolved.username is not None or resolved.password is not None:
        raise RepositoryError('Credentials must not be embedded in repository URLs.')
    if resolved.fragment and not information:
        raise RepositoryError('Retrieval URLs must not contain fragments.')
    if base:
        origin = urllib.parse.urlsplit(base)
        if origin.scheme != 'file' and resolved.scheme == 'file':
            raise RepositoryError('A remote repository cannot refer to local files.')
        if origin.scheme == 'https' and resolved.scheme == 'http':
            raise RepositoryError('HTTPS must not downgrade to HTTP.')
    if resolved.scheme == 'file':
        if resolved.netloc not in ('', 'localhost') or resolved.query:
            raise RepositoryError('Only local file URLs are supported.')
        path = os.path.realpath(urllib.request.url2pathname(resolved.path))
        return urllib.parse.urljoin('file:', urllib.request.pathname2url(path))
    if not resolved.hostname:
        raise RepositoryError('Source URL requires a host.')
    try:
        port = resolved.port
    except ValueError as error:
        raise RepositoryError(str(error))
    host = resolved.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    if port and port != (443 if resolved.scheme == 'https' else 80):
        host += ':' + str(port)
    return urllib.parse.urlunsplit((resolved.scheme.lower(), host, resolved.path or '/',
                                   resolved.query, resolved.fragment))


def _object(value, required, optional, label):
    """Require exactly the permitted fields, reporting errors with the object's label."""
    if not isinstance(value, dict) or set(value) - set(required) - set(optional):
        raise RepositoryError('Invalid or unknown fields in ' + label)
    if set(required) - set(value):
        raise RepositoryError('Missing fields in %s: %s' % (label, ', '.join(
            sorted(set(required) - set(value)))))


def _text(value, label, maximum, pattern=None):
    """Require nonblank bounded text, optionally matching a complete identifier pattern."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RepositoryError('Invalid ' + label)
    if pattern and not pattern.fullmatch(value):
        raise RepositoryError('Invalid ' + label)


def validate_catalog(document, base):
    """Validate the complete v1 contract and return a copy with defaults and resolved URLs.

    Args:
        document (dict): Decoded repository catalog.
        base (str): Final index URL used to resolve relative references.

    Returns:
        dict: Validated catalog. No extension code is imported.
    """
    catalog = copy.deepcopy(document)
    _object(catalog, ('schema_version', 'repository', 'packages'), ('extensions',), 'catalog')
    if type(catalog['schema_version']) is not int or catalog['schema_version'] != 1:
        raise RepositoryError('Unsupported repository schema_version.')
    if 'extensions' in catalog and not isinstance(catalog['extensions'], dict):
        raise RepositoryError('extensions must be an object.')
    repository = catalog['repository']
    _object(repository, ('id', 'name'), ('description',), 'repository')
    _text(repository['id'], 'repository ID', 128, REPOSITORY_ID)
    _text(repository['name'], 'repository name', 200)
    if 'description' in repository:
        _text(repository['description'], 'repository description', 4096)
    if not isinstance(catalog['packages'], list):
        raise RepositoryError('packages must be an array.')
    seen_packages = set()
    for package in catalog['packages']:
        _object(package, ('id', 'installer_name', 'name', 'description', 'types', 'releases'),
                ('keywords', 'homepage'), 'package')
        _text(package['id'], 'package ID', 64, SLUG)
        _text(package['installer_name'], 'installer name', 128, INSTALLER_NAME)
        _text(package['name'], 'package name', 200)
        _text(package['description'], 'package description', 4096)
        if package['id'] in seen_packages:
            raise RepositoryError('Duplicate package ID: ' + package['id'])
        seen_packages.add(package['id'])
        types = package['types']
        if (not isinstance(types, list) or not types or
                any(not isinstance(item, str) or item not in TYPES for item in types) or
                len(types) != len(set(types))):
            raise RepositoryError('Invalid package types.')
        keywords = package.setdefault('keywords', [])
        if not isinstance(keywords, list) or len(keywords) > 64:
            raise RepositoryError('Invalid package keywords.')
        for keyword in keywords:
            _text(keyword, 'keyword', 64)
            if keyword != keyword.strip():
                raise RepositoryError('Keywords must be trimmed.')
        if len({item.casefold() for item in keywords}) != len(keywords):
            raise RepositoryError('Duplicate keywords after case folding.')
        if 'homepage' in package:
            package['homepage'] = url(package['homepage'], base, information=True)
        if not isinstance(package['releases'], list) or not package['releases']:
            raise RepositoryError('A package requires at least one release.')
        seen_versions = set()
        for release in package['releases']:
            _object(release, ('version', 'requires_python', 'requires_weewx', 'archive'),
                    ('release_notes', 'restart_required', 'yanked'), 'release')
            _text(release['version'], 'release version', 128)
            try:
                version = packaging.version.Version(release['version'])
                release['version'] = str(version)
                for name in ('requires_python', 'requires_weewx'):
                    _text(release[name], name, 256)
                    release[name] = str(packaging.specifiers.SpecifierSet(release[name]))
            except (packaging.version.InvalidVersion, packaging.specifiers.InvalidSpecifier) as error:
                raise RepositoryError(str(error))
            if version.local is not None or version in seen_versions:
                raise RepositoryError('Duplicate equivalent version or local version label.')
            # Compare parsed versions so spellings such as 1.0 and 1.0.0 cannot
            # give the same release two different archive identities.
            seen_versions.add(version)
            archive = release['archive']
            _object(archive, ('url', 'format', 'sha256'), (), 'archive')
            if archive['format'] not in FORMATS:
                raise RepositoryError('Unsupported archive format.')
            _text(archive['sha256'], 'archive SHA-256', 64, DIGEST)
            archive['sha256'] = archive['sha256'].lower()
            archive['url'] = url(archive['url'], base)
            for name, default in (('restart_required', True), ('yanked', False)):
                if type(release.setdefault(name, default)) is not bool:
                    raise RepositoryError(name + ' must be a boolean.')
            if 'release_notes' in release:
                release['release_notes'] = url(release['release_notes'], base, information=True)
    return catalog


def incompatibility(release, python_version=None, weewx_version=None):
    """Describe unmet runtime requirements, or return an empty string if compatible.

    Args:
        release (dict): Validated release with Python and WeeWX specifiers.
        python_version (str|None): Python version to check. Default is this interpreter.
        weewx_version (str|None): WeeWX version to check. Default is the running version.

    Returns:
        str: Unmet requirements joined for display, or an empty string.
    """
    reasons = []
    for label, field, actual in (
            ('Python', 'requires_python', python_version or platform.python_version()),
            ('WeeWX', 'requires_weewx', weewx_version or weewx.__version__)):
        requirement = packaging.specifiers.SpecifierSet(release[field])
        if not requirement.contains(actual, prereleases=True):
            reasons.append('%s %s requires %s' % (label, actual, release[field]))
    return '; '.join(reasons)


def select_release(package, version=None, pre=False, python_version=None, weewx_version=None):
    """Select the highest eligible release of one package from one source.

    Args:
        package (dict): Validated package including its release list.
        version (str|None): Exact release to request instead of the highest version.
        pre (bool): True to include pre-release and development versions.
        python_version (str|None): Python version override for compatibility checks.
        weewx_version (str|None): WeeWX version override for compatibility checks.

    Returns:
        dict|None: Highest compatible, non-yanked candidate, or None if no release
            qualifies. An explicit version that cannot be selected raises an error.
    """
    requested = None
    if version is not None:
        try:
            requested = packaging.version.Version(version)
        except packaging.version.InvalidVersion as error:
            raise RepositoryError(str(error))
    candidates = []
    for release in package['releases']:
        parsed = packaging.version.Version(release['version'])
        if requested is not None and parsed != requested:
            continue
        if (release.get('yanked', False) or (parsed.is_prerelease and not pre) or
                incompatibility(release, python_version, weewx_version)):
            continue
        candidates.append(release)
    if not candidates:
        if requested is not None:
            raise RepositoryError('Requested version %s is unavailable, yanked, pre-release, '
                                  'or incompatible.' % version)
        return None
    return max(candidates, key=lambda item: packaging.version.Version(item['version']))


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Apply the source URL policy and redirect limit to every redirect hop."""

    def __init__(self, limit):
        """Start counting redirects for a single download."""
        self.limit = limit
        self.count = 0

    def redirect_request(self, request, response, code, message, headers, new_url):
        """Validate the next URL before urllib follows it."""
        self.count += 1
        if self.count > self.limit:
            raise RepositoryError('Too many download redirects.', 4)
        destination = url(new_url, request.full_url)
        return super().redirect_request(request, response, code, message, headers, destination)


class Downloader:
    """Bound downloads and extraction before any installer is imported."""

    def __init__(self, options=None):
        """Use default limits with any caller-supplied overrides.

        Args:
            options (dict|None): Acquisition options returned by settings(), or None.
        """
        self.options = dict(DEFAULTS, **(options or {}))

    def fetch(self, source, target, limit, validators=None):
        """Download a bounded response or report that its validated cache is current.

        Args:
            source (str): HTTP(S)/file URL, or a local file path.
            target (str): Temporary file to receive decoded response bytes.
            limit (int): Maximum size after HTTP content decoding, in bytes.
            validators (dict|None): Conditional request headers from a validated cache.

        Returns:
            dict: Final URL and optional HTTP validators. For HTTP 304, contains
                not_modified=True and leaves target untouched. The caller must
                check that the final URL still identifies its cached document.
        """
        source = url(source)
        headers = {'Accept-Encoding': 'identity', 'User-Agent': 'WeeWX-extension/1'}
        headers.update(validators or {})
        request = urllib.request.Request(source, headers=headers)
        opener = urllib.request.build_opener(_RedirectHandler(self.options['redirect_limit']))
        try:
            try:
                response = opener.open(request, timeout=self.options['timeout'])
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    final_url = url(error.geturl(), source)
                    error.close()
                    return {'not_modified': True, 'final_url': final_url}
                raise
            with response:
                final_url = url(response.geturl(), source)
                encoding = response.headers.get('Content-Encoding', 'identity').lower()
                if encoding not in ('identity', 'gzip'):
                    raise RepositoryError('Unsupported HTTP content encoding: ' + encoding, 4)
                stream = gzip.GzipFile(fileobj=response) if encoding == 'gzip' else response
                # Apply the limit to decoded bytes. Compressed HTTP content must
                # not let an oversized catalog bypass the index size limit.
                total = 0
                with open(target, 'wb') as output:
                    while True:
                        chunk = stream.read(min(65536, limit - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise RepositoryError('Download exceeds configured size limit.', 4)
                        output.write(chunk)
                return {'final_url': final_url, 'etag': response.headers.get('ETag'),
                        'last_modified': response.headers.get('Last-Modified')}
        except (OSError, EOFError, ValueError, urllib.error.URLError) as error:
            raise RepositoryError('Download failed for %s: %s' % (display(source), error), 4)

    def extract(self, archive, file_format, target):
        """Extract regular files into a temporary tree without importing install.py.

        Args:
            archive (str): Downloaded archive. Repository installs verify its digest first.
            file_format (str): 'zip' or a supported tar format from the catalog.
            target (str): Fresh temporary extraction directory.

        Returns:
            str: Single extension root containing install.py. Links, traversal,
                duplicate names, special files, and exceeded limits are refused.
        """
        try:
            with contextlib.ExitStack() as stack:
                if file_format == 'zip':
                    zip_archive = stack.enter_context(zipfile.ZipFile(archive))
                    members = [(item.filename, item.file_size, item.is_dir(),
                                stat.S_IFMT(item.external_attr >> 16), item.external_attr >> 16)
                               for item in zip_archive.infolist()]
                    open_member = lambda name: zip_archive.open(name)
                else:
                    tar_archive = stack.enter_context(tarfile.open(archive, 'r:*'))
                    members = []
                    for item in tar_archive:
                        if len(members) >= 100000:
                            raise RepositoryError('Too many archive members.', 4)
                        if not (item.isfile() or item.isdir()):
                            raise RepositoryError('Archive links and special files are forbidden.', 4)
                        members.append((item.name, item.size, item.isdir(), 0, item.mode))
                    def open_tar_member(name):
                        """Open an already checked regular tar member."""
                        stream = tar_archive.extractfile(name)
                        if stream is None:
                            raise RepositoryError('Archive member is not a regular file.', 4)
                        return stream

                    open_member = lambda name: open_tar_member(name)
                if len(members) > 100000:
                    raise RepositoryError('Too many archive members.', 4)
                names = set()
                roots = set()
                total = 0
                # Use the same path rules for both archive formats. No extracted
                # member may redirect a later write through a link or alias path.
                for name, size, is_directory, kind, mode in members:
                    clean = name.rstrip('/')
                    parts = clean.split('/')
                    if (not clean or any(part in ('', '.', '..') for part in parts) or
                            '\\' in name or ':' in name or any(ord(c) < 32 for c in name) or
                            kind not in (0, stat.S_IFREG, stat.S_IFDIR)):
                        raise RepositoryError('Unsafe archive member: ' + display(name), 4)
                    if clean in names:
                        raise RepositoryError('Duplicate archive member: ' + display(name), 4)
                    names.add(clean)
                    roots.add(parts[0])
                    total += size
                    if total > self.options['unpacked_max_bytes']:
                        raise RepositoryError('Archive exceeds extraction size limit.', 4)
                    destination = os.path.join(target, *parts)
                    if is_directory:
                        os.makedirs(destination, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(destination), exist_ok=True)
                        with open_member(name) as stream, open(destination, 'wb') as output:
                            copied = 0
                            while True:
                                chunk = stream.read(min(65536, size - copied + 1))
                                if not chunk:
                                    break
                                copied += len(chunk)
                                if copied > size:
                                    raise RepositoryError('Invalid archive member size.', 4)
                                output.write(chunk)
                            if copied != size:
                                raise RepositoryError('Truncated archive member.', 4)
                        os.chmod(destination, (mode & 0o777) or 0o644)
                if len(roots) != 1:
                    raise RepositoryError('Archive must have one top-level extension directory.', 4)
                directory = os.path.join(target, next(iter(roots)))
                if not os.path.isfile(os.path.join(directory, 'install.py')):
                    raise RepositoryError('Archive has no top-level extension install.py.', 4)
                return directory
        except (OSError, EOFError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
            raise RepositoryError('Cannot extract archive: %s' % error, 4)


class Registry:
    """Source registration and disposable catalog caches for one WeeWX configuration.

    Registration writes require the caller's station writer locks. Catalog reads
    and refreshes can replace cache files atomically without changing bindings.
    """

    def __init__(self, config):
        """Read and validate local source definitions without fetching their indexes.

        Args:
            config (dict): WeeWX configuration including the actual config_path.
        """
        self.config = config
        self.configuration_digest = digest(config['config_path'])
        self.options = settings(config)
        self.downloader = Downloader(self.options)
        self.sources = {}
        seen_ids = set()
        seen_urls = set()
        for alias, value in config.get('ExtensionRepositories', {}).items():
            if alias in OPTIONS:
                continue
            try:
                _text(alias, 'repository alias', 64, SLUG)
                _object(value, ('source_id', 'repository_id', 'url'), ('enabled',), 'source')
                _text(value['source_id'], 'source UUID', 36, UUID)
                _text(value['repository_id'], 'repository ID', 128, REPOSITORY_ID)
                location = url(value['url'])
                enabled = str(value.get('enabled', True)).lower()
                if enabled not in ('true', 'false', 'yes', 'no', '1', '0'):
                    raise RepositoryError('Invalid enabled value.')
                if value['source_id'] in seen_ids or location in seen_urls:
                    raise RepositoryError('Duplicate source UUID or URL.')
                seen_ids.add(value['source_id'])
                seen_urls.add(location)
                self.sources[alias] = dict(value, alias=alias, url=location,
                                           enabled=enabled in ('true', 'yes', '1'))
            except RepositoryError as error:
                raise RepositoryError('Invalid source %s: %s' % (display(alias), error), 3)

    def source(self, alias, enabled=True):
        """Return a registered source, normally requiring that it is enabled.

        Args:
            alias (str): Local source alias.
            enabled (bool): True to refuse a disabled source. False allows source
                administration without enabling it.

        Returns:
            dict: Source UUID, publisher ID, canonical URL, alias, and enabled flag.
        """
        if alias not in self.sources:
            raise RepositoryError('Unknown repository alias: ' + display(alias), 3)
        source = self.sources[alias]
        if enabled and not source['enabled']:
            raise RepositoryError('Repository %s is disabled.' % alias)
        return source

    def alias_for_id(self, source_id):
        """Resolve a saved source UUID after an alias rename, or reject a missing source."""
        for alias, source in self.sources.items():
            if source['source_id'] == source_id:
                return alias
        raise RepositoryError('Bound repository UUID is missing: ' + source_id, 3)

    def get(self, alias, refresh=False, persist=True):
        """Read a validated index, checking its publisher identity on retrieval.

        Args:
            alias (str): Enabled source to read.
            refresh (bool): True to contact the source even with a valid cached index.
            persist (bool): True to save a fetched index and its HTTP validators.
                False leaves the cache unchanged for a dry run.

        Returns:
            dict: Validated catalog, registered and final URLs, fetch timestamp,
                and optional HTTP validators. A failed refresh raises an error
                rather than silently returning an older catalog.
        """
        source = self.source(alias)
        path = os.path.join(self.options['cache_root'], source['source_id'] + '.json')
        cached = None
        if os.path.isfile(path):
            try:
                cached = read_json(path)
                if cached['url'] != source['url']:
                    cached = None
                else:
                    cached['catalog'] = validate_catalog(cached['catalog'], cached['final_url'])
                    if cached['catalog']['repository']['id'] != source['repository_id']:
                        cached = None
            except (RepositoryError, KeyError, TypeError):
                cached = None
        if cached and not refresh:
            return cached
        validators = {}
        if cached:
            for field, header in (('etag', 'If-None-Match'), ('last_modified', 'If-Modified-Since')):
                if cached.get(field):
                    validators[header] = cached[field]
        with tempfile.TemporaryDirectory() as temporary:
            target = os.path.join(temporary, 'index.json')
            response = self.downloader.fetch(source['url'], target,
                                             self.options['index_max_bytes'], validators)
            if response.get('not_modified'):
                # Relative archive URLs belong to the final index location. A 304
                # reached at another URL cannot validate those cached resolutions.
                if not cached or response['final_url'] != cached['final_url']:
                    response = self.downloader.fetch(source['url'], target,
                                                     self.options['index_max_bytes'])
                else:
                    entry = dict(cached, fetched_at=time.time())
                    if persist:
                        atomic_json(path, entry)
                    return entry
            if response.get('not_modified'):
                raise RepositoryError('HTTP 304 without a matching validated catalog.', 4)
            catalog = validate_catalog(read_json(target), response['final_url'])
        if catalog['repository']['id'] != source['repository_id']:
            raise RepositoryError('Repository identity changed for %s; register it as a new '
                                  'source instead.' % alias)
        if cached:
            # A version identifies immutable archive bytes. Compare parsed versions
            # so a publisher cannot bypass this check by changing version spelling.
            prior = {(package['id'], packaging.version.Version(release['version'])):
                     release['archive']['sha256'] for package in cached['catalog']['packages']
                     for release in package['releases']}
            for package in catalog['packages']:
                for release in package['releases']:
                    key = (package['id'], packaging.version.Version(release['version']))
                    if key in prior and prior[key] != release['archive']['sha256']:
                        raise RepositoryError('Published archive changed without a new version.', 4)
        entry = dict(response, url=source['url'], catalog=catalog, fetched_at=time.time())
        if persist:
            atomic_json(path, entry)
        return entry

    def add(self, alias, location, dry_run=False):
        """Validate an index before registering a new local source UUID.

        Args:
            alias (str): New local source alias.
            location (str): Index URL or local file path.
            dry_run (bool): True to validate without saving configuration or cache.

        Returns:
            str: Registered alias, or the existing alias if this URL is already
                registered. The caller holds writer locks when saving changes.
        """
        _text(alias, 'repository alias', 64, SLUG)
        if alias in OPTIONS:
            raise RepositoryError('Repository alias is reserved: ' + alias)
        location = url(location)
        for existing, source in self.sources.items():
            if source['url'] == location:
                return existing
        if alias in self.sources:
            raise RepositoryError('Repository alias is already registered: ' + alias, 3)
        with tempfile.TemporaryDirectory() as temporary:
            target = os.path.join(temporary, 'index.json')
            response = self.downloader.fetch(location, target, self.options['index_max_bytes'])
            catalog = validate_catalog(read_json(target), response['final_url'])
        source_id = str(uuid.uuid4())
        if not dry_run:
            self.config.setdefault('ExtensionRepositories', {})[alias] = {
                'source_id': source_id, 'repository_id': catalog['repository']['id'],
                'url': location, 'enabled': True}
            save_configuration(self.config, self.config['config_path'], self.configuration_digest)
            self.sources[alias] = dict(self.config['ExtensionRepositories'][alias], alias=alias,
                                       enabled=True)
            self.configuration_digest = digest(self.config['config_path'])
            atomic_json(os.path.join(self.options['cache_root'], source_id + '.json'),
                        dict(response, url=location, catalog=catalog, fetched_at=time.time()))
        return alias

    def search(self, terms=(), alias=None, package_type=None, refresh=False):
        """Find keyword matches while keeping each source's package separate.

        Args:
            terms (Iterable[str]): Search words; whitespace also separates words
                within a term. Every word must match at least one package field.
            alias (str|None): One source to search, or all enabled sources if None.
            package_type (str|None): Required declared type, or None for any type.
            refresh (bool): True to refresh selected indexes before matching.

        Returns:
            tuple[list, list]: A two-way tuple (matches, failures). Each match is
                (source, package, cache_entry); each failure is (alias, error).
                Matches are sorted by package ID and alias. Failures mean the
                lookup is incomplete, even if another source returned matches.
        """
        if package_type is not None and package_type not in TYPES:
            raise RepositoryError('Unknown extension type: ' + display(package_type))
        selected = [alias] if alias else sorted(self.sources)
        results = []
        errors = []
        words = [word.casefold() for term in terms for word in term.split()]
        for name in selected:
            if not self.source(name, enabled=False)['enabled']:
                if alias:
                    self.source(name)
                continue
            try:
                entry = self.get(name, refresh)
            except RepositoryError as error:
                errors.append((name, error))
                continue
            for package in entry['catalog']['packages']:
                if package_type and package_type not in package['types']:
                    continue
                fields = [package['id'], package['name'], package['description']]
                fields += package['types'] + package['keywords']
                if all(any(word in field.casefold() for field in fields) for word in words):
                    results.append((self.sources[name], package, entry))
        results.sort(key=lambda item: (item[1]['id'], item[0]['alias']))
        return results, errors
