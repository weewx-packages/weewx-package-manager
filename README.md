# WeeWX package manager

Install, search and update WeeWX extensions from repository indexes of your
choice. The manager is itself a WeeWX extension and installs under the
station's `bin/user` directory.

It is experimental. The tested base is unmodified WeeWX 5.5.2 with Python
3.10 and 3.14. The manager is not listed in the shared catalog.

## Install

Activate the Python environment running WeeWX, so `python` and `weectl` use
the same environment. Install the `packaging` dependency there. For a virtual
environment:

```shell
python -m pip install 'packaging>=21'
```

For a system installation, use your distribution's package instead. On Debian
or Raspberry Pi OS it is `python3-packaging`.

Then install the newest release. Replace `/path/to/weewx.conf` with your
station configuration:

```shell
weectl extension install https://github.com/weewx-packages/weewx-package-manager/releases/latest/download/package-manager.tar.gz --config /path/to/weewx.conf
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
transactions.

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

`python tools/build.py` writes the archive under `dist`. Each GitHub release
carries the versioned archive and the same bytes as `package-manager.tar.gz`,
which the install command above fetches.

Clone the repository and activate a separate development virtual environment.
Run these commands from the checkout. The `test` extra installs pytest for development; normal runtime
installation does not require it. Pip supplies the build backend automatically.

```shell
git clone https://github.com/weewx-packages/weewx-package-manager.git
cd weewx-package-manager
python -m pip install -e '.[test]'
python -m pytest -q tests/test_repositories.py
```

The extraction retains the prototype's regression tests for source binding,
checksum verification, updates, local edits, ownership and interrupted writes.
Input hashes and provenance are in [docs/origin.json](docs/origin.json).

Released under the [GNU GPL](LICENSE.txt), preserving the original notices.
