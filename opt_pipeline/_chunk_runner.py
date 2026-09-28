"""独立分块执行器（opt 寻优并行的 worker 进程）。

由 common.py 的分块编排器以普通子进程方式拉起：
    python3 _chunk_runner.py <payload.pkl> <output.pkl>

payload/output 均为 pickle。刻意使用「一个独立 OS 进程跑一块任务」的模型，
而不是 multiprocessing.Pool：在 macOS + Python 3.14 下，父进程已初始化
tushare/pandas 等带后台线程的库后，Pool（spawn 的引导管道或 fork 后线程
锁）在任务量达到数千时会死锁（worker 0% CPU、Pool 反复重启 worker）。
独立子进程各自做普通的串行回测，进程间只用文件交换结果，完全规避该问题。

payload 结构：
    {"kind": "combo",      "df", "initial_capital", "comm_config",
     "jobs": [(strategy_id, params), ...]}
    {"kind": "train_test", "df_train", "df_test", ...同上, "jobs": [...]}
    {"kind": "rolling",    "test_dfs", ...同上, "jobs": [...]}
每完成 100 个任务向 stderr 输出一行进度，便于编排器 tail 观察。
"""

import os
import pickle
import sys
import time
import traceback

# 以脚本方式运行时把项目根加入 sys.path
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common import _run_single_combo  # noqa: E402


def main():
    if len(sys.argv) != 3:
        print("usage: _chunk_runner.py <payload.pkl> <output.pkl>", file=sys.stderr)
        sys.exit(2)
    payload_path, output_path = sys.argv[1], sys.argv[2]
    with open(payload_path, "rb") as f:
        payload = pickle.load(f)

    kind = payload["kind"]
    jobs = payload["jobs"]
    capital = payload["initial_capital"]
    comm = payload["comm_config"]
    t0 = time.time()

    results = []
    for i, job in enumerate(jobs, start=1):
        strategy_id, params = job
        try:
            if kind == "combo":
                results.append(_run_single_combo(
                    (payload["df"], strategy_id, params, capital, comm)
                ))
            elif kind == "train_test":
                common_args = (strategy_id, params, capital, comm)
                train_res = _run_single_combo((payload["df_train"],) + common_args)
                test_res = _run_single_combo((payload["df_test"],) + common_args)
                results.append((train_res, test_res))
            elif kind == "rolling":
                common_args = (strategy_id, params, capital, comm)
                results.append([_run_single_combo((df,) + common_args) for df in payload["test_dfs"]])
            else:
                raise ValueError(f"unknown payload kind: {kind}")
        except Exception:
            # 单个参数组合失败不应让整块丢失：记录错误占位，编排器可见
            results.append({"__error__": traceback.format_exc()})
        if i % 100 == 0 or i == len(jobs):
            print(f"[chunk {os.path.basename(payload_path)}] {i}/{len(jobs)} "
                  f"({i / max(time.time() - t0, 1e-9):.1f} jobs/s)", file=sys.stderr, flush=True)

    tmp_out = output_path + ".tmp"
    with open(tmp_out, "wb") as f:
        pickle.dump(results, f)
    os.replace(tmp_out, output_path)


if __name__ == "__main__":
    main()
