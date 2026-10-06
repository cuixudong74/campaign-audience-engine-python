# Campaign Audience Engine

促销活动定向人群与优惠凭证服务。一个 FastAPI 进程内提供：活动生命周期管理、
基于规则的人群圈选、优惠券发放/核销、推荐归因、配额约束、权限审计以及可恢复
的批量发放。所有可变状态保存在 SQLite（WAL）中，写事务使用
`BEGIN IMMEDIATE` 串行化，跨线程与跨进程（多实例）下仍保证一致。

## 运行

```bash
uv sync
# 内存库（默认，适合本地试验；重启不保留状态）
uv run uvicorn main:app --reload
# 文件库（推荐）：状态持久化，重启后活动状态/预约时间/凭证均保留，
# 且启动时自动追赶本应在停机期间激活的活动
CAMPAIGN_DB=./data/engine.db uv run uvicorn main:app
```

服务启动后访问 `/docs` 查看接口。健康检查：`GET /health`。

身份通过请求头传递：`X-Actor-Id`、`X-Actor-Role`（`admin`/`operator`/
`agent`/`user`，缺省视为普通用户）。

## 测试

```bash
uv run pytest -q
```

覆盖：非法状态流转、重复发券/核销、多线程与**多进程**争抢额度、乱序/迟到
归因、自我/环形推荐、权限拒绝与审计、批量中途崩溃恢复、服务重启持久化。

## 1. 活动生命周期与状态语义

| 当前状态 | 可流转到 |
| --- | --- |
| `draft` | `scheduled`、`ended` |
| `scheduled` | `active`（手动或到 `start_at` 自动）、`ended` |
| `active` | `paused`、`ended`（到 `end_at` 自动结束） |
| `paused` | `active`、`ended` |
| `ended` | 终态 |

越界流转返回 `409 invalid_transition`，消息包含 `源 -> 目标` 以便区分原因。

- 创建时若提供 `start_at`，活动直接进入 `scheduled`；否则为 `draft`。
- 调度器周期执行幂等的 `activate_due()`：到点把 `scheduled -> active`，
  过 `end_at` 把 `active -> ended`。服务重启后先做一次 catch-up，停机期间
  错过的预约时间不会丢失；已激活的活动不会回退。
- 状态推进在 `BEGIN IMMEDIATE` 事务内重读当前状态后再更新，避免并发双跳。

## 2. 人群规则

规则是 JSON 可序列化的确定性 DSL：

```json
{
  "op": "AND",
  "children": [
    {"field": "attr.city", "op": "eq", "value": "Beijing"},
    {"event": "purchase", "op": "count_gte", "value": 2,
     "since": "2025-01-01T00:00:00Z", "min_amount": 100.0}
  ]
}
```

- 逻辑节点：`AND` / `OR` / `NOT`；属性条件：`eq/ne/in/not_in/gt/gte/lt/lte`；
  行为条件：`count/count_gte/count_lte/sum_amount/sum_amount_gte/ever/never/
  last_within_days`，支持 `since` 与 `min_amount` 过滤。
- 规则体先按 key 排序规范化再取 SHA-256 指纹；相同内容重复提交不产生新版本，
  内容修改则生成新版本（`(campaign_id, version)` 不可变）。
- 圈选是“规则 + 数据”的纯函数：同一版本对同一份数据得到相同排序结果和相同
  `selection_hash`。每次圈选结果（命中用户列表 + 哈希）按版本存快照。
- `/explain` 返回逐节点判定轨迹。**改规则不影响已发凭证**：凭证记录发放时的
  `rule_version`，旧版本规则与旧快照始终可查、可解释。

## 3. 凭证发放与核销（幂等与不超发）

- 发放必须携带 `request_id`（`vouchers.request_id` 唯一）。重复提交返回同一
  张凭证；同一 `request_id` 绑定不同活动/用户返回 `409 conflict`。
- 核销必须携带幂等键（`redeem_request_id` 唯一），且更新条件带
  `WHERE status='issued'`；重复核销返回 `409 voucher_not_redeemable`，
  并发双核销只有一方成功。
- 发放校验 `campaign.status == active`，否则 `campaign_not_active`。

## 4. 推荐关系归因

- 拒绝自我推荐（`self_referral`）与成环推荐（`cyclic_referral`，含直接互推与
  多跳环）；被拒绝的关系也落库，原因可追溯。
- 转化按**事件时间**（`occurred_at`，统一规范为秒级 UTC）归因：只有注册时间
  早于转化时间的有效关系可归因，因此事件乱序/迟到到达不改变结论。
- 同一推荐关系最多归因一次（部分唯一索引
  `attributions(referral_id) WHERE attributed=1`）；同一 `event_id` 重复投递
  幂等返回首次结论。

## 5. 配额约束

- `total_quota`：活动总量；`per_user_quota`：每人限领；要求
  `0 < per_user_quota <= total_quota`。
- “读计数 → 判断 → 插入凭证 → 计数 +1” 在同一个立即写事务中完成，多线程/
  多实例同时发放也不会超发。耗尽时发放返回 `409 quota_exhausted`，达个人上限
  返回 `409 per_user_limit_reached`。暂停/结束活动后发放被状态校验拒绝。

## 6. 权限与审计

| 动作 | 允许角色 |
| --- | --- |
| 活动创建/发布/暂停/恢复/结束、改规则、批量任务 | `admin`、`operator` |
| 发券 | `user`、`agent`、`operator`、`admin` |
| 本人核销 | `user`、`operator`、`admin` |
| 代客核销 | `agent`、`admin` |

无论允许还是拒绝，每次鉴权都写入 `audit_logs`（动作、对象、主体角色、结果、
原因）。普通用户尝试核销他人凭证按“需要代客核销权限”处理，拒绝并留痕。
查询：`GET /api/audit?target_type=&target_id=&allowed=false`。

## 7. 批量发放与中途恢复

- `POST /api/batches`（`idempotency_key` 唯一）：作业与全部待处理条目在同一
  事务写入，提交失败不留任何数据；相同 key 重放返回原作业。
- 逐个条目处理：发券结果（或条目级业务拒绝，如额度耗尽）与
  `next_offset` 检查点在同一事务提交，不存在“写了一半的凭证”。
- 条目级幂等键为 `batch:{job_id}:{idx}:{user_id}`。
- 崩溃后 `POST /api/batches/{id}/resume` 从检查点继续；已完成条目不重复发券。
  业务性拒绝（额度/限领/非进行中/不在人群）标记为条目 `failed`，作业结束状态
  为 `completed_with_errors`，其余条目继续。

## 主要接口

```
POST   /api/campaigns
GET    /api/campaigns/{id}
POST   /api/campaigns/{id}/transition      {"target": "scheduled|active|paused|ended"}
PUT    /api/campaigns/{id}/rule            {"rule": {...}}
POST   /api/campaigns/{id}/selection       {"users": [...]}
POST   /api/campaigns/{id}/explain
POST   /api/campaigns/{id}/vouchers        {"user_id","request_id"}
POST   /api/vouchers/{id}/redeem           {"request_id","user_id?"}
POST   /api/referrals                      {"referrer_id","referred_id","campaign_id"}
POST   /api/conversions                    {"event_id","user_id","campaign_id","occurred_at"}
GET    /api/attributions/{event_id}
POST   /api/batches                        {"campaign_id","idempotency_key","user_ids"}
POST   /api/batches/{id}/run               {"fail_after": N}   # 测试用故障注入
POST   /api/batches/{id}/resume
GET    /api/batches/{id}
GET    /api/audit
```

错误响应统一为：

```json
{"error": {"code": "quota_exhausted", "message": "..."}}
```

## 结构

```
app/
  errors.py      可区分错误码
  models.py      领域枚举与数据模型
  storage.py     SQLite 模式、连接、IMMEDIATE 事务
  rule_engine.py 规则 DSL：校验/规范化指纹/求值/解释
  lifecycle.py   活动状态机与到点激活
  audience.py    规则版本、确定性圈选快照、解释
  vouchers.py    幂等发券/核销与配额
  referrals.py   推荐注册、环检测、乱序归因
  authz.py       角色权限与审计
  batch.py       可恢复批量发放
  scheduler.py   后台定时激活
  api.py         FastAPI 装配
```
