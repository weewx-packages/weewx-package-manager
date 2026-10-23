# Copyright 2026 weewx-packages contributors
# See LICENSE.txt for your rights.
"""Run package operations with the station's bin directory on PYTHONPATH."""

import argparse
import sys

from user.packages import cli


def main():
    """Parse manager options without consuming options intended for an installer."""
    parser = argparse.ArgumentParser(description='Manage WeeWX packages and repositories.')
    cli.add_arguments(parser)
    arguments = sys.argv[1:]
    extra = []
    if (arguments and arguments[0] in ('install', 'update', 'switch-source')
            and '--' in arguments):
        separator = arguments.index('--')
        extra = arguments[separator + 1:]
        arguments = arguments[:separator]
    namespace, unknown = parser.parse_known_args(arguments)
    if not getattr(namespace, 'action', None):
        parser.print_help()
        return
    if unknown and namespace.action not in ('install', 'update', 'switch-source'):
        parser.error('unrecognized arguments: ' + ' '.join(unknown))
    if namespace.action == 'repo' and not getattr(namespace, 'repo_action', None):
        namespace.func(namespace)
    else:
        cli.run(namespace, unknown + extra)


if __name__ == '__main__':
    main()
