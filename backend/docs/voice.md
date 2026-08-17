# Voice — speech-to-text and text-to-speech

Users can speak a query instead of typing it, and have replies read back, in
**English, Hindi, Kannada and Telugu**. Speech services come from
[Bhashini](https://bhashini.gov.in) (AI4Bharat / ULCA Dhruva), the same provider
Mitra uses — this is a port of Mitra's stack, not a new design.

Off by default. `VOICE_ENABLED=0` means the container builds neither the
Bhashini client nor the object store, and every `/api/voice` endpoint answers
`503 VOICE_DISABLED`, which the frontend reads as "hide the mic and speaker".

## The shape of it

```
POST /api/voice/upload-url   {conversation_id, content_type} -> {uploadUrl, objectKey, ...}
PUT  <uploadUrl>             the browser sends bytes STRAIGHT TO THE BUCKET
POST /api/voice/transcribe   {objectKey, language}           -> {transcript, language}
POST /api/voice/speak        {text, language}                -> {audio, format}
```

Audio never passes through this API. A worker thread here holds a database
connection for the whole request (`THREADPOOL_SIZE <= DB_POOL_SIZE`), and an
upload is the slowest, least predictable part of the cycle — a user on bad wifi
would hold one for its duration.

**The chat pipeline is untouched.** A transcript lands in the composer for the
user to review and correct, and is only then sent through the normal
`/api/chat`; synthesis consumes text that pipeline already produced. Nothing
here reaches `OrchestrationService`, so `/api/chat`'s pinned contract and its
characterisation fixtures are unaffected.

**Non-English is translated before it reaches the composer.** The LLM works in
English, so a Hindi recording is transcribed in Hindi and then translated — which
is what keeps `/api/chat` monolingual. The language picker is therefore a *voice*
setting, not a conversation setting.

## Switching cloud provider

**A `.env` change, not a code change.** One interface
(`app/integrations/storage/base.py`), one driver per protocol, chosen by
`CLOUD_STORAGE_PROVIDER` at boot.

| Value | Driver | Notes |
|---|---|---|
| `aws`, `s3`, `oci`, `minio` | `S3ObjectStore` (boto3) | one driver; `CLOUD_ENDPOINT` is what distinguishes them |
| `gcp`, `gcs`, `gcloud`, `google` | `GcsObjectStore` | V4 signed URLs. `gcloud` is what the ELEVATE Node services write |
| `local` | `LocalObjectStore` | **development only** |

`CLOUD_STORAGE_PROVIDER` empty falls back to a bare `CLOUD_STORAGE` key, which
some ELEVATE `.env` files set instead. Azure is deliberately **not** in the
registry: a name there is a promise `supported_providers()` prints back to the
operator, and pointing one at a missing module would turn a clear "unsupported
provider" message into a `ModuleNotFoundError` at boot.

### Private and public buckets

`CLOUD_STORAGE_BUCKET_TYPE` changes how objects are **read**, and nothing else:

| | `private` (default) | `public` |
|---|---|---|
| `download_url()` | signed, expires after `expires_s` | the stable unsigned URL |
| anonymous `GET` | **403** | **200** |
| upload | **always presigned** | **always presigned** |

The write path is identical on both. A bucket being publicly *readable* is
never a reason to accept anonymous writes — that would let anyone put anything
at any key.

Voice recordings are user speech. On a public bucket they are readable by
anyone who guesses a key, so `private` is correct for real users; the startup
log emits a warning naming the bucket when voice runs against a public one.
Both are supported, and only one is a good idea.

`PUBLIC_ASSET_BUCKETNAME` is carried for convention-compatibility with the Node
services and is unread here — it is the second-bucket slot that convention
reserves, reachable by passing a bucket name to the factory if a future feature
needs one.

What `ACCOUNTNAME` and `SECRET` mean depends on the provider — the price of one
name per slot in a convention shared with the Node services:

```bash
# AWS S3
CLOUD_STORAGE_PROVIDER=aws
CLOUD_STORAGE_ACCOUNTNAME=AKIA...          # access key id
CLOUD_STORAGE_SECRET=wJalr...              # secret access key
CLOUD_STORAGE_REGION=ap-south-1
CLOUD_STORAGE_BUCKETNAME=saarthi-voice

# Google Cloud Storage
CLOUD_STORAGE_PROVIDER=gcp
CLOUD_STORAGE_ACCOUNTNAME=svc@project.iam.gserviceaccount.com
CLOUD_STORAGE_SECRET={"type":"service_account", ...}   # the whole JSON, one line
CLOUD_STORAGE_BUCKETNAME=saarthi-voice
# NOT a region. GCP has no project slot in this convention, so REGION carries
# the PROJECT ID -- required when SECRET is a bare private key rather than the
# full JSON. Without it the client is built with project=None and raises
# "Project was not passed and could not be determined" at boot.
CLOUD_STORAGE_REGION=my-gcp-project

# OCI Object Storage / MinIO / any S3-compatible service
CLOUD_STORAGE_PROVIDER=oci
CLOUD_ENDPOINT=https://<namespace>.compat.objectstorage.ap-mumbai-1.oraclecloud.com
```

Leaving **both** `ACCOUNTNAME` and `SECRET` empty under an S3 provider uses the
IAM role / instance credential chain, which is preferable in production. Setting
exactly one of them is treated as unconfigured and logs a warning — passing a key
with no secret otherwise fails deep inside the first API call with a message that
reads like an outage.

Adding Azure is one class plus one line in `_STORES`.

### Bucket setup

* **Private.** Access is by signed URL only; no public read.
* **CORS must allow `PUT`** from every origin in `FRONTEND_ORIGINS`. The browser
  uploads to the *storage* origin, so the bucket's rules matter as much as this
  app's. (Under `local` the upload comes back here instead, which is the one
  reason `PUT` is in `main.py`'s `allow_methods`.)
* **Lifecycle rule expiring `voice/*` after 24–48h.** Recordings are user speech
  — PII. The app deletes each object right after transcribing, but that does not
  run if the process dies mid-request, so the bucket rule is the real guarantee.

### Extra dependency for GCS

`google-cloud-storage` is deliberately not in `requirements.txt`: it is imported
lazily inside the GCP branch, so an AWS deployment does not pay to install it.

```bash
uv pip install google-cloud-storage
```

### Verified end to end

Both drivers have been run against live buckets — real objects, real Bhashini,
real ffmpeg — plus the browser leg in Chromium with a fake microphone fed real
synthesised speech:

| Run | Result |
|---|---|
| `aws` / private (`saarthi-voice-qa`) | 16/16 — anonymous `GET` **403**, signed URL 200 |
| `aws` / public | 16/16 — anonymous `GET` **200**, `download_url` unsigned and stable |
| `gcp` / private (via the `gcloud` alias) | 16/16 — V4 signed URLs, anonymous `GET` **403** |
| `local` | 12/12 |
| browser → real S3 | 10/10 — cross-origin `PUT`, transcript `"How do I start a new project?"` |

The GCS **browser** leg was not verifiable: the available service account holds
`storage.objects.{create,get,delete,list}` but not `storage.buckets.get`, so the
bucket's CORS can be neither read nor set. The backend leg needs no CORS and is
unaffected. Adding a CORS rule allowing `PUT` from the frontend origins is the
one outstanding step before a browser can upload to GCS.

## Development, with no cloud account

```bash
CLOUD_STORAGE_PROVIDER=local
LOCAL_STORAGE_DIR=var/storage
VOICE_ENABLED=1
```

Recordings land on disk and `presign_put` returns a URL for
`PUT /api/voice/upload-local/{key}` on this service, carrying an HMAC of the key
and an expiry. That route is **unauthenticated** — a presigned URL is used by a
bare `fetch` with no `Authorization` header — so the signature is what stands in
for auth, and without it the route would be a write-anything-anywhere endpoint.
The signing key is random per process and held only in memory, so a restart
invalidates outstanding URLs. Under any other provider the route 404s.

## ffmpeg

**A system binary, and a hard requirement.** Browsers record WebM/Opus (MP4/AAC
on Safari); Bhashini ASR wants 16 kHz mono PCM WAV.

```bash
brew install ffmpeg          # macOS
apt-get install -y ffmpeg    # Debian/Ubuntu — add this to the deployment image
```

A missing binary only *warns* at boot rather than aborting it: it is the one
piece a working `make install` cannot guarantee, and refusing to start the whole
app over a feature that may not be exercised would be the wrong trade. It fails
at request time with `ASR_FAILED`.

## Behaviour worth knowing

* **ASR is chunked and parallel.** Dhruva's synchronous ASR degrades on long
  clips, so audio is split into `VOICE_CHUNK_DURATION_S` pieces and transcribed
  across `VOICE_ASR_MAX_WORKERS` threads, then reassembled *by chunk index*. A
  single failed chunk loses a few seconds of speech rather than the whole
  recording — the user can see and fix a gap in the composer.
* **TTS is chunked and sequential.** `VOICE_TTS_BYTE_LIMIT` is Bhashini's
  per-request ceiling in **UTF-8 bytes, not characters** — Indic scripts run
  3 bytes/char, so 4800 is roughly 1600 Devanagari characters. Longer text splits
  on `।.?!`, synthesises in order, and merges with a header-aware WAV concat.
* **Markdown is stripped before synthesis** (`bhashini/text.py`), or the
  synthesiser reads asterisks, pipes and URLs aloud. The regex order in that file
  is load-bearing and commented as such.
* **The browser gates silence** before uploading (RMS < 0.02), so an accidental
  tap costs nothing. Mitra does the same.
* **Latency is roughly recording length ÷ parallelism, plus upload.** Bhashini
  ASR is batch, not streaming — there are no live partial transcripts.
* **Dhruva has no published SLA.** Every voice failure degrades to "type
  instead"; none of it can block a turn.

## Error codes

| Code | Status | Meaning |
|---|---|---|
| `VOICE_DISABLED` | 503 | `VOICE_ENABLED=0` — the UI hides the controls |
| `INVALID_REQUEST` | 400 | missing `text`/`conversation_id`, or an unsupported language |
| `NOT_FOUND` | 404 | unknown conversation, or a key the caller does not own |
| `AUDIO_TOO_LARGE` | 413 | over `VOICE_MAX_AUDIO_BYTES` |
| `ASR_FAILED` / `TTS_FAILED` | 502 | Bhashini failed, or the audio was unreadable |
| `UPSTREAM_TIMEOUT` | 504 | Dhruva did not answer — the frontend offers a retry |
| `STORAGE_ERROR` | 502 | the bucket could not be reached |

## Security

* **Object keys are server-generated** (`voice/{conversation_id}/{uuid}.{ext}`)
  and re-checked on the way back in. A client-supplied key would be an
  arbitrary-write primitive, and the embedded conversation id is what makes the
  ownership check possible.
* **No SSRF surface.** Mitra's `/api/asr/` takes `{s3Url}` and fetches whatever
  it is given, which needs an allowlist to be safe. Here the request carries an
  opaque *key* into a bucket fixed by configuration — there is no
  attacker-controlled destination at all. This is the one deliberate divergence
  from Mitra's request shape.
* Ownership failures answer **404, never 403**, so a key cannot be used to
  confirm which conversations exist.
* ffmpeg runs on untrusted input: argument **list**, never `shell=True`, with an
  explicit timeout and temp files in a `TemporaryDirectory`.
* Size is checked against object **metadata** before the body is buffered, so an
  oversized upload costs a HEAD rather than a download.
* **The Bhashini credentials currently in `.env` came from `saathi-backend` and
  should be rotated.** They are committed there in plaintext, and the inference
  key is additionally hardcoded in that repo's `base_translation.py`.
