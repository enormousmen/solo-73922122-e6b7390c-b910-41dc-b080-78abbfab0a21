# OCI 镜像末两层合并服务 · 非 root 入口核对

运行节点已经缓存了基础层，发版时只需下发补丁。本服务把单平台镜像的**末两层补丁合并成一层**，写入本地 OCI 镜像布局（OCI image layout），并以新标签发布，从而减少下发层数。

部署准备改用非 root 用户后，新增 `POST /verify-entrypoint`：给定 **OCI 标签、非零 UID 和单个 GID**，按叠加后的层元数据核对镜像**默认命令的入口文件**是否真的能被该用户执行，并在失败时给出工作目录或每个候选路径上**第一个受阻的位置与原因**。

## 功能与约束

### 层合并（`POST /merge`）

- 输入：本地 OCI 布局中的**原标签**与**新标签**（`POST /merge`）。
- 只处理**单平台**镜像（`index.json` 中标签直接指向 image manifest；manifest list/index 会被拒绝）。
- 末两层必须是**未压缩 tar**，且只含普通文件、目录、普通 whiteout（`.wh.<name>`）、opaque whiteout（`.wh..wh..opq`）；其他条目类型（符号链接、硬链接、设备文件等）会报错拒绝，不会静默产生错误结果。
- 合并语义（与 overlay 应用语义一致）：
  - 删除（whiteout）与目录遮蔽（opaque whiteout）仍然作用于**更低层**——合并层里保留对应的 whiteout / opaque 标记；
  - 同一层在遮蔽目录后新增的文件照常保留；
  - 上层条目覆盖下层同路径条目；下层被遮蔽/删除的条目不会泄漏进合并层。
- **基础层只取已有元数据**：新清单直接沿用基础层的 descriptor（摘要不变），从不读取基础层内容。
- **资源占用**：文件正文全程流式处理（两遍扫描：第一遍只读 tar 头建元数据索引，第二遍从源 blob 顺序拷贝数据写入新层），内存占用不随文件正文大小增长；磁盘上除新层 blob 外只暂存元数据（内存中的条目索引），不产生其他中间文件。
- **清单与配置一致**：新 manifest 的 layers = 基础层 descriptors + 合并层 descriptor；新 config 的 `rootfs.diff_ids` 与 history 同步收缩，`config` 段（Cmd/Entrypoint/Env/WorkingDir/User 等启动配置）原样保留。未压缩层的 diff_id 等于 blob 摘要。
- **失败安全**：所有 blob 先写临时文件、fsync 后原子改名；`index.json` 永远最后更新。任何一步失败，原标签保持可用，新标签不会发布；临时文件会被清理。

### 非 root 入口核对（`POST /verify-entrypoint`）

- 入参只有 **标签 `tag`、非零正整数 `uid`、单个整数 `gid`**（不支持/不接受附加组列表；`uid=0` 会被拒绝，因为 root 会绕过权限检查）。
- **工作目录**取 `config.WorkingDir` 的绝对路径；空值/缺省按 `/`；相对值锚定到根。
- **命令入口**取 `config.Entrypoint[0]`；`Entrypoint` 为空（或缺首项）时取 `config.Cmd[0]`。
- **候选路径解析**：
  - 绝对路径（`/x`）直接查；
  - 含斜杠的相对路径（如 `bin/run`、`./run.sh`）从 `WorkingDir` 解析；
  - 裸命令（如 `serve`）按镜像 `Env.PATH` 的段顺序查找；**空 PATH 段表示工作目录**；相对 PATH 段也从 `WorkingDir` 解析；
  - 所有路径规范化后**不含 `..`**，出现即按非法镜像配置拒绝（422）。
- **判定顺序**：先逐级检查 `WorkingDir` 能否进入（链上每个目录都要有搜索/执行位），再按候选顺序返回**第一个**「逐级可穿过目录、且自身是有执行位的普通文件」的路径。工作目录不可进入时整体结论为不可启动（即使绝对路径候选本身可执行——运行时仍会先 `chdir`）。
- **权限只匹配并使用一类**：`uid` 等于 inode 属主 → 只看属主位；否则 `gid` 等于 inode 属组 → 只看属组位；否则 → 只看其他位。三类不叠加、不回退（例如属主命中但属主 `x` 关闭时，即使 group/other 有 `x` 也拒绝）。
- **以叠加结果为准**：从底到顶扫全部层，同名条目上层覆盖；普通 whiteout（`.wh.<name>`）只遮蔽低层（同层/更高层重建不受影响）；opaque whiteout 只清空某目录的低层内容、目录本身保留；文件条目会顶替同名目录子树。没有显式目录条目、仅由文件路径隐含出的目录，按解包器默认 **`0755`、`UID/GID=0`** 处理。
- **受阻诊断**：每个候选与工作目录都给出 `blocked_at`（第一个受阻的绝对路径）、`code`/`errno`/`reason`（含中文 `reason_zh`）以及逐级 `trace`；`code` 取值 `not_found (ENOENT)`、`permission_denied (EACCES)`、`not_a_directory (ENOTDIR)`、`not_a_regular_file`。
- **资源约束**：沿用现有未压缩 tar 与受支持条目类型（普通文件、目录、两种 whiteout，其余类型报 422）；**每层 tar 头至多扫一遍**，随机访问模式下靠 `tarfile` 跳转**跳过普通文件正文**；内存中**只保留工作目录、候选及其祖先**这些关键路径的元数据（外加少量 tombstone/可见性标记），不随镜像文件数或文件正文大小增长。

## 目录结构

```
app/
  main.py         FastAPI 入口（/merge、/verify-entrypoint、/tags、/healthz、/docs）
  service.py      合并编排：解析标签、重写 config/manifest、原子发布
  merge.py        两层 tar 合并引擎（whiteout / opaque 语义）
  entrypoint.py   非 root 入口核对：候选解析、定向叠加元数据、权限/受阻诊断
  verify.py       entrypoint 的兼容性别名模块
  oci.py          OCI 布局读写原语（摘要寻址、原子写入）
tests/            pytest：合并语义、入口核对、API、失败原子性、内存上界
examples/
  make_sample_image.py      构造三层合并示例镜像（顶层含 opaque + 新增文件）
  verify_equal.py           展开两个标签的文件树并逐路径比对
  make_entrypoint_demo.py   构造入口核对演示（1.0 首选 / 1.1 上层改权后改选 / wd）
  demo.sh                   一键演示合并（Docker Compose 或 DIRECT=1 直跑）
  demo_entrypoint.sh        一键演示入口核对与“改权后改选”
Dockerfile / docker-compose.yml   以非 root 用户 app(uid/gid 可构建参数化) 运行
```

## 运行

```bash
# 准备示例镜像（三层未压缩 tar，标签 demo:1.0.1）
python3 examples/make_sample_image.py --force oci-layout

# 启动服务（Docker Compose，监听 8080）
docker compose up -d --build

# 合并末两层：demo:1.0.1 -> demo:1.0.1-squashed
curl -X POST http://localhost:8080/merge \
  -H 'Content-Type: application/json' \
  -d '{"source_tag":"demo:1.0.1","target_tag":"demo:1.0.1-squashed"}'

# 校验新旧镜像展开后完全一致（路径/内容/权限/属主）
python3 examples/verify_equal.py oci-layout demo:1.0.1 demo:1.0.1-squashed
```

没有 Docker 的环境可以直跑：`DIRECT=1 examples/demo.sh`（一键完成上述全部步骤）。

### 入口核对演示（上层目录改权后改选入口）

```bash
# 构造 entrydemo:1.0 / entrydemo:1.1 / entrydemo:wd 三个标签
python3 examples/make_entrypoint_demo.py --force oci-layout

# 以非 root 容器用户启动（Compose 会用 APP_UID/APP_GID 对齐宿主机绑定挂载）
APP_UID=$(id -u) APP_GID=$(id -g) docker compose up -d --build
# 或无 Docker：DIRECT=1 examples/demo_entrypoint.sh
```

打开 <http://localhost:8080/docs>，找到 `POST /verify-entrypoint` → **Try it out**，
依次用下面两个请求执行，可直观看到“上层把 `/opt/bin1` 改成 `0700 root:root` 后，
入口从 `/opt/bin1/serve` 改选为 `/opt/bin2/serve`”：

```json
{"tag": "entrydemo:1.0", "uid": 1000, "gid": 1000}
```

```json
{"tag": "entrydemo:1.1", "uid": 1000, "gid": 1000}
```

第三个标签演示工作目录优先拦截：

```json
{"tag": "entrydemo:wd", "uid": 1000, "gid": 1000}
```

命令行一键版：`examples/demo_entrypoint.sh`（默认 Docker Compose；`DIRECT=1` 直跑 uvicorn）。

> 容器本身也以非 root 用户运行：`Dockerfile` 创建 `app`（默认 `1000:1000`，
> 可用构建参数 `APP_UID/APP_GID` 覆盖）并设置 `USER app:app`；`docker-compose.yml`
> 用 `user: "${APP_UID:-1000}:${APP_GID:-1000}"` 让绑定挂载的 `oci-layout/` 可读写。

## API

### `POST /verify-entrypoint`

请求体（规范字段 `tag` / `uid` / `gid`；同时兼容 `image`、`source_tag`、
`user_id`、`group_id` 等别名字段，以及 `{"user":{"uid":..,"gid":..}}` 嵌套写法；
另有等价的 `GET /verify-entrypoint?tag=&uid=&gid=`）：

```json
{"tag": "entrydemo:1.1", "uid": 1000, "gid": 1000}
```

可启动时 `200` 响应（节选）：

```json
{
  "ok": true,
  "status": "runnable",
  "entrypoint": "/opt/bin2/serve",
  "command_source": "entrypoint",
  "command": ["serve", "--host", "0.0.0.0", "--port", "8080"],
  "working_dir": {"path": "/app", "ok": true, "code": null},
  "candidates": [
    {"path": "/opt/bin1/serve", "ok": false,
     "blocked_at": "/opt/bin1", "code": "permission_denied",
     "matched_class": "other"},
    {"path": "/opt/bin2/serve", "ok": true, "matched_class": "other"}
  ]
}
```

不可启动时 HTTP 仍为 `200`，但 `ok=false`、`entrypoint=null`，并给出
`working_dir`、每个 `candidates[*]` 的首个受阻点，以及拍平的 `failures` /
`first_block`：

```json
{
  "ok": false,
  "status": "working_dir_blocked",
  "entrypoint": null,
  "blocked_at": "/app",
  "code": "permission_denied",
  "reason": "permission denied: /app (...)",
  "failures": [
    {"scope": "working_dir", "target": "/app",
     "blocked_at": "/app", "code": "permission_denied", "errno": "EACCES"}
  ]
}
```

状态码约定：核对结论（可执行/不可执行）本身用 `200` + `ok` 表达；
`400` 为 `uid` 为 0/负等非法入参，`404` 标签不存在，`422` 镜像/层无法解释
（多平台、压缩层、非法 tar 条目、路径含 `..` 等），`500` 布局损坏。


### `POST /merge`

请求：

```json
{"source_tag": "demo:1.0.1", "target_tag": "demo:1.0.1-squashed"}
```

响应 `200`：

```json
{
  "source_tag": "demo:1.0.1",
  "target_tag": "demo:1.0.1-squashed",
  "manifest_digest": "sha256:…",
  "config_digest": "sha256:…",
  "merged_layer": {"digest": "sha256:…", "diff_id": "sha256:…", "size": 2048,
                   "mediaType": "application/vnd.oci.image.layer.v1.tar"},
  "layers": ["sha256:…(base)", "sha256:…(merged)"],
  "base_layers_kept": 1,
  "layers_merged": 2
}
```

错误码：`400` 原新标签相同；`404` 原标签不存在；`409` 新标签已存在；
`422` 不支持的输入（多平台、层数不足、层被压缩、非法 tar 条目等）；`500` 布局损坏等。

### `GET /tags` / `GET /healthz`

列出布局中的标签 / 健康检查。

## 演示场景说明

示例镜像顶层（patch2）包含 `app/data/.wh..wh..opq`（遮蔽 base 与 patch1 写入
`/app/data` 的全部内容）以及同层新增的 `app/data/fresh.txt`。合并后：

- 合并层仍含 `app/data/.wh..wh..opq` —— 遮蔽继续作用于基础层；
- `app/data/fresh.txt` 照常保留；
- patch1 的 `junk.txt`、`tmpdir/` 等被遮蔽内容不会进入合并层；
- `etc/.wh.old.cfg` 的删除语义保留（即使 base 里也有该文件，展开结果仍一致）。

`examples/verify_equal.py` 会把新旧两个标签的所有层分别展开到内存虚拟文件系统，
逐路径比对类型、内容摘要、权限位与 uid/gid。

## 测试

```bash
python3 -m pytest        # 需要 fastapi/uvicorn/pytest/httpx
```

覆盖：opaque 遮蔽后新增、whiteout 各类组合、元数据/属主保留、32MiB 文件的内存上界
（<16MiB）、API 全流程、失败时不发布新标签且原标签可用、临时文件清理等。

入口核对（`tests/test_entrypoint.py`，40+ 用例）覆盖：Entrypoint→Cmd 回退、
空 `WorkingDir` 与相对 `WorkingDir`、绝对/含斜杠相对/裸命令三类解析、PATH 顺序与
空段=工作目录、属主/属组/其他三类“只用一类”的权限判定、逐级 `blocked_at`
（`not_found`/`permission_denied`/`not_a_directory`/`not_a_regular_file`）、
工作目录优先拦截、上层 chmod/chown 改权改选、whiteout/opaque/重建、隐式目录
`0755 root:root`、定向元数据保留与 32MiB 文件正文零加载的内存上界，以及 HTTP
别名/404/400/422。

## 限制与说明

- 仅单平台镜像；多平台（manifest list）会被 422 拒绝。
- 末两层必须是未压缩 tar（gzip/zstd 层会被拒绝，含魔数嗅探）；入口核对沿用同一套
  未压缩层与条目类型约束。
- 层内只支持普通文件、目录与两种 whiteout；符号链接/硬链接/设备等条目会报 422。
- 权限模型只按给定的**单个 `gid`** 匹配属组，不考虑附加组；不区分 setuid/setgid
  位，只检查相应的执行位；也不检查脚本解释器/读位等 `execve` 之外的条件。
- 镜像 `Env` 完全没有 `PATH` 时，回退到常见默认值
  `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`；若显式设置了空
  `PATH=`，则按 POSIX 规则得到一个空段，即只查工作目录。
- 单进程内用锁串行化合并；多副本部署需自行协调对同一布局的并发写入。
