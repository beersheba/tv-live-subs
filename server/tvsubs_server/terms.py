"""Glossary: a hand-written static part plus terms the translator learns from the stream.

Learned terms enter the prompts only after they were extracted at least MIN_COUNT times (in this or
earlier sessions), so one wrong extraction can't poison the output. Both parts are keyed by source language.
"""
from __future__ import annotations

import os
import threading

MIN_COUNT = 2
LEARNED_CAP = 150


class TermStore:
    def __init__(self, glossary_path: str | None, learned_path: str | None):
        self._learned_path = learned_path
        self._lock = threading.Lock()
        self.static: dict[str, dict[str, str]] = {}
        self._learned: dict[tuple[str, str], list] = {}  # (lang, source) -> [target, count]
        self._load_static(glossary_path)
        self._load_learned()

    def _load_static(self, path: str | None) -> None:
        if not path or not os.path.isfile(path):
            return
        lang = "he"  # entries before any [section] header are Hebrew (the original glossary format)
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                lang = line[1:-1].strip()
            elif "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                self.static.setdefault(lang, {})[k] = v

    def _load_learned(self) -> None:
        if not self._learned_path or not os.path.isfile(self._learned_path):
            return
        for line in open(self._learned_path, encoding="utf-8"):
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 4 and parts[3].isdigit():
                self._learned[(parts[0], parts[1])] = [parts[2], int(parts[3])]

    def static_for(self, lang: str) -> dict[str, str]:
        return dict(self.static.get(lang, {}))

    def learned_for(self, lang: str) -> dict[str, str]:
        with self._lock:
            items = [(k[1], v[0]) for k, v in self._learned.items() if k[0] == lang and v[1] >= MIN_COUNT]
        static = self.static.get(lang, {})
        return {s: t for s, t in items if s not in static}

    def add(self, lang: str, pairs: list[tuple[str, str]]) -> int:
        added = 0
        with self._lock:
            for src, dst in pairs:
                if src in self.static.get(lang, {}):
                    continue
                cur = self._learned.get((lang, src))
                if cur and cur[0] == dst:
                    cur[1] += 1
                elif cur and cur[1] > 1:
                    cur[1] -= 1  # a conflicting rendering weakens a settled one before replacing it
                else:
                    self._learned[(lang, src)] = [dst, 1]
                added += 1
            if len(self._learned) > LEARNED_CAP:  # drop the weakest
                for k, _ in sorted(self._learned.items(), key=lambda kv: kv[1][1])[: len(self._learned) - LEARNED_CAP]:
                    del self._learned[k]
        return added

    def whisper_prompt(self, lang: str, limit: int = 200) -> str:
        """Source-side terms as a Whisper initial prompt (biases spelling of names)."""
        words: list[str] = []
        for term in [*self.static.get(lang, {}), *self.learned_for(lang)]:
            if sum(len(w) + 2 for w in words) + len(term) > limit:
                break
            words.append(term)
        return ", ".join(words)

    def save(self) -> None:
        if not self._learned_path:
            return
        with self._lock:
            rows = [f"{k[0]}\t{k[1]}\t{v[0]}\t{v[1]}" for k, v in self._learned.items()]
        os.makedirs(os.path.dirname(self._learned_path) or ".", exist_ok=True)
        with open(self._learned_path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + ("\n" if rows else ""))


def parse_terms(output: str, source_text: str) -> list[tuple[str, str]]:
    """`source = target` lines from the LLM; keeps only forms that really occur in the source text."""
    out: list[tuple[str, str]] = []
    for line in output.splitlines():
        line = line.strip().lstrip("-*• ").strip("`")
        if "=" not in line or line.upper().startswith("NONE"):
            continue
        src, dst = (x.strip().strip("«»\"'`") for x in line.split("=", 1))
        if 1 < len(src) <= 40 and 1 < len(dst) <= 60 and src != dst and src in source_text:
            out.append((src, dst))
    return out[:8]
