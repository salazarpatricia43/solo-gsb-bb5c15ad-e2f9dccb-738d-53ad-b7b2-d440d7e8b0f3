"""设备缺口查询：按连续缺口区间计算，分页不展开缺失序号全集。

缓冲区只保存真实到达但尚未提交的包；从 next_expected 到最大缓冲序号
之间可能隔着上亿个缺失序号（例如先提交 seq=1、再缓冲 seq=100000000），
逐个构造会让请求阻塞甚至耗尽内存。缺口天然是若干段连续区间——
next_expected 到首个缓冲序号之间、以及相邻两个缓冲序号之间——因此：

- gap_count 由区间长度求和直接得到，不构造缺失序号列表；
- 分页时被 offset 覆盖的整段缺口按区间长度算术跳过，只在命中的
  区间内展开本页所需序号——翻到深处也不会逐个遍历被跳过的缺口。

复杂度 O(缓冲包数 + limit)，与缺口总数无关。
"""

from typing import List, Sequence, Tuple


def gap_page(
    next_expected: int,
    buffered_seqs: Sequence[int],
    limit: int,
    offset: int,
) -> Tuple[int, List[int]]:
    """计算缺口总数与本页缺口（升序）。

    buffered_seqs 必须升序且全部 >= next_expected（摄入路径的不变量：
    缓冲区只含顺序前沿之后的序号）。返回 (gap_count, 本页 gaps)。
    """
    if not buffered_seqs:
        return 0, []
    # 区间 [next_expected, 最大缓冲序号] 内的整数，减去缓冲序号即为缺口
    gap_count = buffered_seqs[-1] - next_expected + 1 - len(buffered_seqs)
    if gap_count <= 0 or offset >= gap_count:
        return max(gap_count, 0), []

    page: List[int] = []
    skip = offset  # 仍需跳过的缺口数：整段算术跳过，不逐个遍历
    prev = next_expected - 1  # 上一个非缺口序号（初始为顺序前沿）
    for seq in buffered_seqs:
        lo, hi = prev + 1, seq - 1  # 本段连续缺口区间 [lo, hi]
        if lo <= hi:
            run = hi - lo + 1
            if skip >= run:
                skip -= run
            else:
                start = lo + skip
                skip = 0
                take = min(hi - start + 1, limit - len(page))
                page.extend(range(start, start + take))
                if len(page) >= limit:
                    break
        prev = seq
    return gap_count, page
