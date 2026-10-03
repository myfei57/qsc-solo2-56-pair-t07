"""内置演练剧本库。

三个剧本全部基于真实站台（ST1/ST2/ST3）与真实门区/列车对象，处置动作就是
现场控制台同款动作：

- ``psd_fault``   站台门故障：一樘站台门故障锁定，标准处置是确认、整站清扫、
                  排查、排除故障后恢复，最后确认发车条件；
- ``gap_alarm``   间隙探测可疑：发车前间隙报警，禁止盲目发车，须复核后再处置；
- ``stale_position`` 位置报文失准：列车位置超窗未刷新，先重新定位再放行。

每个剧本的 ``setup`` 把分叉世界摆到故障前一拍，``inject`` 落故障（进哈希链），
``steps`` 给出带谓词与时限的标准处置。题面目标在会话创建时写入事件链，因此
复盘时每个对错都能引用到具体设备与具体时间。
"""

from __future__ import annotations

from .scenario import Scenario, ScenarioStep


# ---- 谓词：校验"做对了对象、做对了结果" -------------------------------

def _same(value: object, expected: object) -> bool:
    return str(value) == str(expected)


def _first_gate(platform, station_id: str) -> str:
    gates = platform.gates.by_station(station_id)
    return gates[0].id if gates else ""


# ---- 剧本一：站台门故障 -----------------------------------------------

def _psd_setup(platform, target: dict) -> None:
    """故障前一拍：T01 已在 ST1 对位停稳、开门、确认间隙。"""

    station = target["station_id"]
    train = target["train_id"]
    platform.start()
    platform.arrive(train, station, 0)
    platform.check_gap(train)


def _psd_inject(platform, target: dict) -> dict:
    """注入：ST1 首樘站台门置故障并锁定。"""

    station = target["station_id"]
    psd_id = target.get("psd_id") or _first_gate(platform, station)
    target["psd_id"] = psd_id
    result = platform.set_fault(psd_id)
    return {"action": "inject_psd_fault", "psd_id": psd_id, "result": result}


def _psd_is_target_gate(problem: dict, request: dict, result: dict) -> bool:
    return _same(request.get("psd_id"), problem.get("psd_id"))


def _psd_clear_ok(problem: dict, request: dict, result: dict) -> bool:
    return _same(request.get("psd_id"), problem.get("psd_id")) and bool(result.get("ok"))


def _psd_sweep_target(problem: dict, request: dict, result: dict) -> bool:
    return _same(request.get("station_id"), problem.get("station_id"))


def _psd_depart_ok(problem: dict, request: dict, result: dict) -> bool:
    return (
        _same(request.get("train_id"), problem.get("train_id"))
        and _same(request.get("station_id"), problem.get("station_id"))
        and bool(result.get("ok"))
    )


psd_fault = Scenario(
    key="psd_fault",
    name="站台门故障应急处置",
    description="列车停站期间一樘站台门故障锁定，值班员须按序确认、清扫、排查、恢复并放行。",
    briefing=(
        "ST1 下行站台报 1 樘站台门故障并已锁定，T01 在站。请按规章完成现场确认、"
        "整站清扫、故障排查与恢复，确认发车条件后组织发车。"
    ),
    setup=_psd_setup,
    inject=_psd_inject,
    target={"train_id": "T01", "station_id": "ST1"},
    duration_s=300.0,
    pass_score=80,
    steps=(
        ScenarioStep(
            "confirm", "确认故障门区", "fault_status", 30.0,
            critical=True, weight=1,
            rationale="先确认是哪一樘门、处于什么锁定状态，再动手处置。",
        ),
        ScenarioStep(
            "sweep", "整站清扫关门", "sweep", 60.0,
            predicate=_psd_sweep_target, weight=2,
            rationale="清扫必须在故障站台 ST1 上执行，保证客室与站台间无夹人夹物。",
        ),
        ScenarioStep(
            "inspect", "现场排查", "inspect_gate", 120.0,
            predicate=_psd_is_target_gate, weight=1,
            rationale="排查对象必须是故障门 ST1-G01-01，而非邻近门。",
        ),
        ScenarioStep(
            "clear", "排除故障并解锁", "clear_fault", 180.0,
            critical=True, predicate=_psd_clear_ok, weight=3,
            rationale="清故障必须返回 ok（门已关闭、间隙正常），且对象是故障门本身。",
        ),
        ScenarioStep(
            "depart", "确认发车条件并组织发车", "depart", 240.0,
            critical=True, predicate=_psd_depart_ok, weight=2,
            rationale="发车必须成功：门关妥、间隙正常、联锁放行，三条件缺一不可。",
        ),
    ),
)


# ---- 剧本二：间隙探测可疑 ---------------------------------------------

def _gap_setup(platform, target: dict) -> None:
    platform.start()
    platform.arrive(target["train_id"], target["station_id"], 0)


def _gap_inject(platform, target: dict) -> dict:
    """注入：一条可疑（未确认通过）的间隙探测结果。"""

    result = platform.check_gap(target["train_id"], clear=False)
    return {"action": "inject_gap_suspect", "train_id": target["train_id"], "result": result}


def _gap_train(problem: dict, request: dict, result: dict) -> bool:
    return _same(request.get("train_id"), problem.get("train_id"))


def _gap_recheck_clear(problem: dict, request: dict, result: dict) -> bool:
    return _gap_train(problem, request, result) and bool(result.get("clear"))


def _gap_depart_ok(problem: dict, request: dict, result: dict) -> bool:
    return (
        _same(request.get("train_id"), problem.get("train_id"))
        and _same(request.get("station_id"), problem.get("station_id"))
        and bool(result.get("ok"))
    )


gap_alarm = Scenario(
    key="gap_alarm",
    name="发车前间隙探测可疑",
    description="发车前间隙探测出现可疑结果，严禁盲目发车，须复核确认后再组织。",
    briefing=(
        "T01 在 ST1 关门准备发车，间隙探测报可疑（blocked）。严禁在未复核确认前发车；"
        "请重新探测确认，确认安全后组织发车。"
    ),
    setup=_gap_setup,
    inject=_gap_inject,
    target={"train_id": "T01", "station_id": "ST1"},
    duration_s=240.0,
    pass_score=80,
    steps=(
        ScenarioStep(
            "reprobe", "重新探测间隙", "gap_probe", 45.0,
            critical=True, predicate=_gap_recheck_clear, weight=3,
            rationale="必须对 T01 重新探测且结果为 clear；带着 blocked 状态不能往下走。",
        ),
        ScenarioStep(
            "depart", "确认安全后发车", "depart", 120.0,
            critical=True, predicate=_gap_depart_ok, weight=2,
            rationale="只有在间隙确认无异常后发车才会成功；提前发车会被联锁拒绝。",
        ),
    ),
)


# ---- 剧本三：位置报文失准 ---------------------------------------------

def _pos_setup(platform, target: dict) -> None:
    platform.start()
    platform.arrive(target["train_id"], target["station_id"], 0)
    platform.check_gap(target["train_id"])
    platform.refresh_position(target["train_id"], target.get("old_position", 10))


def _pos_inject(platform, target: dict) -> dict:
    """注入：标记位置已超窗失准（通过连续周期不刷新体现）。

    由驱动在注入后推进超过 stale_after_s（8s）不刷新位置来形成失准；这里仅记录
    故障宣告，真正的失准在世界推进里自然发生。
    """

    return {
        "action": "inject_position_stale",
        "train_id": target["train_id"],
        "expected_position": target.get("expected_position", 12),
        "stale_after_s": 8.0,
    }


def _pos_refresh(problem: dict, request: dict, result: dict) -> bool:
    return (
        _same(request.get("train_id"), problem.get("train_id"))
        and bool(result.get("ok"))
    )


def _pos_depart_ok(problem: dict, request: dict, result: dict) -> bool:
    return (
        _same(request.get("train_id"), problem.get("train_id"))
        and _same(request.get("station_id"), problem.get("station_id"))
        and bool(result.get("ok"))
    )


stale_position = Scenario(
    key="stale_position",
    name="列车位置报文失准",
    description="列车位置报文超窗失准，须重新定位刷新后再放行。",
    briefing=(
        "T01 在 ST1，位置信号已超过 8 秒未刷新，控制台判定位置失准。请重新上报"
        "有效位置完成定位，确认信号正常后再组织发车。"
    ),
    setup=_pos_setup,
    inject=_pos_inject,
    target={"train_id": "T01", "station_id": "ST1", "old_position": 10,
            "expected_position": 12},
    duration_s=180.0,
    pass_score=80,
    steps=(
        ScenarioStep(
            "relocate", "重新上报有效位置", "position", 60.0,
            critical=True, predicate=_pos_refresh, weight=3,
            rationale="必须对 T01 上报一个被信号接受的有效位置，恢复定位。",
        ),
        ScenarioStep(
            "depart", "定位恢复后发车", "depart", 120.0,
            critical=True, predicate=_pos_depart_ok, weight=2,
            rationale="位置恢复、联锁正常后发车才会成功。",
        ),
    ),
)

SCENARIOS: dict[str, Scenario] = {
    item.key: item for item in (psd_fault, gap_alarm, stale_position)
}


def get_scenario(key: str) -> Scenario:
    try:
        return SCENARIOS[key]
    except KeyError:
        raise KeyError(f"未知演练剧本: {key}（可用：{sorted(SCENARIOS)}）") from None


def scenario_catalog() -> list[dict]:
    """剧本目录（供控制台/命令行选择）。"""

    return [
        {
            "key": scenario.key,
            "name": scenario.name,
            "description": scenario.description,
            "briefing": scenario.briefing,
            "duration_s": scenario.duration_s,
            "pass_score": scenario.pass_score,
            "steps": [
                {"key": step.key, "title": step.title, "action": step.action,
                 "deadline_s": step.deadline_s, "critical": step.critical,
                 "rationale": step.rationale}
                for step in scenario.steps
            ],
        }
        for scenario in SCENARIOS.values()
    ]
