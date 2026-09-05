# Evidence ledger

Use a small JSON file as the boundary between video analysis and tutorial writing.

```json
{
  "source": {
    "id": "stable video id",
    "url": "https://…",
    "title": "…",
    "creator": "…",
    "duration_seconds": 57
  },
  "steps": [
    {
      "number": 1,
      "time_start": 2,
      "time_end": 6,
      "title": "Outcome-oriented title",
      "evidence_frame": "frames/frame_005.jpg",
      "claims": [
        {"text": "Add a cube", "status": "shown", "at": 4},
        {"text": "Apply scale before modifiers", "status": "recommendation"}
      ],
      "expected": "Observable result after the step"
    }
  ]
}
```

Required invariants:

- Step numbers are unique and ascending.
- Time ranges are inside the source duration and do not run backward.
- Every `shown` or `inferred` claim has an `at` timestamp within its step.
- Every evidence frame exists and represents the same operation as the step.
- Exact numbers and graph connections are `shown` only after visual confirmation.
- `recommendation` claims are useful execution guidance but are never attributed to the video.

Keep the ledger compact. It is not a transcript, scene log, or prose draft.
