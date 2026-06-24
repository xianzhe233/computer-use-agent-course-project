from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CASES_DIR = SCRIPT_DIR / "cases"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and run one OSWorld-style local test case.")
    parser.add_argument("--case", default="h2o_subscript", help="Case id under OSWorld_examples/cases/.")
    parser.add_argument("--list-cases", action="store_true", help="List available cases and exit.")
    parser.add_argument("--prepare-only", action="store_true", help="Prepare files but do not open apps or start agent.")
    parser.add_argument("--force-close-apps", action="store_true", help="Close configured apps before restoring files.")
    parser.add_argument("--mode", help="Override agent mode from case config.")
    parser.add_argument("--max-steps", type=int, default=50, help="Agent max steps.")
    parser.add_argument("--step-timeout", type=int, default=180, help="Agent tool timeout in seconds.")
    parser.add_argument("--model-config", default=str(REPO_ROOT / "config" / "models.local.json"), help="Model config path.")
    parser.add_argument("--model-role", default="mainAgent", help="Model role from model config.")
    parser.add_argument("--quiet", action="store_true", help="Pass --quiet to agent CLI.")
    parser.add_argument(
        "--pass-through-agent-exit-code",
        action="store_true",
        help="Exit with the agent CLI exit code instead of always exiting 0 after a completed run.",
    )
    return parser


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def list_cases() -> None:
    for case_path in sorted(CASES_DIR.glob("*.json")):
        case_config = load_json(case_path)
        print(f"{case_config['case_id']} - {case_config['display_name']}")


def load_case(case_id: str) -> dict[str, Any]:
    case_path = CASES_DIR / f"{case_id}.json"
    if not case_path.exists():
        raise FileNotFoundError(f"Case config not found: {case_path}")
    return load_json(case_path)


def expand_template(value: str, template_map: dict[str, str]) -> str:
    result = value
    for key, replacement in template_map.items():
        result = result.replace("{" + key + "}", replacement)
    return result


def get_desktop_path() -> Path:
    if os.name == "nt":
        user_profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
        return user_profile / "Desktop"
    return Path.home() / "Desktop"


def close_process(process_name: str) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/IM", f"{process_name}.exe", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    subprocess.run(["pkill", "-f", process_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def process_running(process_name: str) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}.exe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="mbcs",
            errors="ignore",
            check=False,
        )
        return f"{process_name}.exe".lower() in result.stdout.lower()
    result = subprocess.run(["pgrep", "-f", process_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def open_document(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path)])


def prepare_case(
    case_config: dict[str, Any], force_close_apps: bool
) -> tuple[list[Path], list[Path], list[str], list[str], list[Path], Path, str, str]:
    template_map = {
        "USERPROFILE": os.environ.get("USERPROFILE", str(Path.home())),
        "DESKTOP": str(get_desktop_path()),
        "REPO_ROOT": str(REPO_ROOT),
        "SCRIPT_ROOT": str(SCRIPT_DIR),
    }

    for process_name in case_config.get("processes_to_close_before_prepare", []):
        process_name = str(process_name)
        if process_running(process_name):
            if force_close_apps:
                close_process(process_name)
                print(f"Closed process: {process_name}")
            else:
                raise RuntimeError(f"Process {process_name} is running. Close it first or rerun with --force-close-apps.")

    prepared_targets: list[Path] = []
    reference_targets: list[Path] = []
    expected_outputs: list[str] = []
    external_targets: list[str] = []
    targets_to_open: list[Path] = []

    for index, file_config in enumerate(case_config["initial_files"]):
        asset_path = SCRIPT_DIR / file_config["asset"]
        target_path = Path(expand_template(file_config["target"], template_map))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset_path, target_path)
        prepared_targets.append(target_path)
        template_map[f"TARGET_{index}"] = str(target_path)
        if file_config.get("open_after_prepare", False):
            targets_to_open.append(target_path)

    for index, file_config in enumerate(case_config.get("reference_files", [])):
        reference_path = SCRIPT_DIR / file_config["asset"]
        reference_targets.append(reference_path)
        template_map[f"REFERENCE_{index}"] = str(reference_path)

    for index, output_config in enumerate(case_config.get("expected_outputs", [])):
        output_path = expand_template(str(output_config["path"]), template_map)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        expected_outputs.append(output_path)
        template_map[f"EXPECTED_OUTPUT_{index}"] = output_path

    for index, external_config in enumerate(case_config.get("external_targets", [])):
        url = str(external_config["url"])
        external_targets.append(url)
        template_map[f"EXTERNAL_{index}"] = url

    source_json_path = SCRIPT_DIR / case_config["source_json"]
    template_map["SOURCE_JSON"] = str(source_json_path)
    agent_task = expand_template(case_config["agent_task_template"], template_map)
    manual_check = expand_template(case_config["manual_check"], template_map)
    return prepared_targets, reference_targets, expected_outputs, external_targets, targets_to_open, source_json_path, agent_task, manual_check


def run_agent(args: argparse.Namespace, case_config: dict[str, Any], agent_task: str) -> int:
    mode = args.mode or case_config.get("agent_mode", "autonomous")
    command = [
        "uv",
        "run",
        "python",
        "-m",
        "computer_use_agent.cli",
        agent_task,
        "--mode",
        str(mode),
        "--workspace",
        str(REPO_ROOT),
        "--runs-root",
        str(REPO_ROOT / "runs"),
        "--max-steps",
        str(args.max_steps),
        "--step-timeout",
        str(args.step_timeout),
        "--model-config",
        str(Path(args.model_config).resolve()),
        "--model-role",
        args.model_role,
    ]
    if args.quiet:
        command.append("--quiet")

    print("Starting agent...")
    print(f"Task: {agent_task}")
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    return completed.returncode


def main() -> int:
    args = build_parser().parse_args()
    if args.list_cases:
        list_cases()
        return 0

    case_config = load_case(args.case)
    (
        prepared_targets,
        reference_targets,
        expected_outputs,
        external_targets,
        targets_to_open,
        source_json_path,
        agent_task,
        manual_check,
    ) = prepare_case(case_config, args.force_close_apps)

    print(f"Case: {case_config['case_id']} - {case_config['display_name']}")
    print("Prepared files:")
    for target_path in prepared_targets:
        print(f"  - {target_path}")
    print(f"Source JSON: {source_json_path}")
    print("Reference files:")
    for reference_path in reference_targets:
        print(f"  - {reference_path}")
    if expected_outputs:
        print("Expected output paths:")
        for output_path in expected_outputs:
            print(f"  - {output_path}")
    if external_targets:
        print("External targets:")
        for url in external_targets:
            print(f"  - {url}")

    if args.prepare_only:
        print("Prepare-only enabled. Agent was not started.")
        print(f"Manual check guidance: {manual_check}")
        return 0

    for target_to_open in targets_to_open:
        open_document(target_to_open)

    launch_wait_seconds = int(case_config.get("app_launch_wait_seconds", 0) or 0)
    if launch_wait_seconds > 0 and targets_to_open:
        time.sleep(launch_wait_seconds)

    agent_exit_code = run_agent(args, case_config, agent_task)
    print(f"Agent runtime exit code: {agent_exit_code}")
    print("This script does not judge task correctness. Please use the manual check below.")
    print(f"Manual check guidance: {manual_check}")
    return agent_exit_code if args.pass_through_agent_exit_code else 0


if __name__ == "__main__":
    raise SystemExit(main())
