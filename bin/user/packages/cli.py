#
#    Copyright (c) 2009-2026 Tom Keffer <tkeffer@gmail.com> and Matthew Wall
#
#    See the file LICENSE.txt for your rights.
#    Modified 2026-10-23 by weewx-packages: standalone extension namespace.
#
"""Command-line interface for extension sources, installation, updates, and recovery.

Parsers select the operation; ExtensionManager coordinates the station changes.
RepositoryError carries an exit status so partial searches and failed batch
operations remain distinguishable from successful empty results in shell scripts.
"""

import os
import sys
import tempfile
import time
import traceback

import packaging.version

import weecfg
from user.packages import extension_manager
from user.packages import repository
import weeutil.startup
from weeutil.printer import Printer


def _options(parser, mutation=False):
    """Add common station options and, for mutations, preview and confirmation flags."""
    parser.add_argument('--config', metavar='FILENAME', help='Path to weewx.conf.')
    parser.add_argument('--verbosity', type=int, default=1, metavar='N',
                        help='Amount of detail (0|1|2|3).')
    if mutation:
        parser.add_argument('--dry-run', action='store_true', help='Preview without saving changes.')
        parser.add_argument('-y', '--yes', action='store_true',
                            help='Confirm a specified operation; never choose sources or conflicts.')
    parser.set_defaults(func=run)


def _release_options(parser):
    """Add release-selection options without passing them to custom installers."""
    parser.add_argument('--version', metavar='VERSION', help='Select an exact release for this operation.')
    parser.add_argument('--pre', action='store_true', help='Allow pre-release and development packages.')


def add_arguments(parser):
    """Add package operations to the standalone command parser."""
    parser.set_defaults(func=lambda _: parser.print_help())
    actions = parser.add_subparsers(dest='action', title='Actions')
    listing = actions.add_parser('list', help='List installed extensions and their sources.')
    _options(listing)
    install = actions.add_parser('install', help='Install a package ID, file, directory, or URL.')
    install.add_argument('source', metavar='SOURCE')
    install.add_argument('--repo', metavar='ALIAS', help='Select a registered package source.')
    _release_options(install)
    _options(install, True)
    search = actions.add_parser('search', help='Search enabled repositories by keyword.')
    search.add_argument('terms', nargs='*', metavar='TERM')
    search.add_argument('--repo', metavar='ALIAS')
    search.add_argument('--type', dest='package_type', choices=repository.TYPES)
    search.add_argument('--refresh', action='store_true', help='Refresh indexes before searching.')
    _options(search)
    for action in ('check-updates', 'update'):
        child = actions.add_parser(action, help='Check or apply updates from installed sources.')
        child.add_argument('names', nargs='*', metavar='NAME')
        _release_options(child)
        _options(child, action == 'update')
    for action in ('adopt', 'switch-source'):
        child = actions.add_parser(action, help='Explicitly bind an existing extension to a source.')
        child.add_argument('name', metavar='NAME')
        child.add_argument('--repo', required=True, metavar='ALIAS')
        child.add_argument('--package', required=True, metavar='PACKAGE_ID')
        if action == 'switch-source':
            _release_options(child)
        _options(child, True)
    for action in ('detach', 'uninstall', 'recover'):
        child = actions.add_parser(action, help='Detach, remove, or recover extension state.')
        if action != 'recover':
            child.add_argument('name', metavar='NAME')
        _options(child, True)
    repositories = actions.add_parser('repo', help='Manage arbitrary repository index URLs.')
    repositories.set_defaults(func=lambda _: repositories.print_help())
    repo_actions = repositories.add_subparsers(dest='repo_action', title='Repository actions')
    for action in ('add', 'list', 'refresh', 'validate', 'enable', 'disable', 'rename',
                   'set-url', 'remove'):
        child = repo_actions.add_parser(action)
        if action in ('add', 'enable', 'disable', 'rename', 'set-url', 'remove'):
            child.add_argument('alias', metavar='ALIAS')
        if action == 'refresh':
            child.add_argument('alias', nargs='?', metavar='ALIAS')
        if action in ('add', 'set-url'):
            child.add_argument('url', metavar='URL')
        if action == 'rename':
            child.add_argument('new_alias', metavar='NEW_ALIAS')
        if action == 'validate':
            child.add_argument('source', metavar='FILE_OR_URL')
        _options(child, action not in ('list', 'validate'))


def run(namespace, extra_args=None):
    """Dispatch parsed extension options and translate failures to command exit codes.

    namespace is the argparse namespace for the selected action. extra_args holds
    installer arguments left by weectl, including those supplied after '--'. Only
    installation, update, and source-switch actions accept these arguments.
    """
    try:
        extra_args = extra_args or []
        if extra_args and namespace.action not in ('install', 'update', 'switch-source'):
            raise repository.RepositoryError('Unexpected arguments: ' + ' '.join(extra_args))
        if namespace.action == 'repo' and namespace.repo_action == 'validate':
            # Publishers must be able to check an index without creating a station
            # or registering the source, so this action precedes application startup.
            downloader = repository.Downloader()
            with tempfile.TemporaryDirectory() as temporary:
                target = os.path.join(temporary, 'index.json')
                response = downloader.fetch(namespace.source, target,
                                             repository.DEFAULTS['index_max_bytes'])
                catalog = repository.validate_catalog(repository.read_json(target),
                                                              response['final_url'])
            print('Valid repository %s: %d packages.' %
                  (repository.display(catalog['repository']['name']), len(catalog['packages'])))
            return
        for attribute in ('source', 'url'):
            # Startup logs the command line. Reject embedded URL credentials before
            # that log entry can expose them.
            value = getattr(namespace, attribute, '')
            if '://' in value:
                repository.url(value)
        _, config, _ = weeutil.startup.start_app('weectl', __name__, namespace.config, None)
        manager = extension_manager.ExtensionManager(
            config, getattr(namespace, 'dry_run', False), Printer(namespace.verbosity))
        yes = getattr(namespace, 'yes', False)
        if namespace.action == 'repo':
            repository_action(manager, namespace)
        elif namespace.action == 'search':
            search(manager, namespace)
        elif namespace.action == 'list':
            list_installed(manager)
        elif namespace.action == 'install':
            manager.install(namespace.source, namespace.repo, namespace.version, namespace.pre,
                            yes, extra_args)
        elif namespace.action in ('check-updates', 'update'):
            manager.updates(namespace.names, namespace.version, namespace.pre,
                            namespace.action == 'check-updates', yes, extra_args)
        elif namespace.action == 'adopt':
            manager.adopt(namespace.name, namespace.repo, namespace.package, yes)
        elif namespace.action == 'switch-source':
            manager.switch_source(namespace.name, namespace.repo, namespace.package,
                                  namespace.version, namespace.pre, yes, extra_args)
        elif namespace.action == 'detach':
            manager.detach(namespace.name, yes)
        elif namespace.action == 'uninstall':
            manager.uninstall(namespace.name, yes)
        elif namespace.action == 'recover':
            manager.recover(yes)
    except repository.RepositoryError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(error.code)
    except KeyboardInterrupt:
        print('Interrupted. Check python -m user.packages recover before another mutation.', file=sys.stderr)
        raise SystemExit(4)
    except Exception as error:
        if namespace.verbosity >= 3:
            traceback.print_exc()
        print('Extension operation failed: %s' % error, file=sys.stderr)
        raise SystemExit(4)


def repository_action(manager, namespace):
    """List sources or apply one parsed repository action under the station locks.

    The namespace supplies repo_action and that action's source arguments. Source
    removal and URL changes validate existing bindings before saving configuration.
    """
    action = namespace.repo_action
    if action == 'list':
        for alias, source in sorted(manager.registry.sources.items()):
            print('%s [%s] %s' % (alias, 'enabled' if source['enabled'] else 'disabled',
                                   repository.display(source['url'])))
        if not manager.registry.sources:
            print('No repositories registered. Use python -m user.packages repo add ALIAS URL.')
        return
    with manager.operation():
        registry = manager.registry
        if action == 'refresh':
            failures = []
            aliases = [namespace.alias] if namespace.alias else sorted(
                name for name, source in registry.sources.items() if source['enabled'])
            for alias in aliases:
                try:
                    entry = registry.get(alias, refresh=True, persist=not manager.dry_run)
                    print('Refreshed %s: %d packages.' % (alias, len(entry['catalog']['packages'])))
                except repository.RepositoryError as error:
                    print('%s: %s' % (alias, error))
                    failures.append(error)
            if failures:
                raise repository.RepositoryError('Repository refresh incomplete.',
                                                       max(error.code for error in failures))
            return
        if action == 'add':
            if not manager.dry_run:
                repository.confirm('Register repository %s at %s?' %
                                           (namespace.alias, namespace.url), namespace.yes)
            alias = registry.add(namespace.alias, namespace.url, manager.dry_run)
            print('%s repository %s.' % ('Would register' if manager.dry_run else 'Registered', alias))
            return
        source = registry.source(namespace.alias, enabled=False)
        if action == 'remove':
            if any(state['source_id'] == source['source_id'] for state in manager.store.all().values()):
                raise repository.RepositoryError('Source has installed bindings; detach, switch, '
                                                       'or uninstall those extensions first.', 3)
        if action == 'rename':
            repository._text(namespace.new_alias, 'repository alias', 64, repository.SLUG)
            if namespace.new_alias in registry.sources or namespace.new_alias in repository.OPTIONS:
                raise repository.RepositoryError('New repository alias is already used or reserved.', 3)
        if action == 'set-url':
            # Moving an index can keep its local UUID and all installed bindings.
            # Replacing its publisher identity requires a separately registered source.
            location = repository.url(namespace.url)
            if any(item['url'] == location and alias != namespace.alias
                   for alias, item in registry.sources.items()):
                raise repository.RepositoryError('URL is registered under another alias.', 3)
            with tempfile.TemporaryDirectory() as temporary:
                target = os.path.join(temporary, 'index.json')
                response = registry.downloader.fetch(location, target, registry.options['index_max_bytes'])
                catalog = repository.validate_catalog(repository.read_json(target),
                                                              response['final_url'])
            if catalog['repository']['id'] != source['repository_id']:
                raise repository.RepositoryError('A different repository ID requires a new source.')
        if manager.dry_run:
            print('Would %s repository %s.' % (action, namespace.alias))
            return
        repository.confirm('%s repository %s?' % (action.capitalize(), namespace.alias),
                                   namespace.yes)
        section = manager.config['ExtensionRepositories']
        if action in ('enable', 'disable'):
            section[namespace.alias]['enabled'] = action == 'enable'
        elif action == 'rename':
            # Bindings use source_id, so an alias rename must preserve that UUID.
            section[namespace.new_alias] = dict(section[namespace.alias])
            del section[namespace.alias]
        elif action == 'remove':
            del section[namespace.alias]
        elif action == 'set-url':
            section[namespace.alias]['url'] = location
        repository.save_configuration(manager.config, manager.config_path,
                                              manager.registry.configuration_digest)
        if action in ('remove', 'set-url'):
            cache = os.path.join(registry.options['cache_root'], source['source_id'] + '.json')
            if os.path.exists(cache):
                os.unlink(cache)
        print('Repository %s: %s.' % (namespace.alias, action))


def search(manager, namespace):
    """Display keyword matches and signal when any selected source failed.

    The namespace contains search terms and optional source/type/refresh filters.
    Available matches are printed before an incomplete lookup returns an error.
    """
    results, errors = manager.registry.search(namespace.terms, namespace.repo,
                                               namespace.package_type, namespace.refresh)
    states = manager.store.all()
    for source, package, entry in results:
        latest = max(package['releases'], key=lambda item: packaging.version.Version(item['version']))
        release = repository.select_release(package)
        compatible = release['version'] if release else 'none: ' + '; '.join(
            repository.incompatibility(item) for item in package['releases'])
        installed = next((state['version'] for state in states.values()
                          if state['source_id'] == source['source_id'] and state['package_id'] == package['id']), '-')
        print('%s [%s] installed %s; latest %s; compatible %s' % (
            package['id'], source['alias'], installed, latest['version'], compatible))
        print('  ' + repository.display(package['description']))
        print('  Index age: %ds' % max(0, int(time.time() - entry['fetched_at'])))
    for alias, error in errors:
        print('Incomplete search, %s: %s' % (alias, error))
    if not results and not errors:
        print('No matching packages.')
    if errors:
        raise repository.RepositoryError('Search results are incomplete.',
                                               max(error.code for _, error in errors))


def list_installed(manager):
    """Display managed state, pending recovery, and legacy installer information.

    Args:
        manager (extension_manager.ExtensionManager): Station to inspect.

    Invalid records are reported individually and make the overall command fail.
    Legacy information requires loading the already cached Python installer.
    """
    root = manager.store.extension_root
    names = sorted(os.listdir(root)) if os.path.isdir(root) else []
    pending = {item['name'] for item in manager.store.pending()}
    failures = []
    print('%-22s %-12s %-18s %s' % ('Installed name', 'Version', 'Source', 'Status / package'))
    for name in sorted(set(names) | pending):
        if not os.path.isdir(os.path.join(root, name)) and name not in pending:
            continue
        try:
            state = manager.store.read(name)
            if name in pending:
                print('%-22s %-12s %-18s %s' % (name, '?', '?', 'awaiting recovery'))
            elif state:
                alias = manager.registry.alias_for_id(state['source_id']) if state['source_id'] else '-'
                status = 'managed' if state['source_id'] else 'detached'
                print('%-22s %-12s %-18s %s / %s' %
                      (name, state['version'], alias, status, state['package_id'] or '-'))
            else:
                _, installer = weecfg.get_extension_installer(os.path.join(root, name))
                print('%-22s %-12s %-18s %s' % (name, installer.get('version', '?'), '-', 'legacy'))
        except Exception as error:
            print('%s: invalid state/installer: %s' % (name, error))
            failures.append(error)
    if failures:
        raise repository.RepositoryError('Some installed extensions could not be listed.', 3)
