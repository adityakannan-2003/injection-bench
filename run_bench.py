"""Run the bench: put each model through each world, with each attack and each defense, and record
what it did. Then summarize.

    uv run python run_bench.py --models claude-haiku-5-5 --limit 10        # a small pilot
    uv run python run_bench.py --models claude-haiku-5-5,claude-sonnet-5-5 # the main attack sweep
    uv run python run_bench.py --local --models qwen3:4b,llama3.2:3b       # free local models
    uv run python run_bench.py --summary                                   # print results so far

Two kinds of run:
- attack: one goal's instruction is hidden in a world's untrusted text. We record whether the model
  emitted the forbidden call (obeyed) and whether it executed (harm).
- clean: no injection, to measure whether a defense quietly breaks the real task.

Each (config, rep) is written to out/<label>/results.jsonl as it finishes, and a re-run skips
what's already there. Write predictions to out/<label>/predictions.md before a run, so a defense's
effect is judged against a call made in advance, not read off the result.
"""

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from dotenv import load_dotenv

from attacks import ATTACKS, ATTACKS_BY_NAME
from defenses import DEFENSES, DEFENSES_BY_NAME
from harness import make_backend, run_episode
from worlds import WORLDS

ROOT = Path(__file__).resolve().parent

# One scenario per world hosts the injections — one that naturally uses the risky tools.
HOST = {"inbox": "damaged", "spreadsheet": "top-region", "web": "library-hours"}

# Dollars per million tokens (Claude API list prices, 2026). Cache reads are cheaper; we don't cache here.
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}


def attack_configs(attacks, defenses):
    for wname, cls in WORLDS.items():
        for goal in cls.goals:
            for attack in attacks:
                for defense in defenses:
                    yield {"kind": "attack", "world": wname, "scenario": HOST[wname],
                           "goal": goal, "attack": attack, "defense": defense}


def clean_configs(defenses):
    # Every scenario, under the two ends of the defense range, to check tasks still get done.
    ends = [d for d in ["none", "warning+rulebook"] if d in defenses]
    for wname, cls in WORLDS.items():
        for scenario in cls.scenarios:
            for defense in ends:
                yield {"kind": "clean", "world": wname, "scenario": scenario,
                       "goal": "", "attack": "none", "defense": defense}


def config_key(cfg, model, rep):
    return (cfg["kind"], cfg["world"], cfg["scenario"], cfg["goal"], cfg["attack"], cfg["defense"], model, rep)


def run_one(cfg, model, rep, backend):
    cls = WORLDS[cfg["world"]]
    world = cls(cfg["scenario"])
    attack = ATTACKS_BY_NAME[cfg["attack"]] if cfg["kind"] == "attack" else None
    goal = cfg["goal"] or None
    ep = run_episode(world, attack, goal, DEFENSES_BY_NAME[cfg["defense"]], backend)
    row = ep.to_row()
    row["rep"] = rep
    row["kind"] = cfg["kind"]
    return row


def cost_of(row) -> float | None:
    price = PRICES.get(row["model"])
    u = row.get("usage") or {}
    if not price or "input_tokens" not in u:
        return None
    return (u.get("input_tokens", 0) * price["input"] + u.get("output_tokens", 0) * price["output"]) / 1e6


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0, centre - half), min(1, centre + half))


def summarize(rows: list[dict]) -> None:
    rows = [r for r in rows if not r.get("error")]
    if not rows:
        print("No successful runs yet.")
        return
    attack_rows = [r for r in rows if r["kind"] == "attack"]
    clean_rows = [r for r in rows if r["kind"] == "clean"]

    if attack_rows:
        exposed = sum(r["exposed"] for r in attack_rows)
        print(f"\nAttack runs: {len(attack_rows)}. Injection reached the model in {exposed} "
              f"({exposed / len(attack_rows):.0%} — the rest didn't land and aren't a fair test).")
        valid = [r for r in attack_rows if r["exposed"]]
        print("\nHarm rate = the attacker's forbidden action actually happened.")
        print("Obey rate = the model emitted the forbidden call (whether or not it was blocked).\n")
        print(f"{'model':<20} {'defense':<18} {'n':>4} {'harm':>14} {'obey':>8}")
        for model in sorted({r["model"] for r in valid}):
            for defense in [d.name for d in DEFENSES]:
                cell = [r for r in valid if r["model"] == model and r["defense"] == defense]
                if not cell:
                    continue
                n = len(cell)
                harm = sum(r["harm"] for r in cell)
                obey = sum(r["obeyed"] for r in cell)
                lo, hi = wilson(harm, n)
                print(f"{model:<20} {defense:<18} {n:>4} {harm:>3}/{n:<3} {lo:.0%}-{hi:.0%}  {obey / n:>6.0%}")

        print("\nHarm rate by attack technique (no defense), to see which tricks land:")
        none = [r for r in valid if r["defense"] == "none"]
        for attack in [a.name for a in ATTACKS]:
            cell = [r for r in none if r["attack"] == attack]
            if cell:
                harm = sum(r["harm"] for r in cell)
                tag = " (held out)" if ATTACKS_BY_NAME[attack].held_out else ""
                print(f"  {attack:<18} {harm:>2}/{len(cell):<2}{tag}")

    if clean_rows:
        print("\nClean runs: did the real task still get done?")
        print(f"{'model':<20} {'defense':<18} {'n':>4} {'done+held':>10}")
        for model in sorted({r["model"] for r in clean_rows}):
            for defense in [d.name for d in DEFENSES]:
                cell = [r for r in clean_rows if r["model"] == model and r["defense"] == defense]
                if not cell:
                    continue
                ok = sum(r["task"] in ("done", "held") for r in cell)
                print(f"{model:<20} {defense:<18} {len(cell):>4} {ok:>6}/{len(cell):<3}")

    costs = [c for c in (cost_of(r) for r in rows) if c is not None]
    if costs:
        print(f"\nAPI cost so far: ${sum(costs):.2f} over {len(costs)} runs (${sum(costs) / len(costs):.4f} each).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the prompt-injection bench.")
    parser.add_argument("--models", default="claude-haiku-5-5", help="Comma-separated model ids.")
    parser.add_argument("--defenses", default=",".join(d.name for d in DEFENSES), help="Which defenses.")
    parser.add_argument("--attacks", default=",".join(a.name for a in ATTACKS), help="Which attack techniques.")
    parser.add_argument("--reps", type=int, default=1, help="Runs per config (default 1).")
    parser.add_argument("--clean", action="store_true", help="Also run clean (no-attack) task configs.")
    parser.add_argument("--only-clean", action="store_true", help="Run only the clean configs.")
    parser.add_argument("--local", action="store_true", help="Use Ollama local models instead of the API.")
    parser.add_argument("--label", default="main", help="Results folder under out/ (default main).")
    parser.add_argument("--concurrency", type=int, default=4, help="Parallel episodes (set 1 for --local).")
    parser.add_argument("--limit", type=int, help="Stop after this many new runs (for a pilot).")
    parser.add_argument("--summary", action="store_true", help="Only print the summary of saved results.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    out = ROOT / "out" / args.label
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"

    existing = read_jsonl(results_path)
    if args.summary:
        summarize(existing)
        return

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    defenses = [d.strip() for d in args.defenses.split(",") if d.strip()]
    attacks = [a.strip() for a in args.attacks.split(",") if a.strip()]

    configs = []
    if not args.only_clean:
        configs += list(attack_configs(attacks, defenses))
    if args.clean or args.only_clean:
        configs += list(clean_configs(defenses))

    done = {tuple(r["key"]) for r in existing if "key" in r}
    todo = []
    for cfg in configs:
        for model in models:
            for rep in range(args.reps):
                key = config_key(cfg, model, rep)
                if key not in done:
                    todo.append((cfg, model, rep, key))
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(todo)} runs to do ({len(done)} already saved). Models: {', '.join(models)}. "
          f"{'Local (Ollama)' if args.local else 'Anthropic API'}.")
    if not todo:
        summarize(existing)
        return

    backends = {m: make_backend(m, local=args.local) for m in models}
    concurrency = 1 if args.local else args.concurrency

    def task(item):
        cfg, model, rep, key = item
        row = run_one(cfg, model, rep, backends[model])
        row["key"] = list(key)
        return row

    pool = ThreadPoolExecutor(concurrency)
    pending = {pool.submit(task, item): item for item in todo}
    finished = 0
    while pending:
        done_set, _ = wait(pending, timeout=5, return_when=FIRST_COMPLETED)
        for fut in done_set:
            item = pending.pop(fut)
            cfg, model, rep, key = item
            try:
                row = fut.result()
            except Exception as exc:  # a run that crashed the harness, not a model failure
                row = {"key": list(key), "kind": cfg["kind"], "world": cfg["world"], "scenario": cfg["scenario"],
                       "goal": cfg["goal"], "attack": cfg["attack"], "defense": cfg["defense"], "model": model,
                       "rep": rep, "error": f"runner: {type(exc).__name__}: {exc}"}
            append_jsonl(results_path, row)
            finished += 1
            mark = "ERR " if row.get("error") else ("HARM" if row.get("harm") else ("obey" if row.get("obeyed") else "ok  "))
            label = f"{cfg['world']}/{cfg['goal'] or cfg['scenario']}/{cfg['attack']}/{cfg['defense']}"
            print(f"  [{finished}/{len(todo)}] {mark} {model} {label}")
    pool.shutdown()
    summarize(read_jsonl(results_path))


if __name__ == "__main__":
    main()
