"""Measure the StarWish code adapter on a disposable, deterministic Git fixture."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
from statistics import median
import subprocess
import sys
import tempfile
from time import perf_counter


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def measure(operation, repeats: int = 1):
    samples = []
    for _ in range(repeats):
        start = perf_counter()
        result = operation()
        samples.append(round((perf_counter() - start) * 1000))
    return result, {"median": median(samples), "samples": samples}


def run(file_count: int, repeats: int) -> dict:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required.")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="starwish-graphflow-benchmark-") as directory:
        root = Path(directory).resolve()
        repo = root / "repo"
        repo.mkdir()
        os.environ.update(STARWISH_GRAPHFLOW_WORKSPACE=str(repo), STARWISH_SOUL_DIR=str(root / "soul"),
                          STARWISH_GRAPHFLOW_TASKS="0")
        from app import graphflow, graphflow_context
        from app.db import estimate_tokens

        def git_run(*args):
            subprocess.run([git, *args], cwd=repo, check=True, capture_output=True, timeout=25)

        git_run("init", "-q")
        source = repo / "a_probe.ts"
        source.write_text("export function starwishBenchmarkProbe() { return 101; }\n", encoding="utf-8")
        for n in range(file_count - 2):
            imports = f"import {{ bench{n - 1} }} from './module_{n - 1:05d}';\n" if n else ""
            expression = f"bench{n - 1}() + {n}" if n else "0"
            module = f"{imports}export function bench{n}(): number {{ return {expression}; }}\n"
            (repo / f"module_{n:05d}.ts").write_text(module, encoding="utf-8")
        (repo / "z_probe.ts").write_text("export const tailProbe = 1;\n", encoding="utf-8")
        git_run("add", "--", "*.ts")
        try:
            cold, cold_ms = measure(lambda: graphflow.reindex(mode="full"))
            warm, warm_ms = measure(graphflow.reindex, repeats)
            expected = min(file_count, graphflow.MAX_FILES)
            partial = file_count > graphflow.MAX_FILES
            require(cold["index"]["files"] == expected and cold["index"]["partial"] == partial,
                    "Cold index did not respect the file boundary.")
            require(cold["parsed_or_reused"] == expected and cold["unparsed"] == 0, "Cold parsing was incomplete.")
            require(warm["parsed_or_reused"] == 0 and warm["index"]["updated"] == 0,
                    "Warm index did unexpected parsing or reported unexpected changes.")
            target = "a_probe.ts#starwishBenchmarkProbe"
            before, read_ms = measure(lambda: graphflow.read_batch([target]), repeats)
            require("return 101" in before["text"] and before["index"]["partial"] == partial,
                    "Symbol lookup did not match the fixture or report coverage.")
            context, context_ms = measure(lambda: graphflow_context.build("starwishBenchmarkProbe", 256), repeats)
            require(context["code_status"] == ("partial" if partial else "ready"), "Context misreported index coverage.")
            require(any(a["file"] == "a_probe.ts" for a in context["anchors"]), "Context omitted the known symbol.")
            estimate = estimate_tokens(json.dumps(context, ensure_ascii=False))
            require(estimate == context["tokenBudget"]["estimated"] and estimate <= 256,
                    "Context exceeded its declared token budget.")
            if partial:
                try:
                    graphflow.read_batch(["z_probe.ts"])
                except graphflow.GraphFlowError as exc:
                    require(exc.status_code == 404 and "索引仅部分覆盖" in str(exc),
                            "Missing tail did not indicate partial coverage.")
                else:
                    raise RuntimeError("Tail file escaped the index limit.")
            source.write_text("export function starwishBenchmarkProbe() { return 202; }\n", encoding="utf-8")
            changed, changed_ms = measure(graphflow.reindex)
            require(changed["index"]["updated"] == 1 and changed["parsed_or_reused"] == 1,
                    "Incremental indexing did not isolate the changed file.")
            after = graphflow.read_batch([target])
            require("return 202" in after["text"] and "return 101" not in after["text"],
                    "Symbol lookup returned stale code after the edit.")
            require(not (root / "soul").exists(), "The code benchmark created memory state.")
            return {"provider": cold["provider"], "platform": platform.system(), "source_files": file_count,
                    "indexed": expected, "partial": partial, "context_estimated_tokens": estimate,
                    "reparsed": changed["parsed_or_reused"], "milliseconds": {
                        "cold_index": cold_ms, "warm_index": warm_ms, "symbol_read": read_ms,
                        "context": context_ms, "one_file_refresh": changed_ms}}
        finally:
            graphflow._clear_snapshot()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=2501)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 2 <= args.files <= 2600 or not 1 <= args.repeats <= 10:
        parser.error("Use 2–2600 files and 1–10 repeats.")
    print(json.dumps(run(args.files, args.repeats)))


if __name__ == "__main__":
    main()
