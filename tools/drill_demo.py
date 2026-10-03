#!/usr/bin/env python3
"""应急演练系统：端到端自演示（零第三方依赖）。

把这套系统的五个关键承诺用真实进程跑一遍，全部留下可检查的产物：

1. **两种模式同台**：shadow（从当前"实时"现场分叉）与 replay（从故障时刻快照
   重放）并发进行，互不干扰，现场全程不被演练动作改动；
2. **一条现场动作都不外发**：演练前后逐文档比对现场存储；
3. **跨进程崩溃安全**：子进程在"处置意图已落盘、结果未落盘"的瞬间被 kill -9，
   全新进程恢复后识别 in_flight、挂起、不自动重发，值班员显式继续后仍可封存
   且深度复核通过；
4. **历史不可变**：对一条已封存演练做字节级篡改，校验立刻失败并指出断点；
5. **复盘可解释**：每一步动作、结果、判断依据、时限与得分都在只读报告里。

用法：``python tools/drill_demo.py``（产物落在临时目录，结尾打印路径）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psdline.app import build_platform  # noqa: E402
from psdline.clock import ManualClock  # noqa: E402
from psdline.config import default_config  # noqa: E402
from psdline.drill import DrillService, MODE_REPLAY, MODE_SHADOW  # noqa: E402
from psdline.drill.errors import LogIntegrityError  # noqa: E402

WORKER = str(Path(__file__).with_name("crash_worker.py"))


def line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def dump(title: str, payload) -> None:
    print(f"--- {title} ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="drill-demo-"))
    live_root = base / "live"
    drill_root = base / "drill"

    line("0. 现场平台（真实站台 ST1/ST2/ST3、72 樘站台门、列车对象）")
    live = build_platform(default_config(), clock=ManualClock(),
                          storage_root=str(live_root))
    live.start()
    # 让现场带上一些真实运营痕迹，模拟早高峰实时数据
    live.arrive("T01", "ST1", 0)
    live.check_gap("T01")
    live_status = live.status()
    print(f"现场存储: {live_root}")
    print(f"站台 {len(live_status['stations'])} 个, 站台门 {len(live_status['doors'])} 樘, "
          f"现场操作计数 {live_status['operations']}")
    live_before = {doc: live.store.read(doc) for doc in live.store.list_ids()}

    service = DrillService(str(drill_root), live, shadow_speed=0.0)

    # ---- 1a. shadow：从当前实时数据分叉 --------------------------------
    line("1a. shadow 模式：复制当前实时现场数据后分叉（真实秒速可开，演示用手动步进）")
    shadow_meta = service.create_session("gap_alarm", "值班员-甲", MODE_SHADOW,
                                         title="早高峰间隙报警实战推演")
    shadow_id = shadow_meta["id"]
    print(f"演练会话: {shadow_id}  实时快照: {shadow_meta['snapshot_digest'][:12]}")
    service.prepare(shadow_id)
    shadow_start = service.start(shadow_id, "值班员-甲")
    print(f"故障注入: {shadow_start['injection']['action']} → "
          f"列车 {shadow_start['injection'].get('train_id')}")
    # 值班员甲规范处置
    service.act(shadow_id, "gap_probe", {"train_id": "T01", "clear": True}, "值班员-甲")
    service.act(shadow_id, "depart", {"train_id": "T01", "station_id": "ST1"}, "值班员-甲")
    for _ in range(3):
        service.tick(shadow_id, 1.0, actor="值班员-甲")
    shadow_sealed = service.seal(shadow_id, "值班员-甲")
    print(f"shadow 演练封存: 得分 {shadow_sealed['report']['evaluation']['score']}, "
          f"合格 {shadow_sealed['report']['evaluation']['passed']}")

    # ---- 1b. replay：从故障时刻快照重放，并发第二场 --------------------
    line("1b. replay 模式：值班员乙用上面那张故障时刻快照，独立重放一场 psd_fault")
    # shadow 创建时拍的实时快照就是可重放素材
    replay_digest = shadow_meta["snapshot_digest"]
    replay_meta = service.create_session(
        "psd_fault", "值班员-乙", MODE_REPLAY,
        snapshot_digest=replay_digest, title="站台门故障快照重放")
    replay_id = replay_meta["id"]
    service.prepare(replay_id)
    replay_start = service.start(replay_id, "值班员-乙")
    psd = replay_start["injection"]["psd_id"]
    print(f"演练会话: {replay_id}  故障门: {psd}")

    # ---- 2. 跨进程崩溃（kill -9 在动作执行瞬间） -----------------------
    line("2. 跨进程崩溃安全：另一个进程接管会话，在处置瞬间被 kill -9")
    acted = subprocess.run([sys.executable, WORKER, str(drill_root), replay_id, "act"],
                           capture_output=True, text=True)
    print(f"子进程正常动作 → 退出码 {acted.returncode}, 输出 {acted.stdout.strip()}")
    crashed = subprocess.run([sys.executable, WORKER, str(drill_root), replay_id, "crash"],
                             capture_output=True, text=True)
    print(f"子进程在 clear_fault 意图落盘后硬死亡 → 退出码 {crashed.returncode}")

    line("2b. 全新进程恢复：识别未完成动作、挂起、绝不自动重发")
    # 用全新服务实例模拟进程重启（不复用任何内存状态）
    recovered = DrillService(str(drill_root), live, shadow_speed=0.0)
    recovery = recovered.recover_all()
    mine = [item for item in recovery if item["session"] == replay_id][0]
    print(f"恢复结论: {json.dumps(mine, ensure_ascii=False)}")
    meta_after = recovered.registry.get(replay_id)
    print(f"会话状态: {meta_after['state']}（应 suspended）, recovered={meta_after['recovered']}")
    print(f"恢复说明: {meta_after['recovery_note']}")
    # 故障门必须仍处于故障态（崩溃的 clear_fault 没有被偷偷重发/生效）
    gate = recovered._open_runtime(replay_id, meta_after).platform.gates.get(psd)
    print(f"故障门 {psd} 仍锁定={gate.locked}（证明未自动重发清故障）")
    assert gate.locked is True

    # 值班员显式恢复，继续把处置做完
    recovered.resume(replay_id, "值班员-乙")
    recovered.act(replay_id, "inspect_gate", {"psd_id": psd}, "值班员-乙")
    recovered.act(replay_id, "clear_fault", {"psd_id": psd}, "值班员-乙")
    recovered.act(replay_id, "depart", {"train_id": "T01", "station_id": "ST1"}, "值班员-乙")
    replay_sealed = recovered.seal(replay_id, "值班员-乙")
    verification = recovered.verify_session(replay_id)
    print(f"恢复后完成并封存: 得分 {replay_sealed['report']['evaluation']['score']}")
    dump("深度复核（哈希链+逐动作重放+世界指纹）", {
        "chain_intact": verification["chain_intact"],
        "events": verification["events"],
        "action_mismatches": verification["action_mismatches"],
        "world_hash_match": verification["world_hash_match"],
        "score_reproducible": verification["score_reproducible"],
        "passed": verification["passed"],
    })

    # ---- 3. 隔离校验：现场没被演练碰过 -------------------------------
    line("3. 隔离校验：演练全程结束后，现场存储逐文档比对")
    live_after = {doc: live.store.read(doc) for doc in live.store.list_ids()}
    changed = [doc for doc in live_before if live_before[doc] != live_after.get(doc)]
    only_live = set(live_after) - set(live_before)
    print(f"演练前现场文档 {len(live_before)} 个，演练后 {len(live_after)} 个")
    print(f"被演练改动的现场文档: {changed if changed else '无'}")
    print(f"演练在现场目录新增的文档: {sorted(only_live) if only_live else '无'}")
    # 注意：shadow 创建后 live 本身没有再被操作；before 快照在 live 运营之后
    assert not changed and not only_live, "演练污染了现场！"
    print("✔ 一条现场动作都没有进入真实现场，演练痕迹全部在独立演练目录")

    # ---- 4. 历史不可变：篡改必被发现 ----------------------------------
    line("4. 历史不可变：对已封存演练的事件做字节级篡改")
    events_path = drill_root / "sessions" / replay_id / "events.jsonl"
    raw = events_path.read_text(encoding="utf-8").splitlines()
    target = json.loads(raw[3])  # 选一条动作记录
    original_payload = json.dumps(target["payload"], ensure_ascii=False)
    # 尝试把一个动作的对象"换"成别的门（典型作弊）
    mutated = original_payload.replace("ST1-G01-01", "ST1-G02-01")
    os.chmod(events_path, 0o644)  # 作弊者先强行解开只读
    lines = raw
    lines[3] = json.dumps({**target, "payload": json.loads(mutated)},
                          ensure_ascii=False)
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        recovered.verify_session(replay_id)
        tamper_detected = False
    except LogIntegrityError as error:
        tamper_detected = True
        print(f"✔ 篡改被立刻识别: {error.message}")
    assert tamper_detected
    print("（只读权限是第二道防线；哈希链即使在权限被绕过的情况下也能检出）")

    # ---- 5. 复盘报告 --------------------------------------------------
    line("5. 复盘报告（只读、含每步判断依据与时限）")
    report = service.report(shadow_id)
    for step in report["evaluation"]["steps"]:
        print(f"  [{step['verdict']:>5}] {step['title']}  "
              f"时限 {step['deadline_s']:.0f}s  得分 {step['earned']}/{step['weight']}")
        print(f"         依据: {step['rationale']}")
    print(f"  总分 {report['evaluation']['score']}/100  合格 {report['evaluation']['passed']}")
    print(f"  出口门控拦截次数: {report['guard']['egress_rejections']}（0 次拦截尝试即无越界）")

    line("完成")
    print(f"演练产物目录（可人工核查）: {drill_root}")
    print(f"  - sessions.json              会话名册")
    print(f"  - snapshots/<hash>/bundle.json  内容寻址不可变快照")
    print(f"  - sessions/<id>/events.jsonl    哈希链事件日志（封存后只读）")
    print(f"  - sessions/<id>/fork/           分叉世界副本（与现场物理分离）")
    print(f"  - sessions/<id>/report.json     只读复盘报告")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
