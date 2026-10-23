# WeeWX package manager

Install, search and update WeeWX extensions from repository indexes of your
choice. This extension contains the repository client previously developed as
a WeeWX core change. It installs under the station's `bin/user` directory.

This is an experimental standalone extraction. The tested base is unmodified
WeeWX 5.5.2 with Python 3.10 and 3.14. There is no published release yet, and the manager is not in the shared catalog.
The local installer version is `0.1.0a1`.

## Install

Start with WeeWX 5.5.2 installed in its Python environment and Git available.
Activate that WeeWX virtual environment before running the commands, so
`python` and `weectl` use the same environment. For a system installation,
install `packaging>=21` through your distribution in WeeWX's Python environment
instead of using pip. On Debian or Raspberry Pi OS the package is
`python3-packaging`.

This project has no release download yet. Obtain the source before building,
starting in any empty working directory:

```shell
git clone https://github.com/weewx-packages/weewx-package-manager.git
cd weewx-package-manager
```

For the virtual environment, install the runtime dependency:

```shell
python -m pip install 'packaging>=21'
```

The archive builder uses only Python's standard library. It does not need
pytest or a Python package build backend. Replace `/path/to/weewx.conf` with
your existing station configuration, then build and install the archive from
the checkout directory:

```shell
python tools/build.py
weectl extension install dist/package-manager-0.1.0a1.tar.gz --config /path/to/weewx.conf
```

For a station at `/home/weewx/weewx-data`, run the installed manager as follows.
Replace the station path and use the same Python interpreter as WeeWX:

```shell
export PYTHONPATH=/home/weewx/weewx-data/bin
python -m user.packages repo add packages https://raw.githubusercontent.com/weewx-packages/catalog/main/repository.json --config /path/to/weewx.conf
python -m user.packages search ecowitt --config /path/to/weewx.conf
python -m user.packages install livedata --config /path/to/weewx.conf
python -m user.packages check-updates --config /path/to/weewx.conf
python -m user.packages update --config /path/to/weewx.conf
```

The catalog above is an example. You can register multiple repository URLs.
When the same package ID exists in several repositories, choose its source
interactively or use `--repo ALIAS`. Updates retain that source binding.

For a shorter command in your shell:

```shell
alias weectl-packages='python -m user.packages'
weectl-packages search skin --config /path/to/weewx.conf
```

Alternatively, after the clone and `cd` steps above, `python -m pip install .`
from the checkout installs the `weectl-packages` command in the activated WeeWX
virtual environment. Pip installs the runtime dependencies from `pyproject.toml`
and obtains `setuptools>=68` in an isolated build environment. Choose either
the WeeWX extension install or the Python package install. Avoid mixing their
ownership of the same modules.

## Operations

Use `python -m user.packages --help` for all actions and append `--help` to an
action for its options. `--pre` permits prereleases. `--version VERSION` selects
an exact package version. Put custom installer arguments after `--`.

`search --category CATEGORY` lists one kind of package: `skin`, `driver`,
`uploader`, `forecast` or `other`. Every package in an index declares exactly
one category.

Mutating actions support `--dry-run`. `--yes` confirms an operation but does not
choose a source or overwrite local edits. `recover` handles interrupted
transactions. Repository definitions and existing installation records use the
same format as the core prototype.

Updates are manual. No background service or schedule is installed.

## Integration with weectl

Stock WeeWX loads `weectl` commands from a fixed list. This extension uses its
own CLI and does not replace `weecfg`, `weectllib` or `weectl`. Stock WeeWX
5.5.2 installs extension archive URLs, files or directories with
`weectl extension install`; it has no repository or package-ID commands.
Use `python -m user.packages` for the repository operations shown here.

The private installer adapter follows WeeWX's installer sequence while exposing
the write hooks required by the transaction journal. Future core changes to
that sequence need a compatibility review.

Package installers execute Python with the permissions of the invoking user.
Register sources whose publishers you trust.

## Development

Use the source checkout obtained above and activate a separate development
virtual environment. Run these commands from the `weewx-package-manager`
directory. The `test` extra installs pytest for development; normal runtime
installation does not require it. Pip supplies the build backend automatically.

```shell
python -m pip install -e '.[test]'
python -m pytest -q tests/test_repositories.py
```

The extraction retains the prototype's regression tests for source binding,
checksum verification, updates, local edits, ownership and interrupted writes.
Input hashes and provenance are in [docs/origin.json](docs/origin.json).

Released under the [GNU GPL](LICENSE.txt), preserving the original notices.
