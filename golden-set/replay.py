"""Recorded AI replies for the golden-set gate (improvement plan Q1, 24 Nov 2026).

The production pipeline asks the model gateway for every AI read. Here the gateway answers from files instead of a
model: each reply is stored under the SHA-256 of the exact messages sent (``<dir>/<sha>.json``). A prompt, skill,
page-selection or model-route change sends different messages, so its replies are missing: the gate then reports
them (``misses``) and must be re-recorded with an approved live run, which shows exactly what changed.

    python golden-set/predict.py --out <dir> --pipeline --replies golden-set/replies          # replay (CI)
    python golden-set/predict.py --out <dir> --pipeline --replies <dir> --record               # live (costs tokens)

Recording needs the Azure settings of ``dps/.env`` and spends tokens; replaying needs nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROUTES = ("small", "strong", "vision")


def message_key(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class ReplyMissing(RuntimeError):
    """No recorded reply for these messages: the prompt or the pages changed since the recording."""


class Replies:
    def __init__(self, folder: Path, record_with=None) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.record_with = record_with  # route -> live completion, only when recording
        self.hits = self.misses = self.recorded = self.errors = 0

    def completion(self, route_name: str, deployment: str):
        def call(messages: list[dict]) -> str:
            from dps.agent import usage

            key = message_key(messages)
            path = self.folder / f"{key}.json"
            if path.is_file():
                recorded = json.loads(path.read_text(encoding="utf-8"))
                if recorded["route"] != route_name:
                    self.misses += 1
                    raise ReplyMissing(f"reply {key[:12]} does not match route {route_name}")
                self.hits += 1
                if tokens := recorded.get("tokens"):
                    usage.note(recorded["deployment"], tokens["prompt"], tokens["completion"],
                               cached_tokens=tokens["cached"])
                return recorded["answer"]
            if self.record_with is None:
                self.misses += 1
                raise ReplyMissing(f"no recorded reply {key[:12]} on route {route_name}")
            previous = usage.last_call()
            try:
                answer = self.record_with(route_name)(messages)
            except Exception:
                self.errors += 1
                raise
            result = {"route": route_name, "deployment": deployment, "answer": answer}
            if (last := usage.last_call()) is not None and last is not previous:
                result["deployment"] = last.model
                result["tokens"] = {"prompt": last.prompt_tokens, "completion": last.completion_tokens,
                                    "cached": last.cached_tokens}
            path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            self.recorded += 1
            return answer

        return call


def replay_gateway(folder: Path, record_with=None, *, parallel: int = 1):
    """A real ``dps.agent.gateway.Gateway`` whose provider answers from recorded replies (or records them)."""
    from dps.agent.gateway import Gateway, Route, Routes

    replies = Replies(folder, record_with)
    routes = Routes({name: Route(name, "azure", f"azure/recorded-{name}", retries=0) for name in ROUTES})
    gateway = Gateway(routes, lambda route: replies.completion(route.name, route.deployment), parallel=parallel)
    gateway.replies = replies  # the counts, for the report
    return gateway
