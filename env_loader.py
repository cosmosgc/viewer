import os
from pathlib import Path


_LOADED_ENV_FILES = set()


def _strip_wrapping_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_env_file(env_path=None, override=False):
    path = Path(env_path) if env_path else Path(__file__).resolve().parent / ".env"
    path = path.resolve()
    cache_key = (str(path), override)
    if cache_key in _LOADED_ENV_FILES or not path.exists():
        return path

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_wrapping_quotes(value.strip())
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value

    _LOADED_ENV_FILES.add(cache_key)
    return path
