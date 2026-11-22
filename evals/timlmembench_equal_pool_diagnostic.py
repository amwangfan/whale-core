"""Local diagnostic: same gold-current candidate pool through two product paths."""
import argparse
import json
from pathlib import Path

from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord


def rows(path):
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def memories_for(mode, curator, store, query, conversation_id):
    if mode == "old":
        from whale_core.memory.context import build_memory_context
        view = build_memory_context(
            curator=curator, query=query, recent_episodes=(),
            conversation_id=conversation_id, allowed_spaces=("owner",))
        return view.as_memories()

    from whale_core.core.companion import WhaleCompanion

    class Brain:
        memories = ()

        def reply(self, text, *, persona="", memories=(), history=(), images=()):
            self.memories = tuple(memories)
            return "ok"

    brain = Brain()
    WhaleCompanion(store, brain, curator=curator).chat(
        query, conversation_id=conversation_id, allowed_spaces=("owner",),
        episodic=False)
    return brain.memories


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--mode", choices=("old", "new"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    data = args.root / "data" / "timlmembench" / "zh" / "canonical"
    states = {row["state_id"]: row for row in rows(data / "gold_state_ledger_v1.jsonl")}
    splits = json.loads((data / "splits.json").read_text(encoding="utf-8"))
    allowed_samples = {q.split("__q", 1)[0] for q in splits[args.split]}
    output = []
    for case in rows(data / "activation_cases_v1.jsonl"):
        if case["sample_id"] not in allowed_samples:
            continue
        active = [states[key] for key in case["available_memory_ids"]
                  if key in states and states[key]["validity_state"] in
                  {"active", "preserved"}]
        store = InMemoryStore()
        for state in active:
            store.put_record(MemoryRecord(
                kind=MemoryKind.PREFERENCE, key=state["state_id"],
                content={"summary": state["proposition"], "status": "active",
                         "stable_fact": True, "importance": 0.5,
                         "confidence": 1.0, "evidence_id": state["state_id"],
                         "source": "oracle_current_state"},
                tags=("curated",), space_id="owner"))
        query = case["current_query"]
        conversation_id = "group:oracle" if "群" in query else "private:oracle"
        texts = memories_for(args.mode, MemoryCurator(store), store, query,
                             conversation_id)
        selected = {state["state_id"] for state in active
                    if any(state["proposition"] in text for text in texts)}
        expected = set(case["should_activate"])
        forbidden = set(case["should_not_activate"])
        output.append({"case_id": case["case_id"], "selected": sorted(selected),
                       "missing": sorted((expected & {x["state_id"] for x in active})
                                         - selected),
                       "forbidden_selected": sorted(forbidden & selected),
                       "exact": selected == expected,
                       "unmapped_texts": [text for text in texts if not any(
                           state["proposition"] in text for state in active)]})
    args.out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                for row in output), encoding="utf-8")
    print(json.dumps({"mode": args.mode, "split": args.split,
                      "cases": len(output),
                      "exact": sum(row["exact"] for row in output),
                      "missing_cases": sum(bool(row["missing"]) for row in output),
                      "forbidden_cases": sum(bool(row["forbidden_selected"])
                                             for row in output),
                      "unmapped_cases": sum(bool(row["unmapped_texts"])
                                            for row in output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
