"""Recorded AI replies (improvement plan Q1): recorded once, replayed without a model, a changed prompt is a miss."""

import json
from pathlib import Path

from predict import predict_agreement_pipeline
from replay import message_key, replay_gateway

HERE = Path(__file__).resolve().parent


def _doc(doc_id: str) -> bytes:
    label = json.loads((HERE / "labels" / f"{doc_id}.json").read_text(encoding="utf-8"))
    return (HERE / "documents" / label["sourceFile"]).read_bytes()


def test_record_then_replay_gives_the_same_result_without_a_model(tmp_path):
    asked = []

    def live(route):
        return lambda messages: asked.append(route) or '{"items": []}'

    recorded = replay_gateway(tmp_path, live)
    first = predict_agreement_pipeline("AGR-011", _doc("AGR-011"), recorded)
    assert recorded.replies.recorded == len(asked) > 0 and recorded.replies.misses == 0

    replayed = replay_gateway(tmp_path)  # no live model at all
    second = predict_agreement_pipeline("AGR-011", _doc("AGR-011"), replayed)
    assert replayed.replies.hits == len(asked) and replayed.replies.misses == 0
    assert [c["metric"] for c in second["covenants"]] == [c["metric"] for c in first["covenants"]]


def test_a_document_or_prompt_never_recorded_is_a_miss(tmp_path):
    gateway = replay_gateway(tmp_path)
    predict_agreement_pipeline("AGR-015", _doc("AGR-015"), gateway)
    assert gateway.replies.misses > 0 and gateway.replies.hits == 0


def test_the_key_is_the_exact_messages():
    a = [{"role": "user", "content": "x"}]
    assert message_key(a) == message_key([{"content": "x", "role": "user"}])
    assert message_key(a) != message_key([{"role": "user", "content": "y"}])
