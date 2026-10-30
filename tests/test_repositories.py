#
#    See the file LICENSE.txt for your rights.
#    Modified 2026-10-23 by weewx-packages: standalone extension namespace.
#
"""Contract, source binding, conflict, and recovery regressions for extension repositories."""

import copy
import hashlib
import http.server
import json
import importlib.resources
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile

import configobj
import pytest

import weecfg
from user.packages import extension_manager
from user.packages import extension_state
from user.packages import repository
from user.packages import requirements


@pytest.fixture
def configuration(tmp_path):
    path = tmp_path / 'station' / 'weewx.conf'
    path.parent.mkdir()
    template = importlib.resources.files('weewx_data') / 'weewx.conf'
    shutil.copyfile(str(template), str(path))
    config = configobj.ConfigObj(str(path), encoding='utf-8', interpolation=False)
    config['WEEWX_ROOT'] = str(path.parent)
    config['USER_ROOT'] = 'bin/user'
    config['StdReport']['SKIN_ROOT'] = 'skins'
    config.write()
    (path.parent / 'bin' / 'user').mkdir(parents=True)
    (path.parent / 'bin' / 'user' / '__init__.py').write_text('')
    return weecfg.read_config(str(path))[1]


def make_release(tmp_path, version='1.0', contents='first', hook='', installer_version=None,
                 file_names=None, installer_name='nordic'):
    directory = tmp_path / ('source-' + version)
    directory.mkdir(exist_ok=True)
    package = directory / 'nordic'
    package.mkdir(exist_ok=True)
    names = file_names or ['skins/Nordic/index.html.tmpl']
    for name in names:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    installer = """import weecfg.extension


def loader():
    return Installer()


class Installer(weecfg.extension.ExtensionInstaller):
    def __init__(self):
        super().__init__(name=%r, version=%r, description='Test skin',
                         files=[('skins/Nordic', %r)],
                         config={'StdReport': {'NordicReport': {'skin': 'Nordic', 'enable': 'true'}}})
%s
""" % (installer_name, installer_version or version, names, hook)
    (package / 'install.py').write_text(installer)
    archive = tmp_path / ('nordic-' + version + '.zip')
    with zipfile.ZipFile(str(archive), 'w') as bundle:
        for path in package.rglob('*'):
            if path.is_file():
                bundle.write(str(path), path.relative_to(directory).as_posix())
    return {'version': version, 'requires_python': '>=3.6', 'requires_weewx': '>=5,<6',
            'archive': {'url': archive.as_uri(), 'format': 'zip',
                        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()},
            'restart_required': False}


def catalog(releases, repository_id='test.weather'):
    return {'schema_version': 1, 'repository': {'id': repository_id, 'name': 'Test weather'},
            'packages': [{'id': 'nordic', 'installer_name': 'nordic', 'name': 'Nordic',
                          'description': 'A test forecast skin', 'types': ['skin'], 'category': 'skin',
                          'keywords': ['ecowitt', 'forecast'], 'releases': releases}]}


def write_catalog(tmp_path, releases, name='repository.json', repository_id='test.weather'):
    path = tmp_path / name
    path.write_text(json.dumps(catalog(releases, repository_id)))
    return path


def register(config, alias, path):
    manager = extension_manager.ExtensionManager(config)
    with manager.operation():
        manager.registry.add(alias, str(path))
    manager.reload()
    return manager


@pytest.mark.parametrize('changes', [
    lambda item: item.update(schema_version=True),
    lambda item: item.update(unknown=True),
    lambda item: item['packages'].append(copy.deepcopy(item['packages'][0])),
    lambda item: item['packages'][0]['releases'].append(dict(item['packages'][0]['releases'][0], version='1.0.0')),
    lambda item: item['packages'][0]['releases'][0].update(requires_python='3.6 or later'),
    lambda item: item['packages'][0]['releases'][0]['archive'].pop('sha256'),
    lambda item: item['packages'][0].update(keywords=['Ecowitt', 'ecowitt']),
    lambda item: item['packages'][0].update(installer_name='../outside'),
    lambda item: item['packages'][0].pop('category'),
    lambda item: item['packages'][0].update(category='service'),
])
def test_reject_invalid_contract(tmp_path, changes):
    document = catalog([make_release(tmp_path)])
    changes(document)
    with pytest.raises(repository.RepositoryError):
        repository.validate_catalog(document, (tmp_path / 'repository.json').as_uri())


@pytest.mark.parametrize('document', ['{"schema_version":1,"schema_version":1}',
                                      '{"number":NaN}', '{"number":Infinity}'])
def test_strict_json(document):
    with pytest.raises(repository.RepositoryError):
        repository.decode_json(document)


def test_catalog_versions_and_search(configuration, tmp_path):
    old = make_release(tmp_path)
    newer = make_release(tmp_path, '2.0')
    newer['requires_python'] = '>=99'
    manager = register(configuration, 'personal', write_catalog(tmp_path, [old, newer]))
    results, errors = manager.registry.search(['ECOWITT', 'skin'])
    assert len(results) == 1 and not errors
    assert repository.select_release(results[0][1])['version'] == '1.0'
    assert manager.registry.search(['ecowitt'], package_type='driver')[0] == []
    assert len(manager.registry.search([], category='skin')[0]) == 1
    assert manager.registry.search([], category='uploader')[0] == []


def test_install_update_and_state(configuration, tmp_path):
    first = make_release(tmp_path)
    second = make_release(tmp_path, '2.0', 'second')
    path = write_catalog(tmp_path, [first])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    target = pathlib.Path(manager.store.roots['skins']) / 'Nordic' / 'index.html.tmpl'
    assert target.read_text() == 'first'
    state = manager.store.read('nordic')
    assert state['source_id'] == manager.registry.sources['personal']['source_id']
    assert state['version'] == '1.0'
    assert state['config_entries']
    assert not manager.store.pending()
    path.write_text(json.dumps(catalog([first, second])))
    manager.updates(['nordic'], yes=True)
    assert target.read_text() == 'second'
    assert manager.store.read('nordic')['version'] == '2.0'
    assert manager.config['StdReport']['NordicReport']['skin'] == 'Nordic'
    manager.uninstall('nordic', yes=True)
    assert not target.exists()
    assert not os.path.exists(manager.store.state_path('nordic'))
    assert 'NordicReport' not in manager.config['StdReport']


def test_selected_source_does_not_follow_higher_version(configuration, tmp_path):
    first = make_release(tmp_path)
    other = make_release(tmp_path, '8.0', 'wrong-source')
    manager = register(configuration, 'personal', write_catalog(tmp_path, [first]))
    register(manager.config, 'alternative', write_catalog(tmp_path, [other], 'other.json', 'other.weather'))
    manager.reload()
    with pytest.raises(repository.RepositoryError, match='terminal'):
        manager.install('nordic', yes=True)
    manager.install('nordic', alias='personal', yes=True)
    manager.updates(['nordic'], yes=True)
    assert manager.store.read('nordic')['version'] == '1.0'
    with pytest.raises(repository.RepositoryError, match='switch-source'):
        manager.install('nordic', alias='alternative', yes=True)
    manager.switch_source('nordic', 'alternative', 'nordic', yes=True)
    assert manager.store.read('nordic')['version'] == '8.0'


def test_bad_checksum_never_imports_installer(configuration, tmp_path):
    release = make_release(tmp_path, hook="    def configure(self, engine):\n        raise RuntimeError('must not run')\n")
    release['archive']['sha256'] = '0' * 64
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    with pytest.raises(repository.RepositoryError, match='SHA-256'):
        manager.install('nordic', yes=True)
    assert not os.path.isdir(manager.store.extension_root)


def test_metadata_mismatch_never_writes(configuration, tmp_path):
    release = make_release(tmp_path, installer_version='2.0')
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    with pytest.raises(repository.RepositoryError, match='differs'):
        manager.install('nordic', yes=True)
    assert not os.path.isdir(manager.store.extension_root)


def test_modified_file_requires_choice_even_with_yes(configuration, tmp_path):
    first = make_release(tmp_path)
    second = make_release(tmp_path, '2.0', 'second')
    path = write_catalog(tmp_path, [first])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    target = pathlib.Path(manager.store.roots['skins']) / 'Nordic' / 'index.html.tmpl'
    target.write_text('my customization')
    path.write_text(json.dumps(catalog([first, second])))
    with pytest.raises(repository.RepositoryError):
        manager.updates(['nordic'], yes=True)
    assert target.read_text() == 'my customization'
    assert manager.store.read('nordic')['version'] == '1.0'


def test_preserved_file_stays_a_conflict(configuration, tmp_path, monkeypatch):
    first = make_release(tmp_path)
    second = make_release(tmp_path, '2.0', 'second')
    third = make_release(tmp_path, '3.0', 'third')
    path = write_catalog(tmp_path, [first])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    target = pathlib.Path(manager.store.roots['skins']) / 'Nordic' / 'index.html.tmpl'
    target.write_text('custom')
    path.write_text(json.dumps(catalog([first, second])))
    choices = []
    monkeypatch.setattr(repository, 'choose', lambda *args: choices.append(args) or 0)
    manager.updates(['nordic'], yes=True)
    assert target.read_text() == 'custom'
    saved = manager.store.read('nordic')['files'][0]
    assert saved['preserved'] and saved['replacement']
    backup = pathlib.Path(manager.store.options['transaction_root']) / saved['replacement']['transaction_id'] / saved['replacement']['path']
    assert backup.read_text() == 'second'
    path.write_text(json.dumps(catalog([first, second, third])))
    manager.updates(['nordic'], yes=True)
    assert len(choices) == 2
    assert target.read_text() == 'custom'


def test_failed_hook_restores_files_and_requires_review(configuration, tmp_path):
    first = make_release(tmp_path)
    failing = make_release(tmp_path, '2.0', 'changed',
                           hook="    def configure(self, engine):\n        raise RuntimeError('hook failure')\n")
    path = write_catalog(tmp_path, [first])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    target = pathlib.Path(manager.store.roots['skins']) / 'Nordic' / 'index.html.tmpl'
    before_config = pathlib.Path(manager.config_path).read_bytes()
    path.write_text(json.dumps(catalog([first, failing])))
    with pytest.raises(repository.RepositoryError, match='failed'):
        manager.updates(['nordic'], yes=True)
    assert target.read_text() == 'first'
    assert pathlib.Path(manager.config_path).read_bytes() == before_config
    assert manager.store.read('nordic')['version'] == '1.0'
    assert manager.store.pending()[0]['status'] == 'needs_attention'
    with pytest.raises(repository.RepositoryError, match='recover'):
        manager.detach('nordic', yes=True)


def test_interrupted_transaction_recovers_without_overwriting_editor(configuration):
    manager = extension_manager.ExtensionManager(configuration)
    target = pathlib.Path(manager.store.roots['user']) / 'interruption.py'
    target.write_text('before')
    with manager.store.locked():
        transaction = extension_state.Transaction(manager.store, 'test', 'test', [str(target)])
        transaction.write(str(target), data=b'after')
    assert manager.store.pending()
    target.write_text('editor change')
    with pytest.raises(repository.RepositoryError, match='subsequent edit'):
        manager.recover(yes=True)
    assert target.read_text() == 'editor change'
    target.write_text('after')
    manager.recover(yes=True)
    assert target.read_text() == 'before'
    assert not manager.store.pending()


@pytest.mark.parametrize('name', ['../escape', '/escape', 'root/../escape', 'root/./file', 'C:/escape'])
def test_unsafe_archives_are_rejected(tmp_path, name):
    archive = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(str(archive), 'w') as bundle:
        bundle.writestr(name, 'bad')
    with pytest.raises(repository.RepositoryError):
        repository.Downloader().extract(str(archive), 'zip', str(tmp_path / 'out'))
    assert not (tmp_path / 'escape').exists()


def test_dry_run_does_not_call_custom_hooks_or_write_cache(configuration, tmp_path):
    hook = "    def configure(self, engine):\n        raise RuntimeError('must not run')\n"
    release = make_release(tmp_path, hook=hook)
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    cache = pathlib.Path(manager.registry.options['cache_root'])
    before = {path.name: path.read_bytes() for path in cache.iterdir()}
    preview = extension_manager.ExtensionManager(manager.config, dry_run=True)
    preview.install('nordic', yes=True)
    assert before == {path.name: path.read_bytes() for path in cache.iterdir()}
    assert not os.path.isdir(manager.store.extension_root)


def test_numeric_overflow_is_not_valid_json():
    with pytest.raises(repository.RepositoryError):
        repository.decode_json('{"extensions":{"number":1e999}}')


def test_edit_between_planning_and_snapshot_is_not_overwritten(configuration, tmp_path, monkeypatch):
    first = make_release(tmp_path)
    second = make_release(tmp_path, '2.0', 'second')
    path = write_catalog(tmp_path, [first])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    target = pathlib.Path(manager.store.roots['skins']) / 'Nordic' / 'index.html.tmpl'
    path.write_text(json.dumps(catalog([first, second])))
    original = extension_state.Transaction.snapshot

    def edit_before_snapshot(transaction, destination):
        if destination == str(target):
            target.write_text('concurrent editor')
        return original(transaction, destination)

    monkeypatch.setattr(extension_state.Transaction, 'snapshot', edit_before_snapshot)
    with pytest.raises(repository.RepositoryError):
        manager.updates(['nordic'], yes=True)
    assert target.read_text() == 'concurrent editor'
    assert manager.store.read('nordic')['version'] == '1.0'


def test_adopt_detached_install_preserves_known_ownership(configuration, tmp_path):
    release = make_release(tmp_path)
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    manager.install_direct(release['archive']['url'], yes=True)
    before = manager.store.read('nordic')
    assert before['source_id'] is None
    manager.adopt('nordic', 'personal', 'nordic', yes=True)
    after = manager.store.read('nordic')
    assert after['source_id'] == manager.registry.sources['personal']['source_id']
    assert after['files'] == before['files']
    assert after['config_entries'] == before['config_entries']
    manager.updates(['nordic'], yes=True)
    manager.uninstall('nordic', yes=True)
    assert 'NordicReport' not in manager.config['StdReport']


def test_check_updates_reports_incompatible_installed_release(configuration, tmp_path):
    release = make_release(tmp_path)
    path = write_catalog(tmp_path, [release])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    release['requires_python'] = '>=99'
    older = make_release(tmp_path, '0.9')
    path.write_text(json.dumps(catalog([older, release])))
    with pytest.raises(repository.RepositoryError, match='failed'):
        manager.updates(['nordic'], check=True)
    assert manager.store.read('nordic')['version'] == '1.0'


def test_update_and_uninstall_preserve_existing_and_edited_settings(configuration, tmp_path):
    configuration['StdReport']['NordicReport'] = {'skin': 'Existing'}
    configuration.write()
    first = make_release(tmp_path)
    second = make_release(tmp_path, '2.0', 'second')
    path = write_catalog(tmp_path, [first])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    _, changed = weecfg.read_config(manager.config_path)
    changed['StdReport']['NordicReport']['enable'] = 'false'
    weecfg.save(changed, manager.config_path)
    path.write_text(json.dumps(catalog([first, second])))
    manager.updates(['nordic'], yes=True)
    assert manager.config['StdReport']['NordicReport']['skin'] == 'Existing'
    assert manager.config['StdReport']['NordicReport']['enable'] == 'false'
    with pytest.raises(repository.RepositoryError, match='terminal'):
        manager.uninstall('nordic', yes=True)
    assert pathlib.Path(manager.store.state_path('nordic')).is_file()


@pytest.fixture
def http_repository(tmp_path):
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get('If-None-Match')))
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/repository.json')
                self.end_headers()
            elif self.path == '/repository.json' and self.headers.get('If-None-Match') == '"v1"':
                self.send_response(304)
                self.end_headers()
            else:
                path = tmp_path / self.path.lstrip('/')
                if not path.is_file():
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('ETag', '"v1"')
                self.end_headers()
                self.wfile.write(path.read_bytes())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d' % server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def cli(config_path, *arguments):
    environment = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).parents[1] / 'bin'))
    return subprocess.run([sys.executable, '-m', 'user.packages'] + list(arguments) +
                          ['--config', str(config_path)], env=environment,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=30)


def test_http_cli_install_search_refresh_update(configuration, tmp_path, http_repository):
    address, requests = http_repository
    release = make_release(tmp_path)
    release['archive']['url'] = 'nordic-1.0.zip'
    write_catalog(tmp_path, [release])
    result = cli(configuration['config_path'], 'repo', 'add', 'personal', address + '/redirect', '-y')
    assert result.returncode == 0, result.stdout
    result = cli(configuration['config_path'], 'search', 'ECOWITT', 'forecast', '--refresh')
    assert result.returncode == 0 and 'nordic [personal]' in result.stdout, result.stdout
    assert ('/repository.json', '"v1"') in requests
    result = cli(configuration['config_path'], 'install', 'nordic', '-y')
    assert result.returncode == 0, result.stdout
    assert any(path == '/nordic-1.0.zip' for path, _ in requests)
    result = cli(configuration['config_path'], 'list')
    assert result.returncode == 0 and 'managed / nordic' in result.stdout, result.stdout
    result = cli(configuration['config_path'], 'update', 'nordic', '-y')
    assert result.returncode == 0 and 'already installed' in result.stdout, result.stdout


def test_one_failed_repository_prevents_implicit_source_choice(configuration, tmp_path):
    release = make_release(tmp_path)
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    other = write_catalog(tmp_path, [release], 'other.json', 'other.weather')
    register(manager.config, 'alternative', other)
    manager.reload()
    other.unlink()
    with pytest.raises(repository.RepositoryError, match='Incomplete lookup'):
        manager.install('nordic', yes=True)
    assert not os.path.isdir(manager.store.extension_root)
    manager.install('nordic', alias='personal', yes=True)
    assert manager.store.read('nordic')['source_id'] == manager.registry.sources['personal']['source_id']


@pytest.mark.parametrize('location,base', [
    ('file:///etc/passwd', 'https://example.invalid/repository.json'),
    ('http://example.invalid/file.zip', 'https://example.invalid/repository.json'),
    ('https://user:password@example.invalid/repository.json', None),
    ('https://example.invalid/repository.json#fragment', None),
    ('https://example.invalid/\nrepository.json', None),
])
def test_unsafe_urls_are_rejected(location, base):
    with pytest.raises(repository.RepositoryError):
        repository.url(location, base)


def test_changed_release_digest_is_rejected_without_cache(configuration, tmp_path):
    release = make_release(tmp_path)
    path = write_catalog(tmp_path, [release])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    shutil.rmtree(manager.registry.options['cache_root'])
    release['archive']['sha256'] = '0' * 64
    path.write_text(json.dumps(catalog([release])))
    with pytest.raises(repository.RepositoryError, match='failed'):
        manager.updates(['nordic'], yes=True)
    assert manager.store.read('nordic')['version'] == '1.0'


def test_changed_roots_are_not_silently_rebound(configuration, tmp_path):
    release = make_release(tmp_path)
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    manager.install('nordic', yes=True)
    _, changed = weecfg.read_config(manager.config_path)
    changed['StdReport']['SKIN_ROOT'] = 'other-skins'
    weecfg.save(changed, manager.config_path)
    with pytest.raises(repository.RepositoryError, match='roots changed'):
        manager.detach('nordic', yes=True)
    original = pathlib.Path(configuration['WEEWX_ROOT']) / 'skins' / 'Nordic' / 'index.html.tmpl'
    assert original.read_text() == 'first'


def test_both_physical_writer_locks_exclude_another_process(configuration):
    manager = extension_manager.ExtensionManager(configuration)
    probe = """import fcntl, sys
with open(sys.argv[1], 'a+b') as stream:
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(10)
"""
    locations = (manager.store.config_path, manager.store.extension_root)
    for location in locations:
        path = os.path.join(os.path.dirname(location),
                            '.' + os.path.basename(location) + '.extensions.lock')
        with manager.store.locked():
            blocked = subprocess.run([sys.executable, '-c', probe, path], timeout=10)
            assert blocked.returncode == 10
        available = subprocess.run([sys.executable, '-c', probe, path], timeout=10)
        assert available.returncode == 0


def test_installer_arguments_after_separator_are_forwarded(configuration, tmp_path):
    hook = """    def process_args(self, args):
        if args != ['--version', 'custom']:
            raise RuntimeError('Wrong installer arguments: %r' % args)
"""
    release = make_release(tmp_path, hook=hook)
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    environment = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).parents[1] / 'bin'))
    result = subprocess.run([sys.executable, '-m', 'user.packages', 'install', 'nordic',
                             '--config', manager.config_path, '-y', '--', '--version', 'custom'],
                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True, timeout=30)
    assert result.returncode == 0, result.stdout
    assert manager.store.read('nordic')['version'] == '1.0'


def test_direct_install_keeps_legacy_destinations(configuration, tmp_path):
    make_release(tmp_path)
    directory = tmp_path / 'source-1.0' / 'nordic'
    installer = directory / 'install.py'
    installer.write_text(installer.read_text().replace('skins/Nordic', 'extras/Nordic'))
    (directory / 'skins').rename(directory / 'extras')
    manager = extension_manager.ExtensionManager(configuration)
    manager.install_direct(str(directory), yes=True)
    assert (pathlib.Path(configuration['WEEWX_ROOT']) / 'extras/Nordic/index.html.tmpl').read_text() == 'first'
    assert manager.store.read('nordic') is None
    manager.updates(yes=True)
    manager.uninstall('nordic', yes=True)
    assert not (pathlib.Path(configuration['WEEWX_ROOT']) / 'extras/Nordic/index.html.tmpl').exists()


def test_tar_package_preserves_executable_modes(configuration, tmp_path):
    release = make_release(tmp_path)
    directory = tmp_path / 'source-1.0' / 'nordic'
    (directory / 'skins/Nordic/index.html.tmpl').chmod(0o755)
    archive = tmp_path / 'nordic-1.0.tar.gz'
    with tarfile.open(str(archive), 'w:gz') as bundle:
        bundle.add(str(directory), arcname='nordic')
    release['archive'] = {'url': archive.as_uri(), 'format': 'tar.gz',
                          'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    manager.install('nordic', yes=True)
    target = pathlib.Path(manager.store.roots['skins']) / 'Nordic/index.html.tmpl'
    assert target.read_text() == 'first'
    assert target.stat().st_mode & 0o777 == 0o755


def test_backup_race_never_creates_a_false_baseline(configuration, monkeypatch):
    manager = extension_manager.ExtensionManager(configuration)
    target = pathlib.Path(manager.store.roots['user']) / 'backup.py'
    target.write_text('before')
    original = repository.copy_file

    def edit_during_backup(source, destination):
        if source == str(target):
            target.write_text('editor change')
        return original(source, destination)

    monkeypatch.setattr(repository, 'copy_file', edit_during_backup)
    with manager.store.locked():
        with pytest.raises(repository.RepositoryError, match='during backup'):
            extension_state.Transaction(manager.store, 'test', 'test', [str(target)])
    manager.recover(yes=True)
    assert target.read_text() == 'editor change'


def test_failed_import_does_not_abort_unrelated_batch_updates(configuration, tmp_path):
    first = make_release(tmp_path)
    broken = make_release(tmp_path, '2.0', hook="\nraise RuntimeError('broken installer')\n")
    other = tmp_path / 'other'
    other.mkdir()
    zeta_first = make_release(other, installer_name='zeta', file_names=['skins/Nordic/zeta.html'])
    zeta_second = make_release(other, '2.0', 'second', installer_name='zeta',
                                file_names=['skins/Nordic/zeta.html'])

    def document(nordic, zeta):
        value = catalog(nordic)
        package = copy.deepcopy(catalog(zeta)['packages'][0])
        package.update(id='zeta', installer_name='zeta')
        value['packages'].append(package)
        return value

    path = tmp_path / 'repository.json'
    path.write_text(json.dumps(document([first], [zeta_first])))
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    manager.install('zeta', yes=True)
    path.write_text(json.dumps(document([first, broken], [zeta_first, zeta_second])))
    with pytest.raises(repository.RepositoryError) as failure:
        manager.updates(yes=True)
    assert failure.value.code == 4
    assert manager.store.read('nordic')['version'] == '1.0'
    assert manager.store.read('zeta')['version'] == '2.0'
    assert not manager.store.pending()


def test_hook_supplied_root_setting_survives_update_and_is_owned(configuration, tmp_path):
    hook = """    def configure(self, engine):
        self['config']['repository_test'] = 'added'
        return False
"""
    first = make_release(tmp_path, hook=hook)
    second = make_release(tmp_path, '2.0', hook=hook)
    path = write_catalog(tmp_path, [first])
    manager = register(configuration, 'personal', path)
    manager.install('nordic', yes=True)
    root = next(item for item in manager.store.read('nordic')['config_entries']
                if item['key'] == 'repository_test')
    assert root['section'] == [] and root['value'] == 'added'
    path.write_text(json.dumps(catalog([first, second])))
    manager.updates(['nordic'], yes=True)
    assert manager.config['repository_test'] == 'added'
    manager.uninstall('nordic', yes=True)
    assert 'repository_test' not in manager.config


@pytest.mark.parametrize('changes', [
    lambda release: release.update(requirements={'python': ['numpy>='], 'debian': []}),
    lambda release: release.update(requirements={'debian': ['Python3-Numpy']}),
    lambda release: release.update(requirements={'cargo': ['serde']}),
    lambda release: release.update(setup=[{'command': 'usermod -aG plugdev {account}'}]),
    lambda release: release.update(setup=[]),
])
def test_reject_invalid_requirements(tmp_path, changes):
    document = catalog([make_release(tmp_path)])
    changes(document['packages'][0]['releases'][0])
    with pytest.raises(repository.RepositoryError):
        repository.validate_catalog(document, (tmp_path / 'repository.json').as_uri())


def test_install_provides_requirements_and_prints_setup(configuration, tmp_path, monkeypatch, capsys):
    release = make_release(tmp_path)
    release['requirements'] = {'python': ['packaging', 'pytest>=999', 'surely-absent-distribution>=1'],
                               'debian': ['python3-surely-absent', 'librtlsdr-surely-absent']}
    release['setup'] = [{'command': 'sudo weectl station reconfigure --driver=user.nordic'},
                        {'command': 'sudo usermod -aG plugdev {user}', 'note': 'dongle access'}]
    manager = register(configuration, 'personal', write_catalog(tmp_path, [release]))
    commands = []
    monkeypatch.setattr(requirements, 'in_virtual_environment', lambda: True)
    monkeypatch.setattr(requirements.shutil, 'which', lambda name: '/usr/bin/' + name)

    def run(command, **options):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='not-installed')

    monkeypatch.setattr(requirements.subprocess, 'run', run)
    manager.install('nordic', yes=True)
    output = capsys.readouterr().out
    pip = [command for command in commands if 'pip' in command]
    # Only what is absent or too old is installed, into WeeWX's own interpreter.
    assert pip == [[sys.executable, '-m', 'pip', 'install', 'pytest>=999',
                    'surely-absent-distribution>=1']]
    assert 'sudo apt install librtlsdr-surely-absent' in output
    # A virtual environment takes Python modules from pip, not from Debian.
    assert 'python3-surely-absent' not in output
    user = requirements.service_user(manager.config_path)
    assert 'sudo usermod -aG plugdev %s    # dongle access' % user in output
    assert '  weectl station reconfigure --driver=user.nordic\n' in output
