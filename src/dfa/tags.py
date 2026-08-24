"""Persistent player tags: the user's own pre-draft read on players.

Stored as JSON next to the project (not in cache/, which is disposable).
The tag vocabulary is data, not code, so new tags can be added without a
release - the UI renders whatever the store says.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Built-in vocabulary. `tone` drives the badge colour in the UI and how the
# draft board treats the tag (good = mild boost in visibility, warn = caution).
DEFAULT_TAGS = [
    {"id": "hunch", "label": "Hunch", "tone": "good"},
    {"id": "split-share", "label": "Split Share", "tone": "warn"},
    {"id": "injury-likely", "label": "Injury Likely", "tone": "warn"},
    {"id": "undervalued", "label": "Undervalued", "tone": "good"},
]


@dataclass
class TagStore:
    path: Path
    vocabulary: list[dict] = field(default_factory=lambda: [dict(t) for t in DEFAULT_TAGS])
    player_tags: dict[int, list[str]] = field(default_factory=dict)
    notes: dict[int, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: Path) -> "TagStore":
        store = cls(path=path)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                saved_vocab = data.get("vocabulary") or []
                # Union: keep built-ins, honour any custom tags the user added.
                known = {t["id"] for t in store.vocabulary}
                store.vocabulary += [t for t in saved_vocab if t.get("id") not in known]
                store.player_tags = {
                    int(pid): list(tags)
                    for pid, tags in (data.get("player_tags") or {}).items()
                }
                store.notes = {
                    int(pid): note
                    for pid, note in (data.get("notes") or {}).items() if note
                }
            except Exception:
                pass  # a corrupt file must not brick draft prep; start clean
        return store

    def save(self) -> None:
        with self._lock:
            self.path.write_text(json.dumps({
                "vocabulary": self.vocabulary,
                "player_tags": {str(k): v for k, v in self.player_tags.items()},
                "notes": {str(k): v for k, v in self.notes.items()},
            }, indent=1))

    # -- mutation ----------------------------------------------------------

    def toggle(self, player_id: int, tag_id: str) -> list[str]:
        """Flip one tag on a player; returns their tags afterwards."""
        if tag_id not in {t["id"] for t in self.vocabulary}:
            raise KeyError(tag_id)
        with self._lock:
            current = self.player_tags.setdefault(player_id, [])
            if tag_id in current:
                current.remove(tag_id)
                if not current:
                    del self.player_tags[player_id]
                    current = []
            else:
                current.append(tag_id)
        self.save()
        return list(current)

    def add_tag_type(self, label: str, tone: str = "good") -> dict:
        """Extend the vocabulary (the 'I will add other tags later' hook)."""
        tag_id = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
        if not tag_id:
            raise ValueError("empty tag label")
        existing = next((t for t in self.vocabulary if t["id"] == tag_id), None)
        if existing:
            return existing
        tag = {"id": tag_id, "label": label, "tone": tone if tone in ("good", "warn") else "good"}
        with self._lock:
            self.vocabulary.append(tag)
        self.save()
        return tag

    def set_note(self, player_id: int, note: str) -> None:
        with self._lock:
            if note.strip():
                self.notes[player_id] = note.strip()[:500]
            else:
                self.notes.pop(player_id, None)
        self.save()

    # -- queries -----------------------------------------------------------

    def tags_for(self, player_id: int) -> list[str]:
        return list(self.player_tags.get(player_id, []))

    def labels_for(self, player_id: int) -> list[dict]:
        by_id = {t["id"]: t for t in self.vocabulary}
        return [by_id[t] for t in self.player_tags.get(player_id, []) if t in by_id]

    @property
    def tagged_count(self) -> int:
        return len(self.player_tags)
