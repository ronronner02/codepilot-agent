"""配置加载的失败路径。缺必需变量时要指出缺哪一项，而不是笼统报错。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "deepseek_api_key": "test-key",
        "_env_file": None,  # 隔离宿主机 .env，避免测试受本地配置影响
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_missing_deepseek_key_names_the_field() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert "deepseek_api_key" in str(exc.value)


def test_api_embedding_provider_requires_its_key() -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(embedding_provider="api", embedding_api_key="")
    assert "EMBEDDING_API_KEY" in str(exc.value)


def test_local_embedding_provider_does_not_require_api_key() -> None:
    settings = _settings(embedding_provider="local", embedding_api_key="")
    assert settings.embedding_provider == "local"


def test_workspace_subdirs_derive_from_root() -> None:
    settings = _settings(workspace_root="/tmp/ws")
    assert settings.repos_dir.parts[-1] == "repos"
    assert settings.index_dir.parts[-1] == "index"
    assert settings.repos_dir.parent == settings.workspace_root
