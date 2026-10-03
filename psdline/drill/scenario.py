"""演练剧本模型。

一个剧本回答四件事：

1. 故障长什么样（:meth:`Scenario.setup` 在分叉平台上把世界摆到"故障前一拍"）；
2. 故障怎么触发（:meth:`Scenario.inject` 由教员/剧本在零点注入，进哈希链）；
3. 标准处置是什么（:attr:`Scenario.steps`，每步一个动作模式 + 时限 + 判据）；
4. 怎么算合格（:attr:`Scenario.pass_score`）。

判据不靠"动作名字对上"那么粗糙：每步带一个谓词，检查这次执行的**结果**是否
真的满足处置意图（例如"必须是对那一樘故障门清故障，而不是随便一樘门"）。
这样抄步骤顺序、但做错对象的动作会被准确判错。

剧本是声明式数据 + 少量谓词函数；谓词只读取题面与执行结果，不碰现场，保证
同一份事件在任何机器上评分一致（复盘可复现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

#: 谓词：(题面, 动作请求, 执行结果) -> 是否满足本步意图
StepPredicate = Callable[[dict, dict, dict], bool]


@dataclass(frozen=True)
class ScenarioStep:
    """标准处置中的一步。"""

    key: str
    title: str
    action: str                       # 对应的现场动作名（dispatch 动作表）
    deadline_s: float                 # 相对故障零点的时限（秒）
    critical: bool = False            # 关键步：做错/漏掉直接不合格
    weight: int = 1
    predicate: StepPredicate | None = None
    rationale: str = ""               # 判断依据（复盘时展示为什么这步对/错）

    def matches(self, problem: dict, request: dict, result: dict) -> bool:
        """该动作是否满足本步：动作名一致且谓词通过（若有谓词）。"""

        if request.get("action") != self.action:
            return False
        if self.predicate is not None:
            try:
                return bool(self.predicate(problem, request, result))
            except Exception:  # 谓词本身出错按不满足处理，不炸评估
                return False
        return True


@dataclass(frozen=True)
class Scenario:
    """一个演练剧本。"""

    key: str
    name: str
    description: str
    setup: Callable[[object, dict], None]
    inject: Callable[[object, dict], dict]
    steps: tuple[ScenarioStep, ...]
    pass_score: int = 80
    duration_s: float = 300.0
    target: dict = field(default_factory=dict)   # 题面：目标列车/站台/门等
    briefing: str = ""                           # 给值班员的故障简报

    def step(self, key: str) -> ScenarioStep | None:
        for item in self.steps:
            if item.key == key:
                return item
        return None
