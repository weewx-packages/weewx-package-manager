fast:
    python -m compileall -q bin install.py tools
    python -m pytest -q tests/test_repositories.py tests/test_standalone.py

build:
    python tools/build.py
