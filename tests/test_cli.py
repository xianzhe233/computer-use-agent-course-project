from computer_use_agent.cli import build_parser


def test_cli_defaults_to_autonomous_mode() -> None:
    args = build_parser().parse_args(["列出当前目录文件"])

    assert args.mode == "autonomous"
    assert args.step_timeout == 180


def test_cli_supports_explicit_mock_mode() -> None:
    args = build_parser().parse_args(["--mode", "mock", "列出当前目录文件"])

    assert args.mode == "mock"


def test_cli_supports_quiet_progress_flag() -> None:
    args = build_parser().parse_args(["--quiet", "列出当前目录文件"])

    assert args.quiet is True
