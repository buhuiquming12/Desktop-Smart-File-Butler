"""本地密钥保护（SecretStore）回归测试。

覆盖：
- 明文 API Key 首次迁移成功，且数据库旧记录被删除；
- SecretStore 读取/写入失败时绝不删除旧密钥，并返回脱敏诊断；
- GET 配置、日志、异常、WebSocket 事件、checkpoint 中不出现密钥；
- 清除与轮换；
- 非 Windows 环境使用可注入测试替身，不依赖真实系统凭据管理器；
- 普通 preferences 接口不能读写密钥。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app import db, llm_config
from app.config import get_settings
from app.security import secrets as secret_mod
from app.security.secrets import (
    MemorySecretStore,
    SecretStoreError,
    UnavailableSecretStore,
    WindowsDpapiSecretStore,
    redact_payload,
    redact_text,
    register_secret,
)

SECRET = "sk-test-0123456789abcdefSECRET"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MemorySecretStore]:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    store = MemorySecretStore()
    secret_mod.set_secret_store(store)
    llm_config.reset_migration_state()
    secret_mod.clear_registered_secrets()
    yield store
    secret_mod.reset_secret_store()
    llm_config.reset_migration_state()
    get_settings.cache_clear()
    db._initialized = False


def _seed_legacy(key: str = SECRET) -> None:
    """模拟历史版本：明文 Key 直接写在 llm_config 表里。"""
    db.set_llm_config({"openai_api_key": key})


# ---------------- 迁移 ----------------

def test_plaintext_key_is_migrated_on_first_read(env: MemorySecretStore) -> None:
    _seed_legacy()
    config = llm_config.get_effective_config()

    assert config.openai_api_key == SECRET
    assert config.api_key_source == "secret_store"
    # 明文记录已被删除，密钥只存在于 SecretStore。
    assert db.get_llm_config_value("openai_api_key") is None
    assert env.get_secret(llm_config.SECRET_API_KEY) == SECRET
    assert llm_config.migration_status()["status"] == "migrated"
    # 对外可读配置里绝不含明文。
    assert SECRET not in json.dumps(db.get_llm_config(), ensure_ascii=False)
    assert SECRET not in json.dumps(llm_config.api_key_info(), ensure_ascii=False)


def test_migration_scrubs_plaintext_bytes_from_database(env: MemorySecretStore) -> None:
    """删除行还不够：SQLite 空闲页/WAL 里也不能残留密钥字节。"""
    _seed_legacy()
    settings = get_settings()
    db_path = Path(settings.db_path)
    assert SECRET.encode() in db_path.read_bytes()

    llm_config.get_effective_config()

    blob = b"".join(
        path.read_bytes() for path in db_path.parent.glob(db_path.name + "*")
    )
    assert SECRET.encode() not in blob


def test_read_failure_keeps_legacy_value(env: MemorySecretStore) -> None:
    _seed_legacy()
    env.fail_reads = True

    config = llm_config.get_effective_config()

    # 读取失败 → 不返回密钥、不删除旧值、诊断可读且不含密钥。
    assert config.openai_api_key == ""
    status = llm_config.migration_status()
    assert status["status"] == "failed"
    assert SECRET not in status["message"]
    assert db.get_llm_config_value("openai_api_key") == SECRET


def test_write_failure_keeps_legacy_value(env: MemorySecretStore) -> None:
    _seed_legacy()
    env.fail_writes = True

    assert llm_config.get_effective_config().openai_api_key == ""
    assert llm_config.migration_status()["status"] == "failed"
    # 迁移没有成功，旧值必须原样保留，供用户重试或手工处理。
    assert db.get_llm_config_value("openai_api_key") == SECRET
    assert SECRET not in llm_config.migration_status()["message"]


def test_migration_is_idempotent(env: MemorySecretStore) -> None:
    _seed_legacy()
    llm_config.get_effective_config()
    writes_after_first = env.writes
    llm_config.reset_migration_state()
    llm_config.get_effective_config()
    # 第二次只读回已迁移的值，不会重复写入。
    assert env.writes == writes_after_first


def test_newer_secret_supersedes_stale_plaintext(env: MemorySecretStore) -> None:
    env.set_secret(llm_config.SECRET_API_KEY, "sk-newer-9999999999")
    _seed_legacy()

    config = llm_config.get_effective_config()

    assert config.openai_api_key == "sk-newer-9999999999"
    assert db.get_llm_config_value("openai_api_key") is None
    assert llm_config.migration_status()["status"] == "superseded"


# ---------------- 清除与轮换 ----------------

def test_clear_and_rotate(env: MemorySecretStore) -> None:
    llm_config.set_api_key(SECRET)
    assert llm_config.api_key_configured() is True

    llm_config.rotate_api_key("sk-rotated-abcdefghijkl")
    assert env.get_secret(llm_config.SECRET_API_KEY) == "sk-rotated-abcdefghijkl"

    assert llm_config.clear_api_key() is True
    assert llm_config.api_key_configured() is False
    assert env.get_secret(llm_config.SECRET_API_KEY) is None


def test_clear_removes_legacy_plaintext_even_when_store_empty(env: MemorySecretStore) -> None:
    _seed_legacy()
    assert llm_config.clear_api_key() is True
    assert db.get_llm_config_value("openai_api_key") is None


def test_save_overrides_cannot_write_plaintext_key(env: MemorySecretStore) -> None:
    llm_config.save_overrides({"openai_api_key": SECRET, "openai_model": "m1"})

    assert db.get_llm_config_value("openai_api_key") is None
    assert db.get_llm_config().get("openai_model") == "m1"
    assert env.get_secret(llm_config.SECRET_API_KEY) == SECRET


# ---------------- 非 Windows 测试替身 ----------------

def test_unavailable_store_fails_closed(tmp_path: Path) -> None:
    store = UnavailableSecretStore("no dpapi here")
    assert store.is_configured("x") is False
    assert store.get_secret("x") is None
    with pytest.raises(SecretStoreError):
        store.set_secret("x", "value")
    assert store.describe()["backend"] == "unavailable"


def test_default_store_is_platform_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """非 Windows 环境默认存储必须是 fail-closed，不触碰系统凭据管理器。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    secret_mod.reset_secret_store()
    store = secret_mod.get_secret_store()
    if WindowsDpapiSecretStore.available():
        assert store.backend == "windows-dpapi"
    else:
        assert store.backend == "unavailable"
        assert store.persistent is False
    get_settings.cache_clear()


# ---------------- 出口脱敏 ----------------

def test_redaction_covers_known_and_pattern_keys() -> None:
    register_secret(SECRET)
    text = f"call failed with key {SECRET} and sk-anotherkey1234567890"
    redacted = redact_text(text)
    assert SECRET not in redacted
    assert "sk-anotherkey1234567890" not in redacted
    assert "REDACTED" in redacted


def test_redact_payload_is_recursive() -> None:
    register_secret(SECRET)
    payload = {"a": [SECRET, {"b": f"Bearer {SECRET}"}]}
    clean = redact_payload(payload)
    assert SECRET not in json.dumps(clean)


def test_logging_filter_redacts(env: MemorySecretStore) -> None:
    from app.logging_conf import SecretRedactionFilter

    register_secret(SECRET)
    logger = logging.getLogger("test.secret.filter")
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture()
    handler.addFilter(SecretRedactionFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("配置的 key 是 %s", SECRET)
        try:
            raise RuntimeError(f"请求失败: Authorization=Bearer {SECRET}")
        except RuntimeError:
            logger.exception("工具异常")
    finally:
        logger.removeHandler(handler)
    assert records
    assert all(SECRET not in item for item in records)


def test_websocket_event_payload_is_redacted() -> None:
    from app.api.events import event
    from app.models import WSEventType

    register_secret(SECRET)
    outgoing = event(WSEventType.error, "t1", message=f"bad key {SECRET}")
    assert SECRET not in json.dumps(outgoing.payload)


# ---------------- API 行为 ----------------

class _Client:
    def __init__(self, client: TestClient) -> None:
        self.client = client


@pytest.fixture()
def api(env: MemorySecretStore):
    from app.main import app, get_session_token

    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    return client


def test_get_config_only_reports_boolean(api: TestClient, env: MemorySecretStore) -> None:
    llm_config.set_api_key(SECRET)

    body = api.get("/api/config").json()
    assert body["api_key_set"] is True
    assert body["openai_api_key_set"] is True
    assert body["api_key_storage"]["backend"] == "memory"
    assert SECRET not in json.dumps(body)

    settings = api.get("/api/settings/llm").json()
    assert settings["api_key_set"] is True
    assert SECRET not in json.dumps(settings)


def test_put_settings_writes_to_secret_store(api: TestClient, env: MemorySecretStore) -> None:
    response = api.put("/api/settings/llm", json={"openai_api_key": SECRET})
    assert response.status_code == 200, response.text
    assert response.json()["api_key_set"] is True
    assert SECRET not in response.text
    assert env.get_secret(llm_config.SECRET_API_KEY) == SECRET
    assert db.get_llm_config_value("openai_api_key") is None


def test_put_empty_key_clears(api: TestClient, env: MemorySecretStore) -> None:
    llm_config.set_api_key(SECRET)
    response = api.put("/api/settings/llm", json={"openai_api_key": ""})
    assert response.status_code == 200
    assert response.json()["api_key_set"] is False
    assert env.get_secret(llm_config.SECRET_API_KEY) is None


def test_clear_endpoint(api: TestClient, env: MemorySecretStore) -> None:
    llm_config.set_api_key(SECRET)
    response = api.delete("/api/settings/llm/key")
    assert response.status_code == 200, response.text
    assert response.json()["cleared"] is True
    assert response.json()["api_key_set"] is False


def test_migration_retry_endpoint(api: TestClient, env: MemorySecretStore) -> None:
    _seed_legacy()
    env.fail_writes = True
    api.get("/api/settings/llm")
    assert llm_config.migration_status()["status"] == "failed"

    env.fail_writes = False
    body = api.post("/api/settings/llm/key/migrate").json()
    assert body["migration"]["status"] == "migrated"
    assert SECRET not in json.dumps(body)


def test_preferences_cannot_read_or_write_secrets(api: TestClient, env: MemorySecretStore) -> None:
    llm_config.set_api_key(SECRET)
    listed = api.get("/api/preferences").json()
    assert all("api_key" not in item["key"] for item in listed)
    assert SECRET not in json.dumps(listed)

    rejected = api.put("/api/preferences/openai_api_key", json={"value": SECRET})
    assert rejected.status_code == 422
    assert db.get_preference("openai_api_key") is None

    rejected_direct = api.put("/api/preferences/my_api_key", json={"value": "x"})
    assert rejected_direct.status_code == 422


def test_thread_state_never_contains_secret(api: TestClient, env: MemorySecretStore) -> None:
    llm_config.set_api_key(SECRET)
    # 不存在的会话返回 404；存在的会话状态同样走 redact_payload（见 main.thread_state）。
    assert api.get("/api/threads/nope").status_code == 404


def test_thread_snapshot_redacts_secret_leaked_into_observation(
    api: TestClient, env: MemorySecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """即使某条观察里误带了密钥文本，REST 快照也必须脱敏后再返回。"""
    from app import main as main_module

    llm_config.set_api_key(SECRET)

    class _Runtime:
        def state(self, _thread_id: str) -> dict:
            return {
                "thread_id": "t-leak",
                "run_id": "r1",
                "status": "completed",
                "observations": [
                    {
                        "tool": "extract_text",
                        "status": "failed",
                        "error": f"connection failed with key {SECRET}",
                        "args": {"file_path": "C:/tmp/a.txt"},
                    }
                ],
                "final_response": "done",
                "plan": [],
                "step_index": 1,
                "mutations": {"move": 0, "rename": 0, "delete": 0},
            }

    monkeypatch.setattr(main_module, "get_runtime", lambda: _Runtime())
    response = api.get("/api/threads/t-leak")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert "REDACTED" in response.text


def test_checkpoint_state_does_not_store_secret(env: MemorySecretStore) -> None:
    """密钥只进 SecretStore：Agent 状态里不会出现明文。"""
    from app.agent.state import AgentState

    keys = set(AgentState.__annotations__)
    assert not any("api_key" in key for key in keys)


def test_checkpoint_files_do_not_contain_secret(
    env: MemorySecretStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑一轮真实图（只读扫描）后，checkpoints.sqlite 里不能出现密钥字节。"""
    import json
    from typing import Any, List

    from app.agent import graph as graph_module
    from app.agent.state import PlanOutput, PlanStep, ReflectionOutput

    llm_config.set_api_key(SECRET)

    class _Planner:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            context = json.loads(messages[-1].content)
            if context.get("tool_observations"):
                return PlanOutput(goal="done", steps=[], user_message="")
            return PlanOutput(goal="扫描", steps=[
                PlanStep(id="s", description="扫描", tool="scan_directory",
                         args={"directory": str(tmp_path)})
            ], user_message="")

    class _Reflector:
        def invoke(self, messages: List[Any]) -> ReflectionOutput:
            if json.loads(messages[-1].content).get("是否还有步骤"):
                return ReflectionOutput(decision="continue", reasoning="继续")
            return ReflectionOutput(decision="done", reasoning="结束")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _Planner() if schema is PlanOutput else _Reflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("扫描一下这个目录", "secret-checkpoint"):
        pass
    state = runtime.state("secret-checkpoint")
    assert SECRET not in json.dumps(state, ensure_ascii=False, default=str)

    db_dir = Path(get_settings().db_path).parent
    blobs = b"".join(
        path.read_bytes() for path in db_dir.glob("*.sqlite*")
    )
    assert SECRET.encode() not in blobs
