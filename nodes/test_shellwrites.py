"""Regression tests for shell write-target detection."""
import pytest

from nodes.shellwrites import write_targets


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat <<'EOF' > pkg/m.py\nx = 1\nEOF", ["pkg/m.py"]),
        ("cat <<EOF >> pkg/m.py\nx = 1\nEOF", ["pkg/m.py"]),
        ("cat <<'EOF' | tee pkg/m.py\nx = 1\nEOF", ["pkg/m.py"]),
    ],
)
def test_heredoc_keeps_trailing_write_target(command, expected):
    assert write_targets(command) == (expected, False)
