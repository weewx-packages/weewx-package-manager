#
#    Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
#
#    See the file LICENSE.txt for your rights.
#
"""Check what a release needs outside its archive, and say how to provide it.

A catalog release can name Python requirements, Debian packages and setup
steps. The archive itself stays a plain WeeWX extension. Before installation the
manager checks the requirements in the Python running WeeWX: in a virtual
environment it installs missing Python requirements with pip after
confirmation. Debian packages need root, so the manager prints the apt command
instead of running it. After installation it prints the setup steps.
"""

import importlib.metadata
import os
import pwd
import re
import shutil
import subprocess
import sys

import packaging.requirements

from user.packages import repository

DEBIAN_PACKAGE = re.compile(r'[a-z0-9][a-z0-9+.-]{0,99}')
# Setup commands may name the account that runs WeeWX, which differs between a
# package installation and a pip installation.
PLACEHOLDER = re.compile(r'\{[^}]*\}')
PLACEHOLDERS = ('{user}',)


def validate(release):
    """Check the requirements and setup fields of a catalog release.

    Args:
        release (dict): Release from a catalog. It is changed in place: Python
            requirements are normalized.

    Raises:
        repository.RepositoryError: A field is malformed.
    """
    if 'requirements' in release:
        needs = release['requirements']
        repository._object(needs, (), ('python', 'debian'), 'release requirements')
        python = needs.get('python', [])
        if not isinstance(python, list) or len(python) > 32:
            raise repository.RepositoryError('Invalid Python requirements.')
        normalized = []
        for item in python:
            repository._text(item, 'Python requirement', 256)
            try:
                normalized.append(str(packaging.requirements.Requirement(item)))
            except packaging.requirements.InvalidRequirement as error:
                raise repository.RepositoryError('Invalid Python requirement: %s' % error)
        if 'python' in needs:
            needs['python'] = normalized
        debian = needs.get('debian', [])
        if not isinstance(debian, list) or len(debian) > 32:
            raise repository.RepositoryError('Invalid Debian packages.')
        for item in debian:
            repository._text(item, 'Debian package', 100, DEBIAN_PACKAGE)
    if 'setup' in release:
        steps = release['setup']
        if not isinstance(steps, list) or not 0 < len(steps) <= 16:
            raise repository.RepositoryError('Invalid setup steps.')
        for step in steps:
            repository._object(step, ('command',), ('note',), 'setup step')
            repository._text(step['command'], 'setup command', 512)
            if any(name not in PLACEHOLDERS for name in PLACEHOLDER.findall(step['command'])):
                raise repository.RepositoryError('Unknown placeholder in setup command.')
            if 'note' in step:
                repository._text(step['note'], 'setup note', 256)


def in_virtual_environment():
    """Return True when WeeWX runs in a virtual environment rather than system Python."""
    return sys.prefix != getattr(sys, 'base_prefix', sys.prefix)


def missing_python(release):
    """Return the Python requirements of a release that this interpreter lacks.

    Args:
        release (dict): Validated catalog release.

    Returns:
        list[str]: Requirements whose distribution is absent or has another version.
    """
    missing = []
    for item in release.get('requirements', {}).get('python', []):
        requirement = packaging.requirements.Requirement(item)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            version = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(item)
            continue
        if requirement.specifier and not requirement.specifier.contains(version, prereleases=True):
            missing.append(item)
    return missing


def debian_packages(release):
    """Return the Debian packages of a release that this Python needs.

    Debian packages named python3-* are the distribution's builds of Python
    requirements. A virtual environment gets those from pip instead.

    Args:
        release (dict): Validated catalog release.

    Returns:
        list[str]: Package names.
    """
    packages = release.get('requirements', {}).get('debian', [])
    if in_virtual_environment():
        return [name for name in packages if not name.startswith('python3-')]
    return packages


def missing_debian(release):
    """Return the Debian packages this Python needs that are not installed.

    Args:
        release (dict): Validated catalog release.

    Returns:
        list[str]|None: Missing packages, or None on a system without dpkg.
    """
    packages = debian_packages(release)
    if not packages:
        return []
    if shutil.which('dpkg-query') is None:
        return None
    missing = []
    for name in packages:
        result = subprocess.run(['dpkg-query', '-W', '-f', '${db:Status-Status}', name],
                                capture_output=True, text=True)
        if result.returncode != 0 or result.stdout.strip() != 'installed':
            missing.append(name)
    return missing


def service_user(config_path):
    """Return the account that most likely runs WeeWX for this configuration.

    A pip installation runs as the owner of its configuration. A package
    installation keeps its configuration under root and runs as weewx.

    Args:
        config_path (str): Path to weewx.conf.

    Returns:
        str: Account name.
    """
    owner = pwd.getpwuid(os.stat(config_path).st_uid).pw_name
    if owner != 'root':
        return owner
    try:
        return pwd.getpwnam('weewx').pw_name
    except KeyError:
        return owner


def prepare(release, yes=False, dry_run=False):
    """Report missing requirements and install Python ones where that is safe.

    In a virtual environment, missing Python requirements are installed with
    the interpreter's pip after confirmation. In system Python, pip would
    conflict with the distribution, so their Debian packages are named instead.
    Installation of the extension continues either way: its files do not
    depend on the requirements, and they can be installed afterwards.

    Args:
        release (dict): Validated catalog release.
        yes (bool): Confirm pip installation without input.
        dry_run (bool): Only report.
    """
    python = missing_python(release)
    if python and in_virtual_environment():
        command = [sys.executable, '-m', 'pip', 'install'] + python
        print('Missing Python requirements: ' + ', '.join(python))
        if dry_run:
            print('Dry run: would run ' + ' '.join(command))
        else:
            repository.confirm('Install them with pip into this environment?', yes)
            subprocess.run(command, check=True)
        python = []
    debian = missing_debian(release)
    if python and not release.get('requirements', {}).get('debian'):
        print('Missing Python requirements: %s. Install them with your system\'s '
              'package manager.' % ', '.join(python))
    if debian:
        print('Missing system packages. Install them with:')
        print('  sudo apt install ' + ' '.join(debian))
    elif debian is None and python:
        print('Missing requirements: %s. Install the matching packages of your system.'
              % ', '.join(python))


def next_steps(release, config_path):
    """Print the setup steps of an installed release, with the WeeWX account filled in.

    Args:
        release (dict): Validated catalog release.
        config_path (str): Path to weewx.conf, used to find the WeeWX account.
    """
    steps = release.get('setup', [])
    if not steps:
        return
    user = service_user(config_path)
    print('Next steps:')
    for step in steps:
        command = step['command'].replace('{user}', user)
        # Catalogs write the steps for a package installation. In a virtual
        # environment, weectl is the environment's own and runs without sudo,
        # which would not find it.
        if in_virtual_environment() and command.startswith('sudo weectl '):
            command = command[len('sudo '):]
        line = '  ' + command
        if step.get('note'):
            line += '    # ' + step['note']
        print(line)


def report(release, config_path):
    """Print whether each requirement of an installed release is met.

    Args:
        release (dict): Validated catalog release.
        config_path (str): Path to weewx.conf.

    Returns:
        bool: True if nothing is missing.
    """
    needs = release.get('requirements', {})
    python = missing_python(release)
    debian = missing_debian(release)
    for item in needs.get('python', []):
        print('Python %s: %s' % (item, 'missing' if item in python else 'ok'))
    for name in debian_packages(release):
        state = 'unknown, no dpkg' if debian is None else 'missing' if name in debian else 'ok'
        print('Debian %s: %s' % (name, state))
    if not needs:
        print('No requirements listed.')
    next_steps(release, config_path)
    return not python and not debian
