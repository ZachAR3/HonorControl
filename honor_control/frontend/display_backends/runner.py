"""Subprocess helper shared by the command-line based display backends."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Sequence

log = logging.getLogger("honor_control.frontend.display_backends")

#: ``runner(argv) -> (returncode, stdout)``; replaceable in tests.
Runner = Callable[[Sequence[str]], tuple[int, str]]

COMMAND_TIMEOUT_SECONDS = 8.0
#: Return code reported when the executable does not exist.
RC_NOT_FOUND = 127


def default_runner(argv: Sequence[str]) -> tuple[int, str]:
    """Run a display tool with a fixed English locale and bounded time.

    The tools' human-readable output is parsed, so it must not be localised.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANGUAGE"] = "C"
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return RC_NOT_FOUND, ""
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("display: %s failed: %s", argv[0] if argv else "?", exc)
        return 1, ""
    return completed.returncode, completed.stdout
