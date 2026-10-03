# 应急演练子系统（psdline.drill）

在**不向现场设备发出任何一条真实动作**的前提下，用真实站台与列车数据、真实联锁
调用链推演应急处置；演练结束能逐步看出每一步的结果、门控判定与判断依据。

## 关键取舍（已定）

1. **数据来源：默认“故障时刻快照重放”，也支持“当前实时分叉”。**
   * `fault_replay`：对现场存储做一次只读快照，之后在沙箱里按场景时钟确定性重放。
     适合复盘已发生故障、反复练同一局面，结果可字节级复现。
   * `live_fork`：快照来源是当前实时态，分叉后同样在独立沙箱里演化，现场继续运行
     互不干扰。适合“就现在的客流/车次练一把”。
   * 两者在捕获点之后都**只读一次现场、之后完全隔离**，避免边练边被现场变化污染。
2. **演练期间不能切回实作。** 会话是纯沙箱，门在 `drill` 模式下物理上没有放行到
   现场的路径；真作通道是另一套独立的现场平台，演练全程不受影响。想回到实作，
   直接用现场控制台即可（演练无需“退出”，`abort` 只是封存这一场演练）。

## 四件事如何一起保证“演练痕迹不混进现场事实”

| 关注点 | 现场 | 演练 |
|---|---|---|
| 存储根 | 配置里的 `storage_root`（如 `var/state`） | 独立 `drill_root`（默认 `var/drill`），下设 `snapshots/`、`sessions/<id>/` |
| 当前态 | 现场 `JsonStore` | 快照复制出的每会话独立沙箱目录 |
| 审计 | 现场 `audit-*.jsonl` | 演练根 `drill-audit.jsonl` + 每会话 `events.jsonl`，哈希链、HMAC 封存 |
| 设备出口 | 现场门控 | 演练门 `drill` 模式永不调用 `FieldGateway`（跑完 `transmitted=0` 可断言） |

纵深防御：

* **门控 fail-closed**：模式不符、动作不在白名单、令牌缺失/串会话/过期、会话已封存，
  一律拒绝；令牌是绑定 `session_id + snapshot_id` 的 HMAC，密钥来自环境变量
  `PSDLINE_DRILL_HMAC` 或演练根下 `0600` 的 `drill.key`（跨 CLI 进程稳定）。
* **快照不可写回现场**：`restore_into` 校验目标必须位于 `sessions/` 子树且不等于
  快照来源根；快照内容寻址、只写一次、逐文档 SHA-256 + 清单整体哈希。
* **动作白名单**：只有明确登记的现场动作能被推演，其它动作在门处即拒。

## 并发、部分失败、崩溃安全、历史不可变

* **并发演练**：每会话独立目录、独立 `fcntl` 会话锁、独立令牌；多个值班员可同时开
  多个会话，互不串扰。锁随进程退出由内核释放，不产生永久锁。
* **部分失败恢复**：提交协议是“沙箱执行成功 → 事件先 fsync 落盘（WAL）→ 更新检查点”。
  崩在检查点前后都安全：现场状态由“快照 + 事件流”重放重建；没来得及落事件的动作
  等于没发生，凭 `client_request_id` 重试是幂等的（演练无现场副作用）。
* **崩溃安全**：所有写都是临时文件 + fsync + `os.replace` 原子替换；事件流打开时
  裁掉结尾撕裂行/失配行，回到最后可信前缀；删除整个沙箱目录后再打开会自动重放重建。
* **历史不可变**：事件逐条哈希链（`prev_hash`），无删除接口；改任意一条都会断链，
  复核即暴露。会话封存写 `session_seal` 并在 `seals.jsonl` 追加链顶 HMAC；封存后
  拒绝再写该会话的审计与动作。

## 判断依据从哪来

每步动作执行**前**，`basis.py` 用沙箱当前态按**与现场相同的规则**做纯查询预判，
给出规则码、现场证据、各前置条件是否满足（如
`INTERLOCK-RELEASE-GATE-CLOSED-GAP-CLEAR`、`GAP-CLEAR-BEFORE-DEPART`）。
真实执行结果与预判分开落盘；复盘里能看到“凭什么这么判、实际成没成、是否一致”。
被联锁成功拦停的危险尝试计为 `blocked_attempts`（系统有效的证据），不算值班员失误。

## CLI 用法（全局选项在子命令前）

```bash
# 1) 只读捕获现场快照（故障时刻重放 / 当前实时分叉）
python -m psdline drill --live-root var/state --drill-root var/drill \
    snapshot --kind fault_replay --label 早高峰故障

# 2) 查看快照 / 开演
python -m psdline drill --drill-root var/drill snapshots
python -m psdline drill --drill-root var/drill start \
    --snapshot snap-xxxx --scenario morning_peak_psd --operator 张三

# 3) 处置（动作参数是 JSON 对象；request-id 用于幂等重试）
python -m psdline drill --drill-root var/drill act --session drill-xxxx \
    --action arrive --args '{"train_id":"T09","station_id":"ST1","position_m":0}' \
    --request-id r1
python -m psdline drill --drill-root var/drill tick --session drill-xxxx --to 3
python -m psdline drill --drill-root var/drill note --session drill-xxxx --text "已联控"

# 4) 结束 / 一键中止 / 复盘 / 校验 / 独立重放
python -m psdline drill --drill-root var/drill finish --session drill-xxxx
python -m psdline drill --drill-root var/drill report --session drill-xxxx
python -m psdline drill --drill-root var/drill verify-audit
python -m psdline drill --drill-root var/drill replay --session drill-xxxx
```

快速自检（不触碰默认现场存储）：

```bash
make drill-demo
```
