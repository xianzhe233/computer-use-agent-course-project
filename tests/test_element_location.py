from pathlib import Path

from computer_use_agent.tools.element_location import (
    ElementLocationCandidate,
    bbox_center,
    locate_element,
)


class FakeElementLocatorBackend:
    def __init__(
        self,
        candidates: list[ElementLocationCandidate] | None = None,
        *,
        should_fail: bool = False,
    ) -> None:
        self.candidates = candidates or []
        self.should_fail = should_fail
        self.calls: list[tuple[str, Path, str]] = []

    def locate(
        self,
        *,
        query: str,
        screenshot_path: Path,
        screenshot_id: str = "",
    ) -> list[ElementLocationCandidate]:
        self.calls.append((query, screenshot_path, screenshot_id))
        if self.should_fail:
            raise RuntimeError("backend failed")
        return self.candidates


def test_locate_element_returns_best_candidate_with_required_fields(tmp_path: Path) -> None:
    screenshot_path = tmp_path / "screenshots" / "ss_0001.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(b"fake-png")
    backend = FakeElementLocatorBackend(
        candidates=[
            ElementLocationCandidate(
                bbox=(10, 20, 70, 80),
                confidence=0.66,
                source="uia",
                reason="partial title match",
            ),
            ElementLocationCandidate(
                bbox=(100, 120, 220, 180),
                confidence=0.91,
                source="hybrid",
                reason="uia candidate aligned with visual region",
            ),
        ]
    )

    result = locate_element(
        query="保存按钮",
        screenshot_path=screenshot_path,
        screenshot_id="ss_0001",
        backend=backend,
    )

    assert result.success is True
    assert result.bbox == (100, 120, 220, 180)
    assert result.confidence == 0.91
    assert result.reason == "uia candidate aligned with visual region"
    assert result.source == "hybrid"
    assert result.artifacts == ["screenshot:ss_0001"]
    assert len(result.candidates) == 2
    assert backend.calls == [("保存按钮", screenshot_path, "ss_0001")]


def test_locate_element_returns_structured_error_when_not_found(tmp_path: Path) -> None:
    screenshot_path = tmp_path / "ss_0002.png"
    screenshot_path.write_bytes(b"fake-png")

    result = locate_element(
        query="地址栏",
        screenshot_path=screenshot_path,
        screenshot_id="ss_0002",
        backend=FakeElementLocatorBackend(),
    )

    assert result.success is False
    assert result.reason == "no matching element candidate found"
    assert result.error == {
        "code": "ELEMENT_NOT_FOUND",
        "message": "No element matched query: 地址栏",
    }


def test_locate_element_validates_query_and_screenshot_path(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.png"

    empty_query_result = locate_element(query="   ", screenshot_path=missing_path)
    missing_shot_result = locate_element(query="菜单", screenshot_path=missing_path)

    assert empty_query_result.error == {
        "code": "INVALID_QUERY",
        "message": "locate_element requires a non-empty query",
    }
    assert missing_shot_result.error == {
        "code": "SCREENSHOT_NOT_FOUND",
        "message": f"Screenshot not found: {missing_path}",
    }


def test_locate_element_returns_backend_failure(tmp_path: Path) -> None:
    screenshot_path = tmp_path / "ss_0003.png"
    screenshot_path.write_bytes(b"fake-png")

    result = locate_element(
        query="搜索框",
        screenshot_path=screenshot_path,
        backend=FakeElementLocatorBackend(should_fail=True),
    )

    assert result.success is False
    assert result.error == {
        "code": "LOCATOR_BACKEND_FAILED",
        "message": "RuntimeError: backend failed",
    }


def test_bbox_center_returns_center_point() -> None:
    assert bbox_center((10, 20, 30, 60)) == (20, 40)
