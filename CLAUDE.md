# WeeWX package manager

An extension containing the repository client extracted from the WeeWX prototype.
Alpha. No public release yet.

## Commands

- `just fast`: compile and run the extracted regression tests plus archive smoke test, about 5 seconds.
- `just build`: deterministic extension archive under `dist`.

Activate a WeeWX environment with pytest and packaging before running them.

## Layout

- `bin/user/packages`: standalone repository client and CLI.
- `install.py`: normal WeeWX extension installer, files only.
- `tests/test_repositories.py`: repository regression tests retained from the prototype.
- `docs/origin.json`: hashes of the uncommitted extraction inputs.

## Conventions

Use the existing WeeWX style, without runtime type annotations. Describe types
in Google-style docstrings. Preserve copyright headers and LICENSE.txt.
Do not replace core files or shadow the normal weectl command.

The private InstallerEngine retains the core's installer order and exposes the
write hooks needed for managed transactions. Compare it with upstream when
updating the supported WeeWX version range.

## Releases

Bump the version in `install.py`, `pyproject.toml` and `tools/build.py`, run
`just build`, then create the release from the merged main branch:
`gh release create vVERSION dist/package-manager-VERSION.tar.gz dist/package-manager.tar.gz --latest`.
Do not mark it as a GitHub prerelease, even for alpha versions: the README and
the catalog install the manager through `releases/latest/download/package-manager.tar.gz`,
which skips prereleases.
