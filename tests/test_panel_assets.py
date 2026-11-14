"""Required control/status elements must exist before frontend rendering."""
from html.parser import HTMLParser
from pathlib import Path
import unittest


class ElementIds(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key == "id" and value:
                self.ids.append(value)


class PanelAssetTests(unittest.TestCase):
    def test_control_and_acknowledgement_elements_are_unique_and_available(self) -> None:
        parser = ElementIds()
        parser.feed((Path(__file__).resolve().parents[1] / "ogame_bot/panel_assets/index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertTrue({"start", "stop", "notice", "save", "settingsFields", "instructionStatus", "installation", "consoleOutput"} <= set(parser.ids))
