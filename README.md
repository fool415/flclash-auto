# FlClash 按需代理启动器

> 中文文档在下方;[English documentation ↓](#english)

> **依赖**：本工具是针对开源客户端 [FlClash](https://github.com/chen08209/FlClash)（GPL-3.0）的第三方命令行调度器，
> 需要本机已安装 FlClash（安装版或便携版均可）。FlClash 本体**不打包在本仓库源码内**，
> 但本仓库的 GitHub Release 会附带官方 Windows 便携版，合规说明见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。

访问网络需要代理时**自动启动 FlClash**,使用期间**并发测速自动切换最快节点**,命令结束后**自动关闭 FlClash**(仅当它是由本工具启动时,按 PID 精确关闭,不误杀其他实例)。

## 快速使用

先克隆仓库并进入目录(以下命令均在仓库目录内执行):

```bash
git clone https://github.com/fool415/flclash-auto.git
cd flclash-auto
```

Git Bash:

```bash
./px.sh curl -sI https://www.google.com     # 任意命令
./px.sh git pull
./px.sh --test                               # 只测速,显示全部节点延迟
./px.sh --status                             # 查看 FlClash/代理状态
./px.sh --buy                                # 打开订阅购买/注册页面(节点需自备,见下节)
```

CMD / PowerShell:

```bat
px.cmd curl -sI https://www.google.com
```

建议加个别名(在 `~/.bashrc` 中):

```bash
alias px="python $(pwd)/flclash_proxy.py"   # 先在仓库目录内执行此行,$(pwd) 会展开成你的实际路径
# 之后任意地方:  px git push
```

## 还没有节点?(购买订阅)

本工具**不提供也不附带任何代理节点/订阅**,它只负责调度你本机已安装的 FlClash。FlClash 里必须有可用的节点
(机场订阅、自建节点等),否则测速/切换无处可选。

如果你还没有订阅,可以用下面的入口自行购买(第三方服务,**非本项目运营**,仅作入口指引)。
表里两家都**已内置邀请码**,`--buy` 打开的是把邀请码带在链接里的注册页,字段会自动填好,不用手输:

| 入口 | 购买/注册页 | 邀请码 | 注册页参数 |
|---|---|---|---|
| `sakura`(SakuraCat,默认) | <https://114514-sakuracat.com> | `10spAT6Z` | `?code=` |
| `kitty`(Kitty Network) | <https://kitty.fo> | `ZVTCCIUS` | `?invite=` |

```bash
./px.sh --buy                 # 默认 sakura:https://114514-sakuracat.com/register?code=10spAT6Z
./px.sh --buy kitty           # kitty:https://kitty.fo/register?invite=ZVTCCIUS
./px.sh --buy sakura          # 显式指定默认入口
export FLCLASH_BUY_URL=https://你的服务商   # 可选:替换默认入口的站点
export FLCLASH_INVITE_CODE=别的邀请码        # 可选:只影响默认入口(sakura)的邀请码
FLCLASH_INVITE_CODE= ./px.sh --buy          # 可选:留空则打开不带 ?code= 的普通注册页
```

> 两家的参数名不一样:sakura 读 `?code=`,kitty 读 `?invite=`(kitty 也兼容 `?code=`)。
> 把邀请码拼到不认的参数名上不会报错,只会静默失效,所以 `--buy` 按入口自动选参数。

**购买后的接入步骤**:

1. 注册并购买套餐,进入面板复制你的**订阅链接**(Clash 格式);
2. 打开 FlClash →「配置」→ 添加订阅(粘贴链接)→ 更新,确认节点出现;
3. 回到终端正常使用 `./px.sh <命令>`,工具会自动测速选最快节点。

> 账号、套餐、支付、退款均由该订阅服务提供,与本项目无关;本工具不读取、不保存任何账号或支付信息,
> `--buy` 只是调用系统默认浏览器打开上面的地址。购买前请自行评估服务条款与风险。
> 用其他订阅服务时把 `FLCLASH_BUY_URL` 指向你自己的购买页即可,`--buy` 与缺节点提示都会跟着变。

> 另外:配置里没有任何真实节点时,`px --status` 会显示「节点数量: 0」并给出购买提示;
> 自动模式测速找不到可切换节点时也会给出同样提示,不再让你对着「切换失败」猜原因。

## 工作流程

```
px <命令>
 │
 ├─ 1. 直连检测(默认探测 https://www.google.com/generate_204,显式绕过系统代理;
 │     generate_204 类地址严格要求 204 应答,防劫持/污染误判)
 │     ├─ 直连可用 → 直接执行命令,不启动 FlClash(直连即最快路径)
 │     └─ 直连失败 ↓
 ├─ 2. 记录系统代理注册表快照(ProxyEnable/ProxyServer/ProxyOverride,停止后还原,
 │     保护你原有的代理设置不被 FlClash 残留覆盖)
 ├─ 3. FlClash 未运行则自动启动(自动探测安装路径;剔除 ELECTRON_RUN_AS_NODE 等干扰
 │     变量;界面已在运行但核心未就绪时等待复用,不重复拉起);已在运行则复用
 ├─ 4. 等待本地代理端口 7890 就绪;若核心未监听(如界面里关了系统代理导致
 │     mixed-port=0),自动 PATCH /configs 打开
 ├─ 5. 通过 Clash API 并发测速全部节点(过滤"剩余流量/到期时间"等信息节点):
 │     global 模式沿 Selector 链展开 GLOBAL(支持 GLOBAL 直接挂子分组的订阅),
 │     逐级切换到延迟最低的节点;rule 模式自动识别主选择组
 ├─ 6. 以 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY=127.0.0.1:7890 执行命令,透传退出码;
 │     Ctrl+C 中断时会先结束子进程再关闭 FlClash
 └─ 7. 命令结束:FlClash 若是本工具启动的 → 按 PID 精确结束进程并把系统代理注册表
        还原到启动前快照;若原本就在运行 → 不做任何改动
```

## 命令一览

| 命令 | 说明 |
|---|---|
| `px <命令...>` | 自动模式(检测→启动→测速→执行→关闭) |
| `px --test` | 测速全部节点并显示表格 |
| `px --status` | 查看 FlClash/核心/端口/系统代理/直连状态 |
| `px --start` | 启动 FlClash 并选最快节点(保持运行) |
| `px --stop` | 强制停止 FlClash 全部实例(并修复系统代理注册表) |
| `px --buy [sakura\|kitty]` | 打开订阅购买/注册页面(自动带邀请码;可指定入口,默认 sakura) |
| `px --keep <命令>` | 执行后保持 FlClash 运行,不自动关闭 |
| `px --force <命令>` | 跳过直连检测,强制走代理(与 `--direct` 互斥) |
| `px --direct <命令>` | 强制直连,不走代理(与 `--force` 互斥) |
| `px --timeout-ms 8000 <命令>` | 单节点测速超时毫秒数(默认 3000) |
| `px --strict <命令>` | 测速/切换失败时中止命令(默认降级为以当前节点执行) |
| `px --no-speedtest <命令>` | 不测速,沿用当前节点 |
| `px --group 节点选择 <命令>` | 指定目标代理组(默认自动判断) |
| `px --canary <URL> <命令>` | 自定义直连探测地址 |

> **命令参数带 `-` 开头的说明**:命令第一个参数之后的 `--xxx` 会原样传给命令(如 `px curl --test` 没问题);仅当命令**首参数**本身以 `-` 开头时,用 `--` 显式分隔:`px -- -my-tool --flag`。

## 配置(环境变量,可选)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FLCLASH_EXE` | `C:\Program Files\FlClash\FlClash.exe` | FlClash 路径 |
| `FLCLASH_API` | `http://127.0.0.1:9090` | Clash 外部控制地址 |
| `FLCLASH_SECRET` | 空 | 外部控制密钥(若设置) |
| `FLCLASH_PORT` | `7890` | 本地混合代理端口 |
| `FLCLASH_CANARY` | `https://www.google.com/generate_204` | 直连探测地址(见下方说明) |
| `FLCLASH_TESTURL` | `https://www.gstatic.com/generate_204` | 节点测速地址 |
| `FLCLASH_START_TIMEOUT` | `45` | 启动就绪等待秒数 |
| `FLCLASH_DELAY_MS` | `3000` | 单节点测速超时毫秒 |
| `FLCLASH_ABORT_ON_PORT_CONFLICT` | 未设置 | 设为 `1` 时,7890 被非 FlClash 程序占用直接中止(默认仅警告) |
| `FLCLASH_BUY_URL` | `https://114514-sakuracat.com` | 默认入口(sakura)的购买地址 |
| `FLCLASH_INVITE_CODE` | `10spAT6Z` | 默认入口(sakura)的邀请码,拼成 `/register?code=<邀请码>`;设为空则不带 |
| `FLCLASH_DEBUG` | 未设置 | 设为 `1` 时输出 API 调用失败详情到 stderr |

## 关于直连探测地址(canary)的选择

直连检测的本质是"**用一个参照站点猜测目标站点是否需要代理**",因此:

- 默认用 google(国内直连不通)适合访问 GitHub/Google/DockerHub 等被墙站点的场景;
- 如果你的目标站点本身国内可直连(如 gitee、npm 淘宝镜像),请用 `--canary https://gitee.com` 或 `FLCLASH_CANARY` 指定一个与目标直连性一致的地址,避免多余地启动代理;
- `generate_204` 类地址要求严格 204 应答,其他地址要求 2xx,可抵御劫持页误判。

## 说明与限制

- 依赖 Python 3(仅标准库;`px.sh`/`px.cmd` 会在找不到 `python` 时回退 `py -3`)。
- 停止策略:命令结束自动关闭时**只结束本工具启动的 PID**;手动 `--stop` 才按进程名全停。FlClashHelperService 服务本身保留,不影响下次启动。
- **并发安全**:多个 `px` 同时运行时通过 `%TEMP%\flclash_proxy.lock` 做存活持有者计数,只有最后一个退出的实例才会关闭 FlClash(持有者按 PID 校验存活,崩溃实例的残留记录会被自动清理)。锁文件本身异常时按保守策略不关 FlClash并提示,可手动 `--stop`。
- 系统代理注册表:启动前快照、停止后还原(ProxyEnable/ProxyServer/ProxyOverride 三项;快照中不存在的项会删除还原)。手动 `--stop` 无快照,仅在残留指向 7890 时关闭开关。
- 复用现有 FlClash 时,若 7890 未监听(如界面里关了系统代理),工具会临时打开该端口并在命令结束后还原(仍有其他实例使用时不还原)。
- 端口占用:7890 被非 FlClash 程序监听时给出警告;设 `FLCLASH_ABORT_ON_PORT_CONFLICT=1` 可改为直接中止。
- 核心进程存在但控制 API 无响应(卡死/残留)时快速失败并建议 `--stop` 清理,不做 45 秒无效等待。
- 启动子进程时会剔除 `ELECTRON_RUN_AS_NODE`,避免从 Electron 系宿主(如某些终端/IDE)调用时 FlClash 静默启动失败。
- 环境变量方式对 `curl/git/pip/npm` 等绝大多数命令行工具生效;不读取环境变量的图形程序请用 FlClash 的系统代理/TUN 模式。
- 购买入口(`--buy` / 缺节点提示)只读地址、只开浏览器,不涉及账号登录、支付或订阅抓取;订阅链接仍需你在 FlClash 里手动导入。
- 内置的两个购买入口只是链接常量(站点 + 邀请码),不含任何账号信息;不想带邀请码可用 `FLCLASH_INVITE_CODE=` 清掉默认入口那一个。

## 换机 / 需要按机器调整的点

| 项 | 说明 |
|---|---|
| FlClash 安装路径 | 默认自动探测 `Program Files` / `Program Files (x86)` / `LOCALAPPDATA\Programs`,不在这些位置时设 `FLCLASH_EXE` |
| 端口 / 控制地址 | `FLCLASH_PORT`(7890)/ `FLCLASH_API`(127.0.0.1:9090)/ `FLCLASH_SECRET`,与 FlClash 内设置保持一致 |
| `~/.bashrc` 别名 | alias 里的脚本绝对路径换成本机实际路径 |
| 注册表路径 | `REG_PATH` 为 Windows 标准系统代理键,无需修改 |

## FlClash 便携版(随 Release 附带)

本仓库的 GitHub Release 会附带 FlClash 官方 Windows 便携版(`FlClash-*-windows-amd64.zip`,GPL-3.0,未做修改)。两种用法任选其一:

1. **解压到脚本旁**:把 zip 解压后,让 `FlClash.exe` 位于本脚本同目录、或同目录下的 `FlClash/` 子目录(或 `FlClash-*/` 目录),脚本会自动探测到,无需额外配置。
2. **任意位置 + 环境变量**:解压到任意目录后,设置 `FLCLASH_EXE` 指向 `FlClash.exe`。

> 首次运行便携版可能触发 Windows SmartScreen「未知发布者」提示,选择「仍要运行」即可。

## 合规声明

本工具以 MIT 许可证发布,见 [LICENSE](./LICENSE)。它调用的 FlClash 客户端以 GPL-3.0 发布,两者是相互独立的软件
(本工具仅启动 FlClash 进程并调用其本地 HTTP API,不构成 GPL 派生作品)。
FlClash 版权、GPL-3.0 全文与再分发说明详见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) 与 [LICENSE-GPL-3.0.txt](./LICENSE-GPL-3.0.txt)。

---

## English

**FlClash On-Demand Proxy Launcher** — a small Windows command-line wrapper around the open-source [FlClash](https://github.com/chen08209/FlClash) client (GPL-3.0). It starts FlClash only when you actually need a proxy, picks the fastest node, runs your command, then closes FlClash again.

> FlClash itself is **not** bundled in this repository's source. The GitHub Release also ships the official Windows portable build, see [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md).

### What it does

1. Tests direct connectivity first — if the site is reachable without a proxy, your command just runs (fastest path).
2. Otherwise starts FlClash, waits for the local proxy port (`7890`), and speed-tests all nodes concurrently through the Clash API.
3. Switches every selector on the path to the lowest-latency node (handles nested groups in `global` mode).
4. Runs your command with `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` pointing to `127.0.0.1:7890` and passes the exit code through.
5. Closes FlClash **only if this tool started it** (by PID) and restores the previous Windows system-proxy registry settings. If FlClash was already running, it is reused and left alone.

### Requirements

- Windows 10/11
- Python 3 (standard library only; `px.sh` / `px.cmd` fall back to `py -3`)
- FlClash installed (installer or portable) — auto-detected under `Program Files`, `Program Files (x86)` or `LOCALAPPDATA\Programs`, or set `FLCLASH_EXE`

### Quick start

```bash
git clone https://github.com/fool415/flclash-auto.git
cd flclash-auto

# Git Bash
./px.sh curl -sI https://www.google.com
./px.sh git pull
./px.sh --test          # speed-test all nodes
./px.sh --status        # show current state
./px.sh --buy kitty     # open a specific entry (default: sakura)

# CMD / PowerShell
px.cmd curl -sI https://www.google.com
```

Optional alias (`~/.bashrc`), so it works from anywhere:

```bash
alias px="python /absolute/path/to/flclash_proxy.py"
```

### Commands

| Command | Description |
|---|---|
| `px <command...>` | Auto mode: probe → start → speed-test → run → close |
| `px --test` | Speed-test all nodes and print a table |
| `px --status` | Show FlClash / core / port / system proxy / direct status and node count |
| `px --start` | Start FlClash, pick the fastest node, keep it running |
| `px --stop` | Force-stop every FlClash instance and repair the system proxy |
| `px --buy [sakura\|kitty]` | Open the subscription purchase/registration page (invite code included; entry selectable, default `sakura`) |
| `px --keep <command>` | Keep FlClash running after the command |
| `px --force <command>` | Skip the direct probe, always use the proxy |
| `px --direct <command>` | Always go direct |
| `px --no-speedtest <command>` | Keep the current node, skip speed-testing |
| `px --group <name> <command>` | Target a specific proxy group |
| `px --canary <url> <command>` | Custom direct-connectivity probe URL |
| `px --timeout-ms 8000 <command>` | Per-node speed-test timeout (default 3000 ms) |
| `px --strict <command>` | Abort if speed-test/switch fails (default: run on current node) |

### No nodes yet? (subscription)

This tool does **not** provide or bundle any proxies/subscriptions — it only drives the FlClash client already on your machine, so FlClash must contain usable nodes.

If you don't have one yet, you can buy a subscription from the third-party providers below (**not operated by this project**; entry link only). Both have an invite code **built in**, and `--buy` opens the registration page with that code already in the URL, so the field is pre-filled:

| Entry | Registration page | Invite code | Query parameter |
|---|---|---|---|
| `sakura` (SakuraCat, default) | <https://114514-sakuracat.com> | `10spAT6Z` | `?code=` |
| `kitty` (Kitty Network) | <https://kitty.fo> | `ZVTCCIUS` | `?invite=` |

```bash
./px.sh --buy                 # default sakura: https://114514-sakuracat.com/register?code=10spAT6Z
./px.sh --buy kitty           # kitty: https://kitty.fo/register?invite=ZVTCCIUS
./px.sh --buy sakura          # explicit default entry
export FLCLASH_BUY_URL=https://your-provider   # optional: replace the default entry's site
export FLCLASH_INVITE_CODE=another             # optional: default entry (sakura) only
FLCLASH_INVITE_CODE= ./px.sh --buy             # optional: plain register page, no ?code=
```

> The two providers use different parameter names: sakura reads `?code=`, kitty reads `?invite=` (kitty also accepts `?code=`). Putting a code on a parameter name the site ignores fails silently, which is why `--buy` picks the right one per entry.

After purchase: copy your Clash subscription URL → FlClash → **Profiles** → add profile → update → then use `./px.sh <command>` again.

Accounts, plans, payment and refunds are handled by that provider; this tool reads and stores no credentials. When your config contains zero real nodes, `px --status` prints `节点数量: 0` and both auto mode and `--status` show the purchase/import hint instead of a bare "switch failed".

### Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `FLCLASH_EXE` | `C:\Program Files\FlClash\FlClash.exe` | Path to `FlClash.exe` |
| `FLCLASH_API` | `http://127.0.0.1:9090` | Clash external-controller address |
| `FLCLASH_SECRET` | empty | External-controller secret, if set |
| `FLCLASH_PORT` | `7890` | Local mixed proxy port |
| `FLCLASH_CANARY` | `https://www.google.com/generate_204` | Direct-connectivity probe URL |
| `FLCLASH_TESTURL` | `https://www.gstatic.com/generate_204` | Node speed-test URL |
| `FLCLASH_START_TIMEOUT` | `45` | Seconds to wait for FlClash to be ready |
| `FLCLASH_DELAY_MS` | `3000` | Per-node speed-test timeout (ms) |
| `FLCLASH_ABORT_ON_PORT_CONFLICT` | unset | `1` = abort if port 7890 is held by a non-FlClash process |
| `FLCLASH_BUY_URL` | `https://114514-sakuracat.com` | Purchase page of the default entry (`sakura`) |
| `FLCLASH_INVITE_CODE` | `10spAT6Z` | Invite code of the default entry (`sakura`), appended as `/register?code=<code>`; set empty to disable |
| `FLCLASH_DEBUG` | unset | `1` = print API error details to stderr |

### Notes & limitations

- Node selection walks the whole selector chain; `rule` mode detects the main selector group automatically.
- Concurrency: parallel `px` runs share `%TEMP%\flclash_proxy.lock` with liveness-checked owners, so only the last exiting instance closes FlClash.
- The system proxy registry keys (`ProxyEnable` / `ProxyServer` / `ProxyOverride`) are snapshotted before start and restored afterwards.
- If FlClash is already running with `mixed-port = 0`, the tool temporarily enables the port and restores it when done.
- `ELECTRON_RUN_AS_NODE` is stripped from the child environment so FlClash doesn't silently no-op when launched from an Electron host.
- Environment-variable proxying works for `curl`, `git`, `pip`, `npm`, etc.; GUI apps that ignore env vars need FlClash's system-proxy or TUN mode.
- `--buy` only opens a URL in your default browser; it never logins, pays, or fetches your subscription.
- The two built-in entries are just link constants (site + invite code) — no credentials. Clear the default one with `FLCLASH_INVITE_CODE=`.

### License

MIT — see [LICENSE](./LICENSE). FlClash is GPL-3.0; this tool only launches it and talks to its local HTTP API, so the two are independent works. See [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) and [LICENSE-GPL-3.0.txt](./LICENSE-GPL-3.0.txt).
