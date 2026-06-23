from computer_use_agent.cli import build_parser


def test_cli_defaults_to_autonomous_hybrid_mode() -> None:
    args = build_parser().parse_args(["列出当前目录文件"])

    assert args.mode == "autonomous"
    assert args.max_steps == 20
    assert args.step_timeout == 180


def test_cli_supports_terminal_only_autonomous_mode() -> None:
    args = build_parser().parse_args(["--mode", "autonomous-terminal", "列出当前目录文件"])

    assert args.mode == "autonomous-terminal"


def test_cli_supports_quiet_progress_flag() -> None:
    args = build_parser().parse_args(["--quiet", "列出当前目录文件"])

    assert args.quiet is True
