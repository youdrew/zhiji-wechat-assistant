"""Progress arithmetic over measured work; no message bodies or speculative totals.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""


def progress_step(previous, changes, at):
    step = {**previous, **changes, "updatedAt": at}
    done, total = step.get("done"), step.get("total")
    unit = step.get("unit")
    # Source-message throughput spans extract/review cycles, but never crosses
    # into a merge layer, a different denominator, or cached-only processing.
    source_phase = step.get("phase") in ("extracting", "reviewing")
    identity = (unit, total, source_phase)
    same = (identity == (previous.get("unit"), previous.get("total"),
                        previous.get("phase") in ("extracting", "reviewing")) and
            step.get("startedAt") == previous.get("startedAt"))
    calls = step.get("successfulCallCount", 0)
    calls = calls if type(calls) is int and calls >= 0 else 0
    last_calls = previous.get("rateSampleCallCount")
    # Older checkpoints have no call baseline; their samples may include the
    # few milliseconds between review completion and saving its source batch.
    known_baseline = same and type(last_calls) is int and last_calls <= calls
    samples = list(previous.get("rateSamples", [])) if known_baseline else []
    if not known_baseline:
        last_calls = previous.get("successfulCallCount", 0) if same else calls
        last_calls = last_calls if type(last_calls) is int and 0 <= last_calls <= calls else calls
    before = previous.get("done")
    measurable = (same and unit == "条" and source_phase and type(before) is int and type(done) is int and
                  type(total) is int and 0 <= before < done <= total)
    if measurable:
        if calls > last_calls:
            # Start with a completed, persisted batch, never a model-finished
            # event whose original-message count has not advanced yet.
            samples.append([at, done])
            samples = samples[-12:]
        else:
            # Cached messages can advance between two new model batches. Drop
            # the old window so that the next interval cannot include that jump.
            samples = []
        last_calls = calls
    elif type(before) is int and type(done) is int and done < before:
        samples, last_calls = [], calls
    step["rateSamples"] = samples
    step["rateSampleCallCount"] = last_calls
    return step


def timed_view(value, at):
    result = dict(value)
    start = value.get("startedAt")
    end = at if value.get("state") == "running" else (value.get("finishedAt") or value.get("updatedAt") or
          max((step["updatedAt"] for step in value.get("steps", []) if type(step.get("updatedAt")) is int), default=None))
    if type(start) is int and type(end) is int:
        result["elapsedMs"] = max(0, end - start)
    result.pop("eta", None)
    if value.get("state") != "running":
        return result
    for step in value.get("steps", []):
        if step.get("state") != "running":
            continue
        samples = step.get("rateSamples", [])
        done, total = step.get("done"), step.get("total")
        # Need multiple persisted advances over real elapsed time. Never use
        # equally weighted task steps or a cached burst to promise completion.
        if (len(samples) < 3 or step.get("unit") != "条" or type(done) is not int or type(total) is not int or
                done >= total or step.get("phase") not in ("extracting", "reviewing")):
            break
        duration = samples[-1][0] - samples[0][0]
        advance = samples[-1][1] - samples[0][1]
        stale = at - samples[-1][0]
        if duration < 5000 or advance <= 0 or stale > max(120000, duration * 2):
            break
        rates = [(right[1] - left[1]) / (right[0] - left[0])
                 for left, right in zip(samples, samples[1:]) if right[0] > left[0] and right[1] > left[1]]
        if len(rates) < 2:
            break
        rate = advance / duration
        slow, fast = min(rates + [rate]) * .7, max(rates + [rate]) * 1.3
        remaining = total - done
        # One stalled interval between two persisted batches drags min(rates) toward
        # zero and once promised "3875 hours" (live 2026-10-29). The band keeps a
        # bounded spread instead of trusting the slowest single interval.
        low = max(1, int(remaining / fast))
        high = max(low, min(int(remaining / slow), low * 4))
        result["eta"] = {"scope": "phase", "label": step["title"] + "提取与复核",
                         "lowMs": low, "highMs": high, "basis": "observed"}
        break
    return result
