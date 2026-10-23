# Copyright 2026 weewx-packages contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a deterministic WeeWX extension archive from the complete project."""

import gzip
import hashlib
import io
from pathlib import Path
import tarfile


def build():
    """Write the archive and its SHA-256 beside it under dist.

    Returns:
        Path: The generated archive.
    """
    root = Path(__file__).resolve().parents[1]
    version = '0.1.0a1'
    destination = root / 'dist' / ('package-manager-%s.tar.gz' % version)
    destination.parent.mkdir(exist_ok=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        files = [root / name for name in (
            'install.py', 'README.md', 'LICENSE.txt', 'CLAUDE.md', 'AGENTS.md',
            'pyproject.toml', 'justfile')]
        for directory in ('bin', 'tests', 'tools', 'docs'):
            files.extend(path for path in (root / directory).rglob('*')
                         if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
        for path in sorted(files):
            content = path.read_bytes()
            info = tarfile.TarInfo('package-manager/' + path.relative_to(root).as_posix())
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    with destination.open('wb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed:
            compressed.write(buffer.getvalue())
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + '.sha256').write_text(
        checksum + '  ' + destination.name + '\n')
    return destination


if __name__ == '__main__':
    print(build())
