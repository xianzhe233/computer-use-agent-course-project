from pathlib import Path

from PIL import Image
from langchain_core.messages import HumanMessage

from computer_use_agent.tools.element_location import (
    ElementLocationCandidate,
    LocatorCandidatePayload,
    LocatorResponsePayload,
    UITarsElementLocator,
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


class FakeStructuredClient:
    def __init__(self, payload: LocatorResponsePayload) -> None:
        self.payload = payload
        self.calls: list[list[object]] = []

    def invoke_structured(self, messages: list[object], *, schema: type[LocatorResponsePayload], temperature: float = 0.0) -> LocatorResponsePayload:
        self.calls.append(messages)
        assert schema is LocatorResponsePayload
        assert temperature == 0.0
        return self.payload


def test_locate_element_returns_best_candidate_with_required_fields(tmp_path: Path) -> None:
    screenshot_path = tmp_path / "screenshots" / "ss_0001.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(b"fake-png")
    backend = FakeElementLocatorBackend(
        candidates=[
            ElementLocationCandidate(
                point=(10, 20),
                confidence=0.66,
                source="vision",
                reason="partial area match",
            ),
            ElementLocationCandidate(
                point=(100, 120),
                confidence=0.91,
                source="vision",
                reason="best visual point",
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
    assert result.point == (100, 120)
    assert result.confidence == 0.91
    assert result.reason == "best visual point"
    assert result.source == "vision"
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


def test_uitars_locator_uses_langchain_messages_and_structured_output(tmp_path: Path) -> None:
    screenshot_path = tmp_path / "locator.png"
    Image.new("RGB", (400, 200), "white").save(screenshot_path)
    client = FakeStructuredClient(
        LocatorResponsePayload(
            candidates=[
                LocatorCandidatePayload(
                    point=[100, 50],
                    confidence=0.92,
                    reason="matched search box",
                )
            ]
        )
    )
    locator = UITarsElementLocator(client=client, max_side=400, max_candidates=3)

    candidates = locator.locate(query="搜索框", screenshot_path=screenshot_path, screenshot_id="ss_0099")

    assert len(candidates) == 1
    assert candidates[0].point == (100, 50)
    assert candidates[0].confidence == 0.92
    assert candidates[0].reason == "matched search box"
    assert len(client.calls) == 1
    messages = client.calls[0]
    assert len(messages) == 2
    assert isinstance(messages[1], HumanMessage)
    content = messages[1].content
    assert isinstance(content, list)
    text_part = content[0]
    assert isinstance(text_part, dict)
    assert "元素描述：搜索框" in str(text_part.get("text", ""))
    image_part = content[1]
    assert isinstance(image_part, dict)
    assert str(image_part.get("type", "")) == "image_url"
    image_url = image_part.get("image_url")
    assert isinstance(image_url, dict)
    assert str(image_url.get("url", "")).startswith("data:image/jpeg;base64,")
