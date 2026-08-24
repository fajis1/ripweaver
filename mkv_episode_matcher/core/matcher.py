import tempfile
from collections import Counter
from pathlib import Path

from loguru import logger

from mkv_episode_matcher.core.models import MatchCandidate, MatchResult, SubtitleFile
from mkv_episode_matcher.core.providers.asr import ASRProvider
from mkv_episode_matcher.core.subtitle_releases import release_match_priority
from mkv_episode_matcher.core.utils import (
    SubtitleReader,
    clean_text,
    extract_audio_chunk,
    get_video_duration,
)


class MultiSegmentMatcher:
    def __init__(self, asr_provider: ASRProvider, temp_dir: Path | None = None):
        self.asr = asr_provider
        self.temp_dir = temp_dir or Path(tempfile.gettempdir()) / "mkv_matcher_chunks"
        self.temp_dir.mkdir(exist_ok=True, parents=True)
        self.chunk_duration = 30
        self.min_confidence = 0.6
        self.last_decision_trace: dict[str, object] = {}
        self._last_segment_trace: dict[str, object] = {}

    @staticmethod
    def _bounded_label(value: object, limit: int = 200) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = " ".join(value.split())[:limit]
        return cleaned or None

    def _process_chunk(  # noqa: C901 - bounded extraction/matching state machine
        self,
        video_path: Path,
        start_time: float,
        reference_subs: list[SubtitleFile],
        chunk_index: int = 0,
        total_chunks: int = 1,
        phase_callback=None,
    ) -> list[MatchCandidate]:
        """Process a single chunk: Extract -> Transcribe -> Match against all subs."""
        chunk_path = self.temp_dir / f"{video_path.stem}_{start_time}.wav"
        self._last_segment_trace = {
            "segment_index": chunk_index,
            "sample_start_seconds": round(float(start_time), 3),
            "sample_duration_seconds": self.chunk_duration,
            "reference_variant_count": len(reference_subs),
            "segment_threshold": self.min_confidence,
            "status": "started",
            "candidate_evaluations": [],
        }
        try:
            # Emit extraction phase
            if phase_callback:
                phase_callback(
                    "extracting_audio",
                    f"🎤 Extracting audio segment {chunk_index + 1}/{total_chunks}...",
                )

            extract_audio_chunk(video_path, start_time, self.chunk_duration, chunk_path)

            # Emit transcription phase
            if phase_callback:
                phase_callback(
                    "transcribing", f"🔊 Transcribing {self.chunk_duration}s segment..."
                )

            transcription = self.asr.transcribe(chunk_path)

            # Clean transcription
            clean_trans = clean_text(transcription)
            if len(clean_trans) < 10:
                self._last_segment_trace.update({
                    "status": "unusable_transcript",
                    "reason": "transcript_too_short",
                    "transcript_character_count": len(clean_trans),
                    "transcript_word_count": len(clean_trans.split()),
                })
                logger.info(
                    "Chunk transcription unusable at {:.1f}s: characters={} words={}",
                    start_time,
                    len(clean_trans),
                    len(clean_trans.split()),
                )
                return []
            logger.debug(
                "Chunk transcription metrics at {:.1f}s: characters={} words={}",
                start_time,
                len(clean_trans),
                len(clean_trans.split()),
            )

            # Emit matching phase
            if phase_callback:
                phase_callback(
                    "comparing",
                    f"🔍 Comparing against {len(reference_subs)} reference subtitles...",
                )

            candidates_by_episode: dict[str, MatchCandidate] = {}
            scored_by_episode: dict[str, MatchCandidate] = {}
            best_score = 0.0
            best_episode = None
            best_release_match = "unresolved"
            for sub in reference_subs:
                # Load text for this time window
                # Note: SubtitleReader.extract_chunk reads file every time.
                # Optimization: Cache full subtitle content in memory for the session?
                # For now, rely on OS file caching.
                if not sub.content:
                    sub.content = SubtitleReader.read_srt_file(sub.path)

                ref_text = " ".join(
                    SubtitleReader.extract_subtitle_chunk(
                        sub.content, start_time, start_time + self.chunk_duration
                    )
                )
                ref_text = clean_text(ref_text)

                if not ref_text:
                    continue

                score = self.asr.calculate_match_score(clean_trans, ref_text)
                if score > best_score:
                    best_score = score
                    best_episode = sub.episode_info.s_e_format
                    best_release_match = sub.release_match
                candidate = MatchCandidate(
                    episode_info=sub.episode_info,
                    confidence=score,
                    reference_file=sub.path,
                    subtitle_release_name=sub.release_name,
                    subtitle_release_match=sub.release_match,
                )
                episode_id = sub.episode_info.s_e_format
                previous_score = scored_by_episode.get(episode_id)
                if previous_score is None or (
                    candidate.confidence,
                    release_match_priority(candidate.subtitle_release_match),
                ) > (
                    previous_score.confidence,
                    release_match_priority(previous_score.subtitle_release_match),
                ):
                    scored_by_episode[episode_id] = candidate
                if score > self.min_confidence:
                    previous = candidates_by_episode.get(episode_id)
                    if previous is None or (
                        candidate.confidence,
                        release_match_priority(candidate.subtitle_release_match),
                    ) > (
                        previous.confidence,
                        release_match_priority(previous.subtitle_release_match),
                    ):
                        candidates_by_episode[episode_id] = candidate

            candidates = list(candidates_by_episode.values())
            ranked_scores = sorted(
                scored_by_episode.values(),
                key=lambda candidate: (
                    candidate.confidence,
                    release_match_priority(candidate.subtitle_release_match),
                    candidate.episode_info.s_e_format,
                ),
                reverse=True,
            )
            self._last_segment_trace.update({
                "status": "qualified" if candidates else "below_threshold",
                "reason": (
                    "candidate_exceeded_segment_threshold"
                    if candidates
                    else "no_candidate_exceeded_segment_threshold"
                ),
                "transcript_character_count": len(clean_trans),
                "transcript_word_count": len(clean_trans.split()),
                "episode_candidate_count": len(ranked_scores),
                "qualifying_candidate_count": len(candidates),
                "best_episode_id": (
                    ranked_scores[0].episode_info.s_e_format if ranked_scores else None
                ),
                "best_score": (
                    round(float(ranked_scores[0].confidence), 6)
                    if ranked_scores
                    else 0.0
                ),
                "candidate_evaluations": [
                    {
                        "rank": rank,
                        "candidate_episode_id": candidate.episode_info.s_e_format,
                        "candidate_episode_title": self._bounded_label(
                            candidate.episode_info.title
                        ),
                        "score": round(float(candidate.confidence), 6),
                        "segment_threshold": self.min_confidence,
                        "qualified": candidate.confidence > self.min_confidence,
                        "subtitle_release_match": candidate.subtitle_release_match,
                        "subtitle_release_name": self._bounded_label(
                            candidate.subtitle_release_name, 240
                        ),
                    }
                    for rank, candidate in enumerate(ranked_scores, start=1)
                ],
            })
            logger.info(
                "Matcher segment summary: segment={} start_seconds={:.1f} "
                "variants={} episodes={} qualifying={} best_episode={} "
                "best_score={:.3f} threshold={:.3f} release_match={}",
                chunk_index + 1,
                start_time,
                len(reference_subs),
                len(ranked_scores),
                len(candidates),
                best_episode or "none",
                best_score,
                self.min_confidence,
                best_release_match,
            )
            if not candidates:
                logger.info(
                    "No candidate exceeded threshold at {:.1f}s: "
                    "words={} best_episode={} best_score={:.3f} threshold={:.3f} "
                    "release_match={}",
                    start_time,
                    len(clean_trans.split()),
                    best_episode or "none",
                    best_score,
                    self.min_confidence,
                    best_release_match,
                )
            return candidates

        except Exception as error:
            self._last_segment_trace.update({
                "status": "failed",
                "reason": "segment_processing_failed",
                "error_type": type(error).__name__,
            })
            logger.error(
                "Error processing chunk at {:.1f}s: {}",
                start_time,
                type(error).__name__,
            )
            return []
        finally:
            if chunk_path.exists():
                chunk_path.unlink()

    def match(  # noqa: C901 - six-window consensus state machine
        self,
        video_path: Path,
        reference_subs: list[SubtitleFile],
        phase_callback=None,
        *,
        acceptance_threshold: float | None = None,
    ) -> MatchResult | None:
        duration = get_video_duration(video_path)
        required_confidence = (
            float(acceptance_threshold)
            if acceptance_threshold is not None
            else self.min_confidence
        )
        self.last_decision_trace = {
            "schema_version": 1,
            "policy": "multi_segment_dialogue_v1",
            "segment_threshold": self.min_confidence,
            "reference_variant_count": len(reference_subs),
            "duration_seconds": round(float(duration), 3),
            "segments": [],
            "decision": "review",
            "reason": "matching_started",
        }
        if duration < 60:
            self.last_decision_trace.update({
                "decision": "review",
                "reason": "video_too_short",
            })
            logger.warning(f"Video too short: {duration}s")
            return None

        # Strategy: 3 primary checkpoints with fallbacks for empty segments
        # Avoid intro (0-120s usually).
        # Primary checkpoints: 15% (after intro), 50% (middle), 85% (end).
        primary_checkpoints = [duration * 0.15, duration * 0.50, duration * 0.85]

        # Fallback checkpoints for when primary segments fail
        fallback_checkpoints = [
            duration * 0.25,
            duration * 0.35,
            duration * 0.65,
            duration * 0.75,
        ]

        # Combine and filter checkpoints
        all_checkpoints = primary_checkpoints + fallback_checkpoints
        checkpoints = [t for t in all_checkpoints if t < duration - 10]

        # Limit total attempts to prevent excessive processing
        checkpoints = checkpoints[:6]
        total_checkpoints = len(checkpoints)

        # Parallel processing of chunks?
        # ASR might be GPU bound and not parallelizable easily within one process due to GIL/VRAM.
        # But extraction is CPU/IO.
        # We'll do sequential for now to be safe with VRAM users.
        # "Faster-Whisper" releases GIL mostly, but VRAM contention is real.

        all_candidates: list[MatchCandidate] = []
        successful_segments = 0
        empty_segments = 0

        for i, t in enumerate(checkpoints):
            logger.info(f"Checking segment {i + 1}/{total_checkpoints} at {t:.1f}s")

            candidates = self._process_chunk(
                video_path,
                t,
                reference_subs,
                chunk_index=i,
                total_chunks=total_checkpoints,
                phase_callback=phase_callback,
            )
            segments = self.last_decision_trace.get("segments")
            if isinstance(segments, list):
                segments.append(dict(self._last_segment_trace))

            if not candidates:
                empty_segments += 1
                logger.debug(
                    "No qualifying candidate at {:.1f}s (segment {})",
                    t,
                    i + 1,
                )
                continue

            successful_segments += 1
            # Sort candidates by score
            candidates.sort(key=lambda x: x.confidence, reverse=True)
            top_match = candidates[0]

            logger.debug(
                f"Top match at {t}s: {top_match.episode_info.s_e_format} ({top_match.confidence:.2f})"
            )

            all_candidates.extend(candidates)

        initial_votes = Counter(
            candidate.episode_info.s_e_format for candidate in all_candidates
        )
        initial_score_sums: dict[str, float] = {}
        for candidate in all_candidates:
            episode_id = candidate.episode_info.s_e_format
            initial_score_sums[episode_id] = (
                initial_score_sums.get(episode_id, 0.0) + candidate.confidence
            )
        initial_winner = (
            max(
                initial_votes,
                key=lambda episode_id: (
                    initial_votes[episode_id],
                    initial_score_sums[episode_id],
                ),
            )
            if initial_votes
            else None
        )
        initial_winner_score = (
            max(
                candidate.confidence
                for candidate in all_candidates
                if candidate.episode_info.s_e_format == initial_winner
            )
            if initial_winner is not None
            else 0.0
        )
        supplemental_reason = (
            "no_initial_qualifying_candidate"
            if initial_winner is None
            else "initial_consensus_insufficient"
            if initial_votes[initial_winner] < 2
            else "initial_winner_below_engine_threshold"
            if initial_winner_score < required_confidence
            else None
        )
        supplemental_attempted = supplemental_reason is not None
        supplemental_count = 0
        if supplemental_attempted:
            # Every unresolved initial pass gets one bounded set of deliberately
            # offset windows. This covers zero-candidate extended scenes, a
            # single unconfirmed anchor, and a consensus whose best score still
            # cannot meet the caller's final engine threshold.
            supplemental_checkpoints = [
                duration * fraction
                for fraction in (0.07, 0.20, 0.30, 0.43, 0.75, 0.93)
                if duration * fraction < duration - 10
            ]
            for offset, t in enumerate(supplemental_checkpoints, start=1):
                logger.info(
                    "Checking supplemental segment {}/{} at {:.1f}s",
                    offset,
                    len(supplemental_checkpoints),
                    t,
                )
                candidates = self._process_chunk(
                    video_path,
                    t,
                    reference_subs,
                    chunk_index=total_checkpoints + offset - 1,
                    total_chunks=total_checkpoints + len(supplemental_checkpoints),
                    phase_callback=phase_callback,
                )
                segments = self.last_decision_trace.get("segments")
                if isinstance(segments, list):
                    segment_trace = dict(self._last_segment_trace)
                    segment_trace["phase"] = "offset-six-window-retry"
                    segments.append(segment_trace)
                supplemental_count += 1
                if not candidates:
                    empty_segments += 1
                    continue
                successful_segments += 1
                candidates.sort(
                    key=lambda candidate: candidate.confidence, reverse=True
                )
                all_candidates.extend(candidates)

        self.last_decision_trace.update({
            "supplemental_attempted": supplemental_attempted,
            "supplemental_segment_count": supplemental_count,
            "supplemental_reason": supplemental_reason,
        })

        logger.info(
            f"Processed {successful_segments} successful segments, {empty_segments} empty segments"
        )

        # Voting Logic
        if not all_candidates:
            self.last_decision_trace.update({
                "decision": "review",
                "reason": "no_candidate_exceeded_segment_threshold",
                "successful_segment_count": successful_segments,
                "empty_segment_count": empty_segments,
            })
            return None

        # Group by Episode ID (SxxExx)
        vote_counter = Counter()
        score_sum = {}

        for c in all_candidates:
            key = c.episode_info.s_e_format
            vote_counter[key] += 1
            if key not in score_sum:
                score_sum[key] = 0.0
            score_sum[key] += c.confidence

        # Winner is the one with most votes. Tie-break with avg confidence.
        best_ep = None
        max_votes = 0

        for ep_key, votes in vote_counter.items():
            if votes > max_votes:
                max_votes = votes
                best_ep = ep_key
            elif votes == max_votes:
                # Tie break
                if best_ep and score_sum[ep_key] > score_sum[best_ep]:
                    best_ep = ep_key

        if best_ep:
            # Reconstruct result based on the episode key
            # Find a candidate that matches this key to get details
            # Ideally return the one with highest confidence
            winning_candidates = [
                c for c in all_candidates if c.episode_info.s_e_format == best_ep
            ]
            best_candidate = max(winning_candidates, key=lambda c: c.confidence)
            ranked_episodes = sorted(
                vote_counter,
                key=lambda episode_id: (
                    vote_counter[episode_id],
                    score_sum[episode_id],
                    episode_id,
                ),
                reverse=True,
            )
            runner_up_episode = ranked_episodes[1] if len(ranked_episodes) > 1 else None
            runner_up_candidates = (
                [
                    candidate
                    for candidate in all_candidates
                    if candidate.episode_info.s_e_format == runner_up_episode
                ]
                if runner_up_episode is not None
                else []
            )
            runner_up_score = max(
                (candidate.confidence for candidate in runner_up_candidates),
                default=0.0,
            )
            self.last_decision_trace.update({
                "decision": "candidate",
                "reason": (
                    "offset_segment_vote_winner"
                    if supplemental_attempted
                    else "segment_vote_winner"
                ),
                "selected_episode_id": best_ep,
                "selected_episode_title": self._bounded_label(
                    best_candidate.episode_info.title
                ),
                "selected_score": round(float(best_candidate.confidence), 6),
                "selected_vote_count": int(vote_counter[best_ep]),
                "selected_score_sum": round(float(score_sum[best_ep]), 6),
                "runner_up_episode_id": runner_up_episode,
                "runner_up_score": round(float(runner_up_score), 6),
                "runner_up_vote_count": (
                    int(vote_counter[runner_up_episode])
                    if runner_up_episode is not None
                    else 0
                ),
                "successful_segment_count": successful_segments,
                "empty_segment_count": empty_segments,
                "subtitle_release_match": best_candidate.subtitle_release_match,
                "subtitle_release_name": self._bounded_label(
                    best_candidate.subtitle_release_name, 240
                ),
            })
            logger.info(
                "Matcher decision: reason=segment_vote_winner episode={} "
                "score={:.3f} votes={} runner_up={} runner_up_score={:.3f} "
                "runner_up_votes={}",
                best_ep,
                best_candidate.confidence,
                vote_counter[best_ep],
                runner_up_episode or "none",
                runner_up_score,
                vote_counter[runner_up_episode] if runner_up_episode is not None else 0,
            )

            return MatchResult(
                episode_info=best_candidate.episode_info,
                confidence=best_candidate.confidence,
                matched_file=video_path,
                matched_time=0,
                chunk_index=-1,  # Consensus
                model_name="consensus",
                original_file=video_path,
                subtitle_release_name=best_candidate.subtitle_release_name,
                subtitle_release_match=best_candidate.subtitle_release_match,
                decision_trace=dict(self.last_decision_trace),
            )

        self.last_decision_trace.update({
            "decision": "review",
            "reason": "vote_winner_unavailable",
            "successful_segment_count": successful_segments,
            "empty_segment_count": empty_segments,
        })
        return None
