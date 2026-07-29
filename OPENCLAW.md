# OpenClaw 接入手册

**OpenClaw 只是 skillEval 的一个运行环境（runtime），不是主角。** 你日常动的是 skillEval：
写题、改 suite、看结果。这份文档只解决一件事 —— 换台机器怎么把 OpenClaw 重新接上。

已实测跑通：`OpenClaw 2026.7.1-2` + `node v24.18.0` + DashScope(qwen provider)，2026-07-28。

---

## 0. TL;DR — 新机器从零到通

全程**不需要交互式终端**（网上和别的文档说"必须 TTY"，那是错的，见 §6.1）：

```bash
# ① node（OpenClaw 2026.7.x 要求 >=22.22.3 或 >=24.15）
nvm install 24 && nvm alias default 24 && nvm use 24

# ② 装 CLI
npm i -g openclaw

# ③ 装 provider 插件（qwen 不在 core 里，是外部官方插件）
openclaw --profile skilleval plugins install @openclaw/qwen-provider

# ④ 配凭据 —— key 从 skillEval 的 .env 来，不手输、不进 shell history
set -a; source .env; set +a
QWEN_API_KEY="$DASHSCOPE_API_KEY" \
  openclaw --profile skilleval onboard \
    --non-interactive --accept-risk \
    --auth-choice qwen-standard-api-key \
    --skip-health

# ⑤ 用仓库自带 full suite 走统一入口预检
.venv/bin/python -m pipeline plan \
    --suite evals/suites/example_full.yaml --healthcheck
# 期望：runtime=openclaw version=OpenClaw 2026.7.1-2 healthy=✓，2 requests
```

`pipeline plan --healthcheck` 仍然不发模型请求：OpenClaw 侧只检查 CLI 版本和
`config validate`，provider 鉴权、模型 ID 与额度留到确认后的 `pipeline run` 验证。
探不通时它会直接告诉你缺什么 —— 比如 node 版本不对，它会把该用哪个 node 打出来：

```text
runtime=openclaw version=? healthy=✗
  PATH 上找不到 openclaw。但它其实装着：/…/node/v24.18.0/bin/openclaw。
  它的 bin 是 `env node` 脚本，给绝对路径没用，要在 suite 的 runtime_options 里加：
    node_bin: /…/node/v24.18.0/bin/node
```

照它说的在你自己的 suite 的 `runtime_options` 里补 `node_bin`，再用同一入口预检。
仓库示例不写机器相关绝对路径：

```bash
cp evals/suites/example_full.yaml evals/suites/full_local.yaml
# 编辑 full_local.yaml:
# runtime_options:
#   profile: skilleval
#   node_bin: /你的/node/v24/bin/node
.venv/bin/python -m pipeline plan \
    --suite evals/suites/full_local.yaml --healthcheck
```

如果 healthcheck 报 `unable to open database file`，先别急着重装。2026 年 7 月 28 日的
本地验证里，这更常见的原因是**当前执行环境不允许 OpenClaw 写 profile/db**。先换到允许
本地写入的环境重跑同一条 healthcheck；只有仍然失败时，才按下面的安装/凭据步骤排查。

四个非显然的参数，缺一个就卡住：

| 参数 | 为什么必须有 |
| --- | --- |
| `--non-interactive` | 不开它就要 TTY，脚本/CI 里跑不了 |
| `--accept-risk` | `--non-interactive` 的强制前置，不给会直接拒绝 |
| `--skip-health` | 最后一步会等 Gateway 起来；**我们走 `agent --local`，根本不需要 Gateway**，不跳过就 exit 1 |
| `--auth-choice qwen-standard-api-key` | Standard(按量) 端点。Coding Plan 是另一个 choice，模型可用范围不同 |

---

## 1. 从哪下

| 东西 | 怎么装 | 下载地址 |
| --- | --- | --- |
| OpenClaw CLI | `npm i -g openclaw` | npm 包：<https://www.npmjs.com/package/openclaw><br>源码：<https://github.com/openclaw/openclaw>（**不用 clone**） |
| node（前置） | `nvm install 24` | <https://nodejs.org/en/download><br>nvm：<https://github.com/nvm-sh/nvm> |
| qwen provider | `openclaw --profile skilleval plugins install @openclaw/qwen-provider` | <https://www.npmjs.com/package/@openclaw/qwen-provider> |
| Docker（只有走容器隔离才需要） | Docker Desktop / Engine | <https://docs.docker.com/get-started/get-docker/> |
| 文档 | 装完就在本地 | `$(npm root -g)/openclaw/docs/` —— 比在线文档更贴合你装的版本 |

装的是哪个版本要跟仓库对得上：本项目验证过的是 `openclaw@2026.7.1-2`，
`environments/openclaw.Dockerfile` 里也钉的是这个版本。装别的版本能不能跑没验证过，
而且换版本会改变 runtime fingerprint → `config_hash` 变 → 跟旧结果不可直接比。

**不要 clone 源码。** 本项目是非侵入接入（AGENTS.md §3.1）：只调 CLI，不改它一行代码。
装 npm 包就够，升级也不用跟着改我们的 adapter。

本地文档很有用，找 provider 配置时直接翻：

```bash
ls $(npm root -g)/openclaw/docs/providers/     # 各家 provider 的配置说明
```

---

## 2. node 版本：最容易卡住的地方

OpenClaw 2026.7.x 的 `engines` 要求 **node >=22.22.3 <23 或 >=24.15**。不满足时它**启动就退出**，
连 `--version` 都不给。

```bash
node --version                    # 必须达标
command -v openclaw               # 应指向对应 node 版本的 bin 目录
openclaw --version                # OpenClaw 2026.7.1-2 (xxxxxxx)
```

### 坑 A：给绝对路径也没用

openclaw 的 bin 是 `#!/usr/bin/env node` 脚本 —— **它按 PATH 找 node，不认你给的绝对路径**。
所以 `/path/to/v24/bin/openclaw` 在 node 22 的 shell 里照样报版本不符。

两种解法：

```bash
nvm alias default 24                          # 一劳永逸（推荐）
```

或者在 suite 里告诉 adapter 用哪个 node（父进程 PATH 不合规时的安全网）：

```yaml
runtime_options:
  node_bin: ~/.nvm/versions/node/v24.18.0/bin/node   # adapter 会把它的目录 prepend 到子进程 PATH
```

### 坑 B：全局包按 node 版本隔离

`nvm use 24` 之后 `npm i -g openclaw` 装的是 **v24 那份**。v22 下那份还在，且永远跑不起来。
切完版本记得清掉旧的，免得日后困惑：

```bash
nvm use 22 && npm uninstall -g openclaw && nvm use 24
```

### 坑 C：验证 nvm 是否生效，`zsh -l -c` 和 `zsh -i -c` 都不可靠

- `zsh -l -c` 是**非交互**，zsh 不读 `.zshrc`，nvm 压根没加载
- `zsh -i -c` 交互了，但会**继承父 shell 钉死的 PATH**，nvm 不覆盖已有路径

必须清空环境才等价于"真开一个新终端"：

```bash
env -i HOME="$HOME" TERM=xterm SHELL=/bin/zsh /bin/zsh -i -c 'node --version; openclaw --version'
```

> 如果你从一个老 shell 启动 skillEval，它继承的还是老 PATH。这时 healthcheck 会报
> "找不到 openclaw" 或版本不符 —— **不是配置坏了，是 shell 没切**。

---

## 3. profile 隔离：别污染主配置

所有命令都加 `--profile skilleval`，状态就落在独立目录，不碰你自己的 OpenClaw：

| | 路径 |
| --- | --- |
| 配置 | `~/.openclaw-skilleval/openclaw.json` |
| 凭据 | `~/.openclaw-skilleval/agents/main/agent/openclaw-agent.sqlite` |
| 会话 | `~/.openclaw-skilleval/agents/main/sessions` |
| 工作区 | `~/.openclaw/workspace-skilleval` |

suite 里声明一次即可，adapter 会带上：

```yaml
runtime_options:
  profile: skilleval
```

### 3.1 suite tool 权限

full suite 的顶层 `tools` 是 OpenClaw 运行时强 allowlist，不只是实验说明：

```yaml
tools: [read, write]
```

adapter 会在每个请求前临时设置 `tools.allow`，回读确认后才调用 agent，并在结束后恢复
profile 原值。空列表会设置 `tools.deny: ["*"]`。local profile 的整个“设置 → agent →
恢复”区间会在同机线程和 pipeline 进程间串行化，防止并发请求互相覆盖；容器请求各自
使用独立 profile。

OpenClaw 原有的 `tools.deny` 仍优先，suite 只会进一步收紧，不会绕过 profile 限制。
`exec` 这类 tool 本身权限很广：允许它后仍可能经 shell 读写或联网，所以不可信 skill
还需要 Docker 的文件系统和网络隔离。

**推倒重来**（配崩了最快的修法）：

```bash
rm -rf ~/.openclaw-skilleval ~/.openclaw/workspace-skilleval
# 然后从 §0 的 ③ 重新开始
```

---

## 4. env 怎么接：key 放哪、怎么传

**结论：`.env` 是唯一手工维护的真相源，OpenClaw 的 auth store 是从它派生的一份副本。**

```
skillEval/.env                     ← 你只改这里
  DASHSCOPE_API_KEY=sk-xxx
        │
        │  onboard 时一次性注入（QWEN_API_KEY=$DASHSCOPE_API_KEY）
        ↓
~/.openclaw-skilleval/.../openclaw-agent.sqlite   ← OpenClaw 自己存一份，运行时从这读
```

### 为什么不能只靠环境变量

实测：配好后**把 `DASHSCOPE_API_KEY` 从环境里拿掉，OpenClaw 照样能跑** —— 说明 key 已进
它自己的 auth store。这是 OpenClaw 的设计，绕不开，接受它就好。

代价是 key 存了两份。所以要记住：

> **换 key 时，改完 `.env` 必须重跑 §0 的 ④，否则 OpenClaw 还在用旧 key。**

### 变量名对不上的坑

qwen provider 在**运行时**认三个变量（先到先得）：`QWEN_API_KEY` → `MODELSTUDIO_API_KEY` → `DASHSCOPE_API_KEY`。

但 **onboard 只认 `QWEN_API_KEY`**（或显式传 `--modelstudio-standard-api-key <key>`）。
所以 §0 ④ 才要临时映射一下：

```bash
QWEN_API_KEY="$DASHSCOPE_API_KEY" openclaw ... onboard ...
```

用环境变量而不是 `--modelstudio-standard-api-key sk-xxx`，是为了**别让 key 进 shell history**。

### 我们的 adapter 怎么传环境

`adapters/runtimes/openclaw.py` 把整个 `os.environ` 传给子进程，另外做两件事：

- **剥掉 `AWS_*`** 并把 `AWS_SHARED_CREDENTIALS_FILE`/`AWS_CONFIG_FILE` 指向 `/dev/null`
  —— 否则 OpenClaw 会读 `~/.aws/credentials` 去探 Bedrock 模型列表，探不到就往 stderr 刷
  `AccessDeniedException`，又吵又慢
- 有 `node_bin` 时把它的目录 prepend 到 `PATH`（见 §2 坑 A）

---

## 4.4 跑在容器里（Docker Environment Backend）

宿主机装不装 OpenClaw 都无所谓，agent 跑在固定镜像里：

前置：装 Docker（<https://docs.docker.com/get-started/get-docker/>）。build 要联网，
OpenClaw 和 provider 插件都从 npm 官方 registry 拉，地址写在 Dockerfile 头部。

```bash
docker build -f environments/openclaw.Dockerfile -t skilleval-openclaw .
docker image inspect skilleval-openclaw --format '{{.Id}}'   # 贴进 suite 的 environment.image
```

镜像 ID 是**本机内容寻址**的，别人 build 出来的不一样 —— 所以仓库里的示例 suite
不钉任何 image，要走容器就在自己那份 suite 里补上 `environment:`：

```yaml
environment:
  backend: docker
  image: sha256:<把上面 inspect 打印的 ID 贴这儿>   # 必须是 ID，不能用浮动 tag
  # network 不写就是 full（默认给网络）—— 容器里的 agent 要调模型 API。
  # 只有测「断网时 skill 会不会退化」这类题才显式写 network: disabled。
```

```bash
.venv/bin/python -m pipeline plan --suite evals/suites/<你的>.yaml --healthcheck
# 期望：environment=healthy; runtime=healthy (容器内探通)
```

### 坑：onboard 不能放进 Dockerfile

直觉做法是 build 时用占位 key `onboard`，运行时再用真 `QWEN_API_KEY` 覆盖 —— **不行，实测 401**。

`onboard` 会把 key 写进 profile 的 auth store，而 **auth store 优先于环境变量**。
§4「运行时认三个 env 变量」只在 auth store 里*没有记录*时成立。所以：

* 镜像里只装 CLI 和 provider 插件，**不 onboard**（好处：真 key 一层都不进镜像，镜像可随便分发）
* `onboard` 由 adapter 在容器起来后执行，约 **6s/容器**，key 取自容器自己的环境变量

但 `onboard` 又是必须的 —— 它不只存 key，还注册 `qwen/*` 这个**模型命名空间**。
不跑它，容器里只有插件自带的 `qwen-oauth/*`，agent 直接
`FailoverError: Unknown model: qwen/qwen3.5-plus`。

### 凭据怎么进容器

suite 里只写变量名，值从 `.env` 来，容器创建时注入（`docker exec` 会继承，
所以 key 不出现在任何命令行上）：

```yaml
environment:
  backend: docker
  image: sha256:...            # 本地 build 的用 image ID，它同样是内容寻址
  network: full                # agent 要调模型 API，断网跑不了
  env_passthrough: [QWEN_API_KEY=DASHSCOPE_API_KEY]   # 容器里的名字=宿主机的名字
```

改名是必要的：容器里 `onboard` 只认 `QWEN_API_KEY`，而 `.env` 的真相源叫
`DASHSCOPE_API_KEY`（§4「变量名对不上的坑」）。

容器 profile 每次都是全新的，所以 `runtime_options.model` **必须显式写**，
而且它进 `config_hash` —— 这正好解掉本机模式下「模型选择不可追溯」那个遗留。

---

## 4.5 skill 怎么注入（full eval 用）

OpenClaw 默认只看得见自己那 18 个 bundled skill，看不到项目 `subjects/` 里的。下载来的
skill 不装到 `~/.codex/skills`：将精确下载快照放在项目本地、默认忽略的
`subjects/<skill>/vN/`，再由
Environment Backend 对每个 request 复制、注入容器。这样宿主 catalog 不会被评测污染。
adapter 的 `prepared()` 会在 **full 模式**下自动完成注入与还原，你不用手动做 ——
这里说明它做了什么，出问题时好排查。

```
suite 解析出 request.skills
      ↓  复制（不是软链）
/tmp/skilleval-skills-xxxx/{pdf,docx,...}/SKILL.md
      ↓  openclaw config set skills.load.extraDirs '["/tmp/..."]'
OpenClaw 扫描到 → loaded_skills 从 18 变成 24
      ↓  run 结束（含异常路径）
extraDirs 还原 + staging 删除
```

**为什么不用软链**：OpenClaw 默认**跳过**解析到 root 之外的软链（安全策略），
只在日志里写一行，`loaded_skills` 纹丝不动 —— 看起来像注入没写对，其实是被挡了。
真要用软链得配 `skills.load.allowSymlinkTargets` 白名单。

**为什么不写 `<workspace>/skills/`**：那是用户的目录，跑 eval 不该往里塞东西。

**还原是恢复原值，不是无脑 unset** —— 你本来配过 `extraDirs` 的话不会被吃掉。

排查用：

```bash
# 跑之前/之后都应该是「Config path not found」（除非你自己配过）
openclaw --profile skilleval config get skills.load.extraDirs

# 残留检查（正常情况下应该没有）
ls -d /tmp/skilleval-skills-* 2>/dev/null || echo "无残留"
```

> `extraDirs` 的优先级**低于** bundled 和 plugin skill。如果你的 skill 和 OpenClaw
> 自带的重名，会被自带的盖住 —— 目前我们的 6 个（pdf/docx/xlsx/pptx/mcp-builder/
> artifacts-builder）都不重名。

---

## 5. 验证四连

出问题时按顺序往下走，能定位到是哪一层坏的：

```bash
# ① CLI 本身
openclaw --version

# ② 配置合法
openclaw --profile skilleval config validate

# ③ OpenClaw 能自己跑通一轮（绕开 skillEval）
openclaw --profile skilleval agent --local --json \
  --session-id smoke --message "只回 OK"

# ④ skillEval 能接上
.venv/bin/python -m pipeline plan \
    --suite evals/suites/example_full.yaml --healthcheck
```

③ 通了但 ④ 不通 → 问题在 adapter 或 PATH 继承（§2 坑 C），不在 OpenClaw。

---

## 6. 坑表

### 6.1 "配置必须在 TTY 里做" —— 错的

`openclaw configure` 确实要 TTY，但 **`onboard` 有完整的非交互模式**（§0 ④）。
别被向导挡住，也别在 CI 里试图喂 TTY。

顺带一提：`configure` 的菜单里选 **Workspace** 只会配工作目录，**不配任何凭据**。
配完看着"成功"，healthcheck 照样报 `missing-provider-auth`。要找的是 model/provider 那一项。

### 6.2 `config set model.primary` 不足以让模型可用

只设 primary 会报 `Unknown model: qwen/xxx`。模型还需要注册进 `agents.defaults.models`
表里 —— 那是 `onboard` 生成的。**别手写 config 绕过 onboard。**

### 6.3 Gateway 起不来不是问题

我们走 `agent --local`（进程内跑），**不需要 Gateway**。
看到 `ECONNREFUSED 127.0.0.1:18789` 或 `Gateway: not detected` 可以无视，加 `--skip-health` 即可。

### 6.4 session id 只能用 ASCII

带中文或空格会报 `Invalid session ID`。adapter 生成的是
`skilleval-{case_id}-{repeat}`，case_id 按 AUTHORING §1.2 的规范就是 ASCII，天然安全。

### 6.5 输出格式会随版本变 —— 这是"接口错了怎么办"的主战场

**症状最阴险：不报错，但 `selected_skills` 全是空的**，而 `raw_output` 里模型明明答对了。

已知两代格式：

```jsonc
// 2026.7.x
{"payloads": [{"text": "```json\n{\"selected_skills\": [\"pdf\"]...", "mediaUrl": null}],
 "meta": {"durationMs": 6549, "agentMeta": {...}}}

// 更早
{"text": "..."}   // 或 reply / message / content
```

adapter 的 `_extract_text()` 两代都认。**升级 OpenClaw 后如果结果突然全空，先看这里**：

```bash
# 把原始输出打出来，对照 _extract_text 支持的字段
openclaw --profile skilleval agent --local --json \
  --session-id fmt --message "只回 OK" | head -20
```

发现新格式就往 `_extract_text()` 里加一个分支，别改调用方。

> 防呆提示：升级 OpenClaw 会让 `config_hash` 变（版本号在 adapter 的 `fingerprint()` 里），
> 所以跨版本的结果会被自动标记为不可比 —— 这是故意的，别绕过。

### 6.6 升级前后必查

跨版本升级时，先确认 adapter 依赖的 CLI 接口还在：

```bash
for f in --local --json --session-id --message --timeout; do
  openclaw agent --help | grep -q -- "$f" && echo "✓ $f" || echo "✗ $f 没了"
done
openclaw --help | grep -q profile && echo "✓ --profile" || echo "✗ --profile 没了"
```

2026.2 → 2026.7 这五个参数**一个没变**，adapter 零改动。auth store 倒是从
`auth-profiles.json` 换成了 `openclaw-agent.sqlite`，healthcheck 的诊断两代都认。

---

## 7. 换机器迁移清单

**别拷贝 `~/.openclaw-skilleval/`**（里面有 sqlite 凭据和机器相关路径），照 §0 重跑一遍最干净。

需要随身带的只有一样：`.env` 里的 `DASHSCOPE_API_KEY`。

- [ ] node 版本达标（§2）
- [ ] `npm i -g openclaw`
- [ ] 装 qwen provider 插件
- [ ] `.env` 就位，跑 §0 ④ 注入凭据
- [ ] §5 验证四连全绿
- [ ] suite 里 `node_bin` 按需调整（默认 node 合规就注释掉）

---

## 8. 当前配置速查

| 项 | 值 |
| --- | --- |
| OpenClaw | 2026.7.1-2 (0790d9f) |
| node | v24.18.0（nvm default 已设） |
| profile | `skilleval` |
| provider | `qwen`（`@openclaw/qwen-provider`），Standard/Global 端点 |
| 默认模型 | `qwen/qwen3.5-plus` |
| 可用模型 | `qwen/qwen3.6-plus`、`qwen/qwen3-max-2026-01-23`、`qwen/glm-5`、`qwen/glm-4.7`、`qwen/kimi-k2.5` 等 |
| suite | `evals/suites/example_full.yaml`（完整 dataset + subject + tool policy） |

改 OpenClaw 侧用哪个模型：

```bash
openclaw --profile skilleval models set qwen/glm-5
```

> 注意：OpenClaw 侧的模型选择**不在 skillEval 的 suite 里**，因此不进 `config_hash`。
> 做 OpenClaw runtime 的模型对照时，要么在 suite 的 `models[].id` 里写清楚跑的是哪个
> （它进目录名），要么在 DEVLOG 里记一笔 —— 否则事后分不清那批结果用的什么模型。

---

## 9. OpenClaw 侧改动登记

### 当前登记：**空**

**对 OpenClaw 零侵入。** 所有能力都是从外面接的：CLI 调用、`--profile` 隔离、
`skills.load.extraDirs` 注入、环境变量注入。没有改过它一行源码，也没有 fork。

### 规矩（AGENTS.md §29 规则 23）

代码改动一律写在 skillEval。**确实**需要改 OpenClaw 时：

1. **先确认真的接不出来** —— 优先级：CLI 参数 → 配置项 → 插件/provider → 文件注入 → 环境变量。
   `_extract_text()` 那类"它输出格式变了"的问题属于**我们该适配**，不是它该改。
2. 不 fork，不在本机 `node_modules` 里直接改（下次 `npm i -g` 就没了，而且换机器复现不了）。
3. 把改动作为一条记录追加到下面，**一条改动一节**，后续整段粘回上游或做成插件。

### 格式

每条必须包含这几项，缺一项就说明还没想清楚：

```markdown
#### C1 · <一句话说清改什么>

- **目标版本**：OpenClaw 2026.7.1-2（改动依赖的版本，升级后必须复验）
- **目标文件**：`src/xxx/yyy.ts`
- **为什么外面接不出来**：（列出试过的 CLI/配置/插件路径，以及各自为什么不行）
- **改动**：
  ```diff
  - 原代码
  + 新代码
  ```
- **skillEval 侧的临时兜底**：没这个补丁时我们怎么退化（降级跑 / 该能力标 N/A / 直接失败）
- **状态**：本地验证 / 已提 issue / 已提 PR #xxx / 已合入
```

> **兜底那一项不能省。** 补丁没进上游之前，skillEval 必须能在**没有它**的情况下照跑 ——
> 否则等于偷偷 fork 了一个「必须打补丁才能用的 OpenClaw」，换机器就崩，
> 而且违反 §3.1 非侵入式集成。
