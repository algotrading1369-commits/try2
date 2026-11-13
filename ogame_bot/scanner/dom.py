"""Árbol HTML mínimo, sin ejecutar scripts ni depender de bibliotecas externas."""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterator


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)

    def walk(self) -> Iterator[Node]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def find(self, predicate: Callable[[Node], bool]) -> list[Node]:
        return [node for node in self.walk() if predicate(node)]

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    @property
    def text(self) -> str:
        return " ".join(child if isinstance(child, str) else child.text for child in self.children if not isinstance(child, Node) or child.tag not in {"script", "style"}).strip()


class Document(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(data)

    def by_id(self, identifier: str) -> list[Node]:
        return self.root.find(lambda node: node.attrs.get("id") == identifier)

    def meta(self, name: str) -> str | None:
        nodes = self.root.find(lambda node: node.tag == "meta" and node.attrs.get("name") == name)
        return nodes[0].attrs.get("content") if len(nodes) == 1 else None
