# Third-party runtime components, services, and data

The deployment archive does not bundle model weights or llama.cpp source. The
EC2 installer downloads/builds them at deployment time (unchanged from
v0.3.x): Qwen/Qwen3-0.6B-GGUF (Apache-2.0), llama.cpp (MIT), faster-whisper,
Silero VAD + YAMNet filter models.

## Radio Browser (https://www.radio-browser.info)

Community-run, free radio station directory used as the primary catalogue
source. Accessed only from the EC2 backend through the distributed API
mirrors (DNS SRV `_api._tcp.radio-browser.info`, fallback
`all.api.radio-browser.info` + reverse lookup). Never called from the browser.
Etiquette honored: descriptive User-Agent
(`FireMudRadioMonitor/0.4 (+EC2 pilot)`), mirror randomization and failover,
bounded timeouts, response caching (countries 6 h, search 10 min, station
detail 5 min), `stationuuid`/`countrycode` as durable keys, and
`/json/url/{stationuuid}` for playback so station click counters stay
accurate. The full `all.json` dump is not used as a search source.

## radio-database (curated override/deletion repository)

Community contribution repo snapshot (`radio-database-main.zip`) providing
419 station override records and 354 deletion UUIDs. Bundled as
`app/data/radio_database_overrides.json` and `radio_database_deletions.json`;
regenerate with `tools/build_radio_database_overlay.py`.

## FFmpeg / ffprobe

Used on EC2 only, as the unprivileged `radio` user with hard timeouts, to
probe SSRF-validated stream URLs and run the capture pipeline.

## Station streams

Stream URLs come from Radio Browser records and stay on the backend
(`managed_stations.stream_url_resolved`); they are never sent to the browser.
Preview audio proxies through FastAPI with a 60-second cap.
