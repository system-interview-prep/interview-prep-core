import base64
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate_vi_stt_collection_plan.py"
SPEC = importlib.util.spec_from_file_location("collection_plan", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

COLLECTOR_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "collect_vi_stt_corpus.py"
COLLECTOR_SPEC = importlib.util.spec_from_file_location("corpus_collector", COLLECTOR_SCRIPT)
assert COLLECTOR_SPEC and COLLECTOR_SPEC.loader
COLLECTOR = importlib.util.module_from_spec(COLLECTOR_SPEC)
COLLECTOR_SPEC.loader.exec_module(COLLECTOR)


def test_collection_plan_has_300_balanced_unique_samples():
    rows = MODULE.generate_plan()
    assert len(rows) == 300
    assert len({row["id"] for row in rows}) == 300
    assert {row["dialect"] for row in rows} == {"north", "central", "south"}
    assert {dialect: sum(row["dialect"] == dialect for row in rows) for dialect in MODULE.DIALECTS} == {
        "north": 100,
        "central": 100,
        "south": 100,
    }
    slots = {row["slotId"] for row in rows}
    assert len(slots) == 30
    assert all(sum(row["slotId"] == slot for row in rows) == 10 for slot in slots)


def test_collector_saves_audio_hash_and_human_ground_truth(tmp_path: Path):
    plan_path = tmp_path / "plan.jsonl"
    plan = MODULE.generate_plan()[:1]
    plan_path.write_text(json.dumps(plan[0], ensure_ascii=False) + "\n", encoding="utf-8")
    store = COLLECTOR.CorpusStore(tmp_path, plan_path)
    raw_audio = b"webm-real-recording" * 100

    result = store.save(
        {
            "id": plan[0]["id"],
            "speakerId": "anonymous-01",
            "reference": "Tôi đã xử lý lỗi bằng cách kiểm tra log.",
            "consent": "internal-eval-consent-v1",
            "durationSeconds": 4.2,
            "mimeType": "audio/webm;codecs=opus",
            "audioBase64": base64.b64encode(raw_audio).decode(),
        }
    )

    assert result == {"saved": plan[0]["id"], "completed": 1, "target": 1}
    manifest = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8"))
    assert manifest["speakerId"] == "anonymous-01"
    assert manifest["reference"].startswith("Tôi đã")
    assert (tmp_path / manifest["audioPath"]).read_bytes() == raw_audio
