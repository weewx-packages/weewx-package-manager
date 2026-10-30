"""Exercise the installed extension against the actual, unmodified weectl CLI."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import weecfg
import weecfg.extension
import weectllib
import weectl

from test_repositories import catalog, make_release, write_catalog


def test_archive_install_manage_and_remove(tmp_path):
    """An installed client must work without importing the source checkout."""
    root = Path(__file__).resolve().parents[1]
    station = tmp_path / 'station'
    config = station / 'weewx.conf'
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment['PYTHONNOUSERSITE'] = '1'

    def command(*arguments, installed=False):
        env = dict(environment)
        if installed:
            env['PYTHONPATH'] = str(station / 'bin')
        result = subprocess.run([sys.executable, '-m'] + list(arguments), cwd=tmp_path,
                                env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30)
        assert result.returncode == 0, result.stdout
        return result.stdout

    core_files = [Path(weectl.__file__)]
    for module in (weecfg, weectllib):
        core_files.extend(Path(module.__file__).parent.rglob('*.py'))
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in core_files}
    assert not hasattr(weecfg.extension.ExtensionEngine, 'install_from_installer')
    command('weectl', 'station', 'create', str(station), '--no-prompt')
    initial_config = config.read_bytes()
    # Building with the checkout as cwd does not put it on the installed CLI's path.
    subprocess.run([sys.executable, str(root / 'tools/build.py')], check=True, cwd=root)
    archive = root / 'dist/package-manager-0.1.0a2.tar.gz'
    command('weectl', 'extension', 'install', str(archive), '--config', str(config), '-y')
    assert config.read_bytes() == initial_config
    for source in (root / 'bin/user/packages').glob('*.py'):
        assert (station / 'bin/user/packages' / source.name).read_bytes() == source.read_bytes()
    command('user.packages', '--help', installed=True)
    first = make_release(tmp_path)
    index = write_catalog(tmp_path, [first])

    def manager(*arguments):
        return command('user.packages', *arguments, '--config', str(config), installed=True)

    manager('repo', 'add', 'local', str(index), '-y')
    assert 'nordic [local]' in manager('search', 'forecast')
    manager('install', 'nordic', '-y')
    target = station / 'skins/Nordic/index.html.tmpl'
    assert target.read_text() == 'first'
    second = make_release(tmp_path, '2.0', 'updated')
    index.write_text(json.dumps(catalog([first, second])))
    manager('update', 'nordic', '-y')
    assert target.read_text() == 'updated'
    manager('uninstall', 'nordic', '-y')
    assert not target.exists()
    command('weectl', 'extension', 'uninstall', 'package-manager', '--config', str(config), '-y')
    assert not list((station / 'bin/user/packages').glob('*.py'))
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in core_files}
