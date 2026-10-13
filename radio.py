"""Radio eligibility and weighting, independent of Discord and Lavalink."""


def eligible(ratings, listeners, threshold, ignore_unrated):
    for user_id in listeners:
        minimum = threshold.get(user_id, 0) if isinstance(threshold, dict) else threshold
        score = ratings.get(user_id)
        if score is None:
            if not ignore_unrated:
                return False
        elif score < minimum:
            return False
    return True


def weight(play_count, ratings):
    scores = [score for score in ratings.values() if score >= 0]
    average = sum(scores) / len(scores) if scores else 0
    return 100 / ((play_count or 0) + 1) + average * 10


def cycle_candidates(songs, ratings, listeners, threshold, ignore_unrated,
                     history, unavailable, last_song=None):
    matching = [song for song in songs if song[0] not in unavailable
                and eligible(ratings.get(song[0], {}), listeners, threshold, ignore_unrated)]
    candidates = [song for song in matching if song[0] not in history]
    if not candidates and matching:
        history.clear()
        candidates = matching
        if len(candidates) > 1:
            candidates = [song for song in candidates if song[0] != last_song]
    return candidates
