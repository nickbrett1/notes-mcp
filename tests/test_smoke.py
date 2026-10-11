"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import notes_mcp

    assert notes_mcp.__version__
