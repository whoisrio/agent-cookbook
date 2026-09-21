"""用 asciinema 把 stage demo 的 case 逐个跑一遍并录成 .cast。

    python -m baby_event_driven_agent.recorder --stage 03                 # 录全部 case
    python -m baby_event_driven_agent.recorder --stage 03 07-tool-running-stop
    python -m baby_event_driven_agent.recorder --stage 03 --render-svg     # 出 .svg（文档引用它）
    python -m baby_event_driven_agent.recorder --stage 03 --render         # 出 .gif/.mp4（本地预览）
    python -m baby_event_driven_agent.recorder --stage 03 --render-only --render-svg --run-id docs
    python -m baby_event_driven_agent.recorder --stage 03 --list

case 名由各 stage 自己定义（`两位编号-语义名`，如 `07-tool-running-stop`）：编号让文件名字典序
= 演示顺序；随名字还带一句"这个 case 在看什么"——`--list` 一问就拿到，落进 index.json 的
`title` 字段，录制时也一起打进 .cast 的标题里，看片时对得上。

两处产物、各归各的树：

    agent 的 session log（还是 demo 那份，按 stage 分目录）：
        src/baby_event_driven_agent/sessions/stage03/session.jsonl

    录制产物（按 stage / run 分层；各种渲染产物与 .cast 同目录）：
        src/baby_event_driven_agent/rec/stage03/<run-id>/index.json
        src/baby_event_driven_agent/rec/stage03/<run-id>/<case>.cast  （原料，进版本控制）
        src/baby_event_driven_agent/rec/stage03/<run-id>/<case>.svg   (--render-svg，文档引用)
        src/baby_event_driven_agent/rec/stage03/<run-id>/<case>.gif   (--render)
        src/baby_event_driven_agent/rec/stage03/<run-id>/<case>.mp4   (--render，不进版本控制)

渲染链两条，各自独立：

- **svg（矢量文本动画）**：**本书没用**——三条路都验过，都不合用（demo 几乎全中文，中文即全角）：
  - `svg-term-cli`：全角字符按 1 列算 → 同一行里 CJK 之后的片段被往左挤、叠在中文上
    （实测 banner 行收尾的 `──` 落在 x=45.09，正确值 ≈71，差 26 列）。
  - `showreel`（PyPI，MIT，基于 pyte）：片段**起点**用 wcwidth 算对了，但片段**内部**每字符
    固定占 1 格 → 长中文被压扁、行尾甩出一大截。
  - `termsvg`（brew，GPL-3.0-only，只吃 asciicast v2）：**中文排得对（唯一一个）**，但行超过
    终端宽度**折行**时会在行首留残片叠字；把录制窗口加宽到不折行（`--window-size 200x40`）
    可绕开，代价是图变宽、嵌进 markdown 后字变小。
  所以文档引用的是 `.gif`（agg 是 asciinema 官方渲染器，终端模拟完整，中文/折行/配色都忠实）。
  哪天真要 svg，正路是自己用 pyte 重放 + 按 wcwidth 生成（120 列也能正确折行）。
  `render_svg()` 留着：纯 ASCII 的录制仍可用它（链路 cast(v3)
  --`asciinema convert -f asciicast-v2`--> cast(v2) --`svg-term-cli`--> `.svg`）。
- **gif/mp4（本地预览、对外分享）**：agg 出 GIF（asciinema 官方渲染器，只出 GIF），
  ffmpeg 再转 MP4。agg 出的 GIF 宽高可能是奇数，而 h264 要求偶数，所以转码加一句
  scale 修正。

几个刻意的选择：
- 每个 case 跑在**独立子进程**里，超时到点连进程组一起 kill——一个 case 卡住
  不影响整批（worker 里模型异常就发不出 turn_end，这是真实存在的挂法）。
- 录制用 asciinema 的 headless 模式；窗口尺寸固定、等待压到 `--idle-time-limit`
  秒，保证每个 case 的观感一致。`.cast` 保留原始时序，这个限制只写进元数据，
  事后还能改。
- 录制是"终端里的录制"，不用管窗口管理/分辨率/焦点——这也是选 asciinema 的原因。

前提：`asciinema`（`brew install asciinema`）；出 svg 还要 node 的 `npx`（免装
svg-term-cli，首次临时下载）或 `npm i -g svg-term-cli`；出 gif/mp4 要 `agg`、`ffmpeg`。
仓库根 .env 配好模型。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parent

# session log 的落点：和 demo 默认一致，按 stage 分目录（这棵树被 .gitignore 忽略）
SESSIONS_ROOT = PACKAGE_DIR / "sessions"
# 录制产物的落点：单独一棵树，按 stage / run 分层（要不要进 git 由你决定）
DEFAULT_REC_DIR = PACKAGE_DIR / "rec"

# stage 号 -> 包模块。新 stage 接上 --list/--case 之后在这里加一行即可。
STAGE_MODULES: dict[str, str] = {
    "01": "baby_event_driven_agent.stages.stage01_receive_events",
    "02": "baby_event_driven_agent.stages.stage02_inbox_steering",
    "03": "baby_event_driven_agent.stages.stage03_interrupt",
    "04": "baby_event_driven_agent.stages.stage04_message_bus",
    "05": "baby_event_driven_agent.stages.stage05_session",
}


def _stage_env() -> dict[str, str]:
    """让子进程能 import 本包（和 pytest 一样靠 PYTHONPATH=src）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def list_cases(stage: str) -> dict[str, str]:
    """问 stage 的 main 要 case 列表：`{case 名: 这个 case 在看什么}`。

    各 stage 的 `--list` 输出是 `名字<TAB>说明`；`--list` 不加载模型配置，所以这里
    不会碰 .env。说明会一路带进 index.json 和 .cast 的标题。
    """
    module = STAGE_MODULES[stage]
    proc = subprocess.run(
        [sys.executable, "-m", f"{module}.main", "--list"],
        cwd=REPO_ROOT,
        env=_stage_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"{module}.main --list 失败（这个 stage 还没接 case？）：\n{proc.stderr.strip()}"
        )
    cases: dict[str, str] = {}
    for ln in proc.stdout.splitlines():
        if ln.strip():
            cid, _, title = ln.partition("\t")
            cases[cid.strip()] = title.strip()
    return cases


def record_case(
    stage: str,
    case: str,
    run_dir: Path,
    *,
    title: str = "",
    window: str,
    idle: float,
    timeout: float,
) -> dict:
    """跑一个 case 并录成 `<run_dir>/<case>.cast`；返回一条 index 记录。"""
    module = STAGE_MODULES[stage]
    cast = run_dir / f"{case}.cast"
    run_dir.mkdir(parents=True, exist_ok=True)
    # session log 仍落在 sessions/<stage>/（demo 的默认位置），不跟着录制跑
    sessions_dir = SESSIONS_ROOT / f"stage{stage}"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    command = " ".join(
        shlex.quote(part)
        for part in (
            sys.executable,
            "-m",
            f"{module}.main",
            case,
            "--sessions-dir",
            str(sessions_dir),
        )
    )
    argv = [
        "asciinema",
        "rec",
        "--headless",  # 不占用当前终端
        "--quiet",
        "--overwrite",
        "--return",  # 以被录制命令的退出码退出，方便判成败
        "--window-size",
        window,
        "--idle-time-limit",
        str(idle),
        "--title",
        f"stage{stage}/{case}" + (f" · {title}" if title else ""),
        "-c",
        command,
        str(cast),
    ]

    started = time.time()
    timed_out = False
    stderr = ""
    # start_new_session：超时时整组 kill，别把挂住的子进程留成孤儿
    proc = subprocess.Popen(
        argv,
        cwd=REPO_ROOT,
        env=_stage_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(timeout=timeout)
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        timed_out = True
        code = None
        stderr = f"超时 {timeout:g}s，已中止"

    return {
        "case": case,
        "title": title,
        "exit": code,
        "timeout": timed_out,
        "secs": round(time.time() - started, 1),
        "cast": str(cast.relative_to(REPO_ROOT)) if cast.exists() else None,
        "sessions": str(sessions_dir.relative_to(REPO_ROOT)),
        "stderr": stderr.strip(),
    }


def _run(argv: list[str]) -> None:
    """跑一条外部命令；非零退出就把最后几行 stderr/stdout 当错误抛出来。"""
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(" / ".join(detail[-3:]) if detail else f"退出码 {proc.returncode}")


def render_cast(cast: Path) -> dict:
    """把 .cast 渲染成**同目录**的 .gif 和 .mp4（本地预览 / 对外分享用，不进版本控制）。

    agg 出 GIF；ffmpeg 再转 MP4。agg 的 GIF 宽高可能是奇数，h264 要偶数，
    所以转码加一句 scale 修正（否则报 width not divisible by 2）。
    """
    gif = cast.with_suffix(".gif")
    mp4 = cast.with_suffix(".mp4")
    result: dict = {"gif": None, "mp4": None, "render_error": ""}
    try:
        _run(["agg", str(cast), str(gif)])
        result["gif"] = str(gif.relative_to(REPO_ROOT)) if gif.exists() else None
        _run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(gif),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4),
            ]
        )
        result["mp4"] = str(mp4.relative_to(REPO_ROOT)) if mp4.exists() else None
    except FileNotFoundError as exc:
        result["render_error"] = (
            f"缺少工具 {exc.filename}（agg: brew install agg；ffmpeg: brew install ffmpeg）"
        )
    except RuntimeError as exc:
        result["render_error"] = str(exc)
    return result


def render_svg(cast: Path, window: str) -> dict:
    """把 .cast 渲染成**同目录**的 .svg：矢量文本 + CSS 动画，文档里引用的是它。

    比 gif 小一到两个数量级（同一个 case 实测：gif 6.45M / svg 0.54M，gzip 后
    0.02M），放大不糊，文本是真 `<text>` 不是描边位图。链路两步：

        cast(v3) --asciinema convert--> cast(v2) --svg-term-cli--> .svg

    svg-term-cli 只吃 asciicast v1/v2，而 asciinema 3.x 写的是 v3，所以中间那步
    不能省。临时 v2 落系统临时目录——不能落 run 目录，否则会被 --render-only 的
    `*.cast` 扫成"多了一个 case"。
    """
    svg = cast.with_suffix(".svg")
    cols, _, rows = window.partition("x")
    result: dict = {"svg": None, "svg_error": ""}
    tmp: Path | None = None
    try:
        handle, tmp_name = tempfile.mkstemp(suffix=".cast", prefix="cookbook-v2-")
        os.close(handle)
        tmp = Path(tmp_name)
        _run(["asciinema", "convert", "-f", "asciicast-v2", str(cast), str(tmp)])
        _run(
            [
                "npx", "--yes", "svg-term-cli",
                "--in", str(tmp),
                "--out", str(svg),
                "--width", cols or "120",
                "--height", rows or "40",
            ]
        )
        result["svg"] = str(svg.relative_to(REPO_ROOT)) if svg.exists() else None
    except FileNotFoundError as exc:
        result["svg_error"] = (
            f"缺少工具 {exc.filename}（asciinema: brew install asciinema；"
            "svg-term-cli: npm i -g svg-term-cli，或用 npx 免装）"
        )
    except RuntimeError as exc:
        result["svg_error"] = str(exc)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return result


def _latest_run_dir(stage_dir: Path) -> Path:
    runs = [p for p in stage_dir.iterdir() if p.is_dir()] if stage_dir.is_dir() else []
    if not runs:
        raise SystemExit(f"没有可渲染的 run：{stage_dir}")
    return max(runs, key=lambda p: p.stat().st_mtime)


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="cookbook-rec",
        description="用 asciinema 把 stage demo 的 case 逐个跑一遍并录成 .cast。",
    )
    parser.add_argument("--stage", required=True, choices=sorted(STAGE_MODULES))
    parser.add_argument("cases", nargs="*", metavar="CASE", help="只录指定 case（默认全部）")
    parser.add_argument("--list", action="store_true", help="列出该 stage 的 case 后退出")
    parser.add_argument("--run-id", default=None, help="run 目录名（默认 UTC 时间戳）")
    parser.add_argument(
        "--rec-dir", default=None, help="录制产物根目录（默认 src/baby_event_driven_agent/rec）"
    )
    parser.add_argument("--window-size", default="120x40", help="录制窗口，如 120x40")
    parser.add_argument(
        "--idle-time-limit",
        type=float,
        default=1.0,
        help="把超过该秒数的等待压掉（只写进元数据，事后可改）",
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="单个 case 上限（秒）")
    parser.add_argument(
        "--render",
        action="store_true",
        help="录完顺手渲染成 .gif/.mp4（放 .cast 同目录；本地预览用，体积大，不进版本控制）",
    )
    parser.add_argument(
        "--render-svg",
        action="store_true",
        help="录完顺手渲染成 .svg（矢量文本动画，文档引用的就是它）",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="不录制，只把某个 run 已有的 .cast 渲染出来（不给 --run-id 就取最近的 run）",
    )
    args = parser.parse_args()

    if args.list:
        for cid, title in list_cases(args.stage).items():
            print(f"{cid}\t{title}")
        return

    rec_root = Path(args.rec_dir) if args.rec_dir else DEFAULT_REC_DIR
    stage_dir = rec_root / f"stage{args.stage}"

    if args.render_only:
        run_dir = stage_dir / args.run_id if args.run_id else _latest_run_dir(stage_dir)
        cases = args.cases or sorted(p.stem for p in run_dir.glob("*.cast"))
        if not cases:
            raise SystemExit(f"这个 run 里没有 .cast：{run_dir}")
        # 不给任何 --render* 时按老规矩出 gif/mp4；只给 --render-svg 就只出 svg。
        do_svg = args.render_svg
        do_gif = args.render or not args.render_svg
        print(f"只渲染 · {len(cases)} 个 case → {run_dir}", flush=True)
        rendered: dict[str, dict] = {}
        for case in cases:
            cast = run_dir / f"{case}.cast"
            if not cast.exists():
                print(f"▶ {case} ... 跳过（没有 {cast.name}）")
                continue
            print(f"▶ {case} ...", end="", flush=True)
            parts: list[str] = []
            if do_svg:
                res = render_svg(cast, args.window_size)
                rendered.setdefault(case, {}).update(res)
                parts.append("✓ svg" if res["svg"] else f"✗ svg：{res['svg_error']}")
            if do_gif:
                res = render_cast(cast)
                rendered.setdefault(case, {}).update(res)
                parts.append("✓ gif + mp4" if res["mp4"] else f"✗ {res['render_error']}")
            print(" · " + " · ".join(parts))
        # 渲染是幂等的，但产物路径得让 index.json 跟上（否则清单里没有 svg）
        index_path = run_dir / "index.json"
        if index_path.exists() and rendered:
            meta = json.loads(index_path.read_text(encoding="utf-8"))
            by_case = {c.get("case"): c for c in meta.get("cases", [])}
            for case, fields in rendered.items():
                if case in by_case:
                    fields.pop("svg_error", None)
                    fields.pop("render_error", None)
                    by_case[case].update(fields)
            index_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"index 已更新（补上渲染产物路径）：{index_path}")
        return

    titles = list_cases(args.stage)
    cases = args.cases or list(titles)
    if not cases:
        raise SystemExit("没有可录的 case")
    unknown = [c for c in cases if c not in titles]
    if unknown:
        raise SystemExit(f"没有这些 case：{', '.join(unknown)}；可选：{', '.join(titles)}")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = stage_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"stage {args.stage} · {len(cases)} 个 case → {run_dir}", flush=True)
    index: list[dict] = []
    for case in cases:
        title = titles.get(case, "")
        print(f"▶ {case} · {title} ..." if title else f"▶ {case} ...", end="", flush=True)
        rec = record_case(
            args.stage,
            case,
            run_dir,
            title=title,
            window=args.window_size,
            idle=args.idle_time_limit,
            timeout=args.timeout,
        )
        if args.render_svg:
            rec.update(render_svg(run_dir / f"{case}.cast", args.window_size))
        if args.render:
            rec.update(render_cast(run_dir / f"{case}.cast"))
        index.append(rec)
        flag = "超时" if rec["timeout"] else f"exit={rec['exit']}"
        tail = ""
        if args.render_svg:
            tail += " · " + ("✓ svg" if rec.get("svg") else f"svg 渲染失败：{rec['svg_error']}")
        if args.render:
            tail += " · " + ("✓ gif + mp4" if rec.get("mp4") else f"gif 渲染失败：{rec['render_error']}")
        print(f" {flag} · {rec['secs']}s · {rec['cast']}{tail}")

    (run_dir / "index.json").write_text(
        json.dumps(
            {
                "stage": args.stage,
                "run_id": run_id,
                "window_size": args.window_size,
                "idle_time_limit": args.idle_time_limit,
                "cases": index,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    bad = [r["case"] for r in index if r["timeout"] or r["exit"] != 0]
    print(f"\n完成 {len(index)} 个；异常 {len(bad)} 个" + (f"：{', '.join(bad)}" if bad else ""))
    print(f"index: {run_dir / 'index.json'}")


if __name__ == "__main__":
    cli()
