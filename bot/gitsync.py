"""Сохранение состояния обратно в репозиторий.

Бот живёт внутри job-а GitHub Actions: файловая система умрёт вместе с ним,
поэтому единственное надёжное хранилище — сам репозиторий.

Стратегия «наша версия побеждает»: единственный писатель state.json — бот,
поэтому вместо слияния мы просто перематываемся на текущий origin и кладём
свой файл сверху. Конфликтов не бывает по построению, а код и прочие файлы,
которые ты меняешь руками, при этом не затираются.
"""

from __future__ import annotations

import os
import subprocess

STATE_FILE = "data/state.json"


def enabled() -> bool:
    return os.environ.get("REPITER_GIT_SYNC") == "1"


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
    )


def _configure() -> None:
    _git("config", "user.name", "repiter-bot")
    _git(
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    )


def push_state(message: str | None = None) -> bool:
    """Коммитит и пушит state.json. Возвращает True, если что-то улетело.

    Любая ошибка гасится: не смогли запушить — не беда, попробуем через
    полминуты. Ронять из-за этого бота нельзя.
    """
    if not enabled():
        return False

    branch = os.environ.get("GITHUB_REF_NAME", "main")
    try:
        _configure()

        # Есть ли вообще что коммитить?
        _git("add", STATE_FILE)
        if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False

        # Перематываемся на актуальный origin, сохраняя рабочую копию.
        if _git("fetch", "origin", branch, check=False).returncode == 0:
            _git("reset", "--mixed", "FETCH_HEAD", check=False)

        _git("add", STATE_FILE)
        if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False

        _git("commit", "-m", message or "state")
        push = _git("push", "origin", f"HEAD:{branch}", check=False)
        if push.returncode != 0:
            print(f"[git] push не удался: {push.stderr.strip()[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[git] не смогли сохранить состояние: {e}")
        return False
