"""OpenClaw runtime：通过 CLI 非侵入接入（AGENTS.md §4.1、§17.2）。

**不 fork、不改 OpenClaw 核心** —— 只调 `openclaw agent --local --json`，
把它的输出归一成标准 RunResult。

两种模式：
  routing_only  把 skill 目录拼进 prompt，要 JSON 回来（和 litellm 一样的判定，但走 agent loop）
  full          直接下发任务，让 OpenClaw 自己发现/加载 skill、调 tool（§18.2）

OpenClaw 用自己的 auth store（`~/.openclaw/agents/*/agent/auth-profiles.json`），
**不读环境变量里的 API key** —— healthcheck 会把这件事说清楚。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

from filelock import FileLock

from contracts import (
    TEXT_EXCERPT_LIMIT,
    Artifact,
    InvocationRequest,
    RunResult,
    RuntimeCapabilities,
    RuntimeHealth,
    ToolCall,
    TrajectoryEvent,
)

from adapters.routing_inputs import create_routing_input

from . import register
from .base import BaseRuntimeAdapter, classify_error_text_subkind
from .litellm import parse_selection

# OpenClaw 启动时会做 bedrock 模型发现：先读环境变量，再回落到 ~/.aws/credentials。
# 本项目不走 bedrock，凭据探测失败只会往 stderr 刷 AccessDenied 噪音（还拖慢启动）。
# 所以既摘环境变量，也把 AWS 的配置文件路径指向 /dev/null，让它干脆地探不到。
_STRIP_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
              "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_REGION")
_FORCE_ENV = {"AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
              "AWS_CONFIG_FILE": "/dev/null",
              "AWS_EC2_METADATA_DISABLED": "true"}

# OpenClaw 扫描额外 skill 目录的配置键（优先级低于 bundled/plugin skill）
_EXTRA_DIRS = "skills.load.extraDirs"
# agent 的默认工作目录；full 模式下每个请求换成独立临时目录（§11.2 isolated）
_WORKSPACE = "agents.defaults.workspace"
# 主模型。容器每次都是全新 profile，不显式设就会落到镜像默认模型上
_MODEL = "agents.defaults.model.primary"
# suite.tools 必须成为 OpenClaw 的硬权限边界，而不是只用于事后评分。
_TOOLS_ALLOW = "tools.allow"
_TOOLS_DENY = "tools.deny"
_TOOL_POLICY_VERSION = "suite-tools-v1"

# OpenClaw 把上游 API 的失败转述到 stderr，退出码一律非 0 —— 只看退出码分不出
# 「CLI 自己挂了」和「模型服务连不上」。靠这些词把后者拨回 network。
_NETWORK_STDERR = ("econnrefused", "enotfound", "etimedout", "socket hang up",
                   "network", "fetch failed", "rate limit", "429",
                   "502", "503", "504", "timeout")


def _looks_like_network(stderr: str) -> bool:
    low = stderr.lower()
    return any(k in low for k in _NETWORK_STDERR)


def _safe_session_id(session: str) -> str:
    """把 session id 收敛成 OpenClaw 认的字符集。

    AUTHORING.md §1.3 允许 id 里出现 `+`（多 skill 题写成 `pdf+xlsx-multi-01`），
    但 OpenClaw 见到 `+` 直接 `Invalid session ID` 退出码 1。这是 runtime 自己的
    约束，不该反向绑架命名规范 —— 所以在 adapter 这层翻译（§17.3）。

    实际最先咬到的不是 case id，是 `execution_id` 里时区偏移的 `+0200`：
    healthcheck 用的是写死的 `skilleval-health`，探得通；真跑起来每题必挂。
    **healthcheck 过了不等于跑得动** —— 探针要用真实格式的 id。
    """
    # ponytail: `+` → `-` 理论上能撞（a+b 和 a-b 同名）。真实 id 里带 execution_id
    # 时间戳，撞不上；哪天真撞了再改成保留原串的编码。
    return re.sub(r"[^A-Za-z0-9._-]", "-", session)


# OpenClaw 进一个空 workspace 会先自己铺一套脚手架文件。它们是 runtime 的状态，
# 不是本次 run 的产物 —— 不排掉的话每个 run 都会凭空多出 8 个 artifact，
# 产物命中率的分母就成了假的。
# ponytail: 写死文件名清单，只排根目录那一层（out/AGENTS.md 仍算产物）。
# 换 OpenClaw 大版本后若 artifacts 里冒出没见过的根文件，回来补这张表。
_BOOTSTRAP_FILES = frozenset({
    "AGENTS.md", "BOOTSTRAP.md", "HEARTBEAT.md", "IDENTITY.md", "SOUL.md",
    "TOOLS.md", "USER.md", "openclaw-workspace-state.json",
})

# `--json` 的 meta 只有聚合 toolSummary，但 OpenClaw 自己把逐次调用写进了会话
# JSONL（`meta.agentMeta.sessionFile`）：assistant 消息里带 toolCall block
# （id + name + arguments），toolResult 消息里带 toolCallId/toolName/isError。
# 这就是 exact 级证据，不需要包 tool dispatch，也不需要改 OpenClaw。
_TOOL_CALL_BLOCKS = frozenset({"toolcall", "tooluse", "functioncall"})

# 参数会原样进 runs.jsonl 和 judge prompt：密钥必须脱敏，整篇文件内容必须截断。
_SECRET_ARG_RE = re.compile(
    r"(?i)(api[-_]?key|access[-_]?token|token|secret|password|passwd|credential"
    r"|authorization|cookie|session[-_]?key)")
_ARG_VALUE_LIMIT = 500


def _sanitize_arguments(value):
    """脱敏 + 限长；结构保留，判参数正确性靠的是路径/字段名这些短值。"""
    if isinstance(value, dict):
        return {k: "<redacted>" if _SECRET_ARG_RE.search(str(k)) else _sanitize_arguments(v)
                for k, v in value.items()}
    if isinstance(value, list):
        # ponytail: 长列表截到 20 项。真遇到需要判第 21 个参数的题再放宽。
        return [_sanitize_arguments(v) for v in value[:20]]
    if isinstance(value, str) and len(value) > _ARG_VALUE_LIMIT:
        return value[:_ARG_VALUE_LIMIT] + f"…[truncated at {_ARG_VALUE_LIMIT} of {len(value)} chars]"
    return value


@register("openclaw")
class OpenClawRuntimeAdapter(BaseRuntimeAdapter):
    """把 OpenClaw 当子进程跑。session_id 串起多轮，workspace 由 OpenClaw 自己管。"""

    def __init__(self, bin: str = "openclaw", agent: str | None = None,
                 profile: str | None = None, extra_args: list[str] | None = None,
                 node_bin: str | None = None, workspace: str | None = None,
                 routing_input: dict | None = None, model: str | None = None,
                 auth_choice: str = "qwen-standard-api-key"):
        self.bin = bin
        self.agent = agent
        self.profile = profile
        self.extra_args = extra_args or []
        # 容器化跑时必须显式指定：镜像里的 profile 是干净的，没有「上次配好的模型」。
        # 顺带解掉一个历史遗留 —— 本机跑时模型选在 OpenClaw 自己的配置里，不进
        # config_hash；写在这儿它就进 fingerprint 了。
        self.model = model
        self.auth_choice = auth_choice
        # artifact 追踪的根目录；不给就问 openclaw 要 agents.defaults.workspace
        self.workspace = workspace
        # openclaw 的 bin 是 `#!/usr/bin/env node` 脚本 —— 给它绝对路径也没用，
        # 它照样按 PATH 找 node。nvm 多版本共存时必须显式指定，否则会撞上
        # "Node.js >=22.22.3 required" 直接退出。指向 node 可执行文件即可。
        self.node_bin = node_bin
        spec = routing_input or {"strategy": "direct", "options": {}}
        self.routing_input = create_routing_input(
            spec.get("strategy", "direct"), **(spec.get("options") or {})
        )
        self._bootstrapped_containers: set[str] = set()
        self._bootstrap_lock = threading.Lock()
        # local OpenClaw 的 profile 配置是进程间共享文件。prepared() 会临时改
        # tools/workspace/extraDirs；必须把「改 → run → 还原」整段跨线程、跨进程
        # 串行化，否则请求 A 可能在请求 B 运行中途恢复掉它的 allowlist。
        # 容器模式每个 request 有独立 profile，不走这些锁。
        self._local_profile_lock = threading.RLock()
        profile_lock_id = hashlib.sha256(
            f"{self.bin}\0{self.profile or '__default__'}".encode("utf-8")
        ).hexdigest()[:16]
        self._local_profile_file_lock = FileLock(
            str(Path(tempfile.gettempdir()) /
                f"skilleval-openclaw-profile-{profile_lock_id}.lock")
        )
        self.version = self._probe_version()

    # ---- 内部 ----

    def _base_cmd(self, request: InvocationRequest | None = None) -> list[str]:
        prefix = (
            request.environment.command_prefix
            if request and request.environment else []
        )
        cmd = [*prefix, self.bin]
        if self.profile:
            cmd += ["--profile", self.profile]
        return cmd

    def _env(self) -> dict[str, str]:
        env = {**{k: v for k, v in os.environ.items() if k not in _STRIP_ENV}, **_FORCE_ENV}
        if self.node_bin:
            node_dir = str(Path(self.node_bin).expanduser().resolve().parent)
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        return env

    def _probe_version(self) -> str | None:
        env = self._env()
        if not shutil.which(self.bin, path=env.get("PATH")):
            return None
        try:
            p = subprocess.run([self.bin, "--version"], capture_output=True,
                               text=True, timeout=60, env=env)
        except (subprocess.SubprocessError, OSError):
            return None
        out = (p.stdout or "").strip()
        # node 版本不合规时，openclaw 把「Node.js >=x is required」打到 stdout 就退出，
        # 那不是版本号 —— 别把它当版本存下来，否则 healthcheck 会误判成健康
        if p.returncode != 0 or "is required" in out:
            return None
        return out or None

    @staticmethod
    def _extract_text(stdout: str) -> str:
        """从 --json 输出里取回复正文。字段名随版本会变，多试几个再退回原文。

        2026.7.x 的结构是 {"payloads": [{"text": ...}], "meta": {...}}；
        更早的版本是顶层 text/reply/message。两代都要认 —— 认错了不会报错，
        只会让 selected_skills 静默变空（模型其实答对了），比崩溃更难发现。
        """
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout

        # 2026.7.x：payloads 数组，把所有 text 段拼起来
        if isinstance(data, dict) and isinstance(data.get("payloads"), list):
            parts = [p["text"] for p in data["payloads"]
                     if isinstance(p, dict) and isinstance(p.get("text"), str)]
            if parts:
                return "\n".join(parts)

        if isinstance(data, str):
            return data
        for key in ("text", "reply", "message", "content", "output", "result", "response"):
            v = data.get(key) if isinstance(data, dict) else None
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):  # 形如 {"message": {"text": ...}}
                for k2 in ("text", "content", "body"):
                    if isinstance(v.get(k2), str):
                        return v[k2]
        return stdout

    @staticmethod
    def _parse_meta(stdout: str) -> dict:
        """从 --json 的 meta 里挖出归一化需要的东西（2026.7.x 结构）。

        `--json` 的 meta 只给**聚合的** toolSummary（用了哪些 tool、共几次、失败几次），
        所以 ToolCall.arguments 留空。逐次的参数与顺序不在这里，在
        `agentMeta.sessionFile` 指向的会话 JSONL 里 —— 见 `_transcript_tool_events`。
        """
        out: dict = {"tool_calls": [], "loaded_skills": [], "usage": {},
                     "resolved_model": None, "session_file": None}
        try:
            meta = (json.loads(stdout) or {}).get("meta") or {}
        except json.JSONDecodeError:
            return out

        agent = meta.get("agentMeta") or {}
        if isinstance(agent.get("sessionFile"), str):
            out["session_file"] = agent["sessionFile"]
        if agent.get("model"):
            out["resolved_model"] = f"{agent.get('provider', '?')}/{agent['model']}"
        u = agent.get("usage") or {}
        if u:
            out["usage"] = {"input_tokens": u.get("input"), "output_tokens": u.get("output"),
                            "total_tokens": u.get("total"),
                            "reasoning_tokens": u.get("reasoningTokens")}

        # toolSummary: {"calls": 3, "tools": ["read","write","exec"], "failures": 0}
        # calls/failures 是整轮的总数，不是每个 tool 的 —— 只挂在第一个上会造成误解，
        # 所以整轮的数字单独放 usage，ToolCall 只记"用过哪个"。
        ts = meta.get("toolSummary") or {}
        out["tool_calls"] = [ToolCall(name=n) for n in (ts.get("tools") or [])
                             if isinstance(n, str)]
        if ts:
            out["usage"]["tool_calls_total"] = ts.get("calls")
            out["usage"]["tool_failures_total"] = ts.get("failures")

        # systemPromptReport.skills.entries = OpenClaw 实际注入 prompt 的 skill
        skills = ((meta.get("systemPromptReport") or {}).get("skills") or {}).get("entries") or []
        out["loaded_skills"] = [e["name"] for e in skills
                                if isinstance(e, dict) and isinstance(e.get("name"), str)]
        return out

    def _read_session_file(self, request: InvocationRequest, path: str) -> str:
        """读会话 JSONL；容器里的会话文件要用同一个 command_prefix 进容器拿。"""
        prefix = (request.environment.command_prefix
                  if request.environment else []) or []
        if prefix:
            p = subprocess.run([*prefix, "cat", path], capture_output=True,
                               text=True, timeout=120, env=self._env())
            return p.stdout if p.returncode == 0 else ""
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _transcript_tool_events(transcript: str) -> list[TrajectoryEvent]:
        """把会话 JSONL 里的逐次 tool 调用/结果归一成 **exact** 事件。

        这是 argument_correctness / order_correctness 从 N/A 变成可判的唯一依据：
        toolCall block 带 id + name + arguments，toolResult 带 toolCallId + isError，
        配对靠 call_id 而不是数组位置。解析不出来就返回空，让调用方退回 coarse ——
        **宁可保留 N/A，也不能拿聚合摘要冒充逐次证据**。

        ponytail: 一个 request 一个 session，所以整份 transcript 就是这一轮。
        接多轮编排（P5）后要按 turn 切，否则第 2 轮会带上第 1 轮的事件。
        """
        events: list[TrajectoryEvent] = []

        def _add(**kwargs) -> None:
            events.append(TrajectoryEvent(step_index=len(events) + 1,
                                          evidence_level="exact",
                                          metadata={"source": "openclaw.session"},
                                          **kwargs))

        for line in transcript.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "message":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "assistant":
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if str(block.get("type") or "").strip().lower() not in _TOOL_CALL_BLOCKS:
                        continue
                    name = block.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    args = (block.get("arguments") if isinstance(block.get("arguments"), dict)
                            else block.get("input") if isinstance(block.get("input"), dict)
                            else block.get("parameters"))
                    _add(event_type="tool_call", name=name, tool_name=name,
                         call_id=block.get("id") if isinstance(block.get("id"), str) else None,
                         arguments=_sanitize_arguments(args) if isinstance(args, dict) else None,
                         status="started")
            elif role == "toolResult":
                name = message.get("toolName")
                if not isinstance(name, str) or not name:
                    continue
                ts = message.get("timestamp")
                _add(event_type="tool_result", name=name, tool_name=name,
                     call_id=(message.get("toolCallId")
                              if isinstance(message.get("toolCallId"), str) else None),
                     status="failed" if message.get("isError") else "success",
                     timestamp_ms=ts if isinstance(ts, int) and ts >= 0 else None)
        return events

    def _workspace_dir(self, request: InvocationRequest | None = None) -> Path | None:
        """OpenClaw 的 workspace，artifact 就在这里面产生。"""
        if request and request.environment and request.environment.host_workspace:
            return Path(request.environment.host_workspace)
        if self.workspace:
            return Path(self.workspace).expanduser()
        try:
            p = subprocess.run(self._base_cmd(request) + ["config", "get", "agents.defaults.workspace"],
                               capture_output=True, text=True, timeout=60, env=self._env())
            v = (p.stdout or "").strip().strip('"')
            return Path(v).expanduser() if v and not v.startswith("Config path not found") else None
        except (subprocess.SubprocessError, OSError):
            return None

    @staticmethod
    def _snapshot(root: Path | None) -> dict[str, tuple[float, int]]:
        """workspace 文件快照：路径 → (mtime, size)。跑前跑后各一次，diff 出 artifact。"""
        if not root or not root.is_dir():
            return {}
        snap = {}
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            # .git / .openclaw 是 OpenClaw 自己的状态，不是 run 的产物。
            # 必须按**相对 root** 的路径判断 —— workspace 本身就住在 ~/.openclaw/ 下面，
            # 拿绝对路径去匹配会把整个 workspace 的文件全排除掉（artifacts 恒为空）。
            try:
                rel = f.relative_to(root)
            except ValueError:
                continue
            if any(part in (".git", ".openclaw") for part in rel.parts):
                continue
            if len(rel.parts) == 1 and rel.name in _BOOTSTRAP_FILES:
                continue
            try:
                st = f.stat()
                snap[str(f)] = (st.st_mtime, st.st_size)
            except OSError:
                pass
        return snap

    @staticmethod
    def _text_excerpt(data: bytes) -> str | None:
        """文本类产物留一份内容前缀给 judge；二进制（docx/png/…）返回 None。

        判定方式就是"能不能按 UTF-8 解出来"——比按扩展名白名单准，模型爱用什么后缀
        都拦不住它。workspace 跑完即删，这里不留内容，之后谁也看不到产物写了什么。
        """
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if len(text) <= TEXT_EXCERPT_LIMIT:
            return text
        return text[:TEXT_EXCERPT_LIMIT] + f"\n…[truncated at {TEXT_EXCERPT_LIMIT} chars]"

    @classmethod
    def _diff_artifacts(cls, before: dict, after: dict, root: Path | None) -> list[Artifact]:
        """跑前跑后对比，登记新增/修改的文件（§11.4：必须记 sha256/大小/mime）。"""
        arts: list[Artifact] = []
        for path, meta in sorted(after.items()):
            if path in before and before[path] == meta:
                continue
            f = Path(path)
            try:
                data = f.read_bytes()
            except OSError:
                continue
            arts.append(Artifact(
                path=str(f.relative_to(root)) if root and f.is_relative_to(root) else str(f),
                sha256="sha256:" + hashlib.sha256(data).hexdigest()[:16],
                size_bytes=len(data),
                mime_type=mimetypes.guess_type(f.name)[0],
                change="modified" if path in before else "created",
                text_excerpt=cls._text_excerpt(data),
            ))
        return arts

    # ---- N4 Environment Resolver：skill 注入与还原（AGENTS.md §10）----

    def _config(self, request: InvocationRequest, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(self._base_cmd(request) + ["config", *args],
                              capture_output=True, text=True, timeout=120, env=self._env())

    def _read_config(self, request: InvocationRequest, key: str) -> str | None:
        """读一个配置键的原样值；没配过返回 None。"""
        p = self._config(request, "get", key)
        out = (p.stdout or "").strip()
        if p.returncode != 0 or not out or out.startswith("Config path not found"):
            return None
        return out

    def _write_config_checked(
        self, request: InvocationRequest, action: str, key: str, value: str | None = None
    ) -> None:
        args = [action, key] if value is None else [action, key, value]
        result = self._config(request, *args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise RuntimeError(
                f"OpenClaw 配置 {action} {key} 失败；"
                f"拒绝在未落实权限/隔离配置时继续：{detail}"
            )

    @staticmethod
    def _in_container(request: InvocationRequest) -> bool:
        return bool(request.environment and request.environment.container_id)

    def _bootstrap_container(self, request: InvocationRequest) -> None:
        """把一个干净容器配到能跑 agent：onboard 拿凭据，再选模型。

        为什么不在镜像里做（省掉每个容器 ~6s）：onboard 会把 key 写进 profile 的
        auth store，而 **auth store 优先于环境变量** —— 用占位 key build 的镜像，
        运行时再传真 key 也照样 401。实测过，别再试一次。
        所以 onboard 必须带着真 key 在运行时跑，key 由 Environment Backend 的
        env_passthrough 注入容器，这里直接用容器自己的环境变量，不经过命令行。

        ponytail: 每个 request 一个容器 = 每题 +6s。真嫌慢就复用容器（要先解决
        repeat 间的状态泄漏，§13.6），或者把 onboard 后的容器 commit 成带凭据的
        私有镜像 —— 后者等于把 key 焊进镜像，只在完全私有的 registry 才可接受。
        """
        container_id = request.environment.container_id if request.environment else None
        if container_id:
            with self._bootstrap_lock:
                if container_id in self._bootstrapped_containers:
                    return

        onboard = subprocess.run(
            self._base_cmd(request) + ["onboard", "--non-interactive", "--accept-risk",
                                       "--auth-choice", self.auth_choice, "--skip-health"],
            capture_output=True, text=True, timeout=300, env=self._env())
        if onboard.returncode != 0:
            raise RuntimeError(
                f"容器内 openclaw onboard 失败（auth_choice={self.auth_choice}）："
                f"{(onboard.stderr or onboard.stdout or '').strip()[-400:]}；"
                f"多半是 environment.env_passthrough 没把 provider 的 key 变量传进容器"
            )
        if self.model:
            set_model = self._config(request, "set", _MODEL, self.model)
            if set_model.returncode != 0:
                raise RuntimeError(
                    f"容器内选模型失败 {self.model}："
                    f"{(set_model.stderr or set_model.stdout or '').strip()[-300:]}"
                )
        # 宿主机可能根本没装 openclaw，__init__ 里那次探测在容器模式下必然是 None。
        # 版本会进 RunResult 和 fingerprint，所以在这儿用容器里的真实版本补上。
        probe = subprocess.run(self._base_cmd(request) + ["--version"],
                               capture_output=True, text=True, timeout=60, env=self._env())
        out = (probe.stdout or "").strip()
        if probe.returncode == 0 and out and "is required" not in out:
            self.version = out
        if container_id:
            with self._bootstrap_lock:
                self._bootstrapped_containers.add(container_id)

    @contextmanager
    def _swapped(
        self, request: InvocationRequest, key: str, value: str
    ) -> Iterator[None]:
        """临时改一个 openclaw 配置键，退出时**恢复原值**（原本没有就删掉）。

        恢复原值而不是无脑 unset —— 用户本来可能就配了，吃掉别人的配置很难查。
        """
        previous = self._read_config(request, key)
        self._write_config_checked(request, "set", key, value)
        try:
            yield
        finally:
            if previous is not None:
                self._write_config_checked(request, "set", key, previous)
            else:
                self._write_config_checked(request, "unset", key)

    @contextmanager
    def _swapped_json_array(
        self, request: InvocationRequest, key: str, values: list[str]
    ) -> Iterator[None]:
        """Fail closed while applying one security-critical OpenClaw list setting."""
        previous = self._read_config(request, key)
        self._write_config_checked(request, "set", key, json.dumps(values))
        try:
            observed = self._read_config(request, key)
            try:
                parsed = json.loads(observed) if observed is not None else None
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"OpenClaw 写入 {key} 后返回了不可解析的值：{observed!r}"
                ) from error
            if parsed != values:
                raise RuntimeError(
                    f"OpenClaw 未落实 {key}：期望 {values!r}，实际 {parsed!r}"
                )
            yield
        finally:
            if previous is not None:
                self._write_config_checked(request, "set", key, previous)
            else:
                self._write_config_checked(request, "unset", key)

    def _tool_policy(self, request: InvocationRequest):
        """Turn suite.tools into an enforced OpenClaw policy for this turn."""
        if request.allowed_tools:
            return self._swapped_json_array(
                request, _TOOLS_ALLOW, request.allowed_tools
            )
        # OpenClaw treats an omitted/empty allowlist as permissive. A wildcard
        # deny is the explicit text-only posture and still permits a final reply.
        return self._swapped_json_array(request, _TOOLS_DENY, ["*"])

    @contextmanager
    def _local_profile_guard(self, request: InvocationRequest) -> Iterator[None]:
        """Serialize temporary profile mutations across threads and processes."""
        with self._local_profile_lock:
            with self._local_profile_file_lock.acquire(
                timeout=request.timeout_seconds
            ):
                yield

    @contextmanager
    def prepared(self, request: InvocationRequest) -> Iterator[None]:
        """把 suite 解出来的 skill 物化到临时目录，通过 extraDirs 挂给 OpenClaw；退出即还原。

        为什么是「复制到 staging + extraDirs」而不是别的：

        * 不写 `<workspace>/skills` —— 那是用户的目录，跑 eval 不该往里塞东西
        * 不用软链 —— OpenClaw 默认**跳过**解析到 root 之外的软链（除非配
          allowSymlinkTargets 白名单），软链会静默不生效，比报错还难查
        * 复制而非引用原目录 —— suite 的 exclude/overlay 已经在 `request.skills` 里
          解析完了，staging 里放什么就是模型能看见什么，V1/V2/None 三种条件天然精确
        * 不改原始 skill 目录，满足 §7.1「不得修改原始 Skill 内容」

        routing-only 不注入：它只需要 metadata 拼进 prompt，让 OpenClaw 真加载 skill
        反而会污染 catalog（§18.1 路由不得加载完整 skill）。

        full 模式还会把 workspace 换成**每个请求一个**的临时目录（§11.2 isolated）。
        不这么做，同一个 case 的 repeat 1 会看见 repeat 0 写下的文件，
        模型可能直接说「已经有了」而不重写 —— 产物命中率就成了运行顺序的函数，
        而不是 skill 的函数（§13.6「不同 Repeat 不得共享状态」）。
        """
        # 容器初始化在 skill_mode 判断之前：routing_only 一样要在容器里调 agent，
        # 一样需要凭据和模型。
        if self._in_container(request):
            self._bootstrap_container(request)

        profile_guard = (
            nullcontext() if self._in_container(request)
            else self._local_profile_guard(request)
        )
        with profile_guard:
            tool_policy = self._tool_policy(request)

            if request.skill_mode != "full":
                with tool_policy:
                    yield
                return

            if request.environment is not None:
                # workspace/skill staging 的创建与清理由 Environment Backend 负责；
                # adapter 只把 runtime 路径写进 OpenClaw 配置。
                skill_dirs = request.environment.runtime_skill_dirs
                workspace = request.environment.runtime_workspace
                if not workspace:
                    raise ValueError("prepared environment 缺 runtime_workspace")
                extra = self._swapped(request, _EXTRA_DIRS, json.dumps(skill_dirs)) \
                    if skill_dirs else nullcontext()
                with tool_policy, extra, self._swapped(request, _WORKSPACE, workspace):
                    yield
                return

            staging = Path(tempfile.mkdtemp(prefix="skilleval-skills-"))
            # ponytail: workspace 跑完即删，RunResult 留产物元数据 + 文本内容前缀
            # （`Artifact.text_excerpt`，judge 判"内容对不对"就靠它）。二进制产物和超过
            # TEXT_EXCERPT_LIMIT 的部分仍然看不到；真要留全档，按 AUTHORING.md §1.4 拷到
            # outputs/{run}/artifacts/{run_id}/ —— 等 P7 Viewer 真的要展示它们时再做。
            workspace = Path(tempfile.mkdtemp(prefix="skilleval-ws-"))
            try:
                for s in request.skills:
                    src = Path(s.source_path).parent
                    if src.is_dir():
                        shutil.copytree(src, staging / s.skill_id, symlinks=False)
                # skills 为空（none 基线）时不设 extraDirs，让 catalog 干干净净
                extra = self._swapped(request, _EXTRA_DIRS, json.dumps([str(staging)])) \
                    if request.skills else nullcontext()
                with tool_policy, extra, self._swapped(request, _WORKSPACE, str(workspace)):
                    yield
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(workspace, ignore_errors=True)

    # ---- Protocol ----

    def _run_impl(self, request: InvocationRequest) -> RunResult:
        if request.skill_mode == "routing_only":
            # OpenClaw CLI 只有一个 --message 入口；保留 role 边界后序列化成文本。
            routed_messages = self.routing_input.build_messages(request)
            message = "\n\n".join(
                f"[{item['role'].upper()}]\n{item['content']}"
                for item in routed_messages
            )
        else:
            message = request.prompt

        session = _safe_session_id(
            request.session_id or f"skilleval-{request.case_id}-{request.repeat_index}")
        cmd = self._base_cmd(request) + ["agent", "--local", "--json",
                                  "--session-id", session, "--message", message]
        if self.agent:
            cmd += ["--agent", self.agent]
        cmd += self.extra_args

        # full 模式才追踪 artifact：routing-only 不该碰文件系统（§18.1），
        # 每题扫两遍 workspace 纯属浪费
        ws = self._workspace_dir(request) if request.skill_mode == "full" else None
        before = self._snapshot(ws)

        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=request.timeout_seconds, env=self._env())
        if p.returncode != 0:
            err = p.stderr.strip()
            error_kind = "network" if _looks_like_network(err) else "runtime"
            return RunResult(
                case_id=request.case_id, repeat_index=request.repeat_index,
                model=str(request.model.get("id", "openclaw")),
                status="failed", raw_output=p.stdout or None,
                error=f"openclaw exit={p.returncode}: {err[-500:]}",
                # CLI 非 0 退出默认算 runtime；但它也可能只是在转述上游 API 的网络失败，
                # 那种要记成 network，否则「模型服务挂了」会被算成「runtime 不稳」
                error_kind=error_kind,
                # network 失败进一步区分 auth/quota/DNS/timeout/rate-limit，用户才知道该
                # 充值还是查网络（HANDOFF ★ 更新 16）；err 只走 CLI stderr，没有异常对象。
                error_subkind=classify_error_text_subkind(err, error_kind),
            )

        text = self._extract_text(p.stdout)
        valid = {s.skill_id for s in request.skills}
        try:
            sel, reason = parse_selection(text, valid)
        except (json.JSONDecodeError, AttributeError):
            # full 模式模型不会回 JSON；退化成在正文里找 skill 名
            sel = [s for s in valid if s in text]
            reason = None

        meta = self._parse_meta(p.stdout)
        artifacts = self._diff_artifacts(before, self._snapshot(ws), ws)
        # 优先用会话 JSONL 的逐次事件（exact）；读不到才退回聚合 toolSummary
        # （coarse，明确不能用于 argument/order 评分）。
        trajectory = self._transcript_tool_events(
            self._read_session_file(request, meta["session_file"])
            if meta["session_file"] else "")
        step = len(trajectory) + 1
        if not trajectory:
            for tool in meta["tool_calls"]:
                trajectory.append(TrajectoryEvent(
                    step_index=step,
                    event_type="tool_call",
                    name=tool.name,
                    tool_name=tool.name,
                    status="unknown",
                    evidence_level="coarse",
                    metadata={"source": "openclaw.toolSummary", "count": tool.count},
                ))
                step += 1
        for artifact in artifacts:
            trajectory.append(TrajectoryEvent(
                step_index=step,
                event_type="state_change",
                name="workspace_state_change",
                status="success",
                evidence_level="derived",
                evidence_refs=[artifact.path],
                state_after={"path": artifact.path, "sha256": artifact.sha256,
                             "size_bytes": artifact.size_bytes,
                             "change": artifact.change},
            ))
            step += 1
        trajectory.append(TrajectoryEvent(
            step_index=step,
            event_type="final",
            name="final_answer",
            status="success",
            evidence_level="exact",
        ))
        return RunResult(
            case_id=request.case_id, repeat_index=request.repeat_index,
            model=str(request.model.get("id", "openclaw")),
            selected_skills=sel, reasoning=reason,
            final_answer=text if request.skill_mode == "full" else None,
            raw_output=p.stdout,
            tool_calls=meta["tool_calls"],
            trajectory=trajectory,
            loaded_skills=meta["loaded_skills"],
            artifacts=artifacts,
            usage=meta["usage"],
            resolved_model=meta["resolved_model"],
        )

    def _healthcheck_container(self, environment) -> RuntimeHealth:
        """在真容器里检查 CLI、onboard 与配置，但不发送模型请求。

        `pipeline plan --healthcheck` 承诺不外发 eval 数据，因此这里只能验证运行前置；
        provider 鉴权、模型 ID 和额度要等用户确认后的真实 run 才能验证。
        """
        probe = InvocationRequest(
            request_id="healthcheck", case_id="skilleval-health", repeat_index=0,
            prompt="ping", skill_mode="none", model={"id": "healthcheck"},
        )
        try:
            with environment.prepared(probe) as prepared:
                self._bootstrap_container(prepared)
                p = self._config(prepared, "validate")
        except Exception as exc:  # noqa: BLE001 — healthcheck 只报告，不外抛
            return RuntimeHealth(healthy=False, runtime=self.name, version=self.version,
                                 detail=f"容器内探针失败：{exc}")
        if p.returncode != 0:
            return RuntimeHealth(
                healthy=False, runtime=self.name, version=self.version,
                detail=f"容器内 config validate 失败："
                       f"{(p.stderr or p.stdout or '').strip()[-300:]}")
        return RuntimeHealth(healthy=True, runtime=self.name, version=self.version,
                             detail="容器内 CLI/onboard/config 均可用；未发送模型请求")

    def _install_hint(self) -> str:
        """先自查再建议（AGENTS.md §29.28）。

        踩过：openclaw 明明装在 nvm 的 node v24 下，只是默认 PATH 上的 node 是 v22，
        我们却回一句「安装：npm i -g openclaw」—— 用户照做只会再装一遍同一个版本。
        它的 bin 是 `env node` 脚本，给绝对路径也没用，必须指 node_bin。
        """
        found = sorted(Path.home().glob(f".nvm/versions/node/*/bin/{self.bin}"))
        if not found:
            return "安装：npm i -g openclaw"
        node = found[-1].parent / "node"
        return (
            f"但它其实装着：{found[-1]}。它的 bin 是 `env node` 脚本，"
            f"给绝对路径没用，要在 suite 的 runtime_options 里加：\n"
            f"    node_bin: {node}"
        )

    def healthcheck(self, environment=None) -> RuntimeHealth:
        # 容器化时宿主机装没装 openclaw 与结果无关，必须进容器探
        if environment is not None and getattr(environment, "name", "local") != "local":
            return self._healthcheck_container(environment)
        if not shutil.which(self.bin, path=self._env().get("PATH")):
            return RuntimeHealth(
                healthy=False, runtime=self.name,
                detail=f"PATH 上找不到 {self.bin}。{self._install_hint()}")
        if self.version is None:
            return RuntimeHealth(
                healthy=False, runtime=self.name,
                detail="openclaw 启动失败（多半是 node 版本不满足它的 engines 要求）。"
                       "在 runtime_options 里指定 node_bin 指向合规的 node，"
                       "或 nvm use 到合规版本")
        try:
            probe = subprocess.run(
                self._base_cmd() + ["config", "validate"],
                capture_output=True, text=True, timeout=60, env=self._env())
        except subprocess.TimeoutExpired:
            return RuntimeHealth(healthy=False, runtime=self.name, version=self.version,
                                 detail="openclaw config validate 超时（>60s）")
        if probe.returncode != 0:
            err = (probe.stderr or probe.stdout or "").strip()
            return RuntimeHealth(healthy=False, runtime=self.name, version=self.version,
                                 detail=f"config validate 失败：{err[-300:]}")
        return RuntimeHealth(
            healthy=True, runtime=self.name, version=self.version,
            detail="CLI/config 可用；未发送模型请求，provider 鉴权与模型可用性留到 run 验证",
        )

    def capabilities(self) -> RuntimeCapabilities:
        # OpenClaw 自带 agent loop / skill 发现 / tool / session / workspace（§4.1）
        return RuntimeCapabilities(
            runtime=self.name,
            skill_modes=["none", "routing_only", "full"],
            tools=True, multi_turn=True, workspace=True, network_control=False,
        )

    def fingerprint(self) -> dict:
        # OpenClaw 版本也算：升级它可能改变 agent loop 行为，结果不可跨版本比
        return {"routing_input": self.routing_input.fingerprint(),
                "openclaw_version": self.version,
                "agent": self.agent, "profile": self.profile,
                "tool_policy": _TOOL_POLICY_VERSION,
                # 容器模式下 model 是本 adapter 显式设的，进指纹才能跨 run 归因；
                # 本机模式留 None，实际模型仍要看 RunResult.resolved_model
                "model": self.model, "auth_choice": self.auth_choice}

    # node_bin 不进 fingerprint：它只决定「用哪个解释器启动」，不改变 agent 行为；
    # 真正影响结果的 openclaw_version 已经在上面了。
