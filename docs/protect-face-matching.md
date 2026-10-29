# Matching faces against Protect's face DB: plan

Status: implemented 2026-10-20 (`piport/facedb.py`); not yet seen naming anyone live.
The threshold ended up at 0.45 (`AIPORT_FACE_DB_THRESH`), not SFace's 0.363: on a day Ian
wasn't there, 7 of 322 distinct faces scored 0.363 to 0.429 against him.

pi-port finds faces and sends their SFace embedding (`faceEmbed`), but never names anyone:
it only matches against a local photo gallery (`<state-dir>/faces/<Name>/`), which is
empty. Protect sends its own known-faces DB (`UpdateFaceDBRequest`), which pi-port
downloads and ignores.

## What we know

### The DB holds pi-port's own embeddings

`known.db` is a tar of `known/uiface.json` and `known/uiface.db`. On 2026-10-20 it held one
subject, "Ian Vincent" (subject 5, instances 0 and 1):

```json
{"EnrolledSubjectIds": [5], "EnrolledSubjectRelationMap": [[5, [1, 0]]],
 "InstanceIdToNameMap": [[1, "Ian Vincent"], [0, "Ian Vincent"]],
 "InstanceIdToSubjectIdMap": [[1, 5], [0, 5]], "InstanceIdToUniqueIdMap": [[1, 3], [0, 4]],
 "SubjectIdToNameMap": [[5, "Ian Vincent"]], "VerifiedUniqueIds": [3, 4],
 "IgnoredSubjectIds": [], "IngnoredInstanceIds": []}
```

One vector in `uiface.db` is exactly (cosine 1.000) the `faceEmbed` pi-port sent at
2026-10-18 16:54:38, a minute before Ian was named in Protect. Protect enrols the embedding
the device sent, so the DB is in SFace's space and pi-port can match against it directly.
Faces named from another source (Protect's own cameras) don't appear: the user has named
three people in Protect, and only Ian, named on an AI Port face, is in the DB.

### `uiface.db` layout

An **Annoy index** (the firmware's `EnrollDB::LoadEmbeddingData` in `enroll_db.cpp` calls
Annoy's `load(path, prefault, error)`; decompiled with Ghidra, `ubnt_smart_detect` v5.1.12) of
512-float items with the dot-product metric:

- Nodes are 2 064 bytes: a 16-byte header (`n_descendants`, `children[2]`, dot factor), then
  512 float32. Item *i* is instance *i* of `uiface.json`, at byte *i* x 2064; its floats start at
  byte 16. Split and root nodes follow the items (an empty DB is 82 560 bytes; each instance adds
  2 064).
- An item holds up to three 128-float embeddings, at floats 0, 132 and 264 (Ian's instance 0
  had two). The rest of the 512 floats is unused memory: NaNs, huge values, text fragments.
  The firmware logs `Successfully load database with {} subjects, {} instances and {}
  embeddings.`
- Every embedding found in three real DBs (2026-10-18 and 2026-10-21) was an exact copy of one
  pi-port sent, though not always from the event the user named: naming a face group on
  Protect's faces page enrols that group's older faces (an Oct 17 and an Oct 18 face).

### How the AI Port firmware matches (`ubnt_smart_detect`, v5.1.12 strings)

- It loads `uiface.db` and `uiface.json` (`Load FaceDB from path`, `Failed to load
  EnrollDB (no uiface.db or uiface.json)`), on `updateFaceDB` with a `faceDBPath`.
- It ranks subjects by **coverage** and similarity: `Rank {} - subject {} (<{}>) -
  coverage: {} = {} / {}, topSim {}`, then `Selected best match: subj_id={}, name={},
  coverage={:.3f}, similarity={:.3f}`. Coverage is the share of a subject's instances that
  pass the threshold.
- It smooths over a track: `Updated identity candidate for tracker {}: subject_id={},
  name={}, confidence={:.3f}, hitCount={}`, `Assigned final identity ...`, `Expiring
  identity candidate ...`.
- The threshold follows `recognitionAccuracy` (levels include `high` and `medium`; set by
  `SetRecognitionAccuracy`). The numeric thresholds aren't in the strings.
- It also checks head pose and crop quality before recognising (`Skip recognition due to
  large head pose`).

### How a match is reported

The per-object fields the firmware writes include `matchedId` and `matchedName`, next to
`associatedFaceTrackerID`, `verifyType`, `blurness` and `qualityScore`; `ubnt_streamer`
also handles `faceEmbed`, `matchedId` and `matchedName`. Protect's detection service logs
the same attributes on every object (`matchedId`, `matchedName`, `namesTopK`,
`topKCandidate`, `faceVerifyStatus`). All captured values are null: no capture has a real
AI Port match, so the value types are a guess. Most likely `matchedId` is the subject id
(an integer, as in `uiface.json`) and `matchedName` the name.

## Change

### 1. `piport/facedb.py`

- `load(path)`: open the tar, read `uiface.json`, and read each instance's embeddings from
  `uiface.db` (item *i* at byte *i* x 2064 + 16, embeddings at floats 0, 132 and 264, unused
  blocks skipped), for every instance id in `InstanceIdToSubjectIdMap`. Return instances as
  `(instance_id, subject_id, name, vector)`, skipping `IgnoredSubjectIds` and
  `IngnoredInstanceIds` (sic).
- Sanity check: every vector finite, with a norm like SFace's (about 5 to 20); otherwise
  log a warning and load nothing.
- `match(embedding)`: cosine similarity to every instance. Per subject: coverage (share of
  its instances at or above the threshold) and top similarity. Best subject by coverage,
  then top similarity. Returns `(subject_id, name, similarity)` or None. Threshold 0.45
  (`AIPORT_FACE_DB_THRESH`; see the status note).

### 2. Loading

`_download_face_db()` calls `facedb.load()` after saving, and keeps the result. Also load
the saved copy at startup, so matching works before Protect re-sends it.

### 3. Matching in `enrich.py`

Where a face embedding is computed, try the Protect DB first, then the local gallery. Keep
the best match per face track, as the firmware's identity candidate does: the name is set
once a track has matched on at least 2 reads (`hitCount`), and doesn't flip on one read.

### 4. Reporting

On the face descriptor, and on its person (via `associatedFaceTrackerID`), send
`matchedId` (subject id) and `matchedName`; also put them in the leave's
`trackerIDAttrMap`. Keep `name`/`tag` as now for the local gallery.

### 5. Tests

- `facedb.load()` on a synthetic tar built to the layout above, and on the real DB's
  shape (2 instances, 1 subject); a corrupt or short file loads nothing.
- `match()`: the exact vector matches; coverage beats a single high-similarity instance;
  below the threshold gives None; ignored subjects never match.
- Descriptors carry `matchedId`/`matchedName` once a track has 2 matching reads.

### 6. Checking it live

1. In Protect, name the other two people on faces that came from the AI Port cameras, so
   the DB has three subjects. Check the downloaded DB: three subjects, each instance's
   embeddings equal to a `faceEmbed` pi-port sent. (The layout is now confirmed from the
   firmware; see above.)
2. One of them walks past a camera. The log shows the match and its similarity; Protect
   shows the name on the event. If Protect ignores `matchedId`/`matchedName`, try
   `matchedId` as the instance's unique id, then as a string.

## Open questions

- The value types of `matchedId`/`matchedName`, and whether Protect needs `namesTopK` or
  `topKCandidate` too.
- The firmware's thresholds for each `recognitionAccuracy` level.
- What the records after the instances are (not needed for matching).

## What Protect does with it (2026-10-21, Protect's `ds.log`)

A walk-by at 09:43 on Camera C showed the protocol works: when a face track carries
`matchedId`, Protect logs `using camera matchedId=178 (no ai-feature-console call)` and
`Face recognition → unifi-protect: group_id=178 name="Alex Example"`, and labels the event
`group:face_178`, `groupType:known`. A face with only `faceEmbed` makes Protect run its own
search (`ai-feature-console search_group`), which may attach an unknown face group.

Two things stopped that event showing the name, both fixed:

- **One face track per person.** Faces are read about once a second; as the person walked up,
  their face grew from 56 to 92 px and moved further between reads than the track matcher
  allows, and became face tracks 5, 7 and 8. Now a face always continues the face track of
  the person whose box holds it (`face_tid`), a named person's match carries over to its face,
  and `associatedFaceTrackerID` is the person's own face track.
- **The embedding waits for a decision.** The first, unmatched embedding made Protect attach
  unknown face group 50 a second before pi-port's match arrived. A face's `faceEmbed` is now
  held out of enter/moving updates until the face is named or has had 2 fresh reads without a
  match; the leave always carries it.

### The leave replaced the name (2026-10-21 11:18)

With one face track and the embedding held until named, Protect's `ds.log` showed
`using camera matchedId=186` and `group_id=186 name="Alex Example"` for the whole live phase.
When the event closed, its object recognition clustered the leave's `faceEmbed` itself into a
new unknown group (`group:face_189`, `groupType:unknown`), which replaced the name. A named
face's leave now carries `matchedId`/`matchedName` and no `faceEmbed`; an unnamed face's leave
still carries its embedding, so Protect clusters strangers.
