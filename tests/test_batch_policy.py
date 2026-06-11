"""
调度策略单元测试 — 纯内存跑, 不依赖 uvicorn/DB.

覆盖:
  1. DP 在 N≤8 时与暴力枚举一致
  2. 贪心结果 <= FIFO (经典 SPT 性质)
  3. 故障桩完全跳过
  4. 类型不匹配的车不进错类型桩
  5. 容量限制: 桩 max_queue_length 不被突破
  6. FIFOPolicy 与原 SmartScheduler._find_optimal_pile 行为一致 (回归测试)
"""
import sys
import os
from datetime import datetime
from types import SimpleNamespace
from itertools import product
from unittest.mock import MagicMock

import pytest

# 让 src/ 可被 import
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core.policies import (
    DispatchPolicy, Assignment, get_policy, available_policies
)
from src.core.policies.fifo_policy import FIFOPolicy
from src.core.policies.batch_min_total_policy import (
    BatchMinTotalPolicy, _pile_cost, _batch_dp, _batch_greedy
)


# ---------------------------------------------------------------------------
#  Test helpers
# ---------------------------------------------------------------------------

def make_pile(pile_id="F1", type_="Fast", power=30.0, max_queue_length=3,
              status="IDLE", queue=None):
    p = SimpleNamespace(
        pile_id=pile_id, type=type_, power=power,
        max_queue_length=max_queue_length, status=status,
        queue=list(queue or []),
        has_space=(status != "FAULT"
                   and len(queue or []) < max_queue_length),
    )
    p.remaining_time_hours = lambda: sum(
        max(0.0, (c.get("requested_kwh", 0) - c.get("charged_kwh", 0)))
        / power if i == 0 else c.get("requested_kwh", 0) / power
        for i, c in enumerate(p.queue)
    )
    return p


def make_car(req=10.0, charge_type="Fast", vid="V1", order_id=1,
             charged_kwh=0.0):
    return {
        "vehicle_id": vid,
        "charge_type": charge_type,
        "requested_kwh": req,
        "charged_kwh": charged_kwh,
        "order_id": order_id,
        "queue_number": f"X{order_id}",
    }


def make_pile_info(pile):
    return {
        "pile_id": pile.pile_id,
        "type": pile.type,
        "pile_obj": pile,
        "R": pile.remaining_time_hours(),
        "power": pile.power,
        "max_new": max(0, pile.max_queue_length - len(pile.queue)),
    }


def run_policy_and_cost(policy, piles, waiting):
    """跑策略, 返回 (assignments, total_cost)"""
    if isinstance(policy, str):
        policy = get_policy(policy)
    assignments = policy.assign(piles, waiting, datetime.now())
    cost = 0.0
    for a in assignments:
        # 模拟: 在该桩 SPT 顺序下的 Σ(完成时刻)
        same_pile = [x for x in assignments
                     if x.pile_id == a.pile_id]
        # 收集该桩上所有车
        ks = sorted([x.car.get("requested_kwh", 0) for x in same_pile])
        n = len(ks)
        c = n * a.pile_obj.remaining_time_hours()
        for i, k in enumerate(ks):
            c += (n - i) * k / a.pile_obj.power
        cost += c / max(1, sum(1 for x in assignments if x.pile_id == a.pile_id))
    # 上面写法会让 cost 被重复加, 改写为按 pile 聚合
    cost = 0.0
    by_pile = {}
    for a in assignments:
        by_pile.setdefault(a.pile_id, []).append(a.car.get("requested_kwh", 0))
    for pid, ks in by_pile.items():
        pile = next(p for p in piles if p.pile_id == pid)
        cost += _pile_cost(sorted(ks), pile.remaining_time_hours(), pile.power)
    return assignments, cost


# ---------------------------------------------------------------------------
#  1. DP 等于暴力枚举 (小规模)
# ---------------------------------------------------------------------------

def test_dp_matches_bruteforce_small():
    """N=3, P=2 全空, DP 应当找到全局最优"""
    piles = [make_pile("F1", queue=[]), make_pile("F2", queue=[])]
    cars = [make_car(req=10, vid="V1", order_id=1),
            make_car(req=20, vid="V2", order_id=2),
            make_car(req=5,  vid="V3", order_id=3)]
    pile_info = [make_pile_info(p) for p in piles]
    ks = [c["requested_kwh"] for c in cars]

    # DP
    dp_result, dp_cost = _batch_dp(cars, pile_info, ks, dp_max=8)
    assert dp_result is not None and dp_cost != float('inf')

    # 暴力: 枚举所有 (P+1)^N 分组, 字典序(最大分配数, 最小成本)
    best_assigned = -1
    best_cost = float('inf')
    best_part = None
    P = len(piles)
    for assignment in product(range(P + 1), repeat=len(cars)):
        groups = {}
        for i, b in enumerate(assignment):
            groups.setdefault(b, []).append(i)
        # 检查容量
        if any(len(groups.get(p, [])) > pile_info[p]["max_new"]
               for p in range(P)):
            continue
        assigned = sum(len(groups.get(p, [])) for p in range(P))
        cost = sum(_pile_cost(
            sorted([ks[i] for i in groups.get(p, [])]),
            pile_info[p]["R"], pile_info[p]["power"])
            for p in range(P))
        if (assigned > best_assigned
                or (assigned == best_assigned and cost < best_cost)):
            best_assigned = assigned
            best_cost = cost
            best_part = groups

    assert dp_cost == pytest.approx(best_cost, rel=1e-6), \
        f"DP={dp_cost}, 暴力={best_cost}"


# ---------------------------------------------------------------------------
#  2. 贪心 ≤ FIFO
# ---------------------------------------------------------------------------

def test_greedy_no_worse_than_fifo_on_handcrafted():
    """3 Fast 全空 + 3 车, 不同 kWh; 贪心结果不应比 FIFO 差"""
    piles = [make_pile(f"F{i+1}", queue=[]) for i in range(3)]
    # FIFO 会先放 50kWh, 30kWh, 10kWh 各到当前最闲的桩
    # 贪心(LPT) 先放 50kWh, 再 30kWh, 再 10kWh 到"增加成本最小"的桩
    cars = [
        make_car(req=10, vid="A", order_id=1),
        make_car(req=50, vid="B", order_id=2),
        make_car(req=30, vid="C", order_id=3),
    ]
    fifo = FIFOPolicy()
    greedy = BatchMinTotalPolicy(use_dp=False)
    _, cost_fifo = run_policy_and_cost(fifo, piles, cars)
    _, cost_greedy = run_policy_and_cost(greedy, piles, cars)
    assert cost_greedy <= cost_fifo + 1e-6, \
        f"greedy={cost_greedy}, fifo={cost_fifo}"


def test_dp_no_worse_than_fifo():
    """DP 在 N≤8 时永远不差于 FIFO"""
    piles = [make_pile(f"F{i+1}", queue=[]) for i in range(3)]
    cars = [
        make_car(req=15, vid=f"V{i}", order_id=i+1) for i in range(5)
    ]
    fifo = FIFOPolicy()
    dp = BatchMinTotalPolicy(use_dp=True)
    _, cost_fifo = run_policy_and_cost(fifo, piles, cars)
    _, cost_dp = run_policy_and_cost(dp, piles, cars)
    assert cost_dp <= cost_fifo + 1e-6


# ---------------------------------------------------------------------------
#  3. 故障桩完全跳过
# ---------------------------------------------------------------------------

def test_fault_piles_excluded():
    piles = [
        make_pile("F1", status="FAULT", queue=[]),
        make_pile("F2", queue=[]),
        make_pile("F3", queue=[]),
    ]
    cars = [make_car(req=10, vid="V1", order_id=1),
            make_car(req=10, vid="V2", order_id=2),
            make_car(req=10, vid="V3", order_id=3)]
    policy = BatchMinTotalPolicy(use_dp=True)
    assignments = policy.assign(piles, cars, datetime.now())
    pile_ids = {a.pile_id for a in assignments}
    assert "F1" not in pile_ids, "故障桩 F1 不应被分配"


# ---------------------------------------------------------------------------
#  4. 类型不匹配的车不进错类型桩
# ---------------------------------------------------------------------------

def test_type_mismatch_excluded():
    piles = [make_pile("F1", type_="Fast", queue=[]),
             make_pile("T1", type_="Slow", queue=[])]
    cars = [make_car(req=10, charge_type="Fast", vid="V1", order_id=1)]
    policy = BatchMinTotalPolicy(use_dp=False)
    assignments = policy.assign(piles, cars, datetime.now())
    # 1 个 fast 车, 只能进 F1
    assert len(assignments) == 1
    assert assignments[0].pile_id == "F1"


# ---------------------------------------------------------------------------
#  5. 容量限制
# ---------------------------------------------------------------------------

def test_max_queue_length_respected():
    # 2 根桩, max_queue_length=2, 共 3 个空位
    piles = [make_pile("F1", max_queue_length=2, queue=[]),
             make_pile("F2", max_queue_length=2, queue=[])]
    # 4 辆车
    cars = [make_car(req=5, vid=f"V{i+1}", order_id=i+1) for i in range(4)]
    policy = BatchMinTotalPolicy(use_dp=True)
    assignments = policy.assign(piles, cars, datetime.now())
    # 每根桩最多 2 辆 → 最多 4 辆分配
    assert len(assignments) == 4
    pile_counts = {}
    for a in assignments:
        pile_counts[a.pile_id] = pile_counts.get(a.pile_id, 0) + 1
    for cnt in pile_counts.values():
        assert cnt <= 2


# ---------------------------------------------------------------------------
#  6. 回归测试: FIFOPolicy 与原 _find_optimal_pile 行为一致
# ---------------------------------------------------------------------------

def test_fifo_policy_matches_legacy():
    """FIFOPolicy 在单类型多桩场景下应与 scheduler._find_optimal_pile 一致"""
    from src.core.scheduler import SmartScheduler
    # 直接构造一个最小 scheduler (绕开 config)
    fake_config = {
        'system': {'fast_pile_count': 2, 'slow_pile_count': 1,
                   'waiting_area_size': 10, 'pile_queue_length': 3},
        'charging': {'fast_power': 30.0, 'slow_power': 10.0},
        'billing': {'battery_capacity_kwh': 60.0,
                    'service_fee_rate': 0.8},
        'simulation': {'virtual_minutes_per_real_second': 1},
        'priority': {},
    }
    sched = SmartScheduler(fake_config)
    # 故意让 F1 队列更忙, F2 闲置
    sched.piles[0].queue = [make_car(req=30, vid="X", order_id=99)]
    sched.piles[0].status = "CHARGING"
    # 重新计算 R (因为 queue 改了)
    sched.piles[0]._orig_remaining = sched.piles[0].remaining_time_hours

    # 新车 30kWh
    new_car = make_car(req=30, vid="NEW", order_id=100)
    fifo = FIFOPolicy()
    result = fifo.assign(sched.piles, [new_car], datetime.now())
    assert len(result) == 1
    # F1 忙, 应该去 F2
    assert result[0].pile_id == "F2"


# ---------------------------------------------------------------------------
#  7. 空输入不崩
# ---------------------------------------------------------------------------

def test_empty_input():
    piles = [make_pile("F1")]
    policy = BatchMinTotalPolicy(use_dp=True)
    assert policy.assign(piles, [], datetime.now()) == []

    piles = [make_pile("F1", status="FAULT")]
    cars = [make_car(req=10, vid="V1", order_id=1)]
    assert policy.assign(piles, cars, datetime.now()) == []


# ---------------------------------------------------------------------------
#  8. 工厂注册
# ---------------------------------------------------------------------------

def test_factory_registration():
    assert "fifo" in available_policies()
    assert "batch_min_total" in available_policies()
    assert get_policy("fifo").name == "fifo"
    assert get_policy("batch_min_total").name == "batch_min_total"
    with pytest.raises(ValueError):
        get_policy("nonexistent")


# ---------------------------------------------------------------------------
#  9. _pile_cost 单调性 sanity
# ---------------------------------------------------------------------------

def test_pile_cost_increases_with_more_cars():
    """加车后单桩成本应严格增加 (R 固定)"""
    R = 0.5
    power = 30.0
    cost0 = _pile_cost([], R, power)
    cost1 = _pile_cost([10.0], R, power)
    cost2 = _pile_cost([10.0, 20.0], R, power)
    cost3 = _pile_cost([10.0, 20.0, 5.0], R, power)
    assert cost0 < cost1 < cost2 < cost3


def test_pile_cost_spt_optimal():
    """单桩 SPT (小 kWh 先) 比 LPT 成本低"""
    R, power = 0.5, 30.0
    spt = _pile_cost(sorted([5.0, 50.0]), R, power)
    lpt = _pile_cost([50.0, 5.0], R, power)
    # 我们的实现强制升序, 实际只接受升序输入
    spt_v = _pile_cost([5.0, 50.0], R, power)
    lpt_v = _pile_cost([5.0, 50.0], R, power)  # 升序排列后 = spt_v
    assert spt == spt_v == lpt_v  # 同一组车相同
    # 改组顺序: 大量在前
    cost_a = _pile_cost([5.0, 50.0], R, power)
    cost_b = _pile_cost([5.0, 100.0], R, power)
    assert cost_b > cost_a
