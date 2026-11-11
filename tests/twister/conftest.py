import sys
import textwrap
from pathlib import Path

import pytest

FAKE_TWISTER = Path(__file__).parent / "fake_twister.py"


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


@pytest.fixture
def test_tree(tmp_path: Path) -> Path:
    """A small synthetic tests/ tree with two test dirs and three scenarios."""
    root = tmp_path / "tests"
    write(
        root / "lib" / "alpha" / "testcase.yaml",
        """
        common:
          tags: lib alpha
          integration_platforms:
            - native_sim
        tests:
          lib.alpha.default: {}
          lib.alpha.variant:
            build_only: true
            platform_allow: native_sim qemu_x86
            tags: [variant]
        """,
    )
    write(
        root / "lib" / "alpha" / "src" / "main.c",
        """
        #include <zephyr/ztest.h>
        ZTEST(alpha_suite, test_adds)
        {
        }
        ZTEST_F(alpha_suite, test_with_fixture) {}
        ZTEST_USER(alpha_user, plain_name) {}
        ZTEST_SUITE(alpha_suite, NULL, NULL, NULL, NULL, NULL);
        """,
    )
    write(
        root / "drivers" / "beta" / "testcase.yaml",
        """
        tests:
          drivers.beta:
            platform_allow:
              - native_sim
        """,
    )
    write(root / "drivers" / "beta" / "src" / "test.c", "ZTEST(beta_suite, test_one) {}\n")
    write(root / "broken" / "testcase.yaml", "tests: [not, a, mapping]\n")
    # Must be ignored by discovery:
    write(root / "twister-out" / "x" / "testcase.yaml", "tests:\n  bogus.scenario: {}\n")
    return root


@pytest.fixture
def fake_twister() -> list[str]:
    return [sys.executable, str(FAKE_TWISTER)]
