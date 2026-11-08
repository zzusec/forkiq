# ForkIQ

管理自己的 GitHub fork，让它们跟上上游；同步失败必须看得见。

ForkIQ 是一个自托管的单人面板：用 GitHub App 连接账号、自动发现你的 fork、按仓库决定「镜像」或「手工 fork」同步方式，并把每一次同步结果写进事件记录。

## 功能

- **自动发现**：安装 GitHub App 后扫描账号下的 fork，一小时一次，也可手动触发。
- **镜像同步**：通过异常删除检测后与上游对齐；fork 不留自有提交，上游改写历史时也先检测再对齐。要保留自己的改动请用手工 fork 模式，或另建仓库。
- **手工 fork 同步**：保留 fork 自有提交，用服务端 merge 把上游更新合进来。
- **版本对比**：每个仓库同时显示本地与上游的最新 tag、短 SHA、提交日期，并直接回答两边是不是同一个版本：同一版本 / 本地领先 N 个提交（上游更新已全部并入，差异来自 fork 自有提交）/ 落后上游 N 个提交 / 与上游分叉 / 检查失败。
- **上游消失不删本地**：上游被删除或改名时只标记待处理，fork 与历史原样保留。
- **异常删除保护**：默认拦截空文件树、超过阈值的删除与无法完成的检测；保留 fork，展示目标提交，等待单次确认。
- **自更新保活**（可选）：上游 Actions 静默超过 24 小时时，触发 fork 自己的 workflow。
- **统计下钻**：概览五项数字可点击进入对应仓库列表，待同步与需关注使用一致的分组筛选。
- **操作可追溯**：每次检查、合并、阻塞、错误都进事件流；列表保留状态，异常全文在详情浮层或仓库详情查看，不撑坏表格。
- **后台刷新**：只替换数据区域，revision 未变化直接返回 204，跳过内容构建与渲染；阅读详情或编辑表单时不打断。带内容哈希的 CSS/JS 使用长期缓存。

## 同步频率与新 fork

- **已受管仓库**：按设置页的「上游更新检查间隔」检查，单位为分钟，范围 0.25–5；例如 1 表示每轮完成后等待 1 分钟再检查，有更新就尝试同步。已有设置保留，数据库仍按秒存储。
- **新 fork 自动发现**：每 60 分钟扫描 GitHub App 有权访问的仓库，在到期后的下一轮检查执行。发现后加入管理，并在同一轮开始检查同步；不会再等待一个发现周期。
- **手动加入**：不必逐个填写已开启自动发现的仓库。若不想等待，可在概览点「发现新 fork」，或粘贴已有 fork 链接后立即安排首次检查。这里不会创建 GitHub fork。关闭自动发现仍可手动发现，不影响已有仓库同步。
- **授权与完成时间**：App 使用「仅选定仓库」时，新 fork 必须先纳入授权范围。检查间隔不是同步完成时限，网络、限流、权限或合并冲突可能导致延后。
- **错误恢复**：镜像遇到目标提交对象缺失时刷新上游提交并有界重试一次；只有明确非快进才按镜像规则强制对齐，成功必须读回验证。持续错误保留记录并约 10 分钟后重试；权限与冲突不自动绕过。已知错误先显示中文说明及建议，原始响应可展开查看。

## 异常删除保护

默认开启，适用于镜像快进、历史改写后的强制对齐和手工 fork 合并。删除文件数超过 **100 个**或删除比例超过 **50%**，任一命中即停止此次更新并归入「需关注」；上游目标文件树为空始终拦截，即使文件数阈值未达到。无法读取完整文件树（包括 GitHub 截断大树）、缺少手工合并的共同祖先或 API 出错时，也不执行更新。

在「系统设置 → 异常删除保护」修改默认开关与阈值；仓库详情可单独开启、关闭或覆盖阈值，留空的阈值跟随系统。数量阈值范围 0–1000000，比例范围 0–100%；等于阈值不拦截，超过才拦截。关闭保护允许清空内容和大量删除，请谨慎使用。

仓库详情显示删除文件数、占 fork 当前文件数的比例、上游剩余文件数和完整目标提交。确认内容无误后点击「确认并同步此次变更」：仅放行该目标提交的一次尝试，fork 基线、分支、模式或保护规则变化会使确认失效。上游目标变化必须重新检测和确认；「立即同步」「恢复」不会绕过保护。执行失败后再次尝试危险变更也需要重新确认。

镜像模式统计 fork 当前路径中目标树不再包含的文件。手工合并只计共同祖先存在、目标上游删除且 fork 仍保留的文件，不把 fork 独有文件算成删除；实际合并固定为检测过的提交 SHA，不跟随移动分支。符号链接与子模块各计一个文件，重命名保守地按旧路径删除计算；同路径文件内容被清空不在本检测范围内。删除检测与服务器备份相互独立；检测不拦截 GitHub 上的直接修改或 Actions 自行写入。服务器快照按下节规则保留，重要项目仍建议异地备份。

## 服务器备份与回滚

- **自动快照**：首次检查（包括暂停的仓库）建立当前默认分支基线；每次真正更新 fork 前备份当前提交。同一提交复用已验证快照，不因 5 分钟轮询重复下载。无法读取仓库的初次备份需等权限或网络恢复。
- **保留规则**：文件在 `$FORKIQ_STATE_DIR/backups/`（默认 `/var/lib/forkiq/backups/`），每仓库保留最近 **3 个不同版本**的成功快照，不足 3 个按实际数量显示。新快照完整验证后才淘汰旧快照；失败不更新 fork，约 10 分钟后重试。关闭删除保护也不跳过备份。
- **手动清理**：仓库详情的每份备份都有下载、回滚和删除操作。删除需要管理员确认，并在回滚使用该备份时自动阻止；删除只移除服务器快照，不影响 GitHub 仓库。保留策略仍会自动保留最近 3 个不同版本。
- **异常上游**：删库、不可访问时不修改 fork；清空文件树、异常大量删除或检测失败仍先拦截，并归入「需关注」、记录事件。已有备份不表示自动放行，手动确认危险变更也必须有当前基线的有效备份。
- **页面回滚**：进入「仓库详情 → 服务器备份 → 回滚到此版本」，核对分支、当前提交与目标提交后确认。后台先备份回滚前状态，再用精确提交和 HEAD 租约覆盖本仓库的指定分支，不改上游或其他分支。执行后自动同步保持暂停，需手动恢复；GitHub Actions 不会随之禁用。
- **失败可见**：备份损坏、权限不足、分支保护或确认后分支被其他人更新时不强行覆盖。推送超时或结果未确认时保持暂停，不自动重试；先核对 GitHub 分支，再决定操作。回滚排队和执行期间不能恢复或修改同步方式。
- **完整性边界**：每份是自包含 Git bundle，保存所示提交的全部文件和所有可达祖先历史，非增量包。它不是整个 GitHub 项目的全量备份：不额外保存其他分支、不可达提交、Git LFS 的实际对象、子模块仓库内容、Issues、Actions 数据或配置。超过保留数会淘汰旧快照；服务器损毁仍会丢失本机备份，请另存异地副本。

备份下载和回滚都需要管理员会话。可下载 bundle 离线检查或恢复，不依赖上游继续存在：

```sh
git clone ./owner-repo-<sha>.bundle recovered
cd recovered
git switch preserved
git log --oneline
```

若 GitHub fork 本身也已删除，面板不会自动重建远端；仍可从下载的 bundle 恢复为独立仓库，再自行推送到新仓库。

## 快速开始

需要 Python 3.10+、Git 2.30+（服务器备份与回滚使用），以及一个公网可访问的域名（GitHub App 回调要用）。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export FORKIQ_STATE_DIR="$PWD/state"      # 数据库与 App 私钥（默认 /var/lib/forkiq）
export FORKIQ_PEM_PATH="$FORKIQ_STATE_DIR/app.pem"
export FORKIQ_PUBLIC_URL="https://fork.example.com"  # 改为你自己的公网地址
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8792
```

首次打开会跳转 `/setup` 设置管理员密码，然后到 `/github/connect` 创建 GitHub App。页面上的按钮会带着预填配置跳到 GitHub，授权后回调自动写入 App id 与私钥，再点「检测」绑定 installation。

创建 App 前必须设置 `FORKIQ_PUBLIC_URL`，推荐使用 HTTPS；只接受根地址，可带端口和末尾 `/`，不支持子路径、账号、查询参数或片段。所有 manifest 地址都来自此配置，不使用请求的 Host 或转发头，也不会指向其他 ForkIQ 实例。缺失或无效时页面会阻止创建并提示修正；已有 App 的安装、检测不受影响。

安装未完成或提前点击了检测，可随时回到「系统设置 → GitHub 连接」或 `/github/connect`，打开安装页后重新检测，无需修改数据库或重建 App。

升级不会自动修改 GitHub 上已有 App 的地址。若旧版本曾把一次性授权码发到非本人实例，建议删除误建的 App，在正确地址重新创建；仅修改环境变量不会撤销可能已被兑换的凭据。

App 需要的权限：`contents: write`、`administration: write`、`workflows: write`、`actions: write`、`metadata: read`。不需要 webhook 事件。

## 部署

`deploy/bootstrap.sh` 在干净的 Linux 主机上创建 `forkiq` 服务账号与目录，`deploy/forkiq.service` 是 systemd 单元（只监听 `127.0.0.1:8792`，由同机 HTTPS 反向代理访问）。把代码放到 `/opt/forkiq`、装好依赖并安装 service 单元后，用 `systemctl edit forkiq` 配置自己的地址：

```ini
[Service]
Environment=FORKIQ_PUBLIC_URL=https://fork.example.com
```

然后启动服务；修改已有服务的地址时，执行 `systemctl restart forkiq` 使环境变量生效：

```sh
systemctl daemon-reload && systemctl enable --now forkiq
```

面板不做 TLS，请在反向代理终止 HTTPS 并传下 `X-Forwarded-Proto`，否则登录 Cookie 不会带 `Secure`。

## 版本与发布包

[GitHub Releases](https://github.com/zzusec/forkiq/releases) 提供每个正式版本的部署包：

- `forkiq-vX.Y.Z.tar.gz` / `forkiq-vX.Y.Z.zip`：同一份代码，包含应用、依赖清单、部署示例、测试与文档；安装时仍需创建 Python 虚拟环境并安装依赖。
- `SHA256SUMS`：两个部署包的 SHA-256 校验值；包内 `release.json` 记录版本、提交与逐文件校验值。
- 只打包 Git 已提交的代码，不含数据库、GitHub App 私钥、虚拟环境或本地配置。升级时保留 `/var/lib/forkiq`、现有虚拟环境与实际 systemd/nginx 配置；不要重跑初始化脚本。

发布一个版本时，先推送代码，再推送对应标签：

```sh
git push origin main
git tag -a v1.0.0 -m "ForkIQ v1.0.0"   # 后续版本使用新的标签，例如 v1.0.1
git push origin v1.0.0
```

GitHub Actions 会先运行 Python 与 JavaScript 回归测试，通过后自动打包并创建 Release。普通代码推送不创建 Release；`v1.1.0-rc.1` 这类标签会标为预发布。已发布版本不应移动标签或覆盖文件，修复请发布新版本。

也可在 Git 仓库中打包指定提交（需 Git 与 Python 3.10+），输出位于 `dist/`：

```sh
python3 deploy/package.py v1.0.0 --ref v1.0.0
cd dist && shasum -a 256 -c SHA256SUMS
```

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 引擎与 Web 层
node --test tests/test_ui.js                                    # 前端脚本
```

`tests/test_engine.py` 用脚本化的假 GitHub 客户端覆盖同步状态机；`tests/test_web.py` 走 FastAPI TestClient 渲染真实模板；`tests/test_ui.js` 在隔离的浏览器原语里执行 `app/static/app.js`。

## 结构

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | 路由、会话与 CSRF、模板渲染、后台刷新片段 |
| `app/engine.py` | 同步引擎与后台 Worker |
| `app/github.py` | GitHub App / REST / GraphQL 客户端（标准库 + PyJWT） |
| `app/db.py` | SQLite（WAL）存储与增量迁移 |
| `app/versions.py` | 本地与上游版本对比判定 |
| `app/templates/` | Jinja 模板（含 `partials/` 刷新片段） |
| `app/static/` | 一份 CSS、一份脚本、内联图标 sprite |
| `PRODUCT.md` / `DESIGN.md` | 产品与设计约束（各自附同名 HTML 版本） |

## 说明

- 单人使用：一个管理员密码、一个 GitHub installation，不做多租户。
- 密码用 scrypt 加盐哈希；所有写操作校验 CSRF；会话 7 天过期。
- 上游被删除、改名、默认分支不一致时一律停止自动推送并提示，不猜也不覆盖。

## Infrastructure Partners

Test runners and the release mirror run on servers provided by [BlueServers](https://blueservers.com/) through its social support program.

## 许可

MIT，见 [LICENSE](LICENSE)。
