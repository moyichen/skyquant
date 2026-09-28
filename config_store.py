"""统一配置与策略参数集管理（对标 freqtrade：固定 config + 参数集覆盖文件）。

目录布局：
  config.yaml                 固定配置：global_setting / commission_config /
                              opt_pipeline / stock_filter / stock_list / stock_blacklist
                              （不再包含 strategy_params）
  params/active.yaml          当前生效参数集：回测/实盘默认读取
  params/drafts/*.yaml        网页控制台手工「另存」的参数组（未生效，可试跑/可生效）
  params/experiments/*.yaml   opt 流水线每次自动归档的完整参数组（带指标元数据）

参数集文件 schema：
    meta:
      name: str               # 组名（文件名）
      source: opt|manual      # 来源
      created_at: 'YYYY-MM-DD HH:MM:SS'
      objective: str          # opt 来源时的寻优目标
      codes: ['000725']       # 涉及标的
      note: str
      metrics: {...}          # opt 来源：code -> strategy -> 指标
    strategy_params:
      '000725':
        trend: {trail_atr_multiple: 1.4, ...}

加载语义（freqtrade 多 config 风格）：load_config() = config.yaml 深合并 active.yaml；
load_config(params_path=...) 则用指定参数集临时覆盖 active（不落盘、不改变生效集）。
合并单元为 (code, strategy) 的完整参数 dict，不做参数级深合并。
"""

import datetime as _dt
import os
import re
import threading
from pathlib import Path
from typing import Iterable, Optional

import yaml

PROJECT_ROOT = Path(__file__).parent.resolve()
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
PARAMS_DIR = PROJECT_ROOT / "params"
ACTIVE_PARAMS_PATH = PARAMS_DIR / "active.yaml"
DRAFTS_DIR = PARAMS_DIR / "drafts"
EXPERIMENTS_DIR = PARAMS_DIR / "experiments"

# 写文件串行化：控制台后台线程 / opt 子进程都可能写参数集
_LOCK = threading.RLock()

_WARNED_LEGACY = False


# ===================== 基础工具 =====================
def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _atomic_write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


def _normalize_params(sp: Optional[dict]) -> dict:
    """strategy_params 键统一为 str（yaml 中未加引号的数字代码会被解析成 int）。"""
    out = {}
    for code, strategies in (sp or {}).items():
        out[str(code)] = {str(sid): dict(params or {}) for sid, params in (strategies or {}).items()}
    return out


def _deep_merge(base: dict, override: dict) -> dict:
    """dict 深合并（仅 dict 层级递归；其余类型 override 整体覆盖 base）。"""
    result = dict(base)
    for k, v in (override or {}).items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _resolve_params_path(params_path) -> Path:
    """参数集路径：绝对路径原样；相对路径锚定项目根（允许 'params/drafts/x.yaml'）。"""
    if params_path is None:
        return ACTIVE_PARAMS_PATH
    p = Path(params_path)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


# ===================== 读取 =====================
def base_config(config_path=None) -> dict:
    """只读 config.yaml 固定配置（不含参数集合并）。"""
    path = Path(config_path) if config_path else CONFIG_PATH
    return _read_yaml(path)


def load_param_set(params_path=None) -> dict:
    """读取一个参数集文件，返回 {meta, strategy_params}；文件不存在时返回空骨架。"""
    path = _resolve_params_path(params_path)
    data = _read_yaml(path)
    return {
        "meta": data.get("meta") or {},
        "strategy_params": _normalize_params(data.get("strategy_params")),
    }


def load_config(config_path=None, params_path=None) -> dict:
    """加载运行时完整配置 = config.yaml 深合并参数集（默认 active.yaml）。

    params_path 传入任意参数集文件 = freqtrade 式临时覆盖（只读合并，不改变生效集）。
    兼容：若 config.yaml 仍残留旧版 strategy_params（迁移前老文件），作为最低优先级
    兜底合并并发出一次 DeprecationWarning。
    """
    global _WARNED_LEGACY
    cfg = base_config(config_path)

    legacy = _normalize_params(cfg.pop("strategy_params", None))
    if legacy and not _WARNED_LEGACY:
        import warnings
        warnings.warn(
            "config.yaml 中的 strategy_params 已废弃，请迁移到 params/active.yaml"
            "（config_store 会临时兼容读取）",
            DeprecationWarning,
            stacklevel=2,
        )
        _WARNED_LEGACY = True

    set_path = _resolve_params_path(params_path)
    params = _normalize_params(load_param_set(set_path)["strategy_params"])

    cfg["strategy_params"] = _deep_merge(legacy, params)
    cfg["_params_path"] = str(set_path)
    cfg["_params_is_active"] = (set_path == ACTIVE_PARAMS_PATH)
    return cfg


# ===================== 写入 =====================
def save_param_set(params_path, strategy_params: dict, meta: Optional[dict] = None) -> Path:
    """（覆盖）写一个参数集文件。meta 省略时自动补 name/source/created_at。"""
    path = _resolve_params_path(params_path)
    with _LOCK:
        data = {
            "meta": meta or {},
            "strategy_params": _normalize_params(strategy_params),
        }
        data["meta"].setdefault("name", path.stem)
        data["meta"].setdefault("source", "manual")
        data["meta"].setdefault("created_at", _now())
        data["meta"]["codes"] = sorted(data["strategy_params"].keys())
        _atomic_write_yaml(path, data)
    return path


def update_active_entry(code: str, strategy_id: str, params: Optional[dict]) -> None:
    """更新生效集中单个 (code, strategy) 单元（网页控制台保存修改用）。

    params 为 None/{} = 删除该策略单元；code 下无任何策略时连 code 一并删除。
    """
    with _LOCK:
        data = _read_yaml(ACTIVE_PARAMS_PATH)
        sp = _normalize_params(data.get("strategy_params"))
        meta = data.get("meta") or {"name": "active", "source": "manual"}
        code = str(code)
        sp.setdefault(code, {})
        if params:
            sp[code][str(strategy_id)] = dict(params)
        else:
            sp[code].pop(str(strategy_id), None)
            if not sp[code]:
                sp.pop(code, None)
        meta["updated_at"] = _now()
        meta["codes"] = sorted(sp.keys())
        _atomic_write_yaml(ACTIVE_PARAMS_PATH, {"meta": meta, "strategy_params": sp})


def export_experiment(strategy_params: dict, meta: Optional[dict] = None) -> Path:
    """把一组参数归档到 params/experiments/<时间戳>_<objective>.yaml，返回路径。"""
    meta = dict(meta or {})
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    objective = re.sub(r"[^0-9A-Za-z_\-]", "_", str(meta.get("objective") or "manual"))
    path = EXPERIMENTS_DIR / f"{stamp}_{objective}.yaml"
    # 同一秒重复导出时追加序号，避免覆盖
    seq = 1
    while path.exists():
        path = EXPERIMENTS_DIR / f"{stamp}_{objective}_{seq}.yaml"
        seq += 1
    meta.setdefault("source", "opt")
    return save_param_set(path, strategy_params, meta=meta)


def apply_param_set(src_path, codes: Optional[Iterable[str]] = None,
                    strategies: Optional[Iterable[str]] = None,
                    note: Optional[str] = None) -> dict:
    """把任意参数集合并进 params/active.yaml（以 code×strategy 为整体单元替换）。

    codes/strategies 给定时只生效对应范围。返回 {applied: [(code, sid)], path}。
    """
    src = load_param_set(src_path)
    wanted_codes = {str(c) for c in codes} if codes else None
    wanted_sids = {str(s) for s in strategies} if strategies else None
    with _LOCK:
        active = _read_yaml(ACTIVE_PARAMS_PATH)
        sp = _normalize_params(active.get("strategy_params"))
        meta = active.get("meta") or {"name": "active", "source": "manual"}
        applied = []
        for code, sid_map in src["strategy_params"].items():
            if wanted_codes is not None and code not in wanted_codes:
                continue
            sp.setdefault(code, {})
            for sid, params in sid_map.items():
                if wanted_sids is not None and sid not in wanted_sids:
                    continue
                sp[code][sid] = dict(params)
                applied.append((code, sid))
        if applied:
            meta["updated_at"] = _now()
            meta["applied_from"] = str(_resolve_params_path(src_path).name)
            if note:
                meta["note"] = note
            meta["codes"] = sorted(sp.keys())
            _atomic_write_yaml(ACTIVE_PARAMS_PATH, {"meta": meta, "strategy_params": sp})
    return {"applied": applied, "path": str(ACTIVE_PARAMS_PATH)}


def save_draft(name: str, strategy_params: dict, note: str = "") -> Path:
    """手工另存一个草稿参数组到 params/drafts/<name>.yaml。"""
    safe = re.sub(r"[^0-9A-Za-z0-9_\-\u4e00-\u9fff]", "_", str(name)).strip("_") or "draft"
    path = DRAFTS_DIR / f"{safe}.yaml"
    return save_param_set(path, strategy_params, meta={"source": "manual", "note": note})


def delete_param_set(params_path) -> None:
    """删除草稿/实验参数集；禁止删除 active.yaml。"""
    path = _resolve_params_path(params_path).resolve()
    if path == ACTIVE_PARAMS_PATH.resolve():
        raise ValueError("生效参数集 active.yaml 不可删除")
    root = PARAMS_DIR.resolve()
    if root not in path.parents and path != root:
        raise ValueError(f"拒绝删除 params 目录之外的文件: {path}")
    path.unlink(missing_ok=True)


# ===================== 列举 =====================
def _set_summary(path: Path, group: str) -> Optional[dict]:
    if not path.is_file() or path.suffix in (".tmp",):
        return None
    data = _read_yaml(path)
    sp = _normalize_params(data.get("strategy_params"))
    n_entries = sum(len(sids) for sids in sp.values())
    meta = data.get("meta") or {}
    return {
        "group": group,
        "name": path.stem,
        "file": str(path.relative_to(PROJECT_ROOT)),
        "meta": meta,
        "n_codes": len(sp),
        "n_entries": n_entries,
    }


def list_param_sets() -> dict:
    """列出 active / drafts / experiments 三组参数集（实验组按时间倒序）。"""
    with _LOCK:
        active = _set_summary(ACTIVE_PARAMS_PATH, "active")
        drafts = sorted(DRAFTS_DIR.glob("*.yaml")) if DRAFTS_DIR.is_dir() else []
        experiments = sorted(EXPERIMENTS_DIR.glob("*.yaml")) if EXPERIMENTS_DIR.is_dir() else []
        return {
            "active": [active] if active else [],
            "drafts": [s for p in drafts if (s := _set_summary(p, "drafts"))],
            "experiments": [s for p in reversed(experiments) if (s := _set_summary(p, "experiments"))],
        }


def ensure_params_dirs() -> None:
    for d in (PARAMS_DIR, DRAFTS_DIR, EXPERIMENTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
