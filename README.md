# WeeWX package manager

Install, search and update WeeWX extensions from repository indexes of your
choice. This extension contains the repository client previously developed as
a WeeWX core change. It installs under the station's `bin/user` directory.

This is an experimental standalone extraction. The tested base is unmodified
WeeWX 5.5.2 with Python 3.10 and 3.14. There is no published release yet, and the manager is not in the shared catalog.
The local installer version is `0.1.0a1`.

## Install

Use the Python environment in which WeeWX is installed. Install the `packaging`
dependency there. On Debian or Raspberry Pi OS with a system installation, use
`sudo apt install python3-packaging`. For a WeeWX virtual environment, use
`python -m pip install 'packaging>=21'` inside that environment.

Build the extension archive from this checkout and install it:

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

Alternatively, `python -m pip install .` installs the `weectl-packages` command
in the active Python environment. Choose either the WeeWX extension install or
the Python package install. Avoid mixing their ownership of the same modules.

## Operations

Use `python -m user.packages --help` for all actions and append `--help` to an
action for its options. `--pre` permits prereleases. `--version VERSION` selects
an exact package version. Put custom installer arguments after `--`.

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

```shell
python -m pip install -e '.[test]'
python -m pytest -q tests/test_repositories.py
```

The extraction retains the prototype's regression tests for source binding,
checksum verification, updates, local edits, ownership and interrupted writes.
Input hashes and provenance are in [docs/origin.json](docs/origin.json).

Released under the [GNU GPL](LICENSE.txt), preserving the original notices.
