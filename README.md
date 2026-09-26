# 国际课程质量证据链

面向合作院校学期评审的纯后端服务：用**内容指纹**与**版本关系**保存课程
大纲、师资、考核与企业反馈，让“某次评审当时看到了哪一版材料”可被证明；
评审决定固定到一组封存清单；后补文件只能触发新的复审请求；敏感企业反馈
按机构与角色最小披露。

仅依赖 Python 3.11 标准库（`http.server` / `sqlite3` / `zoneinfo`）。

## 设计要点

### 证据如何固定
- 每份材料字节以 **SHA-256 内容寻址**（`blobs` 表按摘要去重），上传时
  服务端重算摘要，客户端可传 `expected_sha256` 做端到端校验。
- 同一逻辑材料有不可变的版本链（`versions.supersedes_version_id`），
  重复上传相同字节返回既有版本。
- 评审包封存时，对“包内每个条目的 material/version/sha256/kind/敏感度
  + 封存时刻”做规范化 JSON 哈希，得到 **manifest_fingerprint**。封存后
  再上传新版本或撤回材料都**不改变历史包**。
- 签发结论时对全部评审请求与异议再哈希，得到 **review_fingerprint**，
  其中嵌入 manifest_fingerprint，形成证据链。

### 后补文件只能复审
- 已封存/已决定的包拒绝追加材料（`409 immutability_violation`）。
- 后补材料走“复审包”：`supersedes_package_id` 指向旧包；旧包中**未撤回**
  的条目自动带入，已撤回条目不复制；旧包仅在 `decided` 后允许派生复审。

### 材料撤回
- 版本/材料撤回是追加标记，不删除任何已封存引用（历史可证）。
- 撤回的版本不能进入新包、不能被复审包复制；离线核验会把
  “封存清单引用了事后撤回版本”列为**警告**而非完整性失败。

### 最小披露（敏感企业反馈）
- 跨机构用户默认不可见；审计可跨机构只读。
- 敏感反馈：本机构管理员可见，本机构提交人不可见；评审人仅对**当前仍有效
  分配**（pending/accepted/completed）所在包可见；请求取消或拒绝后立即
  失权；角色调整在下次请求鉴权时即时生效。
- 无权限者看到的清单条目不返回摘要（避免内容指纹本身泄露）。

### 并发、幂等与恢复
- 所有写用例在 `BEGIN IMMEDIATE` 事务内执行；状态推进使用条件 UPDATE
  （`WHERE status = expected`），并发分配/签发下只有一方推进，另一方回放，
  已用多连接线程测试验证。
- 写接口接受 `Idempotency-Key` 头；服务端以唯一键记录首次结果，重试/超时
  重发安全，失败回滚后可用同键重试。
- 截止时间以“当地时间 + IANA 时区”输入，统一换算为 UTC 绝对时刻，正确
  处理跨时区与日界线。

## 分层结构

```
service_09252_006/
  domain/        实体、枚举、错误、指纹纯函数、披露策略
  application/   用例服务（证据/评审包/评审）、端口（Clock、Id、Repository）
  persistence/   SQLite 仓库（事务、条件迁移、幂等键、内容寻址）
  api/           HTTP 边界（Bearer 鉴权、路由、JSON 编解码）
  cli.py         serve / 离线 verify
```

时间与标识经可替换端口接入（`SystemClock`/`FixedClock`、
`Uuid4IdGenerator`/`SequentialIdGenerator`），测试可确定性复现。
运行数据（SQLite 文件）路径由调用方提供，不写入源码目录。

## 运行

```bash
# 数据库文件放在源码目录之外
python3 -m service_09252_006.cli serve \
  --db ./data/qe.db --host 127.0.0.1 --port 8080 \
  --bootstrap-token "$BOOTSTRAP_TOKEN"
```

## 离线完整性核验

不需要服务进程，只读打开数据库，重算全部内容摘要、封存清单指纹与评审
记录指纹：

```bash
python3 -m service_09252_006.cli verify --db ./data/qe.db [--json]
```

退出码：`0` 通过，`2` 发现不一致/篡改，`1` 数据库无法打开。

## HTTP API 摘要

认证：`Authorization: Bearer <token>`；建用户/发 token 的引导端点用
`X-Bootstrap-Token`。写操作建议带 `Idempotency-Key`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/admin/users` | 引导：建用户/角色 |
| POST | `/v1/admin/tokens` | 引导：签发 API token |
| POST | `/v1/materials` | 登记材料（kind/sensitivity） |
| POST | `/v1/materials/{id}/versions` | 上传版本（base64，内容寻址） |
| POST | `/v1/versions/{id}/withdraw` | 撤回版本 |
| POST | `/v1/materials/{id}/withdraw` | 撤回整份材料 |
| POST | `/v1/packages` | 建评审包（可带 `supersedes_package_id`） |
| POST | `/v1/packages/{id}/entries` | 草稿包追加版本 |
| POST | `/v1/packages/{id}/seal` | 封存（固定清单指纹） |
| GET  | `/v1/packages/{id}` | 包视图（敏感条目按权限遮蔽） |
| GET  | `/v1/packages/{id}/entries/{vid}/content` | 授权下载内容字节 |
| POST | `/v1/packages/{id}/assignments` | 分配评审（可带跨时区截止） |
| GET  | `/v1/packages/{id}/requests` | 分配情况 |
| POST | `/v1/requests/{id}/respond` | 评审人接受/拒绝 |
| POST | `/v1/requests/{id}/objections` | 登记异议 |
| POST | `/v1/requests/{id}/verdict` | 提交 approve/object（object 须先有异议） |
| POST | `/v1/requests/{id}/cancel` | 取消分配（即时收回敏感访问权） |
| POST | `/v1/packages/{id}/decision` | 签发 approved/needs_revision/rejected |

评审状态机：`draft → sealed → under_review → decided`；复审包重新走一遍，
旧包不复活。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q service_09252_006 tests
```

覆盖：内容寻址与版本链、封存不变量、**材料撤回**（封存前后）、后补材料
只能复审、**最小披露与权限变化**（取消/拒绝/角色调整/跨机构）、
**跨时区截止**（上海/伦敦/洛杉矶）、异议与签发约束、幂等重放与失败重试、
多连接**并发复审**、离线核验对字节/清单/评审篡改的检出，以及完整 HTTP
端到端流程。

扩展模块覆盖证据、审批、权限、留存、对账与恢复等业务边界。
