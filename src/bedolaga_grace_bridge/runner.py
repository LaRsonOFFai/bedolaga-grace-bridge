from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        super().__init__(f"Команда завершилась ошибкой: {message}")
        self.result = result


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 120,
    check: bool = True,
) -> CommandResult:
    completed = subprocess.run(
        [str(item) for item in args],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    result = CommandResult(
        tuple(str(item) for item in args), completed.returncode, completed.stdout, completed.stderr
    )
    if check and not result.ok:
        raise CommandError(result)
    return result
